"""Select the newest authenticated evidence for one key (MB5).

QS ``select_artifact.select_source/resolve_evidence`` + BP ``newest_exact_source``: a nominated
artifact id is re-authenticated and never replaced by a fallback; otherwise the newest
``mb-handoff--<key>--a*`` whose owner run is authenticated for the expected subject commit, else
the newest ``mb-cache--<key>--<subject>`` owned by a successful Pages run. No admissible artifact
raises :class:`mod_base.errors.Unavailable` (exit 3).

Source runs "for a subject" are the ``config.source.workflow`` runs of this repository, bound to
that workflow's id, that ran on the **canonical branch** (Block Pops: only the protected
controller ever produces evidence, even for a release branch; a release branch's own copy of the
workflow is never a source) and that name the subject commit: by exactly the configured
``source.display_title`` when there is one (Block Pops attests other commits from the canonical
controller), else by ``head_sha``. Their event must be in ``events.canonical`` and, when the
subject is not the canonical branch, also in ``events.other`` (Block Pops ``handoff_events``: a
scheduled canonical run never vouches for a release branch). ``select`` is given only the subject
commit, so it resolves whether the subject is the canonical branch: always in ``default-branch``
mode, never for another commit than the protected head, and otherwise (enrolled branches sharing
the protected head) through the branch listing and, if needed, the adapter's ``targets``.

A handoff is only ever the upload of its run's latest attempt (``mb-handoff--<key>--a<run_attempt>``).
With ``source.require_newest_run`` only the newest such run may supply evidence: when it is not
``completed/success`` the selection is refused rather than falling back to an older run (Block
Pops). Families select ``mb-family-handoff--<f>--<key>--a*`` uploads of a successful
``families[].producer.workflow`` run on the default branch at the expected commit, else the newest
``mb-family-cache--<f>--<key>--<commit>``.

Every API failure propagates: an unavailable owner is never mistaken for an invalid one, and no
older candidate is probed after a request failed (QS ``test_pages_selection_api_budget``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from mod_base.adapter import host
from mod_base.errors import MbError, Unavailable
from mod_base.github import artifacts, jobs, runs
from mod_base.github.api import GitHubApi
from mod_base.model import grammar
from mod_base.model import limits as lim
from mod_base.pages import targets
from mod_base.runtime import Invocation
from mod_base.workflow import PAGES_EVENTS, PAGES_WORKFLOW_PATH, find_job

OWNER = "MB5"


#: The exact keys of the ``Selected`` JSON object (``select --output``, ``authenticate --selected-json``).
SELECTED_KEYS = ("kind", "artifact_id", "name", "digest", "size", "run_id", "run_attempt")
#: ``Selected.kind`` values: the ``grammar.parse_artifact_name`` kinds ``select`` may return.
SELECTED_KINDS = ("handoff", "cache", "family-handoff", "family-cache")

#: At most this many owner runs are authenticated for one exact-name or run inventory
#: (QS ``publication_progress.MAX_CANDIDATES``).
MAX_CANDIDATES = 8
#: Bound of the ``head_sha``-filtered source-run listing (one page).
MAX_SUBJECT_RUNS = 100
#: Bound of the canonical-branch listing searched for display-titled runs. The listing is newest
#: first, so a bound can only hide an older run (fail closed: no evidence), never let an older run
#: pass as the newest.
MAX_CANONICAL_RUNS = 300
SUBJECT_PLACEHOLDER = "{subject_commit}"


def _fail(message: str, reason: str = "selection") -> MbError:
    return MbError(message, reason=reason)


def _positive(value: Any, label: str, maximum: int = lim.MAX_RUN_ID) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= maximum:
        raise _fail(f"{label} must be a positive integer no larger than {maximum}")
    return value


@dataclass(frozen=True)
class Selected:
    """The ``select`` outputs; also the ``--selected-json`` document of ``authenticate``.

    JSON form (written by ``select --output F`` as canonical JSON, read by ``authenticate
    --selected-json F``): an object with exactly :data:`SELECTED_KEYS`, the dataclass field names:
    ``kind`` (one of :data:`SELECTED_KINDS`), ``artifact_id``/``size``/``run_id``/``run_attempt``
    (positive integers, never booleans; ``size`` at most ``limits.MAX_RAW_BUNDLE_BYTES``),
    ``name`` (an artifact name that ``grammar.parse_artifact_name`` parses as ``kind``, with the
    same attempt for a handoff) and ``digest`` (``sha256:<64 hex>``)."""

    kind: str
    artifact_id: int
    name: str
    digest: str
    size: int
    run_id: int
    run_attempt: int

    @classmethod
    def parse(cls, value: Any) -> "Selected":
        """Strictly parse the JSON object form (see the class docstring); raises ``MbError``."""

        if not isinstance(value, dict) or set(value) != set(SELECTED_KEYS):
            raise _fail(f"a selected artifact must be an object with exactly {list(SELECTED_KEYS)}")
        kind = value["kind"]
        if not isinstance(kind, str) or kind not in SELECTED_KINDS:
            raise _fail("a selected artifact kind must be one of " + ", ".join(SELECTED_KINDS))
        artifact_id = _positive(value["artifact_id"], "selected artifact id")
        size = _positive(value["size"], "selected artifact size", lim.MAX_RAW_BUNDLE_BYTES)
        run_id = _positive(value["run_id"], "selected run id")
        run_attempt = _positive(value["run_attempt"], "selected run attempt", lim.MAX_RUN_ATTEMPT)
        digest = grammar.require(grammar.DIGEST, value["digest"], "selected artifact digest")
        parsed = grammar.parse_artifact_name(value["name"])
        if parsed is None or parsed.kind != kind:
            raise _fail(f"the selected artifact name is not a {kind} name")
        if kind in ("handoff", "family-handoff") and parsed.attempt != run_attempt:
            raise _fail("the selected handoff name carries another attempt than its owner run")
        return cls(kind=kind, artifact_id=artifact_id, name=parsed.name, digest=digest, size=size, run_id=run_id,
                   run_attempt=run_attempt)

    def to_json(self) -> dict[str, Any]:
        """The JSON object form: exactly :data:`SELECTED_KEYS` mapped to the field values."""

        return {key: getattr(self, key) for key in SELECTED_KEYS}


# -- Source runs (shared with admission and authenticate) ----------------------------------------------


def workflow_id(api: GitHubApi, path: str) -> int:
    """The id of this repository's workflow ``path`` (``GET /actions/workflows/<file>``)."""

    grammar.require(grammar.WORKFLOW_PATH, path, "workflow path")
    value = api.get_json(f"/repos/{api.repository}/actions/workflows/{path.rsplit('/', 1)[1]}")
    if not isinstance(value, dict) or value.get("path") != path:
        raise _fail(f"the workflow record of {path} is malformed", reason="workflow")
    return grammar.require_positive_int(value.get("id"), "workflow id")


def _same_id(run: Mapping[str, Any], expected: int) -> bool:
    value = run.get("workflow_id")
    return not isinstance(value, bool) and isinstance(value, int) and value == expected


def display_title(invocation: Invocation, commit: str) -> str | None:
    """The configured ``source.display_title`` with ``{subject_commit}`` substituted, or ``None``."""

    template = invocation.config.source["display_title"]
    if template is None:
        return None
    return template.replace(SUBJECT_PLACEHOLDER, grammar.require_sha1(commit, "subject commit"))


def subject_events(invocation: Invocation, *, subject_canonical: bool) -> frozenset[str]:
    """The events a source run may have: it runs on the canonical branch (``events.canonical``)
    and, for a subject that is not the canonical branch, also ``events.other`` (BP ``handoff_events``)."""

    events = invocation.config.source["events"]
    allowed = frozenset(events["canonical"])
    return allowed if subject_canonical else allowed & frozenset(events["other"])


def is_success(run: Mapping[str, Any]) -> bool:
    return run.get("status") == "completed" and run.get("conclusion") == "success"


def _head_repository(run: Mapping[str, Any]) -> Any:
    head = run.get("head_repository")
    return head.get("full_name") if isinstance(head, Mapping) else None


def source_run_valid(invocation: Invocation, run: Mapping[str, Any], *, source_workflow_id: int) -> bool:
    """A ``source.workflow`` run (by path and id) of this repository on the canonical branch with a
    valid head. Status, event and subject are not considered."""

    return (run.get("path") == invocation.config.source["workflow"] and _same_id(run, source_workflow_id)
            and _head_repository(run) == invocation.repository
            and run.get("head_branch") == invocation.config.canonical_branch
            and grammar.is_match(grammar.SHA1, run.get("head_sha")))


def names_commit(invocation: Invocation, run: Mapping[str, Any], commit: str) -> bool:
    """``run`` is bound to subject ``commit``: by the configured display title, else by head."""

    title = display_title(invocation, commit)
    return run.get("head_sha") == commit if title is None else run.get("display_title") == title


def source_run_names_subject(invocation: Invocation, run: Mapping[str, Any], commit: str, *,
                             subject_canonical: bool, source_workflow_id: int) -> bool:
    """True when ``run`` is a source run (:func:`source_run_valid`) for subject ``commit`` whose
    event is admissible for the subject (:func:`subject_events`). Status is not considered."""

    event = run.get("event")
    return (source_run_valid(invocation, run, source_workflow_id=source_workflow_id)
            and isinstance(event, str) and event in subject_events(invocation, subject_canonical=subject_canonical)
            and names_commit(invocation, run, commit))


def pages_owner_valid(run: Mapping[str, Any], invocation: Invocation, *, default_branch: str,
                      pages_workflow_id: int, head_sha: str | None = None) -> bool:
    """A successful ``pages.yml`` run (by path and id) of this repository on the default branch
    (at ``head_sha`` when given), started by a Pages event; never the executing run itself."""

    run_id, attempt, event = run.get("id"), run.get("run_attempt"), run.get("event")
    return (run.get("path") == PAGES_WORKFLOW_PATH and _same_id(run, pages_workflow_id) and is_success(run)
            and isinstance(event, str) and event in PAGES_EVENTS and run.get("head_branch") == default_branch
            and _head_repository(run) == invocation.repository
            and grammar.is_match(grammar.SHA1, run.get("head_sha"))
            and (head_sha is None or run.get("head_sha") == head_sha)
            and not isinstance(run_id, bool) and isinstance(run_id, int) and run_id > 0
            and not isinstance(attempt, bool) and isinstance(attempt, int) and 0 < attempt <= lim.MAX_RUN_ATTEMPT
            and str(run_id) != invocation.environ.get("GITHUB_RUN_ID"))


def producer_run_valid(run: Mapping[str, Any], invocation: Invocation, family: Mapping[str, Any], *,
                       default_branch: str, head_sha: str) -> bool:
    """A successful ``families[].producer.workflow`` run of this repository on the default branch
    at ``head_sha`` started by one of ``producer.events``."""

    producer = family["producer"]
    return (run.get("path") == producer["workflow"] and is_success(run)
            and run.get("event") in producer["events"] and run.get("head_branch") == default_branch
            and run.get("head_sha") == head_sha and _head_repository(run) == invocation.repository)


class SourceRuns:
    """One invocation's bounded, memoized view of ``source.workflow``: its workflow id and pages.yml's
    (read lazily, at most once each), and the runs naming each subject. Without a display title a
    subject's runs are one ``branch=<canonical>&head_sha=<commit>`` listing per commit; with one,
    a single canonical-branch listing (newest :data:`MAX_CANONICAL_RUNS`) serves every subject."""

    def __init__(self, api: GitHubApi, invocation: Invocation) -> None:
        self.api = api
        self.invocation = invocation
        self._ids: dict[str, int] = {}
        self._canonical: list[dict[str, Any]] | None = None
        self._by_commit: dict[str, list[dict[str, Any]]] = {}

    def workflow_id(self, path: str) -> int:
        if path not in self._ids:
            self._ids[path] = workflow_id(self.api, path)
        return self._ids[path]

    @property
    def source_workflow_id(self) -> int:
        return self.workflow_id(self.invocation.config.source["workflow"])

    @property
    def pages_workflow_id(self) -> int:
        return self.workflow_id(PAGES_WORKFLOW_PATH)

    def _listed(self, commit: str) -> list[dict[str, Any]]:
        source, canonical = self.invocation.config.source, self.invocation.config.canonical_branch
        if source["display_title"] is None:
            if commit not in self._by_commit:
                self._by_commit[commit] = runs.workflow_runs(self.api, source["workflow"], branch=canonical,
                                                             head_sha=commit, max_items=MAX_SUBJECT_RUNS)
            return self._by_commit[commit]
        if self._canonical is None:
            self._canonical = runs.workflow_runs(self.api, source["workflow"], branch=canonical,
                                                 max_items=MAX_CANONICAL_RUNS)
        return self._canonical

    def for_subject(self, commit: str, *, subject_canonical: bool) -> list[dict[str, Any]]:
        """Every source run for subject ``commit`` (any status), newest first by ``(created_at, id,
        run_attempt)``."""

        grammar.require_sha1(commit, "subject commit")
        listed = self._listed(commit)
        if not listed:
            return []
        identity = self.source_workflow_id
        return [run for run in listed if source_run_names_subject(
            self.invocation, run, commit, subject_canonical=subject_canonical, source_workflow_id=identity)]

    def newest(self, commit: str, *, subject_canonical: bool) -> dict[str, Any]:
        """The newest source run for ``commit``, which must be ``completed/success``: an older
        successful run is never used while a newer one failed or is still running (BP)."""

        candidates = self.for_subject(commit, subject_canonical=subject_canonical)
        if not candidates:
            raise Unavailable(f"no exact source run exists for subject {commit}", reason="newest-run")
        newest = candidates[0]
        if not is_success(newest):
            raise Unavailable(f"the newest source run {newest['id']} for {commit} is not completed/success; "
                              "refusing older evidence", reason="newest-run")
        return newest


def run_upload(api: GitHubApi, run: Mapping[str, Any], name: str, *, max_size: int) -> artifacts.Artifact | None:
    """The single usable artifact ``name`` of ``run`` (not expired, ``0 < size <= max_size``), or
    ``None``. Two artifacts of one name, or metadata naming another head, fail closed."""

    matches = [artifact for artifact in artifacts.list_for_run(api, run["id"]) if artifact.name == name]
    if len(matches) > 1:
        raise _fail(f"run {run['id']} owns {len(matches)} artifacts named {name}")
    if not matches:
        return None
    artifact = matches[0]
    if artifact.head_sha != run.get("head_sha") or artifact.head_branch != run.get("head_branch"):
        raise _fail(f"artifact {artifact.id} names another head than its owner run {run['id']}")
    if artifact.expired or artifact.size > max_size:
        return None
    return artifact


def upload_in_window(artifact: artifacts.Artifact, job: Mapping[str, Any] | None, step_name: str) -> bool:
    """The artifact was created inside the successful step ``step_name`` of ``job`` (the exact
    upload of that attempt; QS ``artifact_valid``)."""

    if job is None or job.get("status") != "completed" or job.get("conclusion") != "success":
        return False
    try:
        jobs.require_successful_step(job, step_name)
        started, completed = jobs.step_window(job, step_name)
    except MbError:
        return False
    created = artifact.order[0]
    return started <= created <= completed


def successful_job(attempt_jobs: list[dict[str, Any]], name: str, run_attempt: int) -> dict[str, Any] | None:
    """The single job named exactly ``name`` in ``run_attempt`` when it completed successfully."""

    try:
        job = find_job(attempt_jobs, name, run_attempt=run_attempt)
    except MbError:
        return None
    return job if job.get("status") == "completed" and job.get("conclusion") == "success" else None


def handoff_job_name(invocation: Invocation, key: str) -> str:
    """``source.handoff_job`` with its optional ``{key}`` placeholder filled."""

    return invocation.config.source["handoff_job"].replace("{key}", grammar.require_key(key))


def _latest_attempt(api: GitHubApi, run_id: int, attempt: int, label: str) -> dict[str, Any]:
    run = runs.get_run(api, run_id)
    if run.get("run_attempt") != attempt:
        raise _fail(f"{label} was uploaded by attempt {attempt}, but run {run_id} is at attempt "
                    f"{run.get('run_attempt')!r}")
    return run


def subject_is_canonical(invocation: Invocation, api: GitHubApi, *, key: str, commit: str) -> bool:
    """Whether ``key``'s subject at ``commit`` is the canonical branch (only the admissible event
    set depends on it). ``default-branch`` mode: always. ``enrolled-branches`` mode: never for
    another commit than the protected head (the canonical subject is the protected head); at the
    head, the canonical branch unless another listed branch shares that commit, in which case the
    adapter's ``targets`` over exactly those branches decides which one ``key`` names."""

    config = invocation.config
    if config.targets["mode"] == "default-branch":
        return True
    if commit != invocation.implementation_sha:
        return False
    heads = targets.branch_heads(api, max_branches=config.targets["max_branches"])
    if heads.get(config.canonical_branch) != commit:
        raise _fail(f"{config.canonical_branch} advanced past the protected head {commit}",
                    reason="stale-implementation")
    sharing = [name for name, head in heads.items() if head == commit]
    if sharing == [config.canonical_branch]:
        return True
    tree = targets.commit_tree_local(invocation.repo_root, commit)
    declared = host.call(invocation, "targets",
                         {"branches": [{"name": name, "commit": commit, "tree": tree} for name in sharing]})
    for target in declared:
        if target["key"] == key:
            return target["subject"]["branch"] == config.canonical_branch
    raise Unavailable(f"the adapter declares no target {key} at {commit}")


# -- Selection ---------------------------------------------------------------------------------------


def _selected(kind: str, artifact: artifacts.Artifact, run_attempt: int) -> Selected:
    return Selected.parse({"kind": kind, "artifact_id": artifact.id, "name": artifact.name, "digest": artifact.digest,
                           "size": artifact.size, "run_id": artifact.run_id, "run_attempt": run_attempt})


def _nominated(invocation: Invocation, api: GitHubApi, sources: SourceRuns, *, key: str,
               family: Mapping[str, Any] | None, nomination: int, commit: str) -> Selected:
    """Re-authenticate an admission's nomination; any doubt fails closed (never a fallback)."""

    default = invocation.config.canonical_branch
    artifact = artifacts.get_artifact(api, nomination)
    kind = "handoff" if family is None else "family-handoff"
    parsed = grammar.parse_artifact_name(artifact.name)
    if (parsed is None or parsed.kind != kind or parsed.key != key
            or (family is not None and parsed.family != family["id"])):
        raise _fail(f"nominated artifact {nomination} is not a {kind} of {key}", reason="nomination")
    maximum = lim.MAX_RAW_BUNDLE_BYTES if family is None else family["handoff_max_bytes"]
    if artifact.expired or artifact.size > maximum:
        raise _fail(f"nominated artifact {nomination} expired or exceeds its size bound", reason="nomination")
    if parsed.attempt is None:
        raise _fail(f"nominated artifact {nomination} names no attempt", reason="nomination")
    run = _latest_attempt(api, artifact.run_id, parsed.attempt, f"nominated artifact {nomination}")
    if artifact.head_sha != run.get("head_sha") or artifact.head_branch != run.get("head_branch"):
        raise _fail(f"nominated artifact {nomination} names another head than its owner run", reason="nomination")
    if family is None:
        canonical = subject_is_canonical(invocation, api, key=key, commit=commit)
        if not is_success(run) or not source_run_names_subject(invocation, run, commit, subject_canonical=canonical,
                                                               source_workflow_id=sources.source_workflow_id):
            raise _fail(f"nominated artifact {nomination} is not owned by a successful source run for {commit}",
                        reason="nomination")
        if invocation.config.source["require_newest_run"]:
            newest = sources.newest(commit, subject_canonical=canonical)
            if (newest["id"], newest["run_attempt"]) != (run["id"], parsed.attempt):
                raise _fail(f"nominated artifact {nomination} is not from the newest source run for {commit}",
                            reason="newest-run")
        job_name, step = handoff_job_name(invocation, key), invocation.config.source["handoff_step"]
    else:
        if not producer_run_valid(run, invocation, family, default_branch=default, head_sha=commit):
            raise _fail(f"nominated artifact {nomination} is not owned by a successful {family['id']} producer run "
                        f"at {commit}", reason="nomination")
        job_name, step = family["producer"]["job"], family["producer"]["step"]
    job = successful_job(jobs.attempt_jobs(api, run["id"], parsed.attempt), job_name, parsed.attempt)
    if not upload_in_window(artifact, job, step):
        raise _fail(f"nominated artifact {nomination} was not uploaded by the exact successful producer step",
                    reason="nomination")
    if artifacts.get_artifact(api, nomination) != artifact:
        raise _fail(f"nominated artifact {nomination} changed during selection", reason="nomination")
    return _selected(kind, artifact, parsed.attempt)


def _newest_handoff(invocation: Invocation, api: GitHubApi, sources: SourceRuns, key: str,
                    commit: str) -> Selected | None:
    canonical = subject_is_canonical(invocation, api, key=key, commit=commit)
    if invocation.config.source["require_newest_run"]:
        search = [sources.newest(commit, subject_canonical=canonical)]
    else:
        search = [run for run in sources.for_subject(commit, subject_canonical=canonical)
                  if is_success(run)][:MAX_CANDIDATES]
    for run in search:
        artifact = run_upload(api, run, grammar.handoff_name(key, run["run_attempt"]),
                              max_size=lim.MAX_RAW_BUNDLE_BYTES)
        if artifact is not None:
            return _selected("handoff", artifact, run["run_attempt"])
    return None


def _newest_family_handoff(invocation: Invocation, api: GitHubApi, family: Mapping[str, Any], key: str,
                           commit: str) -> Selected | None:
    default = invocation.config.canonical_branch
    listing = runs.workflow_runs(api, family["producer"]["workflow"], head_sha=commit, status="success",
                                 max_items=MAX_SUBJECT_RUNS)
    producers = [run for run in listing
                 if producer_run_valid(run, invocation, family, default_branch=default, head_sha=commit)]
    for run in producers[:MAX_CANDIDATES]:
        name = grammar.family_handoff_name(family["id"], key, run["run_attempt"])
        artifact = run_upload(api, run, name, max_size=family["handoff_max_bytes"])
        if artifact is not None:
            return _selected("family-handoff", artifact, run["run_attempt"])
    return None


def _newest_owned_cache(invocation: Invocation, api: GitHubApi, sources: SourceRuns,
                        name: str) -> tuple[artifacts.Artifact, dict[str, Any]] | None:
    """The newest ``name`` artifact (a cache) owned by a successful Pages run on the default branch,
    with that owner; at most :data:`MAX_CANDIDATES` owners are read, newest first."""

    default = invocation.config.canonical_branch
    candidates = [artifact for artifact in artifacts.list_named(api, name)
                  if not artifact.expired and artifact.head_branch == default]
    for artifact in candidates[:MAX_CANDIDATES]:
        run = runs.get_run(api, artifact.run_id)
        if pages_owner_valid(run, invocation, default_branch=default, pages_workflow_id=sources.pages_workflow_id,
                             head_sha=artifact.head_sha):
            return artifact, run
    return None


def select_evidence(invocation: Invocation, *, api: GitHubApi, key: str, family: str | None = None,
                    nomination: int | None = None, expected_subject_commit: str) -> Selected:
    """Return the selected artifact or raise ``Unavailable`` (see module docstring)."""

    key = grammar.require_key(key)
    commit = grammar.require_sha1(expected_subject_commit, "expected subject commit")
    family_config = None
    if family is not None:
        family_config = invocation.config.family(grammar.require_family(family))
    sources = SourceRuns(api, invocation)
    if nomination is not None:
        grammar.require_positive_int(nomination, "nomination")
        return _nominated(invocation, api, sources, key=key, family=family_config, nomination=nomination,
                          commit=commit)
    if family_config is None:
        selected = _newest_handoff(invocation, api, sources, key, commit)
        cache_kind, cache = "cache", grammar.cache_name(key, commit)
    else:
        selected = _newest_family_handoff(invocation, api, family_config, key, commit)
        cache_kind, cache = "family-cache", grammar.family_cache_name(family_config["id"], key, commit)
    if selected is not None:
        return selected
    owned = _newest_owned_cache(invocation, api, sources, cache)
    if owned is not None:
        artifact, run = owned
        if artifact.size > lim.MAX_RAW_BUNDLE_BYTES:
            raise _fail(f"cache {artifact.id} exceeds the bundle bound")
        return _selected(cache_kind, artifact, run["run_attempt"])
    subject = key if family_config is None else f"{family_config['id']} {key}"
    raise Unavailable(f"no authenticated evidence exists for {subject} at {commit}")

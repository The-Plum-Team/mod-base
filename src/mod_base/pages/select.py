"""Select the newest authenticated evidence for one key (MB5).

QS ``select_artifact.select_source/resolve_evidence`` + BP ``newest_exact_source``: a nominated
artifact id is re-authenticated and never replaced by a fallback; otherwise the newest
``mb-handoff--<key>--a*`` whose owner run is authenticated for the expected subject commit, unless
an ``mb-cache--<key>--<subject>`` owned by a successful Pages run **supersedes** it, else the newest
such cache. No admissible artifact raises :class:`mod_base.errors.Unavailable` (exit 3).

**Supersession** (:func:`supersedes`, QS "the newest valid source"). A cache supersedes a handoff
of the same subject when the successful Pages run that owns it was created after the handoff was
uploaded: that publication selected with the handoff already listed, so its cache carries the
handoff's evidence or newer, and its rotation may retire the handoff at any time (SPEC §5.5 retires
exactly the artifacts created before the owner run, the handoff that publication consumed among
them). A later publication at the same subject therefore takes the cache instead of racing that
rotation for the handoff (publication and rotation hold separate locks, D11). The owner's creation,
not the cache's upload, is the bound: a publication that selected before the handoff existed may
upload its cache afterwards, and that cache may carry older evidence (under
``source.require_newest_run`` evidence of an older run, which ``authenticate`` would refuse every
time; for a family, an older carried generation). Such a cache never masks the handoff.

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
``families[].producer.workflow`` run on the default branch at the expected commit, unless an
``mb-family-cache--<f>--<key>--<commit>`` supersedes it, else the newest such family cache; when
neither exists and the family's ``carry_forward`` is set, the same probes walk a bounded
first-parent history below the expected commit, newest first, and the first earlier generation
found is selected for ``family collect`` (the adapter's carry-forward decision plus R5) to accept
or refuse (:class:`FamilyGenerations`). ``family collect --selected-json`` records the selection in
the collected artifact, and ``build`` re-authenticates that record by id, reusing only
:meth:`FamilyGenerations.history`, never the walk. A nomination is re-authenticated exactly and
never walks.

A selected **family handoff** (nominated or not) is then bound to its kit before its collection
(SPEC §1.8, as ``build`` binds every collected leg): it is downloaded by id and digest into a
temporary directory, its envelope is validated (:func:`mod_base.family.envelope.validate_envelope_dir`)
and must name exactly the producer run attempt that uploaded it, and ``envelope.kit`` must be the pin
of ``families[].producer.workflow`` at that run's head (:func:`mod_base.pages.authenticate.kit_binding`).
A mismatch fails the family leg (reason ``kit-binding``) instead of the whole build, and is never a
fallback to another generation (an older one would not be the newest admissible generation). A
family cache needs no second binding here: it exists only because the build of its successful Pages
owner bound that same envelope, which ``refresh`` then rolled forward byte for byte; ``family
collect`` re-proves the binding of either kind from inert objects anyway.

Every API failure propagates: an unavailable owner is never mistaken for an invalid one, and no
older candidate is probed after a request failed (QS ``test_pages_selection_api_budget``).
"""

from __future__ import annotations

import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mod_base.adapter import host
from mod_base.errors import MbError, Unavailable
from mod_base.family.envelope import validate_envelope_dir
from mod_base.github import artifacts, jobs, runs
from mod_base.github.api import GitHubApi
from mod_base.io.bounded_zip import LIMITS_BY_KIND, artifact_limit
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
    (positive integers, never booleans; ``size``, the archive's, at most ``limits.MAX_ARTIFACT_BYTES``),
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
        size = _positive(value["size"], "selected artifact size", lim.MAX_ARTIFACT_BYTES)
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
    a single canonical-branch listing (newest ``limits.MAX_CANONICAL_RUNS``) serves every subject."""

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
                                                             head_sha=commit, max_items=lim.MAX_SUBJECT_RUNS)
            return self._by_commit[commit]
        if self._canonical is None:
            self._canonical = runs.workflow_runs(self.api, source["workflow"], branch=canonical,
                                                 max_items=lim.MAX_CANONICAL_RUNS)
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


def family_archive_limit(family: Mapping[str, Any]) -> int:
    """The largest family handoff or cache archive of the configured ``family``: the archive limit
    of its expanded ``handoff_max_bytes`` plus ``envelope.json`` (:func:`mod_base.io.bounded_zip.artifact_limit`)."""

    return artifact_limit("family-handoff", max_total_bytes=family["handoff_max_bytes"] + lim.MAX_ENVELOPE_BYTES)


def run_upload(api: GitHubApi, run: Mapping[str, Any], name: str, *, max_size: int) -> artifacts.Artifact | None:
    """The single usable artifact ``name`` of ``run`` (not expired, ``0 < size <= max_size``), or
    ``None``. Two artifacts of one name, or metadata naming another head, fail closed. One
    exact-name listing of the run (``artifacts.list_run_named``) finds it, so none of the run's other
    uploads can disturb it."""

    return _single_upload(run, artifacts.list_run_named(api, run["id"], name), name, max_size=max_size)


def _single_upload(run: Mapping[str, Any], inventory: list[artifacts.Artifact], name: str, *,
                   max_size: int) -> artifacts.Artifact | None:
    """:func:`run_upload` over ``run``'s already listed ``inventory``."""

    matches = [artifact for artifact in inventory if artifact.name == name]
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
    maximum = artifact_limit("handoff") if family is None else family_archive_limit(family)
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
                    commit: str) -> tuple[artifacts.Artifact, int] | None:
    """The handoff of the newest successful source run for ``commit`` that holds one (with
    ``source.require_newest_run`` only the newest run's), with its run attempt."""

    canonical = subject_is_canonical(invocation, api, key=key, commit=commit)
    if invocation.config.source["require_newest_run"]:
        search = [sources.newest(commit, subject_canonical=canonical)]
    else:
        search = [run for run in sources.for_subject(commit, subject_canonical=canonical)
                  if is_success(run)][:lim.MAX_CANDIDATES]
    for run in search:
        artifact = run_upload(api, run, grammar.handoff_name(key, run["run_attempt"]),
                              max_size=artifact_limit("handoff"))
        if artifact is not None:
            return artifact, run["run_attempt"]
    return None


def supersedes(owner: Mapping[str, Any], handoff: artifacts.Artifact) -> bool:
    """A cache owned by the successful Pages run ``owner`` supersedes ``handoff`` (of the same
    subject or family leg and commit): ``owner`` was created strictly after the handoff was uploaded
    (module docstring), the very bound below which that run's rotation may retire the handoff."""

    return runs.run_order(owner)[0] > handoff.order[0]


def _newest_owned_cache(invocation: Invocation, api: GitHubApi, sources: SourceRuns, name: str, *,
                        after: artifacts.Artifact | None = None, owners: dict[int, dict[str, Any]] | None = None,
                        consulted: set[int] | None = None, budget: int = lim.MAX_CANDIDATES,
                        max_size: int | None = None) -> tuple[artifacts.Artifact, dict[str, Any]] | None:
    """The newest ``name`` artifact (a cache) owned by a successful Pages run on the default branch,
    with that owner, newest first; with ``after`` (a handoff) only a cache that :func:`supersedes`
    it. A cache uploaded no later than ``after`` cannot (its owner was created before its upload), so
    the newest-first listing is read only down to it. At most ``budget`` distinct owner runs are read,
    counted in ``consulted`` (shared by a whole walk; a run already in it costs nothing more);
    ``owners`` memoizes the run reads of one invocation. The chosen cache must fit ``max_size`` (a
    family cache: :func:`family_archive_limit`), by default its kind's archive limit, or selection
    fails closed."""

    default = invocation.config.canonical_branch
    parsed = grammar.parse_artifact_name(name)
    if parsed is None or parsed.kind not in ("cache", "family-cache"):
        raise _fail(f"{name} is not a cache name", reason="usage")
    maximum = artifact_limit(parsed.kind) if max_size is None else min(max_size, artifact_limit(parsed.kind))
    owners = {} if owners is None else owners
    consulted = set() if consulted is None else consulted
    for artifact in artifacts.list_named(api, name):
        if after is not None and artifact.order[0] <= after.order[0]:
            return None
        if artifact.expired or artifact.head_branch != default:
            continue
        if artifact.run_id not in consulted:
            if len(consulted) >= budget:
                return None
            consulted.add(artifact.run_id)
        if artifact.run_id not in owners:
            owners[artifact.run_id] = runs.get_run(api, artifact.run_id)
        run = owners[artifact.run_id]
        if (pages_owner_valid(run, invocation, default_branch=default, pages_workflow_id=sources.pages_workflow_id,
                              head_sha=artifact.head_sha)
                and (after is None or supersedes(run, after))):
            if artifact.size > maximum:
                raise _fail(f"cache {artifact.id} exceeds its archive bound")
            return artifact, run
    return None


def _newest_evidence(handoff: tuple[artifacts.Artifact, int] | None,
                     owned: tuple[artifacts.Artifact, dict[str, Any]] | None, *, family: bool) -> Selected | None:
    """The superseding cache when there is one, else the handoff (either may be absent)."""

    if owned is not None:
        return _selected("family-cache" if family else "cache", owned[0], owned[1]["run_attempt"])
    if handoff is not None:
        return _selected("family-handoff" if family else "handoff", *handoff)
    return None


class FamilyGenerations:
    """The family generation :func:`select_evidence` picks without a nomination (``build`` uses only
    :meth:`history`; one instance memoizes the reads of every leg of an invocation).

    At the expected commit ``c``: the ``mb-family-handoff--<f>--<key>--a<attempt>`` of the newest
    successful producer run at ``c`` (:func:`producer_run_valid`; one ``head_sha`` listing, at most
    ``limits.MAX_CANDIDATES`` run inventories), unless an ``mb-family-cache--<f>--<key>--<c>`` owned by
    a successful Pages run :func:`supersedes` it, else the newest such family cache (at most
    ``limits.MAX_CANDIDATES`` owners). When neither exists and the family's ``carry_forward`` is set,
    the **carry-forward walk** visits the first-parent commits below ``c``, newest first: at most
    ``limits.GENERATION_PROBES`` of them, read as inert objects from the protected checkout
    (:func:`targets.first_parent_history`; the family and build jobs check out the full history).
    At each earlier commit it applies the same rule to
    the family handoff of a successful producer run at that commit and the family cache named with
    that commit, and returns the first match as an ordinary ``family-handoff``/``family-cache``
    selection; ``family collect`` (the adapter's carry-forward decision plus R5) accepts or refuses
    it. The walk's producer runs come from one listing of the newest ``limits.MAX_SUBJECT_RUNS``
    successful producer runs of the default branch, of which only the newest
    ``limits.MAX_CANDIDATES`` at an earlier commit are inventoried, and the walk reads at most
    ``limits.GENERATION_PROBES`` distinct cache owners. Both bounds count per walk (a memoized
    read counts too), so ``select`` and ``build`` see the same generation for the same API state.
    Nominations never walk (:func:`select_evidence`)."""

    def __init__(self, api: GitHubApi, invocation: Invocation, *, sources: SourceRuns | None = None) -> None:
        self.api = api
        self.invocation = invocation
        self.sources = SourceRuns(api, invocation) if sources is None else sources
        self._at: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._listed: dict[str, list[dict[str, Any]]] = {}
        self._inventories: dict[int, list[artifacts.Artifact]] = {}
        self._owners: dict[int, dict[str, Any]] = {}
        self._histories: dict[str, list[str]] = {}

    def history(self, commit: str) -> list[str]:
        """``commit`` and at most ``limits.GENERATION_PROBES`` first-parent ancestors, newest first."""

        if commit not in self._histories:
            self._histories[commit] = targets.first_parent_history(self.invocation.repo_root, commit,
                                                                   max_commits=lim.GENERATION_PROBES)
        return self._histories[commit]

    def _valid(self, run: Mapping[str, Any], family: Mapping[str, Any], commit: str) -> bool:
        return producer_run_valid(run, self.invocation, family, default_branch=self.invocation.config.canonical_branch,
                                  head_sha=commit)

    def _producers_at(self, family: Mapping[str, Any], commit: str) -> list[dict[str, Any]]:
        workflow = family["producer"]["workflow"]
        if (workflow, commit) not in self._at:
            self._at[workflow, commit] = runs.workflow_runs(self.api, workflow, head_sha=commit, status="success",
                                                            max_items=lim.MAX_SUBJECT_RUNS)
        return [run for run in self._at[workflow, commit] if self._valid(run, family, commit)][:lim.MAX_CANDIDATES]

    def _walk_producers(self, family: Mapping[str, Any], earlier: list[str]) -> dict[str, list[dict[str, Any]]]:
        """Earlier commit -> its producer runs, newest first: the newest ``limits.MAX_CANDIDATES``
        successful default-branch producer runs at any of ``earlier``."""

        workflow = family["producer"]["workflow"]
        if workflow not in self._listed:
            self._listed[workflow] = runs.workflow_runs(self.api, workflow,
                                                        branch=self.invocation.config.canonical_branch,
                                                        status="success", max_items=lim.MAX_SUBJECT_RUNS)
        commits = set(earlier)
        chosen = [run for run in self._listed[workflow]
                  if run.get("head_sha") in commits and self._valid(run, family, run["head_sha"])][:lim.MAX_CANDIDATES]
        grouped: dict[str, list[dict[str, Any]]] = {}
        for run in chosen:
            grouped.setdefault(run["head_sha"], []).append(run)
        return grouped

    def _handoff(self, family: Mapping[str, Any], key: str,
                 producers: list[dict[str, Any]]) -> tuple[artifacts.Artifact, int] | None:
        for run in producers:
            if run["id"] not in self._inventories:
                self._inventories[run["id"]] = artifacts.list_for_run(self.api, run["id"])
            name = grammar.family_handoff_name(family["id"], key, run["run_attempt"])
            artifact = _single_upload(run, self._inventories[run["id"]], name, max_size=family_archive_limit(family))
            if artifact is not None:
                return artifact, run["run_attempt"]
        return None

    def _generation(self, family: Mapping[str, Any], key: str, commit: str, producers: list[dict[str, Any]], *,
                    consulted: set[int], budget: int) -> Selected | None:
        """The generation at one ``commit``: the producers' family handoff unless a family cache named
        with ``commit`` supersedes it, else the newest such family cache (:func:`_newest_owned_cache`)."""

        handoff = self._handoff(family, key, producers)
        owned = _newest_owned_cache(self.invocation, self.api, self.sources,
                                    grammar.family_cache_name(family["id"], key, commit),
                                    after=None if handoff is None else handoff[0], owners=self._owners,
                                    consulted=consulted, budget=budget, max_size=family_archive_limit(family))
        return _newest_evidence(handoff, owned, family=True)

    def newest(self, family: str, key: str, commit: str) -> Selected | None:
        """The family generation of ``key`` for the expected ``commit`` (see the class docstring),
        or ``None`` when no admissible generation exists."""

        config = self.invocation.config.family(grammar.require_family(family))
        key = grammar.require_key(key)
        commit = grammar.require_sha1(commit, "expected subject commit")
        found = self._generation(config, key, commit, self._producers_at(config, commit), consulted=set(),
                                 budget=lim.MAX_CANDIDATES)
        if found is not None or not config["carry_forward"]:
            return found
        earlier = self.history(commit)[1:]
        producers = self._walk_producers(config, earlier) if earlier else {}
        consulted: set[int] = set()
        for candidate in earlier:
            found = self._generation(config, key, candidate, producers.get(candidate, []), consulted=consulted,
                                     budget=lim.GENERATION_PROBES)
            if found is not None:
                return found
            if len(consulted) >= lim.GENERATION_PROBES:
                return None
        return None


def _require_family_kit(invocation: Invocation, api: GitHubApi, family: Mapping[str, Any], key: str,
                        selected: Selected) -> None:
    """Bind a selected family handoff to its kit (module docstring; reason ``kit-binding``): its
    envelope, downloaded by id and digest and validated in a temporary directory, must name exactly
    the producer run attempt that uploaded it (the attempt endpoint), and ``envelope.kit`` must be
    the pin of ``families[].producer.workflow`` at that run's head."""

    # ``authenticate`` imports this module's selection records and run predicates.
    from mod_base.pages.authenticate import kit_binding

    with tempfile.TemporaryDirectory(prefix="mb-select-family-") as directory:
        root = Path(directory) / "selected"
        artifacts.download(api, artifact_id=selected.artifact_id, name=selected.name, digest=selected.digest,
                           size=selected.size, run_id=selected.run_id, output=root,
                           extraction=LIMITS_BY_KIND["family-handoff"])
        envelope = validate_envelope_dir(invocation, root, family=family["id"], key=key)
    producer = runs.get_run_attempt(api, selected.run_id, selected.run_attempt)
    if not producer_run_valid(producer, invocation, family, default_branch=invocation.config.canonical_branch,
                              head_sha=envelope["producer"]["commit"]):
        raise _fail(f"the {family['id']} handoff {selected.artifact_id} of {key} is not owned by a successful "
                    f"{family['producer']['workflow']} run at its envelope's commit", reason="kit-binding")
    kit_binding(api, invocation, manifest=envelope, owner_run=producer, selected_kind="family-handoff")


def select_evidence(invocation: Invocation, *, api: GitHubApi, key: str, family: str | None = None,
                    nomination: int | None = None, expected_subject_commit: str) -> Selected:
    """Return the selected artifact or raise ``Unavailable`` (see module docstring)."""

    key = grammar.require_key(key)
    commit = grammar.require_sha1(expected_subject_commit, "expected subject commit")
    family_config = None
    if family is not None:
        family_config = invocation.config.family(grammar.require_family(family))
    sources = SourceRuns(api, invocation)
    selected: Selected | None
    if nomination is not None:
        grammar.require_positive_int(nomination, "nomination")
        selected = _nominated(invocation, api, sources, key=key, family=family_config, nomination=nomination,
                              commit=commit)
    elif family_config is not None:
        selected = FamilyGenerations(api, invocation, sources=sources).newest(family_config["id"], key, commit)
        if selected is None:
            below = " or on its bounded first-parent history" if family_config["carry_forward"] else ""
            raise Unavailable(f"no authenticated {family_config['id']} generation exists for {key} at {commit}{below}")
    else:
        handoff = _newest_handoff(invocation, api, sources, key, commit)
        owned = _newest_owned_cache(invocation, api, sources, grammar.cache_name(key, commit),
                                    after=None if handoff is None else handoff[0])
        selected = _newest_evidence(handoff, owned, family=False)
        if selected is None:
            raise Unavailable(f"no authenticated evidence exists for {key} at {commit}")
    if family_config is not None and selected.kind == "family-handoff":
        _require_family_kit(invocation, api, family_config, key, selected)
    return selected

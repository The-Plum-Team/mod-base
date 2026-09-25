"""Source-run authentication producing ``mod-base.selection`` (MB5, SPEC §4.8).

Generic checks in order: the selected artifact by id (name grammar, digest, size, not expired,
owner run and attempt); the handoff run through its attempt endpoint (path and workflow id,
completed/success, event, head repository, head SHA, canonical controller); ``display_title``
(mandatory whenever ``handoff.controller_sha != subject.commit``); the tested run by ``reuse``
(``none``: same run and attempt; ``attested``: exactly one completed/success job named exactly
``config.source.attestation_job``; ``delegated``: the adapter's ``authenticate_extensions`` proved
the reference and the tested run is a successful ``source.workflow`` run); the exact job graph
when ``require_job_graph``; the newest-run rule when ``require_newest_run``; the kit binding
(SPEC §1.8: ``referenced_workflows`` for caches, the pin in ``source.workflow`` at the handoff
run's head for handoffs); and a live subject-head recheck.

``authenticate`` is collect step 3 and writes the selection **draft**
(``documents.validate_selection(draft=True)``): every field except ``manifest_sha256``,
``binding`` and ``composition``, which only the step writing the compact bundle can know
(``compact`` or, for a ``selected`` handoff, ``compose``). ``kit`` is the executing kit of this
Pages run; ``source.kit_binding.sha`` is the selected artifact's recorded ``kit.sha`` proven
against its owner, which may be an older kit. Run records come from ``runs.run_record`` with
``require_controller_head=False`` only for a ``delegated`` tested run.

Details of this port of Block Pops ``authenticate_source`` and the Quick Skin collector checks:

* every handoff run, and an attested tested run, ran on the canonical branch (Block Pops
  ``controller_branch`` rules: a release branch's own copy of the source workflow never vouches
  for anything) and carries the source workflow's id; a Pages owner carries pages.yml's id;
* the event set of such a run is ``events.canonical``, intersected with ``events.other`` when the
  branch it vouches for (the subject for the handoff run, the tested claim's branch for an attested
  run) is not the canonical branch: a scheduled canonical run never vouches for a release branch;
* attested reuse is a manually dispatched attestation of unscheduled coverage (Block Pops
  ``test_pages_source_scope``: scheduled packaged coverage never becomes an attested projection),
  and the tested commit's tree is the subject's tree (Block Pops ``packaged.tree`` binding);
* a controller split (a handoff run whose own commit is not the subject) needs a configured and
  verified display title;
* a handoff must have been uploaded inside the successful ``source.handoff_step`` of the
  ``source.handoff_job`` of its own attempt (Quick Skin ``artifact_valid``);
* a cache is re-authenticated from scratch: its successful Pages owner (by attempt, never this
  run), and again the handoff and tested runs its manifest names; its embedded selection is never
  trusted as authentication.

:func:`kit_binding` also binds family generations (SPEC §1.8): a family handoff and a family cache
alike to the pin of ``families[].producer.workflow`` at the head of their envelope's producer run,
whose exact attempt the caller passes as ``owner_run`` (a family cache is that producer's bundle
copied verbatim by ``refresh``, also when a later publication carried it forward, so its kit is the
producer's, never its Pages owner's); this module never authenticates a family generation itself.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import timezone
from pathlib import Path
from typing import Any

from mod_base.adapter import host
from mod_base.errors import MbError
from mod_base.evidence import validate
from mod_base.github import artifacts, contents, jobs, runs
from mod_base.github.api import GitHubApi
from mod_base.model import documents, grammar
from mod_base.model import limits as lim
from mod_base.model.canonical import canonical_json, read_json_file, sha256_hex
from mod_base.pages.select import (
    Selected,
    SourceRuns,
    display_title,
    handoff_job_name,
    is_success,
    pages_owner_valid,
    subject_events,
    successful_job,
    upload_in_window,
)
from mod_base.pin import parse_pin_files
from mod_base.runtime import Invocation
from mod_base.workflow import PAGES_WORKFLOW_PATH, find_job

OWNER = "MB5"


def _fail(message: str, reason: str = "source-authentication") -> MbError:
    return MbError(message, reason=reason)


def write_new_file(path: Path, data: bytes) -> None:
    """Create ``path`` exclusively (never following or replacing anything) and write ``data``."""

    target = Path(os.path.abspath(path))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(target, flags, 0o644)
    except OSError as exc:
        raise MbError(f"cannot create {target}: {exc.strerror or exc}", reason="output") from exc
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(descriptor, view):]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _implementation(invocation: Invocation) -> dict[str, Any]:
    """The executing Pages run: this repository's ``pages.yml`` on the canonical branch."""

    environ = invocation.environ
    reference = environ.get("GITHUB_WORKFLOW_REF")
    parsed = grammar.parse_workflow_ref(reference)
    if (parsed.repository != invocation.repository or parsed.path != PAGES_WORKFLOW_PATH
            or parsed.branch != invocation.config.canonical_branch):
        raise _fail("authentication runs only in this repository's pages.yml on the canonical branch",
                    reason="environment")
    identity = []
    for name, maximum in (("GITHUB_RUN_ID", lim.MAX_RUN_ID), ("GITHUB_RUN_ATTEMPT", lim.MAX_RUN_ATTEMPT)):
        text = environ.get(name)
        if not grammar.is_match(grammar.POSITIVE_DECIMAL, text) or int(text) > maximum:  # type: ignore[arg-type]
            raise _fail(f"{name} must be a positive decimal", reason="environment")
        identity.append(int(text))  # type: ignore[arg-type]
    return {"branch": parsed.branch, "sha": invocation.implementation_sha, "workflow_ref": reference,
            "run_id": identity[0], "run_attempt": identity[1]}


def _require_source_run(invocation: Invocation, sources: SourceRuns, run: Mapping[str, Any],
                        claim: Mapping[str, Any], *, tested_branch: str, label: str) -> None:
    """A successful ``source.workflow`` attempt (path and id) of this repository on the canonical
    branch at the claim's own head, started by an event admissible for the branch it tested."""

    source, canonical = invocation.config.source, invocation.config.canonical_branch
    events = subject_events(invocation, subject_canonical=tested_branch == canonical)
    head = run.get("head_repository")
    identity = run.get("workflow_id")
    checks = (
        ("workflow", run.get("path") == source["workflow"] == claim["workflow_path"]
         and not isinstance(identity, bool) and identity == sources.source_workflow_id),
        ("head repository", isinstance(head, Mapping) and head.get("full_name") == invocation.repository),
        ("status", is_success(run)),
        ("event", isinstance(run.get("event"), str) and run.get("event") in events),
        ("controller", run.get("head_branch") == canonical),
        ("head", run.get("head_sha") == claim["controller_sha"]
         and run.get("head_branch") == claim["controller_branch"]),
    )
    for name, passed in checks:
        if not passed:
            raise _fail(f"the {label} {claim['run_id']} attempt {claim['run_attempt']} failed provenance: {name}")


def _display_title(invocation: Invocation, run: Mapping[str, Any], commit: str, *, label: str) -> bool:
    """Require ``run``'s display title to name ``commit`` when a title is configured; returns
    whether one was verified."""

    title = display_title(invocation, commit)
    if title is None:
        return False
    if run.get("display_title") != title:
        raise _fail(f"the {label} display title does not name {commit}")
    return True


def _created_at(artifact: artifacts.Artifact) -> str:
    """The artifact's creation time in the ``TIMESTAMP`` form of the selection schema."""

    return artifact.order[0].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_producer_attempt(run: Mapping[str, Any], producer: Any, workflow: str) -> bool:
    """``run`` is exactly the attempt the envelope's ``producer`` claim names: same run id and
    attempt (integers, never booleans), the configured producer workflow, head ``producer.commit``."""

    if not isinstance(producer, Mapping):
        return False
    fields = [(run.get("id"), producer.get("run_id")), (run.get("run_attempt"), producer.get("run_attempt"))]
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0
           for pair in fields for value in pair):
        return False
    return (all(observed == claimed for observed, claimed in fields)
            and producer.get("workflow_path") == workflow
            and grammar.is_match(grammar.SHA1, producer.get("commit"))
            and run.get("head_sha") == producer.get("commit"))


def kit_binding(api: GitHubApi, invocation: Invocation, *, manifest: Mapping[str, Any],
                owner_run: Mapping[str, Any], selected_kind: str) -> dict[str, str]:
    """``{source, sha}``: prove ``manifest.kit.sha`` belongs to the authenticated owner run (SPEC §1.8).

    * ``handoff``: ``owner_run`` is the handoff run; the kit is the pin of ``source.workflow`` at
      that run's head (``workflow_file``).
    * ``family-handoff`` and ``family-cache``: ``manifest`` is the family ``envelope.json`` and
      ``owner_run`` is always its producer run attempt (``producer.run_id``/``run_attempt``, whose
      head is ``producer.commit``), never the Pages run that owns a cache: ``refresh`` copies the
      producer's bundle verbatim into the cache, so a family cache carries the producer's kit. The
      kit is the pin of ``families[].producer.workflow`` at that run's head (``workflow_file``).
      The caller authenticates that run (status, event, branch, repository) first.
    * ``cache``: ``owner_run`` is the owning Pages run; the kit is the one its
      ``referenced_workflows`` resolved (``referenced_workflows``)."""

    kit = manifest["kit"]
    if selected_kind in ("handoff", "family-handoff", "family-cache"):
        if selected_kind == "handoff":
            workflow = invocation.config.source["workflow"]
        else:
            workflow = invocation.config.family(grammar.require_family(manifest.get("family")))["producer"]["workflow"]
            if not _is_producer_attempt(owner_run, manifest.get("producer"), workflow):
                raise _fail(f"the {selected_kind}'s owner run is not its envelope's producer run attempt",
                            reason="kit-binding")
        if owner_run.get("path") != workflow:
            raise _fail(f"the {selected_kind}'s owner run is not a {workflow} run", reason="kit-binding")
        head = grammar.require_sha1(owner_run.get("head_sha"), f"{selected_kind} run head")
        pin = parse_pin_files({workflow: contents.file_at(api, workflow, head, max_bytes=lim.MAX_WORKFLOW_FILE_BYTES)})
        if pin.sha != kit["sha"] or pin.version != "v" + kit["version"]:
            raise _fail(f"the {selected_kind}'s kit {kit['sha']} is not the pin of {workflow} at its run's head "
                        f"{head}", reason="kit-binding")
        return {"source": "workflow_file", "sha": pin.sha}
    if selected_kind == "cache":
        if owner_run.get("path") != PAGES_WORKFLOW_PATH:
            raise _fail(f"the {selected_kind}'s owner run is not a Pages run", reason="kit-binding")
        sha = runs.referenced_kit_sha(owner_run)
        if sha != kit["sha"]:
            raise _fail(f"the {selected_kind}'s kit {kit['sha']} is not the kit its Pages run "
                        f"{owner_run.get('id')!r} executed", reason="kit-binding")
        return {"source": "referenced_workflows", "sha": sha}
    raise _fail(f"no kit binding exists for a {selected_kind!r} selection", reason="usage")


def _selected_artifact(api: GitHubApi, selected: Selected) -> artifacts.Artifact:
    artifact = artifacts.get_artifact(api, selected.artifact_id)
    for label, observed, wanted in (("name", artifact.name, selected.name),
                                    ("digest", artifact.digest, selected.digest),
                                    ("size", artifact.size, selected.size),
                                    ("owner run", artifact.run_id, selected.run_id)):
        if observed != wanted:
            raise _fail(f"selected artifact {selected.artifact_id} {label} differs from the selection",
                        reason="artifact")
    if artifact.expired:
        raise _fail(f"selected artifact {selected.artifact_id} has expired", reason="artifact")
    return artifact


def authenticate_selection(invocation: Invocation, *, api: GitHubApi, key: str, selected_dir: Path,
                           selected: Selected) -> dict[str, Any]:
    """Authenticate the downloaded ``selected_dir`` and return the validated selection draft."""

    key = grammar.require_key(key)
    if not isinstance(selected, Selected):
        raise _fail("the selected artifact must be a Selected record", reason="usage")
    if selected.kind not in ("handoff", "cache"):
        raise _fail("family generations are authenticated by family collect, not authenticate", reason="usage")
    config, source = invocation.config, invocation.config.source
    default = config.canonical_branch
    implementation = _implementation(invocation)
    sources = SourceRuns(api, invocation)

    # 1. The selected artifact by id, and the downloaded bundle it names.
    artifact = _selected_artifact(api, selected)
    if selected.kind == "handoff":
        bundle = validate.load_handoff(invocation, selected_dir, key=key)
    else:
        bundle = validate.load_compact(invocation, selected_dir, key=key)
    manifest = bundle.manifest
    subject, provenance = manifest["subject"], manifest["provenance"]
    handoff, tested, reuse = provenance["handoff"], provenance["tested"], provenance["reuse"]
    if selected.kind == "handoff":
        if (selected.name != grammar.handoff_name(key, handoff["run_attempt"])
                or (selected.run_id, selected.run_attempt) != (handoff["run_id"], handoff["run_attempt"])
                or (artifact.head_sha, artifact.head_branch) != (handoff["commit"], handoff["branch"])):
            raise _fail("the selected handoff is not the upload of the run its manifest names", reason="artifact")
        owner = None
    else:
        if selected.name != grammar.cache_name(key, subject["commit"]):
            raise _fail("the selected cache does not cover its manifest's subject", reason="artifact")
        owner = runs.get_run_attempt(api, selected.run_id, selected.run_attempt)
        if artifact.head_branch != default or not pages_owner_valid(
                owner, invocation, default_branch=default, pages_workflow_id=sources.pages_workflow_id,
                head_sha=artifact.head_sha):
            raise _fail(f"the selected cache is not owned by a successful earlier Pages run on {default}",
                        reason="artifact")

    # 2-3. The handoff run through its attempt endpoint (on the canonical controller), the display
    # title and the controller split.
    handoff_run = runs.get_run_attempt(api, handoff["run_id"], handoff["run_attempt"])
    _require_source_run(invocation, sources, handoff_run, handoff, tested_branch=subject["branch"],
                        label="handoff run")
    split = handoff["commit"] != subject["commit"]
    if not _display_title(invocation, handoff_run, subject["commit"], label="handoff run") and split:
        raise _fail("a controller split requires a configured and verified source.display_title")
    handoff_record = runs.run_record(handoff_run, handoff)
    handoff_jobs: list[dict[str, Any]] = []

    def attempt_jobs_of_handoff() -> list[dict[str, Any]]:
        if not handoff_jobs:
            handoff_jobs.extend(jobs.attempt_jobs(api, handoff["run_id"], handoff["run_attempt"]))
        return handoff_jobs

    if selected.kind == "handoff":
        job = successful_job(attempt_jobs_of_handoff(), handoff_job_name(invocation, key), handoff["run_attempt"])
        if not upload_in_window(artifact, job, source["handoff_step"]):
            raise _fail("the handoff was not uploaded by the successful source.handoff_step of its attempt",
                        reason="artifact")

    # 4. The tested run, by reuse.
    attestation: dict[str, Any] | None = None
    if reuse == "none":
        tested_record = runs.run_record(handoff_run, tested)
    elif reuse == "attested":
        name = source["attestation_job"]
        if name is None:
            raise _fail("attested reuse needs a configured source.attestation_job", reason="reuse")
        job = find_job(attempt_jobs_of_handoff(), name, run_attempt=handoff["run_attempt"])
        if job.get("status") != "completed" or job.get("conclusion") != "success":
            raise _fail("the handoff run's attestation job did not complete successfully", reason="reuse")
        attestation = {"name": name, "id": grammar.require_positive_int(job.get("id"), "attestation job id"),
                       "conclusion": "success"}
        tested_run = runs.get_run_attempt(api, tested["run_id"], tested["run_attempt"])
        _require_source_run(invocation, sources, tested_run, tested, tested_branch=tested["branch"],
                            label="tested run")
        if not _display_title(invocation, tested_run, tested["commit"], label="tested run"):
            raise _fail("attested reuse requires a configured source.display_title", reason="reuse")
        if handoff_run.get("event") != "workflow_dispatch" or tested_run.get("event") == "schedule":
            raise _fail("scheduled packaged coverage cannot become an attested projection", reason="reuse")
        if contents.commit_tree(api, tested["commit"]) != subject["tree"]:
            raise _fail("the attested tested commit does not carry the subject's tree", reason="reuse")
        tested_record = runs.run_record(tested_run, tested)
    else:
        tested_run = runs.get_run_attempt(api, tested["run_id"], tested["run_attempt"])
        if (tested_run.get("path") != source["workflow"] or not is_success(tested_run)
                or tested_run.get("workflow_id") != sources.source_workflow_id):
            raise _fail("the delegated tested run is not a successful source.workflow run", reason="reuse")
        tested_record = runs.run_record(tested_run, tested, require_controller_head=False)

    # R6: every declared extension is verified by the adapter (delegated reuse included).
    verified: list[str] = []
    if manifest["extensions"] is not None:
        result = host.call(invocation, "authenticate_extensions",
                           {"manifest": manifest, "extensions": bundle.extensions},
                           network="authenticate_extensions" in config.network_hooks)
        verified = validate.check_extensions_verified(manifest, result)

    # 5. The exact job graph of the tested attempt.
    graph_sha256: str | None = None
    if source["require_job_graph"]:
        expected = host.call(invocation, "expected_source_jobs",
                             {"expectation": bundle.expectation, "tested_run": tested_record})
        if expected is None:
            raise _fail("source.require_job_graph needs the adapter's expected_source_jobs graph", reason="job-graph")
        observed = (attempt_jobs_of_handoff()
                    if (tested["run_id"], tested["run_attempt"]) == (handoff["run_id"], handoff["run_attempt"])
                    else jobs.attempt_jobs(api, tested["run_id"], tested["run_attempt"]))
        graph_sha256 = jobs.require_job_graph(observed, expected)
        tested_record = {**tested_record, "job_graph_sha256": graph_sha256}

    # 6. The newest-run rule: never an older run for the subject while a newer one exists.
    if source["require_newest_run"]:
        newest = sources.newest(subject["commit"], subject_canonical=subject["branch"] == default)
        if (newest["id"], newest["run_attempt"]) != (handoff["run_id"], handoff["run_attempt"]):
            raise _fail("the evidence does not derive from the newest exact-subject source run attempt",
                        reason="newest-run")

    # 7. The kit binding, then the live subject head.
    binding = kit_binding(api, invocation, manifest=manifest, owner_run=handoff_run if owner is None else owner,
                          selected_kind=selected.kind)
    if contents.branch_head(api, subject["branch"]) != (subject["commit"], subject["tree"]):
        raise _fail(f"{subject['branch']} advanced past the selected subject", reason="stale-subject")

    source_record: dict[str, Any] = {"handoff_run": handoff_record, "tested_run": tested_record, "reuse": reuse,
                                     "kit_binding": binding}
    if attestation is not None:
        source_record["attestation_job"] = attestation
    if graph_sha256 is not None:
        source_record["job_graph_sha256"] = graph_sha256
    draft = {
        "kind": "mod-base.selection",
        "schema_version": 1,
        "repository": invocation.repository,
        "key": key,
        "kit": invocation.kit,
        "implementation": implementation,
        "subject": subject,
        "coverage_sha": provenance["coverage_sha"],
        "selected_artifact": {"kind": selected.kind, "id": artifact.id, "name": artifact.name,
                              "digest": artifact.digest, "size": artifact.size, "run_id": artifact.run_id,
                              "run_attempt": selected.run_attempt,
                              "workflow_path": source["workflow"] if owner is None else PAGES_WORKFLOW_PATH,
                              "created_at": _created_at(artifact)},
        "source": source_record,
        "expectation_sha256": manifest["expectation"]["sha256"],
        "source_manifest_sha256": sha256_hex(bundle.manifest_raw),
        "extensions_verified": verified,
    }
    return documents.validate_selection(draft, draft=True)


def run_authenticate(invocation: Invocation, *, api: GitHubApi, key: str, selected_dir: Path,
                     selected_json: Path, output: Path) -> dict[str, Any]:
    """The ``authenticate`` command: read ``selected_json`` (the exact :meth:`Selected.to_json`
    object written by ``select --output``), authenticate and write the selection draft to
    ``output`` (canonical JSON, new file)."""

    value, _ = read_json_file(Path(os.path.abspath(selected_json)), label="selected artifact",
                              max_bytes=lim.MAX_SELECTED_JSON_BYTES)
    draft = authenticate_selection(invocation, api=api, key=key, selected_dir=selected_dir,
                                   selected=Selected.parse(value))
    data = canonical_json(draft)
    if len(data) > lim.MAX_SELECTION_BYTES:
        raise _fail(f"the selection draft exceeds {lim.MAX_SELECTION_BYTES} bytes")
    write_new_file(output, data)
    return draft

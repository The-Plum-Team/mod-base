"""What the candidate-path tests share: a checkout as a job has one, a Build to stage, a producer.

``ci worker-stage``, ``ci worker-run`` and ``ci worker-seal`` are tested without accounts in
``tests/test_ci_lifecycle_candidate.py`` and with real accounts in ``LinuxCandidateCommandTests``
of ``tests/ci_linux_worker.py``. Both use the synthetic mod of ``tests/ci_mod_harness.py``; files,
Git and processes are real.
"""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any

from mod_base.build_ci.graph import run_graph
from mod_base.build_ci.protocol import plan_sha256
from mod_base.build_ci.records import validate_build_envelope, validate_source_selection
from mod_base.model import grammar
from mod_base.model.canonical import canonical_json, canonical_sha256
from mod_base.workflow import CI_CALLER_WORKFLOWS
from tests import ci_lifecycle_fixture as fixture
from tests import ci_mod_harness as h

#: Where the candidate's dispatcher sets its umask: a test adds candidate code right after it.
UMASK_LINE = "    os.umask(0o077)\n"


def fetch_checkout(mod: Path, base: Path) -> tuple[Path, str, str]:
    """Commit a copy of ``mod`` in ``base/upstream`` and check that commit out in the new
    ``base/candidate`` the way ``actions/checkout`` does: fetched into a fresh repository and
    detached. Returns ``(checkout, commit, tree)``."""

    commit, tree = fixture.commit_candidate(mod, base / "upstream")
    checkout = base / "candidate"
    checkout.mkdir()
    fixture.git(checkout, "init", "-q")
    fixture.git(checkout, "remote", "add", "origin", f"https://github.com/{h.REPOSITORY}")
    fixture.git(checkout, "-c", "protocol.version=2", "fetch", "-q", "--no-tags", "--depth=1",
                (base / "upstream").as_uri(), "+refs/heads/main:refs/remotes/origin/main")
    fixture.git(checkout, "checkout", "-q", "--force", "--detach", commit)
    return checkout, commit, tree


def patch_dispatcher(mod: Path, code: str) -> None:
    """Make the dispatcher of the copy ``mod`` run ``code`` (lines indented by four spaces) in
    every hook, right after it set its umask. For a tested tree only: the protected adapter of a
    job is another copy, whose configured hashes stay as they are."""

    path = mod / "scripts/ci/mod_base_build_dispatch.py"
    source = path.read_text(encoding="utf-8")
    if source.count(UMASK_LINE) != 1:
        raise AssertionError("the synthetic dispatcher no longer sets its umask in one place")
    path.write_text(source.replace(UMASK_LINE, UMASK_LINE + code), encoding="utf-8", newline="\n")


def producer(plan: dict[str, Any], caller: str, mode: str, *, run_id: int, run_attempt: int = 1,
             event: str = "pull_request_target") -> dict[str, Any]:
    """The producer identity of a run of the managed ``caller`` in ``mode`` for the plan's subject."""

    identity, path = plan["identity"], CI_CALLER_WORKFLOWS[caller]
    head = identity["head_sha"] if identity["pr_number"] else identity["tested_sha"]
    return {"run_id": run_id, "run_attempt": run_attempt, "workflow_path": path,
            "workflow_ref": grammar.workflow_ref(identity["repository"], path, identity["base_branch"]),
            "api_head_sha": head, "event": event, "graph_sha256": run_graph(caller, mode).sha256(plan)}


def build_descriptor(plan: dict[str, Any], *, caller: str = "build", mode: str = "full", run_id: int = 41,
                     run_attempt: int = 1, event: str = "pull_request_target") -> dict[str, Any]:
    """The descriptor of the complete Build artifact a run of ``caller`` uploaded for the plan."""

    return {"identity": copy.deepcopy(plan["identity"]), "plan_sha256": plan["plan_sha256"], "profile": plan["profile"],
            "producer": {**producer(plan, caller, mode, run_id=run_id, run_attempt=run_attempt, event=event),
                         "upload_window": {"started_at": "2026-10-08T10:00:00Z", "completed_at": "2026-10-08T10:02:00Z"}},
            "artifact": {"id": 7001, "name": grammar.ci_artifact_name("build", run_id, run_attempt),
                         "digest": "sha256:" + "ab" * 32, "size": 4096,
                         "created_at": "2026-10-08T10:01:00Z", "expires_at": "2026-10-15T10:01:00Z"}}


def complete_envelope(plan: dict[str, Any], root: Path, descriptor: dict[str, Any]) -> dict[str, Any]:
    """The envelope of the complete Build whose files lie below ``root``, as the run of
    ``descriptor`` sealed it. Every planned output must be there."""

    files = []
    for output in sorted((item for target in plan["targets"] for item in target["outputs"]),
                         key=lambda item: item["path"]):
        data = root.joinpath(*output["path"].split("/")).read_bytes()
        files.append({**output, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    envelope = {"kind": "mod-base.build.envelope", "schema_version": 1,
                "identity": copy.deepcopy(plan["identity"]), "plan_sha256": plan["plan_sha256"],
                "profile": plan["profile"],
                "producer": {key: value for key, value in descriptor["producer"].items() if key != "upload_window"},
                "scope": "complete", "target_id": None, "files": files,
                "native_reports": [file["path"] for file in files if file["role"] == "native-report"]}
    return validate_build_envelope(envelope, plan=plan)


def build_complete(sandbox: h.Sandbox, output: Path, descriptor: dict[str, Any]) -> dict[str, Any]:
    """Build every target of the sandbox's plan as a plain process and lay the complete Build out
    in the new directory ``output`` with its envelope, as ``ci fetch-build`` publishes one."""

    plan = sandbox.plan
    for target in plan["targets"]:
        sandbox.build(target["id"])
    output.mkdir(mode=0o700)
    for path in sorted(h.files(sandbox.sealed_build)):
        target = output.joinpath(*path.split("/"))
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.write_bytes((sandbox.sealed_build / path).read_bytes())
    envelope = complete_envelope(plan, output, descriptor)
    (output / grammar.CI_ENVELOPE_NAME).write_bytes(canonical_json(envelope))
    return envelope


def selection(plan: dict[str, Any], descriptor: dict[str, Any], envelope: dict[str, Any], *, run_id: int,
              run_attempt: int = 1, caller: str = "packaged") -> dict[str, Any]:
    """The selection record the run attempt ``run_id``/``run_attempt`` of ``caller`` wrote for
    the Build of ``descriptor``."""

    identity, path = plan["identity"], CI_CALLER_WORKFLOWS[caller]
    document = {"kind": "mod-base.ci.selection", "schema_version": 1, "identity": copy.deepcopy(identity),
                "plan_sha256": plan["plan_sha256"], "profile": plan["profile"],
                "request": {"run_id": run_id, "run_attempt": run_attempt, "nonce": "5e" * 32, "workflow_path": path,
                            "workflow_ref": grammar.workflow_ref(identity["repository"], path, identity["base_branch"])},
                "build": copy.deepcopy(descriptor), "envelope_sha256": canonical_sha256(envelope)}
    return validate_source_selection(document, plan=plan)


def protected_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """``plan`` as a protected push tests it: the default-branch commit is the commit tested, the
    commit executed and the head a run is recorded under."""

    changed = copy.deepcopy(plan)
    identity = changed["identity"]
    identity.update(pr_number=0, head_sha=identity["controller_sha"], head_branch=identity["base_branch"],
                    base_sha=identity["controller_sha"], tested_sha=identity["controller_sha"],
                    tested_parents=["b" * 40])
    changed["plan_sha256"] = plan_sha256(changed)
    return changed

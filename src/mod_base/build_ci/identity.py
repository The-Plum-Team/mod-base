"""The authenticated subject of one Build or packaged job and its private state record.

``ci subject`` runs first in every job. It learns what is tested from the API, never from the run's
own ``head_sha`` or from a caller input alone:

* a pull request: its live head, base and draft state (a draft is a rejection), and the synthetic
  merge commit GitHub tests, whose parents must be exactly ``[base, head]``;
* a protected push, dispatch or schedule: the live head of the default branch, which must be the
  commit that is executing.

The controller is the executing protected commit (``GITHUB_SHA``) and the managed caller that
``GITHUB_WORKFLOW_REF`` names on the default branch; the kit is the checkout the prologue verified.
The identity always names the Build caller as its controller workflow, so a Build run and the
packaged run of one generation derive the same identity and therefore the same plan. The hashes
that depend on the protected policy and on the candidate bytes are bound later
(:func:`mod_base.build_ci.planning.build_plan`).

Mutable state (the default branch, the pull request) is read at the start and again before the
record is written; the commit object, which never changes, is read once.

The record is ``<state>/identity.json``: canonical JSON in a directory only the runner can enter.
``ci subject`` creates that directory; nothing in it is ever replaced.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

from mod_base.build_ci.authenticate import read_pr_generation
from mod_base.build_ci.config import BuildConfig
from mod_base.build_ci.protocol import (BUILD_ADAPTER_API, BUILD_GRAPH_VERSION, PACKAGED_GRAPH_VERSION, PRODUCERS,
                                        SHA1, WORKFLOW, validate_subject)
from mod_base.errors import MbError
from mod_base.github.api import GitHubApi
from mod_base.github.contents import branch_head, default_branch
from mod_base.io.secure_json import loads
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json, canonical_sha256
from mod_base.model.validators import Obj, Str, check
from mod_base.pin import kit_tree_digest
from mod_base.runtime import Invocation
from mod_base.workflow import CI_CALLER_WORKFLOWS

IDENTITY_NAME = "identity.json"
PULL_REQUEST_EVENT = "pull_request_target"
PROTECTED_EVENTS = ("push", "workflow_dispatch", "schedule")
#: The domain of :func:`policy_sha256`; a change of what the digest covers changes this label.
POLICY_FORMAT = "mod-base.build.policy-v1"


class SubjectError(MbError):
    """The subject of this job cannot be authenticated (exit 2)."""

    default_reason = "ci-subject"


class StateError(MbError):
    """The job's private state directory or one of its records cannot be trusted (exit 2)."""

    default_reason = "ci-state"


def run_workflows(producer: str, *, pull_request: bool) -> tuple[str, ...]:
    """The managed callers a job of ``producer`` may run from. Build jobs also run inside the
    packaged caller, whose ``rebuild`` job calls the Build workflow when a protected (never a
    pull-request) subject has no Build to select."""

    if producer == "build" and not pull_request:
        return (CI_CALLER_WORKFLOWS["build"], CI_CALLER_WORKFLOWS["packaged"])
    return (CI_CALLER_WORKFLOWS[producer],)


_RECORD = Obj({
    "producer": Str(choices=PRODUCERS),
    "event": Str(choices=(PULL_REQUEST_EVENT, *PROTECTED_EVENTS)),
    "workflow_path": WORKFLOW,
    "controller_tree": SHA1,
    "subject": validate_subject,
})


def validate_subject_record(document: Any, path: str = "$") -> dict[str, Any]:
    """The closed identity record: the producer, event and caller of this run, the tree of the
    controller commit and the subject; every one of them as ``ci subject`` authenticated it."""

    _RECORD(document, path)
    subject = document["subject"]
    pull_request = bool(subject["pr_number"])
    check((document["event"] == PULL_REQUEST_EVENT) == pull_request, f"{path}.event",
          "a pull request subject needs the pull_request_target event and no other subject may use it")
    check(document["workflow_path"] in run_workflows(document["producer"], pull_request=pull_request),
          f"{path}.workflow_path", "the producer does not run from this managed caller")
    check(subject["controller_workflow"] == CI_CALLER_WORKFLOWS["build"], f"{path}.subject.controller_workflow",
          "must name the managed Build caller")
    check(pull_request or document["controller_tree"] == subject["tested_tree"], f"{path}.controller_tree",
          "a protected subject is its own controller")
    return document


def _environment(invocation: Invocation, name: str) -> str:
    value = invocation.environ.get(name)
    if not value:
        raise MbError(f"{name} is required for this command", reason="environment")
    return value


def _commit(api: GitHubApi, sha: str) -> tuple[str, tuple[str, ...]]:
    """``(tree, ordered parents)`` of one Git commit: an immutable object, read once."""

    value = api.get_json(f"/repos/{api.repository}/git/commits/{sha}")
    check(type(value) is dict and value.get("sha") == sha, "$.commit", "response names another Git object")
    tree, parents = value.get("tree"), value.get("parents")
    check(type(tree) is dict and grammar.is_match(grammar.SHA1, tree.get("sha")), "$.commit.tree", "has no tree")
    check(type(parents) is list and len(parents) <= 2
          and all(type(parent) is dict and grammar.is_match(grammar.SHA1, parent.get("sha")) for parent in parents),
          "$.commit.parents", "must be at most two commits")
    return tree["sha"], tuple(parent["sha"] for parent in parents)


def authenticate_subject(invocation: Invocation, api: GitHubApi, *, producer: str,
                         pr_number: int | None) -> dict[str, Any]:
    """Authenticate what this job tests and return its identity record (:func:`validate_subject_record`).

    ``pr_number`` is the pull request of a ``pull_request_target`` run and ``None`` for a protected
    push, dispatch or schedule. Everything the environment claims is checked before the first
    request; nothing is written here."""

    check(producer in PRODUCERS, "$.producer", "must be build or packaged")
    pull_request = pr_number is not None
    repository = invocation.repository
    controller = invocation.implementation_sha
    check(api.repository == repository, "$.repository", "the API client serves another repository")
    event = _environment(invocation, "GITHUB_EVENT_NAME")
    check(event == PULL_REQUEST_EVENT if pull_request else event in PROTECTED_EVENTS, "$.event",
          "a pull request subject needs the pull_request_target event and no other subject may use it")
    caller = grammar.parse_workflow_ref(_environment(invocation, "GITHUB_WORKFLOW_REF"))
    check(caller.repository == repository and caller.path in run_workflows(producer, pull_request=pull_request),
          "$.workflow_ref", "the run is not this repository's managed caller of the producer")
    kit = {**invocation.kit, "tree_digest": kit_tree_digest(invocation.kit_root)}

    if pull_request:
        generation = read_pr_generation(api, pr_number=pr_number, controller_sha=controller)
        if generation.draft:
            raise SubjectError(f"pull request {pr_number} is a draft: the caller must defer, not call", reason="draft")
        if generation.merge_sha is None:
            raise SubjectError(f"pull request {pr_number} has no test merge: it conflicts or is not computed yet",
                               reason="no-test-merge")
        branch, controller_tree, tested = generation.base_branch, generation.controller_tree, generation.merge_sha
        base_sha, head_sha, head_branch = generation.base_sha, generation.head_sha, generation.head_branch
        # The subject rules below require these parents to be exactly [base, head], in that order.
        tree, parents = _commit(api, tested)
    else:
        branch = default_branch(api)
        live = branch_head(api, branch)
        if live[0] != controller:
            raise SubjectError("the protected default branch has moved past the executing commit",
                               reason="controller-moved")
        controller_tree, head_branch = live[1], branch
        tested = base_sha = head_sha = controller
        tree, parents = _commit(api, tested)
        check(tree == controller_tree and tested not in parents, "$.commit",
              "the default branch and its commit object disagree")
        check(default_branch(api) == branch and branch_head(api, branch) == live, "$.controller_sha",
              "the protected default branch moved during authentication")

    check(caller.branch == branch and _environment(invocation, "GITHUB_REF") == f"refs/heads/{branch}"
          and invocation.config.canonical_branch == branch,
          "$.ref", "the run, its caller and the mod's canonical branch must all be the default branch")
    subject = {
        "repository": repository, "source_repository": repository, "pr_number": pr_number or 0,
        "head_sha": head_sha, "head_branch": head_branch, "base_sha": base_sha, "base_branch": branch,
        "controller_sha": controller, "controller_workflow": CI_CALLER_WORKFLOWS["build"],
        "controller_ref": grammar.workflow_ref(repository, CI_CALLER_WORKFLOWS["build"], branch),
        "kit": kit, "tested_sha": tested, "tested_tree": tree, "tested_parents": list(parents),
        "graph_version": BUILD_GRAPH_VERSION,
    }
    return validate_subject_record({"producer": producer, "event": event, "workflow_path": caller.path,
                                    "controller_tree": controller_tree, "subject": subject})


def policy_sha256(config: BuildConfig, subject: dict[str, Any]) -> str:
    """The closed protected-policy digest of the controller that executes ``subject``.

    It covers the exact bytes of the protected Build config, every source of the adapter import
    closure the config lists (as read from the protected checkout), the kit pin with its tree
    digest, the adapter API and both graph versions. Any change of one of them, and nothing a
    candidate controls, changes the digest; post-merge reuse compares it instead of the controller
    commit, which an ordinary merge always moves."""

    validate_subject(subject)
    check(type(config) is BuildConfig and config.data["repository"] == subject["repository"],
          "$.config", "must be the protected Build config of the subject's repository")
    return canonical_sha256({
        "format": POLICY_FORMAT,
        "build_adapter_api": BUILD_ADAPTER_API,
        "graph_versions": {"build": BUILD_GRAPH_VERSION, "packaged": PACKAGED_GRAPH_VERSION},
        "kit": subject["kit"],
        "config_sha256": config.sha256,
        "adapter_files": [{"path": file.path, "sha256": file.sha256} for file in config.files],
    })


# -- The private state directory ---------------------------------------------------------------------

_NO_FOLLOW = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


def _open_state(state: Path) -> int:
    """A descriptor of the state directory: a real directory only this user can enter."""

    try:
        descriptor = os.open(state, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NO_FOLLOW)
    except OSError as exc:
        raise StateError(f"cannot open the state directory: {exc.strerror or exc}") from exc
    info = os.fstat(descriptor)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        os.close(descriptor)
        raise StateError("the state directory must be a private directory of this user")
    return descriptor


def _record_name(name: str) -> str:
    if not grammar.is_bundle_path(name) or "/" in name:
        raise StateError("a state record is one plainly named file")
    return name


def create_state(state: Path) -> None:
    """Create the job's private state directory. An existing path is never adopted, so every job
    starts from a state that this run wrote."""

    try:
        os.mkdir(state, 0o700)
    except FileExistsError:
        raise StateError("the state directory already exists; `ci subject` runs once, first") from None
    except OSError as exc:
        raise StateError(f"cannot create the state directory: {exc.strerror or exc}") from exc
    os.close(_open_state(state))


def write_state_record(state: Path, name: str, raw: bytes) -> None:
    """Create ``<state>/<name>`` with ``raw``, readable by this user alone; never replaces a record."""

    directory = _open_state(state)
    try:
        descriptor = os.open(_record_name(name), os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NO_FOLLOW, 0o600,
                             dir_fd=directory)
        try:
            view = memoryview(raw)
            while view:
                view = view[os.write(descriptor, view):]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise StateError(f"cannot write the state record {name}: {exc.strerror or exc}") from exc
    finally:
        os.close(directory)


def read_state_record(state: Path, name: str, *, max_bytes: int) -> bytes:
    """Bytes of ``<state>/<name>``: a single-link regular file of this user, mode 0600, of 1 to
    ``max_bytes`` bytes, unchanged while it was read."""

    directory = _open_state(state)
    try:
        descriptor = os.open(_record_name(name), os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | _NO_FOLLOW,
                             dir_fd=directory)
        try:
            before = os.fstat(descriptor)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid() or before.st_nlink != 1
                    or stat.S_IMODE(before.st_mode) != 0o600 or not 1 <= before.st_size <= max_bytes):
                raise StateError(f"the state record {name} is not a private bounded file of this user")
            chunks = []
            remaining = max_bytes + 1
            while remaining:
                chunk = os.read(descriptor, min(limits.CI_PROCESS_READ_BYTES, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            after = os.fstat(descriptor)
            if len(raw) != before.st_size or (after.st_size, after.st_mtime_ns, after.st_nlink) != (
                    before.st_size, before.st_mtime_ns, before.st_nlink):
                raise StateError(f"the state record {name} changed while it was read")
            return raw
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise StateError(f"cannot read the state record {name}: {exc.strerror or exc}") from exc
    finally:
        os.close(directory)


def write_subject(state: Path, record: dict[str, Any]) -> None:
    """Create the state directory and write the identity record into it."""

    raw = canonical_json(validate_subject_record(record))
    create_state(state)
    write_state_record(state, IDENTITY_NAME, raw)


def read_subject(state: Path) -> dict[str, Any]:
    """The identity record ``ci subject`` wrote, strictly decoded and validated again."""

    raw = read_state_record(state, IDENTITY_NAME, max_bytes=limits.MAX_CI_IDENTITY_BYTES)
    document = validate_subject_record(loads(raw, label=IDENTITY_NAME, max_bytes=limits.MAX_CI_IDENTITY_BYTES))
    check(raw == canonical_json(document), "$", "identity record is not canonical JSON")
    return document

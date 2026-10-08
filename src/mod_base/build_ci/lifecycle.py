"""The worker lifecycle of one Build or packaged job (MB11): prepare, plan, stage, run, seal, validate,
finish.

Every step of a job is one ``ci`` command run by the runner. The steps share the job's private
state directory, which ``ci subject`` creates, and the fixed worker root below ``/tmp``. This
module is what the ``ci worker-*`` and ``ci plan`` commands compose:

* :func:`open_job` binds a command to the state, to the protected Build config of the mod checkout
  the prologue verified and to the verified kit;
* :func:`prepare_worker` (``ci worker-prepare``) closes the runner home, fences the host image,
  admits the tool trees, allocates the accounts and hands the protected adapter copy to the
  validator;
* :func:`open_worker` gives every later command the prepared worker, checked against the host;
* :func:`derive_plan` (``ci plan``) stages the candidate files the protected config names, runs
  ``derive_plan`` as the validator and builds the plan;
* :func:`stage_candidate` (``ci worker-stage``) gives the candidate its own copy of the tested
  commit, the verified kit and, for a lane, the complete Build;
* :func:`run_candidate_hook` (``ci worker-run``) runs the one candidate hook of the job behind the
  tool fence and records how it ended;
* :func:`seal_worker` (``ci worker-seal``) locks the candidate and has root prove its tracked
  sources unchanged and freeze its export with the envelope protected code writes;
* :func:`validate_export` (``ci worker-validate``) hands the job's sealed export to the validator,
  runs its verification hook, has root seal the reports with the record protected code builds
  and writes the directory the job uploads;
* :func:`finish_worker` (``ci worker-finish``) is the sweep a job always ends with.

State records are canonical JSON, written once and read strictly:

``worker-host.json``
    the runner home as it was, written before anything is changed. A second ``worker-prepare``
    stops at it, and ``worker-finish`` restores the home from it after any failure.
``worker.json``
    what later steps need: the accounts, the interpreter, the JDK homes, the tool receipt and
    the digest of the protected config.
``ci-plan.json``
    the plan of this job.
``worker-stage.json``
    what the candidate was staged from: the runner's checkout, the tested commit and tree, the
    kit and whether a Gradle seed and the Build went with it.
``worker-run.json``
    how the one candidate hook of the job ended, written also when it failed.
``worker-seal.json``
    what was frozen after a successful hook: the export and the hash of its envelope.
``ci-selection.json``
    (written by ``ci fetch-build``) the Build a lane job runs against.

Between two hook runs both accounts are terminated and locked. A locked account still runs what
the runner starts for it, so locking is the resting state and not the end of a job.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import stat
import tempfile
from collections.abc import Callable, Iterator, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from mod_base.build_ci import adapter, identity, planning
from mod_base.build_ci.authenticate import run_head
from mod_base.build_ci.checkout import read_objects
from mod_base.build_ci.config import BuildConfig, load_build_config
from mod_base.build_ci.controller import (CONTROLLER_VALIDATION_ROOT, ControllerSources,
                                          checkout_controller_sources, execute_controller_validator,
                                          materialize_controller_sources)
from mod_base.build_ci.exports import (BUILD_VALIDATION_ROOT, CANDIDATE_SOURCE_ROOT, inspect_complete_build,
                                       verify_build_export)
from mod_base.build_ci.graph import run_graph
from mod_base.build_ci.handoff import _read_private_record, record_build_validation_execution
from mod_base.build_ci.host import (HOST_RUNNER_HOME, HostBoundary, _canonical_path, authenticate_host_boundary,
                                    inspect_worker_host, protect_worker_host, restore_worker_host)
from mod_base.build_ci.inputs import (DERIVED_PLAN_ROOT, VALIDATOR_INPUT_ROOT, _layout, build_input_sha256,
                                      execute_frozen_build_validator, materialize_validation_inputs,
                                      read_sealed_build, replace_plan_inputs)
from mod_base.build_ci.protocol import ID, SHA1, SHA256, repo_path, validate_plan
from mod_base.build_ci.records import validate_source_selection
from mod_base.build_ci.root_request import (request_build_export_freeze, request_build_validation_freeze,
                                            request_build_validation_grant, request_bundle_staging,
                                            request_candidate_source_check, request_candidate_staging,
                                            request_controller_grant, request_derived_plan, request_derived_runtime,
                                            request_host_fence, request_plan_inputs_grant,
                                            request_runtime_export_freeze, request_runtime_validation_freeze,
                                            request_runtime_validation_grant, request_validation_inputs_grant,
                                            run_root_operation)
from mod_base.build_ci.root_request_schema import _ACCOUNT, _BOUNDARY
from mod_base.build_ci.runtime_exports import verify_runtime_export
from mod_base.build_ci.runtime_handoff import DERIVED_RUNTIME_ROOT, record_runtime_validation_execution
from mod_base.build_ci.runtime_inputs import (RUNTIME_VALIDATION_ROOT, execute_frozen_runtime_validator,
                                              read_sealed_runtime)
from mod_base.build_ci.source import GitSourceEntry, parse_source_inventory, verify_source_copy
from mod_base.build_ci.toolchain import (ToolTreeProof, _execution_tool_paths, authenticate_toolchains,
                                         execute_tool_fenced_worker, inspect_worker_toolchains)
from mod_base.build_ci.validation import SEALED_VALIDATION_ROOT, materialize_validated_export
from mod_base.build_ci.worker import (JAVA_HOMES_ENVIRONMENT, WORKER_ACCOUNTS, WORKER_ROOT, WorkerAccount,
                                      WorkerExecutionError, WorkerResult, allocate_worker_account,
                                      authenticate_worker_account, lock_worker_account, prepare_worker_boundary,
                                      render_worker_log, terminate_worker, worker_account_exists, worker_processes)
from mod_base.build_ci.worker_preparation import _tree_id
from mod_base.errors import MAX_MESSAGE_CHARS, MbError, single_line
from mod_base.github.api import GitHubApi
from mod_base.github.contents import blob, exact_tree
from mod_base.io.atomic_directory import atomic_directory, write_new
from mod_base.io.secure_json import loads
from mod_base.io.tree import authenticate_tree_private_access, copy_regular_data_files, read_child_file
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json, canonical_sha256
from mod_base.model.validators import Bool, Int, List, Nullable, Obj, Str, check
from mod_base.pin import (ACTIONS_DIR, DIGESTED_DIRS, LOCKED_DIRS, STAMP_NAME, Pin, kit_tree_digest, stamp_document,
                          verify_staged_files)
from mod_base.runtime import Invocation
from mod_base.workflow import ci_producer

HOST_NAME = "worker-host.json"
WORKER_NAME = "worker.json"
PLAN_NAME = grammar.CI_PLAN_NAME
#: ``ci worker-prepare --roles``: the accounts a job allocates, in allocation order.
ROLE_SETS = {"validator": ("validator",), "candidate+validator": ("candidate", "validator")}


class LifecycleError(MbError):
    """A job step was asked for in a state the worker lifecycle does not allow (exit 2)."""

    default_reason = "ci-lifecycle"


def _tool_path(value: Any, path: str) -> str:
    check(type(value) is str, path, "must be a canonical absolute POSIX path")
    return str(_canonical_path(value))


_HOST = Obj({"boundary": _BOUNDARY})
_WORKER = Obj({
    "boundary": _BOUNDARY,
    "accounts": Obj({"candidate": Nullable(_ACCOUNT), "validator": _ACCOUNT}),
    "python": _tool_path,
    "java_homes": List(_tool_path, max_items=limits.MAX_CI_TOOL_ROOTS, unique=True),
    "tools": Obj({"roots": List(_tool_path, min_items=1, max_items=limits.MAX_CI_TOOL_ROOTS, unique=True),
                  "metadata_sha256": SHA256, "files": Int(0, limits.MAX_CI_SOURCE_FILES),
                  "entries": Int(1, limits.MAX_CI_SOURCE_ENTRIES),
                  "total_bytes": Int(0, limits.MAX_CI_SOURCE_TREE_BYTES)}),
    "config_sha256": SHA256,
})


@dataclass(frozen=True)
class Job:
    """What a lifecycle command knows of its job once ``ci subject`` has run."""

    state: Path
    record: dict[str, Any]
    config: BuildConfig
    sources: ControllerSources
    kit_root: Path
    kit_digest: str
    run_id: int
    run_attempt: int

    @property
    def subject(self) -> dict[str, Any]:
        return self.record["subject"]


@dataclass(frozen=True)
class Worker:
    """The prepared worker of a job, as ``worker.json`` records it."""

    boundary: HostBoundary
    accounts: Mapping[str, WorkerAccount]
    python: str
    java_homes: tuple[str, ...]
    tools: ToolTreeProof
    config_sha256: str

    @property
    def validator(self) -> WorkerAccount:
        return self.accounts["validator"]

    @property
    def java_home(self) -> str | None:
        """The ``JAVA_HOME`` of a hook that does not select a JDK: the first one the job installed."""

        return self.java_homes[0] if self.java_homes else None


def _run_number(invocation: Invocation, name: str, maximum: int) -> int:
    value = invocation.environ.get(name, "")
    if not grammar.POSITIVE_DECIMAL.fullmatch(value) or int(value) > maximum:
        raise MbError(f"{name} must be a positive decimal", reason="environment")
    return int(value)


def open_job(invocation: Invocation, state: Path) -> Job:
    """Bind this command to the job ``ci subject`` authenticated.

    The state must belong to the executing repository, controller commit and kit. The protected
    Build config and its adapter closure are read from the mod checkout the prologue verified
    (every listed source is compared with its configured hash); nothing is read from the API.
    """

    record = identity.read_subject(state)
    subject = record["subject"]
    check(invocation.repository == subject["repository"]
          and invocation.implementation_sha == subject["controller_sha"],
          "$.state", "the state directory belongs to another repository or controller commit")
    digest = kit_tree_digest(invocation.kit_root)
    check({**invocation.kit, "tree_digest": digest} == subject["kit"], "$.state.subject.kit",
          "the executing kit is not the one `ci subject` authenticated")
    config = load_build_config(invocation.repo_root, repository=subject["repository"])
    sources = checkout_controller_sources(config, controller_sha=subject["controller_sha"],
                                          controller_tree=record["controller_tree"])
    return Job(Path(state), record, config, sources, invocation.kit_root, digest,
               _run_number(invocation, "GITHUB_RUN_ID", limits.MAX_RUN_ID),
               _run_number(invocation, "GITHUB_RUN_ATTEMPT", limits.MAX_RUN_ATTEMPT))


# -- State records -----------------------------------------------------------------------------------


def _read_record(state: Path, name: str, validate: Callable[[Any, str], Any], max_bytes: int) -> dict[str, Any]:
    raw = identity.read_state_record(state, name, max_bytes=max_bytes)
    document = loads(raw, label=name, max_bytes=max_bytes)
    validate(document, "$")
    check(raw == canonical_json(document), "$", f"{name} is not canonical JSON")
    return document


def _recorded(state: Path, name: str) -> bool:
    """Whether the state directory holds ``name``; a missing state directory holds nothing."""

    try:
        os.lstat(Path(state) / name)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError as error:
        raise identity.StateError(f"cannot inspect the state record {name}: {error.strerror or error}") from error
    return True


def _account(role: str, ids: Mapping[str, int]) -> WorkerAccount:
    return WorkerAccount(role, ids["uid"], ids["gid"], str(WORKER_ROOT / adapter.HOME_DIRECTORY[role]))


def _worker_document(worker: Worker) -> dict[str, Any]:
    accounts = {role: None if role not in worker.accounts else
                {"uid": worker.accounts[role].uid, "gid": worker.accounts[role].gid} for role in WORKER_ACCOUNTS}
    return {"boundary": asdict(worker.boundary), "accounts": accounts, "python": worker.python,
            "java_homes": list(worker.java_homes),
            "tools": {**asdict(worker.tools), "roots": list(worker.tools.roots)},
            "config_sha256": worker.config_sha256}


def read_worker(state: Path) -> Worker:
    """The prepared worker ``ci worker-prepare`` recorded, strictly decoded.

    Besides the closed shape, every account must differ in user and group from the runner and
    from the other account. The host is not consulted here (:func:`open_worker` does that).
    """

    document = _read_record(state, WORKER_NAME, _WORKER, limits.MAX_CI_WORKER_RECORD_BYTES)
    boundary = HostBoundary(**document["boundary"])
    accounts = {role: _account(role, ids) for role, ids in document["accounts"].items() if ids is not None}
    users = [boundary.uid, *(account.uid for account in accounts.values())]
    groups = [boundary.gid, *(account.gid for account in accounts.values())]
    check(len(set(users)) == len(users) and len(set(groups)) == len(groups), "$.accounts",
          "worker accounts are not isolated from the runner and from each other")
    tools = ToolTreeProof(**{**document["tools"], "roots": tuple(document["tools"]["roots"])})
    return Worker(boundary, accounts, document["python"], tuple(document["java_homes"]), tools,
                  document["config_sha256"])


def open_worker(job: Job) -> Worker:
    """The prepared worker of this job, checked against the host as it is now.

    The protected config must be the one the worker was prepared for, the runner home must still
    be fenced, every recorded account must be the live one, a role the job did not allocate must
    have no account, and the admitted tool trees must be unchanged: the interpreter in them is
    what this command runs as root next. After ``ci worker-finish`` the home is open again, so
    this fails.
    """

    worker = read_worker(job.state)
    check(worker.config_sha256 == job.config.sha256, "$.worker.config_sha256",
          "the protected Build config changed after the worker was prepared")
    authenticate_host_boundary(worker.boundary)
    for role in WORKER_ACCOUNTS:
        if role in worker.accounts:
            check(authenticate_worker_account(role) == worker.accounts[role], f"$.worker.accounts.{role}",
                  "is not the account this job allocated")
        elif worker_account_exists(role):
            raise LifecycleError(f"a {role} account exists that this job did not allocate")
    authenticate_toolchains(worker.tools, boundary=worker.boundary)
    return worker


def read_plan(job: Job) -> dict[str, Any]:
    """The plan ``ci plan`` recorded for this job: canonical, valid and of exactly its subject."""

    document = _read_record(job.state, PLAN_NAME, lambda value, path: validate_plan(value, path=path),
                            limits.MAX_CI_PLAN_BYTES)
    return planning.require_plan(document, subject=job.subject)


# -- Resting accounts --------------------------------------------------------------------------------


def rest_workers(accounts: Mapping[str, WorkerAccount]) -> None:
    """Terminate and lock every account, each one even when another fails; raise the first failure."""

    failure: MbError | None = None
    for account in tuple(accounts.values()):
        try:
            terminate_worker(account)
        except MbError as error:
            failure = failure or error
    if failure is not None:
        raise failure


@contextlib.contextmanager
def resting(accounts: Mapping[str, WorkerAccount]) -> Iterator[None]:
    """Whatever the block does, every account ends terminated and locked.

    ``accounts`` is read when the block ends, so accounts allocated inside it are included. A
    failure of the block is the one reported; ``ci worker-finish`` sweeps again in any case.
    """

    try:
        yield
    except BaseException:
        with contextlib.suppress(MbError):
            rest_workers(accounts)
        raise
    rest_workers(accounts)


def _root(job: Job, python: str, operation: str, nonce: str) -> None:
    run_root_operation(operation, python=python, kit_root=job.kit_root, kit_digest=job.kit_digest, nonce=nonce)


# -- ci worker-prepare -------------------------------------------------------------------------------


def tool_roots(python: str, java_homes: tuple[str, ...]) -> tuple[str, ...]:
    """The tool trees a job's hooks execute from: the interpreter's installation, both as named
    (a virtual environment or a setup-python prefix) and as its links resolve, and every JDK home.

    This only names them; :func:`mod_base.build_ci.toolchain.inspect_worker_toolchains` admits
    every entry, ancestor and link target, and the interpreter is bound to them before dispatch.
    """

    named = _canonical_path(python)
    resolved = PurePosixPath(os.path.realpath(python))
    for path in (named, resolved):
        check(path.parent.name == "bin" and len(path.parts) > 3, "$.python",
              "must be <prefix>/bin/<interpreter>, in a prefix of its own")
    for home in java_homes:
        _canonical_path(home)
    check(len(set(java_homes)) == len(java_homes), "$.java_home", "is given more than once")
    return tuple(dict.fromkeys((str(named.parent.parent), str(resolved.parent.parent), *java_homes)))


def _hosted_layout(invocation: Invocation) -> dict[str, str]:
    def need(name: str) -> str:
        value = invocation.environ.get(name)
        if not value:
            raise MbError(f"{name} is required for this command", reason="environment")
        return value

    return {"runner_environment": need("RUNNER_ENVIRONMENT"), "runner_home": HOST_RUNNER_HOME,
            "workspace": need("GITHUB_WORKSPACE"), "runner_temp": need("RUNNER_TEMP")}


def prepare_worker(invocation: Invocation, job: Job, *, roles: tuple[str, ...], python: str,
                   java_homes: tuple[str, ...]) -> Worker:
    """``ci worker-prepare``: fence the host and set up the job's disposable accounts.

    In order: admit the hosted runner layout and record the home as it is; create the worker
    boundary; close the runner home; fence the host image as root (the request channel lives
    below the boundary and names the closed home, so the fence follows them); admit the tool
    trees; allocate the accounts; copy the protected adapter closure from the verified mod
    checkout into ``controller/`` and have root hand it to the validator; leave every account
    terminated and locked; record the worker. Nothing is read from the API.

    A failure leaves the home closed and the accounts locked: ``ci worker-finish`` ends the job.
    """

    check(roles in ROLE_SETS.values(), "$.roles", "must be the validator alone or the candidate and the validator")
    roots = tool_roots(python, java_homes)
    layout = _hosted_layout(invocation)
    observed = inspect_worker_host(**layout)
    # Written before anything is changed: a second prepare of this job stops here, and from now
    # on `worker-finish` knows the mode to give the home back.
    identity.write_state_record(job.state, HOST_NAME, canonical_json(_HOST({"boundary": asdict(observed)}, "$")))
    prepare_worker_boundary(runner_environment=layout["runner_environment"])
    boundary = protect_worker_host(**layout)
    check(boundary == observed, "$.boundary", "the runner home changed while it was fenced")
    accounts: dict[str, WorkerAccount] = {}
    with resting(accounts):
        _root(job, python, "host-fence", request_host_fence(boundary=boundary))
        tools = inspect_worker_toolchains(boundary=boundary, roots=roots)
        for home in java_homes or (None,):
            _execution_tool_paths(tools, boundary, python, home)
        for role in roles:
            accounts[role] = allocate_worker_account(role)
        materialize_controller_sources(Path(str(CONTROLLER_VALIDATION_ROOT)), sources=job.sources,
                                       identity=job.subject)
        _root(job, python, "grant-controller",
              request_controller_grant(boundary=boundary, validator=accounts["validator"],
                                       sources=job.sources, subject=job.subject))
    worker = Worker(boundary, accounts, python, java_homes, tools, job.config.sha256)
    identity.write_state_record(job.state, WORKER_NAME, canonical_json(_WORKER(_worker_document(worker), "$")))
    return open_worker(job)


# -- ci plan -----------------------------------------------------------------------------------------


def api_candidate_files(api: GitHubApi, job: Job) -> dict[str, bytes]:
    """The candidate files a plan is derived from, read from the API at the tested tree.

    The result maps each staged name of ``adapter.plan_sources`` (the inventory, the scenario
    contract, then the extra plan inputs of the protected config) to the bytes of its path. The
    tree and the blobs are named by SHA, so each is read once: one request for the tree and one
    per file. A path must be a regular file below real directories; a link, a submodule or an
    oversized blob is a rejection.
    """

    check(api.repository == job.subject["repository"], "$.repository", "the API client serves another repository")
    rows = {row["path"]: row for row in exact_tree(api, job.subject["tested_tree"], recursive=True)}

    def read(path: str) -> bytes:
        parts = path.split("/")
        for count in range(1, len(parts)):
            parent = rows.get("/".join(parts[:count]))
            check(parent is not None and (parent["type"], parent["mode"]) == ("tree", "040000"), "$.candidate",
                  f"{path} is not below real directories of the tested tree")
        row = rows.get(path)
        check(row is not None and row["type"] == "blob" and row["mode"] in {"100644", "100755"}
              and type(row.get("size")) is int and 1 <= row["size"] <= limits.MAX_CI_PLAN_SOURCE_BYTES,
              "$.candidate", f"{path} is not a regular file of 1..{limits.MAX_CI_PLAN_SOURCE_BYTES} bytes "
              "in the tested tree")
        data = blob(api, row["sha"], max_bytes=limits.MAX_CI_PLAN_SOURCE_BYTES)
        check(len(data) == row["size"], "$.candidate", f"{path}: tree and blob sizes disagree")
        return data

    return {name: read(path) for name, path in adapter.plan_sources(job.config.data).items()}


#: One read of the candidate checkout's object store: the closed Git every checkout is read with.
_git = read_objects


def _tested_checkout(checkout: Path, job: Job) -> Path:
    """The absolute path of a candidate checkout whose own Git directory has ``HEAD`` at the
    tested commit and that commit with the tested tree."""

    checkout = Path(os.path.abspath(checkout))
    try:
        if not stat.S_ISDIR(os.lstat(checkout / ".git").st_mode):
            raise LifecycleError("the candidate checkout has no Git directory of its own", reason="git")
    except OSError as error:
        raise LifecycleError("the candidate checkout has no Git directory of its own", reason="git") from error
    tested = job.subject["tested_sha"]
    answer = limits.MAX_CI_GIT_ANSWER_BYTES
    check(_git(checkout, "rev-parse", "--verify", "HEAD^{commit}", max_bytes=answer) == f"{tested}\n".encode("ascii"),
          "$.candidate", "the candidate checkout is not at the tested commit")
    check(_git(checkout, "rev-parse", "--verify", f"{tested}^{{tree}}", max_bytes=answer)
          == f"{job.subject['tested_tree']}\n".encode("ascii"), "$.candidate",
          "the candidate checkout's commit does not have the tested tree")
    return checkout


def checkout_candidate_files(checkout: Path, job: Job) -> dict[str, bytes]:
    """The candidate files a plan is derived from, read as Git objects of the candidate checkout.

    The result has the shape of :func:`api_candidate_files` and the same bytes, without a request.
    The checkout's ``HEAD`` must be the tested commit and that commit's tree the tested tree.
    The working files are never read: a path is resolved in the commit's tree, must be a regular
    file there, and its blob is read by id with the id recomputed from the bytes.
    """

    checkout = _tested_checkout(checkout, job)
    tested = job.subject["tested_sha"]
    answer = limits.MAX_CI_GIT_ANSWER_BYTES

    def read(path: str) -> bytes:
        listing = _git(checkout, "ls-tree", "-z", "-l", "--full-tree", tested, "--", path, max_bytes=answer)
        entry, _, name = listing.rstrip(b"\0").partition(b"\t")
        fields = entry.decode("ascii", "replace").split()
        check(listing.count(b"\0") == 1 and name == path.encode("utf-8") and len(fields) == 4
              and fields[0] in {"100644", "100755"} and fields[1] == "blob"
              and grammar.is_match(grammar.SHA1, fields[2]) and grammar.POSITIVE_DECIMAL.fullmatch(fields[3])
              and int(fields[3]) <= limits.MAX_CI_PLAN_SOURCE_BYTES, "$.candidate",
              f"{path} is not a regular file of 1..{limits.MAX_CI_PLAN_SOURCE_BYTES} bytes in the tested commit")
        data = _git(checkout, "cat-file", "blob", fields[2], max_bytes=limits.MAX_CI_PLAN_SOURCE_BYTES)
        oid = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()  # noqa: S324 - Git object id
        check(len(data) == int(fields[3]) and oid == fields[2], "$.candidate", f"{path} does not match its object id")
        return data

    return {name: read(path) for name, path in adapter.plan_sources(job.config.data).items()}


def run_protected_hook(job: Job, worker: Worker, hook: str, *, plan: dict[str, Any] | None,
                       unit_id: str | None, log: Callable[[str], object]) -> WorkerResult:
    """Run one protected hook as the validator and write its neutralised log through ``log``.

    ``plan`` is ``None`` only for ``derive_plan``, which runs for the job's subject. A hook that
    fails, hangs or leaves a process behind raises after its log was written; either way the
    validator ends terminated and locked.
    """

    try:
        result = execute_controller_validator(
            boundary=worker.boundary, validator=worker.validator, sources=job.sources, tools=worker.tools,
            plan=plan, subject=job.subject if plan is None else None, hook=hook, unit_id=unit_id,
            python=worker.python, java_home=worker.java_home, run_id=job.run_id, run_attempt=job.run_attempt)
    except WorkerExecutionError as error:
        log(render_worker_log(error.result, role="validator"))
        raise
    log(render_worker_log(result, role="validator"))
    return result


def derive_plan(job: Job, worker: Worker, sources: Mapping[str, bytes], *, expected_sha256: str | None,
                log: Callable[[str], object]) -> dict[str, Any]:
    """``ci plan``: derive, bind and record the plan of this job.

    ``sources`` is what :func:`api_candidate_files` or :func:`checkout_candidate_files` read: the
    bytes of every candidate file by staged name. They are staged in ``validation-input/`` and
    handed to the validator;
    ``derive_plan`` runs from the protected adapter copy; root hands back exactly the one file it
    must write; protected code builds the plan around it (identity, policy digest, hashes of the
    candidate files) and compares its hash with ``expected_sha256``, the plan another job of the
    generation derived, when one is given. The input root is then staged again with the plan and
    handed over, so every later protected hook of the job finds its complete input, and the plan
    is recorded in the state. Every account ends terminated and locked, whatever happens.
    """

    if _recorded(job.state, PLAN_NAME):
        raise LifecycleError("this job already has a plan; `ci plan` runs once")
    boundary, validator = worker.boundary, worker.validator
    check(list(sources) == list(adapter.plan_sources(job.config.data)), "$.sources",
          "must hold exactly the candidate files the protected config names")
    extra = dict(sources)
    inventory, scenario_contract = extra.pop(adapter.INVENTORY_INPUT), extra.pop(adapter.SCENARIO_INPUT)
    with resting(worker.accounts):
        materialize_validation_inputs(Path(str(VALIDATOR_INPUT_ROOT)), sources=sources)
        digests = {name: hashlib.sha256(data).hexdigest() for name, data in sources.items()}
        _root(job, worker.python, "grant-plan-inputs",
              request_plan_inputs_grant(boundary=boundary, validator=validator, digests=digests))
        run_protected_hook(job, worker, "derive_plan", plan=None, unit_id=None, log=log)
        _root(job, worker.python, "take-derived-plan", request_derived_plan(boundary=boundary, validator=validator))
        authenticate_host_boundary(boundary)
        _layout(boundary)
        derived = _read_private_record(Path(str(DERIVED_PLAN_ROOT)), name=adapter.PLAN_OUTPUT,
                                       owner_uid=boundary.uid, owner_gid=boundary.gid,
                                       max_bytes=limits.MAX_CI_PLAN_SOURCE_BYTES, label="derived plan")
        plan = planning.build_plan(subject=job.subject, config=job.config, inventory=inventory,
                                   scenario_contract=scenario_contract, plan_inputs=extra, derived=derived)
        planning.require_plan(plan, subject=job.subject, expected_sha256=expected_sha256)
        replace_plan_inputs(boundary=boundary, validator=validator, sources=sources, plan=plan)
        _root(job, worker.python, "grant-validation-inputs",
              request_validation_inputs_grant(boundary=boundary, validator=validator, plan=plan))
    identity.write_state_record(job.state, PLAN_NAME, canonical_json(plan))
    return read_plan(job)


# -- ci worker-stage, ci worker-run, ci worker-seal: the candidate path --------------------------------

STAGE_NAME = "worker-stage.json"
RUN_NAME = "worker-run.json"
SEAL_NAME = "worker-seal.json"
#: The selection record ``ci fetch-build`` keeps in the state of a lane job: the Build the lane runs.
SELECTION_NAME = "ci-selection.json"
#: The producer whose job runs each candidate hook: lanes run in the packaged workflow only.
HOOK_PRODUCERS = {"policy": "build", "build_target": "build", "run_lane": "packaged"}
#: What a job's seal freezes for each candidate hook; ``policy`` exports nothing.
HOOK_EXPORTS = {"policy": None, "build_target": "build", "run_lane": "runtime"}
#: The directories of a kit checkout that a staged kit holds, as a mod's bootstrap copies them.
_KIT_DIRECTORIES = (*DIGESTED_DIRS, *LOCKED_DIRS, ACTIONS_DIR)
_KIT_BOUNDS = dict(max_files=limits.MAX_CI_KIT_INSTALL_FILES, max_entries=limits.MAX_CI_KIT_INSTALL_ENTRIES,
                   max_total_bytes=limits.MAX_CI_KIT_INSTALL_BYTES, max_file_bytes=limits.MAX_CI_KIT_INSTALL_BYTES)

_STAGE = Obj({
    "candidate": _tool_path, "tested_sha": SHA1, "tested_tree": SHA1,
    "source": Obj({"files": Int(1, limits.MAX_CI_SOURCE_FILES), "bytes": Int(0, limits.MAX_CI_SOURCE_TREE_BYTES)}),
    "kit": Obj({"sha": SHA1, "version": Str(grammar.VERSION, max_len=20),
                "tree_digest": Str(grammar.DIGEST, max_len=71)}),
    "gradle_seed": Bool(),
    "bundle": Nullable(Obj({"path": repo_path, "envelope_sha256": SHA256,
                            "files": Int(1, limits.MAX_CI_EXPORT_FILES)})),
})
_EXECUTION_FIELDS = {"returncode": Nullable(Int(-255, 255)), "truncated": Bool(),
                     "log_bytes": Int(0, limits.MAX_CI_LOG_BYTES), "log_sha256": SHA256}
_RUN = Obj({"hook": Str(choices=adapter.CANDIDATE_HOOKS), "unit_id": Nullable(ID), "succeeded": Bool(),
            "error": Nullable(Str(max_len=MAX_MESSAGE_CHARS, text="evidence")), **_EXECUTION_FIELDS})
_SEAL = Obj({"hook": Str(choices=adapter.CANDIDATE_HOOKS), "unit_id": Nullable(ID),
             "export": Nullable(Str(choices=("build", "runtime"))), "envelope_sha256": Nullable(SHA256),
             "files": Int(0, limits.MAX_CI_EXPORT_FILES)})


def _candidate(worker: Worker) -> WorkerAccount:
    if "candidate" not in worker.accounts:
        raise LifecycleError("this job allocated no candidate account: `ci worker-prepare --roles candidate+validator`")
    return worker.accounts["candidate"]


def read_stage(job: Job) -> dict[str, Any]:
    """What ``ci worker-stage`` recorded: the runner's checkout the candidate was staged from, the
    tested commit and tree, the staged kit and whether a Gradle seed and the Build went with it."""

    stage = _read_record(job.state, STAGE_NAME, _STAGE, limits.MAX_CI_WORKER_RECORD_BYTES)
    check((stage["tested_sha"], stage["tested_tree"]) == (job.subject["tested_sha"], job.subject["tested_tree"]),
          "$.stage", "the candidate was staged for another subject")
    return stage


def read_run(job: Job) -> dict[str, Any]:
    """What ``ci worker-run`` recorded of its one candidate hook: the hook and unit, whether it
    succeeded (``error`` says why not) and the exit status and log digest of the execution."""

    run = _read_record(job.state, RUN_NAME, _RUN, limits.MAX_CI_WORKER_RECORD_BYTES)
    check((run["error"] is None) == run["succeeded"] and (not run["succeeded"] or run["returncode"] == 0),
          "$.run", "a successful run has a zero exit and no error, a failed one says why")
    check((run["unit_id"] is None) == (adapter.HOOKS[run["hook"]].unit is None), "$.run.unit_id",
          "a hook runs for its kind of unit")
    return run


def read_seal(job: Job) -> dict[str, Any]:
    """What ``ci worker-seal`` recorded once it sealed: the hook and unit, which export it froze
    (``build`` into ``sealed-build/``, ``runtime`` into ``sealed-runtime/``, ``None`` for the
    source check of ``policy`` alone), the SHA-256 of the canonical envelope and its file count."""

    seal = _read_record(job.state, SEAL_NAME, _SEAL, limits.MAX_CI_WORKER_RECORD_BYTES)
    check(seal["export"] == HOOK_EXPORTS[seal["hook"]]
          and (seal["export"] is None) == (seal["envelope_sha256"] is None) == (seal["files"] == 0)
          and (seal["unit_id"] is None) == (adapter.HOOKS[seal["hook"]].unit is None), "$.seal",
          "names the export its hook produces for its kind of unit, with its envelope")
    return seal


def _complete_build(worker: Worker, plan: dict[str, Any]) -> dict[str, Any]:
    """The envelope of the complete Build ``ci fetch-build`` left in ``sealed-build/``, every byte checked."""

    try:
        return inspect_complete_build(boundary=worker.boundary, plan=plan)[0]
    except OSError as error:
        raise LifecycleError("this job has no Build in sealed-build/: `ci fetch-build` runs first") from error


def checkout_inventory(checkout: Path, job: Job) -> tuple[GitSourceEntry, ...]:
    """The complete inventory of the tested tree, read from the Git objects of the candidate checkout.

    The checkout must be detached at the tested commit, whose tree is the tested tree: ``HEAD``
    holds the commit id itself, not a branch. The tree is listed recursively by plumbing, within
    the source listing cap; a submodule, an unusual mode or an unsafe path is a rejection. The
    listing is bound to the authenticated tree by recomputing the tree's object name from it, so
    no other inventory passes. The working files are not read here.
    """

    checkout = _tested_checkout(checkout, job)
    tested, tree = job.subject["tested_sha"], job.subject["tested_tree"]
    check(read_child_file(checkout / ".git", "HEAD", max_bytes=limits.MAX_CI_GIT_REF_BYTES)
          == f"{tested}\n".encode("ascii"), "$.candidate", "the candidate checkout is not detached at the tested commit")
    inventory = parse_source_inventory(_git(checkout, "ls-tree", "-r", "-l", "-z", "--full-tree", tree,
                                            max_bytes=limits.MAX_CI_SOURCE_LIST_BYTES))
    check(_tree_id(inventory) == tree, "$.candidate", "the candidate checkout's objects do not list the tested tree")
    return inventory


def stage_kit_overlay(job: Job, output: Path) -> Pin:
    """Runner-only: stage the verified kit at the new directory ``output`` as a mod's bootstrap does.

    The copy holds the digested directories, ``template/``, ``tools/`` and ``actions/`` of the kit
    checkout this command executes (regular files only, none executable) and the stamp
    ``MOD_BASE_KIT.json`` for the kit commit and version of the subject. It must have the verified
    kit-digest-v1 and match the staged-file locks. Returns the pin the stamp names; root admits the
    overlay against it again before it copies it to the candidate's ``out/mod-base-kit``.
    """

    kit = job.subject["kit"]
    pin = Pin(kit["sha"], "v" + kit["version"], ())

    def writer(stage: Path, stage_fd: int) -> None:
        for top in _KIT_DIRECTORIES:
            os.mkdir(top, 0o700, dir_fd=stage_fd)
            descriptor = os.open(top, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=stage_fd)
            try:
                copy_regular_data_files(job.kit_root / top, descriptor, **_KIT_BOUNDS)
            finally:
                os.close(descriptor)
        write_new(stage_fd, STAMP_NAME, canonical_json(stamp_document(pin, job.kit_digest)))
        check(kit_tree_digest(stage) == job.kit_digest, "$.kit", "the staged kit differs from the verified checkout")
        verify_staged_files(stage)

    try:
        atomic_directory(output, writer)
    except OSError as error:
        raise LifecycleError(f"cannot stage the kit for the candidate: {error.strerror or error}") from error
    return pin


def stage_candidate(job: Job, worker: Worker, plan: dict[str, Any], checkout: Path, *, gradle_seed: Path | None,
                    bundle: bool) -> dict[str, Any]:
    """``ci worker-stage``: give the candidate its checkout, the kit and, for a lane, the Build.

    The runner's ``checkout`` must be detached at the tested commit and hold exactly the tested
    tree: every tracked file with its bytes and mode and nothing else, ignored files included
    (:func:`checkout_inventory`, ``source.verify_source_copy``). The verified kit is staged with
    its stamp in a temporary directory of the state (:func:`stage_kit_overlay`) and the
    ``stage-candidate`` root operation copies the tracked files, a curated ``.git``, the kit as
    ``out/mod-base-kit`` and the optional Gradle seed into the candidate's own ``repository/`` and
    Gradle home. With ``bundle`` the complete Build of ``sealed-build/`` is then placed at
    ``repository/<bundle.path>`` by ``stage-bundle``, as a copy the candidate owns. Nothing of the
    candidate runs. Every account ends terminated and locked; the stage is recorded last, once.
    """

    candidate = _candidate(worker)
    if _recorded(job.state, STAGE_NAME):
        raise LifecycleError("this job already staged its candidate; `ci worker-stage` runs once")
    checkout = Path(os.path.abspath(checkout))
    seed = None if gradle_seed is None else Path(os.path.abspath(gradle_seed))
    inventory = checkout_inventory(checkout, job)
    verify_source_copy(checkout, inventory=inventory)
    with resting(worker.accounts):
        envelope = _complete_build(worker, plan) if bundle else None
        try:
            with tempfile.TemporaryDirectory(prefix="mb-ci-stage-", dir=job.state) as temporary:
                overlay = Path(temporary) / "kit"
                pin = stage_kit_overlay(job, overlay)
                _root(job, worker.python, "stage-candidate", request_candidate_staging(
                    boundary=worker.boundary, candidate=candidate, repository=job.subject["repository"],
                    tested_sha=job.subject["tested_sha"], tested_tree=job.subject["tested_tree"],
                    inventory=inventory, source=checkout, overlay=overlay, pin=pin, expected_digest=job.kit_digest,
                    gradle_seed=seed))
        except OSError as error:
            raise identity.StateError(f"cannot hold the staged kit in the state directory: "
                                      f"{error.strerror or error}") from error
        if envelope is not None:
            _root(job, worker.python, "stage-bundle", request_bundle_staging(
                boundary=worker.boundary, candidate=candidate, validator=worker.validator, sources=job.sources,
                plan=plan, envelope=envelope))
    stage = {"candidate": checkout.as_posix(), "tested_sha": job.subject["tested_sha"],
             "tested_tree": job.subject["tested_tree"],
             "source": {"files": len(inventory), "bytes": sum(entry.size for entry in inventory)},
             "kit": {"sha": pin.sha, "version": pin.version[1:], "tree_digest": job.kit_digest},
             "gradle_seed": seed is not None,
             "bundle": None if envelope is None else {
                 "path": job.config.data["bundle"]["path"], "envelope_sha256": canonical_sha256(envelope),
                 "files": len(envelope["files"])}}
    identity.write_state_record(job.state, STAGE_NAME, canonical_json(_STAGE(stage, "$")))
    return read_stage(job)


def derive_runtime(job: Job, worker: Worker, plan: dict[str, Any], lane_id: str, *,
                   log: Callable[[str], object]) -> dict[str, str]:
    """The values ``run_lane`` receives for ``lane_id``, as the protected ``derive_runtime`` hook
    returns them: the hook runs as the validator, ``take-derived-runtime`` hands the runner a
    private copy of its one output and removes the original, and the copy is decoded strictly
    (``adapter.parse_runtime_values``). A job derives them once."""

    run_protected_hook(job, worker, "derive_runtime", plan=plan, unit_id=lane_id, log=log)
    _root(job, worker.python, "take-derived-runtime",
          request_derived_runtime(boundary=worker.boundary, validator=worker.validator))
    authenticate_host_boundary(worker.boundary)
    _layout(worker.boundary)
    try:
        raw = _read_private_record(Path(str(DERIVED_RUNTIME_ROOT)), name=adapter.RUNTIME_OUTPUT,
                                   owner_uid=worker.boundary.uid, owner_gid=worker.boundary.gid,
                                   max_bytes=limits.MAX_CI_REPORT_BYTES, label="derived runtime values")
    except OSError as error:
        raise LifecycleError("cannot read the runtime values root handed over") from error
    return adapter.parse_runtime_values(raw)


def _execution(result: WorkerResult | None) -> dict[str, Any]:
    """What a state record and a root request keep of one execution; the log itself was printed."""

    log = b"" if result is None else result.log
    return {"returncode": None if result is None else result.returncode,
            "truncated": False if result is None else result.truncated,
            "log_bytes": len(log), "log_sha256": hashlib.sha256(log).hexdigest()}


def run_candidate_hook(job: Job, worker: Worker, plan: dict[str, Any], hook: str, *, unit_id: str | None,
                       log: Callable[[str], object]) -> dict[str, Any]:
    """``ci worker-run``: run the one candidate hook of this job and record how it ended.

    ``hook`` is ``policy``, ``build_target`` (for a target of the plan) or ``run_lane`` (for a
    lane, in a packaged job whose stage placed the Build). For a lane the protected
    ``derive_runtime`` hook runs first as the validator and its values are passed on unchanged
    (:func:`derive_runtime`). The hook is the dispatcher of the candidate's own ``repository/``
    copy, run as the candidate behind the tool fence with the timeout the protected config sets
    for it. ``JAVA_HOME`` is the first JDK of the job and ``MB_JAVA_HOMES`` lists every one, each
    bound to the admitted tool trees first. The neutralised log goes to ``log`` whatever happens,
    and every account ends terminated and locked.

    ``worker-run.json`` is written once the hook was attempted, also when it failed, hung, left a
    process behind or could not be started; the failure is then raised again. Returns the record
    of a hook that succeeded. A job runs its candidate once.
    """

    check(type(hook) is str and hook in HOOK_PRODUCERS, "$.hook", "is not a candidate hook")
    candidate = _candidate(worker)
    stage = read_stage(job)
    if _recorded(job.state, RUN_NAME):
        raise LifecycleError("this job already ran its candidate hook; `ci worker-run` runs once")
    adapter.plan_unit(plan, hook, unit_id)
    check(job.record["producer"] == HOOK_PRODUCERS[hook], "$.hook",
          f"{hook} is a step of a {HOOK_PRODUCERS[hook]} job")
    if hook == "run_lane" and stage["bundle"] is None:
        raise LifecycleError("run_lane needs the Build in its checkout: `ci worker-stage --bundle`")
    result: WorkerResult | None = None
    failure: MbError | None = None
    try:
        with resting(worker.accounts):
            runtime = derive_runtime(job, worker, plan, unit_id, log=log) if hook == "run_lane" else None
            values = adapter.hook_values(hook, unit_id=unit_id, runtime=runtime)
            if worker.java_homes:
                values[JAVA_HOMES_ENVIRONMENT] = ":".join(worker.java_homes)
            for home in worker.java_homes[1:]:
                _execution_tool_paths(worker.tools, worker.boundary, worker.python, home)
            command = adapter.hook_command(hook, python=worker.python, checkout=str(CANDIDATE_SOURCE_ROOT),
                                           dispatcher=job.config.data["adapter"]["dispatcher"])
            try:
                result = execute_tool_fenced_worker(
                    candidate, boundary=worker.boundary, tools=worker.tools, command=command, python=worker.python,
                    java_home=worker.java_home, identity=plan["identity"], run_id=job.run_id,
                    run_attempt=job.run_attempt, values=values,
                    timeout_seconds=adapter.hook_timeout_seconds(hook, job.config.data))
            except WorkerExecutionError as error:
                result = error.result
                raise
            finally:
                if result is not None:
                    log(render_worker_log(result, role="candidate"))
    except MbError as error:
        failure = error
    run = {"hook": hook, "unit_id": unit_id, "succeeded": failure is None,
           "error": None if failure is None else single_line(failure), **_execution(result)}
    identity.write_state_record(job.state, RUN_NAME, canonical_json(_RUN(run, "$")))
    if failure is not None:
        raise failure
    return read_run(job)


def _producer(job: Job, plan: dict[str, Any], mode: str) -> dict[str, Any]:
    """The producer identity every record of the executing run attempt carries, for the graph the
    run shows in ``mode``: the managed caller and event of the job's identity record, the head
    GitHub records the run under and the digest of that graph for ``plan``."""

    path, subject = job.record["workflow_path"], plan["identity"]
    return {"run_id": job.run_id, "run_attempt": job.run_attempt, "workflow_path": path,
            "workflow_ref": grammar.workflow_ref(subject["repository"], path, subject["base_branch"]),
            "api_head_sha": run_head(subject)[0], "event": job.record["event"],
            "graph_sha256": run_graph(ci_producer(path), mode).sha256(plan)}


def read_selection(job: Job, plan: dict[str, Any]) -> dict[str, Any]:
    """The selection record of a lane job (``ci-selection.json``): the complete Build this run
    attempt of the packaged caller selected for ``plan``, strictly decoded."""

    selection = _read_record(job.state, SELECTION_NAME,
                             lambda value, path: validate_source_selection(value, plan=plan, path=path),
                             limits.MAX_CI_RECORD_BYTES)
    request = selection["request"]
    check((request["run_id"], request["run_attempt"], request["workflow_path"])
          == (job.run_id, job.run_attempt, job.record["workflow_path"]), "$.selection.request",
          "the selection record belongs to another run or attempt")
    return selection


def _lane_mode(job: Job, owning_build: dict[str, Any]) -> str:
    """The graph mode of the packaged run a lane job belongs to: a pull request's own mode, or for
    a protected subject whether the run built its Build itself or selected one of a Build run."""

    built = ci_producer(owning_build["producer"]["workflow_path"])
    if job.subject["pr_number"]:
        return "pull-request"
    if built == "build":
        return "selected"
    check((owning_build["producer"]["run_id"], owning_build["producer"]["run_attempt"])
          == (job.run_id, job.run_attempt), "$.selection.build", "a rebuilt Build is the one this run attempt built")
    return "rebuilt"


def seal_worker(invocation: Invocation, state: Path) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """``ci worker-seal``: lock the candidate and freeze what its hook left, or say why nothing is.

    Runs after ``ci worker-run`` whatever that step did (``if: always()``). The candidate is
    terminated and locked first, before any record is read. Returns the run record and the seal
    record, which is ``None`` when nothing was sealed:

    * a hook that did not succeed seals nothing, and the step that ran it has already failed. A
      job without a record of its run is a rejection, since nothing shows that an earlier step
      failed;
    * otherwise root proves the tracked sources of ``repository/`` unchanged against the tested
      tree's inventory, read again from the runner's checkout, with nothing undeclared outside
      the generated roots of the protected config
      (``root_request_operations.candidate_generated_roots``);
    * ``build_target``: ``freeze-build-export`` copies ``export/`` to ``sealed-build/`` with the
      envelope root builds from the plan, which requires exactly the planned outputs;
    * ``run_lane``: ``freeze-runtime-export`` copies ``export/`` to ``sealed-runtime/`` with the
      runtime envelope root builds, bound to the Build of the job's selection record;
    * ``policy``: ``verify-candidate-source`` alone; it exports nothing.

    The sealed copy is read back as the runner and must carry this run's producer identity. Every
    account ends terminated and locked; ``worker-seal.json`` is written last.
    """

    if _sweep("candidate") is None:
        raise LifecycleError("this job has no candidate account to seal")
    job = open_job(invocation, state)
    worker = open_worker(job)
    candidate = _candidate(worker)
    if _recorded(job.state, SEAL_NAME):
        raise LifecycleError("this job already sealed its candidate; `ci worker-seal` runs once")
    if not _recorded(job.state, RUN_NAME):
        raise LifecycleError("the candidate hook has no recorded result: nothing is sealed")
    run = read_run(job)
    if not run["succeeded"]:
        return run, None
    stage, plan = read_stage(job), read_plan(job)
    hook, unit_id = run["hook"], run["unit_id"]
    execution = {key: run[key] for key in _EXECUTION_FIELDS}
    envelope = None
    with resting(worker.accounts):
        inventory = checkout_inventory(Path(stage["candidate"]), job)
        common = dict(boundary=worker.boundary, candidate=candidate, validator=worker.validator, sources=job.sources,
                      plan=plan, inventory=inventory, execution=execution)
        if hook == "policy":
            _root(job, worker.python, "verify-candidate-source", request_candidate_source_check(**common))
        elif hook == "build_target":
            producer = _producer(job, plan, "full" if ci_producer(job.record["workflow_path"]) == "build" else "rebuilt")
            _root(job, worker.python, "freeze-build-export",
                  request_build_export_freeze(**common, target_id=unit_id, producer=producer))
            authenticate_host_boundary(worker.boundary)
            envelope = verify_build_export(BUILD_VALIDATION_ROOT, plan=plan)
            check((envelope["scope"], envelope["target_id"], envelope["producer"]) == ("target", unit_id, producer),
                  "$.envelope", "the sealed Build is not this job's target partition")
        else:
            owning_build = read_selection(job, plan)["build"]
            build = _complete_build(worker, plan)
            check(stage["bundle"] is not None and canonical_sha256(build) == stage["bundle"]["envelope_sha256"],
                  "$.build", "the sealed Build is not the one that was staged for the candidate")
            producer = _producer(job, plan, _lane_mode(job, owning_build))
            _root(job, worker.python, "freeze-runtime-export", request_runtime_export_freeze(
                **common, lane_id=unit_id, producer=producer, build=build, owning_build=owning_build))
            authenticate_host_boundary(worker.boundary)
            envelope = verify_runtime_export(RUNTIME_VALIDATION_ROOT, plan=plan)
            check((envelope["scope"], envelope["lane_id"], envelope["producer"], envelope["owning_build"])
                  == ("lane", unit_id, producer, owning_build), "$.envelope",
                  "the sealed runtime results are not this job's lane")
    seal = {"hook": hook, "unit_id": unit_id, "export": HOOK_EXPORTS[hook],
            "envelope_sha256": None if envelope is None else canonical_sha256(envelope),
            "files": 0 if envelope is None else len(envelope["files"])}
    identity.write_state_record(job.state, SEAL_NAME, canonical_json(_SEAL(seal, "$")))
    return run, read_seal(job)


# -- ci worker-finish --------------------------------------------------------------------------------


def _sweep(role: str) -> WorkerAccount | None:
    """Terminate and lock the account of ``role`` if the host has one; return it."""

    if not worker_account_exists(role):
        return None
    try:
        account = authenticate_worker_account(role)
    except MbError:
        # Not an account this kit allocates as it stands: lock the name, never signal its UID.
        lock_worker_account(role)
        raise
    terminate_worker(account)
    return account


def finish_worker(state: Path) -> dict[str, Any]:
    """``ci worker-finish``: the sweep every job with accounts ends with, in any state.

    Both accounts are terminated and locked, whichever exist. Once both are locked, neither may
    own a process. Only then the runner home gets the mode it had before ``ci worker-prepare``
    closed it; a home whose accounts could not all be stopped stays closed. Running it again, or
    after a ``worker-prepare`` that failed anywhere, or without one, is safe. Returns what it
    found: the state of each account and the restored home mode (``None`` when this job never
    changed the home).
    """

    swept: dict[str, WorkerAccount | None] = {}
    failure: MbError | None = None
    for role in WORKER_ACCOUNTS:
        try:
            swept[role] = _sweep(role)
        except MbError as error:
            failure = failure or error
    if failure is not None:
        raise failure
    for role, account in swept.items():
        if account is not None and worker_processes(account):
            raise LifecycleError(f"the {role} account still owns a process after it was locked")
    report = {"accounts": {role: "absent" if account is None else "locked" for role, account in swept.items()},
              "home_mode": None}
    if not _recorded(state, HOST_NAME):
        if any(account is not None for account in swept.values()):
            raise LifecycleError("worker accounts exist that this job has no record of preparing")
        return report
    boundary = HostBoundary(**_read_record(state, HOST_NAME, _HOST, limits.MAX_CI_WORKER_RECORD_BYTES)["boundary"])
    restore_worker_host(boundary)
    return {**report, "home_mode": boundary.original_mode}


# -- ci worker-validate ------------------------------------------------------------------------------

#: ``ci worker-validate --hook``: the verification a job ends with -> the accounts that kind of job
#: allocated. A target and a lane are verified in the job whose candidate produced them; the
#: complete Build is verified in the assembling job, which has the validator alone.
VALIDATION_HOOKS = {"verify_target": ROLE_SETS["candidate+validator"], "verify_build": ROLE_SETS["validator"],
                    "verify_runtime": ROLE_SETS["candidate+validator"]}


def validation_unit(plan: dict[str, Any], *, hook: str, unit_id: str | None,
                    roles: tuple[str, ...]) -> dict[str, Any] | None:
    """The target or lane of ``plan`` that ``hook`` verifies in a job that allocated ``roles``
    (``None`` for ``verify_build``, which verifies every target).

    A hook that seals nothing, a unit outside the plan and a unit for ``verify_build`` or none for
    the other two are rejections, and so is a job that allocated other accounts than the hook's
    kind of job does.
    """

    check(type(hook) is str and hook in VALIDATION_HOOKS, "$.hook", "must be a verification hook")
    unit = adapter.plan_unit(plan, hook, unit_id)
    if tuple(roles) != VALIDATION_HOOKS[hook]:
        raise LifecycleError(f"{hook} runs in a job that allocated {' and '.join(VALIDATION_HOOKS[hook])}; "
                             f"this job allocated {' and '.join(roles)}")
    return unit


def validate_export(job: Job, worker: Worker, *, hook: str, unit_id: str | None, output: Path,
                    log: Callable[[str], object]) -> dict[str, Any]:
    """``ci worker-validate``: verify the job's sealed export as the validator and write the
    directory the job uploads.

    ``output`` must not exist; that is checked before anything else. The sealed input must be
    there and be the plan's: for ``verify_target`` the partition of target ``unit_id`` in
    ``sealed-build/``, for ``verify_build`` the complete Build there, both sealed by this run
    attempt and still private to the runner; for ``verify_runtime`` the results of lane
    ``unit_id`` in ``sealed-runtime/`` with the complete Build its envelope names in
    ``sealed-build/``. Root hands what is still private to the validator read-only
    (``grant-build-validation``, ``grant-runtime-validation``; the plan inputs were granted by
    ``ci plan``). The hook runs from the protected adapter copy with the inputs authenticated
    before and after; its result is published for root, which copies the reports out of the
    validator's home and seals them with the record it builds (``freeze-build-validation``,
    ``freeze-runtime-validation``). ``output`` then becomes the export root with the envelope,
    the validation record and the reports (``validation.materialize_validated_export``).

    Returns the envelope and the validation record. Every account ends terminated and locked,
    whatever happens; after a failure there is no ``output``.
    """

    output = Path(os.path.abspath(output))
    if os.path.lexists(output):
        raise LifecycleError("the upload directory exists already; `ci worker-validate` never replaces one")
    plan = read_plan(job)
    validation_unit(plan, hook=hook, unit_id=unit_id, roles=tuple(worker.accounts))
    boundary, validator, sources = worker.boundary, worker.validator, job.sources
    run = dict(run_id=job.run_id, run_attempt=job.run_attempt)

    def execute() -> WorkerResult:
        return run_protected_hook(job, worker, hook, plan=plan, unit_id=unit_id, log=log)

    def root(operation: str, nonce: str) -> None:
        _root(job, worker.python, operation, nonce)

    with resting(worker.accounts):
        if hook == "verify_runtime":
            build, envelope, granted = read_sealed_runtime(boundary=boundary, validator=validator, plan=plan,
                                                           lane_id=unit_id, **run)
            if not granted:  # A step that staged the Build for the lane may have handed it over already.
                root("grant-build-validation", request_build_validation_grant(
                    boundary=boundary, validator=validator, plan=plan, envelope=build))
            lane = dict(boundary=boundary, validator=validator, plan=plan, build=build, runtime=envelope,
                        lane_id=unit_id, **run)
            root("grant-runtime-validation", request_runtime_validation_grant(**lane))
            bound = execute_frozen_runtime_validator(**lane, execute=execute)
            nonce = record_runtime_validation_execution(**lane, sources=sources, bound=bound)
            root("freeze-runtime-validation",
                 request_runtime_validation_freeze(**lane, sources=sources, execution_nonce=nonce))
            export = RUNTIME_VALIDATION_ROOT
        else:
            envelope, granted = read_sealed_build(boundary=boundary, validator=validator, plan=plan)
            if granted:
                raise LifecycleError("sealed-build/ is not the private export this job has just sealed")
            build_input_sha256(plan=plan, envelope=envelope, hook=hook, unit_id=unit_id, **run)
            root("grant-build-validation", request_build_validation_grant(
                boundary=boundary, validator=validator, plan=plan, envelope=envelope))
            sealed = dict(boundary=boundary, plan=plan, envelope=envelope, **run)
            bound = execute_frozen_build_validator(**sealed, validator=validator, hook=hook, unit_id=unit_id,
                                                   execute=execute)
            nonce = record_build_validation_execution(**sealed, sources=sources, bound=bound)
            root("freeze-build-validation", request_build_validation_freeze(
                **sealed, validator=validator, sources=sources, execution_nonce=nonce))
            export = BUILD_VALIDATION_ROOT
        authenticate_host_boundary(boundary)
        _layout(boundary)
        authenticate_tree_private_access(SEALED_VALIDATION_ROOT, owner_uid=boundary.uid, owner_gid=boundary.gid,
                                         max_entries=limits.MAX_CI_EXPORT_ENTRIES)
        record = materialize_validated_export(
            Path(str(export)), Path(str(SEALED_VALIDATION_ROOT)), output, plan=plan, envelope=envelope, hook=hook,
            unit_id=unit_id, **run, source_config_sha256=sources.config.sha256, input_sha256=bound.input_sha256)
    return {"envelope": envelope, "validation": record}

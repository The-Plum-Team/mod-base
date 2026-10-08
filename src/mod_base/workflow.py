"""THE single source of the Pages and Build/E2E workflow, job and step display names (SPEC §5.9).

The jobs API reports a job of a called (reusable) workflow as ``"<caller job name> / <callee job
name>"``; caller-owned jobs keep their bare name, and so does a calling job that was skipped: its
callee never expands, so the API lists that one job under the caller's name. A matrix job that is
skipped before its matrix expands is listed once under its unexpanded YAML name. Consumers must
compare names with exact equality through :func:`find_job`; ``endswith``/``startswith`` matching is
forbidden. Producer-side names are config (``source.handoff_job/handoff_step``,
``families[].producer.job/step``), not constants here.
"""

from __future__ import annotations

import string
from collections.abc import Mapping, Sequence
from typing import Any

from mod_base.errors import MbError
from mod_base.model import grammar

#: Display name and path of the managed caller workflow (kept: QS feature-coverage depends on both).
PAGES_WORKFLOW_NAME = "Project site"
PAGES_WORKFLOW_PATH = ".github/workflows/pages.yml"
PAGES_EVENTS = frozenset({"workflow_dispatch", "schedule"})

# -- Build/E2E: BuildGraphV1 / PackagedGraphV1 names; public App contexts are caller-owned -----------
#: The mod's managed local reusable guard workflow. Every run of a managed caller lists it in
#: ``referenced_workflows`` at the commit the caller itself ran from: the run's controller commit.
CI_GUARD_WORKFLOW_PATH = ".github/workflows/mod-base-guard.yml"
#: Managed caller id -> its path in the mod. ``build`` and ``packaged`` are the two producers.
CI_CALLER_WORKFLOWS = {
    "build": ".github/workflows/mod-base-build.yml",
    "packaged": ".github/workflows/mod-base-packaged-e2e.yml",
    "status": ".github/workflows/mod-base-gate-status.yml",
}
#: Build/E2E callee workflow id -> its path inside the kit repository (never ``CALLEE_WORKFLOWS``).
CI_CALLEE_WORKFLOWS = {
    "build": ".github/workflows/build.yml",
    "select-build": ".github/workflows/select-build.yml",
    "packaged-e2e": ".github/workflows/packaged-e2e.yml",
}
CI_GUARD_CALL = "Verify pinned mod-base"
CI_BUILD_CALL = "Shared Build"
CI_SELECT_CALL = "Select exact Build"
CI_PACKAGED_CALL = "Shared Packaged E2E"
#: Managed caller id -> job key -> the bare display name of that caller-owned job.
CI_CALLER_JOBS = {
    "build": {"guard": CI_GUARD_CALL, "deferred": "Build deferred for draft", "shared": CI_BUILD_CALL},
    "packaged": {"guard": CI_GUARD_CALL, "deferred": "Packaged E2E deferred for draft",
                 "select": CI_SELECT_CALL, "rebuild": CI_BUILD_CALL, "shared": CI_PACKAGED_CALL},
    "status": {"evaluate": "Evaluate protected gates", "publish": "Publish protected gate statuses"},
}
#: Producer id -> calling job key -> the workflow it calls: ``guard`` is the mod's own
#: ``CI_GUARD_WORKFLOW_PATH``, every other value a ``CI_CALLEE_WORKFLOWS`` id.
CI_CALLS = {
    "build": {"guard": "guard", "shared": "build"},
    "packaged": {"guard": "guard", "select": "select-build", "rebuild": "build", "shared": "packaged-e2e"},
}
CI_GUARD_JOBS = {"verify": "Authenticate the pinned kit"}
CI_BUILD_JOBS = {"plan": "Plan protected Build", "policy": "Verify protected policy",
                 "target": "Compile target {id}", "assemble": "Seal complete Build bundle",
                 "gate": "Verify complete Build"}
CI_SELECT_JOBS = {"select": "Select exact Build source"}
CI_PACKAGED_JOBS = {"input": "Authenticate exact Build", "lane": "Run packaged lane {id}",
                    "aggregate": "Seal complete packaged results", "gate": "Verify complete packaged E2E"}
#: Called workflow id (a ``CI_CALLS`` value) -> job key -> its ``name:`` template.
CI_CALLEE_JOBS = {"guard": CI_GUARD_JOBS, "build": CI_BUILD_JOBS, "select-build": CI_SELECT_JOBS,
                  "packaged-e2e": CI_PACKAGED_JOBS}
CI_SEAL_STEP = "Validate frozen native exports"
CI_UPLOAD_STEP = "Upload sealed outputs"
#: Build/E2E callee id -> job key -> the ``ci`` verbs its steps issue after the Build controller
#: prologue, one verb per step and in step order. A callee enters this table and the two below
#: with its workflow file: ``build.yml`` and ``select-build.yml`` are written.
CI_JOB_VERBS = {
    "build": {
        "plan": ("subject", "worker-prepare", "plan", "reuse-admit", "worker-finish"),
        "policy": ("subject", "worker-prepare", "plan", "worker-stage", "worker-run", "worker-seal",
                   "worker-finish"),
        "target": ("subject", "worker-prepare", "plan", "worker-stage", "worker-run", "worker-seal",
                   "worker-validate", "worker-finish"),
        "assemble": ("subject", "worker-prepare", "plan", "assemble", "worker-validate", "worker-finish"),
        "gate": ("subject", "worker-prepare", "plan", "seal-gate", "worker-finish"),
    },
    "select-build": {
        "select": ("subject", "worker-prepare", "plan", "reuse-admit", "select-build", "worker-finish"),
    },
}
#: Build/E2E callee id -> sealing job key -> callee mode -> the kind (``grammar.ci_artifact_name``)
#: of the one artifact the job uploads in that mode. Exactly these jobs have a ``CI_SEAL_STEP``
#: directly followed by a ``CI_UPLOAD_STEP``; ``select-build`` has none and uploads nothing.
CI_JOB_ARTIFACTS = {
    "build": {"target": {"full": "target"}, "assemble": {"full": "build"},
              "gate": {"full": "tested", "reuse": "reuse"}},
}
#: Build/E2E callee id -> job key -> the complete ``permissions:`` of that job, which the calling
#: job of a managed caller must grant.
CI_JOB_PERMISSIONS = {
    callee: {job: {"actions": "read", "contents": "read", "pull-requests": "read"} for job in CI_CALLEE_JOBS[callee]}
    for callee in CI_JOB_VERBS
}
PAGES_CRON = "43 * * * *"
OPERATIONS = ("manual", "deploy", "family", "rotate")
#: The ``operation`` the caller passes to ``publish.yml`` (a schedule becomes ``recovery``).
PUBLISH_OPERATIONS = ("recovery", "manual", "deploy", "family")
PUBLICATION_LOCK = "mod-base-pages-publication"
ROTATION_LOCK = "mod-base-pages-rotation"

#: Callee workflow id -> its path inside the kit repository.
CALLEE_WORKFLOWS = {
    "publish": ".github/workflows/publish.yml",
    "finalize": ".github/workflows/finalize.yml",
    "rotate": ".github/workflows/rotate.yml",
}

CALLER = {
    "verify_kit": "Verify pinned mod-base",
    "publish": "Publish",
    "deploy": "Deploy GitHub Pages",
    "finalize": "Finalize",
    "request_rotation": "Request post-success evidence rotation",
    "rotate": "Rotate",
}
CALLEE = {
    "publish": {
        "admit": "Admit publication",
        "collect": "Collect {key}",
        "family": "Collect {family} {key}",
        "build": "Build atomic static site",
    },
    "finalize": {
        "refresh": "Refresh evidence cache for {key}",
        "refresh_family": "Refresh {family} cache for {key}",
    },
    "rotate": {"rotate": "Rotate the authenticated successful generation"},
}
STEPS = {
    "select": "Select the newest authenticated evidence",
    "family_select": "Select the newest authenticated family generation",
    "cache_upload": "Roll the protected evidence cache forward",
    "family_cache_upload": "Roll the protected family cache forward",
    "baseline_upload": "Retain the complete compact generation for feature evidence reuse",
}

#: Placeholder -> the ``${{ matrix.* }}`` expression the callee YAML uses for it.
MATRIX_EXPRESSIONS = {"key": "${{ matrix.key }}", "family": "${{ matrix.family }}", "id": "${{ matrix.id }}"}


def _placeholders(template: str) -> set[str]:
    return {field for _, field, _, _ in string.Formatter().parse(template) if field is not None}


def _validate_fields(template: str, fields: Mapping[str, Any]) -> None:
    wanted = _placeholders(template)
    if set(fields) != wanted:
        raise MbError(f"job name {template!r} needs exactly the fields {sorted(wanted)}")
    if "key" in fields:
        grammar.require_key(fields["key"])
    if "family" in fields:
        grammar.require_family(fields["family"])
    if "id" in fields:
        grammar.require(grammar.CI_UNIT_ID, fields["id"], "Build/E2E unit id")


def _yaml_template(template: str) -> str:
    result = template
    for field in _placeholders(template):
        result = result.replace("{" + field + "}", MATRIX_EXPRESSIONS[field])
    return result


def caller_job_name(key: str) -> str:
    """Return the bare display name of a caller-owned job, e.g. ``"Deploy GitHub Pages"``."""

    try:
        return CALLER[key]
    except KeyError:
        raise MbError(f"unknown caller job {key!r}") from None


def callee_job_name(workflow: str, job: str, **fields: str) -> str:
    """Return the callee's own ``name:`` value with its placeholders filled (no caller prefix)."""

    try:
        template = CALLEE[workflow][job]
    except KeyError:
        raise MbError(f"unknown callee job {workflow!r}/{job!r}") from None
    _validate_fields(template, fields)
    return template.format(**fields)


def api_job_name(workflow: str, job: str, **fields: str) -> str:
    """Return the name the jobs API reports for a callee job, e.g. ``"Publish / Collect mc1.20.1"``."""

    return f"{CALLER[workflow]} / {callee_job_name(workflow, job, **fields)}"


def workflow_template_name(workflow: str, job: str) -> str:
    """Return the callee job ``name:`` exactly as written in its YAML (``${{ matrix.* }}`` form)."""

    try:
        template = CALLEE[workflow][job]
    except KeyError:
        raise MbError(f"unknown callee job {workflow!r}/{job!r}") from None
    return _yaml_template(template)


def unexpanded_api_job_name(workflow: str, job: str) -> str:
    """Return the name the jobs API reports, once, for a matrix callee job that its job-level ``if``
    skipped before its matrix expanded: the caller prefix and the unexpanded YAML template name, e.g.
    ``"Publish / Collect ${{ matrix.family }} ${{ matrix.key }}"`` (Quick Skin
    ``e2e_job_graph.UNEXPANDED_SCENARIO_JOB``). Exact equality only, like every other name."""

    return f"{CALLER[workflow]} / {workflow_template_name(workflow, job)}"


def _ci_name(table: Mapping[str, Mapping[str, str]], group: str, key: str, label: str) -> str:
    try:
        return table[group][key]
    except (KeyError, TypeError):
        raise MbError(f"unknown Build/E2E {label} {group!r}/{key!r}") from None


def ci_producer(workflow_path: str) -> str:
    """Return the producer id (``"build"`` or ``"packaged"``) of the managed caller at
    ``workflow_path``; any other path is not a Build/E2E producer and raises."""

    for producer in CI_CALLS:
        if CI_CALLER_WORKFLOWS[producer] == workflow_path:
            return producer
    raise MbError("workflow is not a managed Build/E2E producer caller", reason="job-graph")


def ci_caller_job_name(caller: str, job: str) -> str:
    """Return the bare display name of a job the managed caller ``caller`` owns, e.g.
    ``"Build deferred for draft"``: what the jobs API reports for a caller job that has steps."""

    return _ci_name(CI_CALLER_JOBS, caller, job, "caller job")


def ci_callee_job_name(callee: str, job: str, **fields: str) -> str:
    """Return a called workflow's own ``name:`` value with its placeholders filled (no caller prefix)."""

    template = _ci_name(CI_CALLEE_JOBS, callee, job, "callee job")
    _validate_fields(template, fields)
    return template.format(**fields)


def ci_api_job_name(producer: str, call: str, job: str, **fields: str) -> str:
    """Return the name the jobs API reports for job ``job`` of the workflow that the producer's
    calling job ``call`` calls, e.g. ``"Shared Build / Compile target mc1.20.1"``."""

    callee = _ci_name(CI_CALLS, producer, call, "calling job")
    return f"{ci_caller_job_name(producer, call)} / {ci_callee_job_name(callee, job, **fields)}"


def ci_skipped_call_job_name(producer: str, call: str) -> str:
    """Return the name the jobs API reports, once, for the producer's calling job ``call`` when its
    job-level ``if`` skipped the call: the caller's bare job name, with no callee job behind it."""

    _ci_name(CI_CALLS, producer, call, "calling job")
    return ci_caller_job_name(producer, call)


def ci_workflow_template_name(callee: str, job: str) -> str:
    """Return a called workflow's job ``name:`` exactly as written in its YAML (``${{ matrix.id }}``
    form)."""

    return _yaml_template(_ci_name(CI_CALLEE_JOBS, callee, job, "callee job"))


def ci_unexpanded_api_job_name(producer: str, call: str, job: str) -> str:
    """Return the name the jobs API reports, once, for a matrix job of a called workflow that was
    skipped before its matrix expanded, e.g. ``"Shared Build / Compile target ${{ matrix.id }}"``."""

    template = _ci_name(CI_CALLEE_JOBS, _ci_name(CI_CALLS, producer, call, "calling job"), job, "callee job")
    if not _placeholders(template):
        raise MbError(f"Build/E2E callee job {job!r} is not a matrix job")
    return f"{ci_caller_job_name(producer, call)} / {_yaml_template(template)}"


def step_name(key: str) -> str:
    try:
        return STEPS[key]
    except KeyError:
        raise MbError(f"unknown step {key!r}") from None


def find_job(jobs: Sequence[Mapping[str, Any]], name: str, *, run_attempt: int) -> dict[str, Any]:
    """Return the single job named exactly ``name`` in ``run_attempt``.

    Exact equality only; exactly one job in ``jobs`` may carry ``name`` and it must report
    ``run_attempt``. Anything else (no match, several matches, a match from another attempt, a
    malformed record) raises :class:`MbError`. Never ``endswith``/``startswith``.
    """

    if not isinstance(name, str) or not name:
        raise MbError("job name must be a non-empty string")
    if isinstance(run_attempt, bool) or not isinstance(run_attempt, int) or run_attempt <= 0:
        raise MbError("run attempt must be a positive integer")
    if not isinstance(jobs, Sequence) or isinstance(jobs, (str, bytes)):
        raise MbError("jobs must be a list of job records")
    matches: list[Mapping[str, Any]] = []
    for job in jobs:
        if not isinstance(job, Mapping) or not isinstance(job.get("name"), str):
            raise MbError("job record is malformed")
        if job["name"] == name:
            matches.append(job)
    if len(matches) != 1:
        raise MbError(f"expected exactly one job named {name!r}, found {len(matches)}", reason="job-graph")
    job = matches[0]
    attempt = job.get("run_attempt")
    if isinstance(attempt, bool) or attempt != run_attempt:
        raise MbError(f"job {name!r} does not belong to attempt {run_attempt}", reason="job-graph")
    return dict(job)

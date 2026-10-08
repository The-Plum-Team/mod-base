"""One PR generation's request ledger, composed from the actual callee command lines.

Counts are the client's conservative count: downloading one artifact spends two requests,
the REST redirect and a credential-free storage GET. The latter is not charged to the token's
REST rate limit. No retries, unrelated concurrent generations or earlier status events are
included. The named command tests pin every nonzero cost below; the hosted generation compares
its observed commands to the same ledger.
"""

from __future__ import annotations

from dataclasses import dataclass

from tests.test_workflow_ci_policy import ci_callee, step_verb


@dataclass(frozen=True)
class Cost:
    base: int
    proof: str
    targets: int = 0
    lanes: int = 0
    extra_inputs: int = 0
    polls: int = 0


#: No waiting poll when the input job starts after the complete Build, as the hosted generation
#: does. Every pending poll adds one; the wait's own deadline is tested separately.
WAITING_POLLS = 0
COSTS = {
    "subject": Cost(4, "test_ci_commands_subject.SubjectCommandTests."
                       "test_a_pull_request_writes_the_record_and_outputs_in_four_budgeted_requests"),
    "subject:candidate": Cost(1, "test_ci_commands_subject.DerivedSubjectCommandTests."
                                 "test_a_pull_request_writes_what_the_full_command_writes_from_one_budgeted_request"),
    "plan": Cost(3, "ci_linux_worker.LinuxLifecycleCommandTests.test_validator_job_plans_from_the_api_and_finishes",
                 extra_inputs=1),
    "plan:candidate": Cost(0, "ci_linux_worker.LinuxLifecycleCommandTests."
                              "test_candidate_and_validator_job_plans_from_the_candidate_checkout_without_the_api"),
    "select-build": Cost(17, "test_ci_commands_packaged.SelectBuildCommandTests."
                             "test_a_pull_request_selects_the_newest_build_of_its_head_in_seventeen_requests", polls=1),
    "fetch-build": Cost(2, "test_ci_commands_packaged.FetchBuildCommandTests."
                           "test_each_route_materialises_the_selected_bundle_in_the_fixed_root"),
    "assemble": Cost(15, "test_ci_commands_build.AssembleCommandTests."
                         "test_seventeen_targets_cost_forty_nine_requests", targets=2),
    "aggregate": Cost(13, "test_ci_aggregate.AggregateCommandTests."
                          "test_thirty_four_lanes_cost_two_requests_each_and_thirteen", lanes=2),
    "seal-gate:build": Cost(15, "test_ci_gate.BuildGateTests.test_seventeen_targets_cost_the_same_fifteen_requests"),
    "seal-gate:packaged": Cost(23, "test_ci_gate.PackagedGateTests."
                                 "test_thirty_four_lanes_cost_the_same_twenty_three_requests"),
    "gate-status:settle": Cost(5, "test_ci_commands_status.GateSettleTests."
                                 "test_the_two_calls_of_one_job_end_in_the_verified_document"),
    "gate-status": Cost(45, "test_ci_commands_status.GateStatusTests."
                            "test_both_green_gates_are_success_on_the_pull_request_head_within_budget"),
}
#: These commands execute local protected work and cannot create an API client. PR reuse admission
#: is tested to return full without a request. The hosted generation pins all their observed zeros.
LOCAL_VERBS = frozenset({"worker-prepare", "worker-stage", "worker-run", "worker-seal", "worker-validate",
                         "worker-finish", "reuse-admit"})


def request_cost(route: str, *, targets: int = 1, lanes: int = 1, extra_inputs: int = 0,
                 polls: int = WAITING_POLLS) -> int:
    """The pinned cost for a command, for a run whose job/artifact listings fit one page."""

    if route in LOCAL_VERBS:
        return 0
    cost = COSTS[route]
    return cost.base + cost.targets * targets + cost.lanes * lanes + cost.extra_inputs * extra_inputs + cost.polls * polls


def command_route(verb: str, script: str, workflow: str) -> str:
    if verb in ("subject", "plan") and "--candidate candidate" in script:
        return verb + ":candidate"
    if verb == "seal-gate":
        return verb + (":build" if workflow == "build" else ":packaged")
    if verb == "gate-status" and "--settle" in script:
        return "gate-status:settle"
    return verb


def generation_breakdown(*, targets: int, lanes: int, extra_inputs: int = 0,
                         polls: int = WAITING_POLLS) -> dict[str, int]:
    """Requests per job of Build, packaged E2E and one final status evaluation, from their YAML.

    Above 100 jobs/artifacts this is a strict lower bound: further listing pages add requests.
    Structural maxima are deliberately not a promise that one generation fits the token budget.
    """

    result = {}
    for workflow in ("build", "packaged-e2e", "gate-status"):
        for job_id, job in ci_callee(workflow)["jobs"].items():
            multiplier = 1
            if "strategy" in job:
                matrix = job["strategy"]["matrix"]["id"]
                if matrix.endswith(".targets) }}"):
                    multiplier = targets
                elif matrix.endswith(".lanes) }}"):
                    multiplier = lanes
                else:
                    raise AssertionError(f"unbudgeted matrix {workflow}/{job_id}: {matrix}")
            result[f"{workflow}/{job_id}"] = multiplier * sum(
                request_cost(command_route(verb, item["run"], workflow), targets=targets, lanes=lanes,
                             extra_inputs=extra_inputs, polls=polls)
                for item in job["steps"] if (verb := step_verb(item)) is not None)
    return result

"""The managed gate status caller ``mod-base-gate-status.yml`` (docs/BUILD-E2E-DESIGN.md: "Stable
contexts and exact graphs", "Identity and execution boundaries").

Every case reads the template as ``template sync`` renders it for a mod. Structure: its four
triggers, permissions, the one secret and the one environment in the members and publishing jobs
alone, the one third-party action at its reviewed pin, the non-cancelling lock of a pull request.
Jobs: the ``if:`` conditions are evaluated with GitHub's expression rules, so that no failed,
skipped or empty evaluation ever reaches the publishing job, and the members job runs only when the
protected admission counts an organization's members. The guard's shell admits the caller on its
four events. The ``locate``, ``members`` and ``publish`` scripts run under the shell harness against
a stub ``gh``: which pull request an event names and whether its admission counts members, which
comment authors are checked and which of them are members, and which statuses reach GitHub for which
evaluated document, which live pull request and which membership.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from mod_base import workflow
from mod_base.build_ci.activation import BUILD_CALLER, CALLERS, GUARD_CALLER, PACKAGED_CALLER, STATUS_CALLER
from mod_base.build_ci.membership import load_org_members
from mod_base.model import limits
from mod_base.model.canonical import canonical_json
from mod_base.pin import parse_pin_files
from mod_base.template import tool
from tests.test_ci_activation import MANAGED, STATES
from tests.test_managed_ci_callers import (BRANCH, HEAD, KIT_SHA, LOCAL_GUARD, NAMES, OTHER, PR_TYPES, READ_ALL,
                                           READ_RUN, REPOSITORY, RUN_ID, STATUS_EVENTS, TOKEN, VERSION, GuardCase,
                                           declared, event, interpolate, kit_workflow, needed, references, rendered,
                                           simulate)
from tests.test_template_callers import enter, real_mod, row
from tests.test_workflow_policy import (APP_TOKEN, EXPRESSION, PINNED_ACTIONS, ROOT, ShellHarness, caller, load_yaml,
                                        outputs, parse_yaml, require_tools, step)

NAME = "mod-base gate status"
ENVIRONMENT = "mod-base-gate"
CLIENT_ID = "MOD_BASE_GATE_APP_CLIENT_ID"
PRIVATE_KEY = "MOD_BASE_GATE_APP_PRIVATE_KEY"
CRON = "17 * * * *"
LOCATE_STEP = "Find the pull request of this event"
MINT_STEP = "Mint a statuses-only App token"
PUBLISH_STEP = "Publish the evaluated statuses on the live pull request head"
AUTHORS_STEP = "Read the authors of the pull request comments"
MEMBERS_MINT_STEP = "Mint a members-read App token"
SNAPSHOT_STEP = "Read the membership of the candidates"
RECHECK_STEP = "Recheck the membership of the candidates"
READ_PULLS = {"pull-requests": "read"}
READ_LOCATE = {"contents": "read", "pull-requests": "read"}
#: The members job runs exactly when the protected config's admission counts the organization's members.
COUNTED = "needs.locate.outputs.organization != ''"
#: The evaluation runs for a located pull request once the guard and the members job, when it had
#: to run, succeeded; a skipped members job is enough only when it was not asked to run.
LOCATED = ("${{ !cancelled() && needs.guard.result == 'success' && needs.locate.result == 'success' && "
           "needs.locate.outputs.pr-number != '' && (needs.members.result == 'success' || "
           "(needs.members.result == 'skipped' && needs.locate.outputs.organization == '')) }}")
EVALUATED = "${{ !cancelled() && needs.evaluate.result == 'success' && needs.evaluate.outputs.intents != '' }}"
RECHECKED = "needs.members.outputs.recheck == 'true'"
GROUP = ("mod-base-gate-status-${{ github.event.pull_request.number || "
         "github.event.workflow_run.pull_requests[0].number || inputs.pr-number || "
         "format('run-{0}', github.run_id) }}")
OWNER = REPOSITORY.split("/")[0]
APP_TOKEN_VALUE = "fixture-app-token"
APP_SLUG = "quick-skin-gate"
WRITER = APP_SLUG + "[bot]"
FEATURE = "feature/gates"
PULLS = f"GET repos/{REPOSITORY}/pulls?state=open&base={BRANCH}"
RUNS = f"https://github.com/{REPOSITORY}/actions/runs/"
CONTEXTS = {"build": "Trusted PR / Build and verify", "packaged": "Trusted PR / Packaged E2E gate"}


def text() -> str:
    return rendered(paths=(STATUS_CALLER,))[STATUS_CALLER]


def document() -> dict[str, Any]:
    return parse_yaml(text(), STATUS_CALLER)


def script(job: str, name: str) -> str:
    return step(document()["jobs"][job]["steps"], name)["run"]


def pull(number: int = 17, **changes: Any) -> dict[str, Any]:
    """One pull request of the repository against its default branch, as the API lists it."""

    record = {"number": number, "state": "open", "draft": False,
              "head": {"ref": FEATURE, "sha": HEAD, "repo": {"full_name": REPOSITORY}},
              "base": {"ref": BRANCH, "sha": OTHER, "repo": {"full_name": REPOSITORY}}}
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(record.get(key), dict):
            record[key] = {**record[key], **value}
        else:
            record[key] = value
    return record


def intent(gate: str, state: str = "success", run_id: int | None = 42, **changes: Any) -> dict[str, Any]:
    return {"context": CONTEXTS[gate], "state": state, "description": f"the newest {gate} run: {state}",
            "target_url": None if run_id is None else f"{RUNS}{run_id}", **changes}


def intents(build: Mapping[str, Any] | None = None, packaged: Mapping[str, Any] | None = None,
            **changes: Any) -> dict[str, Any]:
    """The document ``ci gate-status`` returns for pull request 17: both gates verified by default."""

    return {"repository": REPOSITORY, "pr_number": 17, "target_sha": HEAD,
            "gates": {"build": dict(build or intent("build")),
                      "packaged": dict(packaged or intent("packaged", run_id=43))}, **changes}


class StatusCallerStructureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = text()
        self.document = document()
        self.jobs = self.document["jobs"]

    def test_header_name_and_top_level_keys(self) -> None:
        self.assertRegex(self.text.splitlines()[0], r"^# mod-base managed: .+, edit only in The-Plum-Team/mod-base "
                                                    r"template/managed/" + re.escape(STATUS_CALLER) + "$")
        self.assertNotIn("PROVISIONAL", self.text)
        self.assertEqual(list(self.document), ["name", "on", "permissions", "concurrency", "jobs"])
        self.assertEqual(self.document["name"], NAME)
        self.assertNotIn(NAME, NAMES.values(), "no name of a workflow whose runs start this one")
        self.assertNotIn("{{", self.text.replace("${{", ""), "no placeholder is left")
        self.assertNotIn("\t", self.text)
        self.assertEqual(rendered(branch="release/1.21", paths=(STATUS_CALLER,))[STATUS_CALLER],
                         self.text.replace('    branches: ["master"]', '    branches: ["release/1.21"]'),
                         "only the pull request base filter changes with the canonical branch")

    def test_the_four_triggers_are_exactly_the_specified_ones(self) -> None:
        triggers = self.document["on"]
        self.assertEqual(tuple(triggers), STATUS_EVENTS)
        # Both gate callers, by the names their templates carry, when a run is requested and when
        # it has completed: the generation is pending from its first moment.
        self.assertEqual(triggers["workflow_run"], {"workflows": [NAMES[BUILD_CALLER], NAMES[PACKAGED_CALLER]],
                                                    "types": ["requested", "completed"]})
        gate_callers = rendered()
        for path in (BUILD_CALLER, PACKAGED_CALLER):
            gate = parse_yaml(gate_callers[path], path)
            self.assertIn(gate["name"], triggers["workflow_run"]["workflows"])
            # The same five kinds of event that start a generation of the gates start its evaluation.
            self.assertEqual(triggers["pull_request_target"], gate["on"]["pull_request_target"])
        self.assertEqual(triggers["pull_request_target"], {"branches": [BRANCH], "types": PR_TYPES})
        self.assertEqual(triggers["schedule"], [{"cron": CRON}])
        self.assertRegex(CRON, r"^[0-5]?[0-9] \* \* \* \*$", "once an hour")
        self.assertNotEqual(CRON, workflow.PAGES_CRON, "not in the minute of the Pages caller")
        self.assertEqual(triggers["workflow_dispatch"], {"inputs": {"pr-number": {
            "description": "The pull request whose gate statuses are evaluated and published again",
            "required": "true", "type": "string"}}})
        for absent in ("push:", "pull_request:", "issue_comment", "repository_dispatch", "workflow_call"):
            self.assertNotIn(absent, self.text)

    def test_no_permission_at_the_top_level_and_only_read_grants_below(self) -> None:
        self.assertEqual(self.document["permissions"], {})
        self.assertEqual({job_id: job["permissions"] for job_id, job in self.jobs.items()},
                         {"guard": READ_RUN, "locate": READ_LOCATE, "members": READ_PULLS, "evaluate": READ_ALL,
                          "publish": READ_PULLS})
        # The one `write` of the file is what the App token may do, and it may do nothing else.
        self.assertEqual([line.strip() for line in self.text.splitlines() if re.search(r"\bwrite\b", line)
                          and not line.lstrip().startswith("#")], ["permission-statuses: write"])
        self.assertNotIn("id-token", self.text)

    def test_the_secret_the_variable_and_the_environment_are_named_in_the_members_and_publishing_jobs_alone(
            self) -> None:
        # The App key appears only in the `with` of the three mint steps.
        self.assertEqual(self.text.count("secrets"), 3)
        self.assertEqual(self.text.count("vars."), 3)
        self.assertEqual(self.text.count("${{ secrets." + PRIVATE_KEY + " }}"), 3)
        self.assertEqual(len(re.findall(r"^\s*environment:", self.text, re.MULTILINE)), 2)
        self.assertEqual({job_id: job.get("environment") for job_id, job in self.jobs.items()},
                         {"guard": None, "locate": None, "members": ENVIRONMENT, "evaluate": None,
                          "publish": ENVIRONMENT})
        key = {"client-id": "${{ vars." + CLIENT_ID + " }}", "private-key": "${{ secrets." + PRIVATE_KEY + " }}"}
        mint = step(self.jobs["publish"]["steps"], MINT_STEP)
        self.assertEqual(mint, {"name": MINT_STEP, "id": "app", "uses": APP_TOKEN,
                                "with": {**key, "permission-statuses": "write"}})
        members_mint = {"name": MEMBERS_MINT_STEP, "id": "app-members", "uses": APP_TOKEN,
                        "with": {**key, "permission-members": "read"}}
        self.assertEqual(step(self.jobs["members"]["steps"], MEMBERS_MINT_STEP),
                         {**members_mint, "if": "steps.authors.outputs.candidates != '[]'"})
        self.assertEqual(step(self.jobs["publish"]["steps"], MEMBERS_MINT_STEP), {**members_mint, "if": RECHECKED})
        # No owner and no repository list: each token is scoped to this repository alone, and may
        # do one thing: write statuses, or read the organization's members.
        for item in (mint, members_mint):
            self.assertEqual([name for name in item["with"] if name not in key], [
                "permission-statuses" if item is mint else "permission-members"])
        self.assertEqual(self.text.count("permission-members: read"), 2)
        self.assertEqual(self.text.count("permission-"), 3)
        for job_id in ("guard", "locate", "evaluate"):
            body = json.dumps(self.jobs[job_id])
            for word in ("secrets", "vars.", ENVIRONMENT, "STATUS_TOKEN", "steps.app"):
                self.assertNotIn(word, body, f"{job_id} never sees {word}")
        self.assertNotIn("secrets", self.jobs["evaluate"], "the kit workflow is passed no secret")
        self.assertNotIn("inherit", self.text)
        # The statuses token reaches one step, the publishing script, as one variable; the
        # members token reaches the step that reads the membership in each of its two jobs.
        self.assertEqual(self.text.count("steps.app.outputs.token"), 1)
        self.assertEqual(self.text.count("steps.app.outputs."), 2, "the token and the name the App writes under")
        self.assertEqual(step(self.jobs["publish"]["steps"], PUBLISH_STEP)["env"]["STATUS_TOKEN"],
                         "${{ steps.app.outputs.token }}")
        self.assertEqual(self.text.count("steps.app-members.outputs."), 2)
        for job_id, name in (("members", SNAPSHOT_STEP), ("publish", RECHECK_STEP)):
            self.assertEqual(step(self.jobs[job_id]["steps"], name)["env"]["GH_TOKEN"],
                             "${{ steps.app-members.outputs.token }}")
        # The members token is minted before the statuses token, and only after the comments
        # were read with the job token.
        names = [item["name"] for item in self.jobs["publish"]["steps"]]
        self.assertEqual(names, [MEMBERS_MINT_STEP, RECHECK_STEP, MINT_STEP, PUBLISH_STEP])
        self.assertEqual([item["name"] for item in self.jobs["members"]["steps"]],
                         [AUTHORS_STEP, MEMBERS_MINT_STEP, SNAPSHOT_STEP])

    def test_the_app_token_action_is_the_one_action_at_its_reviewed_pin(self) -> None:
        used = {(job_id, item["name"]): item["uses"] for job_id, job in self.jobs.items()
                for item in job.get("steps", []) if "uses" in item}
        self.assertEqual(used, {("members", MEMBERS_MINT_STEP): APP_TOKEN, ("publish", MEMBERS_MINT_STEP): APP_TOKEN,
                                ("publish", MINT_STEP): APP_TOKEN})
        self.assertEqual(APP_TOKEN, "actions/create-github-app-token@bcd2ba49218906704ab6c1aa796996da409d3eb1")
        self.assertEqual(PINNED_ACTIONS[APP_TOKEN], "v3.2.0")
        self.assertEqual(self.text.count(f"        uses: {APP_TOKEN} # {PINNED_ACTIONS[APP_TOKEN]}\n"), 3)
        self.assertNotIn("actions/checkout", self.text, "no job of the caller checks anything out")
        # Wherever third-party actions of managed callers are listed: a mod that manages this
        # caller must keep Dependabot off the action, and a freshly seeded mod already does.
        seed = (ROOT / "template/seed/.github/dependabot.yml.tmpl").read_text(encoding="utf-8")
        self.assertIn('      - dependency-name: "actions/create-github-app-token"\n', seed)
        own = (ROOT / ".github/dependabot.yml").read_text(encoding="utf-8")
        self.assertIn("only user of actions/create-github-app-token", own)

    def test_jobs_are_the_registered_ones_in_order(self) -> None:
        self.assertEqual([(job_id, job["name"]) for job_id, job in self.jobs.items()],
                         list(workflow.CI_CALLER_JOBS["status"].items()))
        self.assertEqual({job_id: job["uses"] for job_id, job in self.jobs.items() if "uses" in job},
                         {"guard": LOCAL_GUARD, "evaluate": f"{kit_workflow('gate-status')}@{KIT_SHA}"})
        self.assertEqual(set(workflow.CI_STATUS_CALLS), {job_id for job_id, job in self.jobs.items() if "uses" in job})
        self.assertEqual({job_id: job.get("needs") for job_id, job in self.jobs.items()},
                         {"guard": None, "locate": None, "members": ["locate"], "evaluate": ["guard", "locate", "members"],
                          "publish": ["locate", "members", "evaluate"]})
        self.assertEqual({job_id: job.get("if") for job_id, job in self.jobs.items()},
                         {"guard": None, "locate": None, "members": COUNTED, "evaluate": LOCATED, "publish": EVALUATED})
        for job_id in ("locate", "members", "publish"):
            self.assertEqual((self.jobs[job_id]["runs-on"], self.jobs[job_id]["timeout-minutes"]),
                             ("ubuntu-24.04", "5"))
        self.assertEqual(workflow.ci_api_job_name("status", "evaluate", "evaluate"),
                         "Evaluate protected gates / Evaluate gate states")
        files = {path: content.encode("utf-8") for path, content in
                 rendered(paths=(GUARD_CALLER, BUILD_CALLER, PACKAGED_CALLER, STATUS_CALLER)).items()}
        pin = parse_pin_files(files)
        self.assertEqual((pin.sha, pin.version, len(pin.references)), (KIT_SHA, VERSION, 5))

    def test_calls_pass_exactly_the_callee_inputs_and_grant_what_its_job_holds(self) -> None:
        self.assertEqual(self.jobs["guard"]["with"], {"callees": declared("status")})
        self.assertEqual(declared("status"), "gate-status")
        guard = parse_yaml(rendered()[GUARD_CALLER], GUARD_CALLER)
        self.assertEqual(self.jobs["guard"]["permissions"], guard["jobs"]["verify"]["permissions"])
        passed = self.jobs["evaluate"]["with"]
        self.assertEqual(list(passed.items()), [("kit-sha", "${{ needs.guard.outputs.kit-sha }}"),
                                                ("pr-number", "${{ needs.locate.outputs.pr-number }}"),
                                                ("members", "${{ needs.members.outputs.members }}")])
        callee = load_yaml(ROOT / workflow.CI_CALLEE_WORKFLOWS["gate-status"])
        inputs = callee["on"]["workflow_call"]["inputs"]
        self.assertEqual(list(passed), list(inputs), "every input, in the callee's order")
        self.assertEqual(list(callee["on"]["workflow_call"]["outputs"]), ["intents"])
        held = [job["permissions"] for job in callee["jobs"].values()]
        self.assertEqual(held, [self.jobs["evaluate"]["permissions"]])
        self.assertEqual(held, list(workflow.CI_JOB_PERMISSIONS["gate-status"].values()))
        self.assertEqual(self.jobs["locate"]["outputs"], {"pr-number": "${{ steps.locate.outputs.pr-number }}",
                                                          "organization": "${{ steps.locate.outputs.organization }}"})
        self.assertEqual(self.jobs["members"]["outputs"], {"members": "${{ steps.members.outputs.members }}",
                                                           "candidates": "${{ steps.authors.outputs.candidates }}",
                                                           "recheck": "${{ steps.members.outputs.recheck }}"})

    def test_one_non_cancelling_lock_for_each_pull_request(self) -> None:
        concurrency = self.document["concurrency"]
        self.assertEqual(concurrency, {"group": GROUP, "cancel-in-progress": "false"})
        self.assertNotIn("concurrency", json.dumps(self.jobs), "no job takes a second lock")

        def group(github: Mapping[str, Any], dispatched: str | None = None) -> str:
            context = {"github": github, "inputs": {} if dispatched is None else {"pr-number": dispatched}}
            return interpolate(concurrency["group"], context)

        def after(run: Mapping[str, Any]) -> dict[str, Any]:
            return {**event("workflow_run"), "event": {"workflow_run": run}}

        lock = "mod-base-gate-status-17"
        self.assertEqual(group(event("pull_request_target", draft=False)), lock)
        self.assertEqual(group(event("pull_request_target", draft=True)), lock)
        self.assertEqual(group(after({"pull_requests": [{"number": 17}]})), lock)
        self.assertEqual(group(event("workflow_dispatch"), "17"), lock)
        self.assertEqual(group(event("pull_request_target", draft=False, number=18)), "mod-base-gate-status-18")
        # A gate run of a push or of a dispatch names no pull request, and neither does the hourly
        # run before it has listed any: each such run has a group of its own.
        for github in (after({"pull_requests": []}), after({}), event("schedule")):
            self.assertEqual(group(github), f"mod-base-gate-status-run-{RUN_ID}")
        other = {**event("schedule"), "run_id": int(RUN_ID) + 1}
        self.assertNotEqual(group(other), group(event("schedule")))

    def test_event_data_reaches_shell_only_through_env(self) -> None:
        environments = {
            ("locate", LOCATE_STEP): {
                "GH_TOKEN": "${{ github.token }}", "EVENT_PR_NUMBER": "${{ github.event.pull_request.number }}",
                "DISPATCH_PR_NUMBER": "${{ inputs.pr-number }}", "RUN_EVENT": "${{ github.event.workflow_run.event }}",
                "RUN_HEAD_BRANCH": "${{ github.event.workflow_run.head_branch }}",
                "RUN_HEAD_SHA": "${{ github.event.workflow_run.head_sha }}",
                "RUN_HEAD_REPOSITORY": "${{ github.event.workflow_run.head_repository.full_name }}"},
            ("members", AUTHORS_STEP): {
                "GH_TOKEN": "${{ github.token }}", "PR_NUMBER": "${{ needs.locate.outputs.pr-number }}"},
            ("members", SNAPSHOT_STEP): {
                "GH_TOKEN": "${{ steps.app-members.outputs.token }}",
                "ORGANIZATION": "${{ needs.locate.outputs.organization }}",
                "PR_NUMBER": "${{ needs.locate.outputs.pr-number }}",
                "CHECKED": "${{ steps.authors.outputs.checked }}", "THROUGH": "${{ steps.authors.outputs.through }}",
                "CANDIDATES": "${{ steps.authors.outputs.candidates }}"},
            ("publish", RECHECK_STEP): {
                "GH_TOKEN": "${{ steps.app-members.outputs.token }}",
                "ORGANIZATION": "${{ needs.locate.outputs.organization }}",
                "MEMBERS": "${{ needs.members.outputs.members }}",
                "CANDIDATES": "${{ needs.members.outputs.candidates }}"},
            ("publish", PUBLISH_STEP): {
                "GH_TOKEN": "${{ github.token }}", "STATUS_TOKEN": "${{ steps.app.outputs.token }}",
                "APP_SLUG": "${{ steps.app.outputs.app-slug }}",
                "EVALUATION": "${{ needs.evaluate.result }}", "INTENTS": "${{ needs.evaluate.outputs.intents }}",
                "PR_NUMBER": "${{ needs.locate.outputs.pr-number }}"},
        }
        seen = {}
        for job_id, job in self.jobs.items():
            for item in job.get("steps", []):
                if "uses" in item:
                    continue
                # Only the recheck has a condition: it runs when the evaluation counted a member.
                self.assertEqual(set(item), {"name", "shell", "env", "run"} | ({"id"} if "id" in item else set())
                                 | ({"if"} if item["name"] == RECHECK_STEP else set()))
                self.assertEqual(item.get("if", RECHECKED), RECHECKED)
                self.assertEqual(item["shell"], "bash")
                self.assertNotRegex(item["run"], EXPRESSION)
                self.assertTrue(item["run"].startswith("set -euo pipefail\n"))
                seen[(job_id, item["name"])] = item["env"]
        self.assertEqual(seen, environments)
        for forbidden in ("github.head_ref", "github.event.pull_request.head", "github.event.pull_request.title",
                          "github.event.pull_request.body", "github.event.workflow_run.display_title",
                          "github.event.workflow_run.head_commit", "continue-on-error", "always()", "ext-",
                          "# >>>", "# <<<", "job.workflow_sha"):
            self.assertNotIn(forbidden, self.text)

    def test_the_retry_function_is_the_pages_caller_s(self) -> None:
        pattern = re.compile(r"(?ms)^protected_gh_api_retry\(\) \{.*?^\}\n")
        pages = pattern.findall(step(caller()["jobs"]["verify-kit"]["steps"],
                                     "Bind the executing callee to the protected pin")["run"])
        for job, name in (("locate", LOCATE_STEP), ("members", AUTHORS_STEP), ("members", SNAPSHOT_STEP),
                          ("publish", RECHECK_STEP), ("publish", PUBLISH_STEP)):
            own = pattern.findall(script(job, name))
            self.assertEqual((len(own), own), (1, pages), name)

    def test_the_recheck_reads_the_membership_exactly_as_the_snapshot_did(self) -> None:
        pattern = re.compile(r"(?ms)^# Which of the candidates .*?^org_members_of\(\) \{.*?^\}\n")
        snapshot, recheck = (pattern.findall(script(job, name)) for job, name in
                             (("members", SNAPSHOT_STEP), ("publish", RECHECK_STEP)))
        self.assertEqual(len(snapshot), 1)
        self.assertEqual(snapshot, recheck)
        self.assertNotIn("comments", script("publish", RECHECK_STEP), "the recheck never reads the comments again")

    def test_the_document_bounds_are_the_kit_s(self) -> None:
        authors = script("members", AUTHORS_STEP)
        self.assertIn(f"(( total <= {limits.MAX_CI_ADMISSION_COMMENTS} ))", authors)
        self.assertIn(f"for page in {' '.join(str(page) for page in range(1, limits.MAX_CI_ADMISSION_COMMENTS // 100 + 2))}; do",
                      authors)
        self.assertIn(f"(( ${{#document}} <= {limits.MAX_CI_ORG_MEMBERS_BYTES} ))", script("members", SNAPSHOT_STEP))
        self.assertIn(f"(( ${{#config}} <= {limits.MAX_CI_CONFIG_BYTES} ))", script("locate", LOCATE_STEP))
        callee = (ROOT / workflow.CI_CALLEE_WORKFLOWS["gate-status"]).read_text(encoding="utf-8")
        self.assertIn(f"${{#MEMBERS}} -le {limits.MAX_CI_ORG_MEMBERS_BYTES}", callee)
        body = script("publish", PUBLISH_STEP)
        self.assertIn(f"length <= {limits.MAX_CI_STATUS_DESCRIPTION_CHARS} and", body)
        # A context is the protected string, at most 100 characters, with " (shadow)" in shadow mode.
        self.assertIn(f"length <= {limits.MAX_CI_STATUS_CONTEXT_CHARS + len(' (shadow)')} and", body)
        self.assertIn('--arg runs "https://github.com/$GITHUB_REPOSITORY/actions/runs/"', body)
        self.assertEqual(RUNS, f"https://github.com/{REPOSITORY}/actions/runs/")


class StatusCallerDependabotTests(unittest.TestCase):
    """The Dependabot ignore requirement of the one third-party action a Build/E2E caller pins."""

    def test_a_mod_keeps_dependabot_off_the_app_token_action_while_it_manages_the_caller(self) -> None:
        name = APP_TOKEN.split("@")[0]
        line = f'      - dependency-name: "{name}"\n'
        with tempfile.TemporaryDirectory(prefix="mb-status-dependabot-") as directory:
            repo = real_mod(Path(directory))
            dependabot = repo / ".github/dependabot.yml"
            seeded = dependabot.read_text(encoding="utf-8")
            self.assertEqual(seeded.count(line), 1, "a mod seeded by this kit already ignores the action")
            for state in STATES:
                for path in CALLERS:
                    (repo / path).unlink(missing_ok=True)
                enter(repo, state)
                tool.sync(repo, kit_root=ROOT, write=True)
                managed = STATUS_CALLER in MANAGED[row(state)]
                with self.subTest(state=state):
                    self.assertEqual(tool.check(repo, kit_root=ROOT), [])
                    # A mod that adopted the kit before this caller existed has no such line.
                    dependabot.write_text(seeded.replace(line, ""), encoding="utf-8", newline="\n")
                    drifts = tool.check(repo, kit_root=ROOT)
                    self.assertEqual([(drift.path, drift.kind) for drift in drifts],
                                     [(".github/dependabot.yml", "fragment")] if managed else [])
                    if managed:
                        self.assertIn(f"must ignore {name}, which the managed region of {STATUS_CALLER} pins",
                                      drifts[0].detail)
                dependabot.write_text(seeded, encoding="utf-8", newline="\n")
        self.assertEqual({state for state in STATES if STATUS_CALLER in MANAGED[row(state)]},
                         {state for state in STATES if row(state) not in ("absent", "disabled")},
                         "every active mode manages the status caller")


class StatusCallerJobTests(unittest.TestCase):
    """Which jobs run: nothing but a successful, non-empty evaluation reaches ``publish``."""

    LOCATED = {"guard": {"kit-sha": KIT_SHA}, "locate": {"pr-number": "17", "organization": ""},
               "evaluate": {"intents": "{}"}}
    COUNTING = {**LOCATED, "locate": {"pr-number": "17", "organization": OWNER},
                "members": {"members": "{}", "candidates": "[]", "recheck": "false"}}

    def setUp(self) -> None:
        self.document = document()

    def results(self, name: str = "workflow_run", *, job_outputs: Mapping[str, Mapping[str, str]] | None = None,
                **options: Any) -> dict[str, str]:
        return simulate(self.document, event(name, draft=False),
                        job_outputs=self.LOCATED if job_outputs is None else job_outputs, **options)

    def test_every_event_evaluates_and_publishes_for_its_pull_request(self) -> None:
        for name in STATUS_EVENTS:
            with self.subTest(event=name):
                # Without an organization among the approvers no members job runs, and nothing waits for it.
                self.assertEqual(self.results(name), {"guard": "success", "locate": "success", "members": "skipped",
                                                      "evaluate": "success", "publish": "success"})
                self.assertEqual(self.results(name, job_outputs=self.COUNTING),
                                 dict.fromkeys(("guard", "locate", "members", "evaluate", "publish"), "success"))

    def test_an_event_without_a_pull_request_evaluates_and_publishes_nothing(self) -> None:
        for located in ({"pr-number": "", "organization": ""}, {}):
            results = self.results(job_outputs={**self.LOCATED, "locate": located})
            self.assertEqual(results, {"guard": "success", "locate": "success", "members": "skipped",
                                       "evaluate": "skipped", "publish": "skipped"})

    def test_a_members_job_that_did_not_succeed_evaluates_and_publishes_nothing(self) -> None:
        for outcome in ("failure", "cancelled"):
            with self.subTest(members=outcome):
                results = self.results(job_outputs=self.COUNTING, outcomes={"members": outcome})
                self.assertEqual((results["members"], results["evaluate"], results["publish"]),
                                 (outcome, "skipped", "skipped"))
        # A located pull request whose admission counts members is never evaluated without them,
        # and a failed guard or locate job still stops everything.
        for failed in ("guard", "locate"):
            with self.subTest(failed=failed):
                results = self.results(job_outputs=self.COUNTING, outcomes={failed: "failure"})
                self.assertEqual((results["evaluate"], results["publish"]), ("skipped", "skipped"))
        results = self.results(job_outputs=self.COUNTING, cancelled=True)
        self.assertEqual(set(results.values()) - {"success"}, {"skipped"})
        self.assertEqual((results["members"], results["evaluate"], results["publish"]), ("skipped",) * 3)

    def test_a_failed_skipped_or_empty_evaluation_publishes_nothing(self) -> None:
        cases = {
            "the guard failed": ({"outcomes": {"guard": "failure"}}, "skipped"),
            "the guard was cancelled": ({"outcomes": {"guard": "cancelled"}}, "skipped"),
            "no pull request was located": ({"outcomes": {"locate": "failure"}}, "skipped"),
            "the evaluation failed": ({"outcomes": {"evaluate": "failure"}}, "failure"),
            "the evaluation was cancelled": ({"outcomes": {"evaluate": "cancelled"}}, "cancelled"),
            "the evaluation returned nothing": ({"job_outputs": {**self.LOCATED, "evaluate": {}}}, "success"),
            "the evaluation returned an empty document": (
                {"job_outputs": {**self.LOCATED, "evaluate": {"intents": ""}}}, "success"),
            "the run was cancelled": ({"cancelled": True}, "skipped"),
        }
        for label, (options, evaluated) in cases.items():
            with self.subTest(case=label):
                results = self.results(**options)
                self.assertEqual((results["evaluate"], results["publish"]), (evaluated, "skipped"))

    def test_every_job_after_the_two_first_needs_them(self) -> None:
        jobs = self.document["jobs"]
        self.assertEqual({job_id: needed(job) for job_id, job in jobs.items()},
                         {"guard": [], "locate": [], "members": ["locate"], "evaluate": ["guard", "locate", "members"],
                          "publish": ["locate", "members", "evaluate"]})
        self.assertNotRegex(jobs["members"]["if"], r"always\(\)|failure\(\)|cancelled\(\)|success\(\)",
                            "success() stays implied: the members job never outlives the locate job")
        # The evaluation must also run when the members job was skipped because it had nothing to
        # read, so its condition names every result it needs instead of an implied success().
        for job_id in ("evaluate", "publish"):
            condition = jobs[job_id]["if"]
            self.assertNotRegex(condition, r"always\(\)|failure\(\)|success\(\)")
            self.assertTrue(condition.startswith("${{ !cancelled() && "), condition)
        for name in ("guard", "locate", "members"):
            self.assertIn(f"needs.{name}.result == 'success'", jobs["evaluate"]["if"])
        self.assertIn("needs.evaluate.result == 'success'", jobs["publish"]["if"])


class StatusGuardTests(GuardCase):
    """The guard admits the status caller, on its own events and with its one kit workflow."""

    def test_a_bound_status_run_outputs_the_pin_on_each_of_its_events(self) -> None:
        for name in STATUS_EVENTS:
            with self.subTest(event=name):
                result = self.invoke("status", name)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(outputs(self.output), {"kit-sha": KIT_SHA})
                self.assertEqual([record["route"] for record in self.calls("gh")],
                                 [f"GET repos/{REPOSITORY}", f"GET repos/{REPOSITORY}/actions/runs/{RUN_ID}"])

    def test_the_kit_workflow_of_the_status_caller_may_be_absent_but_never_another(self) -> None:
        guard, status = references(("gate-status",))
        build = {"path": f"{kit_workflow('build')}@{KIT_SHA}", "sha": KIT_SHA}
        for label, listed in {"only the guard": [guard], "both": [guard, status], "reversed": [status, guard]}.items():
            with self.subTest(accepted=label):
                result = self.invoke("status", "workflow_run", run={"referenced_workflows": listed})
                self.assertEqual((result.returncode, outputs(self.output)), (0, {"kit-sha": KIT_SHA}), result.stderr)
        rejected = {
            "a producer's kit workflow": [guard, build],
            "the status workflow at another commit": [guard, {"path": f"{kit_workflow('gate-status')}@{OTHER}",
                                                              "sha": OTHER}],
            "the status workflow by tag": [guard, {"path": f"{kit_workflow('gate-status')}@{VERSION}", "sha": KIT_SHA}],
            "a third party's status workflow": [guard, {
                "path": f"attacker/kit/.github/workflows/gate-status.yml@{KIT_SHA}", "sha": KIT_SHA}],
            "the status workflow twice": [guard, status, status],
            "the guard missing": [status],
        }
        for label, listed in rejected.items():
            with self.subTest(rejected=label):
                result = self.invoke("status", "workflow_run", run={"referenced_workflows": listed})
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(outputs(self.output), {})

    def test_rejected_status_bindings_output_nothing(self) -> None:
        cases: dict[str, dict[str, Any]] = {
            "a push": {"name": "push"},
            "a pull_request event": {"name": "pull_request"},
            "an issue comment": {"name": "issue_comment"},
            "a merge group": {"name": "merge_group"},
            "the callees of the Build caller": {"env": {"CALLEES": "build"}},
            "two callees": {"env": {"CALLEES": "gate-status gate-status"}},
            "the status callee after another": {"env": {"CALLEES": "build gate-status"}},
            "the Build caller's workflow": {"env": {
                "GITHUB_WORKFLOW_REF": f"{REPOSITORY}/{BUILD_CALLER}@refs/heads/{BRANCH}"}},
            "the status caller of another branch": {"env": {
                "GITHUB_WORKFLOW_REF": f"{REPOSITORY}/{STATUS_CALLER}@refs/heads/feature"}},
            "another repository's status caller": {"env": {
                "GITHUB_WORKFLOW_REF": f"attacker/fork/{STATUS_CALLER}@refs/heads/{BRANCH}"}},
            "another branch": {"env": {"GITHUB_REF": "refs/heads/feature"}},
            "a pull request against another base": {"name": "pull_request_target",
                                                    "env": {"GITHUB_BASE_REF": "release"}},
            "a run of the Build caller": {"run": {"path": BUILD_CALLER}},
            "a run of another event": {"run": {"event": "schedule"}},
            # A workflow_run and a schedule execute the default branch: the run must say so itself.
            "a run at another head": {"run": {"head_sha": OTHER}},
            "a run of another branch": {"run": {"head_branch": "feature"}},
            "a run of a fork": {"run": {"head_repository": {"full_name": "attacker/fork"}}},
            "a scheduled run at another head": {"name": "schedule", "run": {"head_sha": OTHER}},
            "a dispatch on another branch": {"name": "workflow_dispatch", "run": {"head_branch": "feature"}},
        }
        for label, case in cases.items():
            with self.subTest(case=label):
                result = self.invoke("status", case.get("name", "workflow_run"), run=case.get("run"),
                                     **case.get("env", {}))
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(outputs(self.output), {})
                self.assertIn("Pinned mod-base: ", result.stderr)
        self.assertEqual(self.invoke("status", "workflow_run").returncode, 0, "the unchanged case is accepted")

    def test_the_events_of_the_status_caller_are_refused_to_the_producers(self) -> None:
        for producer in ("build", "packaged"):
            for name in ("workflow_run", "schedule"):
                with self.subTest(producer=producer, event=name):
                    result = self.invoke(producer, name)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("the Build and packaged E2E callers run only on", result.stderr)
                    self.assertEqual(self.calls("gh") + self.calls("git") + self.calls("python3"), [])
        result = self.invoke("status", "push")
        self.assertIn("the gate status caller runs only on", result.stderr)
        self.assertEqual(self.calls("gh") + self.calls("git") + self.calls("python3"), [])


class ScriptCase(unittest.TestCase):
    """One ``run:`` body of the status caller under the shell harness, with a stub ``gh``."""

    JOB = ""
    STEP = ""
    STUBS: tuple[str, ...] = ("gh", "sleep")

    def setUp(self) -> None:
        require_tools("bash", "jq", "grep")
        temporary = tempfile.TemporaryDirectory(prefix="status caller ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.harness = ShellHarness(self.root / "harness", stubs=self.STUBS)
        self.script = script(self.JOB, self.STEP)
        self.output = self.root / "github_output"
        self.runner_temp = self.root / "runner temp"
        self.runner_temp.mkdir()

    def base(self) -> dict[str, str]:
        return {"GH_TOKEN": TOKEN, "GITHUB_REPOSITORY": REPOSITORY, "GITHUB_REF": f"refs/heads/{BRANCH}",
                "GITHUB_SHA": OTHER, "GITHUB_RUN_ID": RUN_ID, "GITHUB_OUTPUT": str(self.output),
                "RUNNER_TEMP": str(self.runner_temp)}

    def run_script(self, environment: Mapping[str, str | None],
                   fixtures: Mapping[str, Any] | None = None) -> subprocess.CompletedProcess[str]:
        self.output.unlink(missing_ok=True)
        cleaned = {name: value for name, value in environment.items() if value is not None}
        return self.harness.run(self.script, cleaned, fixtures=fixtures)

    def calls(self, tool_name: str = "gh") -> list[dict[str, Any]]:
        return [record for record in self.harness.records() if record["tool"] == tool_name]


#: The protected Build config of the run's commit (``GITHUB_SHA``), read raw by the locate job.
CONFIG = f"GET repos/{REPOSITORY}/contents/scripts/ci/mod-base-build.json?ref={OTHER}"
#: What the locate job reads of it: the approvers of its admission.
USERS = {"admission": {"approvers": [{"login": "AkaNebur", "association": "MEMBER"}]}}
MEMBERS_OF_OWNER = {"admission": {"approvers": [{"login": "AkaNebur", "association": "MEMBER"},
                                                {"organization": OWNER, "role": "member"}]}}


class LocateTests(ScriptCase):
    JOB, STEP = "locate", LOCATE_STEP
    STUBS = ("gh", "sleep", "date")
    BRANCH_PULLS = f"{PULLS}&head={OWNER}:{FEATURE}&per_page=100"
    OPEN_PULLS = f"{PULLS}&sort=updated&direction=desc&per_page=100"
    #: 2026-10-08T00:00:00Z, a whole number of hours since the epoch.
    MIDNIGHT = 1791417600

    def locate(self, name: str, fixtures: Mapping[str, Any] | None = None,
               **environment: str | None) -> subprocess.CompletedProcess[str]:
        values: dict[str, str | None] = {
            **self.base(), "GITHUB_EVENT_NAME": name, "EVENT_PR_NUMBER": "", "DISPATCH_PR_NUMBER": "", "RUN_EVENT": "",
            "RUN_HEAD_BRANCH": "", "RUN_HEAD_SHA": "", "RUN_HEAD_REPOSITORY": "",
            "STUB_DATE_SCRIPT": "print(os.environ['STUB_NOW'])", "STUB_NOW": str(self.MIDNIGHT)}
        if name == "workflow_run":
            values.update(RUN_EVENT="pull_request_target", RUN_HEAD_BRANCH=FEATURE, RUN_HEAD_SHA=HEAD,
                          RUN_HEAD_REPOSITORY=REPOSITORY)
        values.update(environment)
        return self.run_script(values, {CONFIG: {"body": USERS}, **(fixtures or {})})

    def assert_located(self, result: subprocess.CompletedProcess[str], number: str, *, requests: list[str],
                       organization: str = "") -> None:
        """``number`` is located after ``requests``; the protected config is read for it alone."""

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(outputs(self.output), {"pr-number": number, "organization": organization})
        self.assertEqual([record["route"] for record in self.calls()], [*requests, *([CONFIG] if number else [])])
        self.assertTrue(all(record["token"] == TOKEN for record in self.calls()))
        self.assertEqual({tuple(record["headers"]) for record in self.calls() if record["route"] == CONFIG},
                         {("Accept: application/vnd.github.raw+json",)} if number else set())
        self.assertIn(f"pull request #{number}" if number else "names no open pull request", result.stdout)

    def assert_refused(self, result: subprocess.CompletedProcess[str], message: str = "") -> None:
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Locate pull request: " + message, result.stderr)
        self.assertEqual(outputs(self.output), {})

    def test_the_members_job_is_asked_for_exactly_when_the_protected_admission_counts_the_owner_s_members(
            self) -> None:
        for label, config in {"users alone": USERS, "no admission": {}, "an empty list": {"admission": {}},
                              "null approvers": {"admission": {"approvers": None}}}.items():
            with self.subTest(config=label):
                self.assert_located(self.locate("pull_request_target", {CONFIG: {"body": config}},
                                                EVENT_PR_NUMBER="17"), "17", requests=[])
        result = self.locate("pull_request_target", {CONFIG: {"body": MEMBERS_OF_OWNER}}, EVENT_PR_NUMBER="17")
        self.assert_located(result, "17", requests=[], organization=OWNER)
        self.assertIn(f"Its admission counts the members of {OWNER}", result.stdout)
        result = self.locate("workflow_run", {self.BRANCH_PULLS: {"body": [pull()]},
                                              CONFIG: {"body": MEMBERS_OF_OWNER}})
        self.assert_located(result, "17", requests=[self.BRANCH_PULLS], organization=OWNER)
        # No pull request, no config read and no members job.
        self.assert_located(self.locate("workflow_run", {self.BRANCH_PULLS: {"body": []},
                                                         CONFIG: {"body": MEMBERS_OF_OWNER}}),
                            "", requests=[self.BRANCH_PULLS])

    def test_a_config_that_cannot_say_whom_the_admission_counts_is_refused(self) -> None:
        other = {"admission": {"approvers": [{"organization": "another-org", "role": "member"}]}}
        twice = {"admission": {"approvers": [{"organization": OWNER, "role": "member"},
                                             {"organization": OWNER, "role": "member"}]}}
        cases = {
            "another organization": ({"body": other}, "the protected Build config names an organization that does "
                                                      "not own this repository"),
            "the owner in another case": ({"body": {"admission": {"approvers": [
                {"organization": OWNER.lower(), "role": "member"}]}}}, "the protected Build config names an organization"),
            "two organizations": ({"body": twice}, "the protected Build config is malformed"),
            "a number": ({"body": {"admission": {"approvers": [{"organization": 7}]}}},
                         "the protected Build config is malformed"),
            "no object": ({"body": [MEMBERS_OF_OWNER]}, "the protected Build config is malformed"),
            "approvers that are no list": ({"body": {"admission": {"approvers": {"organization": OWNER}}}},
                                           "the protected Build config is malformed"),
            "no JSON": ({"body": "{"}, "the protected Build config is malformed"),
            "a config too large": ({"body": " " * 1048577}, "the protected Build config is too large"),
            "no config": ({"fail": "gh: Not Found (HTTP 404)"}, "cannot read the protected Build config"),
        }
        for label, (response, message) in cases.items():
            with self.subTest(case=label):
                self.assert_refused(self.locate("pull_request_target", {CONFIG: response}, EVENT_PR_NUMBER="17"),
                                    message)
        for label, environment in {"a malformed commit": {"GITHUB_SHA": "HEAD"},
                                   "a malformed repository": {"GITHUB_REPOSITORY": "The-Plum-Team"}}.items():
            with self.subTest(case=label):
                self.assert_refused(self.locate("pull_request_target", EVENT_PR_NUMBER="17", **environment))
                self.assertEqual(self.calls(), [])

    def test_a_pull_request_event_and_a_dispatch_name_their_pull_request_without_a_request(self) -> None:
        self.assert_located(self.locate("pull_request_target", EVENT_PR_NUMBER="17"), "17", requests=[])
        self.assert_located(self.locate("workflow_dispatch", DISPATCH_PR_NUMBER="23"), "23", requests=[])
        # Each event reads its own value alone.
        self.assert_located(self.locate("pull_request_target", EVENT_PR_NUMBER="17", DISPATCH_PR_NUMBER="23"), "17",
                            requests=[])
        self.assert_located(self.locate("workflow_dispatch", EVENT_PR_NUMBER="17", DISPATCH_PR_NUMBER="23"), "23",
                            requests=[])
        for name, variable in (("pull_request_target", "EVENT_PR_NUMBER"), ("workflow_dispatch", "DISPATCH_PR_NUMBER")):
            for number in ("", "0", "017", "17 ", "17\n18", "17; id", "-1", "1" * 19, "#17", "seventeen"):
                with self.subTest(event=name, number=number):
                    self.assert_refused(self.locate(name, **{variable: number}), "malformed pull request number")
                    self.assertEqual(self.calls(), [])

    def test_a_gate_run_names_the_open_pull_request_with_its_branch_and_head(self) -> None:
        result = self.locate("workflow_run", {self.BRANCH_PULLS: {"body": [pull()]}})
        self.assert_located(result, "17", requests=[self.BRANCH_PULLS])
        self.assertEqual(self.BRANCH_PULLS, f"GET repos/{REPOSITORY}/pulls?state=open&base={BRANCH}"
                                            f"&head=The-Plum-Team:{FEATURE}&per_page=100")
        # Others of the listing are not that pull request: only the exact branch, head, base and
        # repository are, and only while the pull request is open.
        others = [pull(18, head={"sha": OTHER}), pull(19, head={"ref": "feature/gates-2"}),
                  pull(20, base={"ref": "release"}), pull(21, head={"repo": {"full_name": "contributor/fork"}}),
                  pull(22, base={"repo": {"full_name": "attacker/fork"}}), pull(23, state="closed"),
                  pull(24, head={"repo": None})]
        result = self.locate("workflow_run", {self.BRANCH_PULLS: {"body": [*others, pull(), *others]}})
        self.assert_located(result, "17", requests=[self.BRANCH_PULLS])
        # A draft is located like any other: its evaluation publishes the pending statuses.
        result = self.locate("workflow_run", {self.BRANCH_PULLS: {"body": [pull(draft=True)]}})
        self.assert_located(result, "17", requests=[self.BRANCH_PULLS])

    def test_a_gate_run_whose_head_is_gone_or_that_tested_no_pull_request_names_none(self) -> None:
        for label, listed in {"no pull request": [], "a newer head": [pull(head={"sha": OTHER})],
                              "closed": [pull(state="closed")],
                              "another base": [pull(base={"ref": "release"})]}.items():
            with self.subTest(case=label):
                self.assert_located(self.locate("workflow_run", {self.BRANCH_PULLS: {"body": listed}}), "",
                                    requests=[self.BRANCH_PULLS])
        # A push or a dispatch of a gate caller, and a fork's run: nothing is even listed.
        for label, environment in {"a push": {"RUN_EVENT": "push", "RUN_HEAD_BRANCH": BRANCH},
                                   "a dispatch": {"RUN_EVENT": "workflow_dispatch", "RUN_HEAD_BRANCH": BRANCH},
                                   "a fork": {"RUN_HEAD_REPOSITORY": "contributor/Quick-Skin-Mod"},
                                   "no repository": {"RUN_HEAD_REPOSITORY": ""},
                                   "a pull_request run of the same name": {"RUN_EVENT": "pull_request"}}.items():
            with self.subTest(case=label):
                self.assert_located(self.locate("workflow_run", **environment), "", requests=[])

    def test_an_ambiguous_malformed_or_unreadable_gate_run_is_refused(self) -> None:
        listing = {self.BRANCH_PULLS: {"body": [pull(), pull(18)]}}
        self.assert_refused(self.locate("workflow_run", listing), "the triggering run does not name at most one")
        for label, body in {"an object": {"message": "Not Found"}, "no JSON": "<html>", "a number entry": [17],
                            "a number that is text": [pull("17")]}.items():  # type: ignore[arg-type]
            with self.subTest(listing=label):
                self.assert_refused(self.locate("workflow_run", {self.BRANCH_PULLS: {"body": body}}))
        for label, environment in {
                "a head that is no commit": {"RUN_HEAD_SHA": "HEAD"}, "an upper-case head": {"RUN_HEAD_SHA": "A" * 40},
                "no head": {"RUN_HEAD_SHA": ""}, "no branch": {"RUN_HEAD_BRANCH": ""},
                "a branch with a query": {"RUN_HEAD_BRANCH": "x&base=release"},
                "a branch with a space": {"RUN_HEAD_BRANCH": "feature gates"},
                "a branch with a line break": {"RUN_HEAD_BRANCH": "feature\ngates"},
                "a branch with a colon": {"RUN_HEAD_BRANCH": "attacker:feature"},
                "a tag ref": {"GITHUB_REF": "refs/tags/v1"}, "no ref": {"GITHUB_REF": ""},
                "a malformed repository": {"GITHUB_REPOSITORY": "The-Plum-Team",
                                           "RUN_HEAD_REPOSITORY": "The-Plum-Team"},
        }.items():
            with self.subTest(case=label):
                self.assert_refused(self.locate("workflow_run", {self.BRANCH_PULLS: {"body": [pull()]}}, **environment))
                self.assertEqual(self.calls(), [], "nothing is requested with a value that was not admitted")
        self.assert_refused(self.locate("workflow_run", {self.BRANCH_PULLS: {"fail": "gh: Forbidden (HTTP 403)"}}),
                            "cannot list the pull requests")
        self.assertEqual(len(self.calls()), 1, "a refusal is not retried")
        for name in ("push", "pull_request", "issue_comment", ""):
            with self.subTest(event=name):
                self.assert_refused(self.locate(name, EVENT_PR_NUMBER="17", DISPATCH_PR_NUMBER="17"),
                                    "the gate status caller does not run on this event")

    def test_a_transient_failure_is_retried_within_the_bounded_budget(self) -> None:
        transient = {"fail": "gh: Server Error (HTTP 502)"}
        result = self.locate("workflow_run", {self.BRANCH_PULLS: [transient, transient, {"body": [pull()]}]})
        self.assert_located(result, "17", requests=[self.BRANCH_PULLS] * 3)
        self.assertEqual([record["argv"] for record in self.calls("sleep")], [["5"], ["10"]])
        self.assert_refused(self.locate("workflow_run", {self.BRANCH_PULLS: [transient] * 9}))
        self.assertEqual(len(self.calls()), 4)
        result = self.locate("pull_request_target", {CONFIG: [transient, {"body": MEMBERS_OF_OWNER}]},
                             EVENT_PR_NUMBER="17")
        self.assert_located(result, "17", requests=[CONFIG], organization=OWNER)

    def test_the_hourly_run_takes_one_open_ready_pull_request_in_turn(self) -> None:
        listed = [pull(31), pull(12), pull(25, draft=True), pull(19, head={"repo": {"full_name": "contributor/fork"}}),
                  pull(7), pull(40, base={"ref": "release"}), pull(44, state="closed")]
        ready = [7, 12, 31]
        seen = []
        for hour in range(6):
            result = self.locate("schedule", {self.OPEN_PULLS: {"body": listed}},
                                 STUB_NOW=str(self.MIDNIGHT + hour * 3600 + 1020))
            expected = ready[(self.MIDNIGHT // 3600 + hour) % len(ready)]
            with self.subTest(hour=hour):
                self.assert_located(result, str(expected), requests=[self.OPEN_PULLS])
            seen.append(expected)
        self.assertEqual(sorted(set(seen[:3])), ready, "three hours, three pull requests")
        self.assertEqual(seen[:3], seen[3:])
        self.assertEqual(self.OPEN_PULLS, f"GET repos/{REPOSITORY}/pulls?state=open&base={BRANCH}"
                                          "&sort=updated&direction=desc&per_page=100")
        self.assertEqual([record["argv"] for record in self.calls("date")], [["-u", "+%s"]])
        # Within one hour every run takes the same pull request.
        for second in (0, 1, 3599):
            result = self.locate("schedule", {self.OPEN_PULLS: {"body": listed}}, STUB_NOW=str(self.MIDNIGHT + second))
            self.assert_located(result, str(ready[self.MIDNIGHT // 3600 % 3]), requests=[self.OPEN_PULLS])

    def test_the_hourly_run_is_bounded_to_the_ten_most_recently_updated(self) -> None:
        self.assertIn("\nbound=10\n", self.script)
        # The listing is in the order of the last update: the eleventh and later are left to their own events.
        listed = [pull(number) for number in (50, 3, 48, 9, 46, 11, 44, 13, 42, 15, 2, 1, 60)]
        taken = sorted(entry["number"] for entry in listed[:10])
        seen = set()
        for hour in range(10):
            result = self.locate("schedule", {self.OPEN_PULLS: {"body": listed}},
                                 STUB_NOW=str(self.MIDNIGHT + hour * 3600))
            self.assertEqual(result.returncode, 0, result.stderr)
            seen.add(int(outputs(self.output)["pr-number"]))
        self.assertEqual(sorted(seen), taken)
        # Drafts and foreign heads are left out before the bound is applied.
        drafts = [pull(number, draft=True) for number in range(100, 109)]
        result = self.locate("schedule", {self.OPEN_PULLS: {"body": [*drafts, pull(5)]}})
        self.assert_located(result, "5", requests=[self.OPEN_PULLS])

    def test_the_hourly_run_without_an_open_ready_pull_request_names_none(self) -> None:
        for label, listed in {"nothing open": [], "drafts alone": [pull(draft=True), pull(18, draft=True)],
                              "a fork alone": [pull(head={"repo": {"full_name": "contributor/fork"}})]}.items():
            with self.subTest(case=label):
                self.assert_located(self.locate("schedule", {self.OPEN_PULLS: {"body": listed}}), "",
                                    requests=[self.OPEN_PULLS])
        self.assert_refused(self.locate("schedule", {self.OPEN_PULLS: {"fail": "gh: Not Found (HTTP 404)"}}),
                            "cannot list the open pull requests")
        self.assert_refused(self.locate("schedule", {self.OPEN_PULLS: {"body": {"message": "x"}}}),
                            "cannot choose among the open pull requests")
        for now in ("", "0", "now", "-5", "1791417600.5"):
            with self.subTest(clock=now):
                self.assert_refused(self.locate("schedule", {self.OPEN_PULLS: {"body": [pull()]}}, STUB_NOW=now),
                                    "cannot read the clock")
                self.assertEqual(self.calls(), [])


class PublishTests(ScriptCase):
    JOB, STEP = "publish", PUBLISH_STEP
    LIVE = f"GET repos/{REPOSITORY}/pulls/17"
    SHOWN = f"GET repos/{REPOSITORY}/commits/{HEAD}/statuses?per_page=100"
    STATUSES = f"POST repos/{REPOSITORY}/statuses/{HEAD}"

    def publish(self, evaluated: Any = None, *, live: Any = None, fixtures: Mapping[str, Any] | None = None,
                **environment: str | None) -> subprocess.CompletedProcess[str]:
        raw = evaluated if isinstance(evaluated, str) else json.dumps(intents() if evaluated is None else evaluated,
                                                                      separators=(",", ":"), sort_keys=True)
        values: dict[str, str | None] = {**self.base(), "GITHUB_EVENT_NAME": "workflow_run",
                                         "STATUS_TOKEN": APP_TOKEN_VALUE, "APP_SLUG": APP_SLUG,
                                         "EVALUATION": "success", "INTENTS": raw, "PR_NUMBER": "17", **environment}
        routes = {self.LIVE: {"body": pull() if live is None else live}, self.SHOWN: {"body": []},
                  self.STATUSES: {"body": {"id": 1}}}
        return self.run_script(values, {**routes, **(fixtures or {})})

    def posted(self) -> list[dict[str, Any]]:
        return [json.loads(record["input"]) for record in self.calls() if record["route"].startswith("POST ")]

    def assert_published(self, result: subprocess.CompletedProcess[str], expected: list[dict[str, Any]], *,
                         unchanged: int = 0) -> None:
        self.assertEqual(result.returncode, 0, result.stderr)
        records = self.calls()
        self.assertEqual([record["route"] for record in records],
                         [self.LIVE, self.SHOWN, *[self.STATUSES] * len(expected)])
        # The live pull request is read with the job's read-only token; the App token reads what
        # the head already shows, and nothing but the App token writes.
        self.assertEqual([record["token"] for record in records], [TOKEN, *[APP_TOKEN_VALUE] * (1 + len(expected))])
        self.assertEqual(self.posted(), expected)
        self.assertEqual(result.stdout, f"Published {len(expected)} gate status(es) on {HEAD} of pull request #17; "
                                        f"{unchanged} unchanged\n")
        self.assertNotIn(APP_TOKEN_VALUE, result.stdout + result.stderr)

    @staticmethod
    def shown(status: Mapping[str, Any], writer: str = WRITER, **changes: Any) -> dict[str, Any]:
        """A status as the head lists it: what was posted, who wrote it, and what GitHub adds."""

        return {"id": 7, "url": "https://api.github.com/x", "context": status["context"], "state": status["state"],
                "description": status["description"], "target_url": status.get("target_url"),
                "creator": {"login": writer, "type": "Bot"}, "created_at": "2026-10-07T10:00:00Z", **changes}

    def assert_nothing(self, result: subprocess.CompletedProcess[str], message: str, *, requests: int = 0) -> None:
        """A failure of the job: nothing is written and at most the live pull request was read."""

        self.assertNotEqual(result.returncode, 0, message)
        self.assertIn("Publish gate statuses: " + message, result.stderr)
        self.assertEqual(self.posted(), [])
        self.assertEqual([record["route"] for record in self.calls()], [self.LIVE] * requests)

    def assert_superseded(self, result: subprocess.CompletedProcess[str], message: str) -> None:
        """An answer the pull request has outrun: dropped without an error and without a write."""

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"::notice title=Gate statuses not published::{message}\n")
        self.assertEqual([record["route"] for record in self.calls()], [self.LIVE])

    def status(self, gate: str, state: str = "success", run_id: int | None = 42) -> dict[str, Any]:
        expected = {"state": state, "context": CONTEXTS[gate], "description": f"the newest {gate} run: {state}"}
        return expected if run_id is None else {**expected, "target_url": f"{RUNS}{run_id}"}

    def test_both_green_gates_are_published_exactly_as_evaluated(self) -> None:
        self.assert_published(self.publish(), [self.status("build"), self.status("packaged", run_id=43)])
        for payload in self.posted():
            self.assertEqual(list(payload), ["state", "context", "description", "target_url"])
        self.assertEqual(sorted((self.runner_temp).iterdir()), [self.runner_temp / "mod-base-gate-status.json"])

    def test_a_state_that_is_no_success_is_written_first(self) -> None:
        cases = {
            ("success", "pending"): ["packaged", "build"], ("pending", "success"): ["build", "packaged"],
            ("success", "failure"): ["packaged", "build"], ("failure", "success"): ["build", "packaged"],
            ("pending", "pending"): ["build", "packaged"], ("failure", "pending"): ["build", "packaged"],
            ("failure", "failure"): ["build", "packaged"], ("pending", "failure"): ["build", "packaged"],
        }
        for (build, packaged), order in cases.items():
            states = {"build": build, "packaged": packaged}
            evaluated = intents(intent("build", build), intent("packaged", packaged, run_id=43))
            with self.subTest(build=build, packaged=packaged):
                self.assert_published(self.publish(evaluated), [
                    self.status(gate, states[gate], 42 if gate == "build" else 43) for gate in order])

    def test_a_status_no_run_decides_has_no_target_and_one_gate_is_one_status(self) -> None:
        evaluated = intents(intent("build", "pending", None), intent("packaged", "pending", None))
        self.assert_published(self.publish(evaluated), [self.status("build", "pending", None),
                                                        self.status("packaged", "pending", None)])
        self.assertTrue(all("target_url" not in payload for payload in self.posted()))
        # A mod that keeps its own packaged gate (shared-build) gets the Build status alone.
        alone = intents()
        del alone["gates"]["packaged"]
        self.assert_published(self.publish(alone), [self.status("build")])
        shadow = intents(intent("build", context=CONTEXTS["build"] + " (shadow)"),
                         intent("packaged", run_id=43, context=CONTEXTS["packaged"] + " (shadow)"))
        self.assert_published(self.publish(shadow), [
            {**self.status("build"), "context": "Trusted PR / Build and verify (shadow)"},
            {**self.status("packaged", run_id=43), "context": "Trusted PR / Packaged E2E gate (shadow)"}])
        bare = intents(intent("build", context="Build"), intent("packaged", run_id=43, context="Packaged E2E"))
        self.assertEqual(self.publish(bare).returncode, 0)
        self.assertEqual([payload["context"] for payload in self.posted()], ["Build", "Packaged E2E"])

    def test_a_description_is_published_byte_for_byte(self) -> None:
        for description in ("x", "y" * 140,
                            "the newest Build run was rejected: $.run: \"quoted\" 'single' \\n $(id) `id`",
                            "waiting: no Build run exists for this head yet", "Größe geändert: naïve café ✓",
                            "-f state=success", "@/etc/passwd", "a  b"):
            evaluated = intents(intent("build", "failure", description=description))
            with self.subTest(description=description):
                result = self.publish(evaluated)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.posted()[0]["description"], description)
                self.assertEqual(self.posted()[0]["state"], "failure")

    def test_an_evaluation_that_did_not_succeed_publishes_nothing(self) -> None:
        for evaluation in ("failure", "skipped", "cancelled", "", "Success", "success "):
            with self.subTest(evaluation=evaluation):
                self.assert_nothing(self.publish(EVALUATION=evaluation), "the evaluation did not succeed")
        for label, raw in {"nothing": "", "one byte": "{", "an oversize document": json.dumps(
                intents(intent("build", description="x" * 140), padding="y" * 4096))}.items():
            with self.subTest(document=label):
                self.assert_nothing(self.publish(raw), "the evaluation returned no bounded intents document")

    def test_missing_credentials_publish_nothing(self) -> None:
        for label, token in {"no token": "", "a blank token": "   ", "a token with a line break": "ghs_a\nb",
                             "a token with a space": "ghs_a b"}.items():
            with self.subTest(case=label):
                self.assert_nothing(self.publish(STATUS_TOKEN=token), "no App token was minted")
        # The job's own token is never the writer, and the live read needs it.
        self.assert_nothing(self.publish(STATUS_TOKEN=TOKEN), "the job token and the App token must be two tokens")
        self.assert_nothing(self.publish(GH_TOKEN=""), "the job token and the App token must be two tokens")

    def test_a_head_that_moved_between_evaluation_and_publication_publishes_nothing(self) -> None:
        self.assert_superseded(self.publish(live=pull(head={"sha": OTHER})),
                               "the pull request has a newer head than the evaluated one")
        # The evaluated commit is the target, never the live head or anything the pull request says.
        evaluated = intents(target_sha=OTHER)
        self.assert_superseded(self.publish(evaluated), "the pull request has a newer head than the evaluated one")
        result = self.publish(evaluated, live=pull(head={"sha": OTHER}),
                              fixtures={self.SHOWN.replace(HEAD, OTHER): {"body": []},
                                        f"POST repos/{REPOSITORY}/statuses/{OTHER}": {"body": {}}})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([record["route"] for record in self.calls()][1:],
                         [self.SHOWN.replace(HEAD, OTHER), *[f"POST repos/{REPOSITORY}/statuses/{OTHER}"] * 2])

    def test_a_draft_takes_pending_statuses_alone(self) -> None:
        draft = pull(draft=True)
        pending = intents(intent("build", "pending", None), intent("packaged", "pending", None))
        self.assert_published(self.publish(pending, live=draft), [self.status("build", "pending", None),
                                                                 self.status("packaged", "pending", None)])
        message = "the pull request became a draft after its evaluation"
        self.assert_superseded(self.publish(live=draft), message)
        for build, packaged in (("success", "pending"), ("pending", "success"), ("failure", "pending"),
                                ("pending", "failure"), ("failure", "failure")):
            evaluated = intents(intent("build", build), intent("packaged", packaged, run_id=43))
            with self.subTest(build=build, packaged=packaged):
                self.assert_superseded(self.publish(evaluated, live=draft), message)
        # A pull request that is ready again takes the pending statuses of its draft evaluation too.
        self.assertEqual(self.publish(pending).returncode, 0)
        self.assertEqual(len(self.posted()), 2)

    def test_a_pull_request_that_left_its_generation_publishes_nothing(self) -> None:
        cases = {
            "closed": (pull(state="closed"), "the pull request is not open any more"),
            "merged": (pull(state="closed", merged=True), "the pull request is not open any more"),
            "another base": (pull(base={"ref": "release"}),
                             "the pull request left the default branch of this repository"),
            "a base of another repository": (pull(base={"repo": {"full_name": "attacker/fork"}}),
                                             "the pull request left the default branch of this repository"),
            "a head of a fork": (pull(head={"repo": {"full_name": "contributor/fork"}}),
                                 "the pull request left the default branch of this repository"),
            "a head without a repository": (pull(head={"repo": None}),
                                            "the pull request left the default branch of this repository"),
        }
        for label, (live, message) in cases.items():
            with self.subTest(case=label):
                self.assert_superseded(self.publish(live=live), message)
        # The default branch is the branch this run executes: the guard bound it.
        self.assert_superseded(self.publish(GITHUB_REF="refs/heads/release"),
                               "the pull request left the default branch of this repository")
        self.assert_nothing(self.publish(GITHUB_REF="refs/tags/v1"), "this run is not on a branch")

    def test_a_live_pull_request_that_cannot_be_read_publishes_nothing(self) -> None:
        for label, live in {"another number": pull(18), "a number that is text": pull("17"),  # type: ignore[arg-type]
                            "a list": [pull()], "no draft state": pull(draft=None), "no head": pull(head=None),
                            "no base": pull(base="master")}.items():
            with self.subTest(live=label):
                self.assert_nothing(self.publish(live=live), "the live pull request is malformed", requests=1)
        result = self.publish(fixtures={self.LIVE: {"fail": "gh: Not Found (HTTP 404)"}})
        self.assert_nothing(result, "cannot read the live pull request", requests=1)
        result = self.publish(fixtures={self.LIVE: {"body": "not json"}})
        self.assert_nothing(result, "cannot read the live pull request", requests=1)
        for number in ("", "0", "017", "17 18", "17\n", "1" * 19):
            with self.subTest(number=number):
                self.assert_nothing(self.publish(PR_NUMBER=number), "malformed pull request number")
        self.assert_nothing(self.publish(GITHUB_REPOSITORY="The-Plum-Team"), "malformed repository")

    def test_a_malformed_intents_document_publishes_nothing(self) -> None:
        def gate(**changes: Any) -> dict[str, Any]:
            return intents(intent("build", **changes))

        def without(key: str) -> dict[str, Any]:
            evaluated = intents()
            del evaluated["gates"]["build"][key]
            return evaluated

        url = RUNS + "42"
        cases: dict[str, Any] = {
            # The shape of the document.
            "no JSON": "{not json}", "a list": [intents()], "a string": json.dumps("x"), "null": "null",
            "an extra key": intents(extra=1), "a signature": intents(approved_by="owner"),
            "no target": {key: value for key, value in intents().items() if key != "target_sha"},
            "no gates": {key: value for key, value in intents().items() if key != "gates"},
            "gates as a list": intents(gates=[intent("build")]), "no gate": intents(gates={}),
            # The pull request, the repository and the commit the evaluation names.
            "another repository": intents(repository="attacker/Quick-Skin-Mod"),
            "the repository in another case": intents(repository=REPOSITORY.lower()),
            "another pull request": intents(pr_number=18), "a number as text": intents(pr_number="17"),
            "a short commit": intents(target_sha=HEAD[:39]), "a long commit": intents(target_sha=HEAD + "a"),
            "an upper-case commit": intents(target_sha="A" * 40),
            "a branch as the target": intents(target_sha="master"),
            "a commit with a line break": intents(target_sha=HEAD[:39] + "\n"),
            "a commit that is no text": intents(target_sha=12345),
            # The two allowed gates.
            "an extra gate": {**intents(), "gates": {**intents()["gates"],
                                                    "deploy": intent("build", context="Deploy")}},
            "the packaged gate alone": intents(gates={"packaged": intent("packaged")}),
            "another gate name": intents(gates={"Build": intent("build")}),
            "a gate that is no object": intents(gates={"build": "success"}),
            "a gate with an extra key": gate(sha=HEAD), "a gate without a state": without("state"),
            "a gate without a context": without("context"), "a gate without a description": without("description"),
            "a gate without a target": without("target_url"),
            "the same context twice": intents(intent("build"), intent("packaged", context=CONTEXTS["build"])),
            "the same context in another case": intents(intent("build"),
                                                        intent("packaged", context=CONTEXTS["build"].upper())),
            # The context: the trimmed printable ASCII of the protected Build config.
            "an empty context": gate(context=""), "a context with a lookalike": gate(context="Trusted PR / Вuild"),
            "a context with an accent": gate(context="Trusted PR / Buíld"),
            "a context with a line break": gate(context="Trusted PR\nBuild"),
            "a context with a tab": gate(context="Trusted PR\tBuild"),
            "a context with a NUL": gate(context="Build\x00"),
            "a context with a DEL": gate(context="Build\x7f"), "a context with a leading space": gate(context=" Build"),
            "a context with a trailing space": gate(context="Build "), "an oversize context": gate(context="B" * 110),
            "a context that is no text": gate(context=7),
            "a context with a no-break space": gate(context="Trusted\xa0PR"),
            # The state.
            "the state error": gate(state="error"), "an upper-case state": gate(state="SUCCESS"),
            "a padded state": gate(state="success "), "a boolean state": gate(state=True), "no state": gate(state=None),
            # The description: one bounded line.
            "an empty description": gate(description=""), "an oversize description": gate(description="x" * 141),
            "a description of two lines": gate(description="verified\n::error::x"),
            "a description with a carriage return": gate(description="verified\rx"),
            "a description with an escape": gate(description="verified\x1b[31m"),
            "a description with a C1 control": gate(description="verified\x85"),
            "a description with a line separator": gate(description="verified x"),
            "a description that is no text": gate(description=["verified"]),
            # The target: a run of this repository, or none.
            "a foreign URL": gate(target_url="https://example.com/"),
            "a run of another repository": gate(
                target_url="https://github.com/attacker/Quick-Skin-Mod/actions/runs/42"),
            "a run of a repository with this prefix": gate(
                target_url=f"https://github.com/{REPOSITORY}-x/actions/runs/42"),
            "another host": gate(target_url=url.replace("github.com", "github.com.evil.example")),
            "plain http": gate(target_url=url.replace("https://", "http://")),
            "the pull request page": gate(target_url=f"https://github.com/{REPOSITORY}/pull/17"),
            "the runs page": gate(target_url=RUNS), "run zero": gate(target_url=RUNS + "0"),
            "a run with a leading zero": gate(target_url=RUNS + "042"),
            "a run with a path": gate(target_url=url + "/job/1"),
            "a run with a query": gate(target_url=url + "?x=1"), "a run with a fragment": gate(target_url=url + "#x"),
            "a run with a line break": gate(target_url=url + "\n"),
            "a run that is no number": gate(target_url=RUNS + "4a"),
            "an oversize run": gate(target_url=RUNS + "1" * 20), "a URL that is no text": gate(target_url=42),
            "a javascript URL": gate(target_url="javascript:alert(1)"),
        }
        for label, evaluated in cases.items():
            with self.subTest(case=label):
                self.assert_nothing(self.publish(evaluated), "the intents document is malformed")
        self.assertEqual(self.publish().returncode, 0, "the unchanged document is admitted")

    def test_a_status_the_app_already_shows_unchanged_is_not_written_again(self) -> None:
        build, packaged = self.status("build"), self.status("packaged", run_id=43)
        # GitHub keeps at most 1000 statuses of one context on one commit, and the caller runs for
        # every event and every hour: an unchanged answer costs no status.
        result = self.publish(fixtures={self.SHOWN: {"body": [self.shown(packaged), self.shown(build)]}})
        self.assert_published(result, [], unchanged=2)
        # Only the newest status of a context counts, and only as far as it differs.
        older = self.shown(build, id=3)
        cases: dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]] = {
            "nothing is shown": ([], [build, packaged]),
            "one context is shown": ([self.shown(build)], [packaged]),
            "another state": ([self.shown(build, state="pending"), self.shown(packaged)], [build]),
            "another description": ([self.shown(build), self.shown(packaged, description="older")], [packaged]),
            "another target": ([self.shown(build, target_url=RUNS + "41"), self.shown(packaged)], [build]),
            "no target": ([self.shown(build, target_url=None), self.shown(packaged)], [build]),
            "an older equal one under a newer other": (
                [self.shown(build, state="failure"), older, self.shown(packaged)], [build]),
            # A status of the same name that somebody else wrote is not this App's answer: a
            # ruleset that names the App as the source of the context would not count it.
            "another writer": ([self.shown(build, "github-actions[bot]"), self.shown(packaged)], [build]),
            "a user as the writer": ([self.shown(build, APP_SLUG), self.shown(packaged, "octocat")], [build, packaged]),
            "no writer": ([self.shown(build, creator=None), self.shown(packaged)], [build]),
            "a context in another case": ([self.shown(build, context=build["context"].upper()),
                                           self.shown(packaged)], [build]),
            "entries that are no statuses": ([None, "x", 7, self.shown(build), self.shown(packaged)], []),
        }
        for label, (listed, expected) in cases.items():
            with self.subTest(case=label):
                self.assert_published(self.publish(fixtures={self.SHOWN: {"body": listed}}), expected,
                                      unchanged=2 - len(expected))
        # A status without a target is unchanged only while the shown one has none either.
        pending = intents(intent("build", "pending", None), intent("packaged", "pending", None))
        first, second = self.status("build", "pending", None), self.status("packaged", "pending", None)
        result = self.publish(pending, fixtures={self.SHOWN: {"body": [self.shown(first), self.shown(second)]}})
        self.assert_published(result, [], unchanged=2)
        result = self.publish(pending, fixtures={self.SHOWN: {"body": [
            self.shown(first, target_url=RUNS + "42"), self.shown(second)]}})
        self.assert_published(result, [first], unchanged=1)

    def test_a_doubt_about_what_the_head_shows_writes_or_fails_but_never_skips(self) -> None:
        build, packaged = self.status("build"), self.status("packaged", run_id=43)
        listed = [self.shown(build), self.shown(packaged)]
        # Without the name the App writes under nothing can be recognised as its own.
        for slug in ("", "Quick Skin Gate", "quick-skin-gate[bot]", "-gate", "x" * 100, "gate\n"):
            with self.subTest(slug=slug):
                self.assert_published(self.publish(APP_SLUG=slug, fixtures={self.SHOWN: {"body": listed}}),
                                      [build, packaged])
        for label, response in {"a refusal": {"fail": "gh: Resource not accessible by integration (HTTP 403)"},
                                "an object": {"body": {"message": "x"}}, "no JSON": {"body": "<html>"}}.items():
            with self.subTest(listing=label):
                result = self.publish(fixtures={self.SHOWN: response})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Publish gate statuses: cannot read the statuses of the evaluated head", result.stderr)
                self.assertEqual(self.posted(), [])

    def test_a_refused_status_ends_the_job_before_the_next_one(self) -> None:
        # The first status is the restrictive one: a failure after it leaves the success unwritten.
        evaluated = intents(intent("build"), intent("packaged", "failure", run_id=43))
        refusal = {"fail": "gh: Resource not accessible by integration (HTTP 403)"}
        result = self.publish(evaluated, fixtures={self.STATUSES: [{"body": {}}, refusal]})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("GitHub refused a status; no further status is published", result.stderr)
        self.assertEqual([payload["state"] for payload in self.posted()], ["failure", "success"])
        result = self.publish(evaluated, fixtures={self.STATUSES: refusal})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([payload["state"] for payload in self.posted()], ["failure"], "the success was never sent")
        self.assertNotIn("Published", result.stdout)

    def test_a_transient_failure_is_retried_within_the_bounded_budget(self) -> None:
        transient = {"fail": "gh: Server Error (HTTP 502)"}
        result = self.publish(fixtures={self.LIVE: [transient, {"body": pull()}],
                                        self.STATUSES: [transient, {"body": {}}, {"body": {}}]})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([record["route"] for record in self.calls()],
                         [self.LIVE, self.LIVE, self.SHOWN, self.STATUSES, self.STATUSES, self.STATUSES])
        self.assertEqual([payload["context"] for payload in self.posted()],
                         [CONTEXTS["build"], CONTEXTS["build"], CONTEXTS["packaged"]])
        self.assertEqual([record["argv"] for record in self.calls("sleep")], [["5"], ["5"]])
        result = self.publish(fixtures={self.STATUSES: [transient] * 9})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.posted()), 4)



MEMBERS_TOKEN = "fixture-members-token"
COMMENTS = f"GET repos/{REPOSITORY}/issues/17/comments?per_page=100&page={{page}}"
ORG_MEMBERS = f"GET orgs/{OWNER}/members?filter=all&role=all&per_page=100&page={{page}}"
MEMBERSHIP = f"GET orgs/{OWNER}/memberships/{{login}}"


def user_comment(number: int, author: int, body: str = "hello", *, login: str | None = None, kind: str = "User",
                 **changes: Any) -> dict[str, Any]:
    """One comment of pull request 17 as the API lists it."""

    return {"id": number, "body": body, "user": {"login": login or f"user-{author}", "id": author, "type": kind},
            "author_association": "NONE", "performed_via_github_app": None, **changes}


def org_member(user_id: int) -> dict[str, Any]:
    return {"login": f"user-{user_id}", "id": user_id, "type": "User", "site_admin": False}


class MembersAuthorsTests(ScriptCase):
    """The members job's first step: every comment author it must check, read like the kit reads them."""

    JOB, STEP = "members", AUTHORS_STEP

    def authors(self, pages: list[list[dict[str, Any]]] | Mapping[str, Any],
                **environment: str | None) -> subprocess.CompletedProcess[str]:
        fixtures = pages if isinstance(pages, Mapping) else {
            COMMENTS.format(page=index + 1): {"body": rows} for index, rows in enumerate(pages)}
        return self.run_script({**self.base(), "PR_NUMBER": "17", **environment}, fixtures)

    def written(self, result: subprocess.CompletedProcess[str]) -> tuple[list[int], int, list[dict[str, Any]]]:
        self.assertEqual(result.returncode, 0, result.stderr)
        written = outputs(self.output)
        self.assertEqual(set(written), {"checked", "through", "candidates"})
        self.assertTrue(all(record["token"] == TOKEN for record in self.calls()))
        return json.loads(written["checked"]), int(written["through"]), json.loads(written["candidates"])

    def test_every_user_author_is_checked_whatever_the_comment_says(self) -> None:
        approve = f"/controller-upgrade approve {HEAD}"
        rows = [user_comment(5, 303, "thanks"), user_comment(9, 202, approve), user_comment(7, 202, "LGTM"),
                user_comment(11, 404, f" {approve} "), user_comment(12, 505, f"\x1f/x revoke {HEAD}"),
                user_comment(13, 606, "I approve of this"), user_comment(14, 707, "APPROVE")]
        result = self.authors([rows])
        checked, through, candidates = self.written(result)
        self.assertEqual(checked, [202, 303, 404, 505, 606, 707])
        self.assertEqual(through, 14)
        # Whatever the kit recognises as a decision contains one of the two words, so its author is
        # a candidate whose membership is read; the words may be anywhere, in any comment.
        self.assertEqual(candidates, [{"id": 202, "login": "user-202"}, {"id": 404, "login": "user-404"},
                                      {"id": 505, "login": "user-505"}, {"id": 606, "login": "user-606"}])
        self.assertEqual([record["route"] for record in self.calls()], [COMMENTS.format(page=1)])
        self.assertNotIn("user-202", result.stdout)
        self.assertIn("Read 7 comment(s) through comment 14: 6 author(s) checked, 4 whose comment could be a decision",
                      result.stdout)

    def test_bots_and_comments_through_an_app_are_skipped_as_the_kit_skips_them(self) -> None:
        approve = f"/controller-upgrade approve {HEAD}"
        rows = [user_comment(20, 1, approve, kind="Bot", login="github-actions[bot]"),
                user_comment(21, 2, approve, performed_via_github_app={"id": 3, "slug": "an-app"}),
                user_comment(22, 3, approve, performed_via_github_app=""),
                {**user_comment(23, 4, approve), "user": None},
                {key: value for key, value in user_comment(24, 5, approve).items() if key != "performed_via_github_app"},
                user_comment(25, 6, approve, kind="Organization")]
        checked, through, candidates = self.written(self.authors([rows]))
        # An absent `performed_via_github_app` is no App, as for the kit.
        self.assertEqual((checked, through, candidates), ([5], 25, [{"id": 5, "login": "user-5"}]))

    def test_no_comment_and_three_hundred_comments_are_read_and_one_more_fails(self) -> None:
        self.assertEqual(self.written(self.authors([[]])), ([], 0, []))
        full = [[user_comment(page * 100 + index + 1, 1000 + page) for index in range(100)] for page in range(3)]
        checked, through, _ = self.written(self.authors([*full, []]))
        self.assertEqual((checked, through), ([1000, 1001, 1002], 300))
        self.assertEqual([record["route"] for record in self.calls()], [COMMENTS.format(page=page) for page in (1, 2, 3, 4)])
        result = self.authors([*full, [user_comment(301, 1)]])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("more than 300 comments", result.stderr)
        self.assertEqual(outputs(self.output), {})

    def test_a_malformed_or_unreadable_listing_fails(self) -> None:
        approve = f"/controller-upgrade approve {HEAD}"
        for label, rows in {"an object": {"message": "x"}, "no id": [{"body": "x"}], "a text id": [{"id": "1", "body": "x"}],
                            "no body": [{"id": 1}], "a body that is no text": [{"id": 1, "body": 7}],
                            "a user without id": [{**user_comment(1, 2, approve), "user": {"login": "x", "type": "User"}}],
                            "a user without login": [{**user_comment(1, 2), "user": {"id": 2, "type": "User"}}],
                            "a fractional id": [user_comment(1, 2) | {"id": 1.5}],
                            "a long page": [user_comment(index + 1, 2) for index in range(101)]}.items():
            with self.subTest(listing=label):
                result = self.authors([rows])  # type: ignore[list-item]
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("the comments of the pull request are malformed", result.stderr)
                self.assertEqual(outputs(self.output), {})
        result = self.authors({COMMENTS.format(page=1): {"fail": "gh: Forbidden (HTTP 403)"}})
        self.assertIn("cannot list the comments of the pull request", result.stderr)
        for number in ("", "0", "17 ", "17; id"):
            with self.subTest(number=number):
                result = self.authors([[]], PR_NUMBER=number)
                self.assertIn("malformed pull request number", result.stderr)
                self.assertEqual(self.calls(), [])


class MembersSnapshotTests(ScriptCase):
    """The members job's last step: the active members among the candidates, as one canonical document."""

    JOB, STEP = "members", SNAPSHOT_STEP
    CANDIDATES = [{"id": 202, "login": "concealed"}, {"id": 303, "login": "collaborator"}]

    def snapshot(self, fixtures: Mapping[str, Any], *, checked: list[int] | None = None,
                 candidates: list[dict[str, Any]] | None = None, **environment: str | None
                 ) -> subprocess.CompletedProcess[str]:
        values = {**self.base(), "GH_TOKEN": MEMBERS_TOKEN, "ORGANIZATION": OWNER, "PR_NUMBER": "17", "THROUGH": "30",
                  "CHECKED": json.dumps([101, 202, 303] if checked is None else checked),
                  "CANDIDATES": json.dumps(self.CANDIDATES if candidates is None else candidates), **environment}
        return self.run_script(values, fixtures)

    def document(self, result: subprocess.CompletedProcess[str]) -> tuple[dict[str, Any], str]:
        self.assertEqual(result.returncode, 0, result.stderr)
        written = outputs(self.output)
        self.assertEqual(set(written), {"members", "recheck"})
        # Exactly the bytes the kit admits: canonical JSON of this pull request and owner.
        path = self.root / "snapshot.json"
        path.write_text(written["members"] + "\n", encoding="utf-8")
        loaded = load_org_members(path, repository=REPOSITORY, pr_number=17)
        self.assertEqual(path.read_bytes(), canonical_json(json.loads(written["members"])))
        self.assertTrue(all(record["token"] == MEMBERS_TOKEN for record in self.calls()))
        self.assertEqual(written["recheck"], "true" if loaded.members else "false")
        return json.loads(written["members"]), written["recheck"]

    def test_without_a_candidate_nothing_is_read_and_no_token_is_needed(self) -> None:
        document, recheck = self.document(self.snapshot({}, candidates=[], GH_TOKEN=""))
        self.assertEqual(document, {"kind": "mod-base.ci.org-members", "schema_version": 1, "organization": OWNER,
                                    "pr_number": 17, "through_comment_id": 30, "checked": [101, 202, 303],
                                    "members": []})
        self.assertEqual((recheck, self.calls()), ("false", []))

    def test_the_candidates_on_the_member_list_are_its_members_by_id(self) -> None:
        listed = [org_member(user_id) for user_id in (1, 202, 999)]
        result = self.snapshot({ORG_MEMBERS.format(page=1): {"body": listed},
                                MEMBERSHIP.format(login="collaborator"): {"fail": "gh: Not Found (HTTP 404)"}})
        document, recheck = self.document(result)
        self.assertEqual((document["members"], recheck), ([202], "true"))
        self.assertEqual([record["route"] for record in self.calls()],
                         [ORG_MEMBERS.format(page=1), MEMBERSHIP.format(login="collaborator")])
        # The member list itself is never printed: only the members among the candidates.
        for hidden in ("user-1", "user-999", '"id":999', "999"):
            self.assertNotIn(hidden, result.stdout + result.stderr)
        self.assertIn("Members of The-Plum-Team among the candidates: [202]", result.stdout)

    def test_a_full_list_is_read_to_its_end_and_more_than_a_thousand_members_fail(self) -> None:
        pages = {ORG_MEMBERS.format(page=page): {"body": [org_member(page * 1000 + index) for index in range(100)]}
                 for page in range(1, 11)}
        pages[ORG_MEMBERS.format(page=1)]["body"][7] = org_member(202)
        pages[ORG_MEMBERS.format(page=10)]["body"][3] = org_member(303)
        document, _ = self.document(self.snapshot({**pages, ORG_MEMBERS.format(page=11): {"body": []}}))
        self.assertEqual(document["members"], [202, 303])
        self.assertEqual(len(self.calls()), 11)
        result = self.snapshot({**pages, ORG_MEMBERS.format(page=11): {"body": [org_member(5)]}})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("more than 1000 members", result.stderr)
        self.assertEqual(outputs(self.output), {})

    def test_a_candidate_missing_from_the_list_is_asked_for_its_own_active_membership(self) -> None:
        listing = {ORG_MEMBERS.format(page=1): {"body": [org_member(1)]}}
        answers = {
            "active": ({"state": "active", "role": "member", "user": {"id": 202, "login": "concealed"}}, [202]),
            "pending": ({"state": "pending", "role": "member", "user": {"id": 202, "login": "concealed"}}, []),
            "not a member": ({"fail": "gh: Not Found (HTTP 404)"}, []),
        }
        for label, (answer, members) in answers.items():
            with self.subTest(answer=label):
                response = answer if "fail" in answer else {"body": answer}
                result = self.snapshot({**listing, MEMBERSHIP.format(login="concealed"): response,
                                        MEMBERSHIP.format(login="collaborator"): {"fail": "gh: Not Found (HTTP 404)"}})
                self.assertEqual(self.document(result)[0]["members"], members)
        for label, response in {
                "another account under the login": {"body": {"state": "active", "user": {"id": 9, "login": "x"}}},
                "no state": {"body": {"user": {"id": 202}}},
                "a refusal": {"fail": "gh: Forbidden (HTTP 403)"},
                "no JSON": {"body": "<html>"}}.items():
            with self.subTest(answer=label):
                result = self.snapshot({**listing, MEMBERSHIP.format(login="concealed"): response,
                                        MEMBERSHIP.format(login="collaborator"): {"fail": "gh: Not Found (HTTP 404)"}})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("cannot read the membership of the candidates", result.stderr)
                self.assertEqual(outputs(self.output), {})

    def test_at_most_twenty_candidates_are_asked_one_by_one(self) -> None:
        def candidates(count: int) -> list[dict[str, Any]]:
            return [{"id": 1000 + index, "login": f"user-{1000 + index}"} for index in range(count)]

        absent = {MEMBERSHIP.format(login=f"user-{1000 + index}"): {"fail": "gh: Not Found (HTTP 404)"}
                  for index in range(21)}
        listing = {ORG_MEMBERS.format(page=1): {"body": []}}
        twenty = candidates(20)
        document, _ = self.document(self.snapshot({**listing, **absent}, checked=[item["id"] for item in twenty],
                                                  candidates=twenty))
        self.assertEqual((document["members"], len(self.calls())), ([], 21))
        many = candidates(21)
        result = self.snapshot({**listing, **absent}, checked=[item["id"] for item in many], candidates=many)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("More than 20 candidates are missing from the member list", result.stderr)
        self.assertEqual(len(self.calls()), 1)

    def test_malformed_inputs_or_a_malformed_list_read_nothing_or_fail(self) -> None:
        for label, environment in {
                "another organization": {"ORGANIZATION": "another-org"}, "no organization": {"ORGANIZATION": ""},
                "a malformed number": {"PR_NUMBER": "17 "}, "a malformed comment id": {"THROUGH": "-1"},
                "a candidate that was not checked": {"CHECKED": "[101]"},
                "a candidate with more keys": {"CANDIDATES": '[{"id":202,"login":"x","admin":true}]'},
                "checked authors that are no list": {"CHECKED": "{}"}, "no App token": {"GH_TOKEN": ""}}.items():
            with self.subTest(case=label):
                result = self.snapshot({ORG_MEMBERS.format(page=1): {"body": []}}, **environment)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Read the organization members: ", result.stderr)
                self.assertEqual((self.calls(), outputs(self.output)), ([], {}))
        for label, response in {"an object": {"body": {"message": "x"}}, "an entry without id": {"body": [{}]},
                                "a refusal": {"fail": "gh: Forbidden (HTTP 403)"},
                                "a long page": {"body": [org_member(index + 1) for index in range(101)]}}.items():
            with self.subTest(listing=label):
                result = self.snapshot({ORG_MEMBERS.format(page=1): response})
                self.assertIn("cannot read the membership of the candidates", result.stderr)
                self.assertEqual(outputs(self.output), {})
        # A login that could leave the path is never requested.
        result = self.snapshot({ORG_MEMBERS.format(page=1): {"body": []}}, CANDIDATES='[{"id":202,"login":"../x"}]')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([record["route"] for record in self.calls()], [ORG_MEMBERS.format(page=1)])


class RecheckTests(ScriptCase):
    """The publishing job reads the membership of the snapshot's candidates again, and nothing else."""

    JOB, STEP = "publish", RECHECK_STEP
    CANDIDATES = [{"id": 202, "login": "concealed"}, {"id": 303, "login": "collaborator"}]
    DOCUMENT = {"kind": "mod-base.ci.org-members", "schema_version": 1, "organization": OWNER, "pr_number": 17,
                "through_comment_id": 30, "checked": [101, 202, 303], "members": [202]}

    def recheck(self, listed: list[int], **environment: str | None) -> subprocess.CompletedProcess[str]:
        values = {**self.base(), "GH_TOKEN": MEMBERS_TOKEN, "ORGANIZATION": OWNER,
                  "MEMBERS": json.dumps(self.DOCUMENT, separators=(",", ":"), sort_keys=True),
                  "CANDIDATES": json.dumps(self.CANDIDATES), **environment}
        return self.run_script(values, {ORG_MEMBERS.format(page=1): {"body": [org_member(item) for item in listed]},
                                        MEMBERSHIP.format(login="collaborator"): {"fail": "gh: Not Found (HTTP 404)"},
                                        MEMBERSHIP.format(login="concealed"): {"fail": "gh: Not Found (HTTP 404)"}})

    def test_the_same_members_pass_and_any_change_publishes_nothing(self) -> None:
        result = self.recheck([1, 202])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([record["route"] for record in self.calls()],
                         [ORG_MEMBERS.format(page=1), MEMBERSHIP.format(login="collaborator")])
        self.assertTrue(all(record["token"] == MEMBERS_TOKEN for record in self.calls()))
        for label, listed in {"a member was removed": [1], "a candidate became a member": [202, 303]}.items():
            with self.subTest(case=label):
                result = self.recheck(listed)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("the membership of the candidates changed after the evaluation; nothing is published",
                              result.stderr)

    def test_a_comment_posted_since_the_snapshot_cannot_fail_the_recheck(self) -> None:
        # The recheck reads the candidates of the snapshot alone: no comment listing is even routed.
        result = self.recheck([202])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any("comments" in record["route"] for record in self.calls()))

    def test_malformed_inputs_fail(self) -> None:
        for label, environment in {"no token": {"GH_TOKEN": ""}, "another organization": {"ORGANIZATION": "x"},
                                   "no document": {"MEMBERS": ""}, "no members": {"MEMBERS": '{"members":7}'},
                                   "no candidates": {"CANDIDATES": "{}"}}.items():
            with self.subTest(case=label):
                result = self.recheck([202], **environment)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Recheck the organization members: ", result.stderr)


class StatusCallerLintTests(unittest.TestCase):
    def test_every_run_body_parses_and_passes_shellcheck(self) -> None:
        require_tools("bash", "shellcheck")
        for job_id, job in document()["jobs"].items():
            for item in job.get("steps", []):
                if "run" not in item:
                    continue
                with self.subTest(job=job_id, step=item["name"]):
                    parsed = subprocess.run(["bash", "-n"], input=item["run"], capture_output=True, text=True,
                                            timeout=60)
                    self.assertEqual(parsed.returncode, 0, parsed.stderr)
                    # SC2154 is excluded as in the Pages tests: env: and the runner assign those variables.
                    checked = subprocess.run(["shellcheck", "-s", "bash", "-e", "SC2154", "-"], input=item["run"],
                                             capture_output=True, text=True, timeout=60)
                    self.assertEqual(checked.returncode, 0, checked.stdout)

    def test_the_rendered_caller_passes_actionlint_beside_the_guard_it_calls(self) -> None:
        require_tools("actionlint", "shellcheck")
        with tempfile.TemporaryDirectory(prefix="actionlint status caller ") as temporary:
            for path, content in rendered(paths=(GUARD_CALLER, STATUS_CALLER)).items():
                target = Path(temporary) / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            result = subprocess.run([shutil.which("actionlint") or "actionlint", "-no-color", STATUS_CALLER],
                                    cwd=temporary, capture_output=True, text=True, timeout=300)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()

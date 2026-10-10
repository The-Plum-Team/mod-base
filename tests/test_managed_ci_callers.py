"""The managed Build/E2E callers ``mod-base-guard.yml``, ``mod-base-build.yml`` and
``mod-base-packaged-e2e.yml`` (docs/BUILD-E2E-DESIGN.md: trust model, concurrency, drafts).

Every case reads the template as ``template sync`` renders it for a mod. Structure: triggers,
permissions, calls and their inputs, concurrency, and the guard's closed list of callers. Graphs:
the ``if:`` conditions are evaluated, with GitHub's expression rules, for every event and selection
outcome, and the jobs they let run or skip must be the jobs of the literal API listings under
``tests/fixtures/ci_graphs``. The guard's shell runs against a stub ``gh`` (accepted and rejected
bindings, bounded retry) and, with the real Git and the real managed bootstrap, against a
repository that holds the rendered callers.
"""

from __future__ import annotations

import base64
import http.server
import json
import math
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from mod_base import workflow
from mod_base.build_ci import graph
from mod_base.build_ci.activation import BUILD_CALLER, GUARD_CALLER, PACKAGED_CALLER, STATUS_CALLER
from mod_base.pin import Pin, parse_pin_files
from mod_base.template import tool
from tests.helpers import ci_activation
from tests.test_pin import BOOT
from tests.test_template_tool import git
from tests.test_workflow_policy import (EXPRESSION, ROOT, ShellHarness, caller, load_yaml, outputs, parse_yaml,
                                        require_tools, step)

REPOSITORY = "The-Plum-Team/Quick-Skin-Mod"
KIT = "The-Plum-Team/mod-base"
KIT_SHA = "0123456789abcdef0123456789abcdef01234567"
VERSION = "v1.2.3"
BRANCH = "master"
HEAD = "a" * 40
OTHER = "f" * 40
RUN_ID = "36042781699"
TOKEN = "fixture-token"
CALLERS = (GUARD_CALLER, BUILD_CALLER, PACKAGED_CALLER)
PRODUCERS = {"build": BUILD_CALLER, "packaged": PACKAGED_CALLER}
#: Every workflow that calls the guard: the two producers and the gate status caller, whose own
#: policy is in ``tests/test_managed_status_caller.py``.
GUARDED_CALLERS = {**PRODUCERS, "status": STATUS_CALLER}
#: The events that start the status caller (the guard admits them for that caller alone).
STATUS_EVENTS = ("workflow_run", "pull_request_target", "schedule", "workflow_dispatch")
MODES = {"build": graph.BUILD_MODES, "packaged": graph.PACKAGED_MODES}
LOCAL_GUARD = "./" + GUARD_CALLER
NAMES = {GUARD_CALLER: "mod-base guard", BUILD_CALLER: "mod-base Build", PACKAGED_CALLER: "mod-base packaged E2E"}
GUARD_STEP = "Verify the pin, kit reachability and referenced workflows"
DEFERRAL_STEP = "Report the draft deferral"
EVENTS = ("pull_request_target", "push", "workflow_dispatch")
PR_TYPES = ["opened", "synchronize", "reopened", "ready_for_review", "converted_to_draft"]
READ_RUN = {"actions": "read", "contents": "read"}
READ_ALL = {"actions": "read", "contents": "read", "pull-requests": "read"}
PERMISSIONS = {
    GUARD_CALLER: {"verify": READ_RUN},
    BUILD_CALLER: {"guard": READ_RUN, "deferred": {}, "shared": READ_ALL},
    PACKAGED_CALLER: {"guard": READ_RUN, "deferred": {}, "select": READ_ALL, "rebuild": READ_ALL, "shared": READ_ALL},
}
#: What each kit-calling job passes, in the order of the callee's inputs (architecture, section 5).
CALLS = {
    BUILD_CALLER: {"shared": {"kit-sha": "${{ needs.guard.outputs.kit-sha }}",
                              "pr-number": "${{ format('{0}', github.event.pull_request.number) }}"}},
    PACKAGED_CALLER: {
        "select": {"kit-sha": "${{ needs.guard.outputs.kit-sha }}"},
        "rebuild": {"kit-sha": "${{ needs.guard.outputs.kit-sha }}"},
        "shared": {"kit-sha": "${{ needs.guard.outputs.kit-sha }}",
                   "pr-number": "${{ format('{0}', github.event.pull_request.number) }}",
                   "mode": "${{ needs.select.outputs.mode }}",
                   "build-run-id": "${{ needs.rebuild.result == 'success' && 'same-run' || "
                                   "needs.select.outputs.build-run-id }}"}},
}
NEEDS = {
    BUILD_CALLER: {"deferred": "guard", "shared": "guard"},
    PACKAGED_CALLER: {"deferred": "guard", "select": "guard", "rebuild": ["guard", "select"],
                      "shared": ["guard", "select", "rebuild"]},
}
DRAFT = "github.event_name == 'pull_request_target' && github.event.pull_request.draft == true"
CONDITIONS = {
    BUILD_CALLER: {"deferred": DRAFT,
                   "shared": "github.event_name != 'pull_request_target' || github.event.pull_request.draft == false"},
    PACKAGED_CALLER: {
        "deferred": DRAFT,
        "select": "github.event_name != 'pull_request_target'",
        "rebuild": "github.event_name != 'pull_request_target' && needs.select.outputs.mode == 'full' && "
                   "needs.select.outputs.found == 'false'",
        "shared": "${{ !cancelled() && needs.guard.result == 'success' && ( "
                  "(github.event_name == 'pull_request_target' && github.event.pull_request.draft == false && "
                  "needs.select.result == 'skipped' && needs.rebuild.result == 'skipped') || "
                  "(github.event_name != 'pull_request_target' && needs.select.result == 'success' && ( "
                  "(needs.select.outputs.mode == 'reuse' && needs.rebuild.result == 'skipped') || "
                  "(needs.select.outputs.mode == 'full' && needs.select.outputs.found == 'true' && "
                  "needs.rebuild.result == 'skipped') || "
                  "(needs.select.outputs.mode == 'full' && needs.select.outputs.found == 'false' && "
                  "needs.rebuild.result == 'success')))) }}"},
}
REQUEST = ("${{ github.event.pull_request.number || (github.event_name == 'workflow_dispatch' && "
           "format('{0}-{1}', github.ref, github.run_id)) || github.ref }}")
GROUPS = {BUILD_CALLER: "build-gate-${{ github.workflow }}-" + REQUEST,
          PACKAGED_CALLER: "packaged-e2e-${{ github.workflow }}-" + REQUEST}
CANCEL = "${{ github.event_name == 'pull_request_target' }}"
#: The concurrency groups of the mods' own Build and packaged E2E workflows (Quick Skin's two, then
#: Block Pops' two; docs/BUILD-E2E-DESIGN.md, "Evidence and current behavior"). Each cancels a run
#: in progress, and in shadow mode each runs beside the managed caller of the same pull request.
_OWN_SUBJECT = "inputs.expected_sha || inputs.attest_target_sha || github.event.pull_request.number || github.ref"
OWN_GROUPS = (
    "build-gate-${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}",
    "packaged-e2e-${{ github.event.pull_request.number || github.ref }}",
    "build-gate-${{ " + _OWN_SUBJECT + " }}",
    "packaged-e2e-${{ " + _OWN_SUBJECT + " }}",
)


def rendered(sha: str = KIT_SHA, version: str = VERSION, branch: str = BRANCH,
             paths: tuple[str, ...] = CALLERS) -> dict[str, str]:
    """The three callers (or ``paths``) as ``template sync`` writes them for a mod pinned at
    ``sha``/``version`` whose canonical branch is ``branch``."""

    files = tool.expected_callers(ROOT, Pin(sha, version, ()), ci_activation("shadow"), branch)
    return {path: files[path].decode("utf-8") for path in paths}  # type: ignore[union-attr]


def documents() -> dict[str, dict[str, Any]]:
    return {path: parse_yaml(text, path) for path, text in rendered().items()}


def calls_of(caller: str) -> Mapping[str, str]:
    """Calling job -> called workflow of a caller that calls the guard."""

    return workflow.CI_STATUS_CALLS if caller == "status" else workflow.CI_CALLS[caller]


def declared(caller: str) -> str:
    """The ``callees`` a caller passes to the guard: the kit workflows it calls, in job order."""

    return " ".join(callee for callee in calls_of(caller).values() if callee != "guard")


def needed(job: Mapping[str, Any]) -> list[str]:
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else list(needs)


def kit_workflow(callee: str) -> str:
    return f"{KIT}/{workflow.CI_CALLEE_WORKFLOWS[callee]}"


# -- GitHub expressions -------------------------------------------------------------------------------

_TOKEN = re.compile(r"\s*(?:(?P<string>'(?:[^']|'')*')|(?P<number>\d+)|(?P<op>==|!=|&&|\|\||[!(),.\[\]])"
                    r"|(?P<name>[A-Za-z_][A-Za-z0-9_-]*))")
_STATUS_CHECK = re.compile(r"\b(?:success|always|cancelled|failure)\(\)")
_INTERPOLATION = re.compile(r"\$\{\{(.*?)\}\}")


def truthy(value: Any) -> bool:
    """GitHub's truthiness: ``false``, ``0``, ``NaN``, ``''`` and ``null`` are falsy."""

    if value is None or value is False or value == "":
        return False
    return not (isinstance(value, (int, float)) and not isinstance(value, bool) and (value == 0 or value != value))


def _kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    return "number" if isinstance(value, (int, float)) else "string" if isinstance(value, str) else "object"


def _number(value: Any) -> float:
    if value is None:
        return 0
    if isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        try:
            return float(value) if value.strip() else 0
        except ValueError:
            return math.nan
    return math.nan


def equal(left: Any, right: Any) -> bool:
    """GitHub's loose equality: strings compare without case, different types as numbers."""

    if _kind(left) == _kind(right):
        return left.lower() == right.lower() if isinstance(left, str) else left == right
    return _number(left) == _number(right)


def text_of(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


class _Evaluation:
    """One evaluation of an expression: ``!``, ``==``, ``!=``, ``&&``, ``||``, parentheses,
    literals, property access, a literal index and function calls. Anything else fails the test,
    so a template that starts to use another operator is not silently misread."""

    def __init__(self, text: str, context: Mapping[str, Any], functions: Mapping[str, Any]) -> None:
        self.items: list[tuple[str, str]] = []
        text, position = text.strip(), 0
        while position < len(text):
            match = _TOKEN.match(text, position)
            if match is None or match.end() == position or match.lastgroup is None:
                raise AssertionError(f"unsupported expression syntax: {text[position:position + 30]!r}")
            self.items.append((match.lastgroup, match.group(match.lastgroup)))
            position = match.end()
        self.index, self.context, self.functions = 0, context, functions

    def peek(self) -> tuple[str, str] | None:
        return self.items[self.index] if self.index < len(self.items) else None

    def take(self, expected: str | None = None) -> tuple[str, str]:
        item = self.peek()
        if item is None or (expected is not None and item != ("op", expected)):
            raise AssertionError(f"malformed expression near token {self.index}: expected {expected or 'a value'}")
        self.index += 1
        return item

    def result(self) -> Any:
        value = self.either()
        if self.peek() is not None:
            raise AssertionError(f"trailing expression text: {self.items[self.index:]}")
        return value

    def either(self) -> Any:
        value = self.both()
        while self.peek() == ("op", "||"):
            self.take()
            other = self.both()
            value = value if truthy(value) else other
        return value

    def both(self) -> Any:
        value = self.comparison()
        while self.peek() == ("op", "&&"):
            self.take()
            other = self.comparison()
            value = other if truthy(value) else value
        return value

    def comparison(self) -> Any:
        value = self.unary()
        while self.peek() in (("op", "=="), ("op", "!=")):
            operator = self.take()[1]
            other = self.unary()
            value = equal(value, other) == (operator == "==")
        return value

    def unary(self) -> Any:
        if self.peek() == ("op", "!"):
            self.take()
            return not truthy(self.unary())
        return self.primary()

    def primary(self) -> Any:
        kind, text = self.take()
        if kind == "string":
            return text[1:-1].replace("''", "'")
        if kind == "number":
            return int(text)
        if (kind, text) == ("op", "("):
            value = self.either()
            self.take(")")
            return value
        if kind != "name":
            raise AssertionError(f"malformed expression at {text!r}")
        if self.peek() == ("op", "("):
            self.take()
            arguments = []
            while self.peek() != ("op", ")"):
                if arguments:
                    self.take(",")
                arguments.append(self.either())
            self.take(")")
            if text not in self.functions:
                raise AssertionError(f"the callers do not use the function {text}()")
            return self.functions[text](*arguments)
        if text in ("true", "false", "null"):
            return {"true": True, "false": False, "null": None}[text]
        if text not in self.context:
            raise AssertionError(f"the callers do not read the {text} context")
        value = self.context[text]
        while self.peek() in (("op", "."), ("op", "[")):
            if self.take() == ("op", "["):
                # A literal index, as GitHub reads it: null beyond the end or of anything but a list.
                kind, index = self.take()
                if kind != "number":
                    raise AssertionError(f"the callers index a list with a literal number alone, not {index!r}")
                self.take("]")
                value = value[int(index)] if isinstance(value, list) and int(index) < len(value) else None
                continue
            kind, name = self.take()
            if kind != "name":
                raise AssertionError(f"malformed property {name!r}")
            value = value.get(name) if isinstance(value, Mapping) else None
        return value


def formatted(template: str, *values: Any) -> str:
    return re.sub(r"\{(\d+)\}", lambda match: text_of(values[int(match.group(1))]), template)


def evaluate(text: str, context: Mapping[str, Any], functions: Mapping[str, Any] | None = None) -> Any:
    return _Evaluation(text, context, {"format": formatted, **(functions or {})}).result()


def interpolate(text: str, context: Mapping[str, Any]) -> str:
    """A workflow string with every ``${{ }}`` replaced as GitHub renders it."""

    return _INTERPOLATION.sub(lambda match: text_of(evaluate(match.group(1), context)), text)


def condition(job: Mapping[str, Any]) -> str:
    """The job's ``if:`` as GitHub evaluates it: one expression, with ``success()`` implied unless
    it calls a status check itself."""

    text = job.get("if")
    if text is None:
        return "success()"
    text = text.strip()
    if text.startswith("${{"):
        if not text.endswith("}}"):
            raise AssertionError("an if: with text after its expression is a string, which is always true")
        text = text[3:-2]
    if "${{" in text or "}}" in text:
        raise AssertionError("an if: must be one expression")
    return text if _STATUS_CHECK.search(text) else f"success() && ({text})"


def simulate(document: Mapping[str, Any], github: Mapping[str, Any], *, job_outputs: Mapping[str, Mapping[str, str]],
             outcomes: Mapping[str, str] | None = None, cancelled: bool = False) -> dict[str, str]:
    """The result of every job of a caller, in YAML order: ``skipped`` where its ``if:`` is false,
    else its outcome (``success`` unless ``outcomes`` says otherwise). ``success()`` is false when
    any job the job needs, directly or not, did not succeed: a skipped one too."""

    results: dict[str, str] = {}
    ancestors: dict[str, set[str]] = {}
    for job_id, job in document["jobs"].items():
        needs = needed(job)
        if any(name not in results for name in needs):
            raise AssertionError(f"{job_id} needs a job that is defined after it")
        ancestors[job_id] = set(needs).union(*(ancestors[name] for name in needs))
        succeeded = not cancelled and all(results[name] == "success" for name in ancestors[job_id])
        functions = {"success": lambda succeeded=succeeded: succeeded, "cancelled": lambda: cancelled,
                     "failure": lambda job_id=job_id: any(results[name] == "failure" for name in ancestors[job_id])}
        ran = truthy(evaluate(condition(job), needs_context(github, needs, results, job_outputs), functions))
        results[job_id] = (outcomes or {}).get(job_id, "success") if ran else "skipped"
    return results


def needs_context(github: Mapping[str, Any], needs: list[str], results: Mapping[str, str],
                  job_outputs: Mapping[str, Mapping[str, str]]) -> dict[str, Any]:
    return {"github": github, "needs": {
        name: {"result": results[name], "outputs": job_outputs.get(name, {}) if results[name] == "success" else {}}
        for name in needs}}


def event(name: str, *, draft: bool | None = None, number: int = 17) -> dict[str, Any]:
    """The ``github`` context of one event on the default branch."""

    payload = {"pull_request": {"number": number, "draft": draft}} if name == "pull_request_target" else {}
    return {"event_name": name, "event": payload, "ref": f"refs/heads/{BRANCH}", "run_id": int(RUN_ID)}


def selection(mode: str, found: str, run_id: str = "") -> dict[str, dict[str, str]]:
    return {"guard": {"kit-sha": KIT_SHA}, "select": {"mode": mode, "found": found, "build-run-id": run_id}}


GUARDED = {"guard": {"kit-sha": KIT_SHA}}
#: Producer -> scenario -> (event, job outputs, the graph modes the run then is).
SCENARIOS: dict[str, dict[str, tuple[dict[str, Any], dict[str, dict[str, str]], tuple[str, ...]]]] = {
    "build": {
        "a draft pull request": (event("pull_request_target", draft=True), GUARDED, ("deferred",)),
        "a ready pull request": (event("pull_request_target", draft=False), GUARDED, ("full",)),
        # Whether a push reuses admitted evidence is decided inside the kit's Build workflow.
        "a push": (event("push"), GUARDED, ("full", "reuse")),
        "a dispatch": (event("workflow_dispatch"), GUARDED, ("full",)),
    },
    "packaged": {
        "a draft pull request": (event("pull_request_target", draft=True), GUARDED, ("deferred",)),
        "a ready pull request": (event("pull_request_target", draft=False), GUARDED, ("pull-request",)),
        "a push with reuse": (event("push"), selection("reuse", "false"), ("reuse",)),
        "a push with reuse of a named run": (event("push"), selection("reuse", "true", "777"), ("reuse",)),
        "a push with a found Build": (event("push"), selection("full", "true", "777"), ("selected",)),
        "a push without a Build": (event("push"), selection("full", "false"), ("rebuilt",)),
        "a dispatch with a found Build": (event("workflow_dispatch"), selection("full", "true", "777"), ("selected",)),
        "a dispatch without a found Build": (event("workflow_dispatch"), selection("full", "false"), ("rebuilt",)),
    },
}


def listing(producer: str, mode: str) -> list[dict[str, Any]]:
    path = ROOT / "tests/fixtures/ci_graphs" / f"{producer}-{mode}.json"
    return json.loads(path.read_text(encoding="utf-8"))["jobs"]


def listed_results(producer: str, mode: str) -> dict[str, str]:
    """What the literal API listing of ``mode`` says of each caller job: ``skipped`` for the one
    bare skipped job, ``success`` for a bare successful job or for the jobs of the workflow it
    called. Every listed job must belong to exactly one caller job."""

    jobs = listing(producer, mode)
    results: dict[str, str] = {}
    claimed = 0
    for job_id, name in workflow.CI_CALLER_JOBS[producer].items():
        bare = [job for job in jobs if job["name"] == name]
        nested = [job for job in jobs if job["name"].startswith(f"{name} / ")]
        claimed += len(bare) + len(nested)
        calls = job_id in workflow.CI_CALLS[producer]
        if len(bare) == 1 and not nested and (bare[0]["conclusion"] == "skipped" or not calls):
            results[job_id] = bare[0]["conclusion"]
        elif nested and not bare and calls:
            results[job_id] = "success"
        else:
            raise AssertionError(f"{producer}-{mode}.json lists {name!r} neither as one job nor as a called workflow")
    if claimed != len(jobs):
        raise AssertionError(f"{producer}-{mode}.json lists a job that no caller job owns")
    return results


class ExpressionTests(unittest.TestCase):
    """The evaluator the graph cases rely on follows GitHub's documented rules."""

    def test_loose_equality_truthiness_and_operand_results(self) -> None:
        context = {"github": {"event": {}, "event_name": "push", "ref": "refs/heads/master", "run_id": 7}}
        cases = {
            "github.event.pull_request.draft == false": True,       # null and false both compare as 0
            "github.event.pull_request.draft == true": False,
            "github.event.pull_request.number == ''": True,
            "github.event_name == 'PUSH'": True,                     # strings compare without case
            "github.event_name != 'push'": False,
            "github.event.pull_request.number || github.ref": "refs/heads/master",
            "github.event_name == 'push' && 'yes' || 'no'": "yes",
            "github.event_name == 'pull' && 'yes' || 'no'": "no",
            "!github.event.pull_request": True,
            "(true || false) && !false": True,
            "format('{0}-{1}', github.ref, github.run_id)": "refs/heads/master-7",
            "format('{0}', github.event.pull_request.number)": "",
            "'it''s' == 'IT''S'": True,
            "1 == '1'": True,
            "'x' == 0": False,
        }
        for text, expected in cases.items():
            with self.subTest(expression=text):
                self.assertEqual(evaluate(text, context), expected)
        self.assertEqual(interpolate("a-${{ github.run_id }}-${{ github.event.x }}-${{ true }}", context), "a-7--true")
        # A literal index: the element, or null beyond the end and of anything that is no list.
        listed = {"github": {"event": {"workflow_run": {"pull_requests": [{"number": 17}, {"number": 18}]}}}}
        for text, expected in {"github.event.workflow_run.pull_requests[0].number": 17,
                               "github.event.workflow_run.pull_requests[1].number": 18,
                               "github.event.workflow_run.pull_requests[2].number": None,
                               "github.event.workflow_run.pull_requests[2].number || 'none'": "none",
                               "github.event.pull_request.labels[0].name": None,
                               "github.event.workflow_run[0]": None}.items():
            with self.subTest(expression=text):
                self.assertEqual(evaluate(text, listed), expected)
        for text in ("github.ref < 1", "always()", "secrets.TOKEN", "github.ref ==", "contains(github.ref, 'x')",
                     "github.event.pull_requests[github.run_id]", "github.event.pull_requests['0']",
                     "github.event.pull_requests[0", "github.event.pull_requests[*].number"):
            with self.subTest(unsupported=text), self.assertRaises(AssertionError):
                evaluate(text, context)

    def test_an_if_is_one_expression_with_success_implied(self) -> None:
        self.assertEqual(condition({}), "success()")
        self.assertEqual(condition({"if": "github.ref == 'x'"}), "success() && (github.ref == 'x')")
        self.assertEqual(condition({"if": "${{ !cancelled() && github.ref == 'x' }}"}),
                         " !cancelled() && github.ref == 'x' ")
        for text in ("${{ github.ref == 'x' }} x", "${{ true }} && ${{ false }}", "true && ${{ false }}"):
            with self.subTest(text=text), self.assertRaises(AssertionError):
                condition({"if": text})


class CallerStructureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.texts = rendered()
        self.documents = documents()

    def test_headers_and_names(self) -> None:
        for path, text in self.texts.items():
            with self.subTest(path=path):
                self.assertRegex(text.splitlines()[0], r"^# mod-base managed: .+, edit only in The-Plum-Team/mod-base "
                                                       r"template/managed/" + re.escape(path) + "$")
                self.assertNotIn("PROVISIONAL", text)
                self.assertEqual(set(self.documents[path]) - {"env", "concurrency"}, {"name", "on", "permissions", "jobs"})
        self.assertEqual({path: document["name"] for path, document in self.documents.items()}, NAMES,
                         "no name a mod's own Build or packaged E2E workflow carries")

    def test_triggers_and_types_are_exactly_the_specified_ones(self) -> None:
        for path in PRODUCERS.values():
            with self.subTest(path=path):
                triggers = self.documents[path]["on"]
                self.assertEqual(list(triggers), list(EVENTS))
                self.assertEqual(triggers["pull_request_target"], {"branches": [BRANCH], "types": PR_TYPES})
                self.assertEqual(triggers["push"], {"branches": [BRANCH]})
                self.assertEqual(triggers["workflow_dispatch"], {})
        guard = self.documents[GUARD_CALLER]["on"]
        self.assertEqual(list(guard), ["workflow_call"])
        self.assertEqual(list(guard["workflow_call"]), ["inputs", "outputs"])
        self.assertEqual({name: (value["required"], value["type"]) for name, value in
                          guard["workflow_call"]["inputs"].items()}, {"callees": ("true", "string")})
        self.assertEqual({name: value["value"] for name, value in guard["workflow_call"]["outputs"].items()},
                         {"kit-sha": "${{ jobs.verify.outputs.kit-sha }}"})

    def test_the_event_base_is_the_canonical_branch_and_nothing_else_changes_with_it(self) -> None:
        other = rendered(branch="release/1.21")
        for path, text in self.texts.items():
            with self.subTest(path=path):
                changed = [(old, new) for old, new in zip(text.splitlines(), other[path].splitlines()) if old != new]
                expected = [] if path == GUARD_CALLER else [
                    ('    branches: ["master"]', '    branches: ["release/1.21"]')] * 2
                self.assertEqual(changed, expected)
        self.assertEqual(parse_yaml(other[BUILD_CALLER], "build")["on"]["push"], {"branches": ["release/1.21"]})

    def test_a_pull_request_against_another_base_creates_no_competing_run(self) -> None:
        # A head can belong to two pull requests. A run rejected for the other base would be
        # newest under that head and prevent selection of the honest default-base generation.
        for path, text in rendered(paths=tuple(GUARDED_CALLERS.values())).items():
            trigger = parse_yaml(text, path)["on"]["pull_request_target"]
            with self.subTest(caller=path):
                self.assertEqual(trigger.get("branches"), [BRANCH])
                self.assertNotIn("release/other", trigger["branches"])

    def test_no_permission_at_the_top_level_and_only_read_grants_below(self) -> None:
        for path, document in self.documents.items():
            with self.subTest(path=path):
                self.assertEqual(document["permissions"], {})
                self.assertEqual({job_id: job["permissions"] for job_id, job in document["jobs"].items()},
                                 PERMISSIONS[path])

    def test_no_secret_no_extension_and_no_other_trigger(self) -> None:
        forbidden = ("secrets", "inherit", "ext-", "# >>>", "# <<<", "always()", "workflow_run", "repository_dispatch",
                     "pull_request:", "schedule", "write", "id-token", "environment:", "continue-on-error",
                     "github.head_ref", "github.event.pull_request.head", "github.event.pull_request.title",
                     "github.event.pull_request.body", "actions/checkout", "job.workflow_sha")
        # The guard names the two events that start the gate status caller alone, and nowhere but
        # in the list it admits for that caller and in what it says when it refuses another event.
        status_events = ("workflow_run", "schedule")
        for path, text in self.texts.items():
            for word in forbidden:
                with self.subTest(path=path, word=word):
                    if path == GUARD_CALLER and word in status_events:
                        self.assertEqual([line.strip() for line in text.splitlines() if word in line], [
                            "workflow_run | pull_request_target | schedule | workflow_dispatch) ;;",
                            '*) fail "the gate status caller runs only on workflow_run, pull_request_target, '
                            'schedule or workflow_dispatch" ;;'])
                    else:
                        self.assertNotIn(word, text)
            with self.subTest(path=path):
                self.assertNotIn("{{", text.replace("${{", ""), "no placeholder is left")
                self.assertNotIn("\t", text)
                self.assertNotIn("on:\n  workflow_run", text)
                self.assertEqual(len(re.findall(r"^  (?:schedule|workflow_run):", text, re.MULTILINE)), 0,
                                 "neither a producer nor the guard is started by those events")

    def test_jobs_are_the_registered_ones_in_order(self) -> None:
        for producer, path in PRODUCERS.items():
            with self.subTest(producer=producer):
                jobs = self.documents[path]["jobs"]
                self.assertEqual([(job_id, job["name"]) for job_id, job in jobs.items()],
                                 list(workflow.CI_CALLER_JOBS[producer].items()))
                for job_id, job in jobs.items():
                    self.assertEqual(job["name"], workflow.ci_caller_job_name(producer, job_id))
                    self.assertEqual("uses" in job, job_id in workflow.CI_CALLS[producer], job_id)
                    self.assertEqual(job.get("needs"), NEEDS[path].get(job_id), job_id)
                    self.assertEqual(job.get("if"), CONDITIONS[path].get(job_id), job_id)
                    self.assertNotIn("secrets", job)
        guard = self.documents[GUARD_CALLER]["jobs"]
        self.assertEqual({job_id: job["name"] for job_id, job in guard.items()}, workflow.CI_GUARD_JOBS)
        self.assertEqual(workflow.ci_api_job_name("build", "guard", "verify"),
                         "Verify pinned mod-base / Authenticate the pinned kit")

    def test_every_uses_is_the_local_guard_or_a_kit_workflow_at_the_one_pin(self) -> None:
        for producer, path in PRODUCERS.items():
            with self.subTest(producer=producer):
                jobs = self.documents[path]["jobs"]
                expected = {job_id: LOCAL_GUARD if callee == "guard" else f"{kit_workflow(callee)}@{KIT_SHA}"
                            for job_id, callee in workflow.CI_CALLS[producer].items()}
                self.assertEqual({job_id: job["uses"] for job_id, job in jobs.items() if "uses" in job}, expected)
                for job in jobs.values():
                    self.assertFalse(any("uses" in item for item in job.get("steps", [])), "no action runs in a caller")
        for job in self.documents[GUARD_CALLER]["jobs"].values():
            self.assertNotIn("uses", job)
            self.assertFalse(any("uses" in item for item in job["steps"]), "the guard is shell alone")
        files = {path: text.encode("utf-8") for path, text in self.texts.items()}
        pin = parse_pin_files(files)
        self.assertEqual((pin.sha, pin.version, len(pin.references)), (KIT_SHA, VERSION, 4))
        bootstrap = BOOT.parse_pin_files(files)
        self.assertEqual((bootstrap.sha, bootstrap.version, len(bootstrap.references)), (KIT_SHA, VERSION, 4),
                         "the managed bootstrap, which the guard runs, reads the same single pin")

    def test_calls_pass_exactly_the_callee_inputs(self) -> None:
        for producer, path in PRODUCERS.items():
            jobs = self.documents[path]["jobs"]
            self.assertEqual(jobs["guard"]["with"], {"callees": " ".join(
                callee for callee in workflow.CI_CALLS[producer].values() if callee != "guard")})
            for job_id, callee in workflow.CI_CALLS[producer].items():
                if callee == "guard":
                    continue
                with self.subTest(producer=producer, job=job_id):
                    passed = jobs[job_id]["with"]
                    self.assertEqual(list(passed.items()), list(CALLS[path][job_id].items()))
                    source = ROOT / workflow.CI_CALLEE_WORKFLOWS[callee]
                    if source.is_file():
                        inputs = load_yaml(source)["on"]["workflow_call"]["inputs"]
                        self.assertEqual(list(passed), list(inputs)[:len(passed)], "inputs in the callee's order")
                        for name, definition in inputs.items():
                            self.assertEqual(definition["type"], "string", name)
                            self.assertTrue(name in passed or definition["required"] == "false", name)

    def test_each_calling_job_grants_exactly_what_the_callee_jobs_hold(self) -> None:
        for producer, path in PRODUCERS.items():
            jobs = self.documents[path]["jobs"]
            self.assertEqual(jobs["guard"]["permissions"],
                             self.documents[GUARD_CALLER]["jobs"]["verify"]["permissions"])
            for job_id, callee in workflow.CI_CALLS[producer].items():
                granted = jobs[job_id]["permissions"]
                with self.subTest(producer=producer, job=job_id):
                    self.assertLessEqual(set(granted.items()), set(READ_ALL.items()), "read grants only")
                    held = list(workflow.CI_JOB_PERMISSIONS.get(callee, {}).values())
                    source = None if callee == "guard" else ROOT / workflow.CI_CALLEE_WORKFLOWS[callee]
                    if source is not None and source.is_file():
                        held.extend(job["permissions"] for job in load_yaml(source)["jobs"].values())
                    for permissions in held:
                        self.assertLessEqual(set(permissions.items()), set(granted.items()),
                                             "a callee job cannot hold more than its calling job grants")
                    if held:
                        union = {scope: level for permissions in held for scope, level in permissions.items()}
                        self.assertEqual(granted, union, "and the calling job grants nothing a callee job lacks")

    def test_concurrency_groups(self) -> None:
        for producer, path in PRODUCERS.items():
            concurrency = self.documents[path]["concurrency"]
            self.assertEqual(concurrency, {"group": GROUPS[path], "cancel-in-progress": CANCEL})
            prefix = {"build": "build-gate-mod-base Build-", "packaged": "packaged-e2e-mod-base packaged E2E-"}[producer]
            name = self.documents[path]["name"]
            cases = {
                "pull_request_target": (f"{prefix}17", "true"),
                "push": (f"{prefix}refs/heads/master", "false"),
                "workflow_dispatch": (f"{prefix}refs/heads/master-{RUN_ID}", "false"),
            }
            for name_of_event, (group, cancel) in cases.items():
                with self.subTest(producer=producer, event=name_of_event):
                    context = {"github": {**event(name_of_event, draft=False), "workflow": name}}
                    self.assertEqual(interpolate(concurrency["group"], context), group)
                    self.assertEqual(interpolate(concurrency["cancel-in-progress"], context), cancel)
            other = {"github": {**event("pull_request_target", draft=True, number=18), "workflow": name}}
            self.assertEqual(interpolate(concurrency["group"], other), f"{prefix}18", "one group per pull request")
        self.assertNotIn("concurrency", self.documents[GUARD_CALLER])

    def test_no_group_is_one_a_mod_s_own_gate_workflow_takes(self) -> None:
        """In shadow mode the mod's own Build and packaged E2E run beside the managed callers and
        stay authoritative. In a shared group the run that starts second cancels the other one of
        the same pull request, and a push to the default branch waits behind the other's."""

        subjects = {"pull_request_target": "17", "push": f"refs/heads/{BRANCH}", "workflow_dispatch": f"refs/heads/{BRANCH}"}
        self.assertEqual(tuple(subjects), EVENTS)
        for name_of_event, subject in subjects.items():
            github = event(name_of_event, draft=False)
            own = {interpolate(group, {"github": {**github, "workflow": own_name}, "inputs": {}})
                   for group in OWN_GROUPS for own_name in ("Build gate", "Packaged E2E")}
            self.assertEqual(own, {f"build-gate-Build gate-{subject}", f"build-gate-Packaged E2E-{subject}",
                                   f"build-gate-{subject}", f"packaged-e2e-{subject}"})
            for producer, path in PRODUCERS.items():
                document = self.documents[path]
                managed = interpolate(document["concurrency"]["group"],
                                      {"github": {**github, "workflow": document["name"]}})
                with self.subTest(producer=producer, event=name_of_event):
                    self.assertNotIn(managed, own)
                    self.assertIn(document["name"], managed, "the group names the managed workflow")

    def test_event_data_reaches_shell_only_through_env(self) -> None:
        environments = {
            (GUARD_CALLER, "verify", GUARD_STEP): {"GH_TOKEN": "${{ github.token }}", "CALLEES": "${{ inputs.callees }}"},
            (BUILD_CALLER, "deferred", DEFERRAL_STEP): {"PR_NUMBER": "${{ github.event.pull_request.number }}"},
            (PACKAGED_CALLER, "deferred", DEFERRAL_STEP): {"PR_NUMBER": "${{ github.event.pull_request.number }}"},
        }
        seen = {}
        for path, document in self.documents.items():
            for job_id, job in document["jobs"].items():
                if "steps" in job:
                    self.assertEqual(job["runs-on"], "ubuntu-24.04")
                    self.assertIn("timeout-minutes", job)
                for item in job.get("steps", []):
                    self.assertEqual(set(item), {"name", "shell", "env", "run"} | ({"id"} if "id" in item else set()))
                    self.assertEqual(item["shell"], "bash")
                    self.assertNotRegex(item["run"], EXPRESSION)
                    self.assertTrue(item["run"].startswith("set -euo pipefail\n"))
                    seen[(path, job_id, item["name"])] = item["env"]
        self.assertEqual(seen, environments)
        self.assertEqual(self.documents[GUARD_CALLER]["env"], {"MB_KIT_SHA": KIT_SHA, "MB_KIT_VERSION": VERSION})
        self.assertEqual(self.documents[GUARD_CALLER]["jobs"]["verify"]["outputs"],
                         {"kit-sha": "${{ steps.bind.outputs.kit-sha }}"})

    def test_the_guard_admits_exactly_the_declared_callees_of_each_caller(self) -> None:
        script = step(self.documents[GUARD_CALLER]["jobs"]["verify"]["steps"], GUARD_STEP)["run"]
        arms = re.findall(r'^  (?:"([a-z0-9 -]+)"|([a-z0-9-]+))\) caller=(\S+) ;;$', script, re.MULTILINE)
        closed = {quoted or plain: path for quoted, plain, path in arms}
        self.assertEqual((len(arms), script.count(" caller=")), (3, 3))
        self.assertEqual({callees: path for callees, path in closed.items() if path in PRODUCERS.values()},
                         {self.documents[path]["jobs"]["guard"]["with"]["callees"]:
                          workflow.CI_CALLER_WORKFLOWS[producer] for producer, path in PRODUCERS.items()})
        # The third caller of the guard is the gate status caller, with the one kit workflow it calls.
        self.assertEqual(closed, {declared(caller): workflow.CI_CALLER_WORKFLOWS[caller] for caller in GUARDED_CALLERS})
        self.assertEqual(closed["gate-status"], STATUS_CALLER)
        self.assertEqual(GUARDED_CALLERS, {caller: workflow.CI_CALLER_WORKFLOWS[caller]
                                           for caller in (*workflow.CI_CALLS, "status")})
        self.assertEqual(set(PRODUCERS.values()), {workflow.CI_CALLER_WORKFLOWS[producer] for producer in workflow.CI_CALLS})
        self.assertIn(f'"$GITHUB_REPOSITORY/{workflow.CI_GUARD_WORKFLOW_PATH}"', script)
        self.assertIn(f'"{KIT}/.github/workflows/\\(.).yml@\\($kit)"', script)
        self.assertEqual(sorted({path.rsplit("/", 1)[1][:-4] for path in workflow.CI_CALLEE_WORKFLOWS.values()}),
                         sorted(workflow.CI_CALLEE_WORKFLOWS), "a callee id is its file name, as the guard assumes")
        # Two closed lists of events: the status caller's first, then the two producers'.
        events = re.findall(r"^    ([a-z_ |]+)\) ;;$", script, re.MULTILINE)
        self.assertEqual([arm.split(" | ") for arm in events], [list(STATUS_EVENTS), list(EVENTS)])
        self.assertEqual(script.count('if [[ "$CALLEES" == gate-status ]]; then'), 1)
        self.assertEqual(len(re.findall(r"^\s+[a-z_ |]+\) ;;$", script, re.MULTILINE)), 2, "no third list")

    def test_the_retry_function_is_the_pages_caller_s(self) -> None:
        pattern = re.compile(r"(?ms)^protected_gh_api_retry\(\) \{.*?^\}\n")
        own = pattern.findall(step(self.documents[GUARD_CALLER]["jobs"]["verify"]["steps"], GUARD_STEP)["run"])
        pages = pattern.findall(step(caller()["jobs"]["verify-kit"]["steps"],
                                     "Bind the executing callee to the protected pin")["run"])
        self.assertEqual((len(own), own), (1, pages))


class CallerGraphTests(unittest.TestCase):
    """The jobs each event lets run are the jobs of the literal API listing of its mode."""

    def setUp(self) -> None:
        self.documents = documents()

    def test_every_event_yields_the_listed_jobs_of_its_mode(self) -> None:
        for producer, scenarios in SCENARIOS.items():
            reached = set()
            for label, (github, job_outputs, modes) in scenarios.items():
                results = simulate(self.documents[PRODUCERS[producer]], github, job_outputs=job_outputs)
                for mode in modes:
                    reached.add(mode)
                    with self.subTest(producer=producer, scenario=label, mode=mode):
                        self.assertEqual(results, listed_results(producer, mode))
            self.assertEqual(reached, set(MODES[producer]), "every mode of the closed set is reached")

    def test_the_attest_only_shape_is_no_run_of_the_build_caller(self) -> None:
        """Attest-only dispatches stay on the mods' own route: the caller owns no such job, and no
        event leaves both the deferral and the Build call skipped behind a successful guard."""

        with self.assertRaisesRegex(AssertionError, "lists a job that no caller job owns"):
            listed_results("build", "attest-only")
        for label, (github, job_outputs, _modes) in SCENARIOS["build"].items():
            results = simulate(self.documents[BUILD_CALLER], github, job_outputs=job_outputs)
            with self.subTest(scenario=label):
                self.assertEqual(sorted((results["deferred"], results["shared"])), ["skipped", "success"])

    def test_the_listings_show_the_steps_of_the_caller_owned_jobs(self) -> None:
        guard = [item["name"] for item in self.documents[GUARD_CALLER]["jobs"]["verify"]["steps"]]
        self.assertEqual(guard, [GUARD_STEP])
        for producer, path in PRODUCERS.items():
            deferral = [item["name"] for item in self.documents[path]["jobs"]["deferred"]["steps"]]
            self.assertEqual(deferral, [DEFERRAL_STEP])
            for mode in MODES[producer]:
                with self.subTest(producer=producer, mode=mode):
                    steps = {job["name"]: [item["name"] for item in job["steps"]] for job in listing(producer, mode)}
                    self.assertEqual(steps[workflow.ci_api_job_name(producer, "guard", "verify")],
                                     ["Set up job", *guard, "Complete job"])
                    expected = ["Set up job", *deferral, "Complete job"] if mode == "deferred" else []
                    self.assertEqual(steps[workflow.ci_caller_job_name(producer, "deferred")], expected)

    def test_what_the_packaged_call_receives_in_each_mode(self) -> None:
        document = self.documents[PACKAGED_CALLER]
        expected = {
            "a ready pull request": {"kit-sha": KIT_SHA, "pr-number": "17", "mode": "", "build-run-id": ""},
            "a push with reuse": {"kit-sha": KIT_SHA, "pr-number": "", "mode": "reuse", "build-run-id": ""},
            "a push with reuse of a named run": {"kit-sha": KIT_SHA, "pr-number": "", "mode": "reuse",
                                                 "build-run-id": "777"},
            "a push with a found Build": {"kit-sha": KIT_SHA, "pr-number": "", "mode": "full", "build-run-id": "777"},
            "a push without a Build": {"kit-sha": KIT_SHA, "pr-number": "", "mode": "full", "build-run-id": "same-run"},
            "a dispatch with a found Build": {"kit-sha": KIT_SHA, "pr-number": "", "mode": "full",
                                              "build-run-id": "777"},
            "a dispatch without a found Build": {"kit-sha": KIT_SHA, "pr-number": "", "mode": "full",
                                                 "build-run-id": "same-run"},
        }
        for label, values in expected.items():
            github, job_outputs, _modes = SCENARIOS["packaged"][label]
            results = simulate(document, github, job_outputs=job_outputs)
            with self.subTest(scenario=label):
                self.assertEqual(results["shared"], "success")
                context = needs_context(github, needed(document["jobs"]["shared"]), results, job_outputs)
                self.assertEqual({name: interpolate(value, context) for name, value in
                                  document["jobs"]["shared"]["with"].items()}, values)
                rebuilt = results["rebuild"] == "success"
                self.assertEqual(rebuilt, values["build-run-id"] == "same-run")
                if rebuilt:
                    context = needs_context(github, needed(document["jobs"]["rebuild"]), results, job_outputs)
                    self.assertEqual({name: interpolate(value, context) for name, value in
                                      document["jobs"]["rebuild"]["with"].items()}, {"kit-sha": KIT_SHA},
                                     "a rebuild is never a pull request's: it passes no pr-number")
        build = self.documents[BUILD_CALLER]
        for label, number in {"a ready pull request": "17", "a push": "", "a dispatch": ""}.items():
            github, job_outputs, _modes = SCENARIOS["build"][label]
            results = simulate(build, github, job_outputs=job_outputs)
            context = needs_context(github, ["guard"], results, job_outputs)
            with self.subTest(build=label):
                self.assertEqual({name: interpolate(value, context) for name, value in
                                  build["jobs"]["shared"]["with"].items()}, {"kit-sha": KIT_SHA, "pr-number": number})

    def test_nothing_follows_a_failed_guard_a_failed_step_or_a_cancellation(self) -> None:
        for producer, scenarios in SCENARIOS.items():
            document = self.documents[PRODUCERS[producer]]
            for label, (github, job_outputs, _modes) in scenarios.items():
                with self.subTest(producer=producer, scenario=label):
                    failed = simulate(document, github, job_outputs=job_outputs, outcomes={"guard": "failure"})
                    self.assertEqual(set(failed.values()) - {"skipped"}, {"failure"})
                    self.assertEqual(failed["guard"], "failure")
                    cancelled = simulate(document, github, job_outputs=job_outputs, cancelled=True)
                    self.assertEqual(set(cancelled.values()), {"skipped"}, "no job starts in a cancelled run")
        document = self.documents[PACKAGED_CALLER]
        push = event("push")
        cases = {
            "the selection failed": (selection("full", "false"), {"select": "failure"},
                                     {"select": "failure", "rebuild": "skipped", "shared": "skipped"}),
            "the rebuild failed": (selection("full", "false"), {"rebuild": "failure"},
                                   {"select": "success", "rebuild": "failure", "shared": "skipped"}),
            "the rebuild was cancelled": (selection("full", "false"), {"rebuild": "cancelled"},
                                          {"select": "success", "rebuild": "cancelled", "shared": "skipped"}),
            "no mode was reported": (selection("", "true", "777"), None,
                                     {"select": "success", "rebuild": "skipped", "shared": "skipped"}),
            "an unknown mode": (selection("partial", "true", "777"), None,
                                {"select": "success", "rebuild": "skipped", "shared": "skipped"}),
            "no answer whether a Build was found": (selection("full", ""), None,
                                                    {"select": "success", "rebuild": "skipped", "shared": "skipped"}),
        }
        for label, (job_outputs, outcomes, expected) in cases.items():
            with self.subTest(case=label):
                results = simulate(document, push, job_outputs=job_outputs, outcomes=outcomes)
                self.assertEqual({name: results[name] for name in expected}, expected)

    def test_a_pull_request_never_selects_or_compiles_in_the_packaged_run(self) -> None:
        document = self.documents[PACKAGED_CALLER]
        for draft in (True, False):
            for job_outputs in (GUARDED, selection("full", "false"), selection("reuse", "true", "777")):
                with self.subTest(draft=draft, outputs=job_outputs):
                    results = simulate(document, event("pull_request_target", draft=draft), job_outputs=job_outputs)
                    self.assertEqual((results["select"], results["rebuild"]), ("skipped", "skipped"))
                    self.assertEqual(results["shared"], "skipped" if draft else "success")
                    self.assertEqual(results["deferred"], "success" if draft else "skipped")


def references(callees: tuple[str, ...], *, head: str = HEAD, kit: str = KIT_SHA) -> list[dict[str, str]]:
    listed = [{"path": f"{REPOSITORY}/{GUARD_CALLER}@{head}", "sha": head, "ref": f"refs/heads/{BRANCH}"}]
    listed.extend({"path": f"{kit_workflow(callee)}@{kit}", "sha": kit} for callee in callees)
    return listed


def run_record(producer: str, name: str, **overrides: object) -> dict[str, Any]:
    """The run of a caller that calls the guard (a producer or ``status``) as the API reports it."""

    callees = tuple(declared(producer).split())
    run: dict[str, Any] = {"id": int(RUN_ID), "path": GUARDED_CALLERS[producer], "event": name, "head_sha": HEAD,
                           "head_branch": BRANCH, "head_repository": {"full_name": REPOSITORY},
                           "referenced_workflows": references(callees)}
    if name == "pull_request_target":
        # A pull request's run reports the head of the pull request, here a fork's.
        run.update(head_sha="c" * 40, head_branch="feature/example",
                   head_repository={"full_name": "contributor/Quick-Skin-Mod"})
    run.update(overrides)
    return run


GIT_STUB_SCRIPT = (
    "if os.environ.get('STUB_GIT_FAIL') in arguments:\n"
    "    sys.stderr.write('fatal: stub failure\\n')\n"
    "    raise SystemExit(128)\n"
    "if 'rev-parse' in arguments:\n"
    "    print(os.environ['STUB_GIT_HEAD'])\n"
)
BOOTSTRAP_STUB_SCRIPT = (
    "verb = 'verify' if 'verify' in arguments else 'pin'\n"
    "value = os.environ.get('STUB_VERIFIED' if verb == 'verify' else 'STUB_PIN', '')\n"
    "if not value:\n"
    "    sys.stderr.write('mod_base_kit: error: stub refusal\\n')\n"
    "    raise SystemExit(2)\n"
    "print(value)\n"
)
GIT_ENV = ("GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL", "GIT_TERMINAL_PROMPT", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0",
           "GIT_CONFIG_VALUE_0", "GH_TOKEN")


class GuardCase(unittest.TestCase):
    STUBS: tuple[str, ...] = ("gh", "git", "python3", "sleep")

    def setUp(self) -> None:
        require_tools("bash", "jq", "base64", "grep")
        temporary = tempfile.TemporaryDirectory(prefix="ci guard ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.harness = ShellHarness(self.root / "harness", stubs=self.STUBS)
        self.script = step(documents()[GUARD_CALLER]["jobs"]["verify"]["steps"], GUARD_STEP)["run"]
        self.output = self.root / "github_output"

    def environment(self, producer: str, name: str, **env: str) -> dict[str, str]:
        environment = {
            "GH_TOKEN": TOKEN, "CALLEES": declared(producer), "MB_KIT_SHA": KIT_SHA, "MB_KIT_VERSION": VERSION,
            "GITHUB_REPOSITORY": REPOSITORY, "GITHUB_RUN_ID": RUN_ID, "GITHUB_SHA": HEAD,
            "GITHUB_REF": f"refs/heads/{BRANCH}", "GITHUB_EVENT_NAME": name,
            "GITHUB_WORKFLOW_REF": f"{REPOSITORY}/{GUARDED_CALLERS[producer]}@refs/heads/{BRANCH}",
            "GITHUB_SERVER_URL": "https://github.com", "GITHUB_OUTPUT": str(self.output),
            "STUB_GIT_SCRIPT": GIT_STUB_SCRIPT, "STUB_GIT_HEAD": HEAD,
            "STUB_PYTHON3_SCRIPT": BOOTSTRAP_STUB_SCRIPT, "STUB_PIN": f"{KIT_SHA} {VERSION}",
            "STUB_VERIFIED": f"{KIT_SHA} {VERSION}",
        }
        if name == "pull_request_target":
            environment["GITHUB_BASE_REF"] = BRANCH
        environment.update(env)
        return environment

    def fixtures(self, producer: str, name: str, **overrides: object) -> dict[str, Any]:
        fixtures: dict[str, Any] = {
            f"GET repos/{REPOSITORY}": {"body": {"default_branch": BRANCH}},
            f"GET repos/{REPOSITORY}/actions/runs/{RUN_ID}": {"body": run_record(producer, name)},
        }
        fixtures.update(overrides)
        return fixtures

    def invoke(self, producer: str = "build", name: str = "push", *, fixtures: Mapping[str, object] | None = None,
               run: Mapping[str, object] | None = None, **env: str) -> subprocess.CompletedProcess[str]:
        self.output.unlink(missing_ok=True)
        overrides = dict(fixtures or {})
        if run is not None:
            overrides[f"GET repos/{REPOSITORY}/actions/runs/{RUN_ID}"] = {"body": run_record(producer, name, **run)}
        return self.harness.run(self.script, self.environment(producer, name, **env),
                                fixtures=self.fixtures(producer, name, **overrides), record_env=GIT_ENV)

    def calls(self, tool_name: str) -> list[dict[str, Any]]:
        return [record for record in self.harness.records() if record["tool"] == tool_name]


class GuardExecutionTests(GuardCase):
    def test_a_bound_run_outputs_the_pin(self) -> None:
        credential = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
        for producer in PRODUCERS:
            for name in EVENTS:
                with self.subTest(producer=producer, event=name):
                    result = self.invoke(producer, name)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(outputs(self.output), {"kit-sha": KIT_SHA})
                    self.assertEqual(result.stdout, f"::add-mask::{credential}\n")
                    self.assertEqual([record["route"] for record in self.calls("gh")],
                                     [f"GET repos/{REPOSITORY}", f"GET repos/{REPOSITORY}/actions/runs/{RUN_ID}"])
                    self.assertTrue(all(record["token"] == TOKEN for record in self.calls("gh")))
                    bootstrap = ["-I", "-B", "mod/scripts/ci/mod_base_kit.py"]
                    self.assertEqual([record["argv"] for record in self.calls("python3")],
                                     [[*bootstrap, "pin", "--repo", "mod"],
                                      [*bootstrap, "verify", "--network", "--repo", "mod"]])
                    self.assertTrue(all(record["env"]["GH_TOKEN"] == TOKEN for record in self.calls("python3")))

    def test_the_protected_head_is_fetched_without_persisting_the_token(self) -> None:
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        credential = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
        git_calls = self.calls("git")
        safe = ["-C", "mod", "-c", "core.hooksPath=/dev/null"]
        self.assertEqual([record["argv"] for record in git_calls], [
            ["init", "--quiet", "--template=", "mod"],
            [*safe, "-c", "protocol.version=2", "fetch", "--quiet", "--depth=1", "--no-tags", "--no-recurse-submodules",
             f"https://github.com/{REPOSITORY}.git", HEAD],
            ["-C", "mod", "rev-parse", "--verify", "--quiet", "FETCH_HEAD^{commit}"],
            [*safe, "-c", "advice.detachedHead=false", "checkout", "--quiet", "--detach", "FETCH_HEAD"],
        ])
        for record in git_calls:
            with self.subTest(git=record["argv"]):
                self.assertEqual((record["env"]["GIT_CONFIG_NOSYSTEM"], record["env"]["GIT_CONFIG_GLOBAL"],
                                  record["env"]["GIT_TERMINAL_PROMPT"]), ("1", "/dev/null", "0"))
                self.assertFalse(any(TOKEN in argument or credential in argument for argument in record["argv"]),
                                 "the credential is never an argument")
                fetches = "fetch" in record["argv"]
                self.assertEqual((record["env"]["GIT_CONFIG_COUNT"], record["env"]["GIT_CONFIG_KEY_0"],
                                  record["env"]["GIT_CONFIG_VALUE_0"]),
                                 ("1", "http.https://github.com/.extraheader", f"AUTHORIZATION: basic {credential}")
                                 if fetches else (None, None, None), "only the fetch carries the credential")
        order = [record["tool"] for record in self.harness.records() if record["tool"] != "sleep"]
        self.assertEqual(order, ["gh", "gh", "git", "git", "git", "git", "python3", "python3"],
                         "the event and the run are admitted before anything is fetched or executed")

    def test_absent_kit_entries_are_tolerated_and_listed_ones_are_checked(self) -> None:
        for producer in PRODUCERS:
            callees = tuple(callee for callee in workflow.CI_CALLS[producer].values() if callee != "guard")
            accepted = {
                "only the guard": references(()),
                "the first kit workflow": references(callees[:1]),
                "reversed": list(reversed(references(callees))),
                "without ref keys": [{"path": entry["path"], "sha": entry["sha"]} for entry in references(callees)],
            }
            for label, listed in accepted.items():
                with self.subTest(producer=producer, case=label):
                    result = self.invoke(producer, "pull_request_target", run={"referenced_workflows": listed})
                    self.assertEqual((result.returncode, outputs(self.output)), (0, {"kit-sha": KIT_SHA}), result.stderr)

    def test_rejected_bindings_output_nothing(self) -> None:
        listed = references(("build",))
        guard, build = listed
        publish = {"path": f"{KIT}/.github/workflows/publish.yml@{KIT_SHA}", "sha": KIT_SHA}
        packaged = {"path": f"{kit_workflow('packaged-e2e')}@{KIT_SHA}", "sha": KIT_SHA}
        cases: dict[str, dict[str, Any]] = {
            "a pull_request event": {"name": "pull_request"},
            "a schedule": {"name": "schedule"},
            "a workflow_run": {"name": "workflow_run"},
            "an issue comment": {"name": "issue_comment"},
            "no declared callee": {"env": {"CALLEES": ""}},
            "a repeated callee": {"env": {"CALLEES": "build build"}},
            "a Pages callee": {"env": {"CALLEES": "publish"}},
            "the packaged callees declared by the Build caller": {"env": {"CALLEES": "select-build build packaged-e2e"}},
            "the packaged callees in another order": {"producer": "packaged",
                                                      "env": {"CALLEES": "build select-build packaged-e2e"}},
            "the Build callee declared by the packaged caller": {"producer": "packaged", "env": {"CALLEES": "build"}},
            # The status caller's declaration admits its own events and its own workflow alone.
            "the status callee declared by the Build caller on a push": {"env": {"CALLEES": "gate-status"}},
            "the status callee declared by the Build caller on a dispatch": {
                "name": "workflow_dispatch", "env": {"CALLEES": "gate-status"}},
            "the status callee declared by the packaged caller": {
                "producer": "packaged", "name": "pull_request_target", "env": {"CALLEES": "gate-status"}},
            "the status callee after the Build callee": {"env": {"CALLEES": "build gate-status"}},
            "a callee list with a line break": {"env": {"CALLEES": "build\nbuild"}},
            "a malformed head": {"env": {"GITHUB_SHA": "HEAD"}},
            "a malformed run id": {"env": {"GITHUB_RUN_ID": "0"}},
            "an unrendered pin": {"env": {"MB_KIT_SHA": "{{PIN}}"}},
            "another branch": {"env": {"GITHUB_REF": "refs/heads/feature"}},
            "a tag of the default branch's name": {"env": {"GITHUB_REF": f"refs/tags/{BRANCH}"}},
            "a pull request against another base": {"name": "pull_request_target", "env": {"GITHUB_BASE_REF": "release"}},
            "a pull request without a base": {"name": "pull_request_target", "env": {"GITHUB_BASE_REF": ""}},
            "the caller of another branch": {"env": {
                "GITHUB_WORKFLOW_REF": f"{REPOSITORY}/{BUILD_CALLER}@refs/heads/feature"}},
            "another workflow": {"env": {"GITHUB_WORKFLOW_REF": f"{REPOSITORY}/.github/workflows/ci.yml@refs/heads/master"}},
            "another repository's caller": {"env": {
                "GITHUB_WORKFLOW_REF": f"attacker/fork/{BUILD_CALLER}@refs/heads/{BRANCH}"}},
            "another run": {"run": {"id": int(RUN_ID) + 1}},
            "a run of another workflow": {"run": {"path": PACKAGED_CALLER}},
            "a run of another event": {"run": {"event": "workflow_dispatch"}},
            "a run at another head": {"run": {"head_sha": OTHER}},
            "a run of another branch": {"run": {"head_branch": "feature"}},
            "a run of a fork": {"run": {"head_repository": {"full_name": "attacker/fork"}}},
            "an unreadable run": {"fixtures": {f"GET repos/{REPOSITORY}/actions/runs/{RUN_ID}":
                                               {"fail": "gh: Not Found (HTTP 404)"}}},
            "a failed fetch": {"env": {"STUB_GIT_FAIL": "fetch"}},
            "a fetched commit that is not github.sha": {"env": {"STUB_GIT_HEAD": OTHER}},
            "a failed checkout": {"env": {"STUB_GIT_FAIL": "checkout"}},
            "workflows without one pin": {"env": {"STUB_PIN": ""}},
            "workflows pinned to another commit": {"env": {"STUB_PIN": f"{OTHER} {VERSION}"}},
            "workflows pinned to another version": {"env": {"STUB_PIN": f"{KIT_SHA} v1.2.4"}},
            "a pin with trailing words": {"env": {"STUB_PIN": f"{KIT_SHA} {VERSION} x"}},
            "no referenced workflows": {"run": {"referenced_workflows": []}},
            "referenced workflows that are no list": {"run": {"referenced_workflows": None}},
            "an entry that is no object": {"run": {"referenced_workflows": [guard, "x"]}},
            "the guard missing": {"run": {"referenced_workflows": [build]}},
            "the guard at another commit": {"run": {"referenced_workflows": [
                {"path": f"{REPOSITORY}/{GUARD_CALLER}@{OTHER}", "sha": OTHER}, build]}},
            "the guard by branch": {"run": {"referenced_workflows": [
                {"path": f"{REPOSITORY}/{GUARD_CALLER}@refs/heads/{BRANCH}", "sha": HEAD}, build]}},
            "the guard's path and sha disagree": {"run": {"referenced_workflows": [
                {"path": f"{REPOSITORY}/{GUARD_CALLER}@{HEAD}", "sha": OTHER}, build]}},
            "the guard of another repository": {"run": {"referenced_workflows": [
                {"path": f"attacker/fork/{GUARD_CALLER}@{HEAD}", "sha": HEAD}, build]}},
            "another local workflow": {"run": {"referenced_workflows": [
                guard, {"path": f"{REPOSITORY}/.github/workflows/other.yml@{HEAD}", "sha": HEAD}]}},
            "the kit at another commit": {"run": {"referenced_workflows": [
                guard, {"path": f"{kit_workflow('build')}@{OTHER}", "sha": OTHER}]}},
            "the kit by tag": {"run": {"referenced_workflows": [
                guard, {"path": f"{kit_workflow('build')}@{VERSION}", "sha": KIT_SHA}]}},
            "the kit's path and sha disagree": {"run": {"referenced_workflows": [
                guard, {"path": f"{kit_workflow('build')}@{KIT_SHA}", "sha": OTHER}]}},
            "a kit workflow this caller does not call": {"run": {"referenced_workflows": [guard, build, packaged]}},
            "a Pages workflow of the kit": {"run": {"referenced_workflows": [guard, build, publish]}},
            "the kit in another letter case": {"run": {"referenced_workflows": [
                guard, {"path": f"the-plum-team/MOD-BASE/.github/workflows/build.yml@{KIT_SHA}", "sha": KIT_SHA}]}},
            "a third party's workflow": {"run": {"referenced_workflows": [
                guard, build, {"path": f"attacker/kit/.github/workflows/build.yml@{KIT_SHA}", "sha": KIT_SHA}]}},
            "a repeated entry": {"run": {"referenced_workflows": [guard, build, build]}},
            "a pin that is not released": {"env": {"STUB_VERIFIED": ""}},
            "another verified pin": {"env": {"STUB_VERIFIED": f"{OTHER} {VERSION}"}},
        }
        for label, case in cases.items():
            with self.subTest(case=label):
                result = self.invoke(case.get("producer", "build"), case.get("name", "push"), run=case.get("run"),
                                     fixtures=case.get("fixtures"), **case.get("env", {}))
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(outputs(self.output), {})
                self.assertIn("Pinned mod-base: ", result.stderr)
        self.assertEqual(self.invoke().returncode, 0, "the unchanged case is accepted")

    def test_nothing_is_fetched_or_executed_for_a_run_that_is_not_admitted(self) -> None:
        refused = {
            "event": lambda: self.invoke("build", "schedule"),
            "ref": lambda: self.invoke(GITHUB_REF="refs/heads/feature"),
            "callees": lambda: self.invoke(CALLEES="publish"),
            "run": lambda: self.invoke(run={"path": PACKAGED_CALLER}),
        }
        for label, attempt in refused.items():
            with self.subTest(rejected=label):
                self.assertNotEqual(attempt().returncode, 0)
                self.assertEqual(self.calls("git") + self.calls("python3"), [])
        result = self.invoke(run={"referenced_workflows": []})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([record["argv"][3] for record in self.calls("python3")], ["pin"],
                         "the network verification is not reached once the references are refused")

    def test_only_transient_failures_are_retried_with_a_bounded_budget(self) -> None:
        route = f"GET repos/{REPOSITORY}/actions/runs/{RUN_ID}"
        transient = {"fail": "gh: Server Error (HTTP 502)"}
        result = self.invoke(fixtures={route: [transient, transient, {"body": run_record("build", "push")}]})
        self.assertEqual((result.returncode, outputs(self.output)), (0, {"kit-sha": KIT_SHA}), result.stderr)
        self.assertEqual([record["argv"] for record in self.calls("sleep")], [["5"], ["10"]])
        self.assertNotEqual(self.invoke(fixtures={route: [transient] * 10}).returncode, 0)
        self.assertEqual(sum(record.get("route") == route for record in self.harness.records()), 4)
        self.assertEqual([record["argv"] for record in self.calls("sleep")], [["5"], ["10"], ["20"]])
        forbidden = [{"fail": "gh: Forbidden (HTTP 403)"}, {"body": run_record("build", "push")}]
        self.assertNotEqual(self.invoke(fixtures={route: forbidden}).returncode, 0)
        self.assertEqual(sum(record.get("route") == route for record in self.harness.records()), 1)


REAL_BOOTSTRAP_STUB_SCRIPT = (
    "import subprocess\n"
    "# The released-commit check needs the network; everything else is the real bootstrap.\n"
    "forwarded = ['pin' if argument == 'verify' else argument for argument in arguments if argument != '--network']\n"
    "raise SystemExit(subprocess.run([sys.executable, *forwarded]).returncode)\n"
)


class GuardCheckoutTests(GuardCase):
    """The guard with the real Git and the real managed bootstrap: it fetches the commit by its id
    from a repository that holds the rendered callers and reads their single pin."""

    STUBS = ("gh", "python3", "sleep")

    def setUp(self) -> None:
        super().setUp()
        require_tools("git")
        self.home = self.root / "home"
        self.home.mkdir()
        self.work = self.root / "work"
        self.work.mkdir()
        git(self.work, "init", "--quiet", home=self.home)
        # Git reads a file:// URL literally, so the stand-in for github.com lies below a plain path.
        server = tempfile.TemporaryDirectory(prefix="ci-guard-server-")
        self.addCleanup(server.cleanup)
        self.server = Path(server.name).resolve()

    def commit(self, files: Mapping[str, str | bytes]) -> str:
        for path, data in files.items():
            target = self.work / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
        git(self.work, "add", "--all", home=self.home)
        git(self.work, "commit", "--quiet", "--allow-empty", "--message", "a protected head", home=self.home)
        head = git(self.work, "rev-parse", "HEAD", home=self.home)
        bare = self.server / f"{REPOSITORY}.git"
        shutil.rmtree(bare, ignore_errors=True)
        bare.parent.mkdir(parents=True, exist_ok=True)
        git(self.root, "clone", "--quiet", "--bare", str(self.work), str(bare), home=self.home)
        git(bare, "config", "uploadpack.allowAnySHA1InWant", "true", home=self.home)
        return head

    def mod(self, **render: str) -> dict[str, str | bytes]:
        return {**rendered(**render), "scripts/ci/mod_base_kit.py":
                (ROOT / "template/managed/scripts/ci/mod_base_kit.py").read_bytes(), "README.md": "a mod\n"}

    def guard(self, head: str, producer: str = "build", name: str = "pull_request_target",
              **env: str) -> subprocess.CompletedProcess[str]:
        shutil.rmtree(self.harness.root / "mod", ignore_errors=True)
        callees = tuple(callee for callee in workflow.CI_CALLS[producer].values() if callee != "guard")
        overrides: dict[str, object] = {"referenced_workflows": references(callees, head=head)}
        if name != "pull_request_target":
            overrides["head_sha"] = head
        return self.invoke(producer, name, run=overrides, GITHUB_SHA=head, GITHUB_SERVER_URL=f"file://{self.server}",
                           STUB_PYTHON3_SCRIPT=REAL_BOOTSTRAP_STUB_SCRIPT, **env)

    def test_the_rendered_callers_at_github_sha_bind_the_run(self) -> None:
        first = self.commit({"README.md": "before the callers\n"})
        head = self.commit(self.mod())
        self.assertNotEqual(first, head)
        for producer in PRODUCERS:
            for name in ("pull_request_target", "workflow_dispatch"):
                with self.subTest(producer=producer, event=name):
                    result = self.guard(head, producer, name)
                    self.assertEqual((result.returncode, outputs(self.output)), (0, {"kit-sha": KIT_SHA}), result.stderr)
        checkout = self.harness.root / "mod"
        self.assertEqual(git(checkout, "rev-parse", "HEAD", home=self.home), head)
        self.assertEqual(git(checkout, "status", "--porcelain", home=self.home), "")
        self.assertEqual((checkout / GUARD_CALLER).read_text(encoding="utf-8"), rendered()[GUARD_CALLER])
        credential = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
        for path in sorted(item for item in (checkout / ".git").rglob("*") if item.is_file()):
            data = path.read_bytes()
            self.assertFalse(TOKEN.encode() in data or credential.encode() in data or b"extraheader" in data,
                             f"{path.name} persists the credential")
        self.assertFalse(any((checkout / ".git/hooks").glob("*")) if (checkout / ".git/hooks").is_dir() else False,
                         "the checkout holds no hook")

    def test_an_older_protected_commit_is_fetched_by_its_id(self) -> None:
        head = self.commit(self.mod())
        self.commit({"README.md": "the branch moved on\n"})
        result = self.guard(head)
        self.assertEqual((result.returncode, outputs(self.output)), (0, {"kit-sha": KIT_SHA}), result.stderr)

    def test_a_head_whose_workflows_do_not_carry_the_guard_s_pin_is_refused(self) -> None:
        cases: dict[str, dict[str, str | bytes]] = {
            "callers of another pin": self.mod(sha=OTHER),
            "callers of another version": self.mod(version="v1.2.4"),
            "a second pin in another workflow": {**self.mod(), ".github/workflows/e2e.yml": (
                f"jobs:\n  x:\n    steps:\n      - uses: {KIT}/actions/setup@{OTHER} # {VERSION}\n")},
            "a kit reference by tag": {**self.mod(), ".github/workflows/e2e.yml": (
                f"jobs:\n  x:\n    steps:\n      - uses: {KIT}/actions/setup@{VERSION}\n")},
            "no bootstrap": {path: data for path, data in self.mod().items() if not path.startswith("scripts/")},
            "no caller": {"README.md": "a mod without workflows\n", "scripts/ci/mod_base_kit.py":
                          self.mod()["scripts/ci/mod_base_kit.py"]},
        }
        for label, files in cases.items():
            with self.subTest(case=label):
                shutil.rmtree(self.work)
                self.work.mkdir()
                git(self.work, "init", "--quiet", home=self.home)
                result = self.guard(self.commit(files))
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(outputs(self.output), {})
                self.assertIn("Pinned mod-base: ", result.stderr)

    def test_a_commit_the_repository_does_not_hold_is_refused(self) -> None:
        self.commit(self.mod())
        result = self.guard(OTHER)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot fetch the protected mod at github.sha", result.stderr)
        self.assertEqual(outputs(self.output), {})

    def test_the_token_is_sent_to_the_repository_s_server_as_a_request_header(self) -> None:
        """Git over HTTP: the credential of the fetch's environment reaches the server the
        repository lies on, as the header ``actions/checkout`` sends, and nothing else carries it."""

        seen: list[tuple[str, str | None]] = []

        class Recorder(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                seen.append((self.path, self.headers.get("Authorization")))
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *arguments: object) -> None:
                return None

        server = http.server.HTTPServer(("127.0.0.1", 0), Recorder)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 30)
        self.addCleanup(server.shutdown)
        address = f"http://127.0.0.1:{server.server_address[1]}"
        result = self.invoke("build", "push", GITHUB_SERVER_URL=address)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Pinned mod-base: cannot fetch the protected mod at github.sha", result.stderr)
        credential = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
        self.assertNotIn(credential, result.stderr)
        self.assertNotIn(TOKEN, result.stderr)
        self.assertTrue(seen, "the fetch asked the server")
        for path, authorization in seen:
            self.assertTrue(path.startswith(f"/{REPOSITORY}.git/info/refs"), path)
            self.assertEqual(authorization, f"basic {credential}")
        self.assertEqual(self.calls("python3"), [], "nothing of a mod that could not be fetched is executed")


class DeferralExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        require_tools("bash")
        temporary = tempfile.TemporaryDirectory(prefix="ci deferral ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.harness = ShellHarness(self.root / "harness")

    def test_the_deferral_is_reported_and_nothing_else_happens(self) -> None:
        for producer, path in PRODUCERS.items():
            script = step(documents()[path]["jobs"]["deferred"]["steps"], DEFERRAL_STEP)["run"]
            summary = self.root / f"summary-{producer}"
            with self.subTest(producer=producer):
                result = self.harness.run(script, {"PR_NUMBER": "17", "GITHUB_STEP_SUMMARY": str(summary)})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertRegex(result.stdout, r"^(Build|Packaged E2E) deferred: pull request #17 is a draft, so no "
                                                r"[^\n]+ Mark the pull request ready for review to start one\.\n$")
                self.assertEqual(summary.read_text(encoding="utf-8"), result.stdout)
                self.assertEqual(self.harness.records(), [], "no API call, no checkout, no kit code")
                for number in ("", "0", "017", "17; id", "1" * 19):
                    refused = self.harness.run(script, {"PR_NUMBER": number, "GITHUB_STEP_SUMMARY": str(summary)})
                    self.assertNotEqual(refused.returncode, 0, number)
                    self.assertEqual(refused.stdout, "")


class CallerLintTests(unittest.TestCase):
    def test_every_run_body_parses_and_passes_shellcheck(self) -> None:
        require_tools("bash", "shellcheck")
        for path, document in documents().items():
            for job_id, job in document["jobs"].items():
                for item in job.get("steps", []):
                    with self.subTest(path=path, job=job_id, step=item["name"]):
                        parsed = subprocess.run(["bash", "-n"], input=item["run"], capture_output=True, text=True,
                                                timeout=60)
                        self.assertEqual(parsed.returncode, 0, parsed.stderr)
                        # SC2154 is excluded as in the Pages tests: env: and the runner assign those variables.
                        checked = subprocess.run(["shellcheck", "-s", "bash", "-e", "SC2154", "-"], input=item["run"],
                                                 capture_output=True, text=True, timeout=60)
                        self.assertEqual(checked.returncode, 0, checked.stdout)

    def test_the_rendered_callers_pass_actionlint(self) -> None:
        require_tools("actionlint", "shellcheck")
        with tempfile.TemporaryDirectory(prefix="actionlint ci callers ") as temporary:
            for path, text in rendered().items():
                target = Path(temporary) / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
            result = subprocess.run([shutil.which("actionlint") or "actionlint", "-no-color", *sorted(CALLERS)],
                                    cwd=temporary, capture_output=True, text=True, timeout=300)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()

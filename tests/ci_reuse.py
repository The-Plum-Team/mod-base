"""A merged pull request with both of its original gates, beside the push that covers it.

The fixture of the post-merge reuse tests. ``tests.ci_attempt.Attempt`` supplies the push: the
fixture repository's default-branch commit, a real job state directory with its identity record
and plan, and a fake GitHub. :class:`Merged` adds what the admission reads: pull request 7, merged
as that very commit, the test merge it was tested as (another commit with the same tree and the
parents ``[base, head]``), and its newest Build and packaged runs under the pull request's head,
finished a day earlier, with literal job listings and real record archives.

Only the API is faked. Every method that changes something seeds the fake again, so a test states
its case by mutating the world and then calls the code under test.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import timedelta
from typing import Any

from mod_base.build_ci import reuse
from mod_base.build_ci.graph import run_graph, sealed_upload, upload_job_name
from mod_base.build_ci.protocol import plan_sha256
from mod_base.build_ci.records import gate_receipt, original_plan
from mod_base.build_ci.selection import producer_record
from mod_base.errors import MbError, Unavailable
from mod_base.model import grammar
from mod_base.model.canonical import canonical_json
from mod_base.workflow import find_job
from tests import ci_mod_harness as h
from tests.ci_attempt import ATTEMPT, Attempt, AttemptCase, expand, record_zip
from tests.helpers import ci_api_artifact, ci_api_run, ci_graph_jobs
from tests.helpers import h as digest

ORIGINAL_RUN = {"build": 142, "packaged": 143}
#: The parent the fixture repository gives its default-branch commit: the base the pull request was tested on.
BASE_SHA = "b" * 40
TEST_MERGE = "9" * 40
PULL = f"/repos/{h.REPOSITORY}/pulls/7"
COMMIT_PULLS = f"/repos/{h.REPOSITORY}/commits/{h.CONTROLLER_SHA}/pulls"
FIRST_PAGE = {"per_page": 100, "page": 1}
LISTING = {"build": "build-full", "packaged": "packaged-pull-request"}
MODE = {"build": "full", "packaged": "pull-request"}
#: Artifact ids of the original runs; lane ``n`` is ``LANE + n``.
BUNDLE, LANE, RESULTS, BUILD_SEAL, PACKAGED_SEAL = 500, 510, 590, 600, 601
GATE = {"build": "Shared Build / Verify complete Build",
        "packaged": "Shared Packaged E2E / Verify complete packaged E2E"}
REUSE_LISTING = {"build": "build-reuse", "packaged": "packaged-reuse"}


def original_identity(covered: dict[str, Any], **changes: Any) -> dict[str, Any]:
    """The identity pull request 7 was tested under, before it merged as the ``covered`` commit:
    the same tree and policy, another commit with the parents ``[base, head]``."""

    return {**copy.deepcopy(covered), "pr_number": 7, "head_sha": h.HEAD_SHA, "head_branch": "feature/synthetic",
            "base_sha": BASE_SHA, "controller_sha": BASE_SHA, "tested_sha": TEST_MERGE,
            "tested_parents": [BASE_SHA, h.HEAD_SHA], **changes}


def earlier(value: Any) -> Any:
    """``value`` (jobs, a run) a day before the literal listings: the originals finished first."""

    return json.loads(json.dumps(value).replace("2026-10-07T", "2026-10-06T"))


class Merged:
    """Pull request 7, merged as the commit ``attempt`` covers, with both of its gates sealed.

    ``identity`` changes fields of the original identity and ``edit`` the body of the original
    plan (the push's own plan otherwise): what the original gates were sealed for. An artifact
    whose id is in ``missing`` is described by the records but was never uploaded, or is gone."""

    def __init__(self, attempt: Attempt, *, edit=None, missing=(), **identity: Any) -> None:
        self.attempt, self.api, self.covered, self.missing = attempt, attempt.api, attempt.plan, frozenset(missing)
        body = copy.deepcopy(self.covered)
        if edit is not None:
            edit(body)
            body["plan_sha256"] = plan_sha256(body)
        self.plan = original_plan(body, original_identity(self.covered["identity"], **identity))
        then, self.merged = self.plan["identity"], self.covered["identity"]["tested_sha"]
        side = {"repo": {"full_name": h.REPOSITORY}}
        self.pull = {"number": 7, "state": "closed", "draft": False, "merged": True,
                     "merged_at": "2026-10-07T09:00:00Z", "merge_commit_sha": self.merged,
                     "updated_at": "2026-10-07T09:00:00Z",
                     "head": {"sha": then["head_sha"], "ref": then["head_branch"], **copy.deepcopy(side)},
                     "base": {"sha": self.merged, "ref": then["base_branch"], **copy.deepcopy(side)}}
        self.associated = [self.pull]
        self.seed_pull()
        self.api.add_commit(then["tested_sha"], then["tested_tree"], parents=then["tested_parents"])
        self.api.add_compare(then["base_sha"], self.merged, {"status": "ahead", "ahead_by": 2, "behind_by": 0})
        self.api.add_compare(self.merged, self.merged, {"status": "identical", "ahead_by": 0, "behind_by": 0})
        self.runs: dict[str, dict[str, Any]] = {}
        self.jobs: dict[str, list[dict[str, Any]]] = {}
        self.records: dict[int, dict[str, Any]] = {}
        self.archives: dict[int, bytes] = {}
        self.documents: dict[str, dict[str, Any]] = {}
        self.seals: dict[str, dict[str, Any]] = {}
        self.add_run("build")
        self.bundle = self.publish("build", "build", None, b"the complete Build of the pull request", BUNDLE)
        self.seal("build", gate_receipt(
            plan=self.plan, producer=self.producer("build"), gate="build", mode="full", artifacts=[self.bundle],
            owning_build=None, native_receipts=self.receipts("targets")))
        self.add_run("packaged")
        self.lanes = [self.publish("packaged", "runtime", lane["id"], b"results of " + lane["id"].encode(),
                                   LANE + index) for index, lane in enumerate(self.plan["lanes"])]
        self.results = self.publish("packaged", "results", None, b"the results index", RESULTS)
        self.seal("packaged", gate_receipt(
            plan=self.plan, producer=self.producer("packaged"), gate="packaged", mode="pull-request",
            artifacts=[*self.lanes, self.results], owning_build=self.bundle, native_receipts=self.receipts("lanes")))

    # -- the pull request ----------------------------------------------------------------------------

    def seed_pull(self) -> None:
        """Seed the pull request and what GitHub lists for the pushed commit (``associated``)."""

        self.api.add_response(PULL, self.pull)
        self.api.add_response(COMMIT_PULLS, self.associated, params=FIRST_PAGE)

    def change_pull(self, **changes: Any) -> None:
        self.pull.update(changes)
        self.seed_pull()

    # -- the original runs ---------------------------------------------------------------------------

    def producer(self, producer: str) -> dict[str, Any]:
        return producer_record(self.plan, caller=producer, run_id=ORIGINAL_RUN[producer], run_attempt=ATTEMPT,
                               event="pull_request_target",
                               graph_sha256=run_graph(producer, MODE[producer]).sha256(self.plan))

    def receipts(self, units: str) -> list[dict[str, Any]]:
        return [{"unit_id": unit["id"], "native_contract_sha256": unit["native_contract_sha256"],
                 "report_sha256": digest(unit["id"] + "-report")} for unit in self.plan[units]]

    def listing(self, producer: str, name: str | None = None) -> list[dict[str, Any]]:
        """The literal listing ``name`` (the full graph by default) as jobs of the original run."""

        jobs = earlier(expand(ci_graph_jobs(name or LISTING[producer]), self.plan))
        for job in jobs:
            job.update(run_id=ORIGINAL_RUN[producer], id=job["id"] + 7_000_000_000)
        return jobs

    def add_run(self, producer: str, listing: str | None = None, **changes: Any) -> None:
        run = earlier(ci_api_run(self.plan, producer, id=ORIGINAL_RUN[producer]))
        run.update(changes)
        self.runs[producer] = run
        self.api.add_run(run)
        self.set_jobs(producer, self.listing(producer, listing))

    def set_run(self, producer: str, **changes: Any) -> None:
        self.runs[producer].update(changes)
        self.api.add_run(self.runs[producer])

    def set_jobs(self, producer: str, jobs: list[dict[str, Any]], *, run_attempt: int = ATTEMPT) -> None:
        self.jobs[producer] = jobs
        self.api.add_jobs(ORIGINAL_RUN[producer], run_attempt, jobs)

    def job(self, producer: str, name: str) -> dict[str, Any]:
        return next(job for job in self.jobs[producer] if job["name"] == name)

    # -- their artifacts -----------------------------------------------------------------------------

    def publish(self, producer: str, kind: str, unit_id: str | None, data: bytes, artifact_id: int,
                **changes: Any) -> dict[str, Any]:
        """Seed the ``kind`` artifact of an original run, created inside the upload step of its
        job; return its canonical descriptor."""

        run_id = ORIGINAL_RUN[producer]
        started, completed = sealed_upload(find_job(self.jobs[producer], upload_job_name(producer, kind, unit_id),
                                                    run_attempt=ATTEMPT))
        expires = (grammar.parse_timestamp(started) + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        descriptor = {
            "identity": copy.deepcopy(self.plan["identity"]), "plan_sha256": self.plan["plan_sha256"],
            "profile": self.plan["profile"],
            "producer": {**self.producer(producer),
                         "upload_window": {"started_at": started, "completed_at": completed}},
            "artifact": {"id": artifact_id, "name": grammar.ci_artifact_name(kind, run_id, ATTEMPT, unit_id),
                         "digest": "sha256:" + hashlib.sha256(data).hexdigest(), "size": len(data),
                         "created_at": started, "expires_at": expires}}
        self.records[artifact_id], self.archives[artifact_id] = ci_api_artifact(descriptor, **changes), data
        if artifact_id not in self.missing:
            self.api.add_artifact(self.records[artifact_id], data)
        return descriptor

    def seal(self, gate: str, document: dict[str, Any] | None = None, *, raw: bytes | None = None,
             name: str = grammar.CI_GATE_NAME) -> None:
        """Publish the tested record of ``gate``: ``document`` (the current one by default), or the
        hostile bytes ``raw`` under the file name ``name``."""

        if document is not None:
            self.documents[gate] = document
        data = record_zip(name, canonical_json(self.documents[gate]) if raw is None else raw)
        self.seals[gate] = self.publish(gate, "tested", gate, data, BUILD_SEAL if gate == "build" else PACKAGED_SEAL)

    def set_artifact(self, artifact_id: int, **changes: Any) -> None:
        workflow_run = changes.pop("workflow_run", {})
        self.records[artifact_id].update(changes)
        self.records[artifact_id]["workflow_run"].update(workflow_run)
        self.api.add_artifact(self.records[artifact_id], self.archives[artifact_id])

    @property
    def source(self) -> dict[str, Any]:
        """The ``source`` a reference of this pull request names."""

        return {"identity": self.plan["identity"], "plan_sha256": self.plan["plan_sha256"],
                "profile": self.plan["profile"], "build_seal": self.seals["build"],
                "packaged_seal": self.seals["packaged"]}


def downloaded(calls: Any) -> list[int]:
    """The artifact ids a patched ``download`` was asked for, in order."""

    return [int(call.args[0].split("/")[-2]) for call in calls.call_args_list]


class ReuseCase(AttemptCase):
    """A push of the fixture repository and the merged pull request it came from."""

    def setUp(self) -> None:
        super().setUp()
        self.scratch = self.temporary / "scratch"
        self.scratch.mkdir()

    def world(self, *, caller: str = "build", targets: int = 1, lanes: int = 1, max_requests: int | None = None,
              **merged) -> Merged:
        attempt = self.attempt(listing=REUSE_LISTING[caller], caller=caller, push=True, targets=targets, lanes=lanes,
                               max_requests=max_requests)
        return Merged(attempt, **merged)

    def decide(self, world: Merged, **changes):
        arguments = {"plan": world.covered, "event": "push", "temporary_root": self.scratch, **changes}
        try:
            return reuse.admit_post_merge_reuse(world.api, **arguments)
        finally:
            self.assertEqual(list(self.scratch.iterdir()), [], "a record download was left behind")
            self.assertEqual(world.api.mutations, [])

    def assert_full(self, world: Merged, reason: str, *, requests: int | None = None) -> reuse.FullRunRequired:
        outcome = self.decide(world)
        self.assertIsInstance(outcome, reuse.FullRunRequired)
        self.assertEqual(outcome.reason, reason, outcome.detail)
        self.assertNotIn("\n", outcome.detail)
        if requests is not None:
            self.assertEqual(world.api.request_count, requests)
        return outcome

    def assert_stops(self, world: Merged, message: str = "") -> MbError:
        """The admission raises: neither a reuse nor permission to start a full run."""

        with self.assertRaisesRegex(MbError, message) as caught:
            self.decide(world)
        self.assertNotIsInstance(caught.exception, Unavailable)
        return caught.exception

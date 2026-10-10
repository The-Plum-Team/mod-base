"""Required hosted integration: one synthetic PR through the real workflow command chain."""

from __future__ import annotations

import json
import time
import unittest

from tests import ci_linux_worker as hosted
from tests.ci_pipeline_fixture import Pipeline


class LinuxPipelineTests(unittest.TestCase):
    HOME = hosted.LinuxLifecycleCommandTests.HOME
    setUp = hosted.LinuxLifecycleCommandTests.setUp
    cleanup = hosted.LinuxLifecycleCommandTests.cleanup

    def test_pull_request_generation(self) -> None:
        self.generation(upgrade=False)

    def test_candidate_kit_upgrade_generation(self) -> None:
        self.generation(upgrade=True)

    def generation(self, *, upgrade: bool) -> None:
        started = time.monotonic()
        pipeline = Pipeline(self, self.temporary, self.api, self.pull, upgrade=upgrade)
        pipeline.begin("build")
        planned = pipeline.job("build", "plan")
        self.assertEqual(planned.outputs["mode"], "full")
        self.assertTrue(planned.outputs["targets"])
        needs = {"plan": planned}
        pipeline.job("build", "policy", needs=needs)
        for target in json.loads(planned.outputs["targets"]):
            pipeline.job("build", "target", needs=needs, unit=target)
        pipeline.job("build", "assemble", needs=needs)
        pipeline.job("build", "gate", needs=needs)
        pipeline.complete("build")
        pipeline.begin("packaged")
        selected = pipeline.job("packaged", "input")
        needs = {"input": selected}
        for lane in json.loads(selected.outputs["lanes"]):
            pipeline.job("packaged", "lane", needs=needs, unit=lane)
        pipeline.job("packaged", "aggregate", needs=needs)
        pipeline.job("packaged", "gate", needs=needs)
        pipeline.complete("packaged")
        pipeline.begin("status")
        evaluated = pipeline.job("status", "evaluate")
        intents = json.loads(evaluated.outputs["intents"])
        self.assertEqual(intents["target_sha"], pipeline.head)
        contexts = json.loads((pipeline.mod / "scripts/ci/mod-base-build.json").read_bytes())["contexts"]
        self.assertEqual({gate: (value["context"], value["state"]) for gate, value in intents["gates"].items()},
                         {gate: (context, "success") for gate, context in contexts.items()})
        pipeline.complete("status")
        self.assertEqual(sum(count for _, _, count in pipeline.requests), 221 if upgrade else 212)
        if upgrade:
            self.assertTrue(pipeline.future_resolved)
            self.assertEqual(pipeline.plan["identity"]["kit"]["sha"], pipeline.pin)
            self.assertEqual(pipeline.plan["candidate_kit"]["sha"], pipeline.future_pin.sha)
        controls = (
            ("altered-lane", "packaged-e2e/gate", "seal-gate", pipeline.altered_lane,
             "the results index lists other lane artifacts"),
            ("missing-target", "build/assemble", "assemble", pipeline.missing_target,
             "Compile target " + pipeline.plan["targets"][0]["id"]),
            ("later-build-attempt", "packaged-e2e/gate", "seal-gate", pipeline.later_attempt,
             "selected Build is no longer the newest exact available producer"),
            ("draft", "build/plan", "subject", pipeline.draft, "is a draft: the caller must defer"),
        )
        for control in controls:
            with self.subTest(control=control[0]):
                pipeline.reject(*control)
        pipeline.report()
        print(f"pipeline: {pipeline.fences} actual host-fence root launches; {time.monotonic() - started:.2f}s")


if __name__ == "__main__":
    unittest.main()

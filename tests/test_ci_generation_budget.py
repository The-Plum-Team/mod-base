"""D14: the real profiles fit one generation; structural maxima do not promise that capacity."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from mod_base.model import limits
from tests import ci_native
from tests.ci_request_budget import COSTS, WAITING_POLLS, generation_breakdown


class GenerationBudgetTests(unittest.TestCase):
    def test_candidate_pin_upgrade_adds_one_release_admission_per_run(self) -> None:
        for targets, lanes, extra, total in ((2, 3, 1, 207), (10, 20, 0, 309), (17, 34, 1, 407)):
            with self.subTest(targets=targets):
                result = generation_breakdown(targets=targets, lanes=lanes, extra_inputs=extra, candidate_upgrade=True)
                self.assertEqual(sum(result.values()), total)
                self.assertLessEqual(total, limits.MAX_CI_GENERATION_REQUESTS)

    def test_every_nonzero_route_names_the_test_that_pins_it(self) -> None:
        for route, cost in COSTS.items():
            module, owner, method = cost.proof.split(".")
            path = Path(__file__).parent / f"{module}.py"
            tree = ast.parse(path.read_text(encoding="utf-8"))
            classes = {item.name: item for item in tree.body if isinstance(item, ast.ClassDef)}
            self.assertIn(owner, classes, route)
            self.assertIn(method, {item.name for item in classes[owner].body if isinstance(item, ast.FunctionDef)}, route)

    def test_the_real_mod_generations_fit_the_pinned_budget(self) -> None:
        totals = {"block-pops": 300, "quick-skin": 398}
        for profile in ci_native.profiles():
            native = ci_native.load(profile, "targets.json")
            breakdown = generation_breakdown(targets=len(native["targets"]), lanes=len(native["lanes"]),
                                               extra_inputs=int(profile == "quick-skin"))
            with self.subTest(profile=profile):
                self.assertEqual(sum(breakdown.values()), totals[profile], breakdown)
                self.assertLessEqual(sum(breakdown.values()), limits.MAX_CI_GENERATION_REQUESTS, breakdown)
                self.assertLess(limits.MAX_CI_GENERATION_REQUESTS, 600)

    def test_the_structural_maximum_is_not_a_generation_capacity_promise(self) -> None:
        # D14 protects the two enrolled profiles. Keeping their native structural bounds does
        # not authorize plans this large: even before additional pagination, they exceed the
        # token's 1000-request hourly budget. No runtime admission was added for this migration.
        breakdown = generation_breakdown(targets=limits.MAX_CI_TARGETS, lanes=limits.MAX_CI_LANES,
                                           extra_inputs=limits.MAX_CI_PLAN_INPUTS)
        self.assertEqual(sum(breakdown.values()), 2274, breakdown)
        self.assertGreater(sum(breakdown.values()), 1000, breakdown)
        self.assertEqual(WAITING_POLLS, 0)

    def test_even_the_last_successful_wait_poll_fits_d14_for_the_larger_profile(self) -> None:
        # Ninety observations fit in 5400 seconds; the final one returns the Build. The first
        # 89 each add a listing to the complete-Build cost. The command's deadline test also
        # covers exhausting the wait without a Build, including a cached draft deferral.
        breakdown = generation_breakdown(targets=17, lanes=34, extra_inputs=1,
                                           polls=limits.MAX_CI_BUILD_POLLS - 2)
        self.assertEqual(sum(breakdown.values()), 487, breakdown)
        self.assertLess(sum(breakdown.values()), 600, breakdown)


if __name__ == "__main__":
    unittest.main()

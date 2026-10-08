"""A lane's sealed results at the file cap of a lane export, read by the aggregating job.

A lane may hold ``limits.MAX_CI_RUNTIME_FILES`` files (``runtime_schema.validate_runtime_envelope``).
Its job uploads them with the runtime envelope and, beside it, the validation record of
``verify_runtime`` and that record's one report (``validation.materialize_validated_export``):
three files more than the lane's own. ``ci aggregate`` reads exactly that artifact
(``transport._read_sealed_lane``), so its extraction has to admit what the writer may upload.
"""

from __future__ import annotations

import copy
import io
import unittest
import zipfile
from typing import Any

from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json, sha256_hex
from tests.ci_attempt import runtime_input_sha256, sealed, validation
from tests.test_ci_aggregate import AggregateCase


def lane_results(plan: dict[str, Any], producer: dict[str, Any], owning_build: dict[str, Any], lane_id: str,
                 count: int) -> tuple[bytes, dict[str, Any]]:
    """A lane results ZIP as the lane job freezes it, with ``count`` native reports and the
    canonical runtime envelope that inventories them."""

    lane = next(lane for lane in plan["lanes"] if lane["id"] == lane_id)
    contents = {f"lanes/{lane_id}/report-{index:03d}.json": canonical_json({"lane": lane_id, "report": index})
                for index in range(count)}
    envelope = {"kind": "mod-base.ci.runtime-envelope", "schema_version": 1,
                "identity": copy.deepcopy(plan["identity"]), "plan_sha256": plan["plan_sha256"],
                "profile": plan["profile"],
                "producer": {key: value for key, value in producer.items() if key != "upload_window"},
                "scope": "lane", "lane_id": lane_id, "owning_build": copy.deepcopy(owning_build),
                "lanes": [{"id": lane_id, "native_contract_sha256": lane["native_contract_sha256"]}],
                "files": [{"path": name, "lane_id": lane_id, "role": "native-report", "size": len(data),
                           "sha256": sha256_hex(data)} for name, data in sorted(contents.items())]}
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as package:
        for name, data in contents.items():
            package.writestr(name, data)
        package.writestr(grammar.CI_RUNTIME_ENVELOPE_NAME, canonical_json(envelope))
    return stream.getvalue(), envelope


class SealedLaneCapTests(AggregateCase):
    def lane_of(self, count: int):
        """A packaged run whose one lane uploaded ``count`` files, sealed with its validation record."""

        attempt = self.world()
        lane = attempt.plan["lanes"][0]["id"]
        data, envelope = lane_results(attempt.plan, attempt.producer(attempt.mode), attempt.owning, lane, count)
        document, files = validation(
            attempt.plan, hook="verify_runtime", unit_id=lane, run_id=attempt.run_id,
            input_sha256=runtime_input_sha256(attempt.plan, attempt.build_sha256, envelope))
        attempt.publish("runtime", lane, sealed(data, document, files), artifact_id=300)
        return attempt

    def test_a_lane_two_files_below_the_cap_is_indexed(self) -> None:
        code, stdout, _ = self.aggregate(self.lane_of(limits.MAX_CI_RUNTIME_FILES - 2))
        self.assertEqual(code, 0, stdout)

    # ``bounded_zip.extract_runtime`` admits the lane's files and one more, the envelope; the
    # artifact also holds the validation record and its report, so the two largest lanes a plan
    # may have (511 and 512 files) cannot be read back.
    @unittest.expectedFailure
    def test_a_lane_with_every_file_a_lane_may_hold_is_indexed(self) -> None:
        code, stdout, _ = self.aggregate(self.lane_of(limits.MAX_CI_RUNTIME_FILES))
        self.assertEqual(code, 0, stdout)


if __name__ == "__main__":
    unittest.main()

"""The mods' real file names through plan, envelope, assembly, verification, the sealed copy and
the archive.

Real temporary trees, no seams: every step runs the code a job runs, on the names Block Pops and
Quick Skin stage today (``files/Quick Skin - Fabric - 1.21.4-1.0.0.jar``).
"""

from __future__ import annotations

import copy
import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from mod_base.build_ci import archive, exports
from mod_base.build_ci.protocol import plan_sha256, validate_plan
from mod_base.build_ci.records import validate_build_envelope
from mod_base.errors import MbError
from mod_base.io import bounded_zip
from mod_base.model import grammar
from mod_base.model.canonical import canonical_json
from tests.helpers import ci_descriptor, ci_envelope, ci_plan

#: What Block Pops and Quick Skin stage today, one lane each.
REAL_OUTPUTS = {
    "lane-a": {"production": "BlockPops - Fabric - 1.20.1-1.2.3.jar", "harness": "BlockPops E2E - Fabric - 1.20.1-0.0.0.jar",
               "sbom": "sbom/block-pops.cdx.json", "native-report": "reports/1.20.1-fabric.json"},
    "lane-b": {"production": "Quick Skin - Fabric - 1.21.4-1.0.0.jar", "harness": "Quick Skin E2E - Fabric - 1.21.4-0.0.0.jar",
               "sbom": "sbom/quick-skin.cdx.json", "native-report": "reports/1.21.4-fabric.json"},
}


def real_plan_and_partitions(outputs=REAL_OUTPUTS):
    plan = ci_plan()
    target, lane = plan["targets"][0], plan["lanes"][0]
    plan["targets"], plan["lanes"] = [], []
    for suffix in ("a", "b"):
        roles = outputs[f"lane-{suffix}"]
        plan["targets"].append({**copy.deepcopy(target), "id": f"target-{suffix}", "outputs": [
            {"path": roles[role], "lane_id": f"lane-{suffix}", "role": role}
            for role in ("production", "harness", "sbom", "native-report")]})
        plan["lanes"].append({**copy.deepcopy(lane), "id": f"lane-{suffix}", "target_id": f"target-{suffix}"})
    plan["plan_sha256"] = plan_sha256(plan)
    partitions = []
    for index, target in enumerate(plan["targets"]):
        envelope = ci_envelope()
        envelope.update(plan_sha256=plan["plan_sha256"], scope="target", target_id=target["id"])
        envelope["files"] = sorted(({**output, "size": 1, "sha256": "0" * 64} for output in target["outputs"]),
                                   key=lambda file: file["path"])
        envelope["native_reports"] = [file["path"] for file in envelope["files"] if file["role"] == "native-report"]
        descriptor = ci_descriptor("target", unit_id=target["id"], artifact_id=100 + index)
        descriptor["plan_sha256"] = plan["plan_sha256"]
        partitions.append({"descriptor": descriptor, "envelope": envelope})
    return plan, partitions


class RealNameExportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()

    def freeze(self, root, envelope):
        payloads = {}
        for file in envelope["files"]:
            data = f"bytes of {file['path']}".encode()
            file.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
            payloads[file["path"]] = data
            (root / file["path"]).parent.mkdir(parents=True, exist_ok=True)
            (root / file["path"]).write_bytes(data)
        (root / grammar.CI_ENVELOPE_NAME).write_bytes(canonical_json(envelope))
        return payloads

    def assembled(self):
        """The complete export of both targets, assembled from their frozen partitions."""

        plan, partitions = real_plan_and_partitions()
        validate_plan(plan)
        payloads = {}
        for index, partition in enumerate(partitions):
            payloads.update(self.freeze(self.base / "inputs" / f"target-{index}", partition["envelope"]))
            validate_build_envelope(partition["envelope"], plan=plan)
        complete = exports.assemble_build_export(self.base / "inputs", partitions=partitions, plan=plan,
                                                 run_id=42, run_attempt=2, output=self.base / "complete")
        return plan, payloads, complete

    def test_real_names_are_planned_assembled_verified_and_sealed_unchanged(self):
        plan, payloads, complete = self.assembled()
        self.assertEqual(sum(" " in name for name in payloads), 4)
        self.assertEqual([file["path"] for file in complete["files"]], sorted(payloads))
        self.assertEqual(exports.verify_build_export(self.base / "complete", plan=plan), complete)
        self.assertEqual(exports.materialize_build_export(self.base / "complete", self.base / "sealed", plan=plan), complete)
        self.assertEqual(exports.verify_build_export(self.base / "sealed", plan=plan), complete)
        for name, data in payloads.items():
            self.assertEqual((self.base / "sealed" / name).read_bytes(), data)
            self.assertNotEqual(os.stat(self.base / "sealed" / name).st_ino, os.stat(self.base / "complete" / name).st_ino)

    def test_real_names_are_archived_and_extracted_unchanged(self):
        plan, payloads, complete = self.assembled()
        metadata = archive.encode_build_export(self.base / "complete", self.base / "encoded", plan=plan)
        encoded = self.base / "encoded" / metadata["path"]
        self.assertEqual((encoded.stat().st_size, hashlib.sha256(encoded.read_bytes()).hexdigest()),
                         (metadata["size"], metadata["sha256"]))
        names = bounded_zip.extract_build(encoded, self.base / "decoded")
        self.assertEqual(names, sorted([*payloads, grammar.CI_ENVELOPE_NAME]))
        self.assertEqual(exports.verify_build_export(self.base / "decoded", plan=plan), complete)
        for name, data in payloads.items():
            self.assertEqual((self.base / "decoded" / name).read_bytes(), data)
        # The same archive is no Pages bundle: that extraction keeps the narrow bundle grammar.
        with self.assertRaisesRegex(bounded_zip.ZipRejected, "unsafe entry name"):
            bounded_zip.extract(encoded, self.base / "pages", bounded_zip.ExtractionLimits(16, 1 << 20, 1 << 20))
        self.assertFalse((self.base / "pages").exists())

    def test_hostile_planned_names_never_validate(self):
        for name in (" leading.jar", "trailing.jar ", ".hidden.jar", "trailing.", "double  space.jar", "tab\tname.jar",
                     "back\\slash.jar", "colon:name.jar", "unicod\xe9.jar", "../outside.jar", "/absolute.jar",
                     "BLOCKPOPS E2E - FABRIC - 1.20.1-0.0.0.JAR", "Sbom/other.json"):
            with self.subTest(name=name):
                outputs = copy.deepcopy(REAL_OUTPUTS)
                outputs["lane-a"]["production"] = name
                with self.assertRaises(MbError):
                    validate_plan(real_plan_and_partitions(outputs)[0])


if __name__ == "__main__":
    unittest.main()

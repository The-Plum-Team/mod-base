"""Every Build/E2E bound, pinned to its literal value and to the bound it preserves.

``docs/BUILD-E2E-DESIGN.md`` ("Build bundle handoff") keeps the consumers' native bounds and forbids
enlarging one because a pipeline fails. So every bound of the shared pipeline is one of four things,
and each table below says which: a mod's own bound (``NATIVE``), a fact of GitHub or Linux
(``PLATFORM``), a decision of the kit (``KIT``) or a value computed from the others (``DERIVED``).
A new bound needs a row here before it can ship, and a bound no kit code names is dead.

Native sources are ``path:line`` at the reviewed commits: ``BP`` is Block Pops
``47a890ae46a2878fb08d29a932803ab91bccdcd9``, ``QS`` is Quick Skin
``c0cdc01ab20f1eac663c628011520c76fc7e3d7a``. Three native bounds have no kit constant, because the
kit has nothing to count:

* at most 16 export roots per seal (``BP:scripts/ci/untrusted_runner.py:45``): a kit job seals
  exactly one export tree, the fixed ``candidate-home/export``;
* 8192 entries per JAR, 1 GiB expanded, 32 nested archives to depth 4
  (``BP:scripts/release/artifact_manifest.py:42-45``): the kit bounds a JAR as one export file
  (``MAX_CI_JAR_BYTES``) and never opens it; these stay with the mod's protected verifier;
* the native 512 MiB fan-in budget is ``MAX_CI_RUNTIME_AGGREGATE_BYTES``, not a second constant.

``ScopeTest`` states the two scope decisions as behaviour, and ``MeasuredTest`` holds every bound
against the sizes of real artifacts (``tests/fixtures/ci_native/*/measured.json``).
"""

from __future__ import annotations

import ast
import copy
import re
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mod_base.build_ci import records, runtime_schema
from mod_base.errors import MbError
from mod_base.model import grammar, limits
from mod_base.model.canonical import strict_loads
from tests import ci_native, helpers

ROOT = Path(__file__).resolve().parents[1]
LIMITS = Path(limits.__file__).resolve()
DOCUMENTS = ROOT / "tests" / "fixtures" / "documents" / "valid"
ADR = ROOT / "docs" / "adr" / "0007-protected-build-and-packaged-runtime.md"
KIB, MIB, GIB = 1024, 1024 * 1024, 1024 * 1024 * 1024

#: A mod's own bound, preserved: name -> (value, where the mod defines it).
NATIVE: dict[str, tuple[Any, str]] = {
    # Block Pops' sandbox: one sealed export tree, the worker's environment and its shutdown.
    "MAX_CI_EXPORT_FILES": (10_000, "BP:scripts/ci/untrusted_runner.py:46 MAX_EXPORT_FILES"),
    "MAX_CI_EXPORT_ENTRIES": (20_000, "BP:scripts/ci/untrusted_runner.py:47 MAX_EXPORT_ENTRIES"),
    "MAX_CI_EXPORT_FILE_BYTES": (1 * GIB, "BP:scripts/ci/untrusted_runner.py:48 MAX_EXPORT_FILE_BYTES"),
    "MAX_CI_EXPORT_TREE_BYTES": (2 * GIB, "BP:scripts/ci/untrusted_runner.py:49 MAX_EXPORT_TOTAL_BYTES"),
    "MAX_CI_LOG_BYTES": (16 * MIB, "BP:scripts/ci/untrusted_runner.py:50 MAX_LOG_BYTES; "
                                   "BP:e2e/packaged_runtime.py:182 and :185; QS:e2e/packaged_runtime.py:203 and :206"),
    "MAX_CI_ENV_BYTES": (256 * KIB, "BP:scripts/ci/untrusted_runner.py:44 MAX_ENV_BYTES"),
    "CI_TERMINATION_GRACE_SECONDS": (15.0, "BP:scripts/ci/untrusted_runner.py:42; normal termination and each "
                                          "mandatory emergency sweep after an exhausted failed phase"),
    "CI_TERMINATION_POLL_SECONDS": (0.25, "BP:scripts/ci/untrusted_runner.py:718"),
    # Its Gradle cache seed, which is also the largest tree the kit copies for a worker.
    "MAX_CI_SOURCE_ENTRIES": (250_000, "BP:scripts/ci/untrusted_runner.py:491"),
    "MAX_CI_SOURCE_FILES": (200_000, "BP:scripts/ci/untrusted_runner.py:492"),
    "MAX_CI_SOURCE_FILE_BYTES": (2 * GIB, "BP:scripts/ci/untrusted_runner.py:493"),
    "MAX_CI_SOURCE_TREE_BYTES": (20 * GIB, "BP:scripts/ci/untrusted_runner.py:494"),
    # Its native Build verification.
    "MAX_CI_JAR_BYTES": (256 * MIB, "BP:scripts/release/artifact_manifest.py:41 MAX_JAR_BYTES"),
    "MAX_CI_BUILD_REPORT_BYTES_BY_PROFILE": (
        {"quick-skin": 4 * MIB, "block-pops": 8 * MIB},
        "block-pops: BP:scripts/release/build_evidence.py:21 MAX_REPORT_BYTES. quick-skin: no native bound of "
        "this value exists; its manifest reader allows 16 MiB (QS:scripts/release/artifact_manifest.py:15), so "
        "4 MiB is the kit's tighter choice. Measured: a 30.1 KiB manifest; Block Pops a 28.7 KiB manifest and a "
        "1.28 MiB build report"),
    "MAX_CI_SBOM_BYTES": (16 * MIB, "QS:scripts/release/generate_sbom.py:31 (measured: 46.5 KiB). Block Pops stages "
                                    "no SBOM, so the bound is one constant and not a value per profile"),
    # Runtime evidence. Both mods apply the first two to each evidence profile (one scenario of a
    # lane, BP:e2e/packaged_runtime.py:2068 export_profile_evidence); the kit to a whole lane.
    "MAX_CI_RUNTIME_FILES": (512, "BP:e2e/packaged_runtime.py:180 MAX_EVIDENCE_FILES; QS:e2e/packaged_runtime.py:201"),
    "MAX_CI_RUNTIME_BYTES": (256 * MIB, "BP:e2e/packaged_runtime.py:181 MAX_EVIDENCE_TOTAL_BYTES; "
                                        "QS:e2e/packaged_runtime.py:202"),
    "MAX_CI_REPORT_BYTES": (4 * MIB, "BP:e2e/packaged_runtime.py:183; BP:scripts/ci/e2e_fanin.py:76; "
                                     "QS:e2e/packaged_runtime.py:204"),
    "MAX_CI_PNG_BYTES": (32 * MIB, "BP:e2e/packaged_runtime.py:184; BP:scripts/ci/e2e_fanin.py:79; "
                                   "QS:e2e/packaged_runtime.py:205"),
    # Block Pops' fan-in: any evidence root it inventories, a lane as well as the aggregate.
    "MAX_CI_RUNTIME_AGGREGATE_FILES": (4096, "BP:scripts/ci/e2e_fanin.py:80 MAX_FILES"),
    "MAX_CI_RUNTIME_AGGREGATE_BYTES": (512 * MIB, "BP:scripts/ci/e2e_fanin.py:81 MAX_TOTAL_BYTES"),
    # Quick Skin's bundle handoff, batches and retention.
    "MAX_CI_BUNDLE_COMPRESSED_BYTES": (512 * MIB, "QS:scripts/ci/staged_build_bundle.py:25 MAX_BUNDLE_BYTES; "
                                                  "QS:scripts/ci/ci_reuse.py:35"),
    "CI_BUILD_WAIT_SECONDS": (5400, "QS:scripts/ci/staged_build_bundle.py:96; "
                                    "QS:.github/workflows/on-demand-e2e.yml:379"),
    "MAX_CI_BATCH_MEMBERS": (50, "QS:scripts/ci/pr_batch.py:29 MAX_PULLS"),
    "CI_RETENTION_DAYS": (
        {"target": 1, "build": 7, "runtime": 7, "results": 7, "tested": 90, "reuse": 90},
        "QS:.github/workflows/build-matrix.yml:126 (1), build-gate.yml:378 (7) and :391 (90), "
        "on-demand-e2e.yml:452 (7) and :581 (90). Block Pops keeps every artifact one day "
        "(BP:.github/workflows/build-gate.yml:532); the design changes that explicitly"),
}

#: A fact of the platform the pipeline runs on: name -> (value, the fact).
PLATFORM: dict[str, tuple[Any, str]] = {
    "MAX_CI_TARGETS": (256, "GitHub: one job matrix expands to at most 256 jobs"),
    "MAX_CI_LANES": (256, "GitHub: one job matrix expands to at most 256 jobs"),
    "MAX_CI_BUILD_RUNS": (1000, "GitHub: a filtered run search lists at most 1,000 runs"),
    "MAX_CI_STATUS_DESCRIPTION_CHARS": (140, "GitHub: the description of a commit status holds at most 140 "
                                             "characters; a status intent carries one"),
    "MAX_CI_BATCH_DOCUMENT_BYTES": (64 * KIB, "GitHub: a pull request body holds at most 65,536 characters; a batch "
                                              "manifest travels in the body of its pull request"),
    "MAX_CI_BATCH_TITLE_CHARS": (256, "GitHub: a pull request title holds at most 256 characters; a member's title "
                                      "is the subject of its squash commit"),
    "MAX_CI_WORKER_TIMEOUT_SECONDS": (6 * 60 * 60, "GitHub: a hosted job runs at most 360 minutes; a mod's "
                                                   "native timeouts are its own Build configuration"),
    "MAX_CI_ENV_VALUE_BYTES": (128 * KIB, "Linux MAX_ARG_STRLEN: one environment string"),
    "MAX_CI_TOOL_PATH_BYTES": (4 * KIB, "Linux PATH_MAX"),
    "MAX_CI_SOURCE_LINK_BYTES": (4 * KIB, "Linux PATH_MAX: the longest symbolic link target"),
    "MAX_CI_TOOL_SYMLINK_HOPS": (40, "Linux MAXSYMLINKS"),
    "MAX_CI_FILE_ID": (2**64 - 1, "Linux: device and inode numbers are unsigned 64-bit"),
    "MAX_CI_UNIX_ID": (2**32 - 2, "Linux: uid_t and gid_t without the all-ones value that means 'no change'"),
    "MIN_CI_WORKER_UID": (1000, "Linux UID_MIN: the first id that is not a system account"),
}

#: A decision of the kit: name -> (value, what it bounds).
KIT: dict[str, tuple[Any, str]] = {
    "MAX_CI_PLAN_BYTES": (4 * MIB, "one canonical plan"),
    "MAX_CI_PLAN_SOURCE_BYTES": (4 * MIB, "one candidate file a plan is derived from, and one derive hook output"),
    "MAX_CI_PLAN_INPUTS": (8, "candidate files a protected Build configuration may name for plan derivation beside "
                              "the inventory and the scenario contract; Quick Skin needs one (gradle.properties)"),
    "MAX_CI_IDENTITY_BYTES": (16 * KIB, "the private subject record of one job"),
    "CI_ARTIFACT_UPLOAD_SKEW_SECONDS": (2, "artifact-service creation clock versus runner upload-step clock only"),
    "CI_TEST_MERGE_WAIT_SECONDS": (15, "maximum wait for GitHub to compute a pending PR test merge"),
    "CI_TEST_MERGE_POLL_SECONDS": (5, "interval between observations of a pending PR test merge"),
    "MAX_CI_SUBJECT_REQUESTS": (16, "API requests one subject authentication may spend"),
    "MAX_CI_DERIVED_SUBJECT_REQUESTS": (4, "API requests one subject derivation in a job that holds the candidate "
                                           "checkout may spend: its one request and the retries of that request"),
    "MAX_CI_GATE_REQUESTS": (96, "API requests one `ci seal-gate` may spend: 15 for a Build gate, 23 for the "
                                 "packaged gate of a pull request and 47 for the gate of a reuse run, whatever the "
                                 "plan's size, and room for retries and further listing pages"),
    "MAX_CI_COMMIT_PULLS": (100, "pull requests GitHub associates with one pushed commit: the one page "
                                 "post-merge reuse reads to find the merge"),
    "MAX_CI_REUSE_ADMIT_REQUESTS": (96, "API requests one `ci reuse-admit` may spend: 38 for an admitted reuse, 1 "
                                        "for a direct push and 11 when the merged tree differs, whatever the plan's "
                                        "size, and room for retries and further listing pages"),
    "MAX_CI_STATUS_CONTEXT_CHARS": (100, "one status context string of the protected Build configuration"),
    "MAX_CI_WORKER_RECORD_BYTES": (256 * KIB, "the private worker record of one job"),
    "MAX_CI_PLAN_REQUESTS": (24, "API requests one plan derivation and initial changed-release admission may spend"),
    "CI_GIT_READ_TIMEOUT_SECONDS": (60.0, "one object read from a checkout of the job"),
    "MAX_CI_GIT_ANSWER_BYTES": (4 * KIB, "one object id or tree entry read from a checkout of the job"),
    "MAX_CI_GIT_COMMIT_BYTES": (1 * MIB, "one commit object read whole from a checkout of the job: header, "
                                         "signature and message"),
    "MAX_CI_FETCH_BUILD_REQUESTS": (8, "requests one `ci fetch-build` may spend: one REST redirect and one "
                                       "credential-free storage GET, plus retries"),
    "MAX_CI_GENERATION_REQUESTS": (440, "D14 regression cap for one complete generation of either native profile; "
                                       "test_ci_generation_budget enforces it, without adding runtime admission"),
    "MAX_CI_GATE_STATUS_REQUESTS": (96, "API requests one `ci gate-status` may spend: 45 with both runs complete, "
                                        "the rest for retries and further listing pages"),
    "MAX_CI_PRIVATE_RECORD_ENTRIES": (2, "a private record directory and its one file"),
    "MAX_CI_POLICY_TESTS": (100_000, "tests one policy suite may discover"),
    "MAX_CI_POLICY_WORKERS": (256, "worker processes of the policy runner"),
    "MAX_CI_ENVELOPE_BYTES": (4 * MIB, "one Build envelope"),
    "MAX_CI_RECORD_BYTES": (4 * MIB, "one selection, gate, reuse or validation record and its artifact"),
    "MAX_CI_CONFIG_BYTES": (1 * MIB, "the protected Build configuration"),
    "MAX_CI_ACTIVATION_BYTES": (8 * KIB, "the profile activation manifest"),
    "MAX_CI_ADAPTER_FILE_BYTES": (4 * MIB, "one file of the adapter's import closure"),
    "MAX_CI_ADAPTER_TREE_BYTES": (64 * MIB, "the whole closure"),
    "MAX_CI_ADAPTER_FILES": (256, "its file count"),
    "MAX_CI_CONTROL_OUTPUT_BYTES": (16 * KIB, "output of one account-control command"),
    "MAX_CI_TOOL_ROOTS": (16, "tool roots scanned for one worker"),
    "MAX_CI_TOOL_TREE_DEPTH": (64, "directory depth of a tool, overlay or Git metadata walk"),
    "MAX_CI_KIT_INSTALL_ENTRIES": (40_000, "entries of the kit overlay staged for a worker"),
    "MAX_CI_KIT_INSTALL_FILES": (20_000, "its files"),
    "MAX_CI_KIT_INSTALL_BYTES": (512 * MIB, "its bytes"),
    "MAX_CI_COMMAND_ARGUMENTS": (256, "arguments of one hook command"),
    "MAX_CI_COMMAND_BYTES": (256 * KIB, "their bytes"),
    "CI_PROCESS_READ_BYTES": (64 * KIB, "one read from a worker's output pipe"),
    "CI_ROOT_OPERATION_TIMEOUT_SECONDS": (1800.0, "one root operation of the privileged bootstrap"),
    "MAX_CI_ROOT_DIAGNOSTIC_BYTES": (4 * KIB, "the stderr a failed root operation may report"),
    "CI_HOST_FENCE_TIMEOUT_SECONDS": (600.0, "one administrative command of the host fence"),
    "MAX_CI_HOST_FENCE_REPORT_BYTES": (64 * KIB, "what the fence keeps of the entries it could not close"),
    "MAX_CI_HOST_MOUNTINFO_BYTES": (64 * KIB, "kernel mount table admitted before unused SDK trees are closed"),
    "CI_SYSTEM_PROFILE_TIMEOUT_SECONDS": (600, "each of the two package-manager commands of `ci system-profile` "
                                               "(root's `timeout`); the natives' install steps have only the job's"),
    "MAX_CI_SYSTEM_PROFILE_LOG_BYTES": (64 * KIB, "the last output of one of them the command keeps and shows"),
    "CI_SYSTEM_PROFILE_LOCK_WAIT_SECONDS": (120, "apt's wait for a lock another apt or dpkg holds "
                                                 "(`DPkg::Lock::Timeout`), inside the command's bound"),
    "CI_SYSTEM_PROFILE_FETCH_RETRIES": (3, "apt's retries of one failed download (`Acquire::Retries`)"),
    "MAX_CI_SEED_PATH_DEPTH": (64, "components of one path inside a Gradle seed, the tree walk's own depth"),
    "MAX_CI_SOURCE_LIST_BYTES": (64 * MIB, "one Git tree listing"),
    "MAX_CI_GIT_REF_BYTES": (4 * KIB, "one loose ref"),
    "MAX_CI_GIT_REF_LIST_BYTES": (64 * MIB, "the packed and shallow ref lists"),
    "MAX_CI_OUTPUTS_PER_TARGET": (1024, "planned outputs of one target"),
    "MAX_CI_OBLIGATIONS_PER_LANE": (1024, "planned obligations of one lane"),
    "CI_BUILD_POLL_SECONDS": (60, "pause between two reads of a pending Build; Quick Skin polls every 30 s "
                                  "(QS:scripts/ci/staged_build_bundle.py:131), the kit spends fewer API requests"),
    "MAX_CI_BATCH_PUSH_RECEIPT_BYTES": (4 * KIB, "the porcelain answer of one batch push"),
    "MAX_CI_BATCH_ALLOWED_PATHS": (4096, "entries of the allowed-path list a batch is built with"),
    "MAX_CI_BATCH_REPORTED_PATHS": (20, "paths one batch refusal names"),
    "CI_BATCH_GIT_TIMEOUT_SECONDS": (120, "one Git plumbing call of the batch store"),
    "CI_BATCH_GIT_TRANSFER_TIMEOUT_SECONDS": (600, "one fetch or push of the batch store"),
    "MAX_CI_TARGET_DOWNLOAD_BYTES": (4 * GIB, "all target partition archives of one fan-in; the assembled "
                                              "tree keeps MAX_CI_EXPORT_TREE_BYTES"),
    "MAX_CI_RUNTIME_ENVELOPE_BYTES": (4 * MIB, "one runtime envelope"),
}

#: Computed from other bounds: name -> (value, the formula in words, the formula).
DERIVED: dict[str, tuple[Any, str, Callable[[], Any]]] = {
    "MAX_CI_TEST_MERGE_POLLS": (4, "first observation plus one per interval of the test-merge wait",
                                 lambda: limits.CI_TEST_MERGE_WAIT_SECONDS // limits.CI_TEST_MERGE_POLL_SECONDS + 1),
    "MAX_CI_ARTIFACTS_PER_GATE": (1000, "at most one artifact per job", lambda: limits.MAX_JOBS_PER_ATTEMPT),
    "MAX_CI_PLAN_INPUT_FILES": (11, "the validator's input tree: the plan, the inventory, the scenario contract "
                                    "and every extra plan input", lambda: 3 + limits.MAX_CI_PLAN_INPUTS),
    "MAX_CI_PLAN_INPUT_ENTRIES": (12, "the files of the validator's input tree and its directory",
                                  lambda: limits.MAX_CI_PLAN_INPUT_FILES + 1),
    "MAX_CI_PLAN_INPUT_BYTES": (44 * MIB, "the plan and every candidate file it is derived from",
                                lambda: (limits.MAX_CI_PLAN_BYTES
                                         + (2 + limits.MAX_CI_PLAN_INPUTS) * limits.MAX_CI_PLAN_SOURCE_BYTES)),
    "MAX_CI_ROOT_REQUEST_BYTES": (
        153_092_096, "a tested-tree inventory as JSON rows (twice its Git listing), a plan, the owning "
                     "Build envelope, one runtime envelope, one record and 2 MiB of metadata",
        lambda: (2 * limits.MAX_CI_SOURCE_LIST_BYTES + limits.MAX_CI_PLAN_BYTES + limits.MAX_CI_ENVELOPE_BYTES
                 + limits.MAX_CI_RUNTIME_ENVELOPE_BYTES + limits.MAX_CI_RECORD_BYTES + 2 * MIB)),
    "MAX_CI_GIT_METADATA_FILES": (200_000, "the source tree bound", lambda: limits.MAX_CI_SOURCE_FILES),
    "MAX_CI_GIT_METADATA_ENTRIES": (250_000, "the source tree bound", lambda: limits.MAX_CI_SOURCE_ENTRIES),
    "MAX_CI_GIT_METADATA_FILE_BYTES": (2 * GIB, "the source tree bound", lambda: limits.MAX_CI_SOURCE_FILE_BYTES),
    "MAX_CI_GIT_METADATA_TREE_BYTES": (20 * GIB, "the source tree bound", lambda: limits.MAX_CI_SOURCE_TREE_BYTES),
    "MAX_CI_BUILD_POLLS": (91, "one poll per interval of the wait, and the first",
                           lambda: limits.CI_BUILD_WAIT_SECONDS // limits.CI_BUILD_POLL_SECONDS + 1),
    "MAX_CI_SELECT_BUILD_REQUESTS": (
        155, "the 4 requests that admit a pull request, one run listing for each poll of a whole wait and the 15 "
             "that describe, download and recheck the Build, with 45 more for retries and further listing pages",
        lambda: (4 + 15 + 45) + limits.MAX_CI_BUILD_POLLS),
    "MAX_CI_BATCH_GIT_OUTPUT_BYTES": (64 * MIB, "the output of one Git call of the batch store: one Git tree listing",
                                      lambda: limits.MAX_CI_SOURCE_LIST_BYTES),
    "MAX_CI_ASSEMBLE_REQUESTS": (
        816, "the 15 requests and 2 per target of `ci assemble`, with 33 and 1 per target more for retries and "
             "further listing pages",
        lambda: (15 + 33) + (2 + 1) * limits.MAX_CI_TARGETS),
    "MAX_CI_AGGREGATE_REQUESTS": (
        816, "the 13 requests and 2 per lane of `ci aggregate`, with 35 and 1 per lane more for retries and "
             "further listing pages",
        lambda: (13 + 35) + (2 + 1) * limits.MAX_CI_LANES),
    "MAX_CI_BATCH_PREPARE_REQUESTS": (
        216, "the 10 requests and 3 per member of `ci batch-prepare`, with 6 and 1 per member more for retries",
        lambda: (10 + 6) + (3 + 1) * limits.MAX_CI_BATCH_MEMBERS),
    "MAX_CI_BATCH_SETTLE_REQUESTS": (
        348, "the 3 requests of `ci batch-settle`, the 28 of the merged gate pair and at most 5 per member, with 17 "
             "and 1 per member more for retries and further listing pages",
        lambda: (3 + 28 + 17) + (5 + 1) * limits.MAX_CI_BATCH_MEMBERS),
    "MAX_CI_TARGET_INPUT_ENTRIES": (
        174_353, "every export file and target directory with all its parent directories, and the root",
        lambda: (limits.MAX_CI_EXPORT_FILES + limits.MAX_CI_TARGETS) * (limits.MAX_BUNDLE_PATH_DEPTH + 1) + 1),
    "MAX_CI_RUNTIME_ENTRIES": (
        69_650, "every aggregate file and the envelope with all their parent directories, and the root",
        lambda: (limits.MAX_CI_RUNTIME_AGGREGATE_FILES + 1) * (limits.MAX_BUNDLE_PATH_DEPTH + 1) + 1),
    "MAX_CI_EXECUTION_LOG_CHARS": (22_369_624, "the base64 length of a full log",
                                   lambda: 4 * ((limits.MAX_CI_LOG_BYTES + 2) // 3)),
    "MAX_CI_EXECUTION_BYTES": (22_435_160, "that log and 64 KiB of record",
                               lambda: limits.MAX_CI_EXECUTION_LOG_CHARS + 64 * KIB),
}

PINNED = {**NATIVE, **PLATFORM, **KIT, **{name: row[:2] for name, row in DERIVED.items()}}
#: Bounds no Python module reads: the workflows carry them as literals, which workflow tests compare
#: (like the Pages ``RETENTION_DAYS``).
WORKFLOW_LITERALS = frozenset({"CI_RETENTION_DAYS"})
NATIVE_SOURCE = re.compile(r"\b(?:BP|QS):[A-Za-z0-9_./-]+\.(?:py|yml):[0-9]+")


def ci_bounds() -> set[str]:
    """Every Build/E2E bound of ``model/limits.py``: the constants named ``...CI_...``."""

    return {name for name in vars(limits) if name.isupper() and re.search(r"(?:^|_)CI_", name)}


def definitions() -> dict[str, set[str]]:
    """``{constant: the constants its definition reads}`` of ``model/limits.py``."""

    read: dict[str, set[str]] = {}
    for node in ast.parse(LIMITS.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            read[node.targets[0].id] = {item.id for item in ast.walk(node.value) if isinstance(item, ast.Name)}
    return read


def users() -> dict[str, set[str]]:
    """``{bound: the kit modules and tools that name it}``, ``model/limits.py`` itself aside."""

    names = ci_bounds()
    pattern = re.compile(r"\b(?:" + "|".join(sorted(names, key=len, reverse=True)) + r")\b")
    found: dict[str, set[str]] = {name: set() for name in names}
    sources = [*(ROOT / "src").rglob("*.py"), *(ROOT / "tools").glob("*.py")]
    for path in sources:
        if path.resolve() == LIMITS:
            continue
        for name in set(pattern.findall(path.read_text(encoding="utf-8"))):
            found[name].add(path.relative_to(ROOT).as_posix())
    return found


def live_bounds() -> set[str]:
    """The bounds some kit module or tool names, and the bounds that only define one of those."""

    used, read = users(), definitions()
    # The generation cap is deliberately a regression test, not a new runtime admission rule.
    live = {name for name, modules in used.items() if modules} | WORKFLOW_LITERALS | {"MAX_CI_GENERATION_REQUESTS"}
    while True:
        more = ({source for name in live for source in read.get(name, ())} & set(used)) - live
        if not more:
            return live
        live |= more


def fixture(name: str) -> dict[str, Any]:
    """A valid document of ``tests/fixtures/documents``, whatever its current shape is."""

    return strict_loads((DOCUMENTS / name).read_bytes(), label=name, max_bytes=limits.MIB)


class PinnedBoundsTest(unittest.TestCase):
    def test_every_bound_has_its_pinned_value(self) -> None:
        for name, (value, _source) in PINNED.items():
            with self.subTest(name=name):
                self.assertEqual(getattr(limits, name), value)
                self.assertIs(type(getattr(limits, name)), type(value))

    def test_every_bound_is_pinned_once(self) -> None:
        tables = (NATIVE, PLATFORM, KIT, DERIVED)
        self.assertEqual(sum(len(table) for table in tables), len({name for table in tables for name in table}))
        present = ci_bounds()
        unpinned = sorted(present - set(PINNED))
        self.assertEqual(unpinned, [], "add each bound to the table that says what it preserves")
        stale = sorted(set(PINNED) - present)
        self.assertEqual(stale, [], "a pinned bound no longer exists in model/limits.py")

    def test_every_native_row_cites_a_reviewed_source_line(self) -> None:
        for name, (_value, source) in NATIVE.items():
            with self.subTest(name=name):
                self.assertRegex(source, NATIVE_SOURCE)
        for name in (*PLATFORM, *KIT, *DERIVED):
            with self.subTest(name=name):
                self.assertTrue(PINNED[name][1].strip())

    def test_every_bound_is_enforced_by_kit_code(self) -> None:
        dead = sorted(ci_bounds() - live_bounds())
        self.assertEqual(dead, [], "no module under src/ or tools/ names these bounds: enforce or delete them")
        self.assertLessEqual(WORKFLOW_LITERALS, ci_bounds())

    def test_derived_bounds_follow_their_formulas(self) -> None:
        for name, (value, _words, formula) in DERIVED.items():
            with self.subTest(name=name):
                self.assertEqual(formula(), value)

    def test_the_wait_is_a_ceiling_the_polling_cannot_outlast(self) -> None:
        # Every pause but the last is a whole interval, so the polls end within the native 5400 s.
        self.assertLessEqual((limits.MAX_CI_BUILD_POLLS - 1) * limits.CI_BUILD_POLL_SECONDS,
                             limits.CI_BUILD_WAIT_SECONDS)
        self.assertLess(limits.CI_BUILD_WAIT_SECONDS, limits.MAX_CI_WORKER_TIMEOUT_SECONDS)

    def test_retention_covers_exactly_the_artifact_kinds(self) -> None:
        self.assertEqual(set(limits.CI_RETENTION_DAYS), set(grammar.CI_ARTIFACT_PREFIXES))
        # Nothing shared with the Pages map: Pages rotation never sees a Build artifact.
        self.assertEqual(set(limits.CI_RETENTION_DAYS) & set(limits.RETENTION_DAYS), set())
        self.assertTrue(all(type(days) is int and 1 <= days <= 90 for days in limits.CI_RETENTION_DAYS.values()))
        # The transient partitions expire first; only the small sealed records outlive the bytes.
        self.assertLess(limits.CI_RETENTION_DAYS["target"], limits.CI_RETENTION_DAYS["build"])
        self.assertLess(limits.CI_RETENTION_DAYS["build"], limits.CI_RETENTION_DAYS["tested"])

    def test_every_profile_has_a_build_report_bound(self) -> None:
        self.assertEqual(set(limits.MAX_CI_BUILD_REPORT_BYTES_BY_PROFILE), set(ci_native.profiles()))
        for profile, bound in limits.MAX_CI_BUILD_REPORT_BYTES_BY_PROFILE.items():
            with self.subTest(profile=profile):
                self.assertLessEqual(bound, limits.MAX_CI_EXPORT_FILE_BYTES)


class ScopeTest(unittest.TestCase):
    """The two scope decisions, stated as behaviour of the public validators.

    Both keep the stricter reading. The design names the 512 MiB archive cap for Quick Skin's
    profile, and both mods count 512 files and 256 MiB per evidence profile, not per lane.
    ``MeasuredTest`` shows what the stricter readings leave unused today.
    """

    def descriptor(self, kind: str, profile: str, size: int) -> dict[str, Any]:
        # Lane, results and packaged tested records come from the packaged caller; the rest from Build.
        caller = "packaged" if kind in ("runtime", "results") else "build"
        unit = {"target": "unit", "runtime": "unit", "tested": "build"}.get(kind)
        document = helpers.ci_descriptor(kind, gate=caller, unit_id=unit)
        document["artifact"]["size"] = size
        document["profile"] = profile
        return document

    def test_one_archive_cap_bounds_every_profile_and_artifact_kind(self) -> None:
        # Quick Skin's cap is its native one. For Block Pops it is a new and tighter bound: its
        # evaluator admits a 2 GiB artifact (BP:scripts/ci/pr_gate.py:166 and :1452) and its
        # bundle never crossed runs before.
        for profile in ci_native.profiles():
            for kind in grammar.CI_ARTIFACT_PREFIXES:
                cap = (limits.MAX_CI_RECORD_BYTES if kind in ("tested", "reuse")
                       else limits.MAX_CI_BUNDLE_COMPRESSED_BYTES)
                with self.subTest(profile=profile, kind=kind):
                    records.validate_descriptor(self.descriptor(kind, profile, cap))
                    with self.assertRaises(MbError):
                        records.validate_descriptor(self.descriptor(kind, profile, cap + 1))
        # The kit's transport could not carry Block Pops' native 2 GiB: it downloads an archive
        # into memory within the artifact cap it shares with Pages, which the design keeps as it is.
        self.assertLess(limits.MAX_CI_BUNDLE_COMPRESSED_BYTES, limits.MAX_ARTIFACT_BYTES)
        self.assertLess(limits.MAX_ARTIFACT_BYTES, 2 * GIB)

    def lane(self, files_per_profile: int, size: int) -> dict[str, Any]:
        """One lane's export holding two evidence profiles of ``files_per_profile`` screenshots."""

        document = fixture("ci-runtime-envelope.json")
        lane = document["lanes"][0]["id"]
        document.update(scope="lane", lane_id=lane, lanes=document["lanes"][:1])
        document["files"] = sorted(
            ({**document["files"][0], "path": f"profiles/{profile}/{index:04d}.png", "lane_id": lane,
              "role": "screenshot", "size": size}
             for profile in ("first", "second") for index in range(files_per_profile)),
            key=lambda item: item["path"])
        return document

    def test_the_runtime_caps_count_one_whole_lane(self) -> None:
        half = limits.MAX_CI_RUNTIME_FILES // 2
        runtime_schema.validate_runtime_envelope(self.lane(half, 1))
        # Each profile holds 257 files, half of what a mod allows it, and the lane is refused.
        with self.assertRaises(MbError):
            runtime_schema.validate_runtime_envelope(self.lane(half + 1, 1))
        full = limits.MAX_CI_RUNTIME_BYTES // (2 * limits.MAX_CI_PNG_BYTES)
        runtime_schema.validate_runtime_envelope(self.lane(full, limits.MAX_CI_PNG_BYTES))
        # Each profile holds 160 MiB of the 256 MiB a mod allows it, and the lane is refused.
        with self.assertRaises(MbError):
            runtime_schema.validate_runtime_envelope(self.lane(full + 1, limits.MAX_CI_PNG_BYTES))


class MeasuredTest(unittest.TestCase):
    """Every bound against real artifacts of both mods (``measured.json`` names the runs)."""

    def test_real_bundles_fit_the_archive_and_export_bounds(self) -> None:
        for profile in ci_native.profiles():
            with self.subTest(profile=profile):
                build = ci_native.load(profile, "measured.json")["build"]
                bundle = build["bundle"]
                # Quick Skin 192.8 MiB (38 % of the cap), Block Pops 118.6 MiB (23 %).
                self.assertLessEqual(bundle["artifact"]["bytes"], limits.MAX_CI_BUNDLE_COMPRESSED_BYTES)
                self.assertLessEqual(bundle["files"], limits.MAX_CI_EXPORT_FILES)
                self.assertLessEqual(bundle["expanded_bytes"], limits.MAX_CI_EXPORT_TREE_BYTES)
                self.assertLessEqual(bundle["largest_jar_bytes"], limits.MAX_CI_JAR_BYTES)
                # Every native file beside the JARs, by the role a plan gives it: the manifest and
                # Block Pops' build report are native reports, Quick Skin's SBOM is an ``sbom``.
                sboms = {name: size for name, size in bundle["other_files"].items() if name.startswith("sbom/")}
                reports = [size for name, size in bundle["other_files"].items() if name not in sboms]
                reports += [build["report"]["bytes"]] if "report" in build else []
                self.assertLessEqual(max(reports), limits.MAX_CI_BUILD_REPORT_BYTES_BY_PROFILE[profile])
                self.assertEqual(sorted(sboms), ["sbom/quick-skin.cdx.json"] if profile == "quick-skin" else [])
                self.assertLessEqual(max(sboms.values(), default=0), limits.MAX_CI_SBOM_BYTES)

    def test_the_role_bounds_of_native_reports_and_the_sbom_rest_on_these_sizes(self) -> None:
        """The evidence behind the two role bounds, so that a change of either needs new evidence.

        Block Pops' build report is the largest native report either mod writes: 16 % of its own
        8 MiB reader cap, which the kit keeps. Both manifests are below 1 % of their bound, and one
        is written per target partition, so a planned manifest is smaller still. Quick Skin's 4 MiB
        is the kit's choice below the 16 MiB its manifest reader admits; nothing measured asks for
        more. Its SBOM is 0.3 % of the 16 MiB its own SBOM reader admits, the bound of the role.
        """

        build = {profile: ci_native.load(profile, "measured.json")["build"] for profile in ci_native.profiles()}
        self.assertEqual(build["block-pops"]["bundle"]["other_files"], {"artifacts.json": 29_390})
        self.assertEqual(build["block-pops"]["report"], {"bytes": 1_341_326, "name": "build-matrix-report.json"})
        self.assertEqual(build["quick-skin"]["bundle"]["other_files"],
                         {"artifacts.json": 30_848, "sbom/quick-skin.cdx.json": 47_632})
        self.assertNotIn("report", build["quick-skin"])
        reports = limits.MAX_CI_BUILD_REPORT_BYTES_BY_PROFILE
        self.assertLess(6 * 1_341_326, reports["block-pops"])
        self.assertLess(100 * 29_390, reports["block-pops"])
        self.assertLess(100 * 30_848, reports["quick-skin"])
        self.assertLess(reports["quick-skin"], 16 * MIB)  # QS:scripts/release/artifact_manifest.py:15
        self.assertLess(100 * 47_632, limits.MAX_CI_SBOM_BYTES)
        # A report of the protected verifier is another contract with its own bound.
        self.assertLessEqual(limits.MAX_CI_REPORT_BYTES, min(reports.values()))

    def test_real_lanes_fit_the_lane_caps_with_every_scenario_counted_together(self) -> None:
        for profile in ci_native.profiles():
            packaged = ci_native.load(profile, "measured.json")["packaged"]
            for lane in packaged["lanes"]:
                with self.subTest(profile=profile, lane=lane["name"]):
                    self.assertLess(lane["bytes"], limits.MAX_CI_RUNTIME_BYTES)
            for sample in packaged["samples"]:
                with self.subTest(profile=profile, sample=sample["artifact"]["name"]):
                    # The largest measured lane is Quick Skin's: 141 files and 52.7 MiB in seven
                    # evidence profiles (28 % and 21 % of the lane caps).
                    self.assertLessEqual(sample["files"], limits.MAX_CI_RUNTIME_FILES)
                    self.assertLessEqual(sample["expanded_bytes"], limits.MAX_CI_RUNTIME_BYTES)
                    self.assertLessEqual(sample["largest_png_bytes"], limits.MAX_CI_PNG_BYTES)
                    self.assertLessEqual(sample["largest_log_bytes"], limits.MAX_CI_LOG_BYTES)
                    self.assertLessEqual(sample["longest_path_chars"], limits.MAX_BUNDLE_PATH_CHARS)
                    self.assertLessEqual(sample["deepest_path"], limits.MAX_BUNDLE_PATH_DEPTH)

    def test_block_pops_aggregate_fits_its_own_fan_in_budget(self) -> None:
        aggregate = ci_native.load("block-pops", "measured.json")["packaged"]["aggregate"]
        self.assertLessEqual(aggregate["files"], limits.MAX_CI_RUNTIME_AGGREGATE_FILES)
        self.assertLessEqual(aggregate["expanded_bytes"], limits.MAX_CI_RUNTIME_AGGREGATE_BYTES)
        self.assertLessEqual(aggregate["artifact"]["bytes"], limits.MAX_CI_BUNDLE_COMPRESSED_BYTES)

    def test_quick_skins_lanes_do_not_fit_one_complete_runtime_export(self) -> None:
        """An open decision, pinned so that nobody settles it by raising a bound.

        Block Pops' fan-in budget holds its own 20 lanes. Quick Skin has no native aggregate, and
        the raw evidence of its 34 lanes is more than twice that budget and more than one artifact
        the kit can download. A complete runtime export of that profile therefore cannot be the
        byte union of its lanes; what it holds instead is for the owner of the aggregate to decide.
        """

        packaged = ci_native.load("quick-skin", "measured.json")["packaged"]
        # Archive bytes; the files they hold are larger still (1.09 times in the measured lanes).
        archives = sum(lane["bytes"] for lane in packaged["lanes"])
        files = len(packaged["lanes"]) * min(sample["files"] for sample in packaged["samples"])
        self.assertGreater(files, limits.MAX_CI_RUNTIME_AGGREGATE_FILES)  # 4,760 against 4,096.
        self.assertGreater(archives, 2 * limits.MAX_CI_RUNTIME_AGGREGATE_BYTES)  # 1,193 MiB against 512.
        self.assertGreater(archives, limits.MAX_ARTIFACT_BYTES)


class DecisionRecordTest(unittest.TestCase):
    """ADR 0007 is the decision record of these bounds; it names the ones that exist."""

    def test_the_record_names_every_preserved_bound_and_no_missing_one(self) -> None:
        text = ADR.read_text(encoding="utf-8")
        named = {name for name in re.findall(r"`([A-Z][A-Z0-9_]*)`", text) if re.search(r"(?:^|_)CI_", name)}
        self.assertEqual(sorted(set(NATIVE) - named), [], "ADR 0007 lacks these preserved native bounds")
        self.assertEqual(sorted(named - ci_bounds()), [], "ADR 0007 names bounds that do not exist")


if __name__ == "__main__":
    unittest.main()

"""Every numeric bound the kit enforces (SPEC §3.0 "Global limits" plus the per-field bounds).

All limits are enforced before allocation: readers check sizes and counts before reading bytes,
decoding images or building collections. Code imports a bound from here; a module constant named
like one of these bounds elsewhere may only alias it with the same value
(``tests/test_model_limits.py``), never define another copy.
"""

from __future__ import annotations

KIB = 1024
MIB = 1024 * KIB
GIB = 1024 * MIB

# -- Documents (SPEC §3.0 table) ------------------------------------------------------------------
MAX_MANIFEST_BYTES = 10 * MIB
MAX_EXPECTATION_BYTES = 16 * MIB
MAX_SELECTION_BYTES = 1 * MIB
MAX_EXTENSIONS_BYTES = 1 * MIB
MAX_SCOPE_DETAIL_BYTES = 64 * KIB
MAX_PROMOTION_BYTES = 4 * MIB
MAX_ENVELOPE_BYTES = 4 * MIB
MAX_PAIRED_BYTES = 16 * MIB
MAX_BUILD_RECORD_BYTES = 64 * KIB
MAX_SITE_DATA_BYTES = 4 * MIB
MAX_GALLERY_DATA_BYTES = 64 * MIB
MAX_TEMPLATE_MANIFEST_BYTES = 256 * KIB
MAX_KIT_STAMP_BYTES = 4 * KIB
MAX_CONFIG_BYTES = 256 * KIB
#: The ``--selected-json`` document of ``authenticate`` and ``family collect`` (a ``Selected`` object:
#: a handful of scalars), also a collected family's ``selected.json``.
MAX_SELECTED_JSON_BYTES = 64 * KIB

# -- Bundles and images ---------------------------------------------------------------------------
MAX_FRAMES = 1000
MAX_LANES = 256
MAX_COMPARISONS = 1000
MAX_SCENARIOS = 256
MAX_ROLES_PER_LANE = 16
MAX_RUNTIME_FILES = 4096
MAX_RUNTIME_JSON_BYTES = 4 * MIB
MAX_SOURCE_PNG_BYTES = 32 * MIB
MAX_DERIVATIVE_BYTES = 4 * MIB
#: Expanded bytes of one bundle uploaded as a single artifact (SPEC §3.0 "Raw bundle 1 GiB"): the
#: bound every producer checks (``prepare``, ``anchor create``, ``family collect``; ``family envelope``
#: through ``families[].handoff_max_bytes``, a share of it, Families below) and every extraction
#: enforces.
MAX_RAW_BUNDLE_BYTES = 1 * GIB
MAX_COMPACT_BUNDLE_BYTES = 256 * MIB
MAX_ANCHOR_BUNDLE_BYTES = MAX_RAW_BUNDLE_BYTES
#: The most an artifact's ZIP archive adds to the files it holds: per-entry local and central
#: headers, deflate framing of payloads that do not compress (PNG, WebP) and slack. It bounds
#: ``bounded_zip.archive_limit(bounds) - bounds.max_total_bytes`` for every extraction kind
#: (``tests/test_model_limits.py``).
MAX_ARCHIVE_OVERHEAD_BYTES = 32 * MIB
#: The largest artifact archive (the ZIP bytes GitHub serves) a kit job admits, selects or downloads
#: into memory: the largest expanded bundle plus :data:`MAX_ARCHIVE_OVERHEAD_BYTES`. An archive of
#: any bundle within its expanded bound therefore fits it (``bounded_zip.archive_limit(kind) <=
#: MAX_ARTIFACT_BYTES`` for every kind), so a producer's own bound predicts what every consumer
#: accepts (``github.artifacts.MAX_ARCHIVE_BYTES``, ``github.api.MAX_DOWNLOAD_BYTES``).
MAX_ARTIFACT_BYTES = MAX_RAW_BUNDLE_BYTES + MAX_ARCHIVE_OVERHEAD_BYTES
MAX_HANDOFF_FILES = MAX_RUNTIME_FILES + 2  # runtime/** + expectation.json + extensions.json
MAX_COMPACT_FILES = MAX_FRAMES + 3  # images/** + expectation.json + selection.json + extensions.json
MAX_ANCHOR_FILES = MAX_FRAMES + 1  # images/** + expectation.json
MAX_IMAGE_PIXELS = 20_000_000
MAX_IMAGE_DIMENSION = 16_384
MAX_SITE_BYTES = 1 * GIB
MAX_SITE_FILES = 8192
#: Characters and components of one bundle path. A sealed CI export path keeps both bounds and
#: only widens the component grammar (``grammar.is_export_path``).
MAX_BUNDLE_PATH_CHARS = 300
MAX_BUNDLE_PATH_DEPTH = 16

# -- Families -------------------------------------------------------------------------------------
MAX_FAMILIES = 8
#: Expanded bytes of an ``mb-collected-family--<family>--<key>`` artifact (``family collect``): one
#: bundle's, since ``build`` downloads it as one archive within ``MAX_ARTIFACT_BYTES``.
MAX_COLLECTED_FAMILY_BYTES = MAX_RAW_BUNDLE_BYTES
#: Expanded bytes of one leg's projection images (``images/`` of a collected family): one compact
#: bundle's WebP derivatives.
MAX_FAMILY_PROJECTION_BYTES = MAX_COMPACT_BUNDLE_BYTES
#: The ceiling of ``families[].handoff_max_bytes``, the expanded bytes of a family's native bundle.
#: A collected family holds that bundle verbatim beside its ``envelope.json``, ``paired.json``,
#: ``selected.json`` and projection images, so the ceiling is what those leave of
#: ``MAX_COLLECTED_FAMILY_BYTES`` (784,269,312 bytes): every native bundle a producer's ``family
#: envelope`` accepts is collectable (``tests/test_model_limits.py``).
MAX_FAMILY_HANDOFF_BYTES = (MAX_COLLECTED_FAMILY_BYTES - MAX_FAMILY_PROJECTION_BYTES - MAX_ENVELOPE_BYTES
                            - MAX_PAIRED_BYTES - MAX_SELECTED_JSON_BYTES)
#: Expanded bytes of a family handoff or cache: the native bundle plus its ``envelope.json`` (the
#: ``family-handoff`` and ``family-cache`` extraction totals).
MAX_FAMILY_BUNDLE_BYTES = MAX_FAMILY_HANDOFF_BYTES + MAX_ENVELOPE_BYTES
MAX_FAMILY_RETENTION_DAYS = 7
MAX_FAMILY_FILES = 8192
MAX_FAMILY_LANES = 512
MAX_FAMILY_PAIRS_PER_LANE = 64
MAX_NOT_APPLICABLE = 1024
MAX_FAMILY_LINKS = 16
MAX_FAMILY_CONTRACTS = 16

# -- Text fields ----------------------------------------------------------------------------------
MAX_RUNTIME_EVIDENCE_LENGTH = 4096  # QS MAX_RUNTIME_EVIDENCE_LENGTH (V9); mandatory on every frame
MAX_TITLE_LENGTH = 200
MAX_EXPECTATION_TEXT_LENGTH = 4096
MAX_LABEL_LENGTH = 80
MAX_PROFILE_LENGTH = 40
MAX_REASON_LENGTH = 200
MAX_LINK_LABEL_LENGTH = 40
MAX_DISPLAY_TITLE_LENGTH = 500
MAX_JOB_NAME_LENGTH = 200
MAX_EXTENSION_NAMES = 16

# -- Targets, admission and GitHub ----------------------------------------------------------------
MAX_KEYS = 64
MAX_BRANCHES = 100
MAX_ARTIFACTS_PER_NAME = 512
MAX_PAGES_API_READS = 160  # QS publication_progress.MAX_REQUESTS
MAX_API_RESPONSE_BYTES = 32 * MIB
MAX_JOBS_PER_ATTEMPT = 1000
MAX_RUN_ATTEMPT = 1000
MAX_RUN_ID = 2**63 - 1
MAX_ARTIFACT_NAME_BYTES = 240
#: Artifacts one rotation may delete (SPEC §5.5). A Quick Skin generation alone supersedes about
#: 35 long-lived artifacts (17 caches, 17 family caches and an anchor), so the budget leaves room to
#: retire what an earlier rotation had to defer (``pages.rotate``'s leftover phase).
DELETION_BUDGET = 64
RUN_POLL_ATTEMPTS = 30
RUN_POLL_INTERVAL_SECONDS = 2.0
#: Complete reads of one paginated listing before an inconsistent snapshot fails closed. GitHub's
#: listings are eventually consistent while the listed run still uploads (or a rotation deletes)
#: artifacts: rows that disagree with ``total_count``, a ``total_count`` that changes between pages or
#: a repeated row discard the whole snapshot, which is read again from page 1
#: (``github.api.read_consistently``). Every read spends the client's request budget.
LISTING_READ_ATTEMPTS = 4
#: The wait before the first re-read of an inconsistent listing; it doubles for every later one.
LISTING_RETRY_DELAY_SECONDS = 2.0
#: The longest wait between two reads of one listing (so at most 2 + 4 + 8 seconds in all).
MAX_LISTING_RETRY_DELAY_SECONDS = 8.0
#: The most runs GitHub lists for a workflow-run search filtered by ``branch``, ``event``, ``head_sha``
#: or ``status`` (among others); its ``total_count`` may be larger (``github.runs.workflow_runs``).
MAX_FILTERED_RUNS_LISTED = 1000
#: Owner runs authenticated for one exact-name or run inventory (QS ``publication_progress``
#: ``MAX_CANDIDATES``; ``select`` and ``admit``).
MAX_CANDIDATES = 8
#: Rows of one ``head_sha``-filtered source-run listing (one page; a full one is confirmed by a second).
MAX_SUBJECT_RUNS = 100
#: Rows of the canonical-branch listing searched for display-titled source runs (newest first, so
#: the bound can only hide an older run: no evidence, never an older run passing as the newest).
MAX_CANONICAL_RUNS = 300
#: Family legs (and progress legs) of one publication: GitHub's job-matrix bound.
MAX_FAMILY_LEGS = 256
#: A ``source.workflow`` file read at a handoff run's head (kit binding).
MAX_WORKFLOW_FILE_BYTES = 1 * MIB
#: Rotation: commits of a subject branch's first-parent history probed for superseded generations:
#: one exact-name read each for the previous cache generation (the probe stops at the first older
#: commit a superseding Pages run published), and, for the leftovers an earlier rotation deferred,
#: ``1 +`` the family legs of the last promoted key reads per commit, only while deletion budget
#: remains. ``select``'s family carry-forward walk probes at most as many commits.
GENERATION_PROBES = 32
#: Rotation: anchor-eligible runs at or before the grace boundary named (one read each) per key.
ANCHOR_PROBES = 16

# -- ZIP extraction -------------------------------------------------------------------------------
MAX_ZIP_RATIO = 200
#: ``mb-collected-family--<family>--<key>`` (``family collect``): ``paired.json``, ``selected.json``
#: (the recorded ``Selected`` generation), at most ``MAX_FAMILY_FILES`` projection images and
#: ``source/`` (the family handoff or cache verbatim: its ``envelope.json`` plus at most
#: ``MAX_FAMILY_FILES`` native files). Its expanded bytes are ``MAX_COLLECTED_FAMILY_BYTES``
#: (Families above).
MAX_COLLECTED_FAMILY_FILES = 2 * MAX_FAMILY_FILES + 3
#: The largest entry count any artifact kind may extract (``collected-family``).
MAX_ZIP_ENTRIES = MAX_COLLECTED_FAMILY_FILES

# -- Adapter host ---------------------------------------------------------------------------------
ADAPTER_TIMEOUT_DEFAULT_SECONDS = 600
ADAPTER_TIMEOUT_MAX_SECONDS = 1800
MAX_ADAPTER_REQUEST_BYTES = 16 * MIB
MAX_ADAPTER_RESPONSE_BYTES = 16 * MIB
MAX_ADAPTER_PYTHON_PATH = 8

# -- Config ---------------------------------------------------------------------------------------
MAX_ICON_BYTES = 512 * KIB
MAX_ICON_DIMENSION = 1024
MAX_PROJECT_LINKS = 8
MAX_LABEL_ENTRIES = 256
MAX_COPY_PARAGRAPHS = 16
MAX_TEMPLATE_PATHS = 32

# -- Protected Build/packaged runtime (independent from Pages budgets) -----------------------------
MAX_CI_PLAN_BYTES = 4 * MIB
#: One candidate file a plan is derived from (the release inventory, the scenario contract, or an
#: extra plan input the protected Build config names).
MAX_CI_PLAN_SOURCE_BYTES = 4 * MIB
#: Extra candidate files a protected Build config may name for plan derivation (``plan_inputs``).
#: Quick Skin needs one: ``gradle.properties`` holds the mod version its JAR names carry.
MAX_CI_PLAN_INPUTS = 8
#: The private ``identity.json`` state record ``ci subject`` writes.
MAX_CI_IDENTITY_BYTES = 16 * KIB
#: API requests of one ``ci subject``: a pull request costs 4, a protected subject 5; the rest is
#: room for retried attempts.
MAX_CI_SUBJECT_REQUESTS = 16
#: The private ``worker.json`` state record ``ci worker-prepare`` writes: the accounts, the tool
#: receipt and at most ``MAX_CI_TOOL_ROOTS`` paths of ``MAX_CI_TOOL_PATH_BYTES`` each, twice.
MAX_CI_WORKER_RECORD_BYTES = 256 * KIB
#: API requests of one ``ci plan`` without a candidate checkout: the tested tree and one blob per
#: candidate file, so 3 without extra plan inputs and at most 11; the rest is room for retried
#: attempts. With a checkout it spends none.
MAX_CI_PLAN_REQUESTS = 24
#: One read (``rev-parse``, ``ls-tree``, ``cat-file``) of the candidate checkout's object store.
CI_GIT_READ_TIMEOUT_SECONDS = 60.0
#: What such a read answers besides a blob: one object id, or one tree entry with its path.
MAX_CI_GIT_ANSWER_BYTES = 4 * KIB
#: ``validation-input/``: the candidate files a plan is derived from (the inventory, the scenario
#: contract and the extra plan inputs) and, once it exists, the plan.
MAX_CI_PLAN_INPUT_FILES = 3 + MAX_CI_PLAN_INPUTS
MAX_CI_PLAN_INPUT_ENTRIES = MAX_CI_PLAN_INPUT_FILES + 1  # MB1 entry caps count the root.
MAX_CI_PLAN_INPUT_BYTES = MAX_CI_PLAN_BYTES + (2 + MAX_CI_PLAN_INPUTS) * MAX_CI_PLAN_SOURCE_BYTES
MAX_CI_PRIVATE_RECORD_ENTRIES = 2  # MB1 entry caps count the root: the directory plus its one leaf.
MAX_CI_POLICY_TESTS = 100000
MAX_CI_POLICY_WORKERS = 256
MAX_CI_ENVELOPE_BYTES = 4 * MIB
MAX_CI_RECORD_BYTES = 4 * MIB
MAX_CI_ARTIFACTS_PER_GATE = MAX_JOBS_PER_ATTEMPT
MAX_CI_CONFIG_BYTES = 1 * MIB
#: Characters of one required status context a protected Build config names.
MAX_CI_STATUS_CONTEXT_CHARS = 100
MAX_CI_ACTIVATION_BYTES = 8 * KIB
MAX_CI_ADAPTER_FILE_BYTES = 4 * MIB
MAX_CI_ADAPTER_TREE_BYTES = 64 * MIB
MAX_CI_ADAPTER_FILES = 256
MAX_CI_WORKER_TIMEOUT_SECONDS = 6 * 60 * 60
MAX_CI_ENV_VALUE_BYTES = 128 * KIB
MAX_CI_ENV_BYTES = 256 * KIB
MAX_CI_CONTROL_OUTPUT_BYTES = 16 * KIB
MAX_CI_TOOL_PATH_BYTES = 4 * KIB
MAX_CI_TOOL_ROOTS = 16
MAX_CI_TOOL_SYMLINK_HOPS = 40
MAX_CI_TOOL_TREE_DEPTH = 64
MAX_CI_KIT_INSTALL_ENTRIES = 40_000
MAX_CI_KIT_INSTALL_FILES = 20_000
MAX_CI_KIT_INSTALL_BYTES = 512 * MIB
MAX_CI_FILE_ID = (1 << 64) - 1
MIN_CI_WORKER_UID = 1000
MAX_CI_UNIX_ID = (1 << 32) - 2  # Linux uid_t/gid_t; exclude the all-ones no-change sentinel.
CI_TERMINATION_GRACE_SECONDS = 15.0
CI_TERMINATION_POLL_SECONDS = 0.25
MAX_CI_COMMAND_ARGUMENTS = 256
MAX_CI_COMMAND_BYTES = 256 * KIB
CI_PROCESS_READ_BYTES = 64 * KIB
CI_ROOT_OPERATION_TIMEOUT_SECONDS = 1800.0
MAX_CI_ROOT_DIAGNOSTIC_BYTES = 4 * KIB
CI_HOST_FENCE_TIMEOUT_SECONDS = 600.0
MAX_CI_HOST_FENCE_REPORT_BYTES = 64 * KIB
MAX_CI_SOURCE_LIST_BYTES = 64 * MIB
MAX_CI_SOURCE_FILES = 200_000
MAX_CI_SOURCE_ENTRIES = 250_000
MAX_CI_SOURCE_FILE_BYTES = 2 * GIB
MAX_CI_SOURCE_TREE_BYTES = 20 * GIB
MAX_CI_SOURCE_LINK_BYTES = 4 * KIB
#: Components of one Gradle seed path (``grammar.is_seed_path``). A cache carries no name grammar,
#: so ``MAX_BUNDLE_PATH_DEPTH`` does not apply: a dependency is already eight components deep
#: (``caches/modules-2/files-2.1/<group>/<module>/<version>/<hash>/<file>``) and an unpacked
#: transform output can add a whole package tree. No MB1 walk descends further
#: (``io.tree.MAX_WALK_DEPTH``).
MAX_CI_SEED_PATH_DEPTH = 64
MAX_CI_GIT_METADATA_FILES = MAX_CI_SOURCE_FILES
MAX_CI_GIT_METADATA_ENTRIES = MAX_CI_SOURCE_ENTRIES
MAX_CI_GIT_METADATA_FILE_BYTES = MAX_CI_SOURCE_FILE_BYTES
MAX_CI_GIT_METADATA_TREE_BYTES = MAX_CI_SOURCE_TREE_BYTES
MAX_CI_GIT_REF_BYTES = 4 * KIB
MAX_CI_GIT_REF_LIST_BYTES = 64 * MIB
MAX_CI_TARGETS = 256
MAX_CI_BUILD_RUNS = 1000
MAX_CI_LANES = 256
MAX_CI_OUTPUTS_PER_TARGET = 1024
MAX_CI_OBLIGATIONS_PER_LANE = 1024
CI_BUILD_WAIT_SECONDS = 5400
CI_BUILD_POLL_SECONDS = 60
MAX_CI_BUILD_POLLS = CI_BUILD_WAIT_SECONDS // CI_BUILD_POLL_SECONDS + 1
#: API requests of one ``ci select-build``. A pull request whose Build is complete costs 17, and
#: one more for every poll before that (91 polls at most, one a minute); a protected subject costs
#: 15. The rest is for retried attempts and for the further pages of a run with over 100 jobs.
MAX_CI_SELECT_BUILD_REQUESTS = MAX_CI_BUILD_POLLS + 64
#: API requests of one ``ci fetch-build`` (21 for a pull request, 18 and 15 for a protected subject);
#: the rest is for retries and pages.
MAX_CI_FETCH_BUILD_REQUESTS = 48
MAX_CI_BATCH_MEMBERS = 50
#: A batch manifest travels in the body of its pull request, which GitHub bounds at 65,536; the
#: whole body, the marker inside it and the decoded document share this bound.
MAX_CI_BATCH_DOCUMENT_BYTES = 64 * KIB
MAX_CI_BATCH_PUSH_RECEIPT_BYTES = 4096
#: A member's pull-request title (GitHub's own bound); it becomes the subject of its squash commit.
MAX_CI_BATCH_TITLE_CHARS = 256
#: Entries of the allowed-path list a caller hands to the batch constructor.
MAX_CI_BATCH_ALLOWED_PATHS = 4096
#: Paths one refusal names (a conflict, a rename that was followed).
MAX_CI_BATCH_REPORTED_PATHS = 20
CI_BATCH_GIT_TIMEOUT_SECONDS = 120
#: One fetch or push of the batch store.
CI_BATCH_GIT_TRANSFER_TIMEOUT_SECONDS = 600
MAX_CI_BATCH_GIT_OUTPUT_BYTES = MAX_CI_SOURCE_LIST_BYTES
#: ``ci batch-prepare`` sends 10 requests plus 3 per member (160 for 50); the rest is for retries.
MAX_CI_BATCH_PREPARE_REQUESTS = 16 + 4 * MAX_CI_BATCH_MEMBERS
#: ``ci batch-settle`` sends 3 requests, the 28 of ``transport.download_merged_gate_pair`` for both
#: original gates and at most 5 per member (281 for 50). The rest is for retries and for the further
#: pages of a run that lists more than 100 jobs or artifacts.
MAX_CI_BATCH_SETTLE_REQUESTS = 48 + 6 * MAX_CI_BATCH_MEMBERS
#: The archive of one ``mb-ci-*`` artifact of any kind and profile: Quick Skin's bundle admission
#: cap, which is tighter than the 2 GiB Block Pops' evaluator admits (``tests/test_ci_limits.py``).
MAX_CI_BUNDLE_COMPRESSED_BYTES = 512 * MIB
MAX_CI_EXPORT_FILES = 10_000
MAX_CI_EXPORT_ENTRIES = 20_000
MAX_CI_EXPORT_FILE_BYTES = GIB
MAX_CI_EXPORT_TREE_BYTES = 2 * GIB
# Additional complete-target transport bounds, independent of native/runtime fan-in limits.
MAX_CI_TARGET_DOWNLOAD_BYTES = 4 * GIB
MAX_CI_TARGET_INPUT_ENTRIES = (MAX_CI_EXPORT_FILES + MAX_CI_TARGETS) * (MAX_BUNDLE_PATH_DEPTH + 1) + 1
#: One lane's whole runtime export. The mods apply these numbers to each evidence profile (one
#: scenario of a lane), so a lane with several scenarios is counted more strictly here.
MAX_CI_RUNTIME_FILES = 512
MAX_CI_RUNTIME_BYTES = 256 * MIB
# Original Block Pops aggregate fan-in bounds; lane limits above remain unchanged.
MAX_CI_RUNTIME_AGGREGATE_FILES = 4096
MAX_CI_RUNTIME_AGGREGATE_BYTES = 512 * MIB
MAX_CI_RUNTIME_ENVELOPE_BYTES = 4 * MIB
# One private root request: the largest operation carries a tested-tree inventory (JSON rows are wider
# than the Git listing they come from), a plan, the owning Build and one runtime envelope.
MAX_CI_ROOT_REQUEST_BYTES = (2 * MAX_CI_SOURCE_LIST_BYTES + MAX_CI_PLAN_BYTES + MAX_CI_ENVELOPE_BYTES
                             + MAX_CI_RUNTIME_ENVELOPE_BYTES + MAX_CI_RECORD_BYTES + 2 * MIB)
# Complete logical closure, including every legal directory prefix and the envelope.
MAX_CI_RUNTIME_ENTRIES = (MAX_CI_RUNTIME_AGGREGATE_FILES + 1) * (MAX_BUNDLE_PATH_DEPTH + 1) + 1
# Private original input parent plus independently bounded Build/runtime child closures.
MAX_CI_ORIGINAL_INPUT_ENTRIES = MAX_CI_EXPORT_ENTRIES + MAX_CI_RUNTIME_ENTRIES + 1
MAX_CI_REPORT_BYTES = 4 * MIB
# Original Build input reports and new validator-output reports have separate contracts.
MAX_CI_BUILD_REPORT_BYTES_BY_PROFILE = {"quick-skin": 4 * MIB, "block-pops": 8 * MIB}
#: One ``sbom`` output of a Build export: what Quick Skin's own SBOM reader admits. Block Pops
#: stages none, and an SBOM is optional in a plan.
MAX_CI_SBOM_BYTES = 16 * MIB
MAX_CI_LOG_BYTES = 16 * MIB
MAX_CI_EXECUTION_LOG_CHARS = 4 * ((MAX_CI_LOG_BYTES + 2) // 3)
MAX_CI_EXECUTION_BYTES = MAX_CI_EXECUTION_LOG_CHARS + 64 * KIB
MAX_CI_PNG_BYTES = 32 * MIB
#: A production or harness JAR as one export file. Its entry count and its expanded and nested-archive
#: limits stay with the mod's own protected verifier, which is the code that opens it.
MAX_CI_JAR_BYTES = 256 * MIB

# -- Retention (days) per artifact kind (SPEC §3.0 artifact table) ---------------------------------
RETENTION_DAYS = {
    "handoff": 1,
    "collected": 1,
    "collected-family": 1,
    "promotion": 1,
    "pages": 1,
    "cache": 90,
    "family-cache": 90,
    "baseline": 90,
}
MAX_ANCHOR_RETENTION_DAYS = 90
MAX_BASELINE_RETENTION_DAYS = 90
#: Retention (days) of every ``mb-ci-*`` artifact kind (``grammar.CI_ARTIFACT_PREFIXES``): transient
#: target partitions one day, staged Build bundles and raw runtime artifacts seven, sealed tested and
#: reuse records ninety. Pages rotation never sees these names.
CI_RETENTION_DAYS = {
    "target": 1,
    "build": 7,
    "runtime": 7,
    "results": 7,
    "tested": 90,
    "reuse": 90,
}

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
#: Owner runs authenticated for one exact-name or run inventory (QS ``publication_progress``
#: ``MAX_CANDIDATES``; ``select`` and ``admit``).
MAX_CANDIDATES = 8
#: Rows of one ``head_sha``-filtered source-run listing (one page).
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

"""Every numeric bound the kit enforces (SPEC §3.0 "Global limits" plus the per-field bounds).

All limits are enforced before allocation: readers check sizes and counts before reading bytes,
decoding images or building collections. Nothing outside this module may define its own copy of
a bound listed here.
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
MAX_RAW_BUNDLE_BYTES = 1 * GIB
MAX_COMPACT_BUNDLE_BYTES = 256 * MIB
MAX_ANCHOR_BUNDLE_BYTES = 1 * GIB
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
MAX_FAMILY_HANDOFF_BYTES = 1 * GIB
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
DELETION_BUDGET = 32
RUN_POLL_ATTEMPTS = 30
RUN_POLL_INTERVAL_SECONDS = 2.0

# -- ZIP extraction -------------------------------------------------------------------------------
MAX_ZIP_RATIO = 200
MAX_ZIP_ENTRIES = 8192

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

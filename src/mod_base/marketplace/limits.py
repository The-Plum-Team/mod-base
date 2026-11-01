"""Every numeric bound of :mod:`mod_base.marketplace`, until they move into ``model/limits.py``.

Each constant says what it bounds. None shares a name with a bound of ``model/limits.py``, so
``tests/test_model_limits.py`` (a same-named constant must alias the kit's bound) never applies.
"""

from __future__ import annotations

KIB = 1024
MIB = 1024 * KIB

# -- Transport ------------------------------------------------------------------------------------
#: Attempts of one request, the first included. Only a GET (or a request declared replayable)
#: spends more than one on a transport error or a 5xx; every request may spend them on HTTP 429.
MARKETPLACE_REQUEST_ATTEMPTS = 5
#: The longest single wait before a retry: the exponential backoff is capped here, and a
#: ``Retry-After`` asking for longer ends the request at once instead of stalling the job.
MAX_MARKETPLACE_RETRY_DELAY_SECONDS = 30.0
#: The longest one transport waits in total for retries (backoff and ``Retry-After`` together).
MAX_MARKETPLACE_RETRY_WAIT_SECONDS = 180.0
#: The socket timeout of every blocking operation of one exchange (connect, send, each read).
MARKETPLACE_REQUEST_TIMEOUT_SECONDS = 60
#: The largest JSON answer read into memory (a declared longer one is refused unread).
MAX_MARKETPLACE_JSON_BYTES = 32 * MIB
#: The largest JSON document one request sends (version data, upload metadata, a release body).
MAX_MARKETPLACE_REQUEST_JSON_BYTES = 1 * MIB
#: Bytes of an error answer read to describe it; the description itself is one line of at most
#: :data:`MAX_MARKETPLACE_ERROR_DETAIL_CHARS` characters with every credential redacted.
MAX_MARKETPLACE_ERROR_BODY_BYTES = 64 * KIB
MAX_MARKETPLACE_ERROR_DETAIL_CHARS = 200
#: The largest file one request uploads or one download reads (a mod JAR is a few MiB).
MAX_MARKETPLACE_FILE_BYTES = 256 * MIB
#: Characters of one uploaded file name (the multipart ``filename``, a GitHub asset name).
MAX_MARKETPLACE_FILENAME_CHARS = 255
#: Characters of one request header value (a token, an ``Accept`` list) and headers per transport.
MAX_MARKETPLACE_HEADER_CHARS = 4096
MAX_MARKETPLACE_HEADERS = 16
#: Characters of one request path, query parameters per request and characters of one value.
MAX_MARKETPLACE_PATH_CHARS = 2048
MAX_MARKETPLACE_QUERY_PARAMETERS = 16
MAX_MARKETPLACE_QUERY_VALUE_CHARS = 1024
#: Characters of the one storage redirect a GitHub asset download follows.
MAX_MARKETPLACE_REDIRECT_URL_CHARS = 8192
#: The largest ``max_requests`` a transport accepts (every attempt and redirect counts).
MAX_MARKETPLACE_REQUESTS = 10_000
#: Parts of one multipart body (text fields and files together).
MAX_MULTIPART_PARTS = 8
#: Characters of one changelog (Modrinth's own limit, applied to CurseForge too).
MAX_MARKETPLACE_CHANGELOG_CHARS = 65_536
#: Minecraft versions and loaders one Modrinth version or CurseForge file declares.
MAX_MARKETPLACE_GAME_VERSIONS = 256
MAX_MARKETPLACE_LOADERS = 16
#: Dependencies (Modrinth) or relations (CurseForge) one upload declares.
MAX_MARKETPLACE_DEPENDENCIES = 64

# -- Modrinth -------------------------------------------------------------------------------------
#: Versions one project listing may hold (``GET /v2/project/{id}/version`` is not paginated).
MAX_MODRINTH_VERSIONS = 10_000
#: Files one Modrinth version answer may hold.
MAX_MODRINTH_VERSION_FILES = 64
#: Modrinth's own bounds of a version's ``name`` and ``version_number``.
MAX_MODRINTH_NAME_CHARS = 64
MAX_MODRINTH_VERSION_NUMBER_CHARS = 32

# -- CurseForge -----------------------------------------------------------------------------------
#: Rows of one public listing page: the listing silently caps a larger request at 50.
CURSEFORGE_PAGE_SIZE = 50
#: Files one project listing may hold, and the pages read for it: the walk is bounded by
#: requests issued, not rows kept, so a listing that never ends cannot spin.
MAX_CURSEFORGE_FILES = 10_000
MAX_CURSEFORGE_PAGES = MAX_CURSEFORGE_FILES // CURSEFORGE_PAGE_SIZE
#: Rows of ``/api/game/versions`` and ``/api/game/version-types``.
MAX_CURSEFORGE_GAME_VERSIONS = 50_000
MAX_CURSEFORGE_VERSION_TYPES = 2_000
#: Characters of one CurseForge display name.
MAX_CURSEFORGE_DISPLAY_NAME_CHARS = 256

# -- GitHub Releases ------------------------------------------------------------------------------
#: Rows of one GitHub listing page (the API's maximum).
GITHUB_PER_PAGE = 100
#: Releases one repository listing may hold, and assets one release may hold (GitHub's own cap).
MAX_GITHUB_RELEASES = 2_000
MAX_GITHUB_RELEASE_ASSETS = 1_000
#: Characters of a release body (GitHub's own limit) and of a release name.
MAX_GITHUB_RELEASE_BODY_CHARS = 125_000
MAX_GITHUB_RELEASE_NAME_CHARS = 256

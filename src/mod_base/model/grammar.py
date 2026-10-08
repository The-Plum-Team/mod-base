"""Identifier grammar and the only builders/parsers of kit artifact names (SPEC §3.0).

Artifact names are built and parsed only here. A key never contains ``--`` and a family is a
hyphen-separated token without ``--``, so every name splits on ``--`` without ambiguity. Parsers
are strict: an ``mb-`` name that does not match its exact grammar is malformed, and a name that
does not start with ``mb-`` is never a kit artifact (rotation must never touch it).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from mod_base.errors import MbError
from mod_base.model import limits

KEY = re.compile(r"^[a-z0-9](?:[a-z0-9._]|-(?!-)){0,62}[a-z0-9]$")
FAMILY = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
MAX_FAMILY_LENGTH = 32
LANE_ID = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+){1,3}$")
MAX_LANE_ID_LENGTH = 200
SHA1 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
BRANCH = re.compile(r"^(?!/)(?!.*(?:\.\.|//))[A-Za-z0-9._/-]{1,200}$")
#: ``owner/name``: a GitHub owner never starts with ``.`` (nor ``-``) and a name is never ``.`` or
#: ``..``, so a validated repository can never traverse an API path or a URL (``/repos/../..``).
#: Every repository-bearing grammar (``RUN_URL``, ``WORKFLOW_REF``) is built from these parts.
_OWNER = r"[A-Za-z0-9][A-Za-z0-9_-]{0,38}"
_NAME = r"(?!\.\.?(?![A-Za-z0-9_.-]))[A-Za-z0-9_.-]{1,100}"
_REPOSITORY = f"{_OWNER}/{_NAME}"
REPOSITORY = re.compile(f"^{_REPOSITORY}$")
VERSION = re.compile(r"^(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})$")
#: Opaque evidence identifiers: frame_id, capture_id, comparison_id, pair_id, reference ids.
IDENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}$")
ARTIFACT_NODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,79}$")
CI_UNIT_ID = re.compile(r"^(?!.*--)[a-z0-9][a-z0-9._-]{0,79}$")
CI_ENVELOPE_NAME = "ci-envelope.json"
CI_ARCHIVE_NAME = "ci-export.zip"
CI_GATE_NAME = "ci-gate.json"
CI_PLAN_NAME = "ci-plan.json"
CI_VALIDATION_NAME = "ci-validation.json"
CI_EXECUTION_NAME = "ci-execution.json"
CI_KIT_INSTALLATION_NAME = "ci-kit-installation.json"
CI_BOOTSTRAP_PROGRAM_NAME = "ci_privileged_bootstrap.py"
CI_ROOT_REQUEST_NAME = "ci-root-request.json"
CI_RUNTIME_ROOT_REQUEST_NAME = "ci-runtime-root-request.json"
CI_RUNTIME_FREEZE_OPERATION = "runtime-validation-v1"
MINECRAFT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,39}$")
LOADER = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
SCENARIO = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
ROLE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
STEP = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
REVIEW_TIER = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
PROFILE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")
VARIANT_ID = re.compile(r"^[a-z0-9](?:[a-z0-9]|[._-](?=[a-z0-9])){0,63}$")
NATIVE_KIND = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
CONTRACT_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
EXTENSION_NAME = re.compile(r"^[a-z0-9-]+\.[a-z0-9_]+$")
MAX_EXTENSION_NAME_LENGTH = 80
EVENT = re.compile(r"^[a-z_]{1,40}$")
WORKFLOW_PATH = re.compile(r"^\.github/workflows/[A-Za-z0-9._-]{1,100}\.ya?ml$")
RFC3339Z = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
#: The shapes the Actions API writes a time in: ``Z`` or a numeric offset, with or without a fraction.
ACTIONS_TIMESTAMP = re.compile(
    r"^(?P<second>[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})(?:\.[0-9]{1,9})?"
    r"(?P<zone>Z|[+-][0-9]{2}:[0-9]{2})$"
)
POSITIVE_DECIMAL = re.compile(r"^[1-9][0-9]{0,18}$")
RUN_URL = re.compile(f"^https://github\\.com/{_REPOSITORY}/actions/runs/[1-9][0-9]{{0,18}}$")
WORKFLOW_REF = re.compile(
    f"^(?P<repository>{_REPOSITORY})/"
    r"(?P<path>\.github/workflows/[A-Za-z0-9._-]{1,100}\.ya?ml)@refs/heads/(?P<branch>.{1,200})$"
)
#: A bundle-relative path: ASCII components, no ".", "..", empty or hidden-traversal component.
_PATH_COMPONENT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$|^\.[A-Za-z0-9_][A-Za-z0-9._-]{0,126}$")

ARTIFACT_PREFIX = "mb-"
PROMOTION_NAME = "mb-promotion"
PAGES_ARTIFACT_NAME = "github-pages"
KIT_STAMP_NAME = "MOD_BASE_KIT.json"

#: Artifact kind -> the exact first "--" segment of its name.
ARTIFACT_PREFIXES = {
    "handoff": "mb-handoff",
    "anchor": "mb-anchor",
    "family-handoff": "mb-family-handoff",
    "collected": "mb-collected",
    "collected-family": "mb-collected-family",
    "cache": "mb-cache",
    "family-cache": "mb-family-cache",
    "baseline": "mb-baseline",
}


def _fail(message: str) -> MbError:
    return MbError(message, reason="grammar")


def is_match(pattern: re.Pattern[str], value: object) -> bool:
    """Return True when ``value`` is a ``str`` fully matching ``pattern`` (never raises)."""

    return isinstance(value, str) and pattern.fullmatch(value) is not None


def require(pattern: re.Pattern[str], value: object, label: str) -> str:
    """Return ``value`` when it is a ``str`` fully matching ``pattern``; raise MbError otherwise."""

    if not is_match(pattern, value):
        raise _fail(f"{label} is not a valid identifier: {_preview(value)}")
    return value  # type: ignore[return-value]


def _preview(value: object) -> str:
    text = repr(value)
    return text if len(text) <= 80 else text[:77] + "..."


def is_key(value: object) -> bool:
    return is_match(KEY, value)


def is_family(value: object) -> bool:
    return is_match(FAMILY, value) and len(value) <= MAX_FAMILY_LENGTH  # type: ignore[arg-type]


def is_lane_id(value: object) -> bool:
    return is_match(LANE_ID, value) and len(value) <= MAX_LANE_ID_LENGTH  # type: ignore[arg-type]


def is_extension_name(value: object) -> bool:
    return is_match(EXTENSION_NAME, value) and len(value) <= MAX_EXTENSION_NAME_LENGTH  # type: ignore[arg-type]


def require_key(value: object, label: str = "key") -> str:
    return require(KEY, value, label)


def require_family(value: object, label: str = "family") -> str:
    if not is_family(value):
        raise _fail(f"{label} is not a valid family id: {_preview(value)}")
    return value  # type: ignore[return-value]


def require_sha1(value: object, label: str = "commit") -> str:
    return require(SHA1, value, label)


def require_positive_int(value: object, label: str, *, maximum: int = limits.MAX_RUN_ID) -> int:
    """Return a positive ``int`` (never ``bool``) no larger than ``maximum``."""

    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= maximum:
        raise _fail(f"{label} must be a positive integer no larger than {maximum}")
    return value


def is_bundle_path(value: object) -> bool:
    """True for a canonical bundle-relative POSIX path (no absolute, ``..``, ``.``, ``\\`` or NUL)."""

    if not isinstance(value, str) or not value or len(value) > limits.MAX_BUNDLE_PATH_CHARS:
        return False
    parts = value.split("/")
    if len(parts) > limits.MAX_BUNDLE_PATH_DEPTH:
        return False
    return all(_PATH_COMPONENT.fullmatch(part) is not None for part in parts)


CI_BATCH_BRANCH_PREFIX = 'batch/'
CI_RUNTIME_ENVELOPE_NAME = 'ci-runtime-envelope.json'
CI_RUNTIME_INPUT_FORMAT = 'mod-base.runtime-validation-input-v1'


def is_batch_branch(value: object) -> bool:
    """A bounded batch/* Git branch with no invalid ref components or final dot/slash."""
    return (is_match(BRANCH, value) and value.startswith(CI_BATCH_BRANCH_PREFIX)
            and not value.endswith(('/', '.'))
            and all(not part.startswith('.') and not part.endswith('.lock') for part in value.split('/')))


def is_repo_path(value: object) -> bool:
    """True for a repository-relative path: a bundle path none of whose components is ``.git``
    (compared case-insensitively, for case-insensitive filesystems)."""

    if not is_bundle_path(value):
        return False
    return all(part.lower() != ".git" for part in value.split("/"))  # type: ignore[union-attr]


#: One component of an export path: ASCII letters, digits, ``.``, ``_``, ``-``, ``+`` and single inner
#: spaces. It neither starts nor ends with a space or a dot.
_EXPORT_COMPONENT = re.compile(r"^[A-Za-z0-9_+-](?:(?:[A-Za-z0-9._+-]| (?! )){0,126}[A-Za-z0-9_+-])?$")


def is_export_path(value: object) -> bool:
    """True for a canonical path of a file a Build or runtime export holds.

    The length, depth and traversal rules are those of a bundle path; the component grammar is the
    wider one the mods' real file names need (``Quick Skin - Fabric - 1.20.1-3.1.0.jar``). No
    component can start with a dot, so no hidden file and no ``.git`` directory is ever named."""

    if not isinstance(value, str) or not value or len(value) > limits.MAX_BUNDLE_PATH_CHARS:
        return False
    parts = value.split("/")
    if len(parts) > limits.MAX_BUNDLE_PATH_DEPTH:
        return False
    return all(_EXPORT_COMPONENT.fullmatch(part) is not None for part in parts)


def parse_timestamp(value: object, label: str = "timestamp") -> datetime:
    """Parse a GitHub ``YYYY-MM-DDTHH:MM:SSZ`` timestamp into an aware UTC datetime."""

    if not is_match(RFC3339Z, value):
        raise _fail(f"{label} must be an RFC 3339 UTC timestamp ending in Z")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)  # type: ignore[arg-type]
    except ValueError as exc:
        raise _fail(f"{label} is not a real calendar time") from exc


def normalize_timestamp(value: object, label: str = "timestamp") -> str:
    """Return an Actions API time as whole-second UTC ``YYYY-MM-DDTHH:MM:SSZ``.

    Job, step and artifact times arrive as ``…Z``, with fractional seconds or with a numeric offset.
    The fraction is dropped and the offset applied, so the result is the one form the kit stores,
    and two results compare as text in time order. Anything else raises MbError."""

    match = ACTIONS_TIMESTAMP.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise _fail(f"{label} must be an RFC 3339 timestamp")
    zone = "+00:00" if match["zone"] == "Z" else match["zone"]
    try:
        moment = datetime.fromisoformat(match["second"] + zone).astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise _fail(f"{label} is not a real calendar time") from exc
    return (f"{moment.year:04d}-{moment.month:02d}-{moment.day:02d}"
            f"T{moment.hour:02d}:{moment.minute:02d}:{moment.second:02d}Z")


@dataclass(frozen=True)
class WorkflowRef:
    """A parsed ``GITHUB_WORKFLOW_REF`` (``owner/repo/.github/workflows/f.yml@refs/heads/b``)."""

    repository: str
    path: str
    branch: str


def parse_workflow_ref(value: object) -> WorkflowRef:
    """Parse a branch-scoped workflow ref; tags, pull refs and malformed values are rejected."""

    match = WORKFLOW_REF.fullmatch(value) if isinstance(value, str) else None
    if match is None or not is_match(BRANCH, match.group("branch")):
        raise _fail(f"workflow ref is not a branch-scoped workflow ref: {_preview(value)}")
    return WorkflowRef(match.group("repository"), match.group("path"), match.group("branch"))


def workflow_ref(repository: str, path: str, branch: str) -> str:
    """Build the exact ``GITHUB_WORKFLOW_REF`` value for a branch."""

    require(REPOSITORY, repository, "repository")
    require(WORKFLOW_PATH, path, "workflow path")
    require(BRANCH, branch, "branch")
    return f"{repository}/{path}@refs/heads/{branch}"


def run_url(repository: str, run_id: int) -> str:
    """Build a run URL. Renderers build every run URL with this; none is copied from a bundle."""

    require(REPOSITORY, repository, "repository")
    require_positive_int(run_id, "run id")
    return f"https://github.com/{repository}/actions/runs/{run_id}"


def short_sha(commit: str, length: int = 12) -> str:
    require_sha1(commit)
    if not 7 <= length <= 40:
        raise _fail("short SHA length must be between 7 and 40")
    return commit[:length]


# -- Artifact names -------------------------------------------------------------------------------


@dataclass(frozen=True)
class ArtifactName:
    """A parsed kit artifact name. Fields not carried by ``kind`` are ``None``."""

    kind: str
    name: str
    key: str | None = None
    family: str | None = None
    attempt: int | None = None
    commit: str | None = None
    run_id: int | None = None
    coverage_sha: str | None = None


def _attempt_text(attempt: int) -> str:
    return f"a{require_positive_int(attempt, 'run attempt', maximum=limits.MAX_RUN_ATTEMPT)}"


def _finish(name: str) -> str:
    if len(name.encode("utf-8")) > limits.MAX_ARTIFACT_NAME_BYTES:
        raise _fail("artifact name exceeds the GitHub name bound")
    return name


def handoff_name(key: str, attempt: int) -> str:
    return _finish(f"mb-handoff--{require_key(key)}--{_attempt_text(attempt)}")


def anchor_name(key: str, commit: str, run_id: int, attempt: int) -> str:
    return _finish(
        f"mb-anchor--{require_key(key)}--{require_sha1(commit)}--"
        f"{require_positive_int(run_id, 'run id')}--{_attempt_text(attempt)}"
    )


def family_handoff_name(family: str, key: str, attempt: int) -> str:
    return _finish(f"mb-family-handoff--{require_family(family)}--{require_key(key)}--{_attempt_text(attempt)}")


def collected_name(key: str) -> str:
    return _finish(f"mb-collected--{require_key(key)}")


def collected_family_name(family: str, key: str) -> str:
    return _finish(f"mb-collected-family--{require_family(family)}--{require_key(key)}")


def cache_name(key: str, coverage_sha: str) -> str:
    return _finish(f"mb-cache--{require_key(key)}--{require_sha1(coverage_sha, 'coverage SHA')}")


def family_cache_name(family: str, key: str, coverage_sha: str) -> str:
    return _finish(
        f"mb-family-cache--{require_family(family)}--{require_key(key)}--"
        f"{require_sha1(coverage_sha, 'coverage SHA')}"
    )


def baseline_name(key: str, commit: str, tested_run_id: int) -> str:
    return _finish(
        f"mb-baseline--{require_key(key)}--{require_sha1(commit)}--"
        f"{require_positive_int(tested_run_id, 'tested run id')}"
    )


def baseline_name_regex(key_pattern: str) -> str:
    """Return the full-match regex source for ``mb-baseline`` names whose key matches ``key_pattern``.

    QS keeps ``PUBLIC_BASELINE_NAME`` as a literal and its test compares it with
    ``baseline_name_regex(r"mc[0-9]+(?:\\.[0-9]+){1,2}")``.
    """

    if not isinstance(key_pattern, str) or not key_pattern or "\n" in key_pattern:
        raise _fail("key pattern must be a non-empty single-line regex")
    re.compile(key_pattern)
    return rf"^mb-baseline--{key_pattern}--[0-9a-f]{{40}}--[1-9][0-9]*$"


def _parse_attempt(text: str) -> int | None:
    if not text.startswith("a") or not POSITIVE_DECIMAL.fullmatch(text[1:]):
        return None
    value = int(text[1:])
    return value if value <= limits.MAX_RUN_ATTEMPT else None


def _parse_run_id(text: str) -> int | None:
    if not POSITIVE_DECIMAL.fullmatch(text):
        return None
    value = int(text)
    return value if value <= limits.MAX_RUN_ID else None


def parse_artifact_name(name: object) -> ArtifactName | None:
    """Parse a kit artifact name, or return ``None`` when it is not exactly a kit name.

    ``mb-promotion`` and ``github-pages`` are recognized; every other name must match one of the
    parameterized grammars in full. A ``None`` result means "not ours": callers that delete must
    treat it as untouchable.
    """

    if not isinstance(name, str) or len(name.encode("utf-8")) > limits.MAX_ARTIFACT_NAME_BYTES:
        return None
    if name == PROMOTION_NAME:
        return ArtifactName(kind="promotion", name=name)
    if name == PAGES_ARTIFACT_NAME:
        return ArtifactName(kind="pages", name=name)
    parts = name.split("--")
    prefix, fields = parts[0], parts[1:]
    parsed: ArtifactName | None = None
    if prefix == "mb-handoff" and len(fields) == 2 and is_key(fields[0]):
        attempt = _parse_attempt(fields[1])
        if attempt is not None:
            parsed = ArtifactName("handoff", name, key=fields[0], attempt=attempt)
    elif prefix == "mb-anchor" and len(fields) == 4 and is_key(fields[0]) and is_match(SHA1, fields[1]):
        run_id, attempt = _parse_run_id(fields[2]), _parse_attempt(fields[3])
        if run_id is not None and attempt is not None:
            parsed = ArtifactName("anchor", name, key=fields[0], commit=fields[1], run_id=run_id, attempt=attempt)
    elif prefix == "mb-family-handoff" and len(fields) == 3 and is_family(fields[0]) and is_key(fields[1]):
        attempt = _parse_attempt(fields[2])
        if attempt is not None:
            parsed = ArtifactName("family-handoff", name, key=fields[1], family=fields[0], attempt=attempt)
    elif prefix == "mb-collected" and len(fields) == 1 and is_key(fields[0]):
        parsed = ArtifactName("collected", name, key=fields[0])
    elif prefix == "mb-collected-family" and len(fields) == 2 and is_family(fields[0]) and is_key(fields[1]):
        parsed = ArtifactName("collected-family", name, key=fields[1], family=fields[0])
    elif prefix == "mb-cache" and len(fields) == 2 and is_key(fields[0]) and is_match(SHA1, fields[1]):
        parsed = ArtifactName("cache", name, key=fields[0], coverage_sha=fields[1])
    elif (prefix == "mb-family-cache" and len(fields) == 3 and is_family(fields[0]) and is_key(fields[1])
          and is_match(SHA1, fields[2])):
        parsed = ArtifactName("family-cache", name, key=fields[1], family=fields[0], coverage_sha=fields[2])
    elif prefix == "mb-baseline" and len(fields) == 3 and is_key(fields[0]) and is_match(SHA1, fields[1]):
        run_id = _parse_run_id(fields[2])
        if run_id is not None:
            parsed = ArtifactName("baseline", name, key=fields[0], commit=fields[1], run_id=run_id)
    return parsed


def require_artifact_name(name: object, kind: str) -> ArtifactName:
    """Parse ``name`` and require its kind; raise MbError for foreign or malformed names."""

    parsed = parse_artifact_name(name)
    if parsed is None or parsed.kind != kind:
        raise _fail(f"artifact name is not a valid {kind} name: {_preview(name)}")
    return parsed


def is_kit_artifact_name(name: object) -> bool:
    """True only for names the Pages pipeline may create, list, download or retire."""

    return parse_artifact_name(name) is not None


# Build artifacts have their own closed grammar. The Pages parser deliberately does not
# recognize them: Pages rotation must not acquire authority over these retained source bytes.
CI_ARTIFACT_PREFIXES = {
    "target": "mb-ci-target", "build": "mb-ci-build", "runtime": "mb-ci-runtime",
    "results": "mb-ci-results", "tested": "mb-ci-tested", "reuse": "mb-ci-reuse",
}


@dataclass(frozen=True)
class CIArtifactName:
    kind: str
    name: str
    run_id: int
    run_attempt: int
    unit_id: str | None = None


def ci_artifact_name(kind: str, run_id: int, run_attempt: int, unit_id: str | None = None) -> str:
    """Attempt-specific CI names; a name never establishes a tested identity by itself."""

    if not isinstance(kind, str) or kind not in CI_ARTIFACT_PREFIXES:
        raise _fail("unknown CI artifact kind")
    require_positive_int(run_id, "CI run id")
    require_positive_int(run_attempt, "CI attempt", maximum=limits.MAX_RUN_ATTEMPT)
    if kind in {"target", "runtime", "tested"}:
        require(CI_UNIT_ID, unit_id, "CI unit id")
        if kind == "tested" and unit_id not in {"build", "packaged"}:
            raise _fail("tested CI artifact must name build or packaged")
    elif unit_id is not None:
        raise _fail("aggregate CI artifact has no unit id")
    name = f"{CI_ARTIFACT_PREFIXES[kind]}--{run_id}--a{run_attempt}"
    return _finish(name + (f"--{unit_id}" if unit_id is not None else ""))


def parse_ci_artifact_name(name: object) -> CIArtifactName | None:
    if (not isinstance(name, str) or not name.isascii()
            or len(name) > limits.MAX_ARTIFACT_NAME_BYTES):
        return None
    parts = name.split("--")
    if len(parts) not in {3, 4}:
        return None
    prefixes = {prefix: kind for kind, prefix in CI_ARTIFACT_PREFIXES.items()}
    kind = prefixes.get(parts[0])
    run_id, attempt = _parse_run_id(parts[1]), _parse_attempt(parts[2])
    if kind is None or run_id is None or attempt is None:
        return None
    unit = parts[3] if len(parts) == 4 else None
    try:
        if ci_artifact_name(kind, run_id, attempt, unit) != name:
            return None
    except MbError:
        return None
    return CIArtifactName(kind, name, run_id, attempt, unit)

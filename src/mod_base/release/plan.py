"""``mod-base.release.plan`` v1: what one release publishes, derived from one tested generation.

:func:`derive_release_plan` decides the release of the live default-branch commit that a
``mod-base.ci.generation`` covers (``ci select-generation --with-build`` selected, authenticated and
published it, with the complete Build bundle the packaged gate tested under ``build/``):

* the released commit is the covered commit (``identity.tested_sha``, :func:`released_commit`);
  the Build bundle keeps the identity it was built under, which a reused generation's merged pull
  request gives it, and must be exactly the bundle the generation names (its envelope's binding and
  producer are those of the generation's ``build`` descriptor) with exactly the files its envelope
  inventories;
* every lane of the generation has exactly one ``production`` file in the envelope, no other lane
  has one, and the config lists exactly those lanes;
* the version is the value of ``version.key`` in the Java properties file ``version.file``
  (:func:`read_version`), and the changelog is its section of ``changelog.file``
  (:func:`read_changelog`);
* the tags, names and marketplace metadata are the config's templates rendered for that version
  (:func:`mod_base.release.config.release_layout`), and each tag holds its lanes' production JARs,
  hashed here once each (size, SHA-256, SHA-1, SHA-512), the SHA-256 and size equal to the
  envelope's, followed by the SBOMs of its targets: every envelope file of role ``sbom`` at
  ``targets/<target>/sbom/<file>`` joins the tag whose Minecraft version is ``<target>`` as
  ``<file>``, or, when one tag holds every lane (``tags.group`` ``all``), that tag as
  ``<target>-<file>``. An SBOM asset names no lane (``lane`` is null). Any other SBOM, and one
  whose target is the Minecraft version of no tag, is not published.

:func:`seal_release` writes the plan and copies of exactly the planned files into a new directory,
``release-plan.json`` and ``assets/<tag>/<filename>``; :func:`admit_release` accepts such a
directory only when it holds exactly those files with exactly those bytes. The plan's identity is
:func:`plan_digest`, the SHA-256 of its canonical bytes; it holds no hash of itself.
"""

from __future__ import annotations

import copy
import hashlib
import os
import re
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from mod_base.build_ci.protocol import validate_identity
from mod_base.build_ci.records import GENERATION_MODES, validate_build_envelope, validate_generation
from mod_base.errors import MbError
from mod_base.io.atomic_directory import atomic_directory, write_new
from mod_base.io.secure_json import loads
from mod_base.io.tree import EXPORT_PATHS, read_child_file, regular_files, stream_child_file, validate_tree_entries
from mod_base.model import grammar as g
from mod_base.model import limits as lim
from mod_base.model.canonical import canonical_json, canonical_sha256, sha256_hex
from mod_base.model.validators import Int, List, Map, Nullable, Obj, Str, Validator, check, is_display_text
from mod_base.release.config import (CURSEFORGE_RELATION_TYPES, CURSEFORGE_SLUG, LOADER_NAMES, MARKETPLACES,
                                     MAX_RELEASE_CURSEFORGE_PROJECT_ID, MAX_RELEASE_DEPENDENCIES,
                                     MAX_RELEASE_GAME_VERSIONS, MAX_RELEASE_LANES, MAX_RELEASE_LOADERS,
                                     MAX_RELEASE_MODRINTH_NAME_CHARS, MAX_RELEASE_TAG_CHARS, MAX_RELEASE_TEXT_CHARS,
                                     MAX_RELEASE_VERSION_CHARS, MODRINTH_DEPENDENCY_TYPES, MODRINTH_PROJECT,
                                     MODRINTH_VERSION_NUMBER, RELEASE_VERSION, VERSION_TYPES, is_tag, pointer_tokens,
                                     release_header, release_layout, validate_release_config)

# Bounds (they move to model/limits.py with the release component).
#: Bytes of the canonical plan.
MAX_RELEASE_PLAN_BYTES = 4 * lim.MIB
#: Bytes of one published file (a production JAR or an SBOM), read once while it is hashed.
MAX_RELEASE_ASSET_BYTES = 512 * lim.MIB
#: Bytes of every published file of one release together: they come from one Build export.
MAX_RELEASE_TOTAL_BYTES = lim.MAX_CI_EXPORT_TREE_BYTES
#: Tags of one release: at most one per lane.
MAX_RELEASE_TAGS = MAX_RELEASE_LANES
#: Files of one release, across its tags: a production JAR per lane and at most one SBOM per target.
MAX_RELEASE_ASSETS = 2 * MAX_RELEASE_LANES
#: Bytes of the properties or JSON file the version is read from.
MAX_RELEASE_VERSION_FILE_BYTES = lim.MIB
#: Bytes of the changelog file.
MAX_RELEASE_CHANGELOG_FILE_BYTES = 4 * lim.MIB
#: UTF-8 bytes of the released changelog section (Modrinth's changelog limit is 65,536 characters).
MAX_RELEASE_CHANGELOG_BYTES = 64 * lim.KIB

#: The file of a sealed release that holds its plan.
PLAN_NAME = "release-plan.json"
#: The directory of a sealed release that holds one directory of assets per tag.
ASSETS_DIRECTORY = "assets"
#: An SBOM a release publishes: ``targets/<target>/sbom/<file>`` of the Build export.
_SBOM_PATH = re.compile(r"^targets/(?P<target>[^/]+)/sbom/(?P<file>[^/]+)$")
#: Text no changelog may hold: it opens the ledger block of a release body (``release.ledger``).
LEDGER_MARKER = "<!-- mod-base-release-ledger:"
#: The heading of a released changelog section: ``## <version> (<YYYY-MM-DD>)``.
_HEADING = re.compile(r"^## (?P<version>\S+) \((?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})\)$")
#: Java properties: the physical line terminators and the whitespace around a key.
_LINE_BREAK = re.compile(r"\r\n|\r|\n")
_PROPERTY_SPACE = " \t\f"

SHA1 = Str(g.SHA1, max_len=40)
SHA256 = Str(g.SHA256, max_len=64)
SHA512 = Str(re.compile(r"^[0-9a-f]{128}$"), max_len=128)
LANE = Str(g.CI_UNIT_ID, max_len=80)
_MINECRAFT = Str(g.MINECRAFT, max_len=40)


class ReleasePlanError(MbError):
    """A release cannot be planned, sealed or admitted from these inputs (exit 2)."""

    default_reason = "release-plan"


def _tag(value: Any, path: str) -> str:
    check(isinstance(value, str) and len(value) <= MAX_RELEASE_TAG_CHARS and is_tag(value), path,
          "must be a Git tag of the release tag grammar")
    return value


def _version(value: Any, path: str) -> str:
    check(isinstance(value, str) and len(value) <= MAX_RELEASE_VERSION_CHARS
          and RELEASE_VERSION.fullmatch(value) is not None, path, "must be a release version")
    return value


def _changelog_text(value: Any, path: str) -> str:
    """Released changelog text: non-blank UTF-8 of at most :data:`MAX_RELEASE_CHANGELOG_BYTES`, no
    control character but newline and tab, no carriage return and no ledger marker."""

    check(isinstance(value, str) and bool(value.strip()), path, "must be non-blank text")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        size = -1
    check(0 < size <= MAX_RELEASE_CHANGELOG_BYTES, path,
          f"must be valid UTF-8 of at most {MAX_RELEASE_CHANGELOG_BYTES} bytes")
    check(all(character in "\n\t" or (ord(character) >= 32 and ord(character) != 127) for character in value),
          path, "holds a control character other than newline or tab")
    check(LEDGER_MARKER not in value, path, "holds the release ledger marker")
    check(value == value.strip("\n") and value.split("\n", 1)[0].strip() and value.rsplit("\n", 1)[-1].strip(),
          path, "must start and end with a non-blank line")
    return value


def _text(max_chars: int) -> Validator:
    def validate(value: Any, path: str) -> str:
        check(is_display_text(value, max_chars), path, f"must be one line of display text of at most {max_chars} characters")
        return value
    return validate


def _filename(value: Any, path: str) -> str:
    check(isinstance(value, str) and "/" not in value and g.is_export_path(value), path,
          "must be one export path component")
    return value


def _asset_path(value: Any, path: str) -> str:
    check(g.is_export_path(value) and value != g.CI_ENVELOPE_NAME, path,
          "must be an export path of the bundle other than its envelope")
    return value


_ASSET = Obj({"lane": Nullable(LANE), "path": _asset_path, "filename": _filename, "size": Int(1, MAX_RELEASE_ASSET_BYTES),
              "sha256": SHA256, "sha1": SHA1, "sha512": SHA512})
_TAG = Obj({"tag": _tag, "name": _text(MAX_RELEASE_TEXT_CHARS), "minecraft": Nullable(_MINECRAFT),
            "lanes": List(LANE, min_items=1, max_items=MAX_RELEASE_LANES, sorted_values=True),
            "assets": List(_ASSET, min_items=1, max_items=MAX_RELEASE_ASSETS)})
_LOADERS = List(Str(choices=tuple(LOADER_NAMES)), min_items=1, max_items=MAX_RELEASE_LOADERS, unique=True)
_GAME_VERSIONS = List(_MINECRAFT, min_items=1, max_items=MAX_RELEASE_GAME_VERSIONS, unique=True)
_MODRINTH = Obj({
    "project_id": Str(MODRINTH_PROJECT, max_len=64),
    "version_number": Str(MODRINTH_VERSION_NUMBER, max_len=32),
    "name": _text(MAX_RELEASE_MODRINTH_NAME_CHARS),
    "game_versions": _GAME_VERSIONS, "loaders": _LOADERS, "version_type": Str(choices=VERSION_TYPES),
    "dependencies": List(Obj({"project_id": Str(MODRINTH_PROJECT, max_len=64),
                              "dependency_type": Str(choices=MODRINTH_DEPENDENCY_TYPES)}),
                         max_items=MAX_RELEASE_DEPENDENCIES, unique_by=lambda item: item["project_id"].casefold()),
})
_CURSEFORGE = Obj({
    "project_id": Int(1, MAX_RELEASE_CURSEFORGE_PROJECT_ID),
    "display_name": _text(MAX_RELEASE_TEXT_CHARS),
    "release_type": Str(choices=VERSION_TYPES), "game_versions": _GAME_VERSIONS, "loaders": _LOADERS,
    "relations": List(Obj({"slug": Str(CURSEFORGE_SLUG, max_len=64), "type": Str(choices=CURSEFORGE_RELATION_TYPES)}),
                      max_items=MAX_RELEASE_DEPENDENCIES, unique_by=lambda item: item["slug"]),
})
_LANE = Obj({"tag": _tag}, {"modrinth": _MODRINTH, "curseforge": _CURSEFORGE})
_KIT = Obj({"sha": SHA1, "version": Str(g.VERSION, max_len=20), "tree_digest": Str(g.DIGEST, max_len=71)})
_PLAN = Obj({
    **release_header("mod-base.release.plan"),
    "identity": validate_identity,
    "generation_sha256": SHA256,
    "mode": Str(choices=GENERATION_MODES),
    "config_sha256": SHA256,
    "kit": _KIT,
    "version": _version,
    "changelog": Obj({"text": _changelog_text, "sha256": SHA256}),
    "tags": List(_TAG, min_items=1, max_items=MAX_RELEASE_TAGS, unique_by=lambda item: item["tag"]),
    "lanes": Map(g.CI_UNIT_ID, _LANE, min_items=1, max_items=MAX_RELEASE_LANES, max_key_len=80),
})


def github_asset_name(filename: str) -> str:
    """The name GitHub gives a release asset uploaded as ``filename``: every space becomes a period.
    Two files of one release must differ even after that, and without regard to case."""

    return filename.replace(" ", ".")


def _check_kit(kit: dict[str, Any], identity: dict[str, Any], path: str) -> None:
    """The kit that plans a release is the kit pinned at the released commit, which is the kit
    the covered identity names."""

    check(all(kit[key] == identity["kit"][key] for key in ("sha", "version", "tree_digest")), path,
          "is not the kit the released commit pins")


def validate_release_plan(document: Any, *, path: str = "$") -> dict[str, Any]:
    """The closed schema and every rule that ties its parts together: one tag per lane, every tag's
    assets one production JAR per lane in lane order, then its SBOMs by path (each named as the
    module describes), unique paths across the plan and unique file names within a tag (also as GitHub renames them and
    without regard to case), the same marketplaces and projects for every lane, unique Modrinth
    version numbers and the changelog hash. Structure only: :func:`derive_release_plan` and
    :func:`admit_release` bind the bytes."""

    _PLAN(document, path)
    identity = document["identity"]
    check(identity["pr_number"] == 0 and identity["head_branch"] == identity["base_branch"], f"{path}.identity",
          "a release publishes a commit of the protected default branch")
    _check_kit(document["kit"], identity, f"{path}.kit")
    changelog = document["changelog"]
    check(changelog["sha256"] == sha256_hex(changelog["text"].encode("utf-8")), f"{path}.changelog.sha256",
          "is not the SHA-256 of the changelog text")
    tags = [tag["tag"] for tag in document["tags"]]
    check(tags == sorted(tags), f"{path}.tags", "must be sorted by tag")
    lanes = document["lanes"]
    placed: dict[str, str] = {}
    paths: set[str] = set()
    total = 0
    count = 0
    for index, tag in enumerate(document["tags"]):
        here = f"{path}.tags[{index}]"
        names: set[str] = set()
        assets = tag["assets"]
        count += len(assets)
        jars, sboms = assets[:len(tag["lanes"])], assets[len(tag["lanes"]):]
        check([asset["lane"] for asset in jars] == tag["lanes"], f"{here}.assets",
              "must start with one production JAR per lane, in lane order")
        sbom_paths = [asset["path"] for asset in sboms]
        check(sbom_paths == sorted(set(sbom_paths)), f"{here}.assets", "must end with its SBOMs, sorted by path")
        for position, asset in enumerate(assets):
            where = f"{here}.assets[{position}]"
            if position < len(jars):
                check(asset["filename"] == asset["path"].rsplit("/", 1)[-1], f"{where}.filename",
                      "must be the last component of the path")
            else:
                sbom = _SBOM_PATH.fullmatch(asset["path"])
                check(asset["lane"] is None and sbom is not None, where,
                      "an SBOM names no lane and lies at targets/<target>/sbom/<file>")
                check(tag["minecraft"] is None or sbom["target"] == tag["minecraft"], f"{where}.path",
                      "is the SBOM of another target than the tag's")
                expected = sbom["file"] if tag["minecraft"] is not None else f"{sbom['target']}-{sbom['file']}"
                check(asset["filename"] == expected, f"{where}.filename", f"must be {expected!r}"[:200])
            folded = asset["path"].casefold()
            check(folded not in paths, f"{where}.path", "repeats a file of the plan")
            paths.add(folded)
            spelled = {asset["filename"].casefold(), github_asset_name(asset["filename"]).casefold()}
            check(not spelled & names, f"{where}.filename", "repeats a file name of the tag")
            names |= spelled
            total += asset["size"]
        for lane in tag["lanes"]:
            check(lane not in placed, f"{here}.lanes", f"lane {lane} is in two tags")
            placed[lane] = tag["tag"]
    check(total <= MAX_RELEASE_TOTAL_BYTES, f"{path}.tags", "the release exceeds its whole-byte bound")
    check(count <= MAX_RELEASE_ASSETS, f"{path}.tags", f"the release holds more than {MAX_RELEASE_ASSETS} files")
    check(set(placed) == set(lanes), f"{path}.lanes", "must describe exactly the lanes of the tags")
    published = None
    for lane_id, lane in sorted(lanes.items()):
        here = f"{path}.lanes.{lane_id}"
        check(lane["tag"] == placed[lane_id], f"{here}.tag", "is not the tag that holds the lane")
        current = tuple(marketplace for marketplace in MARKETPLACES if marketplace in lane)
        check(bool(current), here, "publishes to no marketplace")
        published = current if published is None else published
        check(current == published, here, "publishes to other marketplaces than the other lanes")
    for marketplace in published or ():
        projects = {lane[marketplace]["project_id"] for lane in lanes.values()}
        check(len(projects) == 1, f"{path}.lanes", f"names more than one {marketplace} project")
    if "modrinth" in (published or ()):
        numbers = [lane["modrinth"]["version_number"] for lane in lanes.values()]
        check(len(set(numbers)) == len(numbers), f"{path}.lanes", "repeats a Modrinth version number")
    check(len(canonical_json(document)) <= MAX_RELEASE_PLAN_BYTES, path,
          f"exceeds {MAX_RELEASE_PLAN_BYTES} canonical bytes")
    return document


def plan_digest(plan: dict[str, Any]) -> str:
    """The identity of a release plan: the SHA-256 of its canonical bytes."""

    return canonical_sha256(validate_release_plan(plan))


def released_commit(plan: dict[str, Any]) -> str:
    """The commit a validated plan releases, at which every tag is created."""

    return plan["identity"]["tested_sha"]


# -- Repository sources ---------------------------------------------------------------------------


def _unescape_key(raw: str, path: str) -> str:
    """A Java properties key as ``Properties.load`` decodes it (``\\uXXXX`` and ``\\x`` -> ``x``)."""

    result: list[str] = []
    index = 0
    while index < len(raw):
        character = raw[index]
        if character != "\\":
            result.append(character)
            index += 1
            continue
        check(index + 1 < len(raw), path, "has a key ending in a backslash")
        escaped = raw[index + 1]
        if escaped == "u":
            digits = raw[index + 2:index + 6]
            check(len(digits) == 4 and all(digit in "0123456789abcdefABCDEF" for digit in digits), path,
                  "has a malformed \\u escape in a key")
            result.append(chr(int(digits, 16)))
            index += 6
            continue
        result.append({"t": "\t", "n": "\n", "r": "\r", "f": "\f"}.get(escaped, escaped))
        index += 2
    return "".join(result)


def _logical_lines(text: str) -> list[tuple[str, bool]]:
    """The logical lines of a Java properties file that hold an entry: ``(line, continued)``, with
    leading whitespace removed and continuation lines joined as ``Properties.load`` joins them."""

    lines: list[tuple[str, bool]] = []
    current: list[str] | None = None
    for physical in _LINE_BREAK.split(text):
        stripped = physical.lstrip(_PROPERTY_SPACE)
        if current is None:
            if not stripped or stripped[0] in "#!":
                continue
            current = []
        trailing = len(stripped) - len(stripped.rstrip("\\"))
        if trailing % 2:
            current.append(stripped[:-1])
            continue
        current.append(stripped)
        lines.append(("".join(current), len(current) > 1))
        current = None
    if current is not None:
        lines.append(("".join(current), True))
    return lines


def _property_key(line: str) -> str:
    """The raw (still escaped) key of a logical line: up to the first unescaped ``=``, ``:`` or
    whitespace."""

    index = 0
    while index < len(line):
        if line[index] == "\\":
            index += 2
            continue
        if line[index] in "=:" or line[index] in _PROPERTY_SPACE:
            break
        index += 1
    return line[:index]


def read_version(repo: Path, version: dict[str, Any]) -> str:
    """The version of the release, from ``repo/<version.file>`` read through no symlink and
    matching :data:`mod_base.release.config.RELEASE_VERSION`: the string at ``version.pointer`` of
    a JSON file (:func:`_json_version`) or the value of ``version.key`` of a Java properties file.

    A properties file is read as ``Properties.load`` reads a stream (ISO-8859-1), through no symlink, and
    the key must be defined exactly once, by one plain line ``key=value`` (whitespace allowed
    around ``=``): never through an escape, another separator or a continuation line, which a
    reader other than Java could split differently. The value is the whole rest of the line and
    must match :data:`mod_base.release.config.RELEASE_VERSION`."""

    label = f"$.version.file ({version['file']})"
    raw = read_child_file(repo, version["file"], max_bytes=MAX_RELEASE_VERSION_FILE_BYTES)
    if version["format"] == "json":
        return _version(_json_version(raw, version["pointer"], label), label)
    found: list[str] = []
    for line, continued in _logical_lines(raw.decode("latin-1")):
        key = _property_key(line)
        if _unescape_key(key, label) != version["key"]:
            continue
        match = re.fullmatch(r"(?P<key>[^\\=: \t\f]+)[ \t\f]*=[ \t\f]*(?P<value>[^\\]*)", line)
        check(not continued and match is not None and match["key"] == version["key"], label,
              "must define the version key on one plain key=value line")
        found.append(match["value"])
    check(len(found) == 1, label, f"must define {version['key']!r} exactly once"[:200])
    return _version(found[0], label)


def _json_version(raw: bytes, pointer: str, label: str) -> Any:
    """The value at ``pointer`` of a strict JSON document (no duplicate key, no non-finite number):
    every token names an existing member of an object or, as a decimal without a leading zero, an
    element of an array, and the value is a string."""

    value = loads(raw, label=label, max_bytes=MAX_RELEASE_VERSION_FILE_BYTES)
    for token in pointer_tokens(pointer):
        if isinstance(value, dict):
            check(token in value, label, f"has no member at {pointer}"[:200])
            value = value[token]
        elif isinstance(value, list):
            check(re.fullmatch(r"0|[1-9][0-9]{0,8}", token) is not None and int(token) < len(value), label,
                  f"has no element at {pointer}"[:200])
            value = value[int(token)]
        else:
            raise ReleasePlanError(f"{label}: has no value at {pointer}"[:300])
    check(isinstance(value, str), label, f"must hold a string at {pointer}"[:200])
    return value


def read_changelog(repo: Path, changelog: dict[str, Any], version: str) -> str:
    """The section of ``version`` in the Markdown changelog ``repo/<changelog.file>``.

    The file is strict UTF-8 without a byte-order mark or a carriage return, read through no
    symlink. Its first ``## `` heading must be exactly ``## <version> (<YYYY-MM-DD>)`` with a real
    date; the section is every line after it up to the next ``## `` heading, without its leading
    and trailing blank lines, and must satisfy the plan's changelog text rule."""

    label = f"$.changelog.file ({changelog['file']})"
    raw = read_child_file(repo, changelog["file"], max_bytes=MAX_RELEASE_CHANGELOG_FILE_BYTES)
    check(not raw.startswith(b"\xef\xbb\xbf"), label, "starts with a byte-order mark")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ReleasePlanError(f"{label}: is not valid UTF-8") from error
    check("\r" not in text, label, "holds a carriage return: the changelog must use LF line endings")
    lines = text.split("\n")
    start = next((index for index, line in enumerate(lines) if line.startswith("## ")), None)
    check(start is not None, label, "has no release heading")
    heading = _HEADING.fullmatch(lines[start])
    check(heading is not None, label, "the first release heading must read '## <version> (<YYYY-MM-DD>)'")
    check(heading["version"] == version, label, f"the first release heading is not version {version}")
    try:
        date.fromisoformat(heading["date"])
    except ValueError as error:
        raise ReleasePlanError(f"{label}: the first release heading has no real date") from error
    end = next((index for index in range(start + 1, len(lines)) if lines[index].startswith("## ")), len(lines))
    section = lines[start + 1:end]
    while section and not section[0].strip():
        section.pop(0)
    while section and not section[-1].strip():
        section.pop()
    check(bool(section), label, f"the section of version {version} is empty")
    return _changelog_text("\n".join(section), label)


# -- The Build bundle -----------------------------------------------------------------------------


def _read_bundle(bundle: Path, generation: dict[str, Any]) -> dict[str, Any]:
    """The envelope of the complete Build bundle the generation names, after checking that the
    directory holds exactly the files it inventories with their sizes (their bytes are hashed
    where they are used)."""

    validate_tree_entries(bundle, max_entries=lim.MAX_CI_EXPORT_ENTRIES)
    raw = read_child_file(bundle, g.CI_ENVELOPE_NAME, max_bytes=lim.MAX_CI_ENVELOPE_BYTES)
    envelope = validate_build_envelope(loads(raw, label=g.CI_ENVELOPE_NAME, max_bytes=lim.MAX_CI_ENVELOPE_BYTES),
                                       path="$.envelope")
    check(raw == canonical_json(envelope), "$.envelope", "must be canonical JSON")
    check((envelope["scope"], envelope["target_id"]) == ("complete", None), "$.envelope.scope",
          "is not the complete Build bundle")
    descriptor = generation["build"]
    check(all(envelope[key] == descriptor[key] for key in ("identity", "plan_sha256", "profile")),
          "$.envelope", "is not the Build bundle of the generation")
    check(envelope["producer"] == {key: value for key, value in descriptor["producer"].items()
                                   if key != "upload_window"},
          "$.envelope.producer", "is not the producer of the generation's Build bundle")
    sizes = regular_files(bundle, max_files=lim.MAX_CI_EXPORT_FILES + 1,
                          max_total_bytes=lim.MAX_CI_EXPORT_TREE_BYTES + len(raw),
                          max_file_bytes=lim.MAX_CI_EXPORT_FILE_BYTES, rule=EXPORT_PATHS)
    expected = dict(sorted({**{file["path"]: file["size"] for file in envelope["files"]},
                            g.CI_ENVELOPE_NAME: len(raw)}.items()))
    check(sizes == expected, "$.bundle", "does not hold exactly the files of its envelope")
    return envelope


def _hash_file(root: Path, relative: str, *, max_bytes: int,
               write: Callable[[bytes], None] | None = None) -> dict[str, Any]:
    """Size, SHA-256, SHA-1 and SHA-512 of one export file, read once (and handed to ``write``)."""

    digests = (hashlib.sha256(), hashlib.sha1(), hashlib.sha512())

    def consume(chunk: bytes) -> None:
        for digest in digests:
            digest.update(chunk)
        if write is not None:
            write(chunk)

    size = stream_child_file(root, relative, max_bytes=max_bytes, consume=consume, rule=EXPORT_PATHS)
    return {"size": size, "sha256": digests[0].hexdigest(), "sha1": digests[1].hexdigest(),
            "sha512": digests[2].hexdigest()}


def _asset(bundle: Path, file: dict[str, Any], lane: str | None, filename: str) -> dict[str, Any]:
    check(file["size"] <= MAX_RELEASE_ASSET_BYTES, f"$.envelope.files ({file['path']})"[:200],
          "exceeds the release asset bound")
    observed = _hash_file(bundle, file["path"], max_bytes=file["size"])
    check((observed["size"], observed["sha256"]) == (file["size"], file["sha256"]),
          f"$.bundle ({file['path']})"[:200], "differs from its envelope")
    return {"lane": lane, "path": file["path"], "filename": filename, **observed}


def derive_release_plan(*, generation: dict[str, Any], bundle: Path, config: dict[str, Any], config_sha256: str,
                        repo: Path, kit: dict[str, Any]) -> dict[str, Any]:
    """The release plan of the commit ``generation`` covers (see the module).

    ``generation`` is the ``mod-base.ci.generation`` that ``ci select-generation --with-build``
    published and ``bundle`` the ``build/`` directory beside it; ``config`` is the protected release
    config and ``config_sha256`` the SHA-256 of its file; ``repo`` is the protected checkout of the
    released commit; ``kit`` is ``{sha, version, tree_digest}`` of the executing kit, which must be
    the kit the covered identity names."""

    validate_generation(generation, path="$.generation")
    validate_release_config(config, path="$.config")
    SHA256(config_sha256, "$.config_sha256")
    _KIT(kit, "$.kit")
    identity = generation["identity"]
    _check_kit(kit, identity, "$.kit")
    check(config["repository"] == identity["repository"], "$.config.repository",
          "is not the repository of the released commit")
    envelope = _read_bundle(bundle, generation)
    production: dict[str, dict[str, Any]] = {}
    sboms: list[tuple[str, str, dict[str, Any]]] = []
    for file in envelope["files"]:
        if file["role"] == "production":
            check(file["lane_id"] not in production, "$.envelope.files", f"lane {file['lane_id']} has two production JARs")
            production[file["lane_id"]] = file
        elif file["role"] == "sbom" and (sbom := _SBOM_PATH.fullmatch(file["path"])) is not None:
            sboms.append((sbom["target"], sbom["file"], file))
    planned = {lane["id"] for lane in generation["lanes"]}
    check(set(production) == planned, "$.envelope.files", "does not hold one production JAR for every planned lane "
          "and no other")
    check(set(config["lanes"]) == planned, "$.config.lanes", "does not list exactly the planned lanes")
    version = read_version(repo, config["version"])
    text = read_changelog(repo, config["changelog"], version)
    layout, lanes = release_layout(config, version, path="$.config")
    tags = []
    for tag, entry in layout.items():
        minecraft = entry["minecraft"]
        assets = [_asset(bundle, production[lane], lane, production[lane]["path"].rsplit("/", 1)[-1])
                  for lane in entry["lanes"]]
        for target, name, file in sboms:  # sorted by path, as the envelope is
            if minecraft is None:
                filename = f"{target}-{name}"
                _filename(filename, f"$.envelope.files ({file['path']})"[:200])
                assets.append(_asset(bundle, file, None, filename))
            elif target == minecraft:
                assets.append(_asset(bundle, file, None, name))
        tags.append({"tag": tag, "name": entry["name"], "minecraft": minecraft, "lanes": entry["lanes"],
                     "assets": assets})
    plan = {"kind": "mod-base.release.plan", "schema_version": 1, "identity": copy.deepcopy(identity),
            "generation_sha256": canonical_sha256(generation), "mode": generation["mode"],
            "config_sha256": config_sha256, "kit": dict(kit), "version": version,
            "changelog": {"text": text, "sha256": sha256_hex(text.encode("utf-8"))},
            "tags": tags, "lanes": lanes}
    return validate_release_plan(plan)


# -- The sealed release ---------------------------------------------------------------------------


def _assets(plan: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Every planned file with its path in a sealed release, ``assets/<tag>/<filename>``."""

    return [(f"{ASSETS_DIRECTORY}/{tag['tag']}/{asset['filename']}", asset)
            for tag in plan["tags"] for asset in tag["assets"]]


def _digests(asset: dict[str, Any]) -> dict[str, Any]:
    return {key: asset[key] for key in ("size", "sha256", "sha1", "sha512")}


def seal_release(output: Path, *, plan: dict[str, Any], bundle: Path) -> dict[str, Any]:
    """Publish ``output``, a new directory holding ``release-plan.json`` (the plan's canonical
    bytes) and a copy of every planned file of ``bundle`` at ``assets/<tag>/<filename>``. Every
    file is read once, its size and three digests checked while it is copied, and the whole stage
    is admitted (:func:`admit_release`) before the atomic, exclusive publication. Returns the plan."""

    validate_release_plan(plan)
    check(isinstance(output, Path) and not os.path.lexists(output), "$.output", "must be a new path")
    raw = canonical_json(plan)

    def writer(stage: Path, stage_fd: int) -> dict[str, Any]:
        write_new(stage_fd, PLAN_NAME, raw)
        for relative, asset in _assets(plan):
            # One file at a time: read once, hashed while read, written only once it matches.
            copy = bytearray()
            observed = _hash_file(bundle, asset["path"], max_bytes=asset["size"], write=copy.extend)
            check(observed == _digests(asset), f"$.bundle ({asset['path']})"[:200], "differs from the plan")
            write_new(stage_fd, relative, bytes(copy))
        os.fsync(stage_fd)
        check(admit_release(stage, plan_sha256=sha256_hex(raw)) == plan, "$.output",
              "the sealed release differs from the plan")
        return plan

    try:
        return atomic_directory(output, writer)
    except OSError as error:
        raise ReleasePlanError(f"cannot seal the release: {error.strerror or error}") from error


def admit_release(directory: Path, *, plan_sha256: str | None = None) -> dict[str, Any]:
    """The plan of a sealed release directory, after checking that the directory holds exactly
    ``release-plan.json`` in its canonical bytes and every planned asset with its planned size,
    SHA-256, SHA-1 and SHA-512: no other file or directory, no symlink, no special or hard-linked
    file. A consistent directory proves only itself: with ``plan_sha256`` (the digest the plan job
    published) the plan must also be exactly that plan."""

    validate_tree_entries(directory, max_entries=lim.MAX_CI_EXPORT_ENTRIES)
    raw = read_child_file(directory, PLAN_NAME, max_bytes=MAX_RELEASE_PLAN_BYTES)
    plan = validate_release_plan(loads(raw, label=PLAN_NAME, max_bytes=MAX_RELEASE_PLAN_BYTES))
    check(raw == canonical_json(plan), f"$.{PLAN_NAME}", "must be canonical JSON")
    if plan_sha256 is not None:
        check(sha256_hex(raw) == SHA256(plan_sha256, "$.plan_sha256"), "$.plan_sha256", "is not the sealed plan")
    assets = _assets(plan)
    # The root, the plan, the assets directory, one directory per tag and the assets: any other
    # entry, an empty directory included, exceeds the count.
    validate_tree_entries(directory, max_entries=3 + len(plan["tags"]) + len(assets))
    sizes = regular_files(directory, max_files=len(assets) + 1, max_total_bytes=MAX_RELEASE_TOTAL_BYTES + len(raw),
                          max_file_bytes=max(MAX_RELEASE_ASSET_BYTES, len(raw)), rule=EXPORT_PATHS)
    expected = dict(sorted({PLAN_NAME: len(raw), **{relative: asset["size"] for relative, asset in assets}}.items()))
    check(sizes == expected, "$.directory", "does not hold exactly the plan and its assets")
    for relative, asset in assets:
        check(_hash_file(directory, relative, max_bytes=asset["size"]) == _digests(asset), f"$.{relative}"[:200],
              "differs from the plan")
    check(read_child_file(directory, PLAN_NAME, max_bytes=MAX_RELEASE_PLAN_BYTES) == raw, f"$.{PLAN_NAME}",
          "changed while the release was admitted")
    return plan

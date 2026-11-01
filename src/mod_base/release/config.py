"""``mod-base.release.config`` v1: the protected release data of a mod.

The file is ``scripts/ci/mod-base-release.json`` in the protected checkout, strict JSON (written by
hand, so not necessarily canonical) of at most :data:`MAX_RELEASE_CONFIG_BYTES`. It names where the
version comes from (``version.format`` ``properties``: the key ``version.key`` of a Java properties
file; ``json``: the string at the RFC 6901 pointer ``version.pointer`` of a JSON file) and where the
changelog comes from, the deployment environment (``environment``) whose approval publishes and
which alone holds the marketplace tokens, how tags and release names are spelled, the marketplace
projects and, for every lane of the Build plan, what each marketplace is told about its production
JAR. It names no file, command, token, workflow or JAR: the bytes come from the tested generation
alone (:mod:`mod_base.release.plan`).

Templates are display text with ``{name}`` placeholders. A tag template, and the release name
spelled from it, knows ``{version}`` and, when the config makes one tag per Minecraft version
(``tags.group`` ``"minecraft"``), ``{minecraft}``, which it must then use; with one tag for every
lane (``"all"``) ``{minecraft}`` has no single value and is refused. A marketplace template is
rendered per lane and knows :data:`LANE_PLACEHOLDERS`: ``{version}``, the lane's ``{minecraft}``,
its first listed loader as ``{loader}`` (``neoforge``) and ``{loader_name}`` (``NeoForge``), the
lane id as ``{lane}`` and the lane's rendered ``{tag}``. Any other brace is refused. Every rendered
text is one line of display text of at most :data:`MAX_RELEASE_TEXT_CHARS` characters, a tag
matches :data:`TAG` (and Git's ref rules), a Modrinth version number :data:`MODRINTH_VERSION_NUMBER`.

:func:`validate_release_config` also renders every template of every lane for the version
:data:`SAMPLE_VERSION`, so a config that cannot produce a valid, unambiguous release is refused
when it is written, not only when a release is planned; :func:`release_layout` renders the real
version again when a plan is derived.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from mod_base import readable_schema_versions
from mod_base.build_ci.protocol import repo_path
from mod_base.io.secure_json import loads
from mod_base.model import grammar as g
from mod_base.model import limits as lim
from mod_base.model.canonical import read_regular_file, sha256_hex
from mod_base.model.validators import Const, Int, List, Map, Obj, OneOf, Str, Validator, check, is_display_text

def release_header(kind: str) -> dict[str, Validator]:
    """``kind`` and ``schema_version`` validators of a release document: the versions
    :func:`mod_base.readable_schema_versions` admits (N and N-1)."""

    readable = readable_schema_versions(kind)
    return {"kind": Const(kind), "schema_version": Int(min(readable), max(readable))}


#: How a changelog file holds a version's notes: ``section`` (the default), the section headed
#: ``## <version> (<date>)`` of one file that opens with it; ``file``, a whole file of its own,
#: whose path names ``{version}``.
CHANGELOG_FORMATS = ("section", "file")


def _changelog_file(value: Any, path: str) -> str:
    """A repository path, in which ``{version}`` may stand for one path component's text."""

    check(isinstance(value, str) and value.count("{version}") <= 1, path,
          "must be a repository path naming {version} at most once")
    return repo_path(value.replace("{version}", "0.0.0"), path) and value


def changelog_path(changelog: dict[str, Any], version: str) -> str:
    """The changelog file of ``version``: the config's path with ``{version}`` filled in."""

    return changelog["file"].replace("{version}", version)


#: Where a mod keeps its release config.
RELEASE_CONFIG_PATH = "scripts/ci/mod-base-release.json"

# Bounds (they move to model/limits.py with the release component).
#: Bytes of the config file.
MAX_RELEASE_CONFIG_BYTES = 256 * lim.KIB
#: Lanes of one config, the Build plan's own lane bound.
MAX_RELEASE_LANES = lim.MAX_CI_LANES
#: Loaders one lane's JAR declares.
MAX_RELEASE_LOADERS = 4
#: Minecraft versions one lane's JAR is published for.
MAX_RELEASE_GAME_VERSIONS = 64
#: Modrinth dependencies, and separately CurseForge relations, of one lane.
MAX_RELEASE_DEPENDENCIES = 32
#: Characters of a template and of every text rendered from one (release names, marketplace names).
MAX_RELEASE_TEXT_CHARS = 256
#: Characters of a rendered tag.
MAX_RELEASE_TAG_CHARS = 128
#: Characters of a Modrinth version number: Modrinth refuses longer ones.
MAX_RELEASE_MODRINTH_VERSION_NUMBER_CHARS = 32
#: Characters of a Modrinth version name: Modrinth refuses longer ones.
MAX_RELEASE_MODRINTH_NAME_CHARS = 64
#: Characters of the version key of the properties file.
MAX_RELEASE_PROPERTY_KEY_CHARS = 128
#: Characters of the JSON pointer to the version, and its reference tokens.
MAX_RELEASE_POINTER_CHARS = 256
MAX_RELEASE_POINTER_TOKENS = 16
#: Characters of a mod version.
MAX_RELEASE_VERSION_CHARS = 64
#: The largest CurseForge project id (a positive 32-bit integer).
MAX_RELEASE_CURSEFORGE_PROJECT_ID = 2**31 - 1

#: The version of a release, as the properties file holds it.
RELEASE_VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+){1,3}(?:[-+][0-9A-Za-z.-]+)?$")
#: The version every template is rendered with when a config is validated.
SAMPLE_VERSION = "1.0.0"
#: A Git tag the release creates. Beside the grammar, Git's ref rules: no ``..``, no final ``.`` and
#: no final ``.lock`` (:func:`is_tag`). A valid tag is also one export path component, the name of
#: its directory in a sealed release.
TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
#: A Modrinth version number: an ASCII subset of what Modrinth accepts, at most 32 characters.
MODRINTH_VERSION_NUMBER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,31}$")
#: A Modrinth project id or slug.
MODRINTH_PROJECT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,63}$")
#: The released project itself is named by its Modrinth id, the only name Modrinth answers with.
MODRINTH_ID = re.compile(r"^[A-Za-z0-9]{8}$")
#: The deployment environment that holds the mod's marketplace tokens and approves a publication.
ENVIRONMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
#: Environments the kit gives other credentials, which a release must never be approved in.
RESERVED_ENVIRONMENTS = ("github-pages", "mod-base-gate")
#: A CurseForge project slug.
CURSEFORGE_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
#: An RFC 6901 JSON pointer with at least one reference token (one ``/`` before each).
JSON_POINTER = re.compile(r"^(?:/(?:[^~/]|~[01])*)+$")
#: The formats of the file the version is read from.
VERSION_FORMATS = ("properties", "json")
#: The key of the version in the properties file.
PROPERTY_KEY = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")

#: The loaders a lane may declare, with the name ``{loader_name}`` renders.
LOADER_NAMES = {"fabric": "Fabric", "forge": "Forge", "neoforge": "NeoForge", "quilt": "Quilt"}
VERSION_TYPES = ("release", "beta", "alpha")
TAG_GROUPS = ("minecraft", "all")
MODRINTH_DEPENDENCY_TYPES = ("required", "optional", "incompatible", "embedded")
CURSEFORGE_RELATION_TYPES = ("requiredDependency", "optionalDependency", "embeddedLibrary", "incompatible", "tool")
#: The marketplaces a release publishes to, in the order every document lists them.
MARKETPLACES = ("modrinth", "curseforge")
#: What a tag template and a release name may name (``minecraft`` only for ``tags.group`` minecraft).
TAG_PLACEHOLDERS = ("version", "minecraft")
#: What a marketplace template may name.
LANE_PLACEHOLDERS = ("version", "minecraft", "loader", "loader_name", "lane", "tag")

_PLACEHOLDER = re.compile(r"\{([a-z_]{1,32})\}")


def is_tag(value: Any) -> bool:
    """A tag of :data:`TAG` that Git accepts as a ref name."""

    return (isinstance(value, str) and TAG.fullmatch(value) is not None and ".." not in value
            and not value.endswith(".") and not value.endswith(".lock"))


def placeholders(template: str, path: str = "$") -> tuple[str, ...]:
    """The placeholder names of ``template`` in order. Every ``{`` and ``}`` must belong to one."""

    check(isinstance(template, str), path, "must be a string")
    parts = _PLACEHOLDER.split(template)
    check(all("{" not in literal and "}" not in literal for literal in parts[0::2]), path,
          "holds a brace that is not a {placeholder}")
    return tuple(parts[1::2])


def _render(template: str, values: dict[str, str], path: str, *, max_chars: int = MAX_RELEASE_TEXT_CHARS) -> str:
    names = placeholders(template, path)
    unknown = sorted(set(names) - set(values))
    check(not unknown, path, f"names an unknown placeholder {{{unknown[0] if unknown else ''}}}")
    parts = _PLACEHOLDER.split(template)
    for index in range(1, len(parts), 2):
        value = values[parts[index]]
        check(isinstance(value, str), path, "placeholder values must be strings")
        parts[index] = value
    text = "".join(parts)
    check(is_display_text(text, max_chars), path,
          f"must render to one line of display text of at most {max_chars} characters")
    return text


def render(template: str, **values: str) -> str:
    """Render ``template`` with exactly the placeholders ``values`` provides; refuse an unknown
    placeholder, a stray brace and any text that is not one line of display text of at most
    :data:`MAX_RELEASE_TEXT_CHARS` characters."""

    return _render(template, values, "$")


def _template(value: Any, path: str) -> str:
    Str(max_len=MAX_RELEASE_TEXT_CHARS, text="display")(value, path)
    placeholders(value, path)
    return value


def _pair(name: Validator, kinds: tuple[str, ...]) -> Validator:
    """A ``[name, kind]`` array: a dependency and its type."""

    kind = Str(choices=kinds)

    def validate(value: Any, path: str) -> list[str]:
        check(isinstance(value, list) and len(value) == 2, path, "must be a [project, type] pair")
        name(value[0], f"{path}[0]")
        kind(value[1], f"{path}[1]")
        return value

    return validate


def _json_pointer(value: Any, path: str) -> str:
    """An RFC 6901 pointer to the version string: ``/`` before every token, ``~`` only as ``~0``
    or ``~1``, at least one and at most :data:`MAX_RELEASE_POINTER_TOKENS` tokens."""

    check(isinstance(value, str) and 1 < len(value) <= MAX_RELEASE_POINTER_CHARS
          and JSON_POINTER.fullmatch(value) is not None
          and len(value.split("/")) - 1 <= MAX_RELEASE_POINTER_TOKENS, path,
          "must be a JSON pointer such as /project/mod_version")
    return value


def pointer_tokens(pointer: str) -> list[str]:
    """The decoded reference tokens of a valid JSON pointer (``~1`` -> ``/``, then ``~0`` -> ``~``)."""

    return [token.replace("~1", "/").replace("~0", "~") for token in pointer.split("/")[1:]]


_MINECRAFT = Str(g.MINECRAFT, max_len=40)
_LANE = Obj({
    "minecraft": _MINECRAFT,
    "loaders": List(Str(choices=tuple(LOADER_NAMES)), min_items=1, max_items=MAX_RELEASE_LOADERS, unique=True),
    "game_versions": List(_MINECRAFT, min_items=1, max_items=MAX_RELEASE_GAME_VERSIONS, unique=True),
    "version_type": Str(choices=VERSION_TYPES),
}, {
    # Absent means none; present only when the config publishes to that marketplace.
    "modrinth_dependencies": List(_pair(Str(MODRINTH_PROJECT, max_len=64), MODRINTH_DEPENDENCY_TYPES),
                                  max_items=MAX_RELEASE_DEPENDENCIES, unique_by=lambda item: item[0].casefold()),
    "curseforge_relations": List(_pair(Str(CURSEFORGE_SLUG, max_len=64), CURSEFORGE_RELATION_TYPES),
                                 max_items=MAX_RELEASE_DEPENDENCIES, unique_by=lambda item: item[0]),
})
_CONFIG = Obj({
    **release_header("mod-base.release.config"),
    "repository": Str(g.REPOSITORY, max_len=201),
    "environment": Str(ENVIRONMENT, max_len=64),
    "version": OneOf(
        Obj({"file": repo_path, "format": Const("properties"),
             "key": Str(PROPERTY_KEY, max_len=MAX_RELEASE_PROPERTY_KEY_CHARS)}),
        Obj({"file": repo_path, "format": Const("json"), "pointer": _json_pointer}),
    ),
    "changelog": Obj({"file": _changelog_file}, {"format": Str(choices=CHANGELOG_FORMATS)}),
    "tags": Obj({"template": _template, "group": Str(choices=TAG_GROUPS)}),
    "release_name": _template,
    "lanes": Map(g.CI_UNIT_ID, _LANE, min_items=1, max_items=MAX_RELEASE_LANES, max_key_len=80),
}, {
    "modrinth": Obj({"project_id": Str(MODRINTH_ID, max_len=8), "version_number": _template,
                     "name": _template}),
    "curseforge": Obj({"project_id": Int(1, MAX_RELEASE_CURSEFORGE_PROJECT_ID), "display_name": _template}),
})


def _allowed(template: str, allowed: tuple[str, ...], required: tuple[str, ...], path: str) -> None:
    names = set(placeholders(template, path))
    unknown = sorted(names - set(allowed))
    check(not unknown, path, f"names an unknown placeholder {{{unknown[0] if unknown else ''}}}")
    missing = sorted(set(required) - names)
    check(not missing, path, f"must name {{{missing[0] if missing else ''}}}")


def marketplaces(config: dict[str, Any]) -> tuple[str, ...]:
    """The marketplaces a validated config publishes to, in :data:`MARKETPLACES` order."""

    return tuple(marketplace for marketplace in MARKETPLACES if marketplace in config)


def validate_release_config(document: Any, *, path: str = "$") -> dict[str, Any]:
    """The closed schema, the placeholders of every template and a rehearsal of every rendering
    at :data:`SAMPLE_VERSION` (:func:`release_layout`)."""

    _CONFIG(document, path)
    check(bool(marketplaces(document)), path, "must publish to Modrinth, CurseForge or both")
    check(document["environment"] not in RESERVED_ENVIRONMENTS, f"{path}.environment",
          "names an environment that holds another credential of the kit")
    per_version = "{version}" in document["changelog"]["file"]
    check(per_version == (document["changelog"].get("format", "section") == "file"), f"{path}.changelog",
          "a changelog file per version (format file) names {version} in its path, and only it does")
    group = document["tags"]["group"]
    tag_names = TAG_PLACEHOLDERS if group == "minecraft" else ("version",)
    _allowed(document["tags"]["template"], tag_names, tag_names, f"{path}.tags.template")
    _allowed(document["release_name"], tag_names, (), f"{path}.release_name")
    if "modrinth" in document:
        number = document["modrinth"]["version_number"]
        _allowed(number, LANE_PLACEHOLDERS, (), f"{path}.modrinth.version_number")
        # A version number names the version, itself or through the tag that names it.
        check(bool({"version", "tag"} & set(placeholders(number))), f"{path}.modrinth.version_number",
              "must name {version} or {tag}")
        _allowed(document["modrinth"]["name"], LANE_PLACEHOLDERS, (), f"{path}.modrinth.name")
    if "curseforge" in document:
        _allowed(document["curseforge"]["display_name"], LANE_PLACEHOLDERS, (), f"{path}.curseforge.display_name")
    for lane_id, lane in document["lanes"].items():
        here = f"{path}.lanes.{lane_id}"
        check(lane["minecraft"] in lane["game_versions"], f"{here}.game_versions",
              "must include the lane's own Minecraft version")
        for key, marketplace in (("modrinth_dependencies", "modrinth"), ("curseforge_relations", "curseforge")):
            check(key not in lane or marketplace in document, f"{here}.{key}",
                  f"names dependencies of a marketplace the config does not publish to ({marketplace})")
        if "modrinth" in document:
            check(all(dependency[0].casefold() != document["modrinth"]["project_id"].casefold()
                      for dependency in lane.get("modrinth_dependencies", ())),
                  f"{here}.modrinth_dependencies", "names the released project itself")
    release_layout(document, SAMPLE_VERSION, path=path)
    return document


def release_layout(config: dict[str, Any], version: str, *,
                   path: str = "$") -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Render the release of ``version`` from a structurally valid ``config``.

    Returns ``(tags, lanes)``: ``tags`` maps every tag to ``{"name", "minecraft", "lanes"}`` (the
    Minecraft version of a ``minecraft`` group, ``None`` for ``all``; lane ids sorted),
    ``lanes`` maps every lane id to ``{"tag"}`` plus, for each marketplace the config publishes to,
    the metadata that marketplace receives for the lane's production JAR. Refuses an invalid
    version, a template that renders an invalid tag, name or version number, two Minecraft
    versions that render one tag, two tags that render one release name and two lanes that render
    one Modrinth version number."""

    check(isinstance(version, str) and len(version) <= MAX_RELEASE_VERSION_CHARS
          and RELEASE_VERSION.fullmatch(version) is not None, f"{path}.version", "is not a release version")
    group = config["tags"]["group"]
    tags: dict[str, dict[str, Any]] = {}
    by_minecraft: dict[str, str] = {}
    lanes: dict[str, dict[str, Any]] = {}
    for lane_id in sorted(config["lanes"]):
        lane = config["lanes"][lane_id]
        tag_values = {"version": version, **({"minecraft": lane["minecraft"]} if group == "minecraft" else {})}
        tag = _render(config["tags"]["template"], tag_values, f"{path}.tags.template", max_chars=MAX_RELEASE_TAG_CHARS)
        check(is_tag(tag), f"{path}.tags.template", f"renders an invalid Git tag {tag!r}"[:200])
        name = _render(config["release_name"], tag_values, f"{path}.release_name")
        key = lane["minecraft"] if group == "minecraft" else ""
        check(by_minecraft.setdefault(key, tag) == tag, f"{path}.tags.template", "renders two tags for one group")
        entry = tags.setdefault(tag, {"name": name, "minecraft": key or None, "lanes": []})
        check(entry["name"] == name, f"{path}.release_name", "renders two names for one tag")
        entry["lanes"].append(lane_id)
        values = {"version": version, "minecraft": lane["minecraft"], "loader": lane["loaders"][0],
                  "loader_name": LOADER_NAMES[lane["loaders"][0]], "lane": lane_id, "tag": tag}
        metadata: dict[str, Any] = {"tag": tag}
        if "modrinth" in config:
            project = config["modrinth"]
            number = _render(project["version_number"], values, f"{path}.modrinth.version_number",
                             max_chars=MAX_RELEASE_MODRINTH_VERSION_NUMBER_CHARS)
            check(MODRINTH_VERSION_NUMBER.fullmatch(number) is not None, f"{path}.modrinth.version_number",
                  f"renders an invalid Modrinth version number {number!r}"[:200])
            metadata["modrinth"] = {
                "project_id": project["project_id"], "version_number": number,
                "name": _render(project["name"], values, f"{path}.modrinth.name",
                                max_chars=MAX_RELEASE_MODRINTH_NAME_CHARS),
                "game_versions": list(lane["game_versions"]), "loaders": list(lane["loaders"]),
                "version_type": lane["version_type"],
                "dependencies": [{"project_id": project_id, "dependency_type": kind}
                                 for project_id, kind in lane.get("modrinth_dependencies", ())],
            }
        if "curseforge" in config:
            project = config["curseforge"]
            metadata["curseforge"] = {
                "project_id": project["project_id"],
                "display_name": _render(project["display_name"], values, f"{path}.curseforge.display_name"),
                "release_type": lane["version_type"], "game_versions": list(lane["game_versions"]),
                "loaders": list(lane["loaders"]),
                "relations": [{"slug": slug, "type": kind} for slug, kind in lane.get("curseforge_relations", ())],
            }
        lanes[lane_id] = metadata
    names = [entry["name"] for entry in tags.values()]
    check(len(set(names)) == len(names), f"{path}.release_name", "renders one name for two tags")
    if "modrinth" in config:
        numbers = [metadata["modrinth"]["version_number"] for metadata in lanes.values()]
        check(len(set(numbers)) == len(numbers), f"{path}.modrinth.version_number",
              "renders one version number for two lanes")
    return dict(sorted(tags.items())), lanes


def load_release_config(path: Path) -> tuple[dict[str, Any], str]:
    """Read and validate the release config at ``path`` (a regular file that is no symlink; its
    ancestors are the caller's protected checkout). Returns the document and the SHA-256 of the
    file's exact bytes."""

    raw = read_regular_file(path, label=RELEASE_CONFIG_PATH, max_bytes=MAX_RELEASE_CONFIG_BYTES)
    document = validate_release_config(loads(raw, label=RELEASE_CONFIG_PATH, max_bytes=MAX_RELEASE_CONFIG_BYTES))
    return document, sha256_hex(raw)

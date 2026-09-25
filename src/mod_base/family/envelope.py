"""``mod-base.family.envelope`` creation and validation (MB4).

The envelope (``envelope.json``) sits at the root of each family handoff and cache next to the
mod's native bundle, which only the adapter understands. ``files`` is the exact inventory of the
native bundle (every file except ``envelope.json``); ``native.manifest_sha256`` names its
``manifest.json``; ``producer`` is the producing run's claim from the environment.

On top of ``documents.validate_family_envelope`` the kit requires, at creation and at every
validation:

* the family is configured and the envelope names this repository;
* the producer ran the family's configured ``producer.workflow`` at exactly the envelope subject
  (``producer.branch``/``producer.commit`` equal ``subject.branch``/``subject.commit``);
* the envelope covers exactly that checkout (``coverage_sha == subject.commit``): a producer can
  attest only the commit it ran at, so no envelope claims an unrelated coverage that collection
  would then publish without an ancestry proof;
* ``carried_from`` appears only for a family whose config allows ``carry_forward``; it names the
  earlier commit whose native evidence the producer carried forward to its checkout, and
  ``family collect`` re-proves it (R5) up to ``coverage_sha`` at every collection;
* the native bundle is a tree of at most ``limits.MAX_FAMILY_FILES`` non-empty regular files of at
  most ``limits.MAX_SOURCE_PNG_BYTES`` each (the family artifact extraction bounds) and at most
  the family's ``handoff_max_bytes`` in total, holding a root ``manifest.json`` whose strict JSON
  carries the recorded string ``kind`` and integer ``schema_version``;
* every native path stays a canonical bundle path below the collected ``source/`` directory (at
  most :data:`MAX_NATIVE_PATH_DEPTH` components and :data:`MAX_NATIVE_PATH_CHARS` characters) and
  no two paths, ``envelope.json`` included, collide under case folding at any prefix, so every
  handoff, cache and collected artifact stays extractable;
* ``envelope.json`` is canonical JSON (one document, one byte sequence, one hash).

Creation copies the native bundle byte for byte, hashing each file from the very bytes it writes,
and never writes ``carried_from``: a producer states the coverage it built, and carrying that
coverage forward is a collection-time decision the core re-proves (R5, :mod:`mod_base.family.paired`).
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from mod_base.errors import MbError
from mod_base.family import _git
from mod_base.family._common import SOURCE_DIRECTORY, fail, family_config, real_directory
from mod_base.io.atomic_directory import atomic_directory, write_new
from mod_base.io.tree import file_records, read_child_file, regular_files
from mod_base.model import grammar
from mod_base.model import limits as lim
from mod_base.model.canonical import canonical_json, sha256_hex, strict_loads
from mod_base.model.documents import BRANCH, RUN_CLAIM, SHA1, validate_family_envelope
from mod_base.model.validators import Obj
from mod_base.runtime import Invocation

OWNER = "MB4"
ENVELOPE_NAME = "envelope.json"
NATIVE_MANIFEST = "manifest.json"
#: The largest native file: the ``family-handoff``/``family-cache`` extraction entry bound.
MAX_NATIVE_FILE_BYTES = lim.MAX_SOURCE_PNG_BYTES
#: ``family collect`` re-emits every native file as ``source/<path>``, which must remain a canonical
#: bundle path (``grammar.is_bundle_path``): one component and ``len("source/")`` characters fewer.
MAX_NATIVE_PATH_DEPTH = lim.MAX_BUNDLE_PATH_DEPTH - 1
MAX_NATIVE_PATH_CHARS = lim.MAX_BUNDLE_PATH_CHARS - len(SOURCE_DIRECTORY) - 1
_SUBJECT_REF = Obj({"branch": BRANCH, "commit": SHA1})
REASON = "family-envelope"


def _fail(message: str, reason: str = REASON) -> MbError:
    return fail(message, reason)


def _native_identity(data: bytes) -> dict[str, Any]:
    """``{kind, schema_version}`` of a native ``manifest.json`` (strict JSON)."""

    manifest = strict_loads(data, label="the native manifest.json", max_bytes=lim.MAX_MANIFEST_BYTES)
    kind = manifest.get("kind") if isinstance(manifest, dict) else None
    version = manifest.get("schema_version") if isinstance(manifest, dict) else None
    if not isinstance(kind, str) or isinstance(version, bool) or not isinstance(version, int):
        raise _fail("the native manifest.json must be an object with a string kind and an integer schema_version")
    return {"kind": kind, "schema_version": version}


def _require_collectable_paths(paths: Iterable[str]) -> None:
    """Every native path fits below ``source/`` and no two paths (``envelope.json`` included) fold
    to the same name at any prefix, as the bounded extraction of every family artifact requires."""

    folded = {ENVELOPE_NAME.casefold(): ENVELOPE_NAME}
    for relative in paths:
        parts = relative.split("/")
        if len(parts) > MAX_NATIVE_PATH_DEPTH:
            raise _fail(f"native path {relative[:120]!r} has more than {MAX_NATIVE_PATH_DEPTH} components, leaving no "
                        f"room for the collected {SOURCE_DIRECTORY}/ directory")
        if len(relative) > MAX_NATIVE_PATH_CHARS:
            raise _fail(f"native path {relative[:120]!r} is longer than {MAX_NATIVE_PATH_CHARS} characters, leaving "
                        f"no room for the collected {SOURCE_DIRECTORY}/ directory")
        for depth in range(1, len(parts) + 1):
            prefix = "/".join(parts[:depth])
            prior = folded.setdefault(prefix.casefold(), prefix)
            if prior != prefix:
                raise _fail(f"native paths collide under case folding: {prior[:120]!r}, {prefix[:120]!r}")


def _check_envelope(invocation: Invocation, envelope: Any, config: Mapping[str, Any], *, family: str,
                    key: str | None) -> dict[str, Any]:
    """The structural schema plus the kit rules of the module docstring that need no file."""

    validate_family_envelope(envelope, max_total_bytes=config["handoff_max_bytes"])
    if envelope["family"] != family:
        raise _fail(f"the envelope wraps family {envelope['family']!r}, not {family!r}")
    if key is not None and envelope["key"] != key:
        raise _fail(f"the envelope covers key {envelope['key']!r}, not {key!r}")
    if envelope["repository"] != invocation.repository:
        raise _fail("the envelope names another repository than this run")
    producer, subject = envelope["producer"], envelope["subject"]
    if producer["workflow_path"] != config["producer"]["workflow"]:
        raise _fail(f"the envelope producer is not a {config['producer']['workflow']} run")
    if producer["branch"] != subject["branch"] or producer["commit"] != subject["commit"]:
        raise _fail("the envelope subject is not the producer run's own checkout")
    if envelope["coverage_sha"] != subject["commit"]:
        raise _fail(f"the envelope covers {envelope['coverage_sha']}, not its producer's checkout {subject['commit']}")
    if "carried_from" in envelope and not config["carry_forward"]:
        raise _fail(f"family {family!r} does not allow carry-forward, but its envelope records one",
                    reason="carry-forward")
    _require_collectable_paths(record["path"] for record in envelope["files"])
    return envelope


def create_envelope(invocation: Invocation, *, family: str, key: str, bundle_dir: Path, coverage_sha: str,
                    subject: Mapping[str, str], producer: Mapping[str, Any], output: Path) -> dict[str, Any]:
    """Copy the native bundle into the new ``output`` and write ``envelope.json`` beside it.

    ``subject`` is ``{branch, commit}``: the envelope's ``subject.tree`` is read from the checked-out
    repository, whose ``HEAD`` must equal ``commit``. The native bundle must be a bounded
    regular-file tree (at most the family's ``handoff_max_bytes``) holding ``manifest.json`` with a
    string ``kind`` and integer ``schema_version``. Returns the validated envelope."""

    config = family_config(invocation, family)
    grammar.require_key(key)
    grammar.require_sha1(coverage_sha, "coverage SHA")
    subject = _SUBJECT_REF(dict(subject), "$.subject")
    producer = RUN_CLAIM(dict(producer), "$.producer")
    if coverage_sha != subject["commit"]:
        raise _fail(f"the coverage SHA {coverage_sha} is not the subject commit {subject['commit']}: a producer "
                    "attests exactly its own checkout")
    commit, tree = _git.head(invocation.repo_root)
    if commit != subject["commit"]:
        raise _fail(f"the checked-out HEAD {commit} is not the subject commit {subject['commit']}")
    bundle = real_directory(bundle_dir, "native bundle", REASON)
    target = Path(os.path.abspath(output))
    if target == bundle or bundle in target.parents:
        raise _fail("the envelope output must lie outside the native bundle")
    maximum = config["handoff_max_bytes"]
    sizes = regular_files(bundle, max_files=lim.MAX_FAMILY_FILES, max_total_bytes=maximum,
                          max_file_bytes=MAX_NATIVE_FILE_BYTES)
    if ENVELOPE_NAME in sizes:
        raise _fail(f"the native bundle must not hold {ENVELOPE_NAME} (the envelope's own name)")
    if NATIVE_MANIFEST not in sizes:
        raise _fail(f"the native bundle has no root {NATIVE_MANIFEST}")
    _require_collectable_paths(sizes)
    header = {
        "kind": "mod-base.family.envelope",
        "schema_version": 1,
        "repository": invocation.repository,
        "family": family,
        "key": key,
        "kit": invocation.kit,
        "subject": {**subject, "tree": tree},
        "coverage_sha": coverage_sha,
        "producer": producer,
    }

    def writer(stage: Path, descriptor: int) -> dict[str, Any]:
        records: list[dict[str, Any]] = []
        native: dict[str, Any] = {}
        total = 0
        for relative, size in sizes.items():
            data = read_child_file(bundle, relative, max_bytes=MAX_NATIVE_FILE_BYTES)
            total += len(data)
            if len(data) != size or total > maximum:
                raise _fail(f"the native bundle changed while it was copied ({relative!r})")
            write_new(descriptor, relative, data)
            records.append({"path": relative, "sha256": sha256_hex(data), "size": len(data)})
            if relative == NATIVE_MANIFEST:
                native = {"manifest_path": NATIVE_MANIFEST, "manifest_sha256": records[-1]["sha256"],
                          **_native_identity(data)}
        envelope = _check_envelope(invocation, {**header, "native": native, "files": records}, config,
                                   family=family, key=key)
        document = canonical_json(envelope)
        if len(document) > lim.MAX_ENVELOPE_BYTES:
            raise _fail(f"{ENVELOPE_NAME} would exceed {lim.MAX_ENVELOPE_BYTES} bytes")
        write_new(descriptor, ENVELOPE_NAME, document)
        return validate_envelope_dir(invocation, stage, family=family, key=key)

    return atomic_directory(target, writer)


def validate_envelope_dir(invocation: Invocation, root: Path, *, family: str | None = None,
                          key: str | None = None) -> dict[str, Any]:
    """Validate ``root/envelope.json`` against the exact directory inventory and the configured
    family (``handoff_max_bytes``); return the envelope."""

    if key is not None:
        grammar.require_key(key)
    directory = real_directory(root, "family bundle", REASON)
    raw = read_child_file(directory, ENVELOPE_NAME, max_bytes=lim.MAX_ENVELOPE_BYTES)
    document = strict_loads(raw, label=ENVELOPE_NAME, max_bytes=lim.MAX_ENVELOPE_BYTES)
    if canonical_json(document) != raw:
        raise _fail(f"{ENVELOPE_NAME} is not canonical JSON")
    named = document.get("family") if isinstance(document, dict) else None
    if family is None:
        if not grammar.is_family(named):
            raise _fail(f"{ENVELOPE_NAME} names no valid family")
        family = named
    config = family_config(invocation, family)
    envelope = _check_envelope(invocation, document, config, family=family, key=key)
    maximum = config["handoff_max_bytes"]
    observed = file_records(directory, exclude=(ENVELOPE_NAME,), max_files=lim.MAX_FAMILY_FILES + 1,
                            max_total_bytes=maximum + lim.MAX_ENVELOPE_BYTES, max_file_bytes=MAX_NATIVE_FILE_BYTES)
    if observed != envelope["files"]:
        wanted = {record["path"] for record in envelope["files"]}
        present = {record["path"] for record in observed}
        detail = (f"missing={sorted(wanted - present)[:5]} extra={sorted(present - wanted)[:5]}"
                  if wanted != present else "a file hash or size differs")
        raise _fail(f"the family bundle inventory differs from its envelope ({detail})")
    manifest = read_child_file(directory, NATIVE_MANIFEST, max_bytes=MAX_NATIVE_FILE_BYTES)
    native = envelope["native"]
    if sha256_hex(manifest) != native["manifest_sha256"]:
        raise _fail(f"the native {NATIVE_MANIFEST} changed while it was validated")
    if _native_identity(manifest) != {"kind": native["kind"], "schema_version": native["schema_version"]}:
        raise _fail(f"the native {NATIVE_MANIFEST} kind or schema_version differs from its envelope")
    return envelope

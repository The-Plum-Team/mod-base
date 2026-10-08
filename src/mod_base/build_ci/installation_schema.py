"""Closed local root-installation record, independent of pin or execution authority."""

from __future__ import annotations

from typing import Any

from mod_base import KIT_REPOSITORY, readable_schema_versions
from mod_base.model import grammar, limits
from mod_base.model.validators import Const, Int, Obj, Str


_VERSIONS = readable_schema_versions("mod-base.ci.kit-installation")
_RECORD = Obj({
    "kind": Const("mod-base.ci.kit-installation"),
    "schema_version": Int(min(_VERSIONS), max(_VERSIONS)),
    "kit": Obj({"repository": Const(KIT_REPOSITORY), "sha": Str(grammar.SHA1, max_len=40),
                "version": Str(grammar.VERSION, max_len=20)}),
    "tree_digest": Str(grammar.DIGEST, max_len=71),
    "files": Int(1, limits.MAX_CI_KIT_INSTALL_FILES),
    "total_bytes": Int(0, limits.MAX_CI_KIT_INSTALL_BYTES),
    "device": Int(0, limits.MAX_CI_FILE_ID), "inode": Int(1, limits.MAX_CI_FILE_ID),
})


def validate_kit_installation(document: Any, *, path: str = "$") -> dict[str, Any]:
    """Data only; closed fields choose no program, path, hook, permissions or status."""
    _RECORD(document, path)
    return document

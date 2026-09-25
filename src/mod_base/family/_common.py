"""Private helpers shared by the family modules (MB4)."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

from mod_base.errors import MbError
from mod_base.model import grammar
from mod_base.runtime import Invocation

#: ``family collect`` re-emits the selected family generation below this directory of its output
#: (public as ``mod_base.family.paired.SOURCE_DIRECTORY``); the envelope keeps native paths short
#: enough to stay canonical bundle paths there.
SOURCE_DIRECTORY = "source"


def fail(message: str, reason: str) -> MbError:
    return MbError(message[:600], reason=reason)


def family_config(invocation: Invocation, family: str) -> dict[str, Any]:
    """The configured family ``family`` (a grammar-checked id), or :class:`MbError`."""

    return invocation.config.family(grammar.require_family(family))


def real_directory(path: Path, label: str, reason: str) -> Path:
    """``path`` made absolute; it must be an existing real directory (not a symlink)."""

    candidate = Path(os.path.abspath(path))
    try:
        info = candidate.lstat()
    except OSError as exc:
        raise fail(f"cannot inspect the {label}: {exc.strerror or exc}", reason) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise fail(f"the {label} must be a real directory", reason)
    return candidate

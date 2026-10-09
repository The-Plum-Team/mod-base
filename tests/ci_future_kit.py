"""Real released future-kit trees for the overlay and hosted generation regressions."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from mod_base.pin import Pin, kit_tree_digest
from tests.ci_lifecycle_fixture import git


def future_kit(source: Path, destination: Path, *, candidate_only: bool = False) -> tuple[Pin, str]:
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    init = destination / "src/mod_base/__init__.py"
    text = init.read_text(encoding="utf-8")
    text = re.sub(r'__version__ = "[^"]+"', '__version__ = "1.0.4"', text)
    text += "\nCANDIDATE_FUTURE_KIT = True\n"
    if candidate_only:
        text += ("import os as _future_os, pwd as _future_pwd\n"
                 "if _future_os.getuid() != _future_pwd.getpwnam('modbase_candidate').pw_uid:\n"
                 "    raise RuntimeError('future kit executed outside the candidate account')\n")
    init.write_text(text, encoding="utf-8", newline="\n")
    digest = kit_tree_digest(destination)
    for workflow in (destination / ".github/workflows").glob("*.yml"):
        text = workflow.read_text(encoding="utf-8")
        text = re.sub(r'MB_KIT_TREE_DIGEST: "sha256:[0-9a-f]{64}"', f'MB_KIT_TREE_DIGEST: "{digest}"', text)
        workflow.write_text(text, encoding="utf-8", newline="\n")
    git(destination, "init", "-q")
    if (source / ".git").is_dir():
        git(destination, "fetch", "-q", "--no-tags", source.as_uri(), "HEAD")
        git(destination, "reset", "--soft", "FETCH_HEAD")
    git(destination, "add", "-A")
    git(destination, "commit", "-q", "-m", "released future kit")
    sha = git(destination, "rev-parse", "HEAD")
    git(destination, "tag", "-a", "v1.0.4", "-m", "released future kit")
    return Pin(sha, "v1.0.4", ()), digest


def release(api: object, pin: Pin) -> None:
    api.add_compare(pin.sha, "main", {"status": "ahead", "behind_by": 0, "ahead_by": 1},
                    repository="The-Plum-Team/mod-base")
    api.add_ref("tags/" + pin.version, pin.sha, annotated_tag_sha="5" * 40,
                repository="The-Plum-Team/mod-base")

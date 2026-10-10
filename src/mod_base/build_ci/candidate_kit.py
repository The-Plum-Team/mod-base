"""Protected admission of a candidate's kit; future implementations remain inert bytes."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Any

from mod_base.build_ci.checkout import head_commit, read_objects
from mod_base.build_ci.source import parse_source_inventory, verify_source_copy
from mod_base.build_ci.worker_preparation import _tree_id
from mod_base.errors import MbError
from mod_base.github.api import GitHubApi
from mod_base.io.tree import read_child_file
from mod_base.model import limits
from mod_base.model.validators import check
from mod_base.pin import ACTIONS_DIR, Pin, kit_tree_digest, parse_pin, verify_released, verify_staged_files


def verified_checkout(checkout: Path, sha: str, tree: str | None = None) -> Path:
    """Bind regular checkout files to the exact commit, using only protected Git plumbing."""
    checkout = Path(os.path.abspath(checkout))
    commit = head_commit(checkout)
    check(commit.sha == sha and (tree is None or commit.tree == tree), "$.candidate_kit",
          "checkout commit differs from the admitted immutable commit")
    inventory = parse_source_inventory(read_objects(
        checkout, "ls-tree", "-r", "-l", "-z", "--full-tree", commit.tree,
        max_bytes=limits.MAX_CI_SOURCE_LIST_BYTES))
    check(_tree_id(inventory) == commit.tree, "$.candidate_kit", "checkout inventory differs from its commit tree")
    verify_source_copy(checkout, inventory=inventory)
    return checkout


def planned_pin(checkout: Path, subject: dict[str, Any], *, api: GitHubApi | None) -> dict[str, str] | None:
    """Parse every candidate pin; admit changed releases only in the run's first planning job.

    With ``api=None`` the caller must bind this result to an independently supplied expected plan
    hash, or perform release admission itself, before publishing outputs or staging anything.
    """
    pin = parse_pin(verified_checkout(checkout, subject["tested_sha"], subject["tested_tree"]))
    if (pin.sha, pin.version) == (subject["kit"]["sha"], "v" + subject["kit"]["version"]):
        return None
    if api is not None:
        verify_released(pin, api)
    return {"sha": pin.sha, "version": pin.version[1:]}


def verify_future_checkout(checkout: Path, pin: Pin) -> str:
    """Check the future commit, its kit-digest-v1 literal and both locks without importing it.

    The protected implementation deliberately refuses a future representation it cannot read.
    Such a change first needs a release teaching the protected verifier that representation.
    """
    checkout = verified_checkout(checkout, pin.sha)
    try:
        workflow = read_child_file(checkout / ".github/workflows", "build.yml",
                                   max_bytes=limits.MAX_CI_PLAN_SOURCE_BYTES).decode("utf-8")
        literals = re.findall(r'^  MB_KIT_TREE_DIGEST: "(sha256:[0-9a-f]{64})"$', workflow, re.MULTILINE)
        formats = set(re.findall(r'# >>> (kit-digest-v[0-9]+)\n', workflow))
        check(len(literals) == 1 and formats == {"kit-digest-v1"}, "$.candidate_kit",
              "future kit does not declare the supported kit-digest-v1 literal")
        digest = kit_tree_digest(checkout)
        check(digest == literals[0], "$.candidate_kit", "future kit tree differs from its own digest literal")
        check(stat.S_ISDIR((checkout / ACTIONS_DIR).lstat().st_mode), "$.candidate_kit",
              "future kit must supply the supported actions/ directory and its staged-file lock")
        verify_staged_files(checkout)
    except (MbError, OSError, UnicodeError) as error:
        raise MbError(f"cannot verify the candidate kit with the executing verifier: {error}; "
                      "a compatibility-first release is needed for another digest or lock format",
                      reason="candidate-kit") from error
    return digest

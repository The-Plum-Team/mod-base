"""Repository contents, Git objects and reachability reads (MB1).

Used for the kit binding of handoffs (the pin in ``config.source.workflow`` at the handoff run's
head), live head rechecks, enrolled-branch trees and ``compare/<kit_sha>...main`` reachability.
Every object read by id is re-hashed: a blob's Git object id is recomputed from its decoded bytes.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import urllib.parse
from typing import Any

from mod_base.errors import MbError
from mod_base.github.api import GitHubApi
from mod_base.model import grammar

OWNER = "MB1"

COMPARE_STATUSES = frozenset({"ahead", "behind", "identical", "diverged"})
TREE_MODES = frozenset({"100644", "100755", "040000", "120000", "160000"})
TREE_TYPES = frozenset({"blob", "tree", "commit"})
MAX_TREE_ENTRIES = 100_000
MAX_TREE_PATH_CHARS = 4096


def _fail(message: str) -> MbError:
    return MbError(message, reason="github-contents")


def _git_blob_id(data: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()


def _decode_base64(content: Any, label: str) -> bytes:
    """Strict base64 as the API sends it: newline-wrapped lines, no other whitespace."""

    if not isinstance(content, str) or any(character.isspace() and character not in "\r\n" for character in content):
        raise _fail(f"{label} base64 content is malformed")
    try:
        return base64.b64decode("".join(content.splitlines()), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise _fail(f"{label} is not canonical base64") from exc


def _ref_name(value: Any, label: str) -> str:
    """A commit SHA or a branch name, percent-encoded for a path segment."""

    if grammar.is_match(grammar.SHA1, value):
        return value
    return urllib.parse.quote(grammar.require(grammar.BRANCH, value, label), safe="")


def default_branch(api: GitHubApi) -> str:
    """``GET /repos/{repo}`` ``.default_branch`` (validated branch grammar)."""

    value = api.get_json(f"/repos/{api.repository}")
    if not isinstance(value, dict) or value.get("full_name") != api.repository:
        raise _fail("repository response does not name this repository")
    return grammar.require(grammar.BRANCH, value.get("default_branch"), "default branch")


def branch_head(api: GitHubApi, branch: str) -> tuple[str, str]:
    """``(commit, tree)`` of the live head of ``branch``."""

    grammar.require(grammar.BRANCH, branch, "branch")
    value = api.get_json(f"/repos/{api.repository}/branches/{urllib.parse.quote(branch, safe='')}")
    try:
        name = value["name"]
        commit = value["commit"]["sha"]
        tree = value["commit"]["commit"]["tree"]["sha"]
    except (KeyError, TypeError) as exc:
        raise _fail(f"branch {branch!r} response has no commit and tree") from exc
    if name != branch or not grammar.is_match(grammar.SHA1, commit) or not grammar.is_match(grammar.SHA1, tree):
        raise _fail(f"branch {branch!r} response is malformed or names another branch")
    return commit, tree


def commit_tree(api: GitHubApi, commit: str) -> str:
    """The tree SHA of ``commit`` (``/git/commits/{sha}``)."""

    grammar.require_sha1(commit, "commit")
    value = api.get_json(f"/repos/{api.repository}/git/commits/{commit}")
    try:
        sha = value["sha"]
        tree = value["tree"]["sha"]
    except (KeyError, TypeError) as exc:
        raise _fail(f"commit {commit} response has no tree") from exc
    if sha != commit or not grammar.is_match(grammar.SHA1, tree):
        raise _fail(f"commit {commit} response is malformed or names another commit")
    return tree


def file_at(api: GitHubApi, path: str, ref: str, *, max_bytes: int) -> bytes:
    """Bytes of ``path`` at commit ``ref`` (contents API, base64 decoded strictly, size and Git blob
    SHA re-verified)."""

    if not grammar.is_repo_path(path):
        raise _fail(f"repository path is not canonical: {path!r}"[:200])
    grammar.require_sha1(ref, "ref")
    _bound(max_bytes)
    value = api.get_json(f"/repos/{api.repository}/contents/{urllib.parse.quote(path, safe='/')}",
                         params={"ref": ref})
    if not isinstance(value, dict) or value.get("type") != "file" or value.get("path") != path:
        raise _fail(f"{path} at {ref} is not a file")
    size, oid = value.get("size"), value.get("sha")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0 or not grammar.is_match(grammar.SHA1, oid):
        raise _fail(f"{path} at {ref} has no valid size and blob id")
    if size > max_bytes:
        raise _fail(f"{path} at {ref} exceeds its {max_bytes}-byte bound")
    if value.get("encoding") == "base64":
        data = _decode_base64(value.get("content"), f"{path} at {ref}")
    elif value.get("encoding") == "none" and value.get("content") == "" and size > 0:
        data = blob(api, oid, max_bytes=max_bytes)  # the contents API omits files over 1 MiB
    else:
        raise _fail(f"{path} at {ref} has an unsupported encoding")
    if len(data) != size or _git_blob_id(data) != oid:
        raise _fail(f"{path} at {ref} does not match its size and blob id")
    return data


def _bound(max_bytes: Any) -> int:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise _fail("max_bytes must be a positive integer")
    return max_bytes


def _tree_entry(value: Any, index: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _fail(f"tree entry {index} is malformed")
    path, mode, kind, sha = value.get("path"), value.get("mode"), value.get("type"), value.get("sha")
    if (not isinstance(path, str) or not path or len(path) > MAX_TREE_PATH_CHARS or path.startswith("/")
            or any(part in {"", ".", ".."} for part in path.split("/")) or "\x00" in path):
        raise _fail(f"tree entry {index} has an unsafe path")
    # Type first: an unhashable API value (a list or object) is a malformed entry, not a TypeError.
    if (not isinstance(mode, str) or mode not in TREE_MODES or not isinstance(kind, str) or kind not in TREE_TYPES
            or not grammar.is_match(grammar.SHA1, sha)):
        raise _fail(f"tree entry {index} has an invalid mode, type or object id")
    entry = {"path": path, "mode": mode, "type": kind, "sha": sha}
    if "size" in value:
        size = value["size"]
        if kind != "blob" or isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise _fail(f"tree entry {index} has an invalid size")
        entry["size"] = size
    return entry


def tree(api: GitHubApi, sha: str, *, recursive: bool = True) -> list[dict[str, Any]]:
    """Entries of a Git tree; ``truncated`` must be false.

    ``sha`` may also be a commit (the API lists that commit's root tree, as ``tools/
    verify_action_tree.py`` reads a pin): a response naming another tree is accepted only when
    ``/git/commits/{sha}`` proves it is that commit's tree."""

    grammar.require_sha1(sha, "tree")
    value = api.get_json(f"/repos/{api.repository}/git/trees/{sha}", params={"recursive": 1} if recursive else None)
    if (not isinstance(value, dict) or not grammar.is_match(grammar.SHA1, value.get("sha"))
            or not isinstance(value.get("tree"), list)):
        raise _fail(f"tree {sha} response is malformed")
    if value["sha"] != sha and commit_tree(api, sha) != value["sha"]:
        raise _fail(f"tree {sha} response names a tree that is neither {sha} nor its commit's tree")
    if value.get("truncated") is not False:
        raise _fail(f"tree {sha} listing is truncated or does not say it is complete")
    rows = value["tree"]
    if len(rows) > MAX_TREE_ENTRIES:
        raise _fail(f"tree {sha} exceeds {MAX_TREE_ENTRIES} entries")
    entries = [_tree_entry(row, index) for index, row in enumerate(rows)]
    paths = [entry["path"] for entry in entries]
    if len(set(paths)) != len(paths):
        raise _fail(f"tree {sha} repeats a path")
    return entries


def blob(api: GitHubApi, oid: str, *, max_bytes: int) -> bytes:
    """A Git blob by id, base64-decoded, with its Git object id recomputed and compared."""

    grammar.require_sha1(oid, "blob")
    _bound(max_bytes)
    value = api.get_json(f"/repos/{api.repository}/git/blobs/{oid}")
    if not isinstance(value, dict) or value.get("sha") != oid or value.get("encoding") != "base64":
        raise _fail(f"blob {oid} response is malformed or names another object")
    size = value.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= max_bytes:
        raise _fail(f"blob {oid} size is outside 0..{max_bytes} bytes")
    content = value.get("content")
    if not isinstance(content, str) or len(content) > (max_bytes // 3 + 1) * 4 * 2 + 64:
        raise _fail(f"blob {oid} content exceeds its bound")
    data = _decode_base64(content, f"blob {oid}")
    if len(data) != size or _git_blob_id(data) != oid:
        raise _fail(f"blob {oid} object identity is stale")
    return data


def compare(api: GitHubApi, base: str, head: str, *, repository: str | None = None) -> dict[str, Any]:
    """``GET /repos/{repository or api.repository}/compare/{base}...{head}`` projected to
    ``{status, ahead_by, behind_by}`` with validated types."""

    owner = api.repository if repository is None else grammar.require(grammar.REPOSITORY, repository, "repository")
    left, right = _ref_name(base, "compare base"), _ref_name(head, "compare head")
    value = api.get_json(f"/repos/{owner}/compare/{left}...{right}")
    if not isinstance(value, dict):
        raise _fail("compare response is malformed")
    status, ahead, behind = value.get("status"), value.get("ahead_by"), value.get("behind_by")
    if (not isinstance(status, str) or status not in COMPARE_STATUSES
            or any(isinstance(count, bool) or not isinstance(count, int) or count < 0 for count in (ahead, behind))):
        raise _fail("compare response has an invalid status or counts")
    return {"status": status, "ahead_by": ahead, "behind_by": behind}

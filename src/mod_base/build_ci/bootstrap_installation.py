"""Fixed private bootstrap program bound to the admitted kit's staged tools lock (MB11).

Runs only from the old independently admitted protected kit. Never imports/executes the copied
program, installs interpreters or treats root-owned bytes as approval of their own provenance.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from mod_base import KIT_REPOSITORY
from mod_base.build_ci.handoff import _read_private_record
from mod_base.build_ci.host import HostBoundary, _canonical_path, _open_directory, authenticate_privileged_host_boundary
from mod_base.build_ci.inputs import _layout
from mod_base.build_ci.installation import KitInstallation, PRIVILEGED_KIT_ROOT, authenticate_privileged_kit
from mod_base.build_ci.worker import WORKER_ROOT, WorkerError
from mod_base.io.atomic_directory import atomic_directory, write_new
from mod_base.io.tree import authenticate_tree_private_access, read_child_file
from mod_base.model import grammar, limits
from mod_base.model.validators import Int, check
from mod_base.pin import STAGED_LOCK
from mod_base.runtime import Invocation


PRIVILEGED_BOOTSTRAP_ROOT = WORKER_ROOT / "privileged-bootstrap"


@dataclass(frozen=True)
class BootstrapInstallation:
    sha256: str
    size: int
    device: int
    inode: int


def _approved_program_digest() -> str:
    raw = read_child_file(Path(str(PRIVILEGED_KIT_ROOT)), STAGED_LOCK,
                          max_bytes=limits.MAX_CI_BOOTSTRAP_LOCK_BYTES)
    try:
        text = raw.decode("ascii")
    except UnicodeError as error:
        raise WorkerError("bootstrap tools lock is not ASCII") from error
    check(text.endswith("\n") and "\r" not in text, "$.bootstrap.lock", "tools lock must use canonical lines")
    rows = []
    for line in text[:-1].split("\n"):
        parts = line.split("  ./")
        check(len(parts) == 2, "$.bootstrap.lock", "malformed tools lock line")
        sha, path = parts
        grammar.require(grammar.SHA256, sha, "locked program hash")
        check(grammar.is_repo_path(path) and path.startswith(("template/", "tools/")),
              "$.bootstrap.lock", "tools lock path outside its roots")
        rows.append((path, sha))
    paths = [path for path, _ in rows]
    check(paths == sorted(set(paths)) and len({path.casefold() for path in paths}) == len(paths),
          "$.bootstrap.lock", "tools lock paths must be sorted and unique")
    selected = [sha for path, sha in rows if path == "tools/" + grammar.CI_BOOTSTRAP_PROGRAM_NAME]
    check(len(selected) == 1, "$.bootstrap.lock", "fixed bootstrap is not enrolled in the admitted kit")
    return selected[0]


def _identity(root: Path) -> tuple[int, int]:
    fd = _open_directory(tuple(_canonical_path(root.as_posix()).parts[1:]))
    try:
        info = os.fstat(fd)
        return info.st_dev, info.st_ino
    finally:
        os.close(fd)


def install_privileged_bootstrap(invocation: Invocation, *, boundary: HostBoundary,
                                  installation: KitInstallation) -> BootstrapInstallation:
    """Exclusively copy the fixed lock-bound program from a fenced protected source; never run it."""
    try:
        authenticate_privileged_host_boundary(boundary)
        authenticate_privileged_kit(installation, boundary=boundary)
        check(type(invocation) is Invocation, "$.bootstrap", "requires a protected invocation")
        check(invocation.kit == {"repository": KIT_REPOSITORY, "sha": installation.kit_sha,
                                 "version": installation.kit_version},
              "$.bootstrap", "invocation and admitted kit identity differ")
        source = invocation.kit_root
        check(isinstance(source, Path) and _canonical_path(boundary.home) in _canonical_path(source.as_posix()).parents,
              "$.bootstrap", "program source must be behind the runner home fence")
        digest = _approved_program_digest()
        relative = "tools/" + grammar.CI_BOOTSTRAP_PROGRAM_NAME
        raw = read_child_file(source, relative, max_bytes=limits.MAX_CI_BOOTSTRAP_BYTES)
        check(hashlib.sha256(raw).hexdigest() == digest, "$.bootstrap", "program differs from admitted tools lock")
        _layout(boundary)

        def fill(stage: Path, descriptor: int) -> BootstrapInstallation:
            write_new(descriptor, grammar.CI_BOOTSTRAP_PROGRAM_NAME, raw)
            leaf = os.open(grammar.CI_BOOTSTRAP_PROGRAM_NAME, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
            try:
                os.fchmod(leaf, 0o600)
                os.fsync(leaf)
            finally:
                os.close(leaf)
            authenticate_tree_private_access(stage, owner_uid=0, owner_gid=0, max_entries=limits.MAX_CI_PRIVATE_RECORD_ENTRIES)
            initial = _identity(stage)
            check(read_child_file(stage, grammar.CI_BOOTSTRAP_PROGRAM_NAME,
                    max_bytes=limits.MAX_CI_BOOTSTRAP_BYTES) == raw, "$.bootstrap", "private program bytes changed")
            check(read_child_file(source, relative, max_bytes=limits.MAX_CI_BOOTSTRAP_BYTES) == raw,
                  "$.bootstrap", "program source changed during installation")
            authenticate_privileged_kit(installation, boundary=boundary)
            check(_approved_program_digest() == digest, "$.bootstrap", "program lock changed")
            authenticate_tree_private_access(stage, owner_uid=0, owner_gid=0, max_entries=limits.MAX_CI_PRIVATE_RECORD_ENTRIES)
            check(_identity(stage) == initial, "$.bootstrap", "private program root changed")
            authenticate_privileged_host_boundary(boundary)
            _layout(boundary)
            return BootstrapInstallation(digest, len(raw), *initial)

        return atomic_directory(Path(str(PRIVILEGED_BOOTSTRAP_ROOT)), fill)
    except OSError as error:
        raise WorkerError("cannot install private privileged bootstrap") from error


def authenticate_privileged_bootstrap(program: BootstrapInstallation, *, boundary: HostBoundary,
                                       installation: KitInstallation) -> None:
    """Recheck original program root and bytes against the independently admitted kit lock."""
    try:
        authenticate_privileged_host_boundary(boundary)
        authenticate_privileged_kit(installation, boundary=boundary)
        check(type(program) is BootstrapInstallation, "$.bootstrap", "invalid program installation data")
        grammar.require(grammar.SHA256, program.sha256, "installed program hash")
        Int(1, limits.MAX_CI_BOOTSTRAP_BYTES)(program.size, "$.bootstrap.size")
        Int(0, limits.MAX_CI_FILE_ID)(program.device, "$.bootstrap.device")
        Int(1, limits.MAX_CI_FILE_ID)(program.inode, "$.bootstrap.inode")
        check(program.sha256 == _approved_program_digest(), "$.bootstrap", "program enrollment changed")
        _layout(boundary)
        root = Path(str(PRIVILEGED_BOOTSTRAP_ROOT))
        check(_identity(root) == (program.device, program.inode), "$.bootstrap", "program root identity changed")
        authenticate_tree_private_access(root, owner_uid=0, owner_gid=0, max_entries=limits.MAX_CI_PRIVATE_RECORD_ENTRIES)
        raw = _read_private_record(root, name=grammar.CI_BOOTSTRAP_PROGRAM_NAME, owner_uid=0, owner_gid=0,
                                  max_bytes=limits.MAX_CI_BOOTSTRAP_BYTES, label="bootstrap program")
        check((hashlib.sha256(raw).hexdigest(), len(raw)) == (program.sha256, program.size),
              "$.bootstrap", "private program bytes changed")
        authenticate_privileged_kit(installation, boundary=boundary)
        check(program.sha256 == _approved_program_digest(), "$.bootstrap", "program enrollment changed")
        authenticate_tree_private_access(root, owner_uid=0, owner_gid=0, max_entries=limits.MAX_CI_PRIVATE_RECORD_ENTRIES)
        check(_identity(root) == (program.device, program.inode)
              and _read_private_record(root, name=grammar.CI_BOOTSTRAP_PROGRAM_NAME, owner_uid=0, owner_gid=0,
                    max_bytes=limits.MAX_CI_BOOTSTRAP_BYTES, label="bootstrap program") == raw,
              "$.bootstrap", "private program changed during admission")
        authenticate_privileged_host_boundary(boundary)
    except OSError as error:
        raise WorkerError("cannot authenticate private privileged bootstrap") from error

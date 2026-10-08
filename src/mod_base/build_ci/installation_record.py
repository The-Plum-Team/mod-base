"""Fixed private root installation record; never a candidate or pin authority claim."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from mod_base import KIT_REPOSITORY, SCHEMA_VERSIONS
from mod_base.build_ci.handoff import _read_private_record
from mod_base.build_ci.host import HostBoundary, authenticate_privileged_host_boundary
from mod_base.build_ci.inputs import _layout
from mod_base.build_ci.installation import KitInstallation, authenticate_privileged_kit
from mod_base.build_ci.installation_schema import validate_kit_installation
from mod_base.build_ci.worker import WORKER_ROOT, WorkerError
from mod_base.io.atomic_directory import atomic_directory, write_new
from mod_base.io.secure_json import loads
from mod_base.io.tree import authenticate_tree_private_access, read_child_file
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json
from mod_base.model.validators import check


PRIVILEGED_KIT_RECORD_ROOT = WORKER_ROOT / "privileged-kit-record"


def record_privileged_kit_installation(installation: KitInstallation, *, boundary: HostBoundary) -> dict[str, Any]:
    """Root-only new record of a genuinely retained copy; independent pin provenance is required."""
    try:
        authenticate_privileged_host_boundary(boundary)
        authenticate_privileged_kit(installation, boundary=boundary)
        document = {"kind": "mod-base.ci.kit-installation",
            "schema_version": SCHEMA_VERSIONS["mod-base.ci.kit-installation"],
            "kit": {"repository": KIT_REPOSITORY, "sha": installation.kit_sha, "version": installation.kit_version},
            "tree_digest": installation.digest, "files": installation.files, "total_bytes": installation.total_bytes,
            "device": installation.device, "inode": installation.inode}
        validate_kit_installation(document)
        raw = canonical_json(document)
        check(len(raw) <= limits.MAX_CI_KIT_INSTALL_RECORD_BYTES, "$.installation", "installation record exceeds its local cap")
        _layout(boundary)

        def fill(stage: Path, descriptor: int) -> None:
            write_new(descriptor, grammar.CI_KIT_INSTALLATION_NAME, raw)
            leaf = os.open(grammar.CI_KIT_INSTALLATION_NAME, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
            try:
                os.fchmod(leaf, 0o600)
                os.fsync(leaf)
            finally:
                os.close(leaf)
            authenticate_tree_private_access(stage, owner_uid=0, owner_gid=0, max_entries=limits.MAX_CI_PRIVATE_RECORD_ENTRIES)
            check(read_child_file(stage, grammar.CI_KIT_INSTALLATION_NAME,
                    max_bytes=limits.MAX_CI_KIT_INSTALL_RECORD_BYTES) == raw,
                  "$.installation", "installation record changed during publication")
            authenticate_privileged_kit(installation, boundary=boundary)
            authenticate_tree_private_access(stage, owner_uid=0, owner_gid=0, max_entries=limits.MAX_CI_PRIVATE_RECORD_ENTRIES)
            authenticate_privileged_host_boundary(boundary)
            _layout(boundary)

        atomic_directory(Path(str(PRIVILEGED_KIT_RECORD_ROOT)), fill)
        return document
    except OSError as error:
        raise WorkerError("cannot publish private kit installation record") from error


def read_privileged_kit_installation(*, boundary: HostBoundary) -> KitInstallation:
    """Protected caller read/recheck, not a pre-import bootstrap or new pin authorization."""
    try:
        authenticate_privileged_host_boundary(boundary)
        _layout(boundary)
        root = Path(str(PRIVILEGED_KIT_RECORD_ROOT))
        authenticate_tree_private_access(root, owner_uid=0, owner_gid=0, max_entries=limits.MAX_CI_PRIVATE_RECORD_ENTRIES)
        raw = _read_private_record(root, name=grammar.CI_KIT_INSTALLATION_NAME, owner_uid=0, owner_gid=0,
                    max_bytes=limits.MAX_CI_KIT_INSTALL_RECORD_BYTES, label="kit installation record")
        document = loads(raw, label=grammar.CI_KIT_INSTALLATION_NAME,
                         max_bytes=limits.MAX_CI_KIT_INSTALL_RECORD_BYTES)
        validate_kit_installation(document)
        check(raw == canonical_json(document), "$.installation", "installation record must be canonical JSON")
        kit = document["kit"]
        installation = KitInstallation(kit["sha"], kit["version"], document["tree_digest"], document["files"],
                                       document["total_bytes"], document["device"], document["inode"])
        authenticate_privileged_kit(installation, boundary=boundary)
        authenticate_tree_private_access(root, owner_uid=0, owner_gid=0, max_entries=limits.MAX_CI_PRIVATE_RECORD_ENTRIES)
        check(_read_private_record(root, name=grammar.CI_KIT_INSTALLATION_NAME, owner_uid=0, owner_gid=0,
                max_bytes=limits.MAX_CI_KIT_INSTALL_RECORD_BYTES, label="kit installation record") == raw,
              "$.installation", "installation record changed during kit reauthentication")
        authenticate_privileged_host_boundary(boundary)
        return installation
    except OSError as error:
        raise WorkerError("cannot read private kit installation record") from error

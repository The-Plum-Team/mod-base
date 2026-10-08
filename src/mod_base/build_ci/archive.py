"""Bounded local ZIP encoding of independently sealed exports (MB11), never upload authority."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
import zipfile
from pathlib import Path
from typing import Any, BinaryIO

from mod_base.build_ci.exports import verify_build_export
from mod_base.errors import MbError
from mod_base.io.atomic_directory import atomic_directory
from mod_base.io.bounded_zip import extract_build
from mod_base.io.tree import sha256_file, stream_child_file
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json
from mod_base.model.validators import check


class _CappedStream:
    """Seekable ZIP sink with the compressed ceiling applied before every physical write."""

    def __init__(self, stream: BinaryIO) -> None:
        self.stream = stream

    def tell(self) -> int:
        return self.stream.tell()

    def seek(self, offset: int, whence: int = 0) -> int:
        return self.stream.seek(offset, whence)

    def write(self, data: bytes) -> int:
        check(self.tell() + len(data) <= limits.MAX_CI_BUNDLE_COMPRESSED_BYTES,
              "$.archive.size", "encoded Build ZIP exceeds compressed admission cap")
        remaining = memoryview(data)
        while remaining:
            written = self.stream.write(remaining)
            check(type(written) is int and 0 < written <= len(remaining),
                  "$.archive", "archive stream made invalid write progress")
            remaining = remaining[written:]
        return len(data)

    def flush(self) -> None:
        self.stream.flush()


def _archive_stream(stage_fd: int) -> BinaryIO:
    descriptor = os.open(grammar.CI_ARCHIVE_NAME,
        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        0o600, dir_fd=stage_fd)
    try:
        return os.fdopen(descriptor, "w+b", buffering=0)
    except BaseException:
        os.close(descriptor)
        raise


def encode_build_export(root: Path, output: Path, *, plan: dict[str, Any]) -> dict[str, Any]:
    """Publish one local stored ZIP after source/stream/extraction round-trip validation.

    Output is a private directory containing only CI_ARCHIVE_NAME. It is not an artifact
    descriptor, native receipt or an instruction to upload this ZIP as a nested GitHub artifact.
    Caller owns protected ancestors and excludes both worker UIDs; source must be quiescent.
    """

    try:
        source_path, output_path = root.resolve(strict=True), output.resolve(strict=False)
        check(source_path != output_path and source_path not in output_path.parents
              and output_path not in source_path.parents, "$.output", "archive input/output roots overlap")
    except (OSError, RuntimeError) as exc:
        raise MbError("cannot resolve private archive roots") from exc
    expected = verify_build_export(root, plan=plan)
    raw = canonical_json(expected)
    records = [{key: f[key] for key in ("path", "size", "sha256")} for f in expected["files"]]
    records.append({"path": grammar.CI_ENVELOPE_NAME, "size": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest()})
    records.sort(key=lambda record: record["path"])

    def writer(stage: Path, stage_fd: int) -> dict[str, Any]:
        with _archive_stream(stage_fd) as stream:
            with zipfile.ZipFile(_CappedStream(stream), "w", compression=zipfile.ZIP_STORED,
                                 allowZip64=False) as package:
                for record in records:
                    info = zipfile.ZipInfo(record["path"], date_time=(1980, 1, 1, 0, 0, 0))
                    info.create_system = 3
                    info.external_attr = (stat.S_IFREG | 0o644) << 16
                    info.file_size = record["size"]
                    digest = hashlib.sha256()
                    with package.open(info, "w") as entry:
                        def consume(chunk: bytes) -> None:
                            digest.update(chunk)
                            check(entry.write(chunk) == len(chunk), "$.archive", "ZIP entry write was incomplete")
                        size = stream_child_file(root, record["path"], max_bytes=record["size"], consume=consume)
                    check(size == record["size"] and digest.hexdigest() == record["sha256"],
                          "$.archive.source", "export bytes changed during archive encoding")
            stream.flush()
            os.fsync(stream.fileno())
            size = os.fstat(stream.fileno()).st_size
        archive = stage / grammar.CI_ARCHIVE_NAME
        with tempfile.TemporaryDirectory(prefix=".verify-", dir=stage) as temporary:
            extracted = Path(temporary) / "export"
            extract_build(archive, extracted)
            check(verify_build_export(extracted, plan=plan) == expected,
                  "$.archive", "encoded archive differs from independently admitted export")
        check(verify_build_export(root, plan=plan) == expected,
              "$.archive.source", "source export changed during archive verification")
        return {"path": grammar.CI_ARCHIVE_NAME, "size": size,
                "sha256": sha256_file(archive, max_bytes=limits.MAX_CI_BUNDLE_COMPRESSED_BYTES)}

    try:
        return atomic_directory(output, writer)
    except (OSError, ValueError, OverflowError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise MbError("cannot encode private Build archive") from exc

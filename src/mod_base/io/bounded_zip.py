"""Bounded ZIP extraction (MB1).

Union of Quick Skin ``scripts/ci/bounded_zip.py`` and Block Pops ``download_artifact`` rules:
stored or deflate entries only; no encrypted, symlink, special, absolute, ``..``, backslash or
duplicate entries; no entry outside the archive or sharing bytes with another; directory entries
only as parents; per-entry and total expanded-size caps, an entry-count cap (never above
``limits.MAX_ZIP_ENTRIES``) and a compression ratio of at most 200, all checked from the central
directory before any byte is inflated and again while streaming. Whatever :mod:`zipfile` raises
for a hostile archive surfaces as :class:`ZipRejected`. Extraction publishes through
:func:`mod_base.io.atomic_directory.atomic_directory`, so ``destination`` must not exist.

Every file name must also be a canonical bundle path (:func:`mod_base.model.grammar.is_bundle_path`,
the grammar every manifest inventory uses), NFC-normalized, and unique even under case folding
(including every parent component), so an archive can never create two names a case-insensitive
filesystem would merge. Pages/Build refuse empty files; the fixed runtime route preserves logs
and requires subsequent envelope/native role admission. A ``Path`` archive is
read through one ``O_NOFOLLOW`` descriptor bound to the regular file that was inspected. The
central directory itself is bounded from its end record before :mod:`zipfile` allocates one
object per entry, so a directory of millions of tiny entries is refused without being parsed.
"""

from __future__ import annotations

import io
import os
import stat
import struct
import unicodedata
import zipfile
import zlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import BinaryIO

from mod_base.errors import MbError
from mod_base.io import atomic_directory as atomic
from mod_base.model import grammar, limits

OWNER = "MB1"

#: MS-DOS "directory" attribute in the low byte of ``external_attr``.
_DOS_DIRECTORY = 0x10
#: General-purpose flag bits: 0 = encrypted, 6 = strong encryption, 13 = masked local headers.
_ENCRYPTION_FLAGS = 0x1 | 0x40 | 0x2000
_MAX_NAME_BYTES = 512
#: Slack over ``max_total_bytes`` a ``Path`` archive may carry (headers, directory, deflate overhead).
_ARCHIVE_SLACK_BYTES = 1 << 20
_CHUNK = 1 << 20
#: End-of-central-directory records as :mod:`zipfile` reads them (APPNOTE 4.3.14-4.3.16).
_EOCD = struct.Struct("<4s4H2LH")
_EOCD_SIGNATURE = b"PK\x05\x06"
_ZIP64_LOCATOR = struct.Struct("<4sLQL")
_ZIP64_LOCATOR_SIGNATURE = b"PK\x06\x07"
_ZIP64_EOCD = struct.Struct("<4sQ2H2L4Q")
_ZIP64_EOCD_SIGNATURE = b"PK\x06\x06"
#: The central-directory bytes one accepted entry may occupy: the fixed header, a name within
#: ``_MAX_NAME_BYTES`` and a generous allowance for extra fields (timestamps, Zip64 sizes).
_MAX_CENTRAL_ENTRY_BYTES = 46 + _MAX_NAME_BYTES + 1024
#: The fixed part of a local file header (APPNOTE 4.3.7), which precedes every entry's data.
_LOCAL_HEADER_BYTES = 30
#: What :mod:`zipfile` raises for a hostile archive besides ``OSError``: a "version needed to
#: extract" above 6.3 (``NotImplementedError``) and offsets beyond ``ssize_t`` (``OverflowError``)
#: included.
_MALFORMED = (EOFError, ValueError, OverflowError, NotImplementedError, struct.error, zipfile.BadZipFile,
              zipfile.LargeZipFile)


class ZipRejected(MbError):
    """An archive violates the extraction policy (exit 2)."""

    default_reason = "zip-rejected"


@dataclass(frozen=True)
class ExtractionLimits:
    """Bounds for one archive. ``suffixes`` (when set) restricts every file name's extension."""

    max_entries: int
    max_total_bytes: int
    max_entry_bytes: int
    max_ratio: int = limits.MAX_ZIP_RATIO
    suffixes: frozenset[str] | None = None


#: Limits per downloaded artifact kind (see ``model.grammar.ARTIFACT_PREFIXES``).
LIMITS_BY_KIND: dict[str, ExtractionLimits] = {
    "handoff": ExtractionLimits(limits.MAX_HANDOFF_FILES + 1, limits.MAX_RAW_BUNDLE_BYTES,
                                limits.MAX_SOURCE_PNG_BYTES, suffixes=frozenset({".json", ".png"})),
    "anchor": ExtractionLimits(limits.MAX_ANCHOR_FILES + 1, limits.MAX_ANCHOR_BUNDLE_BYTES,
                               limits.MAX_SOURCE_PNG_BYTES, suffixes=frozenset({".json", ".png"})),
    "cache": ExtractionLimits(limits.MAX_COMPACT_FILES + 1, limits.MAX_COMPACT_BUNDLE_BYTES,
                              limits.MAX_EXPECTATION_BYTES, suffixes=frozenset({".json", ".webp"})),
    "collected": ExtractionLimits(limits.MAX_COMPACT_FILES + 1, limits.MAX_COMPACT_BUNDLE_BYTES,
                                  limits.MAX_EXPECTATION_BYTES, suffixes=frozenset({".json", ".webp"})),
    "baseline": ExtractionLimits(limits.MAX_COMPACT_FILES + 1, limits.MAX_COMPACT_BUNDLE_BYTES,
                                 limits.MAX_EXPECTATION_BYTES, suffixes=frozenset({".json", ".webp"})),
    # ``envelope.json`` plus a native bundle of at most ``families[].handoff_max_bytes``.
    "family-handoff": ExtractionLimits(limits.MAX_FAMILY_FILES + 1, limits.MAX_FAMILY_BUNDLE_BYTES,
                                       limits.MAX_SOURCE_PNG_BYTES),
    "family-cache": ExtractionLimits(limits.MAX_FAMILY_FILES + 1, limits.MAX_FAMILY_BUNDLE_BYTES,
                                     limits.MAX_SOURCE_PNG_BYTES),
    # ``paired.json`` + ``selected.json`` + ``images/*.webp`` (projected from the native bundle) +
    # ``source/`` (the family handoff or cache verbatim: ``envelope.json`` plus native files of any
    # suffix). Its expanded total is one bundle's, which the family bounds partition
    # (``limits.MAX_FAMILY_HANDOFF_BYTES``), so every family generation a producer created is
    # collectable into an artifact ``build`` downloads (``artifact_limit``).
    "collected-family": ExtractionLimits(limits.MAX_COLLECTED_FAMILY_FILES, limits.MAX_COLLECTED_FAMILY_BYTES,
                                         limits.MAX_SOURCE_PNG_BYTES),
    "promotion": ExtractionLimits(2, limits.MAX_PROMOTION_BYTES, limits.MAX_PROMOTION_BYTES,
                                  suffixes=frozenset({".json"})),
}


@dataclass(frozen=True)
class _Entry:
    info: zipfile.ZipInfo
    name: str
    directory: bool


def _check_limits(limits_: ExtractionLimits, maximum_entries: int = limits.MAX_ZIP_ENTRIES) -> None:
    if not isinstance(limits_, ExtractionLimits):
        raise ZipRejected("extraction limits must be an ExtractionLimits")
    values = (limits_.max_entries, limits_.max_total_bytes, limits_.max_entry_bytes, limits_.max_ratio)
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in values):
        raise ZipRejected("extraction limits must be positive integers")
    if limits_.max_ratio > limits.MAX_ZIP_RATIO:
        raise ZipRejected(f"extraction ratio may not exceed {limits.MAX_ZIP_RATIO}")
    if limits_.max_entries > maximum_entries:
        raise ZipRejected(f"extraction entries may not exceed {maximum_entries}")
    if limits_.suffixes is not None and (not limits_.suffixes or any(
            not isinstance(suffix, str) or not suffix.startswith(".") for suffix in limits_.suffixes)):
        raise ZipRejected("extraction suffixes must be non-empty '.ext' strings")


def _safe_name(info: zipfile.ZipInfo) -> tuple[str, bool]:
    raw = info.filename
    directory = raw.endswith("/")
    candidate = raw[:-1] if directory else raw
    # ``zipfile`` silently truncates a stored name at NUL; the stored name itself must be canonical.
    if (info.orig_filename != raw or not candidate or len(raw.encode("utf-8", "surrogatepass")) > _MAX_NAME_BYTES
            or any(ord(character) < 32 or ord(character) == 127 for character in raw)
            or "\\" in raw or ":" in raw or unicodedata.normalize("NFC", raw) != raw
            or candidate.startswith("/") or not grammar.is_bundle_path(candidate)):
        raise ZipRejected(f"archive has an unsafe entry name {raw[:120]!r}")
    return candidate, directory


def _check_type(info: zipfile.ZipInfo, name: str, directory: bool) -> None:
    unix_mode = (info.external_attr >> 16) & 0xFFFF
    kind = stat.S_IFMT(unix_mode)
    if stat.S_ISLNK(unix_mode):
        raise ZipRejected(f"archive link entry is forbidden: {name!r}")
    if kind and not (stat.S_ISDIR(unix_mode) or stat.S_ISREG(unix_mode)):
        raise ZipRejected(f"archive special entry is forbidden: {name!r}")
    if directory and kind == stat.S_IFREG or not directory and kind == stat.S_IFDIR:
        raise ZipRejected(f"archive entry type disagrees with its name: {name!r}")
    if not directory and info.external_attr & _DOS_DIRECTORY:
        raise ZipRejected(f"archive file entry carries a directory attribute: {name!r}")
    if info.flag_bits & _ENCRYPTION_FLAGS:
        raise ZipRejected(f"encrypted archive entry is forbidden: {name!r}")
    if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
        raise ZipRejected(f"archive entry uses an unapproved compression method: {name!r}")


def _span(info: zipfile.ZipInfo) -> int:
    """The fewest archive bytes an entry occupies: its local header, stored name and data."""

    return _LOCAL_HEADER_BYTES + len(info.filename) + info.compress_size


def _inspect(package: zipfile.ZipFile, limits_: ExtractionLimits, archive_size: int, *,
             allow_empty: bool = False, max_files: int | None = None) -> tuple[list[_Entry], int]:
    """Validate the whole central directory before any byte is inflated.

    Every entry's local header, name and compressed data must lie inside the ``archive_size`` bytes
    (so no hostile offset, such as a Zip64 value of ``2**63``, ever reaches a seek) and no two
    entries may share bytes (the "overlapped entries" zip bomb). Names are ASCII by then, so a
    name's length is its stored byte count."""

    infos = package.infolist()
    if not infos or len(infos) > limits_.max_entries:
        raise ZipRejected(f"archive entry count {len(infos)} is outside 1..{limits_.max_entries}")
    if max_files is not None and sum(not info.is_dir() for info in infos) > max_files:
        raise ZipRejected("runtime archive exceeds its file-count bound")
    entries: list[_Entry] = []
    folded: dict[str, str] = {}
    kinds: dict[str, bool] = {}
    total = 0
    for info in infos:
        name, directory = _safe_name(info)
        parts = name.split("/")
        for depth in range(1, len(parts) + 1):
            prefix = "/".join(parts[:depth])
            prior = folded.setdefault(prefix.casefold(), prefix)
            if prior != prefix:
                raise ZipRejected(f"archive paths collide under case folding: {prior!r}, {prefix!r}")
        if name in kinds:
            raise ZipRejected(f"archive has a duplicate entry {name!r}")
        _check_type(info, name, directory)
        if info.file_size < 0 or info.compress_size < 0:
            raise ZipRejected(f"archive entry has a negative size: {name!r}")
        if not 0 <= info.header_offset <= archive_size - _span(info):
            raise ZipRejected(f"archive entry lies outside the archive: {name!r}")
        if directory:
            if info.file_size or info.compress_size > 2:
                raise ZipRejected(f"archive directory entry carries data: {name!r}")
        else:
            minimum = 0 if allow_empty else 1
            if not minimum <= info.file_size <= limits_.max_entry_bytes:
                raise ZipRejected(f"archive entry size is outside {minimum}..{limits_.max_entry_bytes}: {name!r}")
            if (info.compress_size <= 0 and info.file_size > 0) or info.file_size > info.compress_size * limits_.max_ratio:
                raise ZipRejected(f"archive entry compression ratio exceeds {limits_.max_ratio}: {name!r}")
            if info.compress_type == zipfile.ZIP_STORED and info.compress_size != info.file_size:
                raise ZipRejected(f"stored archive entry sizes disagree: {name!r}")
            if limits_.suffixes is not None and not name.endswith(tuple(limits_.suffixes)):
                raise ZipRejected(f"archive entry has an unapproved suffix: {name!r}")
            total += info.file_size
            if total > limits_.max_total_bytes:
                raise ZipRejected("archive expands beyond its total byte bound")
        kinds[name] = directory
        entries.append(_Entry(info, name, directory))
    end = 0
    for info in sorted((entry.info for entry in entries), key=lambda info: info.header_offset):
        if info.header_offset < end:
            raise ZipRejected(f"archive entries overlap: {info.filename!r}")
        end = info.header_offset + _span(info)
    files = [entry.name for entry in entries if not entry.directory]
    if not files:
        raise ZipRejected("archive contains no files")
    parents = {"/".join(name.split("/")[:depth]) for name in files for depth in range(1, name.count("/") + 1)}
    for name, directory in kinds.items():
        if directory and name not in parents:
            raise ZipRejected(f"archive directory entry is not the parent of a file: {name!r}")
        if not directory and name in parents:
            raise ZipRejected(f"archive file is also used as a directory: {name!r}")
    return entries, total


def _extract_entry(package: zipfile.ZipFile, entry: _Entry, stage: int, limits_: ExtractionLimits,
                   extracted: list[int]) -> None:
    descriptor = atomic._open_new_file(stage, entry.name)
    written = 0
    try:
        with package.open(entry.info, "r") as source:
            while True:
                chunk = source.read(min(_CHUNK, entry.info.file_size - written + 1))
                if not chunk:
                    break
                written += len(chunk)
                extracted[0] += len(chunk)
                if (written > entry.info.file_size or written > limits_.max_entry_bytes
                        or extracted[0] > limits_.max_total_bytes):
                    raise ZipRejected(f"archive entry expanded beyond its declared size: {entry.name!r}")
                atomic._write_all(descriptor, chunk)
        if written != entry.info.file_size:
            raise ZipRejected(f"archive entry size changed while extracting: {entry.name!r}")
        atomic._seal_new_file(descriptor)
    finally:
        os.close(descriptor)


def _check_central_directory(stream: BinaryIO, limits_: ExtractionLimits) -> int:
    """Bound the central directory before :mod:`zipfile` parses it into one object per entry.

    ``zipfile`` reads the whole central directory named by the end-of-central-directory record
    (and, when a Zip64 locator precedes it, the Zip64 record) and allocates a ``ZipInfo`` for every
    entry before any count can be checked. This locates that record exactly as ``zipfile`` does
    (the last 22 bytes, else the last signature within the maximal comment window) and requires
    every size and count it can select to fit ``max_entries`` entries of bounded header size.
    Returns the archive size.
    """

    size = stream.seek(0, io.SEEK_END)
    window = min(size, _EOCD.size + (1 << 16))  # Python 3.11 searches one byte further than 3.12+
    stream.seek(size - window)
    tail = stream.read(window)
    position = len(tail) - _EOCD.size
    if position < 0 or tail[position:position + 4] != _EOCD_SIGNATURE or tail[-2:] != b"\0\0":
        position = tail.rfind(_EOCD_SIGNATURE)
    if position < 0 or len(tail) - position < _EOCD.size:
        raise ZipRejected("archive has no end-of-central-directory record")
    _, _, _, _, count, directory_size, _, _ = _EOCD.unpack_from(tail, position)
    record = size - window + position
    counts, sizes = [], []
    if record >= _ZIP64_LOCATOR.size:
        stream.seek(record - _ZIP64_LOCATOR.size)
        signature, _, locator_offset, _ = _ZIP64_LOCATOR.unpack(stream.read(_ZIP64_LOCATOR.size))
        if signature == _ZIP64_LOCATOR_SIGNATURE:
            # Python 3.11 reads the Zip64 record just before the locator; 3.12+ first at its offset.
            for offset in {locator_offset, record - _ZIP64_LOCATOR.size - _ZIP64_EOCD.size}:
                if 0 <= offset <= size - _ZIP64_EOCD.size:
                    stream.seek(offset)
                    zip64 = _ZIP64_EOCD.unpack(stream.read(_ZIP64_EOCD.size))
                    if zip64[0] == _ZIP64_EOCD_SIGNATURE:
                        counts.append(zip64[7])
                        sizes.append(zip64[8])
    if not counts or count != 0xFFFF:
        counts.append(count)
    if not sizes or directory_size != 0xFFFFFFFF:
        sizes.append(directory_size)
    if max(counts) > limits_.max_entries or max(sizes) > limits_.max_entries * _MAX_CENTRAL_ENTRY_BYTES:
        raise ZipRejected(f"archive central directory exceeds {limits_.max_entries} bounded entries")
    stream.seek(0)
    return size


def archive_limit(limits_: ExtractionLimits) -> int:
    """Largest archive accepted by the existing Pages extraction limits."""
    return _archive_limit(limits_, limits.MAX_ZIP_ENTRIES)


def _archive_limit(limits_: ExtractionLimits, maximum_entries: int) -> int:
    """The largest archive (compressed bytes) ``limits_`` accept: stored data plus headers and slack.

    Validates ``limits_`` first, so a downloader can refuse an oversized artifact from its metadata
    before fetching a byte."""

    _check_limits(limits_, maximum_entries)
    return limits_.max_total_bytes + limits_.max_total_bytes // 512 + limits_.max_entries * 1024 + _ARCHIVE_SLACK_BYTES


def artifact_limit(kind: str, *, max_total_bytes: int | None = None) -> int:
    """The largest artifact (archive bytes) of ``kind`` that a kit job admits, selects or downloads:
    :func:`archive_limit` of ``LIMITS_BY_KIND[kind]``, its expanded total narrowed to
    ``max_total_bytes`` when given (a family's ``handoff_max_bytes`` plus its ``envelope.json``, never
    above the kind's own).
    It never exceeds ``limits.MAX_ARTIFACT_BYTES``, so a bundle within its expanded bound is an
    artifact every consumer accepts (``tests/test_model_limits.py``)."""

    if kind not in LIMITS_BY_KIND:
        raise ZipRejected(f"no artifact kind {kind!r}")
    bounds = LIMITS_BY_KIND[kind]
    if max_total_bytes is not None:
        if (isinstance(max_total_bytes, bool) or not isinstance(max_total_bytes, int)
                or not 0 < max_total_bytes <= bounds.max_total_bytes):
            raise ZipRejected(f"the expanded bound of a {kind} must be a positive integer within "
                              f"{bounds.max_total_bytes}")
        bounds = replace(bounds, max_total_bytes=max_total_bytes)
    return min(archive_limit(bounds), limits.MAX_ARTIFACT_BYTES)


def _archive_stream(archive: Path | bytes, limits_: ExtractionLimits,
                    maximum_entries: int = limits.MAX_ZIP_ENTRIES, *,
                    max_archive_bytes: int | None = None) -> BinaryIO:
    maximum = _archive_limit(limits_, maximum_entries)
    if max_archive_bytes is not None:
        maximum = min(maximum, max_archive_bytes)
    if isinstance(archive, (bytes, bytearray)):
        if not 0 < len(archive) <= maximum:
            raise ZipRejected(f"archive size is outside 1..{maximum} bytes")
        return io.BytesIO(bytes(archive))
    if not isinstance(archive, Path):
        raise ZipRejected("archive must be bytes or a path")
    try:
        before = archive.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise ZipRejected("archive must be a regular file, not a link or special file")
        if not 0 < before.st_size <= maximum:
            raise ZipRejected(f"archive size is outside 1..{maximum} bytes")
        descriptor = os.open(archive, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
                             | getattr(os, "O_NONBLOCK", 0))
    except OSError as exc:
        raise ZipRejected(f"cannot open the archive: {exc.strerror or exc}") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino, opened.st_size) != (
                before.st_dev, before.st_ino, before.st_size):
            raise ZipRejected("archive changed while it was opened")
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def extract(archive: Path | bytes, destination: Path, limits_: ExtractionLimits) -> list[str]:
    """Validate and extract ``archive`` into the new directory ``destination``.

    Returns the extracted relative file paths, sorted. Raises :class:`ZipRejected` for any
    policy violation; on failure ``destination`` does not exist.
    """

    return _extract(archive, destination, limits_, limits.MAX_ZIP_ENTRIES)


def extract_build(archive: Path | bytes, destination: Path) -> list[str]:
    """Extract a Build export with fixed CI bounds; Pages limits remain unchanged."""

    bounds = ExtractionLimits(limits.MAX_CI_EXPORT_ENTRIES,
                              limits.MAX_CI_EXPORT_TREE_BYTES + limits.MAX_CI_ENVELOPE_BYTES,
                              limits.MAX_CI_EXPORT_FILE_BYTES)
    return _extract(archive, destination, bounds, limits.MAX_CI_EXPORT_ENTRIES)


def extract_runtime(archive: Path | bytes, destination: Path, *, scope: str) -> list[str]:
    """Extract fixed lane/complete runtime data; empty logs need subsequent native/byte admission.

    This route alone allows empty files. Existing Pages/Build contracts retain nonempty files.
    Every transport must independently bind exact envelope, native roles and original producers.
    """
    if type(scope) is not str or scope not in ('lane', 'complete'):
        raise ZipRejected('runtime archive scope must be lane or complete')
    lane = scope == 'lane'
    files = limits.MAX_CI_RUNTIME_FILES if lane else limits.MAX_CI_RUNTIME_AGGREGATE_FILES
    total = limits.MAX_CI_RUNTIME_BYTES if lane else limits.MAX_CI_RUNTIME_AGGREGATE_BYTES
    bounds = ExtractionLimits(limits.MAX_CI_RUNTIME_ENTRIES,
                              total + limits.MAX_CI_RUNTIME_ENVELOPE_BYTES,
                              max(limits.MAX_CI_PNG_BYTES, limits.MAX_CI_RUNTIME_ENVELOPE_BYTES))
    return _extract(archive, destination, bounds, limits.MAX_CI_RUNTIME_ENTRIES,
                    allow_empty=True, max_files=files + 1,
                    max_archive_bytes=limits.MAX_CI_BUNDLE_COMPRESSED_BYTES)


def _extract(archive: Path | bytes, destination: Path, limits_: ExtractionLimits,
             maximum_entries: int, *, allow_empty: bool = False, max_files: int | None = None,
             max_archive_bytes: int | None = None) -> list[str]:
    stream = _archive_stream(archive, limits_, maximum_entries, max_archive_bytes=max_archive_bytes)
    with stream:
        try:
            archive_size = _check_central_directory(stream, limits_)
            package = zipfile.ZipFile(stream, "r")
        except (OSError, *_MALFORMED) as exc:
            raise ZipRejected(f"archive is not a valid ZIP: {type(exc).__name__}") from exc
        with package:
            try:
                entries, declared = _inspect(package, limits_, archive_size, allow_empty=allow_empty, max_files=max_files)
            except (ValueError, OverflowError, zipfile.BadZipFile) as exc:
                raise ZipRejected(f"archive directory is malformed: {type(exc).__name__}") from exc

            def writer(_stage_path: Path, stage: int) -> list[str]:
                extracted = [0]
                for entry in entries:
                    if entry.directory:
                        continue
                    try:
                        _extract_entry(package, entry, stage, limits_, extracted)
                    except OSError as exc:
                        raise ZipRejected(f"cannot extract {entry.name!r}: {exc.strerror or exc}") from exc
                    except (*_MALFORMED, RuntimeError, zlib.error) as exc:
                        # CRC mismatch, truncated data, local header disagreeing with the directory.
                        raise ZipRejected(f"cannot extract {entry.name!r}: {type(exc).__name__}") from exc
                if extracted[0] != declared:
                    raise ZipRejected("archive expanded size differs from its directory")
                return sorted(entry.name for entry in entries if not entry.directory)

            try:
                return atomic.atomic_directory(destination, writer)
            except atomic.AtomicDirectoryError as exc:
                raise ZipRejected(f"cannot publish the extraction: {exc}", reason="zip-output") from exc

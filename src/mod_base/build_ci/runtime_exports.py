"""Exact frozen runtime bytes and private independent copying (MB11), not native validity."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mod_base.build_ci.protocol import validate_plan
from mod_base.build_ci.runtime_schema import validate_runtime_envelope
from mod_base.io.atomic_directory import atomic_directory
from mod_base.io.secure_json import loads
from mod_base.io.tree import (EXPORT_PATHS, copy_regular_data_files, read_child_file, regular_data_records,
                              validate_tree_entries)
from mod_base.model import grammar as g, limits as lim
from mod_base.model.canonical import canonical_json
from mod_base.model.validators import check


def _bounds(envelope: dict[str, Any], raw_size: int) -> dict[str, Any]:
    """Caps of one scope and the path rule of every sealed export (the mod's own file names)."""
    lane = envelope['scope'] == 'lane'
    return {'max_files': (lim.MAX_CI_RUNTIME_FILES if lane else lim.MAX_CI_RUNTIME_AGGREGATE_FILES) + 1,
            'max_entries': lim.MAX_CI_RUNTIME_ENTRIES,
            'max_total_bytes': (lim.MAX_CI_RUNTIME_BYTES if lane else lim.MAX_CI_RUNTIME_AGGREGATE_BYTES) + raw_size,
            'max_file_bytes': max(lim.MAX_CI_PNG_BYTES, lim.MAX_CI_RUNTIME_ENVELOPE_BYTES),
            'rule': EXPORT_PATHS}


def verify_runtime_export(root: Path, *, plan: dict[str, Any]) -> dict[str, Any]:
    """Match canonical inventory to every frozen file's actual size/hash, including empty logs.

    MB1 admits no-follow regular single-link data. Caller must exclude writers and retain
    original root ancestry, producer/Build admission and independent native file/role validation.
    This reads all payloads, never candidate code; byte equality does not prove E2E success.
    """
    validate_plan(plan)
    validate_tree_entries(root, max_entries=lim.MAX_CI_RUNTIME_ENTRIES)
    raw = read_child_file(root, g.CI_RUNTIME_ENVELOPE_NAME, max_bytes=lim.MAX_CI_RUNTIME_ENVELOPE_BYTES)
    envelope = loads(raw, label=g.CI_RUNTIME_ENVELOPE_NAME, max_bytes=lim.MAX_CI_RUNTIME_ENVELOPE_BYTES)
    validate_runtime_envelope(envelope, plan=plan)
    check(raw == canonical_json(envelope), '$.envelope', 'runtime envelope must be canonical JSON')
    observed = regular_data_records(root, **_bounds(envelope, len(raw)))
    expected = [{key: file[key] for key in ('path', 'size', 'sha256')} for file in envelope['files']]
    expected.append({'path': g.CI_RUNTIME_ENVELOPE_NAME, 'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()})
    check(observed == sorted(expected, key=lambda file: file['path']), '$.files',
          'frozen runtime differs from its complete file inventory')
    check(read_child_file(root, g.CI_RUNTIME_ENVELOPE_NAME, max_bytes=lim.MAX_CI_RUNTIME_ENVELOPE_BYTES) == raw,
          '$.envelope', 'runtime envelope changed during verification')
    return envelope


def materialize_runtime_export(root: Path, output: Path, *, plan: dict[str, Any]) -> dict[str, Any]:
    """Atomically copy exact runtime data into a fresh independently protected private parent."""
    return _materialize_runtime_export(root, output, plan=plan, before_publish=None)


def _materialize_runtime_export(root: Path, output: Path, *, plan: dict[str, Any],
                                before_publish: Callable[[], None] | None) -> dict[str, Any]:
    """Internal transport closing admission; no public API supplies approval callbacks."""
    expected = verify_runtime_export(root, plan=plan)
    raw = canonical_json(expected)
    inventory = [{key: file[key] for key in ('path', 'size', 'sha256')} for file in expected['files']]
    inventory.append({'path': g.CI_RUNTIME_ENVELOPE_NAME, 'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()})
    inventory.sort(key=lambda file: file['path'])

    def writer(stage: Path, stage_fd: int) -> dict[str, Any]:
        copied = copy_regular_data_files(root, stage_fd, **_bounds(expected, len(raw)))
        check(copied == inventory, '$.files', 'copied runtime differs from its admitted inventory')
        check(verify_runtime_export(stage, plan=plan) == expected, '$.envelope', 'staged runtime differs')
        check(verify_runtime_export(root, plan=plan) == expected, '$.source', 'runtime source changed during copy')
        if before_publish is not None:
            before_publish()
        check(verify_runtime_export(stage, plan=plan) == expected, '$.envelope', 'staged runtime changed during final admission')
        check(verify_runtime_export(root, plan=plan) == expected, '$.source', 'runtime source changed during final admission')
        return expected

    return atomic_directory(output, writer)

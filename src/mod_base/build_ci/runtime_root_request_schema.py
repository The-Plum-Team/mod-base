"""Initial closed runtime Root request data (MB11), separate from Build request v1.

No field selects executable code, a hook, a pathname or authority. Physical admission of
original sources, three frozen inputs, execution and independently enrolled Root remains required.
"""

from __future__ import annotations

from typing import Any

from mod_base import readable_schema_versions
from mod_base.build_ci.records import bind_build_envelope
from mod_base.build_ci.root_request_schema import (_BOUNDARY, _SOURCES, _SHA, _WORKER_ID,
                                                  _envelope, _metadata, _plan)
from mod_base.build_ci.runtime_schema import validate_runtime_envelope
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json
from mod_base.model.validators import Const, Int, Obj, Str, check


_VERSIONS = readable_schema_versions('mod-base.ci.runtime-root-request')


def _runtime(value: Any, path: str) -> dict[str, Any]:
    validate_runtime_envelope(value, path=path)
    check(len(canonical_json(value)) <= limits.MAX_CI_RUNTIME_ENVELOPE_BYTES,
          path, 'Root runtime envelope exceeds its original cap')
    return value


_REQUEST = Obj({'kind': Const('mod-base.ci.runtime-root-request'),
    'schema_version': Int(min(_VERSIONS), max(_VERSIONS)), 'nonce': _SHA, 'execution_nonce': _SHA,
    'boundary': _BOUNDARY, 'validator': Obj({'uid': _WORKER_ID, 'gid': _WORKER_ID}),
    'sources': _SOURCES, 'plan': _plan, 'build': _envelope, 'runtime': _runtime,
    'lane_id': Str(grammar.CI_UNIT_ID, max_len=80),
    'run_id': Int(1, limits.MAX_RUN_ID), 'run_attempt': Int(1, limits.MAX_RUN_ATTEMPT)})


def validate_runtime_root_request(document: Any, *, path: str = '$') -> dict[str, Any]:
    """Validate closed context data only, preserving complete cross-run owning Build identity."""
    _REQUEST(document, path)
    plan, build, runtime = document['plan'], document['build'], document['runtime']
    validate_runtime_envelope(runtime, plan=plan, path=f'{path}.runtime')
    check(build['scope'] == 'complete', f'{path}.build', 'Root runtime needs its complete owning Build')
    bind_build_envelope(build, descriptor=runtime['owning_build'], plan=plan)
    check(runtime['scope'] == 'lane' and runtime['lane_id'] == document['lane_id'],
          f'{path}.lane_id', 'Root runtime requires the exact original enrolled lane')
    check((document['run_id'], document['run_attempt']) ==
          (runtime['producer']['run_id'], runtime['producer']['run_attempt']),
          path, 'Root runtime producing attempt differs')
    _metadata(document, path)
    check(len(canonical_json(document)) <= limits.MAX_CI_RUNTIME_ROOT_REQUEST_BYTES,
          path, 'Root runtime request exceeds its local cap')
    return document

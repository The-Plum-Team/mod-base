"""Initial closed runtime payload inventory (MB11); data is never native/byte/gate authority."""

from __future__ import annotations

from typing import Any

from mod_base import readable_schema_versions
from mod_base.build_ci.protocol import check_output_paths, export_path, validate_identity, validate_plan
from mod_base.build_ci.records import _PRODUCER_IDENTITY, _producer_binding, validate_descriptor
from mod_base.model import grammar as g, limits as lim
from mod_base.model.validators import Const, Int, List, Nullable, Obj, Str, check


_ID = Str(g.CI_UNIT_ID, max_len=80)
_SHA = Str(g.SHA256, max_len=64)
#: A runtime file keeps the name the mod gave it: the rule of the sealed tree that holds it.
_FILE = Obj({'path': export_path, 'lane_id': Nullable(_ID),
             'role': Str(choices=('native-report', 'runtime-log', 'screenshot', 'crash-report')),
             'size': Int(0, lim.MAX_CI_PNG_BYTES), 'sha256': _SHA})
_LANE = Obj({'id': _ID, 'native_contract_sha256': _SHA})
_ENVELOPE = Obj({'kind': Const('mod-base.ci.runtime-envelope'),
                 'schema_version': Int(min(readable_schema_versions('mod-base.ci.runtime-envelope')),
                                       max(readable_schema_versions('mod-base.ci.runtime-envelope'))),
                 'identity': validate_identity, 'plan_sha256': _SHA,
                 'profile': Str(choices=('quick-skin', 'block-pops')), 'producer': _PRODUCER_IDENTITY,
                 'scope': Str(choices=('lane', 'complete')), 'lane_id': Nullable(_ID),
                 'owning_build': validate_descriptor,
                 'lanes': List(_LANE, min_items=1, max_items=lim.MAX_CI_LANES, unique_by=lambda lane: lane['id']),
                 'files': List(_FILE, min_items=1, max_items=lim.MAX_CI_RUNTIME_AGGREGATE_FILES,
                               unique_by=lambda file: file['path'])})


def validate_runtime_envelope(document: Any, *, plan: dict[str, Any] | None = None,
                               path: str = '$') -> dict[str, Any]:
    """Validate bounded scope/inventory/Build bindings; independently derive native files/roles.

    Caller admits actual producer/API/pin/plan, private frozen roots, complete bytes and native
    report/image/log semantics. Role labels never grant a larger native-file bound or native
    success. Lane lists retain plan identity, not another authored scenario/action catalog.
    """
    # Reject a lane's oversized collection before walking nested file records.
    if type(document) is dict and type(document.get('files')) is list:
        cap = (lim.MAX_CI_RUNTIME_FILES if document.get('scope') == 'lane'
               else lim.MAX_CI_RUNTIME_AGGREGATE_FILES)
        check(len(document['files']) <= cap, path+'.files', 'runtime scope file count exceeds its cap')
    _ENVELOPE(document, path)
    _producer_binding(document['producer'], document['identity'], path+'.producer')
    owner = document['owning_build']
    check(all(owner[key] == document[key] for key in ('identity', 'plan_sha256', 'profile'))
          and g.parse_ci_artifact_name(owner['artifact']['name']).kind == 'build',
          path+'.owning_build', 'runtime requires the exact bound complete Build descriptor')
    lanes = {lane['id'] for lane in document['lanes']}
    if document['scope'] == 'lane':
        check(document['lane_id'] is not None and len(lanes) == 1 and document['lane_id'] in lanes,
              path+'.lane_id', 'lane export requires one exact lane')
        file_cap, byte_cap = lim.MAX_CI_RUNTIME_FILES, lim.MAX_CI_RUNTIME_BYTES
    else:
        check(document['lane_id'] is None, path+'.lane_id', 'complete runtime export has no selected lane')
        file_cap, byte_cap = lim.MAX_CI_RUNTIME_AGGREGATE_FILES, lim.MAX_CI_RUNTIME_AGGREGATE_BYTES
    check(len(document['files']) <= file_cap, path+'.files', 'runtime scope file count exceeds its cap')
    names = [file['path'] for file in document['files']]
    check(names == sorted(names), path+'.files', 'runtime inventory is not path ordered')
    check_output_paths(names, path+'.files')
    observed_lanes = set()
    lane_counts = dict.fromkeys(lanes, 0)
    lane_bytes = dict.fromkeys(lanes, 0)
    total = 0
    for index, file in enumerate(document['files']):
        label = f'{path}.files[{index}]'
        check(all(part.casefold() != '.git' for part in file['path'].split('/'))
              and file['path'].split('/')[0].casefold() != g.CI_RUNTIME_ENVELOPE_NAME.casefold(),
              label, 'runtime inventory contains Git internals or its own envelope')
        check(file['lane_id'] in lanes or (file['lane_id'] is None and document['scope'] == 'complete'),
              label, 'runtime file names another lane or invalid aggregate scope')
        if file['lane_id'] is not None:
            observed_lanes.add(file['lane_id'])
            lane_counts[file['lane_id']] += 1
            lane_bytes[file['lane_id']] += file['size']
            check(lane_counts[file['lane_id']] <= lim.MAX_CI_RUNTIME_FILES
                  and lane_bytes[file['lane_id']] <= lim.MAX_CI_RUNTIME_BYTES,
                  label, 'runtime lane exceeds its file or byte cap')
        cap = {'native-report': lim.MAX_CI_REPORT_BYTES, 'runtime-log': lim.MAX_CI_LOG_BYTES,
               'screenshot': lim.MAX_CI_PNG_BYTES, 'crash-report': lim.MAX_CI_LOG_BYTES}[file['role']]
        check(file['size'] <= cap and (file['size'] > 0 or file['role'] in ('runtime-log', 'crash-report')),
              label, 'runtime file exceeds its native role bound or is empty report/image data')
        total += file['size']
        check(total <= byte_cap, path+'.files', 'runtime scope bytes exceed their cap')
    check(observed_lanes == lanes, path+'.files', 'runtime inventory does not cover its declared lanes')
    if plan is not None:
        validate_plan(plan)
        check(all(document[key] == plan[key] for key in ('identity', 'plan_sha256', 'profile')),
              path, 'runtime envelope differs from independently admitted plan')
        expected = [{'id': lane['id'], 'native_contract_sha256': lane['native_contract_sha256']}
                    for lane in plan['lanes'] if document['scope'] == 'complete' or lane['id'] == document['lane_id']]
        check(document['lanes'] == expected, path+'.lanes', 'runtime lanes/contracts differ from the complete ordered plan')
    return document


def bind_runtime_envelope(envelope: dict[str, Any], *, descriptor: dict[str, Any],
                          owning_build: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    """Bind inventory to independent original selections; caller retains genuine API admission."""
    validate_descriptor(descriptor)
    validate_descriptor(owning_build)
    validate_runtime_envelope(envelope, plan=plan)
    check(all(descriptor[key] == envelope[key] for key in ('identity', 'plan_sha256', 'profile')),
          '$.descriptor', 'runtime descriptor differs from the original envelope binding')
    check(envelope['owning_build'] == owning_build, '$.owning_build', 'runtime owning Build selection differs')
    producer = {key: value for key, value in descriptor['producer'].items() if key != 'upload_window'}
    check(envelope['producer'] == producer, '$.producer', 'runtime export differs from selected producer')
    name = g.parse_ci_artifact_name(descriptor['artifact']['name'])
    expected = ('runtime', envelope['lane_id']) if envelope['scope'] == 'lane' else ('results', None)
    check((name.kind, name.unit_id) == expected, '$.descriptor.artifact.name', 'runtime descriptor scope differs')
    return envelope

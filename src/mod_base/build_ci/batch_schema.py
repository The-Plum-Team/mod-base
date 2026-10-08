"""Closed batch manifest data (MB11); no Git/API/byte/native/writer authority."""

from __future__ import annotations

from typing import Any

from mod_base import readable_schema_versions
from mod_base.model import grammar as g, limits as lim
from mod_base.model.validators import Bool, Const, Int, List, Nullable, Obj, Str, check


_SHA1 = Str(g.SHA1, max_len=40)
_SHA256 = Str(g.SHA256, max_len=64)
_REPO = Str(g.REPOSITORY, max_len=201)
_BRANCH = Str(g.BRANCH, max_len=200)
_SIDE = Obj({'mode': Str(choices=('100644', '100755', '120000')),
             'size': Int(0, lim.MAX_CI_SOURCE_FILE_BYTES), 'git_blob': _SHA1})
_PATCH = Obj({'path': Str(max_len=lim.MAX_BUNDLE_PATH_CHARS),
              'before': Nullable(_SIDE), 'after': Nullable(_SIDE)})
_MEMBER = Obj({'pr_number': Int(1, lim.MAX_RUN_ID), 'source_repository': _REPO,
               'head_branch': _BRANCH, 'head_sha': _SHA1, 'head_tree': _SHA1, 'draft': Bool(),
               'merge_base_sha': _SHA1, 'merge_base_tree': _SHA1,
               'merge_base_bytes_sha256': _SHA256, 'head_bytes_sha256': _SHA256,
               'patch': List(_PATCH, min_items=1, max_items=lim.MAX_CI_BATCH_PATCH_FILES),
               'parent_sha': _SHA1, 'squash_sha': _SHA1, 'result_tree': _SHA1})


def _members(value: Any, path: str) -> list[dict[str, Any]]:
    check(type(value) is list and 1 <= len(value) <= lim.MAX_CI_BATCH_MEMBERS,
          path, 'batch requires a bounded nonempty member list')
    total = 0
    for member in value:
        check(type(member) is dict and type(member.get('patch')) is list,
              path, 'batch member lacks its patch list')
        total += len(member['patch'])
        check(total <= lim.MAX_CI_BATCH_PATCH_FILES, path, 'whole batch patch inventory exceeds its cap')
    for index, member in enumerate(value):
        _MEMBER(member, f'{path}[{index}]')
    return value


_BATCH = Obj({'kind': Const('mod-base.ci.batch'),
              'schema_version': Int(min(readable_schema_versions('mod-base.ci.batch')),
                                    max(readable_schema_versions('mod-base.ci.batch'))),
              'repository': _REPO, 'profile': Str(choices=('quick-skin', 'block-pops')),
              'base_branch': _BRANCH, 'base_sha': _SHA1, 'base_tree': _SHA1,
              'branch': _BRANCH, 'policy_sha256': _SHA256,
              'members': _members, 'result_tree': _SHA1})


def validate_batch_manifest(document: Any, *, path: str = '$') -> dict[str, Any]:
    """Validate closed data and structural linkage, never provenance or construction approval."""

    _BATCH(document, path)
    branch = document['branch']
    check(branch.startswith(g.CI_BATCH_BRANCH_PREFIX) and not branch.endswith('/'),
          path+'.branch', 'requires nonempty batch namespace')
    numbers: set[int] = set()
    commits = {document['base_sha']}
    previous_commit, previous_tree = document['base_sha'], document['base_tree']
    sizes: dict[str, int] = {}
    path_bytes = 0
    for index, member in enumerate(document['members']):
        label = f'{path}.members[{index}]'
        check(member['pr_number'] not in numbers, label, 'duplicate batch member')
        numbers.add(member['pr_number'])
        check(member['source_repository'] == document['repository'], label, 'foreign batch member')
        check(member['head_branch'] != document['base_branch']
              and not member['head_branch'].startswith(g.CI_BATCH_BRANCH_PREFIX), label, 'nested batch/base member')
        check(member['parent_sha'] == previous_commit, label, 'batch parent differs from ordered predecessor')
        check(member['squash_sha'] not in commits and member['result_tree'] != previous_tree,
              label, 'duplicate squash commit or no-op batch result')
        commits.add(member['squash_sha'])
        previous_commit, previous_tree = member['squash_sha'], member['result_tree']
        previous = ''
        spellings: dict[str, str] = {}
        totals = {'before': 0, 'after': 0}
        for entry in member['patch']:
            name = entry['path']
            check(g.is_repo_path(name) and name > previous, label+'.patch', 'invalid/duplicate/unordered patch path')
            previous = name
            path_bytes += len(name.encode('utf-8'))
            check(path_bytes <= lim.MAX_CI_BATCH_PATCH_PATH_BYTES, label, 'batch patch paths exceed byte cap')
            parts = name.split('/')
            for position in range(1, len(parts)+1):
                prefix = '/'.join(parts[:position])
                folded = prefix.casefold()
                check(spellings.get(folded, prefix) == prefix, label, 'batch patch contains case alias')
                check(folded in spellings or len(spellings) < lim.MAX_CI_BATCH_PATCH_ENTRIES,
                      label, 'batch patch prefixes exceed entry cap')
                spellings[folded] = prefix
            check(entry['before'] != entry['after'], label+'.patch', 'empty/no-op patch entry')
            for side in ('before', 'after'):
                value = entry[side]
                if value is None:
                    continue
                check(value['mode'] != '120000' or 1 <= value['size'] <= lim.MAX_CI_SOURCE_LINK_BYTES,
                      label+'.patch', 'invalid literal link byte size')
                totals[side] += value['size']
                check(totals[side] <= lim.MAX_CI_SOURCE_TREE_BYTES, label, 'patch side exceeds source tree byte cap')
                check(sizes.get(value['git_blob'], value['size']) == value['size'], label, 'conflicting blob sizes')
                sizes[value['git_blob']] = value['size']
    check(document['result_tree'] == previous_tree, path+'.result_tree', 'batch result differs from final member')
    return document

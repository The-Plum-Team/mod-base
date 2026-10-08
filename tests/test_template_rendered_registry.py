"""Closed renderer selection and pre-write manifest admission with explicit file seams."""

import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from mod_base.errors import MbError
from mod_base.model.canonical import canonical_json
from mod_base.model.documents import validate_template_manifest
from mod_base.template import tool


class RenderedRegistryTests(unittest.TestCase):
    def manifest(self):
        root=Path(__file__).resolve().parents[1]
        return json.loads((root/'template/manifest.json').read_bytes())

    def admit(self, manifest, data=b'plain protected workflow', operation=None):
        with patch.object(tool,'_read',return_value=canonical_json(manifest)), \
                patch.object(tool,'_state',side_effect=lambda root,path:'absent' if path==tool.ACTIVATION_PATH else 'file'), \
                patch.object(tool,'_real_directory',side_effect=lambda path,label:path), \
                patch.object(tool,'_template_bytes',return_value=data):
            return tool.load_manifest(Path('fixture-kit')) if operation is None else operation()

    def test_current_pages_entry_uses_closed_path_source_and_region_policy(self):
        manifest=self.manifest()
        entry=next(item for item in manifest['files'] if item['path']==tool.CALLER_PATH)
        self.assertEqual(tool._rendered_caller_kind(entry),'pages-extension')
        self.assertEqual(self.admit(manifest,b'{{PIN}} {{VERSION}}'),manifest)
        self.assertIsNone(tool._rendered_caller_kind({'path':'README.md','source':'managed/README.md','class':'managed'}))

    def test_known_path_or_source_cannot_be_reassigned_or_case_aliased(self):
        entry={'path':tool.CALLER_PATH,'source':'managed/.github/workflows/pages.yml','class':'managed'}
        for changes in ({'source':'managed/other.yml'},{'path':'.github/workflows/other.yml'},
                        {'path':'.github/workflows/Pages.yml'},{'source':'managed/.github/workflows/Pages.yml'},
                        {'class':'seeded'},{'class':'fragment'}):
            with self.subTest(changes=changes),self.assertRaises(MbError):
                tool._rendered_caller_kind({**entry,**changes})

    def test_unregistered_workflow_tokens_reject_in_every_class_before_writes(self):
        for klass in ('managed','fragment','seeded'):
            for token in (b'{{PIN}}',b'{{VERSION}}'):
                manifest=self.manifest()
                entry={'class':klass,'path':'.github/workflows/future.yml','source':('managed/' if klass=='managed' else 'seed/')+'future.yml.tmpl'}
                if klass=='fragment':entry.update(lines=['fixture'],markers=['fixture'])
                manifest['files'].append(entry)
                validate_template_manifest(manifest)
                with self.subTest(klass=klass,token=token),self.assertRaises(MbError):
                    self.admit(manifest,token)

    def test_plain_workflow_is_not_enrolled_as_a_rendered_caller(self):
        manifest=copy.deepcopy(self.manifest())
        manifest['files'].append({'class':'managed','path':'.github/workflows/plain.yml','source':'managed/plain.yml'})
        self.assertEqual(self.admit(manifest),manifest)

    def test_check_sync_and_init_reject_unknown_rendering_before_any_write(self):
        manifest=self.manifest()
        manifest['files'].append({'class':'managed','path':'.github/workflows/future.yml','source':'managed/future.yml'})
        operations=(lambda:tool.check(Path('mod'),kit_root=Path('kit')),
                    lambda:tool.sync(Path('mod'),kit_root=Path('kit'),write=True),
                    lambda:tool.init(Path('mod'),kit_root=Path('kit'),seed=True,from_config=None))
        for operation in operations:
            with self.subTest(operation=operation),patch.object(tool,'_write_new') as new, \
                    patch.object(tool,'_write_replace') as replace,self.assertRaisesRegex(MbError,'unregistered workflow'):
                try:self.admit(manifest,b'{{PIN}}',operation)
                finally:new.assert_not_called();replace.assert_not_called()

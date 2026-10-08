"""Bounded local activation preflight before templating; explicit filesystem seams."""

import unittest
from pathlib import Path
from unittest.mock import patch

from mod_base.errors import MbError
from mod_base.model import limits
from mod_base.model.canonical import canonical_json
from mod_base.template import tool
from tests.helpers import ci_activation, ci_config


class TemplateActivationTests(unittest.TestCase):
    def exercise(self, *, manifest=None, config=None, manifest_state='file', config_state='file', operation=None):
        manifest=ci_activation() if manifest is None else manifest
        config=ci_config() if config is None else config
        def state(root,path):
            return manifest_state if path==tool.ACTIVATION_PATH else config_state
        def read(path,**kwargs):
            if path.as_posix().endswith(tool.ACTIVATION_PATH):
                self.assertEqual(kwargs['max_bytes'],limits.MAX_CI_ACTIVATION_BYTES)
                return manifest if isinstance(manifest,bytes) else canonical_json(manifest)
            self.assertTrue(path.as_posix().endswith(tool.BUILD_CONFIG_PATH))
            self.assertEqual(kwargs['max_bytes'],limits.MAX_CI_CONFIG_BYTES)
            return canonical_json(config)
        with patch.object(tool,'_state',side_effect=state),patch.object(tool,'read_regular_file',side_effect=read) as reading, \
                patch.object(tool,'_real_directory',side_effect=lambda path,label:path):
            result=tool.load_template_activation(Path('mod')) if operation is None else operation()
            if manifest_state=='absent':reading.assert_not_called()
            return result

    def test_legacy_absence_does_not_read_or_change_activation_state(self):
        self.assertIsNone(self.exercise(manifest_state='absent'))

    def test_disabled_profile_is_bound_to_regular_native_config_under_distinct_caps(self):
        self.assertEqual(self.exercise(),ci_activation())

    def test_malformed_missing_native_config_and_foreign_profile_reject(self):
        for arguments in ({'manifest_state':'invalid'},{'config_state':'absent'},{'config_state':'invalid'},
                          {'manifest':b'{"kind":'}, {'manifest':b' '*(limits.MAX_CI_ACTIVATION_BYTES+1)},
                          {'manifest':{**ci_activation(),'repository':'foreign/mod'}},
                          {'manifest':{**ci_activation(),'profile':'quick-skin'}},
                          {'config':{**ci_config(),'permissions':{}}}):
            with self.subTest(arguments=arguments),self.assertRaises(MbError):self.exercise(**arguments)

    def test_active_and_rollback_modes_cannot_gain_unsupported_caller_success(self):
        for mode in ('shadow','shared-build','shared-build-and-e2e','reviewed-rollback'):
            with self.subTest(mode=mode),self.assertRaisesRegex(MbError,'no admitted Build/E2E'):
                self.exercise(manifest={**ci_activation(),'mode':mode})

    def test_actual_check_sync_and_init_preflight_before_manifest_selection_or_writes(self):
        operations=(lambda:tool.check(Path('mod'),kit_root=Path('kit')),
                    lambda:tool.sync(Path('mod'),kit_root=Path('kit'),write=True),
                    lambda:tool.init(Path('mod'),kit_root=Path('kit'),seed=True,from_config=None))
        for operation in operations:
            with self.subTest(operation=operation),patch.object(tool,'load_manifest') as registry, \
                    patch.object(tool,'_write_new') as new,patch.object(tool,'_write_replace') as replace:
                with self.assertRaisesRegex(MbError,'no admitted Build/E2E'):
                    self.exercise(manifest={**ci_activation(),'mode':'shadow'},operation=operation)
                registry.assert_not_called();new.assert_not_called();replace.assert_not_called()

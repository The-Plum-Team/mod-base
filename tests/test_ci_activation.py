"""Initial closed profile data and bounded dispatch; not owner or workflow authority."""

import unittest

from mod_base.build_ci.activation import ACTIVATION_MODES, ACTIVATION_PATH, validate_activation
from mod_base.errors import MbError
from mod_base.model import limits
from mod_base.model.canonical import canonical_json
from mod_base.model.documents import load_document, validate_document
from tests.helpers import ci_activation


class ActivationTests(unittest.TestCase):
    def test_all_closed_modes_and_native_profiles_validate_without_side_effects(self):
        self.assertEqual(ACTIVATION_PATH,'site/mod-base-build-activation.json')
        for profile in ('quick-skin','block-pops'):
            for mode in ACTIVATION_MODES:
                document={**ci_activation(),'profile':profile,'mode':mode}
                with self.subTest(profile=profile,mode=mode):
                    self.assertIs(validate_activation(document),document)
                    self.assertIs(validate_document(document),document)

    def test_unknown_profiles_modes_types_and_versions_reject(self):
        for field,value in (('mode','enabled'),('mode',True),('profile','other'),('profile',[]),
                            ('schema_version',True),('schema_version',0),('schema_version',2),
                            ('repository','../foreign'),('repository','a/'+'b'*201),
                            ('kind','mod-base.build.config')):
            with self.subTest(field=field,value=value),self.assertRaises(MbError):
                validate_activation({**ci_activation(),field:value})
        for field in ci_activation():
            document=ci_activation();del document[field]
            with self.subTest(missing=field),self.assertRaises(MbError):validate_activation(document)

    def test_no_arbitrary_execution_template_pin_or_authority_keys_are_accepted(self):
        for field,value in (('templates',['native.yml']),('jobs',{}),('permissions',{'contents':'write'}),
                            ('secrets',{}),('kit_sha','a'*40),('extensions',{}),('deferred',['native.yml']),
                            ('approval',True),('matrix',[]),('scenarios',[]),('path','other.json')):
            with self.subTest(field=field),self.assertRaises(MbError):
                validate_activation({**ci_activation(),field:value})

    def test_kind_reader_enforces_its_cap_and_strict_json_before_dispatch(self):
        raw=canonical_json(ci_activation())
        self.assertEqual(load_document(raw,kind='mod-base.ci.activation'),ci_activation())
        for data in (raw.replace(b'"schema_version":1',b'"schema_version":1,"schema_version":1'),
                     raw.replace(b'"schema_version":1',b'"schema_version":NaN'),
                     b' '*(limits.MAX_CI_ACTIVATION_BYTES+1)):
            with self.subTest(data=data[:30]),self.assertRaises(MbError):
                load_document(data,kind='mod-base.ci.activation')

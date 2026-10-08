"""Native count/profile parity and real spawned policy-runner fixtures, without credentials."""

import dataclasses
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mod_base.build_ci.policy import (BoundedPolicyStream, PolicyCounts, admit_policy_unit)
from mod_base.errors import MbError
from mod_base.model import limits


ROOT = Path(__file__).resolve().parents[1]
PASSING = """import unittest
class Fixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.ready = True
    @classmethod
    def tearDownClass(cls): cls.ready = False
    def test_a(self): self.assertTrue(self.ready)
    def test_b(self): self.assertTrue(self.ready)
@unittest.skip('explicit method-count skip')
class Skipped(unittest.TestCase):
    def test_skip(self): raise AssertionError('must not run')
"""
CLASS_SKIP = """import unittest
class Unavailable(unittest.TestCase):
    @classmethod
    def setUpClass(cls): raise unittest.SkipTest('fixture needs a tool')
    def test_a(self): raise AssertionError('must not run')
    def test_b(self): raise AssertionError('must not run')
"""


class PolicyCountTests(unittest.TestCase):
    def test_regular_method_skips_expected_failures_and_native_fixture_skips_keep_exact_counts(self):
        for profile in ("block-pops", "quick-skin"):
            for counts in (PolicyCounts(3,0,0,1,0,1,0,True), PolicyCounts(1,0,0,2,0,0,0,True)):
                # Native tearDownClass SkipTest is an extra skipped holder, not an omitted test.
                with self.subTest(profile=profile, counts=counts):
                    self.assertEqual(admit_policy_unit(profile=profile, discovered=counts.tests_run,
                                     repeat=1, fixture=True, counts=counts), 0)

    def test_only_qs_exact_whole_fixture_skips_cover_unexecuted_discovery(self):
        counts = PolicyCounts(0,0,0,2,2,0,0,True)
        self.assertEqual(admit_policy_unit(profile="quick-skin",discovered=6,repeat=2,fixture=True,counts=counts),6)
        for profile, fixture, changed in [("block-pops", True, counts), ("quick-skin", False, counts),
                ("quick-skin", True, dataclasses.replace(counts, class_skips=1)),
                ("quick-skin", True, dataclasses.replace(counts, tests_run=3))]:
            with self.subTest(profile=profile, fixture=fixture), self.assertRaises(MbError):
                admit_policy_unit(profile=profile,discovered=6,repeat=2,fixture=fixture,counts=changed)

    def test_failed_cleanup_unexpected_success_bad_metadata_and_forged_success_never_pass(self):
        regular = PolicyCounts(1,0,0,0,0,0,0,True)
        for counts in (dataclasses.replace(regular, failures=1), dataclasses.replace(regular, errors=1),
                dataclasses.replace(regular,unexpected_successes=1), dataclasses.replace(regular,successful=False),
                dataclasses.replace(regular,tests_run=True),dataclasses.replace(regular,class_skips=1),
                dataclasses.replace(regular,tests_run=limits.MAX_CI_POLICY_TESTS+1)):
            with self.subTest(counts=counts), self.assertRaises(MbError):
                admit_policy_unit(profile="quick-skin",discovered=1,repeat=1,fixture=True,counts=counts)
        for kwargs in ({"profile":"unknown"},{"discovered":0},{"discovered":True},{"repeat":2},{"fixture":1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(MbError):
                admit_policy_unit(**{**dict(profile="block-pops",discovered=1,repeat=1,fixture=True,counts=regular),**kwargs})


class PolicyDiagnosticTests(unittest.TestCase):
    def test_utf8_prefix_is_bounded_without_partial_characters_and_later_output_is_discarded(self):
        stream = BoundedPolicyStream(7)
        self.assertEqual(stream.write("éééé"),4)
        self.assertEqual(stream.getvalue(),"ééé")
        self.assertTrue(stream.truncated)
        self.assertEqual(stream.write("later"),5)
        self.assertEqual(stream.getvalue(),"ééé")
        stream.flush()

    def test_caps_nontext_surrogates_and_closed_streams_fail_with_mb_errors(self):
        for cap in (0,True,limits.MAX_CI_LOG_BYTES+1):
            with self.assertRaises(MbError):
                BoundedPolicyStream(cap)
        stream = BoundedPolicyStream(16)
        for text in (b"bytes","\ud800"):
            with self.assertRaises(MbError):
                stream.write(text)
        stream.close()
        with self.assertRaises(MbError):
            stream.write("closed")


class PolicyRunnerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="mod-base-policy-runner-")
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        self.start = self.base / "tests"
        self.start.mkdir()
        (self.start / "__init__.py").write_text("",encoding="utf-8")
        # Benign authored fixtures only, in a fresh process with no ambient credentials.
        self.environment = {"PYTHONPATH":str(ROOT/"src"),"PYTHONDONTWRITEBYTECODE":"1",
                            "PATH":os.defpath,"TMP":str(self.base),"TEMP":str(self.base),"TMPDIR":str(self.base)}
        if "SYSTEMROOT" in os.environ:
            self.environment["SYSTEMROOT"] = os.environ["SYSTEMROOT"]

    def module(self, name, source):
        (self.start / f"test_{name}.py").write_text(source,encoding="utf-8")

    def run_profile(self, profile):
        arguments = [sys.executable,str(ROOT/"tools"/"parallel_unittest.py"),str(self.start),
                     "--policy-profile",profile,"-j","2","-v","--slowest","0"]
        if profile == "block-pops":
            arguments += ["-t",str(self.base)]
        return subprocess.run(arguments,cwd=self.base,env=self.environment,stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=40,check=False)

    def serial_count(self, profile):
        arguments = [sys.executable,"-m","unittest","discover","-s",str(self.start)]
        if profile == "block-pops":
            arguments += ["-t",str(self.base)]
        result = subprocess.run(arguments,cwd=self.base,env=self.environment,stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=20,check=False)
        count = re.search(rb"Ran (\d+) tests? in",result.stderr)
        self.assertIsNotNone(count,result.stderr)
        self.assertEqual(result.returncode,0,result.stderr+result.stdout)
        return int(count.group(1))

    def test_both_profiles_match_serial_fixture_method_skip_and_reexport_counts(self):
        self.module("one",PASSING)
        for profile, importing in (("quick-skin","from test_one import Fixture\n"),
                                    ("block-pops","from tests.test_one import Fixture\n")):
            self.module("reexport",importing)
            with self.subTest(profile=profile):
                result = self.run_profile(profile)
                self.assertEqual(result.returncode,0,result.stderr+result.stdout)
                self.assertEqual(self.serial_count(profile),5)
                self.assertIn(b"Ran 5 tests in",result.stdout)

    def test_qs_start_root_sibling_imports_and_worker_temp_inheritance_are_preserved(self):
        self.module("helper","VALUE=7\n")
        self.module("user","import os,subprocess,sys,tempfile,unittest\nfrom test_helper import VALUE\n"
                    "class User(unittest.TestCase):\n    def test_a(self):\n        self.assertEqual(VALUE,7)\n"
                    "        directory=tempfile.gettempdir()\n        self.assertTrue(os.path.basename(directory).startswith('worker-'))\n"
                    "        child=subprocess.run([sys.executable,'-c','import tempfile; print(tempfile.gettempdir())'],capture_output=True,text=True,check=True)\n"
                    "        self.assertEqual(directory,child.stdout.strip())\n")
        result = self.run_profile("quick-skin")
        self.assertEqual(result.returncode,0,result.stderr+result.stdout)
        self.assertIn(b"Ran 1 test in",result.stdout)

    def test_native_teardown_class_skip_keeps_full_method_count_in_both_profiles(self):
        self.module("teardown","import unittest\nclass CleanupSkip(unittest.TestCase):\n"
                    "    @classmethod\n    def tearDownClass(cls): raise unittest.SkipTest('native cleanup skip')\n"
                    "    @unittest.skip('native method skip')\n    def test_a(self): pass\n")
        for profile in ("quick-skin","block-pops"):
            result = self.run_profile(profile)
            self.assertEqual(result.returncode,0,result.stderr+result.stdout)
            self.assertEqual(self.serial_count(profile),1)
            self.assertIn(b"OK (skipped=2)",result.stdout)

    def test_discovery_cap_rejects_before_scheduling_or_class_reload(self):
        specification = importlib.util.spec_from_file_location("policy_runner_cap_fixture",ROOT/"tools/parallel_unittest.py")
        runner = importlib.util.module_from_spec(specification)
        sys.modules[specification.name] = runner
        self.addCleanup(sys.modules.pop,specification.name)
        specification.loader.exec_module(runner)
        loader = unittest.TestLoader()
        loader.discover = lambda *args,**kwargs: unittest.TestSuite([unittest.TestCase() for _ in range(3)])
        with patch.object(runner,"MAX_CI_POLICY_TESTS",2), \
                patch.object(runner.unittest,"TestLoader",return_value=loader), \
                patch.object(loader,"loadTestsFromName") as loading:
            units,errors = runner.discover([str(self.start)],pattern="test_*.py",top_level=self.base)
        self.assertEqual(units,[])
        self.assertEqual(errors,["policy discovery exceeds its test cap"])
        loading.assert_not_called()

    def test_whole_class_skip_beside_executed_tests_passes_only_qs_profile(self):
        self.module("one",PASSING)
        self.module("skip",CLASS_SKIP)
        qs,bp = self.run_profile("quick-skin"),self.run_profile("block-pops")
        self.assertEqual(qs.returncode,0,qs.stderr+qs.stdout)
        self.assertEqual(bp.returncode,1,bp.stderr+bp.stdout)
        self.assertEqual(self.serial_count("quick-skin"),3)
        self.assertIn(b"3 of 5 discovered tests ran",qs.stdout)
        self.assertIn(b"OK (skipped=2)",qs.stdout)
        self.assertIn(b"ran 0 tests, discovered 2",bp.stderr)

    def test_all_whole_class_skips_and_empty_discovery_cannot_succeed(self):
        for profile in ("quick-skin","block-pops"):
            result = self.run_profile(profile)
            self.assertEqual(result.returncode,1,result.stderr+result.stdout)
            self.assertIn(b"discovery failed closed",result.stderr)
        self.module("skip",CLASS_SKIP)
        result = self.run_profile("quick-skin")
        self.assertEqual(result.returncode,1,result.stderr+result.stdout)
        self.assertIn(b"no test ran",result.stderr)

    def test_fixture_cleanup_errors_unexpected_success_import_errors_and_dead_workers_fail(self):
        cases = [CLASS_SKIP.replace("raise unittest.SkipTest('fixture needs a tool')",
                         "cls.addClassCleanup(lambda: 1/0); raise unittest.SkipTest('fixture needs a tool')"),
                 "import unittest\nclass Bad(unittest.TestCase):\n    @unittest.expectedFailure\n    def test_a(self): pass\n",
                 "import missing_mod_base_policy_fixture_module\n",
                 "import os,unittest\nclass Bad(unittest.TestCase):\n    def test_a(self): os._exit(0)\n"]
        self.module("one",PASSING)
        for profile in ("quick-skin","block-pops"):
            for index, source in enumerate(cases):
                self.module("bad",source)
                with self.subTest(profile=profile,index=index):
                    result = self.run_profile(profile)
                    self.assertEqual(result.returncode,1,result.stderr+result.stdout)
                    self.assertNotIn(b"\nOK",result.stdout)


if __name__ == "__main__":
    unittest.main()

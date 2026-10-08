"""Fixed launch orchestration; explicit seams do not establish Linux or installer provenance."""

import copy
import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from mod_base.build_ci import privileged_launch as launch
from mod_base.build_ci.bootstrap_installation import BootstrapInstallation
from mod_base.build_ci.installation import KitInstallation
from mod_base.build_ci.python_installation import PythonInstallation
from mod_base.build_ci.root_request import RootFreezeContext
from mod_base.build_ci.toolchain import ToolBytesProof, ToolTreeProof
from mod_base.errors import MbError
from tests.test_ci_root_request import fixture, BOUNDARY, VALIDATOR


class PrivilegedLaunchTests(unittest.TestCase):
    def fixture(self):
        invocation,sources,plan,envelope=fixture()
        invocation=replace(invocation,repo_root=Path("/home/runner/controller"))
        kit=plan["identity"]["kit"]
        installation=KitInstallation(kit["sha"],kit["version"],kit["tree_digest"],3,3,1,11)
        program=BootstrapInstallation("a"*64,10,1,12)
        tools=ToolBytesProof(ToolTreeProof(("/opt/hostedtoolcache/Python/x64",),"b"*64,2,8,10),"sha256:"+"c"*64)
        context=RootFreezeContext(sources,plan,envelope,42,2,"d"*64)
        args=dict(boundary=BOUNDARY,installation=installation,program=program,tools=tools,
                  python="/opt/hostedtoolcache/Python/x64/bin/python3",nonce="e"*64)
        return invocation,args,context

    def seams(self,stack,invocation,args,context):
        mocks={}
        for name,kwargs in {
            "authenticate_privileged_host_boundary":{},"authenticate_worker_account":{"return_value":VALIDATOR},
            "_accounts":{},"authenticate_privileged_bootstrap":{},
            "authenticate_toolchain_bytes":{"return_value":args["tools"]},"_execution_tool_paths":{},
            "build_root_freeze_invocation":{"return_value":invocation},
            "read_root_freeze_request":{"return_value":context},"_control":{"return_value":b""},
            "_sealed_identity":{"return_value":(1,13)},"authenticate_tree_private_access":{},
            "verify_validation_export":{"return_value":{"synthetic":"receipt"}},"terminate_worker":{}
        }.items():mocks[name]=stack.enter_context(patch.object(launch,name,**kwargs))
        return mocks

    def test_actual_closed_command_shape_matches_installed_entry_and_bound_context(self):
        from tests.test_ci_privileged_bootstrap import bootstrap
        invocation,args,context=self.fixture()
        with ExitStack() as stack:
            mocks=self.seams(stack,invocation,args,context)
            self.assertEqual(launch.execute_privileged_freeze_request(invocation,**args),{"synthetic":"receipt"})
            command=mocks["_control"].call_args.args[0]
            self.assertEqual(command[:4],(args["python"],"-I","-B","-S"))
            self.assertEqual(command[4],str(launch.PRIVILEGED_BOOTSTRAP_ROOT/launch.grammar.CI_BOOTSTRAP_PROGRAM_NAME))
            values=bootstrap._entry_arguments(list(command[5:]))
            self.assertEqual(launch.PRIVILEGED_FREEZE_FLAGS,bootstrap.ENTRY_FLAGS)
            self.assertEqual(values["--nonce"],args["nonce"])
            self.assertEqual(values["--controller-root"],invocation.repo_root.as_posix())
            self.assertEqual(values["--kit-digest"],args["installation"].digest)
            self.assertEqual(mocks["_control"].call_args.kwargs,
                {"timeout":20.0,"accepted":frozenset({0}),"cwd":Path(str(launch.PRIVILEGED_KIT_ROOT))})
            self.assertEqual(mocks["authenticate_toolchain_bytes"].call_count,3)
            for call in mocks["authenticate_toolchain_bytes"].call_args_list:
                self.assertEqual(call.args,(args["tools"].tools,))
                self.assertEqual(call.kwargs,{"boundary":BOUNDARY,"expected_digest":args["tools"].digest,"privileged":True})
            verified=mocks["verify_validation_export"].call_args.kwargs
            self.assertEqual(verified["plan"],context.plan)
            self.assertEqual((verified["hook"],verified["unit_id"],verified["run_id"],verified["run_attempt"]),
                             ("verify_build",None,42,2))
            self.assertEqual(verified["source_config_sha256"],context.sources.config.sha256)
            mocks["terminate_worker"].assert_called_once_with(VALIDATOR)

    def test_role_failure_precedes_account_access_and_launch(self):
        invocation,args,context=self.fixture()
        with ExitStack() as stack:
            mocks=self.seams(stack,invocation,args,context)
            mocks["authenticate_privileged_host_boundary"].side_effect=MbError("role")
            with self.assertRaises(MbError):launch.execute_privileged_freeze_request(invocation,**args)
            mocks["authenticate_worker_account"].assert_not_called()
            mocks["_control"].assert_not_called()
            mocks["terminate_worker"].assert_not_called()

    def test_each_failed_admission_forbids_launch_and_terminates_admitted_validator(self):
        invocation,args,context=self.fixture()
        for name in ("authenticate_privileged_bootstrap","authenticate_toolchain_bytes","_execution_tool_paths",
                     "build_root_freeze_invocation","read_root_freeze_request"):
            with self.subTest(name=name),ExitStack() as stack:
                mocks=self.seams(stack,invocation,args,context);mocks[name].side_effect=MbError("admission")
                with self.assertRaises(MbError):launch.execute_privileged_freeze_request(invocation,**args)
                mocks["_control"].assert_not_called()
                mocks["terminate_worker"].assert_called_once_with(VALIDATOR)

    def test_config_kit_and_retained_bytes_proof_drift_forbid_launch(self):
        invocation,args,context=self.fixture()
        for mode in ("invocation","config","kit","tools","request-digest"):
            with self.subTest(mode=mode),ExitStack() as stack:
                case_args=dict(args)
                mocks=self.seams(stack,invocation,args,context)
                changed=invocation
                if mode=="invocation":changed=object()
                elif mode=="config":mocks["build_root_freeze_invocation"].return_value=replace(
                    invocation,config=replace(invocation.config,sha256="f"*64))
                elif mode=="kit":case_args["installation"]=replace(args["installation"],kit_sha="f"*40)
                elif mode=="tools":mocks["authenticate_toolchain_bytes"].return_value=replace(args["tools"],digest="sha256:"+"f"*64)
                else:
                    plan=copy.deepcopy(context.plan);plan["identity"]["kit"]["tree_digest"]="sha256:"+"f"*64
                    mocks["read_root_freeze_request"].return_value=replace(context,plan=plan)
                with self.assertRaises(MbError):launch.execute_privileged_freeze_request(changed,**case_args)
                mocks["_control"].assert_not_called()

    def test_successful_exit_cannot_replace_receipt_or_post_launch_reinspection(self):
        invocation,args,context=self.fixture()
        for mode in ("stdout","request","program","tools","receipt","sealed-root"):
            with self.subTest(mode=mode),ExitStack() as stack:
                mocks=self.seams(stack,invocation,args,context)
                if mode=="stdout":mocks["_control"].return_value=b"forged success"
                elif mode=="request":mocks["read_root_freeze_request"].side_effect=[context,replace(context,execution_nonce="f"*64)]
                elif mode=="program":mocks["authenticate_privileged_bootstrap"].side_effect=[None,None,MbError("post")]
                elif mode=="tools":mocks["authenticate_toolchain_bytes"].side_effect=[args["tools"],args["tools"],MbError("post")]
                elif mode=="receipt":mocks["verify_validation_export"].side_effect=MbError("receipt")
                else:mocks["_sealed_identity"].side_effect=[(1,13),(1,14)]
                with self.assertRaises(MbError):launch.execute_privileged_freeze_request(invocation,**args)
                mocks["terminate_worker"].assert_called_once_with(VALIDATOR)

    def test_process_failure_and_io_failure_terminate_and_never_read_receipt(self):
        invocation,args,context=self.fixture()
        for error in (MbError("process"),OSError("process")):
            with self.subTest(error=type(error).__name__),ExitStack() as stack:
                mocks=self.seams(stack,invocation,args,context);mocks["_control"].side_effect=error
                with self.assertRaises(MbError):launch.execute_privileged_freeze_request(invocation,**args)
                mocks["verify_validation_export"].assert_not_called()
                mocks["terminate_worker"].assert_called_once_with(VALIDATOR)


class InstalledPythonLaunchTests(unittest.TestCase):
    fixture = PrivilegedLaunchTests.fixture
    seams = PrivilegedLaunchTests.seams

    def installed(self):
        return PythonInstallation("3.11.17", "sha256:" + "a" * 64, "sha256:" + "b" * 64, 4, 2, 100, 1, 10)

    def test_fixed_sdk_rechecks_bracket_launch_and_preserve_complete_tool_admission(self):
        invocation, args, context = self.fixture()
        args["python"] = self.installed()
        executable = "/opt/hostedtoolcache/Python/3.11.17/x64/bin/python3.11"
        with ExitStack() as stack:
            mocks = self.seams(stack, invocation, args, context)
            authenticate = stack.enter_context(patch.object(launch, "authenticate_privileged_python_installation", return_value=executable))
            self.assertEqual({"synthetic": "receipt"}, launch.execute_installed_python_freeze_request(invocation, **args))
            self.assertEqual(3, authenticate.call_count)
            for call in authenticate.call_args_list:
                self.assertEqual(call.args, (args["python"],))
                self.assertEqual(call.kwargs, {"boundary": BOUNDARY, "installation": args["installation"]})
            self.assertEqual(3, mocks["authenticate_toolchain_bytes"].call_count)
            self.assertEqual(executable, mocks["_control"].call_args.args[0][0])
            mocks["terminate_worker"].assert_called_once_with(VALIDATOR)

    def test_source_failure_wrong_path_or_unapproved_tools_forbid_launch(self):
        invocation, args, context = self.fixture()
        args["python"] = self.installed()
        executable = "/opt/hostedtoolcache/Python/3.11.17/x64/bin/python3.11"
        for kind in ("source", "path", "tools"):
            with self.subTest(kind=kind), ExitStack() as stack:
                mocks = self.seams(stack, invocation, args, context)
                authenticate = stack.enter_context(patch.object(launch, "authenticate_privileged_python_installation", return_value=executable))
                if kind == "source":
                    authenticate.side_effect = MbError("source-derived SDK differs")
                elif kind == "path":
                    authenticate.return_value = "/wrong/python"
                else:
                    mocks["authenticate_toolchain_bytes"].return_value = replace(args["tools"], digest="sha256:" + "f" * 64)
                with self.assertRaises(MbError):
                    launch.execute_installed_python_freeze_request(invocation, **args)
                mocks["_control"].assert_not_called()
                mocks["terminate_worker"].assert_called_once_with(VALIDATOR)

    def test_post_process_sdk_drift_rejects_receipt_and_stops_validator(self):
        invocation, args, context = self.fixture()
        args["python"] = self.installed()
        executable = "/opt/hostedtoolcache/Python/3.11.17/x64/bin/python3.11"
        with ExitStack() as stack:
            mocks = self.seams(stack, invocation, args, context)
            stack.enter_context(patch.object(launch, "authenticate_privileged_python_installation", side_effect=[executable, executable, MbError("changed SDK")]))
            with self.assertRaises(MbError):
                launch.execute_installed_python_freeze_request(invocation, **args)
            mocks["_control"].assert_called_once()
            mocks["verify_validation_export"].assert_not_called()
            mocks["terminate_worker"].assert_called_once_with(VALIDATOR)

    def test_unsupported_receipts_do_not_touch_the_account_or_process(self):
        invocation, args, context = self.fixture()
        for proof in (object(), replace(self.installed(), version="../escape"), replace(self.installed(), version=True)):
            with self.subTest(proof=proof), ExitStack() as stack:
                mocks = self.seams(stack, invocation, args, context)
                args["python"] = proof
                with self.assertRaises(MbError):
                    launch.execute_installed_python_freeze_request(invocation, **args)
                mocks["authenticate_worker_account"].assert_not_called()
                mocks["_control"].assert_not_called()

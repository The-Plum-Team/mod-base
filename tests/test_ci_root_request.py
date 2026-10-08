"""Private request origin and fixed reconstruction; Windows seams are explicit, not Linux proof."""

import copy
import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from mod_base.build_ci import root_request as request
from mod_base.build_ci.controller import authenticate_controller_sources
from mod_base.build_ci.host import HostBoundary
from mod_base.build_ci.installation import KitInstallation
from mod_base.build_ci.worker import WorkerAccount, WorkerError
from mod_base.errors import MbError
from mod_base.model.canonical import canonical_json, canonical_sha256
from mod_base.runtime import build_invocation


BOUNDARY = HostBoundary("/home/runner",1001,121,1,10,0o755)
VALIDATOR = WorkerAccount("validator",2001,2001,"validator-home")


def fixture(*, empty=False):
    from tests.test_ci_controller import ControllerSourceTests
    plan,api,_,protected = ControllerSourceTests().fixture(empty_module=empty)
    sources = authenticate_controller_sources(api,identity=plan["identity"],protected_paths=protected)
    from tests.helpers import ci_envelope
    invocation = build_invocation(Path(__file__).parent / "fixtures/mods/qs_like",None,
        {"MOD_BASE_KIT_SHA":plan["identity"]["kit"]["sha"],"GITHUB_REPOSITORY":plan["identity"]["repository"],
         "GITHUB_SHA":plan["identity"]["controller_sha"]},check_repository=False)
    # The generic documents advertise the next release. This invocation is actually running
    # the unreleased checkout's current version; binding must use that executing version.
    plan["identity"]["kit"]["version"] = invocation.kit["version"]
    plan["plan_sha256"] = canonical_sha256({key:value for key,value in plan.items() if key!="plan_sha256"})
    envelope = ci_envelope()
    envelope["identity"] = copy.deepcopy(plan["identity"])
    envelope["plan_sha256"] = plan["plan_sha256"]
    return invocation,sources,plan,envelope


class RootRequestTests(unittest.TestCase):
    def test_composition_uses_only_fenced_data_and_explicit_runtime_identity(self):
        invocation,sources,plan,envelope = fixture()
        raw = (Path(__file__).parent / "fixtures/mods/qs_like/site/mod-base.json").read_bytes()
        kit = invocation.kit
        installed = KitInstallation(kit["sha"],kit["version"],plan["identity"]["kit"]["tree_digest"],3,3,1,11)
        composed = build_invocation(Path(__file__).parent / "fixtures/mods/qs_like",None,dict(invocation.environ),
            check_repository=False,root=Path(str(request.PRIVILEGED_KIT_ROOT)))
        def factory(repo,config,environ,**kwargs):
            self.assertEqual(repo,Path("/home/runner/controller"))
            self.assertIsNone(config)
            self.assertEqual(kwargs,{"check_repository":False,"root":Path(str(request.PRIVILEGED_KIT_ROOT))})
            self.assertEqual(environ,dict(invocation.environ))
            return replace(composed,repo_root=repo)
        with ExitStack() as stack:
            stack.enter_context(patch.object(request,"authenticate_privileged_host_boundary"))
            stack.enter_context(patch.object(request,"_open_directory",return_value=9))
            stack.enter_context(patch.object(request.os,"fstat",return_value=SimpleNamespace(
                st_mode=0o40755,st_uid=BOUNDARY.uid,st_dev=1,st_ino=12)))
            closing=stack.enter_context(patch.object(request.os,"close"))
            stack.enter_context(patch.object(request,"read_privileged_kit_installation",return_value=installed))
            reading=stack.enter_context(patch.object(request,"read_child_file",return_value=raw))
            stack.enter_context(patch.object(request,"build_invocation",side_effect=factory))
            actual = request.build_root_freeze_invocation(boundary=BOUNDARY,controller_root="/home/runner/controller",
                repository=invocation.repository,controller_sha=invocation.implementation_sha,kit_sha=kit["sha"])
        self.assertEqual(dict(actual.environ),dict(invocation.environ))
        self.assertEqual(actual.config,invocation.config)
        self.assertEqual(reading.call_count,2)
        for call in reading.call_args_list:
            self.assertEqual(call.args,(Path("/home/runner/controller"),"site/mod-base.json"))
            self.assertEqual(call.kwargs,{"max_bytes":request.limits.MAX_CONFIG_BYTES})
        self.assertEqual(closing.call_count,2)

    def test_composition_rejects_role_path_and_checkout_identity_before_config(self):
        invocation,_,_,_ = fixture()
        args=dict(boundary=BOUNDARY,repository=invocation.repository,
                  controller_sha=invocation.implementation_sha,kit_sha=invocation.kit["sha"])
        with patch.object(request,"authenticate_privileged_host_boundary",side_effect=WorkerError("role")), \
                patch.object(request,"read_child_file") as reading,self.assertRaises(MbError):
            request.build_root_freeze_invocation(controller_root="/home/runner/controller",**args)
        reading.assert_not_called()
        for path in ("/home/runner","/tmp/controller","/home/runner/../candidate","/home/runner//controller"):
            with self.subTest(path=path),patch.object(request,"authenticate_privileged_host_boundary"), \
                    patch.object(request,"read_child_file") as reading,self.assertRaises(MbError):
                request.build_root_freeze_invocation(controller_root=path,**args)
            reading.assert_not_called()
        with patch.object(request,"authenticate_privileged_host_boundary"), \
                patch.object(request,"_open_directory",return_value=9),patch.object(request.os,"close") as closing, \
                patch.object(request.os,"fstat",return_value=SimpleNamespace(st_mode=0o40755,st_uid=0)), \
                patch.object(request,"read_child_file") as reading,self.assertRaises(MbError):
            request.build_root_freeze_invocation(controller_root="/home/runner/controller",**args)
        reading.assert_not_called()
        closing.assert_called_once_with(9)

    def _seams(self, stack):
        stack.enter_context(patch.object(request,"authenticate_host_boundary"))
        stack.enter_context(patch.object(request,"authenticate_privileged_host_boundary"))
        stack.enter_context(patch.object(request,"_accounts"))
        stack.enter_context(patch.object(request,"_layout"))
        stack.enter_context(patch.object(request,"_inspect_sources",return_value=(1,12)))
        stack.enter_context(patch.object(request,"_source_root_identity",return_value=(1,12)))
        stack.enter_context(patch.object(request,"_inspect_inputs",return_value=((1,13),(1,14))))
        stack.enter_context(patch.object(request,"authenticate_tree_private_access"))
        stack.enter_context(patch.object(request,"authenticate_tree_read_access"))

    def test_composition_rejects_config_kit_and_named_checkout_drift(self):
        invocation,_,plan,_ = fixture()
        raw = (Path(__file__).parent / "fixtures/mods/qs_like/site/mod-base.json").read_bytes()
        kit = invocation.kit
        installed = KitInstallation(kit["sha"],kit["version"],plan["identity"]["kit"]["tree_digest"],3,3,1,11)
        first = SimpleNamespace(st_mode=0o40755,st_uid=BOUNDARY.uid,st_dev=1,st_ino=12)
        changed = SimpleNamespace(st_mode=0o40755,st_uid=BOUNDARY.uid,st_dev=1,st_ino=13)
        for mode in ("config-read", "config-factory", "root", "kit-sha", "kit-version", "os-error"):
            with self.subTest(mode=mode),ExitStack() as stack:
                stack.enter_context(patch.object(request,"authenticate_privileged_host_boundary"))
                stack.enter_context(patch.object(request,"_open_directory",return_value=9))
                stack.enter_context(patch.object(request.os,"close"))
                stack.enter_context(patch.object(request.os,"fstat",side_effect=
                    OSError("fixture") if mode=="os-error" else [first,changed if mode=="root" else first]))
                admitted = replace(installed,kit_sha="f"*40) if mode=="kit-sha" else installed
                if mode=="kit-version": admitted=replace(installed,kit_version="9.0.0")
                stack.enter_context(patch.object(request,"read_privileged_kit_installation",return_value=admitted))
                stack.enter_context(patch.object(request,"read_child_file",side_effect=[raw,raw+b" " if mode=="config-read" else raw]))
                actual = replace(invocation,config=replace(invocation.config,sha256="f"*64)) if mode=="config-factory" else invocation
                stack.enter_context(patch.object(request,"build_invocation",return_value=actual))
                with self.assertRaises(MbError):
                    request.build_root_freeze_invocation(boundary=BOUNDARY,controller_root="/home/runner/controller",
                        repository=invocation.repository,controller_sha=invocation.implementation_sha,kit_sha=kit["sha"])

    def _published(self, invocation,sources,plan,envelope):
        captured = {}
        with ExitStack() as stack:
            self._seams(stack)
            stack.enter_context(patch.object(request.os,"urandom",return_value=b"R"*32))
            stack.enter_context(patch.object(request.os,"O_NOFOLLOW",0,create=True))
            stack.enter_context(patch.object(request.os,"open",return_value=7))
            stack.enter_context(patch.object(request.os,"fchmod",create=True))
            stack.enter_context(patch.object(request.os,"fsync"))
            stack.enter_context(patch.object(request.os,"close"))
            def write(fd,name,raw):
                self.assertEqual((fd,name),(31,"ci-root-request.json"))
                captured["raw"] = raw
            stack.enter_context(patch.object(request,"write_new",side_effect=write))
            stack.enter_context(patch.object(request,"read_child_file",side_effect=lambda *a,**kw: captured["raw"]))
            publish = stack.enter_context(patch.object(request,"atomic_directory",
                side_effect=lambda path,fill: fill(Path("/private-stage"),31)))
            nonce = request.record_root_freeze_request(invocation,boundary=BOUNDARY,validator=VALIDATOR,
                sources=sources,plan=plan,envelope=envelope,run_id=42,run_attempt=2,execution_nonce="a"*64)
            self.assertEqual(publish.call_args.args[0],Path(str(request.ROOT_REQUEST_ROOT)))
        return nonce,captured["raw"]

    def test_writer_fixed_private_publication_and_metadata_only_context(self):
        from mod_base.io.secure_json import loads
        invocation,sources,plan,envelope = fixture()
        nonce,raw = self._published(invocation,sources,plan,envelope)
        document = loads(raw,label="fixture",max_bytes=len(raw))
        self.assertEqual(nonce,"52"*32)
        self.assertEqual(document["sources"],request._source_metadata(sources))
        self.assertNotIn("data",document["sources"]["files"][0])
        self.assertEqual(document["execution_nonce"],"a"*64)
        self.assertEqual(document["plan"],plan)

    def _read(self, invocation,sources,plan,raw,nonce,*,final=None,identity_change=False,installed=None):
        kit = plan["identity"]["kit"]
        if installed is None:
            installed = KitInstallation(kit["sha"],kit["version"],kit["tree_digest"],3,3,1,11)
        files = {file.path:file.data for file in (sources.config,*sources.files)}
        with ExitStack() as stack:
            self._seams(stack)
            stack.enter_context(patch.object(request,"_read",side_effect=[raw,raw if final is None else final]))
            stack.enter_context(patch.object(request,"read_privileged_kit_installation",return_value=installed))
            reading = stack.enter_context(patch.object(request,"read_child_file",side_effect=lambda root,path,**kw: files[path]))
            if identity_change:
                stack.enter_context(patch.object(request,"_inspect_sources",return_value=(1,99)))
            context = request.read_root_freeze_request(invocation,boundary=BOUNDARY,validator=VALIDATOR,nonce=nonce)
            for call in reading.call_args_list:
                self.assertEqual(call.args[0],Path(str(request.CONTROLLER_VALIDATION_ROOT)))
        return context

    def test_reader_reconstructs_actual_source_bytes_including_empty_modules(self):
        invocation,sources,plan,envelope = fixture(empty=True)
        nonce,raw = self._published(invocation,sources,plan,envelope)
        context = self._read(invocation,sources,plan,raw,nonce)
        self.assertEqual(context.sources,sources)
        self.assertTrue(any(file.data==b"" for file in context.sources.files))
        self.assertEqual(context.execution_nonce,"a"*64)
        self.assertEqual(context.envelope,envelope)

    def test_reader_rejects_nonce_canonicality_record_and_copy_root_drift(self):
        invocation,sources,plan,envelope = fixture()
        nonce,raw = self._published(invocation,sources,plan,envelope)
        for kwargs in (dict(nonce="f"*64),dict(raw=raw+b" "),dict(final=raw+b" "),dict(identity_change=True)):
            args=dict(raw=raw,nonce=nonce)
            args.update(kwargs)
            with self.subTest(args=kwargs),self.assertRaises(MbError):
                self._read(invocation,sources,plan,**args)

    def test_reader_binds_retained_host_validator_invocation_and_installed_kit(self):
        from mod_base.io.secure_json import loads
        invocation,sources,plan,envelope = fixture()
        nonce,raw = self._published(invocation,sources,plan,envelope)
        document = loads(raw,label="fixture",max_bytes=len(raw))
        for section,key,value in (("boundary","inode",11),("validator","uid",2002)):
            changed = copy.deepcopy(document)
            changed[section][key] = value
            with self.subTest(section=section),self.assertRaises(MbError):
                self._read(invocation,sources,plan,canonical_json(changed),nonce)
        for changed in (object(),replace(invocation,implementation_sha_override="f"*40)):
            with self.subTest(invocation=changed),self.assertRaises(MbError):
                self._read(changed,sources,plan,raw,nonce)
        kit = plan["identity"]["kit"]
        installed = KitInstallation(kit["sha"],kit["version"],kit["tree_digest"],3,3,1,11)
        for key,value in (("kit_sha","f"*40),("kit_version","9.0.0"),("digest","sha256:"+"f"*64)):
            with self.subTest(kit=key),self.assertRaises(MbError):
                self._read(invocation,sources,plan,raw,nonce,installed=replace(installed,**{key:value}))

    def test_restore_rejects_corrupt_bytes_size_and_native_source_config(self):
        from mod_base.io.secure_json import loads
        invocation,sources,plan,envelope = fixture()
        _,raw = self._published(invocation,sources,plan,envelope)
        document = loads(raw,label="fixture",max_bytes=len(raw))
        files = {file.path:file.data for file in (sources.config,*sources.files)}
        for changed in (b"corrupt",b"x"*len(sources.files[0].data)):
            altered = dict(files)
            altered[sources.files[0].path] = changed
            with patch.object(request,"read_child_file",side_effect=lambda root,path,**kw: altered[path]),self.assertRaises(MbError):
                request._restore_sources(document)
        changed = copy.deepcopy(document)
        changed["sources"]["config"]["sha256"] = "f"*64
        with patch.object(request,"read_child_file",side_effect=lambda root,path,**kw: files[path]),self.assertRaises(MbError):
            request._restore_sources(changed)

    def test_role_failure_precedes_reads_and_publication(self):
        invocation,sources,plan,envelope = fixture()
        for operation,role in ((lambda:request.record_root_freeze_request(invocation,boundary=BOUNDARY,validator=VALIDATOR,
            sources=sources,plan=plan,envelope=envelope,run_id=42,run_attempt=2,execution_nonce="a"*64),"authenticate_host_boundary"),
            (lambda:request.read_root_freeze_request(invocation,boundary=BOUNDARY,validator=VALIDATOR,nonce="a"*64),"authenticate_privileged_host_boundary")):
            with patch.object(request,role,side_effect=WorkerError("role")),patch.object(request,"_read") as reading, \
                    patch.object(request,"atomic_directory") as publication,self.assertRaises(MbError):
                operation()
            reading.assert_not_called()
            publication.assert_not_called()

    def test_fixed_freeze_delegates_execution_nonce_rechecks_and_terminates(self):
        invocation,sources,plan,envelope = fixture()
        context=request.RootFreezeContext(sources,plan,envelope,42,2,"a"*64)
        changed=request.RootFreezeContext(sources,plan,envelope,42,2,"b"*64)
        for result in (context,changed,WorkerError("post-read")):
            with self.subTest(result=result),ExitStack() as stack:
                self._seams(stack)
                stack.enter_context(patch.object(request,"read_root_freeze_request",side_effect=[context,result]))
                freezing=stack.enter_context(patch.object(request,"freeze_handed_off_build_validation",return_value={"fixture":"receipt"}))
                stopping=stack.enter_context(patch.object(request,"terminate_worker"))
                if result==context:
                    self.assertEqual(request.freeze_root_requested_build_validation(invocation,boundary=BOUNDARY,
                        validator=VALIDATOR,nonce="f"*64),{"fixture":"receipt"})
                else:
                    with self.assertRaises(MbError):
                        request.freeze_root_requested_build_validation(invocation,boundary=BOUNDARY,validator=VALIDATOR,nonce="f"*64)
                self.assertEqual(freezing.call_args.kwargs["nonce"],"a"*64)
                stopping.assert_called_once_with(VALIDATOR)

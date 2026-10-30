"""tv-collect: one live collection over a FAKE injected adapter, plus both-way guard controls.

Everything here is synthetic: synthetic report/frame/policy/profile files, a synthetic
"signing" (two placeholder XML entries) around the reviewed probe sources, and a fake
operation provider. No TV, network, binary, SDK, certificate or real device value is used.

Run: python -B -m unittest tests.test_helper_live_collect
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import traceback
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "helper"))
sys.path.insert(0, str(REPO / "public-toolkit"))
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO / "tools" / "tv-device-facts"))
import pack  # noqa: E402
from tvkit import livecollect as lc  # noqa: E402
from tvkit import liveops  # noqa: E402
import session as hs  # noqa: E402
from tvkit import factsjoin as fj  # noqa: E402
from test_public_toolkit_factsjoin import (  # noqa: E402
    DUID_DIGEST, _encode, failed_frame, native_doc, product_info_dict,
)

PROFILE_FIXTURE = REPO / "tests" / "fixtures" / "consumer" / "profile-v2.json"
PACKAGE = "A1B2C3D4E5"
APP_ID = PACKAGE + ".Facts"
SECRET_MARKER = "SYNTHETIC-PROVIDER-SECRET-LEAK"

RUNTIMECONFIG = json.dumps({
    "runtimeOptions": {"tfm": "net6.0",
                       "framework": {"name": "Microsoft.NETCore.App", "version": "6.0.0"}},
}).encode("utf-8")
DEPS = json.dumps({
    "runtimeTarget": {"name": ".NETCoreApp,Version=v6.0"},
    "targets": {"net6.0": {}}, "libraries": {},
}).encode("utf-8")
DLL = b"SYNTHETIC-INSPECTOR-DLL\x00" * 8


def sha(data):
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- synthetic fixtures

def make_wgt(*, package=PACKAGE, extra=None, symlink=None):
    """A ZIP shaped like a signed probe widget; signatures are placeholders."""
    files = pack.assemble(package + ".Facts", package, "0.1.0")
    files["author-signature.xml"] = b"<signature/>\n"
    files["signature1.xml"] = b"<signature/>\n"
    if extra:
        files.update(extra)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_STORED
            if symlink == name:
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, files[name])
    return buf.getvalue()


def report_bytes(**overrides):
    obj = product_info_dict()
    if overrides:
        obj.update(overrides)
    return json.dumps(obj).encode("utf-8")


def write_sources(root):
    data = {
        "policy": fj._SYNTHETIC_POLICY_JSON.encode("utf-8"),
        "profile": PROFILE_FIXTURE.read_bytes(),
    }
    paths = {}
    for name, body in data.items():
        path = root / f"{name}.json"
        path.write_bytes(body)
        paths[name] = path
    return paths


def write_native(root):
    paths = {}
    for key, name, body in (("dll", lc.DLL_NAME, DLL),
                            ("runtimeconfig", lc.RUNTIMECONFIG_NAME, RUNTIMECONFIG),
                            ("deps", lc.DEPS_NAME, DEPS)):
        path = root / name
        path.write_bytes(body)
        paths[key] = path
    return paths


# ---------------------------------------------------------------- fake adapter

class _MalformedCleanupField:
    """A hostile field value whose repr carries a secret."""

    def __repr__(self):
        return f"<{SECRET_MARKER}>"


class FakeOps(liveops.LiveOps):
    name = "fake"
    implemented = True

    def __init__(self, **session_options):
        self.session_options = session_options
        self.session_calls = []
        self.session_obj = None
        self.calls = []
        self.uninstalled = []
        self.cleaned = []
        self.closed = 0
        self.pushed_sha = None
        self.fail_on = session_options.pop("fail_on", None)
        self.fail_exc = session_options.pop("fail_exc", None)
        self.reconcile_unknown = session_options.pop("reconcile_unknown", False)
        self.report = session_options.pop("report", None)
        self.frame = session_options.pop("frame", None)
        self.session_error = session_options.pop("session_error", None)
        self.extra_options = session_options

    def session(self, run_id):
        self.session_calls.append(run_id)
        if self.session_error is not None:
            raise self.session_error
        self.session_obj = FakeSession(self, run_id, **self.extra_options)
        return self.session_obj


class FakeSession(liveops.LiveSession):
    def __init__(self, provider, run_id, *, token="session-A", binding=None,
                 absent=True, exit_code=0, native_token=None, native_binding=None,
                 absence_token=None, absence_binding=None, fail_on=None, fail_exc=None,
                 drift_after=None, reconcile_absent_after=False,
                 reconcile_remove_unowned=False, malformed_cleanup=False,
                 cleanup_error=None, close_error=None):
        self.provider = provider
        self.run_id = run_id
        self._token = token
        self._binding = binding if binding is not None else object()
        self.absent = absent
        self.exit_code = exit_code
        self.native_token = native_token
        self.native_binding = native_binding
        self.absence_token = absence_token
        self.absence_binding = absence_binding
        self.fail_on = provider.fail_on if fail_on is None else fail_on
        self.fail_exc = provider.fail_exc if fail_exc is None else fail_exc
        self.drift_after = drift_after
        self.reconcile_unknown = provider.reconcile_unknown
        self.reconcile_absent_after = reconcile_absent_after
        self.reconcile_remove_unowned = reconcile_remove_unowned
        self.malformed_cleanup = malformed_cleanup
        self.cleanup_error = cleanup_error
        self.close_error = close_error

    @property
    def session_token(self):
        return self._token

    @property
    def binding(self):
        return self._binding

    def _result(self, cls, **fields):
        return cls(ok=True, run_id=self.run_id, session_token=self._token,
                   binding=self._binding, **fields)

    def _maybe_fail(self, op):
        if self.fail_on == op:
            raise self.fail_exc if self.fail_exc is not None else liveops.LiveOpsError(SECRET_MARKER)

    def _maybe_drift(self, op):
        if self.drift_after == op:
            self._token = "session-changed"
            self._binding = object()

    def assert_absent(self, package_id, data_paths):
        self.provider.calls.append(("assert_absent", package_id, data_paths))
        self._maybe_fail("assert_absent")
        result = liveops.AbsenceResult(
            ok=True, run_id=self.run_id,
            session_token=self.absence_token if self.absence_token is not None else self._token,
            binding=self.absence_binding if self.absence_binding is not None else self._binding,
            package_absent=self.absent, data_absent=self.absent)
        self._maybe_drift("assert_absent")
        return result

    def install_widget(self, package_id, wgt, wgt_sha, remote_tmp_wgt):
        self.provider.calls.append(("install_widget", package_id, wgt, wgt_sha, remote_tmp_wgt))
        self.provider.pushed_sha = wgt_sha
        self._maybe_fail("install")
        result = self._result(liveops.InstallResult, pushed=True, install_attempted=True, installed=True)
        self._maybe_drift("install")
        return result

    def launch_facts(self, package_id):
        self.provider.calls.append(("launch_facts", package_id))
        self._maybe_fail("launch_facts")
        result = self._result(liveops.LaunchResult, launched=True)
        self._maybe_drift("launch_facts")
        return result

    def read_report(self, package_id, data_paths):
        self.provider.calls.append(("read_report", package_id, data_paths))
        self._maybe_fail("read_report")
        body = self.provider.report if self.provider.report is not None else report_bytes()
        result = self._result(liveops.ReportResult, data=body)
        self._maybe_drift("read_report")
        return result

    def push_native(self, artifacts, remote_tmp_dir):
        self.provider.calls.append(("push_native", artifacts, remote_tmp_dir))
        self._maybe_fail("push_native")
        result = self._result(liveops.OpResult)
        self._maybe_drift("push_native")
        return result

    def run_native(self, remote_dll):
        self.provider.calls.append(("run_native", remote_dll))
        self._maybe_fail("run_native")
        stdout = self.provider.frame if self.provider.frame is not None else _encode(native_doc())
        result = liveops.NativeResult(
            ok=True, run_id=self.run_id,
            session_token=self.native_token if self.native_token is not None else self._token,
            binding=self.native_binding if self.native_binding is not None else self._binding,
            exit_code=self.exit_code, stdout=stdout)
        self._maybe_drift("run_native")
        return result

    def close_owned_run(self, owned):
        self.provider.cleaned.append(owned)
        if self.cleanup_error is not None:
            raise self.cleanup_error
        if self.malformed_cleanup:
            return liveops.CleanupReport(
                package_removed=False, package_absent_after=False,
                removed_uploads=_MalformedCleanupField(), unconfirmed_uploads=(),
                recovery_needed=False)
        uploads = tuple(u.remote_path for u in owned.uploads)
        if self.reconcile_unknown:
            return liveops.CleanupReport(
                package_removed=False, package_absent_after=False, removed_uploads=(),
                unconfirmed_uploads=uploads, recovery_needed=True)
        if self.reconcile_remove_unowned:
            # A provider that "fixes" a namespace it never proved it owned.
            self.provider.uninstalled.append(owned.package_id)
            return liveops.CleanupReport(
                package_removed=True, package_absent_after=True, removed_uploads=uploads,
                unconfirmed_uploads=(), recovery_needed=False)
        if self.reconcile_absent_after:
            # A partial/failed install that left nothing: absence verified, no uninstall.
            return liveops.CleanupReport(
                package_removed=False, package_absent_after=True, removed_uploads=uploads,
                unconfirmed_uploads=(), recovery_needed=False)
        owned_package = owned.absence_confirmed and owned.install_attempted
        if owned_package:
            self.provider.uninstalled.append(owned.package_id)
        return liveops.CleanupReport(
            package_removed=owned_package, package_absent_after=owned_package,
            removed_uploads=uploads, unconfirmed_uploads=(), recovery_needed=False)

    def close(self):
        self.provider.closed += 1
        if self.close_error is not None:
            raise self.close_error


class LiveCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="quiettv-live-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.owner = self.root / "owner"
        self.owner.mkdir()
        self.sources = write_sources(self.owner)
        self.native = write_native(self.owner)
        self.wgt_bytes = make_wgt()
        self.wgt_path = self.owner / "probe.wgt"
        self.wgt_path.write_bytes(self.wgt_bytes)
        self.out_dir = self.root / "out"
        self.ops = FakeOps()

    def kwargs(self, **over):
        kw = dict(
            ops=self.ops, out_dir=str(self.out_dir), package_id=PACKAGE,
            signed_wgt=str(self.wgt_path), wgt_sha256=sha(self.wgt_bytes),
            inspector_dll=str(self.native["dll"]), dll_sha256=sha(DLL),
            inspector_runtimeconfig=str(self.native["runtimeconfig"]),
            runtimeconfig_sha256=sha(RUNTIMECONFIG),
            inspector_deps=str(self.native["deps"]), deps_sha256=sha(DEPS),
            policy=str(self.sources["policy"]), profile=str(self.sources["profile"]),
            operator_trust_policy_and_profile=True, operator_paired_from_same_tv=True)
        kw.update(over)
        return kw

    def files(self, out=None):
        out = Path(out or self.out_dir)
        return {name: out / name for name in (*lc.ARTIFACT_NAMES, lc.COLLECTION_NAME)}

    def assert_no_artifacts(self, out=None):
        for name, path in self.files(out).items():
            self.assertFalse(path.exists(), name)


# ---------------------------------------------------------------- positive path

class SuccessfulCollectionTests(LiveCase):
    def test_collector_caps_fit_helper_saved_sources(self):
        limits = dict(hs.PREFLIGHT_SOURCES)
        self.assertLessEqual(liveops.MAX_REPORT_BYTES, limits["productInfoReport"])
        self.assertLessEqual(liveops.MAX_NATIVE_FRAME_BYTES, limits["nativeInspectionFrame"])

    def test_two_artifacts_receipt_and_seven_facts_with_real_helper_preflight(self):
        summary = lc.collect_reports(**self.kwargs())
        self.assertEqual(summary["status"], "collected")
        self.assertEqual(summary["preflightFields"], 7)
        self.assertEqual(summary["cleanup"], "complete")
        for name, path in self.files().items():
            self.assertTrue(path.is_file(), name)

        # The adapter's exact ordering and computed values.
        kinds = [call[0] for call in self.ops.calls]
        self.assertEqual(kinds, ["assert_absent", "install_widget", "launch_facts",
                                 "read_report", "push_native", "run_native"])
        self.assertEqual(self.ops.calls[0][2], liveops.data_paths(PACKAGE))
        self.assertEqual(self.ops.uninstalled, [PACKAGE])
        self.assertEqual(self.ops.closed, 1)

        receipt = json.loads((self.out_dir / lc.RECEIPT_NAME).read_text("utf-8"))
        self.assertEqual(len(receipt["facts"]), 8)  # seven match fields + the opaque deviceId
        self.assertEqual(receipt["productInfoSha256"], summary["productInfoSha256"])

        # The EXISTING Helper loader and the EXISTING offline preflight reproduce the facts.
        cfg = hs.load_preflight_config(str(self.out_dir / lc.PREFLIGHT_NAME))
        state = hs.Session(preflight_config=cfg).run_preflight()
        self.assertEqual(state["preflight"]["status"], "success")
        result = state["preflight"]["result"]
        self.assertEqual(len(result["fields"]), 7)
        values = {field["field"]: field["value"] for field in result["fields"]}
        self.assertEqual(values["model"], "SYNTHETIC-QTV0001")
        self.assertEqual(values["arch"], "armv7l")
        self.assertEqual(values["firmwareBuild"], "SYNTHETIC-BUILD-0000.1")
        self.assertTrue(all(not action["enabled"] for action in state["actions"].values()))

        # No session token, target identity or source path leaks into the public summary,
        # the collector's own records or the Helper view. The EXACT report artifact
        # necessarily carries the on-device DUID digest; that is a hash, not a raw DUID.
        public = json.dumps(summary) + json.dumps(state)
        for own in (lc.COLLECTION_NAME, lc.RECEIPT_NAME, lc.PREFLIGHT_NAME, lc.REPORT_NAME, lc.FRAME_NAME):
            if own != lc.REPORT_NAME:
                public += (self.out_dir / own).read_text("utf-8", "replace")
        self.assertNotIn(self.ops.session_obj.session_token, public)
        self.assertNotIn(DUID_DIGEST, json.dumps(summary) + json.dumps(state))
        self.assertNotIn(DUID_DIGEST, (self.out_dir / lc.COLLECTION_NAME).read_text("utf-8"))
        self.assertNotIn(str(self.owner), json.dumps(summary))
        collection = json.loads((self.out_dir / lc.COLLECTION_NAME).read_text("utf-8"))
        self.assertEqual(collection["cleanup"]["status"], "complete")

    def test_identical_fresh_bytes_across_two_runs_are_accepted(self):
        # Correction: freshness is lifecycle evidence, never byte novelty.
        first = lc.collect_reports(**self.kwargs())
        second_out = self.root / "out-2"
        ops2 = FakeOps()
        second = lc.collect_reports(**self.kwargs(ops=ops2, out_dir=str(second_out)))
        self.assertEqual(first["productInfoSha256"], second["productInfoSha256"])
        self.assertEqual(first["inspectionSha256"], second["inspectionSha256"])
        self.assertTrue((second_out / lc.REPORT_NAME).is_file())
        self.assertEqual(ops2.uninstalled, [PACKAGE])

    def test_report_and_frame_bytes_are_exactly_what_was_written(self):
        lc.collect_reports(**self.kwargs())
        self.assertEqual((self.out_dir / lc.REPORT_NAME).read_bytes(), report_bytes())
        self.assertEqual((self.out_dir / lc.FRAME_NAME).read_bytes(), _encode(native_doc()))


# ---------------------------------------------------------------- refusal paths

class AdapterRefusalTests(LiveCase):
    def test_no_adapter_instance_refuses_before_any_tv_work(self):
        with self.assertRaises(lc.LiveCollectionError):
            lc.collect_reports(**self.kwargs(ops=None))
        self.assertFalse(self.out_dir.exists())

    def test_stale_or_colliding_namespace_refuses_without_install_or_uninstall(self):
        ops = FakeOps(absent=False)
        with self.assertRaises(lc.LiveCollectionError):
            lc.collect_reports(**self.kwargs(ops=ops))
        kinds = [call[0] for call in ops.calls]
        self.assertEqual(kinds, ["assert_absent"])
        self.assertEqual(ops.uninstalled, [])
        self.assertFalse(self.out_dir.exists())
        # Cleanup still ran and reported nothing owned.
        self.assertEqual(len(ops.cleaned), 1)
        self.assertFalse(ops.cleaned[0].absence_confirmed)

    def test_cross_session_token_refuses(self):
        ops = FakeOps(native_token="session-B")
        with self.assertRaises(lc.LiveCollectionError):
            lc.collect_reports(**self.kwargs(ops=ops))
        self.assert_no_artifacts()

    def test_mixed_target_binding_refuses(self):
        ops = FakeOps(native_binding=object())
        with self.assertRaises(lc.LiveCollectionError):
            lc.collect_reports(**self.kwargs(ops=ops))
        self.assert_no_artifacts()

    def test_native_nonzero_exit_with_valid_looking_frame_refuses(self):
        ops = FakeOps(exit_code=1)
        with self.assertRaises(lc.LiveCollectionError):
            lc.collect_reports(**self.kwargs(ops=ops))
        self.assert_no_artifacts()

    def test_transport_trailer_is_not_stripped_by_the_collector(self):
        # frame.py refuses unknown noise; only the adapter may strip exactly one trailer.
        ops = FakeOps(frame=_encode(native_doc()) + b"[exit:0]\n")
        with self.assertRaises(lc.LiveCollectionError):
            lc.collect_reports(**self.kwargs(ops=ops))
        self.assert_no_artifacts()

    def test_provider_error_is_not_echoed_and_cleanup_is_reconciled(self):
        ops = FakeOps(fail_on="install", reconcile_unknown=True)
        with self.assertRaises(lc.LiveCollectionError) as ctx:
            lc.collect_reports(**self.kwargs(ops=ops))
        self.assertNotIn(SECRET_MARKER, str(ctx.exception))
        self.assertEqual(ops.uninstalled, [])          # ownership could not be confirmed
        self.assertEqual(len(ops.cleaned), 1)
        self.assertTrue(ops.cleaned[0].install_attempted)
        status = json.loads((self.out_dir / lc.COLLECTION_NAME).read_text("utf-8"))
        self.assertEqual(status["cleanup"]["status"], "recovery-needed")

    def test_cleanup_failure_after_a_valid_report_is_surfaced_and_artifacts_kept(self):
        ops = FakeOps(reconcile_unknown=True)
        with self.assertRaises(lc.LiveCollectionError):
            lc.collect_reports(**self.kwargs(ops=ops))
        for name in (lc.REPORT_NAME, lc.FRAME_NAME, lc.RECEIPT_NAME, lc.PREFLIGHT_NAME):
            self.assertTrue((self.out_dir / name).is_file(), name)
        status = json.loads((self.out_dir / lc.COLLECTION_NAME).read_text("utf-8"))
        self.assertEqual(status["cleanup"]["status"], "recovery-needed")


class FrozenBindingTests(LiveCase):
    """The session pair is frozen at open and checked after each op / before each op."""

    def assert_no_provider_text(self, exc):
        self.assertNotIn(SECRET_MARKER, str(exc))
        self.assertNotIn(SECRET_MARKER, "".join(traceback.format_exception(exc)))

    def test_wrong_absence_token_or_binding_refuses_before_install(self):
        for label, kw in {
            "token": dict(absence_token="SYNTHETIC-OTHER-SESSION"),
            "binding": dict(absence_binding=object()),
        }.items():
            with self.subTest(case=label):
                ops = FakeOps(**kw)
                with self.assertRaises(lc.LiveCollectionError) as ctx:
                    lc.collect_reports(**self.kwargs(ops=ops))
                self.assertEqual([call[0] for call in ops.calls], ["assert_absent"])
                self.assertEqual(ops.uninstalled, [])
                self.assert_no_provider_text(ctx.exception)
                self.assertFalse(self.out_dir.exists())

    def test_changed_session_binding_between_ops_refuses_before_the_next_write(self):
        ops = FakeOps(drift_after="assert_absent")
        with self.assertRaises(lc.LiveCollectionError) as ctx:
            lc.collect_reports(**self.kwargs(ops=ops))
        self.assert_no_provider_text(ctx.exception)
        self.assertEqual([call[0] for call in ops.calls], ["assert_absent"])
        self.assertEqual(ops.uninstalled, [])
        self.assertEqual(ops.cleaned, [])        # never reconcile a changed target
        self.assertFalse(self.out_dir.exists())

    def test_session_open_failure_is_finite_and_not_echoed(self):
        ops = FakeOps(session_error=RuntimeError(SECRET_MARKER))
        with self.assertRaises(lc.LiveCollectionError) as ctx:
            lc.collect_reports(**self.kwargs(ops=ops))
        self.assert_no_provider_text(ctx.exception)
        self.assertEqual(ops.calls, [])
        self.assertFalse(self.out_dir.exists())

    def test_cleanup_scoped_to_the_initial_binding_never_uninstalls_a_drifted_target(self):
        # Binding changes after install: cleanup is skipped even though ownership exists.
        ops = FakeOps(drift_after="install")
        with self.assertRaises(lc.LiveCollectionError) as ctx:
            lc.collect_reports(**self.kwargs(ops=ops))
        self.assertIn("install_widget", [call[0] for call in ops.calls])
        self.assertEqual(ops.uninstalled, [])
        self.assertEqual(ops.cleaned, [])
        self.assertIn("recovery", str(ctx.exception).lower())
        status = json.loads((self.out_dir / lc.COLLECTION_NAME).read_text("utf-8"))
        self.assertEqual(status["cleanup"]["status"], "recovery-needed")
        self.assertEqual(status["namespace"]["installAttempted"], True)
        self.assertEqual(len(status["namespace"]["uploads"]), 1)


class ProviderFailureContainmentTests(LiveCase):
    def test_op_runtime_error_and_malicious_livecollectionerror_are_not_echoed(self):
        for label, exc in {
            "runtime": RuntimeError(SECRET_MARKER),
            "live": lc.LiveCollectionError(SECRET_MARKER),
        }.items():
            with self.subTest(kind=label):
                out = self.root / f"out-{label}"
                ops = FakeOps(fail_on="install", fail_exc=exc, reconcile_unknown=True)
                with self.assertRaises(lc.LiveCollectionError) as ctx:
                    lc.collect_reports(**self.kwargs(ops=ops, out_dir=str(out)))
                self.assertNotIn(SECRET_MARKER, str(ctx.exception))
                self.assertNotIn(SECRET_MARKER, "".join(traceback.format_exception(ctx.exception)))
                self.assertIn("recovery", str(ctx.exception).lower())
                self.assertNotIn(SECRET_MARKER, (out / lc.COLLECTION_NAME).read_text("utf-8"))

    def test_both_primary_and_cleanup_failure_surface_recovery_first(self):
        ops = FakeOps(fail_on="install", reconcile_unknown=True)
        with self.assertRaises(lc.LiveCollectionError) as ctx:
            lc.collect_reports(**self.kwargs(ops=ops))
        self.assertIn("recovery", str(ctx.exception).lower())
        self.assertNotIn(SECRET_MARKER, str(ctx.exception))
        status = json.loads((self.out_dir / lc.COLLECTION_NAME).read_text("utf-8"))
        self.assertEqual(status["cleanup"]["status"], "recovery-needed")
        self.assertEqual(status["primaryFailure"]["category"], "refused")
        namespace = status["namespace"]
        self.assertEqual(namespace["packageId"], PACKAGE)
        self.assertRegex(namespace["remoteRunDir"], r"/home/owner/share/tmp/sdk_tools/quiettv-collect-[0-9a-f]{32}$")
        self.assertEqual(namespace["absenceConfirmed"], True)
        self.assertEqual(namespace["installAttempted"], True)
        self.assertEqual([u["sha256"] for u in namespace["uploads"]], [sha(self.wgt_bytes)])
        self.assertNotIn("session-A", (self.out_dir / lc.COLLECTION_NAME).read_text("utf-8"))
        self.assertNotIn(SECRET_MARKER, (self.out_dir / lc.COLLECTION_NAME).read_text("utf-8"))

    def test_malformed_cleanup_report_types_are_finite(self):
        ops = FakeOps(malformed_cleanup=True)
        with self.assertRaises(lc.LiveCollectionError) as ctx:
            lc.collect_reports(**self.kwargs(ops=ops))
        self.assertIn("recovery", str(ctx.exception).lower())
        self.assertNotIn(SECRET_MARKER, str(ctx.exception))
        self.assertNotIn(SECRET_MARKER, "".join(traceback.format_exception(ctx.exception)))
        self.assertNotIn(SECRET_MARKER, (self.out_dir / lc.COLLECTION_NAME).read_text("utf-8"))

    def test_cleanup_exception_is_finite_and_keeps_a_recovery_record(self):
        ops = FakeOps(cleanup_error=RuntimeError(SECRET_MARKER))
        with self.assertRaises(lc.LiveCollectionError) as ctx:
            lc.collect_reports(**self.kwargs(ops=ops))
        self.assertIn("recovery", str(ctx.exception).lower())
        self.assertNotIn(SECRET_MARKER, str(ctx.exception))
        self.assertEqual(json.loads((self.out_dir / lc.COLLECTION_NAME).read_text(
            "utf-8"))["cleanup"]["status"], "recovery-needed")

    def test_partial_install_cleanup_may_confirm_absence_without_uninstalling(self):
        ops = FakeOps(fail_on="install", reconcile_absent_after=True)
        with self.assertRaises(lc.LiveCollectionError) as ctx:
            lc.collect_reports(**self.kwargs(ops=ops))
        self.assertNotIn("recovery", str(ctx.exception).lower())
        self.assertEqual(ops.uninstalled, [])
        self.assert_no_artifacts()

    def test_cleanup_removing_a_package_it_never_owned_is_refused(self):
        ops = FakeOps(absent=False, reconcile_remove_unowned=True)
        with self.assertRaises(lc.LiveCollectionError) as ctx:
            lc.collect_reports(**self.kwargs(ops=ops))
        self.assertIn("recovery", str(ctx.exception).lower())
        self.assertEqual(ops.uninstalled, [PACKAGE])   # the provider did the unsafe thing
        self.assertFalse(self.out_dir.exists())        # no install happened, so no record needed

    def test_output_dir_allocation_failure_happens_before_the_first_tv_write(self):
        with mock.patch.object(lc, "_create_out_dir",
                               side_effect=lc.LiveCollectionError(
                                   "the output directory could not be created")):
            with self.assertRaises(lc.LiveCollectionError):
                lc.collect_reports(**self.kwargs())
        self.assertEqual([call[0] for call in self.ops.calls], ["assert_absent"])


class ObservationRefusalTests(LiveCase):
    def assert_refused_before_push(self, report=None, frame=None, exit_code=0, *, before_push=True):
        ops = FakeOps(report=report, frame=frame, exit_code=exit_code)
        with self.assertRaises(lc.LiveCollectionError):
            lc.collect_reports(**self.kwargs(ops=ops))
        kinds = [call[0] for call in ops.calls]
        if before_push:
            self.assertNotIn("push_native", kinds)
        self.assert_no_artifacts()

    def test_oversize_report_refused(self):
        self.assert_refused_before_push(report=b"{" + b"x" * liveops.MAX_REPORT_BYTES)

    def test_incomplete_report_refused(self):
        obj = product_info_dict()
        obj["status"] = "incomplete"
        self.assert_refused_before_push(report=json.dumps(obj).encode())

    def test_wrong_report_schema_refused(self):
        obj = product_info_dict()
        obj["schema"] = "quiettv-device-facts/0"
        self.assert_refused_before_push(report=json.dumps(obj).encode())

    def test_raw_duid_in_identity_slot_refused(self):
        obj = product_info_dict()
        obj["identity"]["digest"] = "RAWDUID12345"
        self.assert_refused_before_push(report=json.dumps(obj).encode())

    def test_policy_drift_in_the_fresh_report_refused(self):
        obj = product_info_dict()
        obj["fields"]["realModel"]["value"] = "DRIFTED-MODEL"
        ops = FakeOps(report=json.dumps(obj).encode())
        with self.assertRaises(lc.LiveCollectionError):
            lc.collect_reports(**self.kwargs(ops=ops))
        self.assertIn("install_widget", [call[0] for call in ops.calls])
        self.assert_no_artifacts()

    def test_failed_native_run_refused(self):
        ops = FakeOps(frame=failed_frame())
        with self.assertRaises(lc.LiveCollectionError):
            lc.collect_reports(**self.kwargs(ops=ops))
        self.assert_no_artifacts()

    def test_native_output_over_cap_refused(self):
        # The native run necessarily happens after its push; the cap is still a refusal.
        self.assert_refused_before_push(frame=b"x" * (liveops.MAX_NATIVE_FRAME_BYTES + 1),
                                        before_push=False)


class WidgetValidationTests(LiveCase):
    def test_remote_entrypoint_is_not_the_reviewed_probe(self):
        config = pack.assemble(APP_ID, PACKAGE, "0.1.0")["config.xml"]
        changed = config.replace(b'src="index.html"', b'src="https://media.example/other.html"')
        self.assertNotEqual(config, changed)
        self.wgt_bytes = make_wgt(extra={"config.xml": changed})
        self.wgt_path.write_bytes(self.wgt_bytes)
        with self.assertRaises(lc.LiveCollectionError):
            lc.collect_reports(**self.kwargs())
        self.assertEqual(self.ops.session_calls, [])

    def test_package_mismatch_refused_before_any_tv_operation(self):
        other = make_wgt(package="Z9Y8X7W6V5")
        wgt = self.owner / "other.wgt"
        wgt.write_bytes(other)
        ops = FakeOps()
        with self.assertRaises(lc.LiveCollectionError):
            lc.collect_reports(**self.kwargs(ops=ops, signed_wgt=str(wgt), wgt_sha256=sha(other)))
        self.assertEqual(ops.session_calls, [])
        self.assertEqual(ops.calls, [])

    def test_extra_member_traversal_and_symlink_refused(self):
        cases = {
            "extra": make_wgt(extra={"notes.txt": b"x"}),
            "traversal": make_wgt(extra={"../evil": b"x"}),
            "symlink": make_wgt(symlink="index.html"),
        }
        for label, body in cases.items():
            with self.subTest(case=label):
                path = self.owner / f"{label}.wgt"
                path.write_bytes(body)
                ops = FakeOps()
                with self.assertRaises(lc.LiveCollectionError):
                    lc.collect_reports(**self.kwargs(ops=ops, signed_wgt=str(path), wgt_sha256=sha(body)))
                self.assertEqual(ops.session_calls, [])

    def test_mismatched_pins_refused_before_any_tv_operation(self):
        wrong = "0" * 64
        for label, kw in {
            "wgt": dict(wgt_sha256=wrong),
            "dll": dict(dll_sha256=wrong),
            "runtimeconfig": dict(runtimeconfig_sha256=wrong),
            "deps": dict(deps_sha256=wrong),
        }.items():
            with self.subTest(pin=label):
                ops = FakeOps()
                with self.assertRaises(lc.LiveCollectionError):
                    lc.collect_reports(**self.kwargs(ops=ops, **kw))
                self.assertEqual(ops.session_calls, [])

    def test_runtimeconfig_must_be_the_pinned_net6_framework_document(self):
        bad = json.dumps({"runtimeOptions": {"tfm": "net8.0",
                                            "framework": {"name": "Microsoft.NETCore.App",
                                                          "version": "8.0.0"}}}).encode()
        path = self.owner / "bad.runtimeconfig"
        path.write_bytes(bad)
        ops = FakeOps()
        with self.assertRaises(lc.LiveCollectionError):
            lc.collect_reports(**self.kwargs(
                ops=ops, inspector_runtimeconfig=str(path), runtimeconfig_sha256=sha(bad)))
        self.assertEqual(ops.session_calls, [])

    def test_policy_and_profile_must_belong_together(self):
        policy = json.loads(fj._SYNTHETIC_POLICY_JSON)
        policy["profileId"] = "SOME-OTHER-PROFILE"
        path = self.owner / "other-policy.json"
        path.write_bytes(json.dumps(policy).encode())
        ops = FakeOps()
        with self.assertRaises(lc.LiveCollectionError):
            lc.collect_reports(**self.kwargs(ops=ops, policy=str(path)))
        self.assertEqual(ops.session_calls, [])


class ContainmentTests(LiveCase):
    def test_output_inside_a_repository_refused(self):
        inside = REPO / "live-collect-out"
        with self.assertRaises(lc.LiveCollectionError):
            lc.collect_reports(**self.kwargs(out_dir=str(inside)))
        self.assertFalse(inside.exists())

    def test_source_inside_a_repository_refused(self):
        with self.assertRaises(lc.LiveCollectionError):
            lc.collect_reports(**self.kwargs(policy=str(REPO / "README.md")))
        self.assertFalse(self.out_dir.exists())

    def test_output_directory_must_not_pre_exist(self):
        self.out_dir.mkdir()
        with self.assertRaises(lc.LiveCollectionError):
            lc.collect_reports(**self.kwargs())
        self.assertEqual(list(self.out_dir.iterdir()), [])
        self.assertEqual(self.ops.calls, [])  # refused before any TV session

    def test_operator_assertions_are_required(self):
        for kw in (dict(operator_trust_policy_and_profile=False),
                   dict(operator_paired_from_same_tv=False)):
            with self.subTest(**kw), self.assertRaises(lc.LiveCollectionError):
                lc.collect_reports(**self.kwargs(**kw))
        self.assertEqual(self.ops.session_calls, [])


# ---------------------------------------------------------------- adapter registry / CLI

class RegistryAndCliTests(LiveCase):
    def test_registry_is_empty_and_get_liveops_fails_closed(self):
        self.assertEqual(liveops.LIVEOPS, {})
        for name in (None, "", "parent-adapter"):
            with self.subTest(name=name), self.assertRaises(liveops.LiveOpsError):
                liveops.get_liveops(name)

    def _cli(self):
        spec = importlib.util.spec_from_file_location(
            "quiettv_tv_collect_cli", REPO / "tools" / "tv-collect" / "collect.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_cli_with_empty_registry_refuses_and_writes_nothing(self):
        cli = self._cli()
        argv = ["--adapter", "parent-adapter", "--out-dir", str(self.out_dir),
                "--signed-wgt", str(self.wgt_path), "--wgt-sha256", sha(self.wgt_bytes),
                "--inspector-dll", str(self.native["dll"]), "--dll-sha256", sha(DLL),
                "--inspector-runtimeconfig", str(self.native["runtimeconfig"]),
                "--runtimeconfig-sha256", sha(RUNTIMECONFIG),
                "--inspector-deps", str(self.native["deps"]), "--deps-sha256", sha(DEPS),
                "--package-id", PACKAGE, "--policy", str(self.sources["policy"]),
                "--profile", str(self.sources["profile"]),
                "--operator-trust-policy-and-profile", "--operator-paired-from-same-tv"]
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = cli.main(argv)
        self.assertEqual(rc, 2)
        self.assertIn("collect refused", err.getvalue())
        self.assertFalse(self.out_dir.exists())

    def test_cli_has_no_module_or_command_parameter(self):
        cli = self._cli()
        argv = ["--adapter", "x", "--out-dir", str(self.out_dir), "--module", "evil.plugin"]
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main(argv)

    def test_cli_never_prints_provider_failure_text(self):
        cli = self._cli()
        liveops.LIVEOPS["leak-test"] = lambda: FakeOps(
            fail_on="install", fail_exc=RuntimeError(SECRET_MARKER), reconcile_unknown=True)
        argv = ["--adapter", "leak-test", "--out-dir", str(self.out_dir),
                "--signed-wgt", str(self.wgt_path), "--wgt-sha256", sha(self.wgt_bytes),
                "--inspector-dll", str(self.native["dll"]), "--dll-sha256", sha(DLL),
                "--inspector-runtimeconfig", str(self.native["runtimeconfig"]),
                "--runtimeconfig-sha256", sha(RUNTIMECONFIG),
                "--inspector-deps", str(self.native["deps"]), "--deps-sha256", sha(DEPS),
                "--package-id", PACKAGE, "--policy", str(self.sources["policy"]),
                "--profile", str(self.sources["profile"]),
                "--operator-trust-policy-and-profile", "--operator-paired-from-same-tv"]
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = cli.main(argv)
        finally:
            liveops.LIVEOPS.pop("leak-test", None)
        self.assertEqual(rc, 2)
        self.assertIn("collect refused", err.getvalue())
        self.assertNotIn(SECRET_MARKER, out.getvalue() + err.getvalue())


# ---------------------------------------------------------------- guard-off controls
# Each control removes ONE isolated guard in memory and shows the exact trap the guard
# stops; the unpatched run refuses. No real data or adapter is involved.

class GuardOffControls(LiveCase):
    def test_absence_guard_is_what_refuses_a_stale_artifact(self):
        with self.assertRaises(lc.LiveCollectionError):
            lc.collect_reports(**self.kwargs(ops=FakeOps(absent=False)))
        with mock.patch.object(lc, "_require_absence", lambda result: None):
            summary = lc.collect_reports(**self.kwargs(ops=FakeOps(absent=False)))
        self.assertEqual(summary["status"], "collected")   # guard removed -> stale accepted

    def test_hash_guard_is_what_refuses_an_unverified_widget(self):
        wrong = "0" * 64
        with self.assertRaises(lc.LiveCollectionError):
            lc.collect_reports(**self.kwargs(wgt_sha256=wrong))
        ops = FakeOps()
        with mock.patch.object(lc, "_require_pin", lambda data, expected, label: expected):
            lc.collect_reports(**self.kwargs(ops=ops, wgt_sha256=wrong))
        self.assertEqual(ops.pushed_sha, wrong)            # guard removed -> unverified pin pushed

    def test_session_binding_guard_is_what_refuses_a_cross_session_pair(self):
        with self.assertRaises(lc.LiveCollectionError):
            lc.collect_reports(**self.kwargs(ops=FakeOps(native_token="session-B")))
        ops = FakeOps(native_token="session-B")
        with mock.patch.object(lc, "_require_session_echo", lambda result, frozen: None):
            summary = lc.collect_reports(**self.kwargs(
                ops=ops, out_dir=str(self.root / "out-guard-off")))
        self.assertEqual(summary["status"], "collected")   # guard removed -> mixed pair accepted


if __name__ == "__main__":
    unittest.main()

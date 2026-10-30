"""helper saved-report preflight: config loading, the real loopback POST path, and refusals.

The positive path runs end to end through the REAL 127.0.0.1 Helper server with a real session
and SYNTHETIC report/frame/policy/profile files written to a temp dir. Nothing patches the core
loader, factsjoin or preflight_display to succeed. The synthetic observations reuse the factsjoin
test helpers; no owner data, hash, token, private profile or TV address is used.
Run: python -B -m unittest tests.test_helper_preflight
"""
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "helper"))
sys.path.insert(0, str(REPO / "tests"))
import server as srv  # noqa: E402
import session as hs  # noqa: E402
from test_helper_session import CountingEngine, FakeReader, OTHER, TARGET  # noqa: E402
from test_helper_server import ServerCase  # noqa: E402 (base case only; it defines no test_ methods)
# Only FUNCTIONS and constants are imported here: importing a TestCase class would make unittest
# collect and run it a second time.
from test_public_toolkit_factsjoin import (  # noqa: E402
    DUID_DIGEST, _encode, failed_frame, native_doc, product_info_dict,
)
from tvkit import factsjoin as fj  # noqa: E402

PROFILE_FIXTURE = REPO / "tests" / "fixtures" / "consumer" / "profile-v2.json"
PROFILE_ID = "SYNTHETIC-QTV0001-0000.1-armv7l-tizen9"
IDENTITY_TOKEN = fj.identity_token(fj.FACTS_IDENTITY_SCHEME, DUID_DIGEST)
EXPECTED_FIELDS = {
    "model": "SYNTHETIC-QTV0001", "firmware": "0000.1", "arch": "armv7l", "osName": "Tizen",
    "osVersion": "9", "firmwareBuild": "SYNTHETIC-BUILD-0000.1", "region": "SYNTHETIC-REGION",
}
EXPECTED_SOURCES = {
    "model": "observed:productInfo.realModel", "arch": "observed:native.machine",
    "firmwareBuild": "observed:productInfo.firmware", "firmware": "interpreted",
    "osName": "interpreted", "osVersion": "interpreted", "region": "interpreted",
}
CONFIG_SCHEMA = "quiettv-helper-preflight/1"


def write_config(tmp, paths, *, assertions=None, drop=None, extra=None, same=None):
    cfg = {
        "schema": CONFIG_SCHEMA,
        "productInfoReport": paths["report"],
        "nativeInspectionFrame": paths["frame"],
        "joinPolicy": paths["policy"],
        "profile": paths["profile"],
        "operatorAssertions": {"trustPolicyAndProfile": True, "pairedFromSameTv": True},
    }
    if assertions is not None:
        cfg["operatorAssertions"] = assertions
    if same is not None:  # point one role at another role's file (ambiguous config)
        cfg[same[0]] = cfg[same[1]]
    if extra is not None:
        cfg.update(extra)
    if drop is not None:
        cfg.pop(drop)
    path = Path(tmp) / "config.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return str(path)


def write_sources(tmp, *, report_obj=None, report_raw=None, frame=None, profile=None):
    if report_raw is not None:
        report = report_raw
    else:
        report = json.dumps(report_obj if report_obj is not None else product_info_dict()).encode("utf-8")
    frame_bytes = frame if frame is not None else _encode(native_doc())
    profile_bytes = profile if profile is not None else PROFILE_FIXTURE.read_bytes()
    paths = {}
    for name, data in (("report", report), ("frame", frame_bytes),
                       ("policy", fj._SYNTHETIC_POLICY_JSON.encode("utf-8")), ("profile", profile_bytes)):
        p = Path(tmp) / (name + ".dat")
        p.write_bytes(data)
        paths[name] = str(p)
    return paths


# ------------------------------------------------------------------ config loader (startup)

class ConfigLoaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.paths = write_sources(self.tmp)

    def test_valid_config_loads(self):
        cfg = hs.load_preflight_config(write_config(self.tmp, self.paths))
        self.assertTrue(cfg.trust_policy_and_profile and cfg.paired_from_same_tv)
        self.assertEqual(os.path.basename(cfg.product_info_report), "report.dat")

    def test_malformed_config_refused(self):
        bad_json = Path(self.tmp) / "bad.json"
        bad_json.write_text("{ not json", encoding="utf-8")
        dup = Path(self.tmp) / "dup.json"
        dup.write_text('{"schema": "a", "schema": "b"}', encoding="utf-8")
        for path in (str(bad_json), str(dup)):
            with self.assertRaises(hs.PreflightConfigError):
                hs.load_preflight_config(path)
        for kw in (dict(extra={"schema": "wrong/1"}), dict(drop="profile"),
                   dict(extra={"surprise": "x"}),
                   dict(assertions={"trustPolicyAndProfile": False, "pairedFromSameTv": True}),
                   dict(assertions={"trustPolicyAndProfile": True, "pairedFromSameTv": False}),
                   dict(assertions={"trustPolicyAndProfile": True})):
            with self.subTest(kw=kw), self.assertRaises(hs.PreflightConfigError):
                hs.load_preflight_config(write_config(self.tmp, self.paths, **kw))

    def test_ambiguous_config_same_file_for_two_roles_refused(self):
        with self.assertRaises(hs.PreflightConfigError):
            hs.load_preflight_config(write_config(self.tmp, self.paths, same=("profile", "joinPolicy")))

    def test_non_local_url_or_unc_or_relative_refused(self):
        for bad in ("http://example/report.json", "\\\\host\\share\\report.json",
                    "//host/share/report.json", "report.dat", os.path.join("..", "report.dat")):
            path = write_config(self.tmp, {**self.paths, "report": bad})
            with self.subTest(bad=bad), self.assertRaises(hs.PreflightConfigError):
                hs.load_preflight_config(path)

    @unittest.skipUnless(hasattr(__import__("_winapi"), "CreateJunction"), "Windows junctions only")
    def test_parent_junction_source_refused(self):
        import _winapi
        realdir = Path(self.tmp) / "real"
        realdir.mkdir()
        (realdir / "report.dat").write_bytes(Path(self.paths["report"]).read_bytes())
        junc = Path(self.tmp) / "jn"
        _winapi.CreateJunction(str(realdir), str(junc))
        path = write_config(self.tmp, {**self.paths, "report": str(junc / "report.dat")})
        with self.assertRaises(hs.PreflightConfigError):
            hs.load_preflight_config(path)


# ------------------------------------------------------------------ local-file reader gates

class LocalFileReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_oversize_refused_both_ways(self):
        f = Path(self.tmp) / "big.dat"
        f.write_bytes(b"x" * 100)
        with self.assertRaises(hs._PreflightSourceError) as ctx:
            hs._read_local_file(str(f), 50)
        self.assertEqual(ctx.exception.code, "source-too-large")
        self.assertEqual(hs._read_local_file(str(f), 1000), b"x" * 100)  # under the bound reads fine

    def test_missing_or_non_regular_refused(self):
        with self.assertRaises(hs._PreflightSourceError):
            hs._read_local_file(str(Path(self.tmp) / "nope.dat"), 50)
        with self.assertRaises(hs._PreflightSourceError):
            hs._read_local_file(self.tmp, 50)  # a directory is not a regular file

    @unittest.skipUnless(hasattr(__import__("_winapi"), "CreateJunction"), "Windows junctions only")
    def test_parent_junction_refused_both_ways(self):
        import _winapi
        realdir = Path(self.tmp) / "real"
        realdir.mkdir()
        (realdir / "f.dat").write_bytes(b"hello")
        junc = Path(self.tmp) / "jn"
        _winapi.CreateJunction(str(realdir), str(junc))
        via = str(junc / "f.dat")
        with self.assertRaises(hs._PreflightSourceError):
            hs._read_local_file(via, 1000)
        # Both ways: with the reparse gate removed, the junction path reads. So the gate is the refusal.
        with mock.patch.object(hs, "_is_link_or_reparse", return_value=False):
            self.assertEqual(hs._read_local_file(via, 1000), b"hello")

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unsupported")
    def test_symlink_leaf_refused(self):
        target = Path(self.tmp) / "target.dat"
        target.write_bytes(b"hi")
        link = Path(self.tmp) / "link.dat"
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation needs privilege")
        with self.assertRaises(hs._PreflightSourceError):
            hs._read_local_file(str(link), 1000)
        # A symlink leaf is caught twice over: the reparse gate AND the open-time (st_dev, st_ino)
        # swap guard (lstat sees the link, fstat sees the followed target). Removing only the reparse
        # gate still refuses, which is the point -- so the junction case above carries the both-ways.
        with mock.patch.object(hs, "_is_link_or_reparse", return_value=False):
            with self.assertRaises(hs._PreflightSourceError):
                hs._read_local_file(str(link), 1000)


# ------------------------------------------------------------------ real server POST path

class PreflightServerCase(ServerCase):
    """A real loopback Helper whose session is configured with synthetic saved-report files."""

    def source_overrides(self):
        return {}

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.src_paths = write_sources(self.tmpdir, **self.source_overrides())
        cfg = hs.load_preflight_config(write_config(self.tmpdir, self.src_paths))
        self.reader = FakeReader()
        self.engine = CountingEngine()
        self.engine.capabilities = frozenset()
        self.app = srv.HelperApp(hs.Session(reader=self.reader, engine=self.engine, preflight_config=cfg))
        self.app.start()
        self.bodies = []
        self.stderr = io.StringIO()
        import contextlib
        self._redirect = contextlib.redirect_stderr(self.stderr)
        self._redirect.__enter__()

    def _assert_no_leak(self):
        # ServerCase.call records every response body into self.bodies, so this sees the real HTTP
        # bytes, not a re-serialisation. The positive-control test below proves this fires on a leak.
        leak = "\n".join(self.bodies) + self.stderr.getvalue()
        secrets_plain = [DUID_DIGEST, IDENTITY_TOKEN, str(self.tmpdir), *self.src_paths.values()]
        # JSON responses escape every backslash, so a leaked Windows path appears as C:\\... in the
        # body. Check both the plain path and its JSON-escaped form, or the body half is blind.
        for secret in secrets_plain:
            self.assertNotIn(secret, leak)
            self.assertNotIn(json.dumps(secret)[1:-1], leak)  # JSON-escaped form (backslashes doubled)

    def tearDown(self):
        self._assert_no_leak()
        super().tearDown()

    def preflight(self, **kw):
        return self.call("POST", "/api/preflight", kw.pop("body", {}), **kw)


class PositivePreflightTests(PreflightServerCase):
    def test_full_preflight_shows_seven_fields_with_sources(self):
        status, st = self.preflight()
        self.assertEqual(status, 200)
        pf = st["preflight"]
        self.assertTrue(pf["configured"])
        self.assertEqual(pf["status"], "success")
        self.assertIsNone(pf["error"])
        result = pf["result"]
        self.assertEqual(result["profileId"], PROFILE_ID)
        self.assertEqual(result["identityScheme"], fj.FACTS_IDENTITY_SCHEME)
        self.assertEqual(len(result["fields"]), 7)
        values = {f["field"]: f["value"] for f in result["fields"]}
        sources = {f["field"]: f["source"] for f in result["fields"]}
        self.assertEqual(values, EXPECTED_FIELDS)
        self.assertEqual(sources, EXPECTED_SOURCES)
        self.assertTrue(all(f["label"] for f in result["fields"]))
        self.assertTrue(result["operatorAssertions"]["trustPolicyAndProfile"])
        # No device-id token, digest or raw deviceId reaches the display.
        flat = json.dumps(st)
        self.assertNotIn("deviceId", [f["field"] for f in result["fields"]])
        self.assertNotIn(IDENTITY_TOKEN, flat)
        self.assertNotIn(DUID_DIGEST, flat)

    def test_preflight_does_not_mark_target_verified_or_enable_actions(self):
        _, st = self.preflight()
        self.assertEqual(st["preflight"]["status"], "success")
        self.assertIsNone(st["check"])             # no live check was run or implied
        self.assertIsNone(st["target"])
        self.assertTrue(all(a["enabled"] is False for a in st["actions"].values()))
        self.assertTrue(all(not c["available"] for c in st["capabilities"].values()))
        # Change actions still refuse after a positive preflight, with zero device/engine calls.
        for name in hs.ACTIONS:
            status, out = self.call("POST", "/api/action/" + name, {})
            self.assertEqual(status, 409)
            self.assertTrue(out["refused"])
            self.assertFalse(out["changed"])
            self.assertIn("preview", [g["id"] for g in out["gates"]])
        self.assertEqual(self.reader.calls, [])
        self.assertEqual(self.engine.calls, 0)

    def test_csrf_protections_hold_on_preflight(self):
        self.assertEqual(self.preflight(origin=None)[0], 403)          # missing Origin on a mutation
        self.assertEqual(self.preflight(origin="http://evil.example")[0], 403)
        self.assertEqual(self.preflight(token="wrong-token")[0], 403)
        self.assertEqual(self.preflight(host="evil.example:%d" % self.app.port)[0], 421)
        self.assertEqual(self.preflight()[0], 200)                     # correct request still works

    def test_body_must_be_empty_object(self):
        for body in ({"x": 1}, {"componentIds": []}):
            self.assertEqual(self.preflight(body=body)[0], 400)
        for raw in (b"[]", b"null", b'{"a":1}'):
            self.assertEqual(self.call("POST", "/api/preflight", raw=raw)[0], 400)
        self.assertEqual(self.preflight()[0], 200)

    def test_target_change_invalidates_shown_result(self):
        self.assertEqual(self.preflight()[1]["preflight"]["status"], "success")
        self.assertEqual(self.call("POST", "/api/target", {"ip": TARGET})[0], 200)
        self.assertEqual(self.call("GET", "/api/state")[1]["preflight"]["status"], "idle")

    def test_live_check_invalidates_shown_result(self):
        self.assertEqual(self.call("POST", "/api/target", {"ip": TARGET})[0], 200)
        self.assertEqual(self.preflight()[1]["preflight"]["status"], "success")
        self.assertEqual(self.call("POST", "/api/check", {"target": TARGET})[0], 202)
        self.assertTrue(self.app.session.wait_idle())
        self.assertEqual(self.call("GET", "/api/state")[1]["preflight"]["status"], "idle")


class DriftPreflightTests(PreflightServerCase):
    MARKER = "LEAKMARKER-REALMODEL-VALUE"

    def source_overrides(self):
        obj = product_info_dict()
        obj["fields"]["realModel"]["value"] = self.MARKER  # drift: no longer matches the trusted policy
        return {"report_obj": obj}

    def test_drift_is_verify_failed_with_a_fixed_reason_and_no_echo(self):
        status, st = self.preflight()
        self.assertEqual(status, 200)
        self.assertEqual(st["preflight"]["status"], "error")
        self.assertEqual(st["preflight"]["error"]["code"], "verify-failed")
        self.assertEqual(st["preflight"]["error"]["message"], hs.PREFLIGHT_ERRORS["verify-failed"])
        self.assertIsNone(st["preflight"]["result"])
        self.assertNotIn(self.MARKER, json.dumps(st))  # the drifted value never leaks


class FirmwareDriftPreflightTests(PreflightServerCase):
    def source_overrides(self):
        obj = product_info_dict()
        obj["fields"]["firmware"]["value"] = "SYNTHETIC-BUILD-9999.9"  # firmware drift -> policy gate refuses
        return {"report_obj": obj}

    def test_firmware_drift_is_verify_failed(self):
        _, st = self.preflight()
        self.assertEqual(st["preflight"]["error"]["code"], "verify-failed")


class ProfileMismatchPreflightTests(PreflightServerCase):
    def source_overrides(self):
        prof = json.loads(PROFILE_FIXTURE.read_text("utf-8"))
        prof["match"]["region"] = "SYNTHETIC-DIFFERENT"  # same profile id, different match -> ProfileError branch
        return {"profile": json.dumps(prof).encode("utf-8")}

    def test_profile_mismatch_is_verify_failed(self):
        _, st = self.preflight()
        self.assertEqual(st["preflight"]["error"]["code"], "verify-failed")


class FailedNativePreflightTests(PreflightServerCase):
    def source_overrides(self):
        return {"frame": failed_frame()}

    def test_failed_native_run_is_verify_failed(self):
        status, st = self.preflight()
        self.assertEqual(status, 200)
        self.assertEqual(st["preflight"]["status"], "error")
        self.assertEqual(st["preflight"]["error"]["code"], "verify-failed")


class MissingNativeRuntimePreflightTests(PreflightServerCase):
    def source_overrides(self):
        return {"frame": _encode({**native_doc(), "observedRuntime": None})}

    def test_missing_native_runtime_is_verify_failed(self):
        _, st = self.preflight()
        self.assertEqual(st["preflight"]["error"]["code"], "verify-failed")


class TruncatedFramePreflightTests(PreflightServerCase):
    def source_overrides(self):
        full = _encode(native_doc())
        return {"frame": full[: len(full) // 2]}  # truncated frame exercises the catch-all

    def test_truncated_frame_is_verify_failed(self):
        _, st = self.preflight()
        self.assertEqual(st["preflight"]["error"]["code"], "verify-failed")


class TruncatedReportPreflightTests(PreflightServerCase):
    def source_overrides(self):
        full = json.dumps(product_info_dict()).encode("utf-8")
        return {"report_raw": full[: len(full) // 2]}  # report cut mid-JSON

    def test_truncated_report_is_verify_failed(self):
        _, st = self.preflight()
        self.assertEqual(st["preflight"]["error"]["code"], "verify-failed")


class MalformedProfileSecretKeyPreflightTests(PreflightServerCase):
    MARKER = "SEKRIT-PROFILE-KEY"

    def source_overrides(self):
        # The profile is the one source the Helper parses itself (not factsjoin): prove our own
        # strict-JSON boundary refuses a duplicate key and never echoes the key name.
        raw = ('{"' + self.MARKER + '": 1, "' + self.MARKER + '": 2}').encode("utf-8")
        return {"profile": raw}

    def test_malformed_profile_is_verify_failed_without_echo(self):
        _, st = self.preflight()
        self.assertEqual(st["preflight"]["error"]["code"], "verify-failed")
        self.assertNotIn(self.MARKER, json.dumps(st))


class MissingAtRuntimePreflightTests(PreflightServerCase):
    def test_source_deleted_after_startup_is_source_unreadable(self):
        os.unlink(self.src_paths["report"])  # present at startup, gone before the POST
        _, st = self.preflight()
        self.assertEqual(st["preflight"]["error"]["code"], "source-unreadable")


class LeakDetectorPositiveControlTests(PreflightServerCase):
    """Positive controls: prove _assert_no_leak actually fires on a real leak, for each half."""

    def source_overrides(self):
        obj = product_info_dict()
        obj["fields"]["realModel"]["value"] = "DRIFTED"  # force the verify-failed path so the patched
        return {"report_obj": obj}                        # error message (with a leaked path) is returned

    def tearDown(self):
        ServerCase.tearDown(self)  # skip _assert_no_leak; these tests deliberately inject a path

    def test_detector_catches_a_path_leaked_into_the_http_body(self):
        injected = self.src_paths["report"]
        with mock.patch.dict(hs.PREFLIGHT_ERRORS, {"verify-failed": "leaked: " + injected}):
            self.preflight()
        self.assertTrue(self.bodies)  # call() really recorded the response body
        with self.assertRaises(AssertionError):
            self._assert_no_leak()    # the detector sees the leaked path (as its JSON-escaped form)

    def test_detector_catches_a_path_leaked_into_stderr(self):
        print("leaked: " + self.src_paths["report"], file=sys.stderr)  # redirected into self.stderr
        with self.assertRaises(AssertionError):
            self._assert_no_leak()


class OversizeSourcePreflightTests(PreflightServerCase):
    def source_overrides(self):
        # A profile that is valid JSON but padded past the read bound (leading whitespace is ignored by JSON).
        padded = b" " * (300 * 1024) + PROFILE_FIXTURE.read_bytes()
        return {"profile": padded}

    def test_oversize_source_refused_then_accepted_when_bound_raised(self):
        status, st = self.preflight()
        self.assertEqual(status, 200)
        self.assertEqual(st["preflight"]["error"]["code"], "source-too-large")
        # Both ways: with the profile read bound raised, the same file reads and the join succeeds.
        raised = tuple((k, 4 * 1024 * 1024 if k == "profile" else v) for k, v in hs.PREFLIGHT_SOURCES)
        with mock.patch.object(hs, "PREFLIGHT_SOURCES", raised):
            _, st2 = self.preflight()
        self.assertEqual(st2["preflight"]["status"], "success")


# ------------------------------------------------------------------ not-configured and invalidation

class NotConfiguredTests(ServerCase):
    def test_preflight_refused_when_unconfigured(self):
        status, out = self.call("POST", "/api/preflight", {})
        self.assertEqual(status, 409)
        self.assertEqual(out["error"], hs.PREFLIGHT_ERRORS["not-configured"])
        self.assertEqual(self.call("GET", "/api/state")[1]["preflight"], {"configured": False, "status": "idle",
                                                                          "error": None, "result": None})


class InvalidationBothWaysTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = hs.load_preflight_config(write_config(self.tmp, write_sources(self.tmp)))

    def session(self):
        return hs.Session(reader=FakeReader(), preflight_config=self.cfg)

    def test_set_target_clears_preflight_both_ways(self):
        s = self.session()
        self.assertEqual(s.run_preflight()["preflight"]["status"], "success")
        s.set_target(TARGET)
        self.assertEqual(s.state()["preflight"]["status"], "idle")
        # Both ways: with the invalidation removed, the stale success would survive a target change.
        s2 = self.session()
        s2.run_preflight()
        with mock.patch.object(hs.Session, "_invalidate_preflight_locked", lambda self: None):
            s2.set_target(TARGET)
        self.assertEqual(s2.state()["preflight"]["status"], "success")

    def test_start_check_clears_preflight(self):
        s = self.session()
        s.set_target(TARGET)
        self.assertEqual(s.run_preflight()["preflight"]["status"], "success")
        s.start_check(TARGET)
        self.assertTrue(s.wait_idle())
        self.assertEqual(s.state()["preflight"]["status"], "idle")

    def test_stale_result_dropped_when_generation_changed_mid_run(self):
        # A target change during the (out-of-lock) compute must not store a stale result.
        s = self.session()
        s.set_target(TARGET)
        real_compute = s._compute_preflight

        def compute_then_change(config):
            result = real_compute(config)
            s.set_target(OTHER)  # bumps the generation while "computing"
            return result
        with mock.patch.object(s, "_compute_preflight", compute_then_change):
            st = s.run_preflight()
        self.assertEqual(st["preflight"]["status"], "idle")  # not stored, because it was invalidated


class MainStartupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _run_main(self, config_path):
        import contextlib
        out, err, served = io.StringIO(), io.StringIO(), []
        with mock.patch.object(srv, "HelperApp") as app_ctor, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = srv.main(["--no-browser", "--preflight-config", config_path], browser_open=None,
                          serve=served.append)
        return rc, out.getvalue(), err.getvalue(), served, app_ctor

    def test_main_refuses_a_bad_preflight_config_without_binding(self):
        bad = Path(self.tmp) / "bad.json"
        bad.write_text("{ not json", encoding="utf-8")
        unc = write_config(self.tmp, write_sources(self.tmp), extra={"productInfoReport": r"\\host\share\r.json"})
        # Each case: refused, the server never built (so nothing binds), no source path in stdio.
        for label, path, leak in (("malformed", str(bad), str(bad)),
                                   ("unc-source", unc, r"\\host\share\r.json")):
            with self.subTest(case=label):
                rc, out, err, served, app_ctor = self._run_main(path)
                self.assertEqual(rc, 2)
                app_ctor.assert_not_called()  # the server is never built, so nothing binds
                self.assertEqual(served, [])
                self.assertIn("preflight config refused", err)
                self.assertNotIn(leak, out + err)  # the refusal names no path, on either stream

    def test_main_accepts_a_valid_preflight_config(self):
        srcs = write_sources(self.tmp)
        cfg_path = write_config(self.tmp, srcs)
        seen = {}
        import contextlib
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = srv.main(["--no-browser", "--preflight-config", cfg_path], browser_open=None,
                          serve=lambda app: seen.update(configured=app.session.state()["preflight"]["configured"]))
        self.assertEqual(rc, 0)
        self.assertTrue(seen["configured"])
        for src in srcs.values():  # a normal startup prints no source path to stdout either
            self.assertNotIn(src, out.getvalue())


if __name__ == "__main__":
    unittest.main()

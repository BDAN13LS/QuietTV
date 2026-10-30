"""helper/bootstrap.py: the owner-invoked certificate path over the pinned Apps2Samsung CLI.

Drives the REAL command builder, process runner and strict JSON parser against an isolated
executable fixture (tests/fixtures/a2s_cli_fixture.py), never a mocked all-success return. Every
DUID, certificate and report value here is synthetic; no test contacts a real TV or runs the real
binary. Run: python -B -m unittest tests.test_helper_bootstrap
"""
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "helper"))
import bootstrap as bs  # noqa: E402

FIXTURE = REPO / "tests" / "fixtures" / "a2s_cli_fixture.py"
DUID1 = "SYNTHDUIDAAA1"   # raw-DUID shape (^[A-Za-z0-9]{10,64}$), not a QuietTV hash
DUID2 = "SYNTHDUIDBBB2"
HASH_LOOKALIKE = "a" * 64  # 64 lowercase hex: a QuietTV device-id token, refused as a raw DUID


def profile(level, *, exists=True, usable=True, duids=(), path="C:\\owner\\store\\p"):
    p = {"level": level, "exists": exists, "usable": usable, "duids": list(duids)}
    if exists:
        p.update(validFrom="2026-01-01", validUntil="2027-01-01", path=path)
    return p


def inspect_doc(*profiles):
    return {"ok": True, "store": "C:\\owner\\store\\Certificate", "profiles": list(profiles)}


def prepare_doc(level, duids):
    return {"ok": True, "duid": duids[0], "level": level, "profile": profile(level, duids=duids)}


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.dummy_exe = self.tmp / "apps2samsung-cli.exe"
        self.dummy_exe.write_bytes(b"not the real binary; path only")

    # -- helpers --

    def write_spec(self, name="spec.json", **spec):
        p = self.tmp / name
        p.write_text(json.dumps(spec), "utf-8")
        return p

    def launcher(self, spec_path):
        return [sys.executable, "-I", str(FIXTURE), "--spec", str(spec_path)]

    def dep(self, spec_path):
        return bs.DependencyContract(launcher=tuple(self.launcher(spec_path)), version="3.1.0")

    def data_dir(self, name="a2s"):
        return bs.resolve_data_dir(str(self.tmp / name), create=True)

    def author_file(self, data_dir, level, content):
        p = bs._author_path(data_dir, level)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        return hashlib.sha256(content).hexdigest()

    def write_pin(self, data_dir, level, sha):
        (data_dir / bs.PIN_SUBDIR).mkdir(parents=True, exist_ok=True)
        bs._pin_path(data_dir, level).write_text(
            json.dumps({"schema": bs.PIN_SCHEMA, "level": level, "authorKeyContainerSha256": sha}), "ascii")

    # -- verify --

    def test_verify_version_ok(self):
        spec = self.write_spec(version="3.1.0")
        dep = bs.verify_dependency(cli_exe=str(self.dummy_exe), launcher_override=self.launcher(spec))
        self.assertEqual(dep.version, "3.1.0")

    def test_verify_wrong_version_refused(self):
        spec = self.write_spec(version="3.0.9")
        with self.assertRaises(bs.BootstrapError):
            bs.verify_dependency(cli_exe=str(self.dummy_exe), launcher_override=self.launcher(spec))

    def test_verify_nonzero_version_refused(self):
        spec = self.write_spec(version="3.1.0", versionExit=1)
        with self.assertRaises(bs.BootstrapError):
            bs.verify_dependency(cli_exe=str(self.dummy_exe), launcher_override=self.launcher(spec))

    def test_verify_zip_bytes_ok_and_mismatch(self):
        # Positive control: point the pin at a synthetic file and verify the real hashing accepts it.
        content = b"synthetic-zip-bytes-for-test"
        zpath = self.tmp / "cli.zip"
        zpath.write_bytes(content)
        spec = self.write_spec(version="3.1.0")
        orig_size, orig_sha = bs.CLI_ZIP_SIZE, bs.CLI_ZIP_SHA256
        bs.CLI_ZIP_SIZE, bs.CLI_ZIP_SHA256 = len(content), hashlib.sha256(content).hexdigest()
        try:
            dep = bs.verify_dependency(cli_exe=str(self.dummy_exe), cli_zip=str(zpath),
                                       launcher_override=self.launcher(spec))
            self.assertEqual(dep.version, "3.1.0")
            # Negative control: same size, wrong digest.
            bs.CLI_ZIP_SHA256 = "0" * 64
            with self.assertRaises(bs.BootstrapError):
                bs.verify_dependency(cli_exe=str(self.dummy_exe), cli_zip=str(zpath),
                                     launcher_override=self.launcher(spec))
            # Negative control: wrong size.
            bs.CLI_ZIP_SHA256 = hashlib.sha256(content).hexdigest()
            bs.CLI_ZIP_SIZE = len(content) + 1
            with self.assertRaises(bs.BootstrapError):
                bs.verify_dependency(cli_exe=str(self.dummy_exe), cli_zip=str(zpath),
                                     launcher_override=self.launcher(spec))
        finally:
            bs.CLI_ZIP_SIZE, bs.CLI_ZIP_SHA256 = orig_size, orig_sha

    def test_verify_exe_inside_repo_refused(self):
        with self.assertRaises(bs.BootstrapError):
            bs.verify_dependency(cli_exe=str(FIXTURE), launcher_override=self.launcher(self.write_spec()))

    # -- inspect --

    def test_inspect_status_only_no_duid_or_path_leak(self):
        spec = self.write_spec(inspect=inspect_doc(
            profile("Public", exists=False, usable=False),
            profile("Partner", duids=[DUID1, DUID2], path="C:\\secret\\path")))
        result = bs.inspect_profiles(self.dep(spec), self.data_dir())
        pub, part = result["profiles"]
        self.assertEqual(pub["level"], "Public")
        self.assertFalse(pub["exists"])
        self.assertEqual(pub["coveredDuidCount"], 0)
        self.assertEqual(part["coveredDuidCount"], 2)
        self.assertTrue(part["usable"])
        blob = json.dumps(result)
        for leak in (DUID1, DUID2, "secret", "store", "duids", "path"):
            self.assertNotIn(leak, blob)

    def test_inspect_nonzero_refused(self):
        spec = self.write_spec(inspect=inspect_doc(), inspectExit=1)
        with self.assertRaises(bs.BootstrapError):
            bs.inspect_profiles(self.dep(spec), self.data_dir())

    def test_inspect_malformed_json_refused(self):
        spec = self.write_spec(inspectStdout="this is not json")
        with self.assertRaises(bs.BootstrapError):
            bs.inspect_profiles(self.dep(spec), self.data_dir())

    # -- prepare --

    def test_prepare_reuse_ok_pin_matches(self):
        dd = self.data_dir()
        sha = self.author_file(dd, "Public", b"AUTHOR-v1")
        self.write_pin(dd, "Public", sha)
        spec = self.write_spec(
            inspect=inspect_doc(profile("Public", duids=[DUID1])),
            prepare=prepare_doc("Public", [DUID1]),
            writeAuthor={"Public": "AUTHOR-v1"})  # reuse: identical author bytes
        result = bs.prepare_certificate(self.dep(spec), dd, duid=DUID1, level="Public")
        self.assertTrue(result["reused"])
        self.assertFalse(result["authorPinRecorded"])
        self.assertEqual(result["coveredDuidCount"], 1)
        self.assertNotIn(DUID1, json.dumps(result))

    def test_prepare_first_mint_records_pin(self):
        dd = self.data_dir()
        spec = self.write_spec(
            inspect=inspect_doc(profile("Public", exists=False, usable=False)),  # DUID not covered yet
            prepare=prepare_doc("Public", [DUID1]),
            writeAuthor={"Public": "AUTHOR-fresh"})
        result = bs.prepare_certificate(self.dep(spec), dd, duid=DUID1, level="Public", allow_sign_in=True)
        self.assertFalse(result["reused"])
        self.assertTrue(result["authorPinRecorded"])
        pinned = bs._load_pin(dd, "Public")
        self.assertEqual(pinned, hashlib.sha256(b"AUTHOR-fresh").hexdigest())

    def test_prepare_refuses_signin_without_optin_and_does_not_run(self):
        dd = self.data_dir()
        spec = self.write_spec(
            inspect=inspect_doc(profile("Public", exists=False, usable=False)),
            prepare=prepare_doc("Public", [DUID1]), writeAuthor={"Public": "SHOULD-NOT-WRITE"})
        with self.assertRaises(bs.BootstrapError):
            bs.prepare_certificate(self.dep(spec), dd, duid=DUID1, level="Public", allow_sign_in=False)
        self.assertFalse(bs._author_path(dd, "Public").exists())  # prepare never ran

    def test_prepare_pin_mismatch_refused_and_does_not_run(self):
        dd = self.data_dir()
        self.author_file(dd, "Public", b"AUTHOR-v1")
        self.write_pin(dd, "Public", hashlib.sha256(b"A-DIFFERENT-AUTHOR").hexdigest())
        spec = self.write_spec(inspect=inspect_doc(profile("Public", duids=[DUID1])),
                               prepare=prepare_doc("Public", [DUID1]), writeAuthor={"Public": "AUTHOR-v2"})
        with self.assertRaises(bs.BootstrapError):
            bs.prepare_certificate(self.dep(spec), dd, duid=DUID1, level="Public")
        # the author file was not rewritten by a prepare run
        self.assertEqual(bs._author_path(dd, "Public").read_bytes(), b"AUTHOR-v1")

    def test_prepare_unpinned_author_requires_trust(self):
        dd = self.data_dir()
        sha = self.author_file(dd, "Public", b"AUTHOR-v1")
        spec = self.write_spec(inspect=inspect_doc(profile("Public", duids=[DUID1])),
                               prepare=prepare_doc("Public", [DUID1]), writeAuthor={"Public": "AUTHOR-v1"})
        with self.assertRaises(bs.BootstrapError):
            bs.prepare_certificate(self.dep(spec), dd, duid=DUID1, level="Public")
        # with the opt-in it adopts and pins the current identity
        result = bs.prepare_certificate(self.dep(spec), dd, duid=DUID1, level="Public",
                                        trust_current_author=True)
        self.assertTrue(result["authorPinRecorded"])
        self.assertEqual(bs._load_pin(dd, "Public"), sha)

    def test_prepare_pinned_author_missing_refused(self):
        dd = self.data_dir()
        self.write_pin(dd, "Public", hashlib.sha256(b"AUTHOR-v1").hexdigest())  # pin but no author file
        spec = self.write_spec(inspect=inspect_doc(profile("Public", duids=[DUID1])),
                               prepare=prepare_doc("Public", [DUID1]))
        with self.assertRaises(bs.BootstrapError):
            bs.prepare_certificate(self.dep(spec), dd, duid=DUID1, level="Public")

    def test_prepare_duid_hash_lookalike_refused(self):
        dd = self.data_dir()
        spec = self.write_spec(inspect=inspect_doc(), prepare=prepare_doc("Public", [DUID1]))
        with self.assertRaises(bs.BootstrapError) as cm:
            bs.prepare_certificate(self.dep(spec), dd, duid=HASH_LOOKALIKE, level="Public")
        self.assertNotIn(HASH_LOOKALIKE, str(cm.exception))

    def test_prepare_bad_duid_refused(self):
        with self.assertRaises(bs.BootstrapError):
            bs.validate_duid("short")
        with self.assertRaises(bs.BootstrapError):
            bs.validate_duid("has-a-dash-0000")

    def test_prepare_partner_passes_partner_flag(self):
        # The fixture writes author under the Partner profile dir only when --partner is forwarded.
        dd = self.data_dir()
        spec = self.write_spec(
            inspect=inspect_doc(profile("Partner", duids=[DUID1])),
            prepare=prepare_doc("Partner", [DUID1]), writeAuthor={"Partner": "AUTHOR-partner"})
        result = bs.prepare_certificate(self.dep(spec), dd, duid=DUID1, level="Partner", allow_sign_in=True)
        self.assertEqual(result["level"], "Partner")
        self.assertTrue(bs._author_path(dd, "Partner").exists())

    # -- runner containment --

    def test_timeout_kills_child(self):
        spec = self.write_spec(version="3.1.0", sleep=5)
        with self.assertRaises(bs.BootstrapError):
            bs._run_cli(self.launcher(spec), ["--version"], data_dir=None, timeout=1.0)

    def test_stdout_cap_refused(self):
        spec = self.write_spec(inspectStdout="x" * 5000)
        orig = bs.MAX_STDOUT_BYTES
        bs.MAX_STDOUT_BYTES = 100
        try:
            with self.assertRaises(bs.BootstrapError):
                bs._run_cli(self.launcher(spec), ["cert", "--json", "-q"],
                            data_dir=self.data_dir(), timeout=20.0)
        finally:
            bs.MAX_STDOUT_BYTES = orig

    def test_env_containment(self):
        dd = self.data_dir()
        dump = self.tmp / "env.json"
        spec = self.write_spec(inspect=inspect_doc(), dumpEnvTo=str(dump))
        os.environ["GITHUB_TOKEN"] = "SYNTHETIC-TOKEN-SHOULD-NOT-REACH-CHILD"
        os.environ["APPS2SAMSUNG_DATA_DIR"] = "C:\\shared\\should\\be\\overridden"
        self.addCleanup(os.environ.pop, "GITHUB_TOKEN", None)
        try:
            bs.inspect_profiles(self.dep(spec), dd)
        finally:
            os.environ.pop("APPS2SAMSUNG_DATA_DIR", None)
        child_env = json.loads(dump.read_text("utf-8"))
        self.assertNotIn("GITHUB_TOKEN", child_env)
        self.assertEqual(child_env.get("APPS2SAMSUNG_DATA_DIR"), str(dd))

    # -- data-dir containment --

    def test_data_dir_inside_repo_refused(self):
        with self.assertRaises(bs.BootstrapError):
            bs.resolve_data_dir(str(REPO / "helper" / "would-be-data"), create=True)

    def test_data_dir_dotdot_refused(self):
        with self.assertRaises(bs.BootstrapError):
            bs.resolve_data_dir(str(self.tmp / ".." / "x"), create=True)

    @unittest.skipUnless(sys.platform == "win32" and hasattr(__import__("_winapi"), "CreateJunction"),
                         "Windows junctions only")
    def test_data_dir_junction_refused(self):
        import _winapi
        real = self.tmp / "real"
        real.mkdir()
        junc = self.tmp / "junc"
        _winapi.CreateJunction(str(real), str(junc))
        with self.assertRaises(bs.BootstrapError):
            bs.resolve_data_dir(str(junc), create=True)

    # -- acquisition gap --

    def test_acquire_state_is_honest_refusal(self):
        state = bs.acquisition_state()
        self.assertEqual(state["status"], "refused")
        self.assertEqual(state["schema"], bs.ACQUIRE_SCHEMA)
        self.assertEqual(state["upstream"]["commit"], bs.UPSTREAM_COMMIT)
        self.assertIn("cert", state["upstream"]["verbs"])
        self.assertNotIn("pull", state["upstream"]["verbs"])
        self.assertIn("missingOperation", state["gap"])
        self.assertIn("preflight-config", state["fallback"])


if __name__ == "__main__":
    unittest.main()

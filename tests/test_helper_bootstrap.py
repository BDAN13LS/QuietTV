"""helper/bootstrap.py: the owner-invoked certificate path over the pinned Apps2Samsung CLI.

Drives the REAL command builder, process runner, strict JSON parser and PKCS#12 fingerprinter
against isolated executable fixtures (tests/fixtures/a2s_cli_fixture.py and runner_tree_fixture.py),
never a mocked all-success return. Every DUID, certificate and report value is synthetic; no test
contacts a real TV or runs the real binary. The PKCS#12 fixtures are generated offline here with the
pinned `cryptography` package. Run: python -B -m unittest tests.test_helper_bootstrap
"""
import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "helper"))
sys.path.insert(0, str(REPO / "public-toolkit"))
import bootstrap as bs  # noqa: E402

FIXTURE = REPO / "tests" / "fixtures" / "a2s_cli_fixture.py"
TREE_FIXTURE = REPO / "tests" / "fixtures" / "runner_tree_fixture.py"
DUID1 = "SYNTHDUIDAAA1"
DUID2 = "SYNTHDUIDBBB2"
HEX64 = "a" * 64

# --- synthetic PKCS#12 generation (offline, pinned cryptography) ---
_CRYPTO_OK = bs._HAVE_CRYPTO
if _CRYPTO_OK:
    import datetime
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import pkcs12

    _CERTS = {}

    def cert_key(name):
        if name not in _CERTS:
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            who = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Jelly2Sams-" + name)])
            cert = (x509.CertificateBuilder().subject_name(who).issuer_name(who)
                    .public_key(key.public_key()).serial_number(x509.random_serial_number())
                    .not_valid_before(datetime.datetime(2026, 1, 1))
                    .not_valid_after(datetime.datetime(2027, 1, 1)).sign(key, hashes.SHA256()))
            _CERTS[name] = (cert, key)
        return _CERTS[name]

    def write_p12(path, name, password):
        cert, key = cert_key(name)
        data = pkcs12.serialize_key_and_certificates(
            b"usercertificate", key, cert, None, serialization.BestAvailableEncryption(password.encode()))
        Path(path).write_bytes(data)
        return cert.fingerprint(hashes.SHA256()).hex()


def profile(level, *, exists=True, usable=True, duids=(), include_path=True):
    p = {"level": level, "exists": exists, "usable": usable, "duids": list(duids)}
    if exists:
        p.update(validFrom="2026-01-01", validUntil="2027-01-01")
        if include_path:
            p["path"] = "C:\\owner\\store\\" + level
    return p


def inspect_doc(public, partner):
    return {"ok": True, "store": "C:\\owner\\store\\Certificate", "profiles": [public, partner]}


def prepare_doc(level, duids):
    return {"ok": True, "duid": duids[0], "level": level, "profile": profile(level, duids=duids)}


class BaseTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.dummy_exe = self.tmp / "apps2samsung-cli.exe"
        self.dummy_exe.write_bytes(b"not the real binary; path only")
        self._pins = (bs.CLI_EXE_SIZE, bs.CLI_EXE_SHA256, bs.CLI_ZIP_SIZE, bs.CLI_ZIP_SHA256,
                      bs._launcher_for, bs.MAX_STDOUT_BYTES, bs.MAX_STDERR_BYTES)
        self.addCleanup(self._restore)

    def _restore(self):
        (bs.CLI_EXE_SIZE, bs.CLI_EXE_SHA256, bs.CLI_ZIP_SIZE, bs.CLI_ZIP_SHA256,
         bs._launcher_for, bs.MAX_STDOUT_BYTES, bs.MAX_STDERR_BYTES) = self._pins

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


class VerifyTests(BaseTest):
    def pin_dummy(self):
        content = self.dummy_exe.read_bytes()
        bs.CLI_EXE_SIZE, bs.CLI_EXE_SHA256 = len(content), hashlib.sha256(content).hexdigest()

    def test_verify_ok_binds_bytes_then_version(self):
        self.pin_dummy()
        spec = self.write_spec(version="3.1.0")
        bs._launcher_for = lambda exe: self.launcher(spec)
        dep = bs.verify_dependency(cli_exe=str(self.dummy_exe))
        self.assertEqual(dep.version, "3.1.0")

    def test_verify_wrong_exe_bytes_refused_before_run(self):
        bs.CLI_EXE_SIZE = self.dummy_exe.stat().st_size  # size matches, digest will not
        bs.CLI_EXE_SHA256 = "f" * 64
        ran = []
        bs._launcher_for = lambda exe: ran.append(1) or self.launcher(self.write_spec())
        with self.assertRaises(bs.BootstrapError):
            bs.verify_dependency(cli_exe=str(self.dummy_exe))
        self.assertEqual(ran, [])  # never reached --version

    def test_verify_correct_archive_wrong_exe_never_runs(self):
        zip_content = b"synthetic-archive-bytes"
        zpath = self.tmp / "cli.zip"
        zpath.write_bytes(zip_content)
        bs.CLI_ZIP_SIZE, bs.CLI_ZIP_SHA256 = len(zip_content), hashlib.sha256(zip_content).hexdigest()
        # exe pin left at the real pinned value, which the dummy cannot match
        ran = []
        bs._launcher_for = lambda exe: ran.append(1) or self.launcher(self.write_spec())
        with self.assertRaises(bs.BootstrapError):
            bs.verify_dependency(cli_exe=str(self.dummy_exe), cli_zip=str(zpath))
        self.assertEqual(ran, [])

    def test_verify_exe_byte_size_mismatch_refused(self):
        bs.CLI_EXE_SIZE = self.dummy_exe.stat().st_size + 1
        bs.CLI_EXE_SHA256 = hashlib.sha256(self.dummy_exe.read_bytes()).hexdigest()
        with self.assertRaises(bs.BootstrapError):
            bs.verify_dependency(cli_exe=str(self.dummy_exe))

    def test_verify_wrong_version_refused(self):
        self.pin_dummy()
        spec = self.write_spec(version="3.0.9")
        bs._launcher_for = lambda exe: self.launcher(spec)
        with self.assertRaises(bs.BootstrapError):
            bs.verify_dependency(cli_exe=str(self.dummy_exe))

    def test_verify_archive_digest_mismatch_refused(self):
        self.pin_dummy()
        zpath = self.tmp / "cli.zip"
        zpath.write_bytes(b"archive")
        bs.CLI_ZIP_SIZE = len(b"archive")  # size matches, digest does not
        with self.assertRaises(bs.BootstrapError):
            bs.verify_dependency(cli_exe=str(self.dummy_exe), cli_zip=str(zpath))

    def test_verify_exe_inside_repo_refused(self):
        with self.assertRaises(bs.BootstrapError):
            bs.verify_dependency(cli_exe=str(FIXTURE))


class InspectTests(BaseTest):
    def test_inspect_status_only_no_leak(self):
        spec = self.write_spec(inspect=inspect_doc(
            profile("Public", exists=False, usable=False),
            profile("Partner", duids=[DUID1, DUID2], include_path=True)))
        result = bs.inspect_profiles(self.dep(spec), self.data_dir())
        pub, part = result["profiles"]
        self.assertEqual(pub["coveredDuidCount"], 0)
        self.assertFalse(pub["exists"])
        self.assertEqual(part["coveredDuidCount"], 2)
        self.assertTrue(part["usable"])
        blob = json.dumps(result)
        for leak in (DUID1, DUID2, "store", "duids", "path", "owner"):
            self.assertNotIn(leak, blob)

    def test_inspect_rejects_string_boolean(self):
        bad = profile("Public", exists=False, usable=False)
        bad["usable"] = "false"  # a string must not become a usable profile
        spec = self.write_spec(inspect=inspect_doc(bad, profile("Partner", exists=False, usable=False)))
        with self.assertRaises(bs.BootstrapError):
            bs.inspect_profiles(self.dep(spec), self.data_dir())

    def test_inspect_rejects_arbitrary_date_string(self):
        bad = profile("Public", duids=[DUID1])
        bad["validFrom"] = "SYNTHETIC-SEKRIT"  # not an ISO date; never forwarded
        spec = self.write_spec(inspect=inspect_doc(bad, profile("Partner", exists=False, usable=False)))
        with self.assertRaises(bs.BootstrapError):
            bs.inspect_profiles(self.dep(spec), self.data_dir())

    def test_inspect_rejects_duplicate_json_keys(self):
        raw = '{"ok": true, "ok": true, "profiles": []}'
        spec = self.write_spec(inspectStdout=raw)
        with self.assertRaises(bs.BootstrapError):
            bs.inspect_profiles(self.dep(spec), self.data_dir())

    def test_inspect_requires_two_unique_levels(self):
        spec = self.write_spec(inspect=inspect_doc(
            profile("Public", exists=False, usable=False), profile("Public", exists=False, usable=False)))
        with self.assertRaises(bs.BootstrapError):
            bs.inspect_profiles(self.dep(spec), self.data_dir())

    def test_inspect_nonzero_refused(self):
        spec = self.write_spec(inspect=inspect_doc(profile("Public", exists=False, usable=False),
                                                   profile("Partner", exists=False, usable=False)),
                               inspectExit=1)
        with self.assertRaises(bs.BootstrapError):
            bs.inspect_profiles(self.dep(spec), self.data_dir())

    def test_inspect_empty_profiles_are_not_ready(self):
        spec = self.write_spec(inspect=inspect_doc(profile("Public", exists=False, usable=False),
                                                   profile("Partner", exists=False, usable=False)))
        result = bs.inspect_profiles(self.dep(spec), self.data_dir())
        for p in result["profiles"]:
            self.assertFalse(p["exists"])
            self.assertFalse(p["usable"])
            self.assertEqual(p["coveredDuidCount"], 0)


class DuidTests(BaseTest):
    def test_bad_duid_refused(self):
        for v in ("short", "has-a-dash-0000", "ABCDEFGHIJ!@"):
            with self.assertRaises(bs.BootstrapError):
                bs.validate_duid(v)

    def test_bare_hex64_refused_without_assertion(self):
        with self.assertRaises(bs.BootstrapError):
            bs.validate_duid(HEX64)

    def test_hex64_accepted_only_when_asserted(self):
        self.assertEqual(bs.validate_duid(HEX64, assert_raw_hex=True), HEX64)


class RunnerTests(BaseTest):
    def test_timeout_kills_tree(self):
        spec = self.write_spec(version="3.1.0", sleep=5)
        with self.assertRaises(bs.BootstrapError):
            bs._run_cli(self.launcher(spec), ["--version"], data_dir=None, timeout=1.0)

    def test_stdout_overflow_stops_producer_and_cleans_grandchild(self):
        bs.MAX_STDOUT_BYTES = 1000
        heartbeat = self.tmp / "hb.txt"
        marker = self.tmp / "marker.txt"
        launcher = [sys.executable, "-I", str(TREE_FIXTURE)]
        with self.assertRaises(bs.BootstrapError):
            bs._run_cli(launcher, [str(heartbeat), str(marker), "4000000"], data_dir=None, timeout=20.0)
        self.assertFalse(marker.exists())           # producer stopped before its completion marker
        self.assertTrue(heartbeat.exists())
        size1 = heartbeat.stat().st_size
        time.sleep(0.7)
        self.assertEqual(heartbeat.stat().st_size, size1)  # grandchild no longer writing

    def test_stderr_cap_enforced(self):
        bs.MAX_STDERR_BYTES = 100
        # The fixture emits 10000 bytes on stderr via a tiny inline producer.
        producer = ("import sys\n"
                    "sys.stderr.write('e'*10000)\n"
                    "sys.stderr.flush()\n"
                    "import time; time.sleep(3)\n")
        pfile = self.tmp / "err_producer.py"
        pfile.write_text(producer, "utf-8")
        with self.assertRaises(bs.BootstrapError):
            bs._run_cli([sys.executable, "-I", str(pfile)], [], data_dir=None, timeout=20.0)

    def test_env_containment(self):
        dd = self.data_dir()
        dump = self.tmp / "env.json"
        spec = self.write_spec(inspect=inspect_doc(profile("Public", exists=False, usable=False),
                                                   profile("Partner", exists=False, usable=False)),
                               dumpEnvTo=str(dump))
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


class DataDirTests(BaseTest):
    def test_inside_repo_refused(self):
        with self.assertRaises(bs.BootstrapError):
            bs.resolve_data_dir(str(REPO / "helper" / "would-be-data"), create=True)

    def test_dotdot_refused(self):
        with self.assertRaises(bs.BootstrapError):
            bs.resolve_data_dir(str(self.tmp / ".." / "x"), create=True)

    @unittest.skipUnless(sys.platform == "win32" and hasattr(__import__("_winapi"), "CreateJunction"),
                         "Windows junctions only")
    def test_junction_refused(self):
        import _winapi
        real = self.tmp / "real"
        real.mkdir()
        junc = self.tmp / "junc"
        _winapi.CreateJunction(str(real), str(junc))
        with self.assertRaises(bs.BootstrapError):
            bs.resolve_data_dir(str(junc), create=True)


@unittest.skipUnless(_CRYPTO_OK, "cryptography required for certificate fingerprinting")
class FingerprintTests(BaseTest):
    def test_same_cert_different_encryption_same_fingerprint(self):
        a1, a2 = self.tmp / "a1.p12", self.tmp / "a2.p12"
        fp1 = write_p12(a1, "A", "password-one")
        fp2 = write_p12(a2, "A", "a-totally-different-password")
        f1 = bs.author_cert_fingerprint(a1.read_bytes(), "password-one")
        f2 = bs.author_cert_fingerprint(a2.read_bytes(), "a-totally-different-password")
        self.assertEqual(f1, f2)
        self.assertEqual(f1, fp1)
        self.assertEqual(fp1, fp2)

    def test_different_cert_different_fingerprint(self):
        a, b = self.tmp / "a.p12", self.tmp / "b.p12"
        fa = write_p12(a, "A", "pw")
        fb = write_p12(b, "B", "pw")
        self.assertNotEqual(fa, fb)

    def test_malformed_bytes_are_not_a_certificate(self):
        with self.assertRaises(bs.BootstrapError):
            bs.author_cert_fingerprint(b"this is not a pkcs12 container", "pw")


@unittest.skipUnless(_CRYPTO_OK, "cryptography required for certificate fingerprinting")
class PrepareTests(BaseTest):
    def seed_author(self, dd, level, name, password):
        prof = dd / bs.CERT_SUBDIR / bs.PROFILE_DIR_NAME[level]
        prof.mkdir(parents=True)
        fp = write_p12(prof / bs.AUTHOR_FILE, name, password)
        (prof / bs.DISTRIBUTOR_FILE).write_bytes(b"synthetic-distributor")
        (prof / bs.PASSWORD_FILE).write_text(password, "utf-8")
        return fp

    def write_pin(self, dd, level, fp):
        (dd / bs.PIN_SUBDIR).mkdir(parents=True, exist_ok=True)
        bs._pin_path(dd, level).write_text(
            json.dumps({"schema": bs.PIN_SCHEMA, "level": level, "authorCertSha256": fp}), "ascii")

    def candidates_dir(self, dd):
        return dd / "quiettv-bootstrap" / "candidates"

    def p12_src(self, name, password):
        src = self.tmp / f"src-{name}-{password}.p12"
        write_p12(src, name, password)
        return str(src)

    def test_reuse_ok_does_not_call_prepare(self):
        dd = self.data_dir()
        fp = self.seed_author(dd, "Public", "A", "pw")
        self.write_pin(dd, "Public", fp)
        spec = self.write_spec(inspect=inspect_doc(profile("Public", duids=[DUID1]),
                                                   profile("Partner", exists=False, usable=False)),
                               writeAuthorP12={"Public": {"src": self.p12_src("B", "pw"), "password": "pw"}})
        result = bs.prepare_certificate(self.dep(spec), dd, duid=DUID1, level="Public")
        self.assertTrue(result["reused"])
        self.assertFalse(result["authorPinRecorded"])
        self.assertFalse(self.candidates_dir(dd).exists())  # prepare never ran

    def test_first_mint_stages_validates_activates_and_pins(self):
        dd = self.data_dir()
        spec = self.write_spec(
            inspect=inspect_doc(profile("Public", exists=False, usable=False),
                                profile("Partner", exists=False, usable=False)),
            prepare=prepare_doc("Public", [DUID1]),
            writeAuthorP12={"Public": {"src": self.p12_src("A", "pw"), "password": "pw"}})
        result = bs.prepare_certificate(self.dep(spec), dd, duid=DUID1, level="Public", allow_sign_in=True)
        self.assertFalse(result["reused"])
        self.assertTrue(result["authorPinRecorded"])
        activated = dd / bs.CERT_SUBDIR / bs.PROFILE_DIR_NAME["Public"] / bs.AUTHOR_FILE
        self.assertTrue(activated.exists())
        self.assertFalse(self.candidates_dir(dd).exists() and any(self.candidates_dir(dd).iterdir()))

    def test_extend_preserves_author_identity(self):
        dd = self.data_dir()
        fp = self.seed_author(dd, "Public", "A", "pw")
        self.write_pin(dd, "Public", fp)
        before_bytes = (dd / bs.CERT_SUBDIR / bs.PROFILE_DIR_NAME["Public"] / bs.AUTHOR_FILE).read_bytes()
        spec = self.write_spec(
            inspect=inspect_doc(profile("Public", duids=[DUID2]),  # usable but does not cover DUID1
                                profile("Partner", exists=False, usable=False)),
            prepare=prepare_doc("Public", [DUID2, DUID1]),
            writeAuthorP12={"Public": {"src": self.p12_src("A", "re-encrypted-pw"),
                                       "password": "re-encrypted-pw"}})  # same cert A, new encryption
        result = bs.prepare_certificate(self.dep(spec), dd, duid=DUID1, level="Public", allow_sign_in=True)
        self.assertFalse(result["reused"])
        self.assertFalse(result["authorPinRecorded"])  # pin already existed
        self.assertEqual(result["coveredDuidCount"], 2)
        activated = dd / bs.CERT_SUBDIR / bs.PROFILE_DIR_NAME["Public"] / bs.AUTHOR_FILE
        self.assertEqual(bs._author_fingerprint(activated.parent), fp)  # identity preserved
        self.assertNotEqual(activated.read_bytes(), before_bytes)  # file replaced by the activated candidate

    def test_author_change_during_prepare_refused_originals_untouched(self):
        dd = self.data_dir()
        fp = self.seed_author(dd, "Public", "A", "pw")
        self.write_pin(dd, "Public", fp)
        prof = dd / bs.CERT_SUBDIR / bs.PROFILE_DIR_NAME["Public"]
        before_bytes = (prof / bs.AUTHOR_FILE).read_bytes()
        spec = self.write_spec(
            inspect=inspect_doc(profile("Public", duids=[DUID2]),
                                profile("Partner", exists=False, usable=False)),
            prepare=prepare_doc("Public", [DUID2, DUID1]),
            writeAuthorP12={"Public": {"src": self.p12_src("B", "pw"), "password": "pw"}})  # DIFFERENT cert
        with self.assertRaises(bs.BootstrapError):
            bs.prepare_certificate(self.dep(spec), dd, duid=DUID1, level="Public", allow_sign_in=True)
        self.assertEqual((prof / bs.AUTHOR_FILE).read_bytes(), before_bytes)  # original byte-unchanged

    def test_prepared_other_tv_reported_success_refused(self):
        dd = self.data_dir()
        spec = self.write_spec(
            inspect=inspect_doc(profile("Public", exists=False, usable=False),
                                profile("Partner", exists=False, usable=False)),
            prepare=prepare_doc("Public", [DUID2]),  # covers a DIFFERENT TV than requested DUID1
            writeAuthorP12={"Public": {"src": self.p12_src("A", "pw"), "password": "pw"}})
        with self.assertRaises(bs.BootstrapError):
            bs.prepare_certificate(self.dep(spec), dd, duid=DUID1, level="Public", allow_sign_in=True)
        self.assertFalse((dd / bs.CERT_SUBDIR / bs.PROFILE_DIR_NAME["Public"] / bs.AUTHOR_FILE).exists())

    def test_pin_mismatch_refused_does_not_run(self):
        dd = self.data_dir()
        self.seed_author(dd, "Public", "A", "pw")
        self.write_pin(dd, "Public", write_p12(self.tmp / "other.p12", "B", "pw"))  # pin for a different cert
        prof = dd / bs.CERT_SUBDIR / bs.PROFILE_DIR_NAME["Public"]
        before_bytes = (prof / bs.AUTHOR_FILE).read_bytes()
        spec = self.write_spec(inspect=inspect_doc(profile("Public", duids=[DUID1]),
                                                   profile("Partner", exists=False, usable=False)),
                               prepare=prepare_doc("Public", [DUID1]))
        with self.assertRaises(bs.BootstrapError):
            bs.prepare_certificate(self.dep(spec), dd, duid=DUID1, level="Public")
        self.assertEqual((prof / bs.AUTHOR_FILE).read_bytes(), before_bytes)
        self.assertFalse(self.candidates_dir(dd).exists())

    def test_pinned_author_missing_refused(self):
        dd = self.data_dir()
        self.write_pin(dd, "Public", "0" * 64)
        spec = self.write_spec(inspect=inspect_doc(profile("Public", duids=[DUID1]),
                                                   profile("Partner", exists=False, usable=False)),
                               prepare=prepare_doc("Public", [DUID1]))
        with self.assertRaises(bs.BootstrapError):
            bs.prepare_certificate(self.dep(spec), dd, duid=DUID1, level="Public")

    def test_unpinned_author_requires_trust(self):
        dd = self.data_dir()
        fp = self.seed_author(dd, "Public", "A", "pw")
        spec = self.write_spec(inspect=inspect_doc(profile("Public", duids=[DUID1]),
                                                   profile("Partner", exists=False, usable=False)),
                               prepare=prepare_doc("Public", [DUID1]))
        with self.assertRaises(bs.BootstrapError):
            bs.prepare_certificate(self.dep(spec), dd, duid=DUID1, level="Public")
        result = bs.prepare_certificate(self.dep(spec), dd, duid=DUID1, level="Public",
                                        trust_current_author=True)
        self.assertTrue(result["authorPinRecorded"])
        self.assertEqual(bs._load_pin(dd, "Public"), fp)

    def test_refuses_signin_without_optin_and_does_not_run(self):
        dd = self.data_dir()
        spec = self.write_spec(inspect=inspect_doc(profile("Public", exists=False, usable=False),
                                                   profile("Partner", exists=False, usable=False)),
                               prepare=prepare_doc("Public", [DUID1]),
                               writeAuthorP12={"Public": {"src": self.p12_src("A", "pw"), "password": "pw"}})
        with self.assertRaises(bs.BootstrapError):
            bs.prepare_certificate(self.dep(spec), dd, duid=DUID1, level="Public", allow_sign_in=False)
        self.assertFalse((dd / bs.CERT_SUBDIR / bs.PROFILE_DIR_NAME["Public"] / bs.AUTHOR_FILE).exists())
        self.assertFalse(self.candidates_dir(dd).exists())

    def test_malformed_author_in_store_refused(self):
        dd = self.data_dir()
        prof = dd / bs.CERT_SUBDIR / bs.PROFILE_DIR_NAME["Public"]
        prof.mkdir(parents=True)
        (prof / bs.AUTHOR_FILE).write_bytes(b"NOT A CERTIFICATE")
        (prof / bs.PASSWORD_FILE).write_text("pw", "utf-8")
        spec = self.write_spec(inspect=inspect_doc(profile("Public", duids=[DUID1]),
                                                   profile("Partner", exists=False, usable=False)),
                               prepare=prepare_doc("Public", [DUID1]))
        with self.assertRaises(bs.BootstrapError):
            bs.prepare_certificate(self.dep(spec), dd, duid=DUID1, level="Public")


class AcquireTests(BaseTest):
    def test_acquire_state_names_gap_and_sdb_constraint(self):
        state = bs.acquisition_state()
        self.assertEqual(state["status"], "refused")
        self.assertEqual(state["schema"], bs.ACQUIRE_SCHEMA)
        self.assertEqual(state["upstream"]["commit"], bs.UPSTREAM_COMMIT)
        self.assertNotIn("pull", state["upstream"]["verbs"])
        self.assertIn("missingOperation", state["gap"])
        self.assertIn("sdbKillConstraint", state["gap"])
        self.assertIn("preflight-config", state["fallback"])


if __name__ == "__main__":
    unittest.main()

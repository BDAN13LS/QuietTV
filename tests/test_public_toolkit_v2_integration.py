"""capsule/1 versus profile/2: capsule validation and the CLI never accept a v2 profile.

capsule/1 cannot bind firmwareBuild or region, so it is refused outright with
a profile/2, and exact ``match_device`` runs on every facts path. Synthetic
fixtures only. Each guard has an in-test guard-off control.
"""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_public_toolkit_fixtures as fx  # noqa: E402
import test_public_toolkit_profile_v2 as pv2  # noqa: E402
from tvkit import assemble as asm, capsule, cli, transport  # noqa: E402
from tvkit import export as ex  # noqa: E402
from tvkit.capsule import CapsuleError  # noqa: E402
from tvkit.profile import device_id_sha256, parse_profile  # noqa: E402

V1_FIELDS = ("model", "firmware", "arch", "osName", "osVersion")
REFUSAL = "cannot bind firmware build or region"


class V2Integration(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.v2 = parse_profile(pv2.load("profile-v2.json"))
        self.v2_facts = pv2.consumer_facts()
        self.v1 = fx.profile()
        # A capsule/1 manifest aimed at the v2 profile: every field it CAN carry is right.
        members, payloads, xattrs = fx.build_parts()
        manifest = fx.capsule_manifest(payloads, xattrs, members)
        manifest["profileId"] = self.v2.profile_id
        manifest["device"] = {**{k: self.v2_facts[k] for k in V1_FIELDS},
                              "deviceIdSha256": device_id_sha256(pv2.DEVICE_ID)}
        self.v2_capsule = fx.write_capsule(self.tmp / "v2cap", manifest, payloads, xattrs)
        self.v1_capsule = fx.write_capsule(self.tmp / "v1cap")

    def facts_file(self, name, facts):
        path = self.tmp / name
        path.write_text(json.dumps(facts), encoding="utf-8")
        return str(path)

    def run_cli(self, *args, profile=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(cli, "_profile", return_value=profile or self.v2):
            code = cli.main(list(args))
        return code, out.getvalue() + err.getvalue()

    def wrong_variants(self):
        return {
            "wrong-build": {**self.v2_facts, "firmwareBuild": "SYNTHETIC-BUILD-0000.2"},
            "wrong-region": {**self.v2_facts, "region": "SYNTHETIC-OTHER"},
            "no-build": {k: v for k, v in self.v2_facts.items() if k != "firmwareBuild"},
            "no-region": {k: v for k, v in self.v2_facts.items() if k != "region"},
        }

    # ---- capsule.validate ------------------------------------------------

    def test_capsule1_refused_with_profile2_even_for_exact_facts(self):
        for facts in (None, self.v2_facts):
            with self.subTest(facts=facts is not None), self.assertRaises(CapsuleError) as ctx:
                capsule.validate(self.v2_capsule, self.v2, facts)
            self.assertIn(REFUSAL, str(ctx.exception))

    def test_profile2_bad_facts_refused_by_validate(self):
        for label, facts in self.wrong_variants().items():
            with self.subTest(label), self.assertRaises(CapsuleError):
                capsule.validate(self.v2_capsule, self.v2, facts)

    def test_match_device_guard_runs_in_validate_when_v1_gate_is_off(self):
        # With only the v1-only gate weakened, wrong or missing build/region is still refused.
        with mock.patch.object(capsule, "require_capsule1_profile"):
            for label, facts in self.wrong_variants().items():
                with self.subTest(label), self.assertRaises(CapsuleError) as ctx:
                    capsule.validate(self.v2_capsule, self.v2, facts)
                self.assertIn("firmwareBuild" if "build" in label else "region", str(ctx.exception))

    def test_guard_off_controls_validate(self):
        with mock.patch.object(capsule, "require_capsule1_profile"):
            self.assertTrue(capsule.validate(self.v2_capsule, self.v2, self.v2_facts).device_verified)
            with mock.patch.object(capsule, "match_device"):
                for label, facts in self.wrong_variants().items():
                    with self.subTest(label):
                        self.assertTrue(capsule.validate(self.v2_capsule, self.v2, facts).payload_verified)

    def test_v1_capsule_and_profile_still_accepted(self):
        result = capsule.validate(self.v1_capsule, self.v1, dict(fx.FACTS))
        self.assertTrue(result.payload_verified and result.device_verified)
        self.assertTrue(capsule.validate(self.v1_capsule, self.v1).payload_verified)
        # v1 profile with facts that also carry v2 keys stays fine; a wrong v1 field is still refused.
        extra = {**fx.FACTS, "firmwareBuild": "anything", "region": "anything"}
        self.assertTrue(capsule.validate(self.v1_capsule, self.v1, extra).device_verified)
        with self.assertRaises(CapsuleError):
            capsule.validate(self.v1_capsule, self.v1, {**fx.FACTS, "firmware": "9999.9"})

    # ---- assemble --------------------------------------------------------

    def write_spec(self, directory, with_payloads=True):
        """A real assemble spec (and, optionally, its payload/xattr artifacts) for the fixture capsule."""
        directory.mkdir()
        members, payloads, xattrs = fx.build_parts()
        manifest = fx.capsule_manifest(payloads, xattrs, members)
        packages = []
        for pid in fx.PACKAGE_IDS:
            if with_payloads:
                (directory / f"{pid}.tar").write_bytes(payloads[pid])
                (directory / f"{pid}.json").write_bytes(xattrs[pid])
            packages.append({"packageId": pid, "installRoot": f"/opt/usr/apps/{pid}",
                             "payload": f"{pid}.tar", "xattrManifest": f"{pid}.json"})
        spec = {"schema": "tvkit-assemble-spec/1", "packages": packages,
                "registryChanges": manifest["registryChanges"],
                "systemKeys": [{"key": k["key"], "original": k["original"],
                                "expectedCurrent": k["expectedCurrent"]} for k in manifest["systemKeyChanges"]],
                "ourPackages": manifest["ourPackages"]}
        (directory / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
        return directory / "spec.json"

    def test_assemble_refuses_profile2_before_reading_or_creating_anything(self):
        # The spec names payload files that do not exist: a refusal that comes from the
        # profile gate never gets as far as reading them.
        spec = self.write_spec(self.tmp / "cap-src-missing", with_payloads=False)
        out = self.tmp / "asm-out"
        with self.assertRaises(CapsuleError) as ctx:
            asm.assemble(spec, self.v2, self.v2_facts, out)
        self.assertIn(REFUSAL, str(ctx.exception))
        self.assertFalse(out.exists())
        # Same with real artifacts present: still nothing is written.
        spec = self.write_spec(self.tmp / "cap-src-real")
        with self.assertRaises(CapsuleError) as ctx:
            asm.assemble(spec, self.v2, self.v2_facts, out)
        self.assertIn(REFUSAL, str(ctx.exception))
        self.assertFalse(out.exists())
        self.assertEqual(sorted(p.name for p in self.tmp.glob("asm-out*")), [])

    def test_cli_assemble_refuses_profile2_with_real_spec_and_leaves_no_output(self):
        spec = self.write_spec(self.tmp / "cli-src")
        out = self.tmp / "cli-out"
        code, output = self.run_cli("capsule", "assemble", str(spec), "--out", str(out),
                                    "--device-facts", self.facts_file("good.json", self.v2_facts))
        self.assertEqual(code, 1)
        self.assertIn(REFUSAL, output)
        self.assertFalse(out.exists())

    def test_assemble_v1_still_succeeds(self):
        spec = self.write_spec(self.tmp / "v1-src")
        out = self.tmp / "v1-out"
        result = asm.assemble(spec, self.v1, dict(fx.FACTS), out)
        self.assertTrue(result.payload_verified and result.device_verified)
        self.assertTrue((out / "capsule.json").is_file())
        code, output = self.run_cli("capsule", "assemble", str(spec), "--out", str(self.tmp / "v1-cli-out"),
                                    "--device-facts", self.facts_file("v1.json", fx.FACTS), profile=self.v1)
        self.assertEqual(code, 0)
        self.assertIn("capsule valid", output)

    def test_guard_off_control_assemble_old_behaviour_leaves_output(self):
        # With the assemble gate weakened, the old behaviour returns: the folder is created
        # (and the payload read) before the capsule validator finally refuses.
        spec = self.write_spec(self.tmp / "off-src")
        out = self.tmp / "off-out"
        with mock.patch.object(asm, "require_capsule1_profile"), self.assertRaises(CapsuleError) as ctx:
            asm.assemble(spec, self.v2, self.v2_facts, out)
        self.assertIn(REFUSAL, str(ctx.exception))  # the capsule-level guard is the backstop
        self.assertTrue(out.exists())               # but output was left behind
        missing = self.write_spec(self.tmp / "off-src2", with_payloads=False)
        with mock.patch.object(asm, "require_capsule1_profile"), self.assertRaises(FileNotFoundError):
            asm.assemble(missing, self.v2, self.v2_facts, self.tmp / "off-out2")

    # ---- CLI -------------------------------------------------------------

    def test_cli_capsule_validate_refuses_profile2(self):
        good = self.facts_file("good.json", self.v2_facts)
        for extra in ((), ("--device-facts", good)):
            code, output = self.run_cli("capsule", "validate", str(self.v2_capsule), *extra)
            with self.subTest(extra=extra):
                self.assertEqual(code, 1)
                self.assertIn(REFUSAL, output)
                self.assertNotIn("capsule valid", output)
        for label, facts in self.wrong_variants().items():
            code, output = self.run_cli("capsule", "validate", str(self.v2_capsule),
                                        "--device-facts", self.facts_file(f"{label}.json", facts))
            with self.subTest(label):
                self.assertEqual(code, 1)
                self.assertNotIn("capsule valid", output)

    def test_cli_plan_and_apply_refuse_profile2(self):
        good = self.facts_file("good.json", self.v2_facts)
        saved = dict(transport.TRANSPORTS)
        calls = []
        transport.TRANSPORTS["fake"] = lambda: calls.append("factory")
        self.addCleanup(lambda: (transport.TRANSPORTS.clear(), transport.TRANSPORTS.update(saved)))
        invocations = [("plan", "restore", "--capsule", str(self.v2_capsule), "--device-facts", good),
                       ("plan", "setup", "--capsule", str(self.v2_capsule)),
                       ("apply", "restore", "--capsule", str(self.v2_capsule), "--device-facts", good),
                       ("apply", "restore", "--capsule", str(self.v2_capsule),
                        "--apply", "--transport", "fake")]
        for argv in invocations:
            code, output = self.run_cli(*argv)
            with self.subTest(argv=argv[:2] + argv[-2:]):
                self.assertEqual(code, 1)
                self.assertIn(REFUSAL, output)
                self.assertNotIn("DRY RUN", output)
                self.assertNotIn("applied", output.lower())
        self.assertEqual(calls, [])  # the transport was never even constructed

    def test_cli_assemble_refuses_profile2_before_creating_output(self):
        out = self.tmp / "out"
        code, output = self.run_cli("capsule", "assemble", str(self.tmp / "spec.json"),
                                    "--device-facts", self.facts_file("good.json", self.v2_facts),
                                    "--out", str(out))
        self.assertEqual(code, 1)
        self.assertIn(REFUSAL, output)
        self.assertFalse(out.exists())

    def test_cli_match_device_runs_on_transport_facts(self):
        # v1 profile through apply --apply: facts read from the transport must match exactly.
        saved = dict(transport.TRANSPORTS)
        self.addCleanup(lambda: (transport.TRANSPORTS.clear(), transport.TRANSPORTS.update(saved)))
        ran = []

        class Wrong(transport.Transport):
            name, implemented, capabilities = "wrong", True, frozenset()

            def read_device_facts(self):
                return {**fx.FACTS, "arch": "x86_64"}

            def run(self, step):
                ran.append(step)
                return transport.StepResult(True, True)

        transport.TRANSPORTS["wrong"] = Wrong
        code, output = self.run_cli("apply", "restore", "--capsule", str(self.v1_capsule),
                                    "--apply", "--transport", "wrong", profile=self.v1)
        self.assertEqual(code, 1)
        self.assertIn("arch", output)
        self.assertEqual(ran, [])

    def test_cli_v1_unchanged(self):
        good = self.facts_file("v1.json", fx.FACTS)
        code, output = self.run_cli("capsule", "validate", str(self.v1_capsule),
                                    "--device-facts", good, profile=self.v1)
        self.assertEqual(code, 0)
        self.assertIn("device identity VERIFIED", output)
        code, output = self.run_cli("plan", "restore", "--capsule", str(self.v1_capsule), profile=self.v1)
        self.assertEqual(code, 0)
        self.assertIn("RESTORE PLAN", output)
        code, output = self.run_cli("apply", "restore", "--capsule", str(self.v1_capsule), profile=self.v1)
        self.assertEqual(code, 0)
        self.assertIn("DRY RUN", output)
        code, _ = self.run_cli("capsule", "validate", str(self.v1_capsule), "--device-facts",
                               self.facts_file("bad.json", {**fx.FACTS, "arch": "x86_64"}), profile=self.v1)
        self.assertEqual(code, 1)

    def test_guard_off_controls_cli(self):
        # Each guard, weakened in turn, changes the protected outcome.
        good = self.facts_file("good.json", self.v2_facts)
        bad = self.facts_file("bad.json", self.wrong_variants()["wrong-build"])
        with mock.patch.object(cli, "require_capsule1_profile"), \
                mock.patch.object(capsule, "require_capsule1_profile"):
            code, output = self.run_cli("capsule", "validate", str(self.v2_capsule), "--device-facts", good)
            self.assertEqual(code, 0)
            self.assertIn("capsule valid", output)
            code, _ = self.run_cli("capsule", "validate", str(self.v2_capsule), "--device-facts", bad)
            self.assertEqual(code, 1)  # match_device still refuses
            with mock.patch.object(cli, "match_device"), mock.patch.object(capsule, "match_device"):
                code, _ = self.run_cli("capsule", "validate", str(self.v2_capsule), "--device-facts", bad)
                self.assertEqual(code, 0)


class ExportAllowlist(unittest.TestCase):
    def test_only_reviewed_consumer_and_bootstrap_fixtures_are_allowlisted(self):
        data = json.loads((fx.TOOLKIT / "export-allowlist.json").read_text(encoding="utf-8"))
        fixture_entries = [e for e in data["include"] if e.startswith("tests/fixtures")]
        self.assertEqual(sorted(fixture_entries), [
            "tests/fixtures/a2s_cli_fixture.py",
            "tests/fixtures/consumer/components.json",
            "tests/fixtures/consumer/inventory.json",
            "tests/fixtures/consumer/profile-v2.json",
            "tests/fixtures/recovery2/boot-policy.json",
            "tests/fixtures/recovery2/external-policy.json",
            "tests/fixtures/runner_tree_fixture.py"])
        for entry in fixture_entries:
            self.assertTrue((fx.ROOT / entry).is_file(), entry)
            text = (fx.ROOT / entry).read_text(encoding="utf-8")
            self.assertNotIn('"deviceId"', text)
            if entry.endswith(".json"):
                self.assertTrue("org.example" in text or "SYNTHETIC" in text, entry)
            else:
                # The two explicitly named executable fixtures create synthetic child processes;
                # no fixture-directory wildcard or arbitrary executable is admitted.
                self.assertIn("synthetic", text.lower(), entry)

    RECOVERY2_FILES = ("public-toolkit/tvkit/recovery2.py", "public-toolkit/docs/RECOVERY2.md",
                       "tests/test_public_toolkit_recovery2.py", "tests/fixtures/recovery2/boot-policy.json",
                       "tests/fixtures/recovery2/external-policy.json")

    def run_recovery2_tests(self, root):
        # -I: no user site, no environment, no current-folder imports; discover adds only the tests folder.
        tests = root / "tests"
        return subprocess.run([sys.executable, "-I", "-m", "unittest", "discover", "-s", str(tests),
                               "-t", str(tests), "-p", "test_public_toolkit_recovery2.py"],
                              cwd=str(tests), capture_output=True, text=True, timeout=300)

    def test_export_ships_recovery2_source_and_fixtures_and_its_tests_pass(self):
        if (fx.ROOT / ex.MANIFEST_NAME).exists():  # already an exported tree: check it in place
            for rel in self.RECOVERY2_FILES:
                self.assertTrue((fx.ROOT / rel).is_file(), rel)
            return
        report = ex.build_report(fx.ROOT, ex.load_allowlist(fx.TOOLKIT / "export-allowlist.json"))
        self.assertEqual([(v.path, v.rule) for v in report.violations], [])
        paths = {p for p, _, _ in report.files}
        for rel in self.RECOVERY2_FILES:
            self.assertIn(rel, paths)
        with tempfile.TemporaryDirectory() as tmp:  # fresh folder outside the checkout, no deny/owner inputs
            out = Path(tmp) / "export"
            ex.write_export(fx.ROOT, report, out)
            for rel in self.RECOVERY2_FILES:
                self.assertEqual((out / rel).read_bytes(), (fx.ROOT / rel).read_bytes(), rel)
            result = self.run_recovery2_tests(out)
            self.assertEqual(result.returncode, 0, result.stderr[-2000:])
            self.assertRegex(result.stderr, r"Ran [1-9]\d* tests")
            # Negative control: the same exported tree without one fixture fails.
            (out / "tests/fixtures/recovery2/boot-policy.json").unlink()
            self.assertNotEqual(self.run_recovery2_tests(out).returncode, 0)


if __name__ == "__main__":
    unittest.main()

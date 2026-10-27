from __future__ import annotations

import contextlib
import io
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_public_toolkit_fixtures as fx  # noqa: E402
from tvkit import cli, transport  # noqa: E402
from tvkit.capsule import CapsuleError, validate  # noqa: E402
from tvkit.plan import FORBIDDEN_ACTIONS, check_key_line, registry_sql, restore_plan, setup_plan  # noqa: E402

PACKAGE_STEPS = ["stage-files", "extract-bytes", "restore-ownership", "restore-modes",
                 "restore-xattrs", "recheck-modes", "verify-tree"]
EXPECTED_RESTORE = (
    ["verify-capsule", "match-device"] + ["check-target"] * 3
    + (PACKAGE_STEPS + ["move-into-place", "verify-installed"]) * 3
    + ["registry-restore-changed-fields",
       "system-key-compare-and-set", "system-key-compare-and-set",
       "verify-control", "remove-our-package", "verify-control-final"]
)
EXPECTED_SETUP = (
    ["match-device", "verify-backup-capsule"]
    + (PACKAGE_STEPS + ["remove-rehearsal-copy"]) * 3
    + ["registry-rehearsal-on-copy", "system-key-compare-only", "system-key-compare-only",
       "rehearsal-passed", "install-our-package", "registry-apply-changed-fields",
       "system-key-compare-and-set", "system-key-compare-and-set", "verify-control"]
)


class PlanCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.profile = fx.profile()
        self.capsule_dir = fx.write_capsule(self.tmp / "cap")
        self.capsule = validate(self.capsule_dir, self.profile, fx.FACTS)

    def tearDown(self):
        self._tmp.cleanup()


class RestoreOrder(PlanCase):
    def test_exact_restore_order(self):
        plan = restore_plan(self.capsule, self.profile)
        self.assertEqual([s.action for s in plan.steps], EXPECTED_RESTORE)
        self.assertEqual([s.number for s in plan.steps], list(range(1, len(EXPECTED_RESTORE) + 1)))

    def test_ours_removed_last_after_control(self):
        actions = [s.action for s in restore_plan(self.capsule, self.profile).steps]
        self.assertLess(actions.index("verify-control"), actions.index("remove-our-package"))
        self.assertLess(max(i for i, a in enumerate(actions) if a.startswith("system-key")),
                        actions.index("verify-control"))
        self.assertLess(max(i for i, a in enumerate(actions) if a == "verify-installed"),
                        actions.index("registry-restore-changed-fields"))

    def test_exact_setup_order_rehearsal_before_install(self):
        plan = setup_plan(self.capsule, self.profile, {"packageId": fx.OUR_PACKAGE, "sha256": "a" * 64})
        self.assertEqual([s.action for s in plan.steps], EXPECTED_SETUP)

    def test_install_needs_pin_and_valid_id(self):
        for install in ({"packageId": fx.OUR_PACKAGE, "sha256": None},
                        {"packageId": fx.OUR_PACKAGE, "sha256": "abc"},
                        {"packageId": "org.tizen.setting", "sha256": "a" * 64},
                        {"packageId": "com.samsung.tv.SecurityCenter", "sha256": "a" * 64},
                        {"packageId": "../evil", "sha256": "a" * 64}):
            with self.subTest(install=install), self.assertRaises(CapsuleError):
                setup_plan(self.capsule, self.profile, install)

    def test_no_forbidden_actions(self):
        for plan in (restore_plan(self.capsule, self.profile), setup_plan(self.capsule, self.profile)):
            self.assertFalse({s.action for s in plan.steps} & FORBIDDEN_ACTIONS)

    def test_refuses_unverified_capsule(self):
        self.capsule.payload_verified = False
        with self.assertRaises(CapsuleError):
            restore_plan(self.capsule, self.profile)
        with self.assertRaises(CapsuleError):
            setup_plan(self.capsule, self.profile)


SCHEMA = "CREATE TABLE package_app_info (app_id TEXT PRIMARY KEY, app_disable, app_onboot, app_autorestart, app_label)"


class RegistrySql(PlanCase):
    def db(self, alpha=("true", "false", "false"), bravo=(1, 0, 0)):
        conn = sqlite3.connect(":memory:", isolation_level=None)
        conn.execute(SCHEMA)
        conn.execute("INSERT INTO package_app_info VALUES ('org.example.alpha', ?, ?, ?, 'A')", alpha)
        conn.execute("INSERT INTO package_app_info VALUES ('org.example.bravo', ?, ?, ?, 'B')", bravo)
        conn.execute("INSERT INTO package_app_info VALUES ('org.example.unrelated', 'true', 'true', 'true', 'U')")
        return conn

    @staticmethod
    def rows(conn):
        return conn.execute("SELECT * FROM package_app_info ORDER BY app_id").fetchall()

    def run_sql(self, conn, sql):
        try:
            conn.executescript(sql)
            return True
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK")
            return False

    def test_restore_changes_only_recorded_fields(self):
        conn = self.db()
        self.assertTrue(self.run_sql(conn, registry_sql(self.profile, self.capsule.registry_changes, "restore")))
        self.assertEqual(self.rows(conn), [
            ("org.example.alpha", "false", "true", "false", "A"),
            ("org.example.bravo", 0, 0, 0, "B"),
            ("org.example.unrelated", "true", "true", "true", "U"),
        ])

    def test_restore_refused_when_state_differs_and_nothing_changes(self):
        conn = self.db(bravo=(0, 0, 0))  # bravo not in the applied state
        before = self.rows(conn)
        self.assertFalse(self.run_sql(conn, registry_sql(self.profile, self.capsule.registry_changes, "restore")))
        self.assertEqual(self.rows(conn), before)

    def test_round_trip(self):
        conn = self.db(alpha=("false", "true", "false"), bravo=(0, 0, 0))
        before = self.rows(conn)
        self.assertTrue(self.run_sql(conn, registry_sql(self.profile, self.capsule.registry_changes, "apply")))
        self.assertNotEqual(self.rows(conn), before)
        self.assertTrue(self.run_sql(conn, registry_sql(self.profile, self.capsule.registry_changes, "restore")))
        self.assertEqual(self.rows(conn), before)


class StartupKeys(PlanCase):
    def setUp(self):
        super().setUp()
        self.change = self.capsule.system_key_changes[0]

    def test_exact_line_accepted(self):
        line = f"[system] {self.change.key} = String: {self.change.expected_current}\n"
        self.assertEqual(check_key_line(line, self.change.key, self.change.expected_current_sha256),
                         self.change.expected_current)

    def test_mismatches_refused(self):
        good = f"[system] {self.change.key} = String: {self.change.expected_current}"
        for line in (
            f"[system] {self.change.key} = String: {self.change.original}",
            good + " ",
            good.replace("[system]", "[user]"),
            good.replace("String:", "Int:"),
            good.replace(self.change.key, "db/appfw/home_appid"),
            "",
        ):
            with self.subTest(line=line), self.assertRaises(CapsuleError) as ctx:
                check_key_line(line, self.change.key, self.change.expected_current_sha256)
            self.assertNotIn(self.change.original, str(ctx.exception))

    def test_restore_step_binds_both_hashes(self):
        plan = restore_plan(self.capsule, self.profile)
        step = next(s for s in plan.steps if s.action == "system-key-compare-and-set")
        self.assertEqual(step.detail["requireCurrentSha256"], self.change.expected_current_sha256)
        self.assertEqual(step.detail["requireAfterSha256"], self.change.original_sha256)
        self.assertEqual(step.detail["write"], self.change.original)


class FakeTransport(transport.Transport):
    name = "fake"
    implemented = True

    def __init__(self, facts=None, fail_at=None, capabilities=None, log=None):
        self.facts = facts or dict(fx.FACTS)
        self.fail_at = fail_at
        self.log = log if log is not None else []
        self.capabilities = frozenset(capabilities if capabilities is not None else {
            "read-device-facts", "inspect-path", "stage-file", "extract-tar", "chown-entries",
            "chmod-entries", "xattr-restore", "verify-tree", "move-into-place", "run-guarded-sql",
            "system-key", "manual-acceptance", "remove-package", "run-guarded-sql-on-copy",
            "system-key-read", "install-package", "remove-scratch"})

    def read_device_facts(self):
        self.log.append("facts")
        return self.facts

    def run(self, step):
        self.log.append(step.action)
        if step.action == self.fail_at:
            return transport.StepResult(False, False, "simulated failure")
        return transport.StepResult(True, True)


class ApplyCli(PlanCase):
    def setUp(self):
        super().setUp()
        self.saved = dict(transport.TRANSPORTS)
        self.calls = []

    def tearDown(self):
        transport.TRANSPORTS.clear()
        transport.TRANSPORTS.update(self.saved)
        super().tearDown()

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(args))
        return code, out.getvalue() + err.getvalue()

    def register(self, **kwargs):
        def factory():
            self.calls.append("factory")
            return FakeTransport(log=self.calls, **kwargs)
        transport.TRANSPORTS["fake"] = factory

    def test_shipped_registry_is_empty(self):
        self.assertEqual(self.saved, {})

    def test_dry_run_default(self):
        code, output = self.run_cli("apply", "restore", "--capsule", str(self.capsule_dir))
        self.assertEqual(code, 0)
        self.assertIn("DRY RUN", output)
        self.assertNotIn("applied", output.lower())

    def test_apply_without_adapter_fails_closed(self):
        for extra in ((), ("--transport", "missing")):
            code, output = self.run_cli("apply", "restore", "--capsule", str(self.capsule_dir), "--apply", *extra)
            with self.subTest(extra=extra):
                self.assertEqual(code, 3)
                self.assertNotIn("applied", output.lower())
                self.assertNotIn("success", output.lower())
                self.assertIn("Nothing was changed", output)

    def test_unverified_capsule_refused_before_transport(self):
        self.register()
        (self.capsule_dir / "payload" / "org.example.alpha.tar").write_bytes(b"tampered")
        code, output = self.run_cli("apply", "restore", "--capsule", str(self.capsule_dir),
                                    "--apply", "--transport", "fake")
        self.assertEqual(code, 1)
        self.assertEqual(self.calls, [])
        self.assertNotIn("applied", output.lower())

    def test_wrong_device_refused_before_any_step(self):
        self.register(facts={**fx.FACTS, "deviceId": "SOMEONE-ELSES-TV"})
        code, output = self.run_cli("apply", "restore", "--capsule", str(self.capsule_dir),
                                    "--apply", "--transport", "fake")
        self.assertEqual(code, 1)
        self.assertEqual(self.calls, ["factory", "facts"])

    def test_missing_capability_runs_nothing(self):
        self.register(capabilities={"read-device-facts"})
        code, output = self.run_cli("apply", "restore", "--capsule", str(self.capsule_dir),
                                    "--apply", "--transport", "fake")
        self.assertEqual(code, 3)
        self.assertEqual(self.calls, ["factory", "facts"])
        self.assertNotIn("applied", output.lower())

    def test_full_fake_run_in_plan_order(self):
        self.register()
        code, output = self.run_cli("apply", "restore", "--capsule", str(self.capsule_dir),
                                    "--apply", "--transport", "fake")
        self.assertEqual(code, 0, output)
        self.assertEqual(self.calls[:2], ["factory", "facts"])
        self.assertEqual(self.calls[2:], [a for a in EXPECTED_RESTORE if a != "verify-capsule"])
        self.assertIn("applied and verified", output)

    def test_failed_step_stops_and_is_not_called_applied(self):
        self.register(fail_at="restore-xattrs")
        code, output = self.run_cli("apply", "restore", "--capsule", str(self.capsule_dir),
                                    "--apply", "--transport", "fake")
        self.assertEqual(code, 1)
        self.assertEqual(self.calls[-1], "restore-xattrs")
        self.assertNotIn("move-into-place", self.calls)
        self.assertIn("NOT complete", output)
        self.assertNotIn("applied", output.lower())

    def test_plan_command_and_validate_command(self):
        facts = fx.write_facts(self.tmp / "facts.json")
        code, output = self.run_cli("capsule", "validate", str(self.capsule_dir), "--device-facts", str(facts))
        self.assertEqual(code, 0)
        self.assertIn("device identity VERIFIED", output)
        code, output = self.run_cli("plan", "restore", "--capsule", str(self.capsule_dir), "--json")
        self.assertEqual(code, 0)
        self.assertIn('"remove-our-package"', output)
        bad = fx.write_facts(self.tmp / "bad.json", firmware="1300.9")
        code, output = self.run_cli("capsule", "validate", str(self.capsule_dir), "--device-facts", str(bad))
        self.assertEqual(code, 1)


class EntryPoint(unittest.TestCase):
    def test_isolated_mode_help_and_doctor(self):
        entry = str(fx.TOOLKIT / "tvkit_cli.py")
        with tempfile.TemporaryDirectory() as cwd:
            result = subprocess.run([sys.executable, "-I", entry, "--help"], capture_output=True, text=True,
                                    cwd=cwd, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("doctor", result.stdout)
            result = subprocess.run([sys.executable, "-I", entry, "doctor"], capture_output=True, text=True,
                                    cwd=cwd, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("NOT CONFIGURED", result.stdout)
            self.assertIn("transport adapters implemented: 0", result.stdout)


if __name__ == "__main__":
    unittest.main()

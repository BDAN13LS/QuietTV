"""Rescue refusals and the synthetic restore path.

One synthetic package, a fake in-memory adapter, no secrets and no device.
A passing restore here proves the gate and the ordering only; it says
nothing about real hardware.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(REPO))

import test_public_toolkit_fixtures as fx  # noqa: E402
from rescue import main as rescue  # noqa: E402
from tvkit.capsule import CapsuleError  # noqa: E402
from tvkit.profile import ProfileError, device_id_sha256, parse_profile  # noqa: E402
from tvkit.transport import StepFailed, StepResult, Transport, TransportUnavailable  # noqa: E402

PID = "org.example.alpha"
ROOT = f"/opt/usr/apps/{PID}"
V2_PROFILE = TESTS / "fixtures" / "consumer" / "profile-v2.json"
RESTORE_CAPS = frozenset({
    "read-device-facts", "inspect-path", "stage-file", "extract-tar", "chown-entries",
    "chmod-entries", "xattr-restore", "verify-tree", "move-into-place", "remove-scratch",
})


def one_package_parts():
    members = fx.package_members(PID)
    return members, fx.make_tar(members), fx.xattr_manifest(PID, members)


def one_package_manifest(device: dict, payload: bytes, xattrs: bytes, members) -> dict:
    return {
        "schema": "tvkit-capsule/1",
        "profileId": device.pop("profileId"),
        "provenance": "owner-device-backup",
        "device": device,
        "packages": [{
            "packageId": PID,
            "installRoot": ROOT,
            "payload": {"file": f"payload/{PID}.tar", "sha256": fx.sha(payload), "size": len(payload)},
            "xattrManifest": {"file": f"xattrs/{PID}.json", "sha256": fx.sha(xattrs), "size": len(xattrs)},
            "entries": fx.capsule_entries(members),
        }],
        "registryChanges": [],
        "systemKeyChanges": [],
        "ourPackages": [],
    }


def write_one(directory: Path, manifest: dict, payload: bytes, xattrs: bytes) -> Path:
    (directory / "payload").mkdir(parents=True)
    (directory / "xattrs").mkdir()
    (directory / "payload" / f"{PID}.tar").write_bytes(payload)
    (directory / "xattrs" / f"{PID}.json").write_bytes(xattrs)
    (directory / "capsule.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return directory


def tree_hash(directory: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        digest.update(path.relative_to(directory).as_posix().encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


class FakeTV(rescue.RescueTransport):
    """Synthetic in-memory TV. Its "success" is a test double, not a device result."""

    name = "synthetic-fake"
    implemented = True

    def __init__(self, facts, package_entries, xattr_sha, *, caps=RESTORE_CAPS, fail_at=None,
                 evidence=None, registry=None, facts_sequence=None):
        self.facts = dict(facts)
        self.facts_sequence = list(facts_sequence or [])
        self.capabilities = frozenset(caps)
        self.entries, self.xattr_sha = package_entries, xattr_sha
        self.installed = False
        self.fail_at = fail_at
        self.evidence = evidence
        self.registry = dict(registry or {})
        self.runs: list[str] = []
        self.reads: list[str] = []

    def read_device_facts(self):
        self.reads.append("facts")
        return dict(self.facts_sequence.pop(0) if self.facts_sequence else self.facts)

    def inspect_target(self, package):
        self.reads.append(f"inspect {package.package_id}")
        if self.evidence is not None:
            return self.evidence
        if self.installed:
            return rescue.TargetEvidence(package.package_id, package.install_root, "present",
                                         self.entries, self.xattr_sha)
        return rescue.TargetEvidence(package.package_id, package.install_root, "absent")

    def read_registry(self, app_id, fields):
        self.reads.append(f"registry {app_id}")
        return rescue.RegistryEvidence(app_id, {f: self.registry[(app_id, f)] for f in fields})

    def run(self, step):
        self.runs.append(step.action)
        if step.requires == rescue.MANUAL:
            return StepResult(True, True, "a fake must never be able to pass owner checks")
        if step.action == self.fail_at:
            return StepResult(True, False, "synthetic verification failure")
        if step.action == "move-into-place":
            self.installed = True
        return StepResult(True, True)


class RescueCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.profile = fx.profile()
        self.facts = dict(fx.FACTS)
        self.members, self.payload, self.xattrs = one_package_parts()
        self.manifest = one_package_manifest(self.device_block(self.profile, self.facts),
                                             self.payload, self.xattrs, self.members)

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def device_block(profile, facts):
        block = {k: facts[k] for k in ("model", "firmware", "arch", "osName", "osVersion")}
        return {**block, "deviceIdSha256": device_id_sha256(facts["deviceId"]),
                "profileId": profile.profile_id}

    def capsule(self, manifest=None, payload=None, name=None) -> Path:
        self._count = getattr(self, "_count", 0) + 1
        name = name or f"cap{self._count}"
        return write_one(self.tmp / name, manifest or self.manifest, payload or self.payload, self.xattrs)

    def entries(self):
        if not hasattr(self, "_entries"):
            from tvkit.capsule import validate
            self._entries = validate(self.capsule(), self.profile).packages[0].entries
        return self._entries

    def tv(self, **kw) -> FakeTV:
        return FakeTV(kw.pop("facts", self.facts), self.entries(), fx.sha(self.xattrs), **kw)

    def assertRefusedNoWrites(self, path, tv, exc_type, fragment, profile=None):
        before = tree_hash(path)
        with self.assertRaises(exc_type) as ctx:
            rescue.restore(path, profile or self.profile, tv)
        self.assertIn(fragment, str(ctx.exception))
        self.assertEqual(tv.runs, [], "a refused run must not call run() at all")
        self.assertEqual(tree_hash(path), before, "the capsule must not change")
        return ctx.exception


class SyntheticRestore(RescueCase):
    def test_restore_stops_before_owner_checks_and_never_claims_restored(self):
        tv = self.tv(caps=RESTORE_CAPS | {rescue.MANUAL})
        result = rescue.restore(self.capsule(), self.profile, tv)
        self.assertEqual(result.status, "pending-owner-acceptance")
        self.assertEqual(result.completed, result.total)
        self.assertEqual(result.pending, ("verify-control",))
        self.assertNotIn("verify-control", tv.runs)
        self.assertIn("move-into-place", tv.runs)
        self.assertTrue(tv.installed)

    def test_restore_does_not_need_manual_acceptance_capability(self):
        result = rescue.restore(self.capsule(), self.profile, self.tv())
        self.assertEqual(result.status, "pending-owner-acceptance")

    def test_second_run_on_identical_target_only_reads(self):
        path = self.capsule()
        tv = self.tv()
        rescue.restore(path, self.profile, tv)
        first = list(tv.runs)
        result = rescue.restore(path, self.profile, tv)
        self.assertEqual(result.status, "nothing-to-do")
        self.assertEqual(result.skipped_packages, (PID,))
        self.assertEqual(result.pending, ("verify-control",), "a rerun must not drop the owner checks")
        self.assertEqual(tv.runs, first, "the second run made no run() call")

    def test_noop_adapter_reporting_success_is_caught_by_read_back(self):
        class NoOpTV(FakeTV):
            def run(self, step):
                self.runs.append(step.action)
                return StepResult(True, True)

        tv = NoOpTV(self.facts, self.entries(), fx.sha(self.xattrs))
        with self.assertRaises(rescue.ReadbackFailed) as ctx:
            rescue.restore(self.capsule(), self.profile, tv)
        self.assertIn("does not match", str(ctx.exception))
        self.assertIn("move-into-place", tv.runs)

    def test_noop_registry_write_is_caught_by_read_back(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["registryChanges"] = [{"appId": PID, "fields": {"app_disable": {"before": "false", "applied": "true"}}}]
        tv = self.tv(caps=RESTORE_CAPS | {"registry-read", "run-guarded-sql"},
                     registry={(PID, "app_disable"): "true"})
        with self.assertRaises(rescue.ReadbackFailed):
            rescue.restore(self.capsule(manifest), self.profile, tv)

    def test_untyped_registry_evidence_refused(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["registryChanges"] = [{"appId": PID, "fields": {"app_disable": {"before": "false", "applied": "true"}}}]
        path = self.capsule(manifest)
        for fields in (None, ["true"], {"app_disable": 1}):
            with self.subTest(fields=fields):
                tv = self.tv(caps=RESTORE_CAPS | {"registry-read", "run-guarded-sql"})
                tv.read_registry = lambda app_id, names, f=fields: rescue.RegistryEvidence(app_id, f)
                self.assertRefusedNoWrites(path, tv, rescue.RescueRefused, "typed registry evidence")

    def test_registry_three_way(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["registryChanges"] = [{"appId": PID, "fields": {"app_disable": {"before": "false", "applied": "true"}}}]
        path = self.capsule(manifest)
        caps = RESTORE_CAPS | {"registry-read", "run-guarded-sql"}
        tv = self.tv(caps=caps, registry={(PID, "app_disable"): "true"})
        tv.run = self._registry_run(tv)
        self.assertEqual(rescue.restore(path, self.profile, tv).status, "pending-owner-acceptance")
        self.assertIn("registry-restore-changed-fields", tv.runs)
        self.assertEqual(rescue.restore(path, self.profile, tv).status, "nothing-to-do")
        tv2 = self.tv(caps=caps, registry={(PID, "app_disable"): "something-else"})
        self.assertRefusedNoWrites(path, tv2, rescue.RescueRefused, "registry fields changed")

    @staticmethod
    def _registry_run(tv):
        base = FakeTV.run.__get__(tv)

        def run(step):
            result = base(step)
            if step.action == "registry-restore-changed-fields":
                tv.registry[(PID, "app_disable")] = "false"
            return result
        return run

    def test_stop_on_first_failed_verification(self):
        tv = self.tv(fail_at="restore-ownership")
        with self.assertRaises(rescue.RunStopped) as ctx:
            rescue.restore(self.capsule(), self.profile, tv)
        self.assertEqual(tv.runs[-1], "restore-ownership")
        self.assertNotIn("restore-modes", tv.runs)
        self.assertNotIn("move-into-place", tv.runs)
        self.assertFalse(tv.installed)
        self.assertEqual(ctx.exception.completed, tv.runs.index("restore-ownership") + 1)
        self.assertIsInstance(ctx.exception.__cause__, StepFailed)

    def test_rehearsal_touches_scratch_only(self):
        tv = self.tv()
        result = rescue.rehearse(self.capsule(), self.profile, tv)
        self.assertEqual(result.status, "rehearsed")
        self.assertIn("remove-rehearsal-copy", tv.runs)
        for action in ("move-into-place", "verify-control", "install-our-package"):
            self.assertNotIn(action, tv.runs)
        self.assertFalse(tv.installed)


class RaisingTV(FakeTV):
    """Mutates its state, then raises, at a chosen step or at read-back."""

    def __init__(self, *args, raise_at=None, raise_on_readback=False, exc=OSError, **kw):
        super().__init__(*args, **kw)
        self.raise_at, self.raise_on_readback, self.exc = raise_at, raise_on_readback, exc
        self.mutated: list[str] = []

    def run(self, step):
        if step.action == self.raise_at:
            self.runs.append(step.action)
            self.mutated.append(step.action)  # the write happened, then the adapter blew up
            if step.action == "move-into-place":
                self.installed = True
            raise self.exc(f"synthetic adapter failure at {step.action}")
        return super().run(step)

    def inspect_target(self, package):
        if self.raise_on_readback and self.runs:
            raise self.exc("synthetic adapter failure during read-back")
        return super().inspect_target(package)


class AdapterRaisesDuringRun(RescueCase):
    """Once a device step is handed over, any adapter exception is "stopped, TV may be changed"."""

    EXCEPTIONS = (TransportUnavailable, OSError)

    def raising(self, **kw) -> RaisingTV:
        return RaisingTV(self.facts, self.entries(), fx.sha(self.xattrs), **kw)

    def run_cli(self, tv, command="restore"):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(rescue, "get_transport", return_value=tv),                 redirect_stdout(out), redirect_stderr(err):
            code = rescue.main([command, "--capsule", str(self.capsule()), "--profile", fx.PROFILE_ID,
                                "--apply", "--transport", "synthetic-fake"])
        return code, err.getvalue()

    def assertStoppedMayBeChanged(self, tv, command="restore"):
        code, err = self.run_cli(tv, command)
        self.assertEqual(code, rescue.EXIT_STOPPED, err)
        self.assertNotIn("Nothing was changed", err)
        self.assertNotIn("refused:", err)
        self.assertIn("may be partly changed", err)
        return err

    def test_first_device_step_raises(self):
        for exc in self.EXCEPTIONS:
            with self.subTest(exc=exc.__name__):
                tv = self.raising(raise_at="match-device", exc=exc)
                with self.assertRaises(rescue.RunStopped) as ctx:
                    rescue.restore(self.capsule(), self.profile, tv)
                self.assertEqual((ctx.exception.attempted, ctx.exception.completed), (1, 1))
                self.assertIsInstance(ctx.exception.__cause__, exc)
                err = self.assertStoppedMayBeChanged(self.raising(raise_at="match-device", exc=exc))
                self.assertIn("1 device steps were handed", err)

    def test_later_step_raises_after_writing(self):
        for exc in self.EXCEPTIONS:
            with self.subTest(exc=exc.__name__):
                tv = self.raising(raise_at="move-into-place", exc=exc)
                with self.assertRaises(rescue.RunStopped) as ctx:
                    rescue.restore(self.capsule(), self.profile, tv)
                self.assertTrue(tv.installed, "the fake really changed state before raising")
                self.assertEqual(ctx.exception.attempted, len(tv.runs))
                self.assertEqual(ctx.exception.completed, ctx.exception.attempted)  # local step + verified ones
                self.assertNotIn("verify-installed", tv.runs)
                self.assertStoppedMayBeChanged(self.raising(raise_at="move-into-place", exc=exc))

    def test_read_back_raises(self):
        for exc in self.EXCEPTIONS:
            with self.subTest(exc=exc.__name__):
                tv = self.raising(raise_on_readback=True, exc=exc)
                with self.assertRaises(rescue.RunStopped) as ctx:
                    rescue.restore(self.capsule(), self.profile, tv)
                self.assertEqual(ctx.exception.completed, ctx.exception.total)
                self.assertIn("read-back", str(ctx.exception))
                self.assertStoppedMayBeChanged(self.raising(raise_on_readback=True, exc=exc))

    def test_rehearsal_step_raises(self):
        for exc in self.EXCEPTIONS:
            with self.subTest(exc=exc.__name__):
                tv = self.raising(raise_at="extract-bytes", exc=exc)
                with self.assertRaises(rescue.RunStopped):
                    rescue.rehearse(self.capsule(), self.profile, tv)
                self.assertStoppedMayBeChanged(self.raising(raise_at="extract-bytes", exc=exc), "rehearse")
                tv = self.raising(raise_at="stage-files", exc=exc)  # first device step of the rehearsal
                self.assertStoppedMayBeChanged(tv, "rehearse")

    def test_errors_before_execution_still_refuse_without_change(self):
        class InspectFails(RaisingTV):
            def inspect_target(self, package):
                raise self.exc("synthetic failure while inspecting, before any write")

        for exc, expected in ((OSError, rescue.EXIT_REFUSED), (TransportUnavailable, rescue.EXIT_NO_ADAPTER)):
            with self.subTest(exc=exc.__name__):
                tv = InspectFails(self.facts, self.entries(), fx.sha(self.xattrs), exc=exc)
                with self.assertRaises(exc):
                    rescue.restore(self.capsule(), self.profile, tv)
                self.assertEqual(tv.runs, [])
                code, err = self.run_cli(InspectFails(self.facts, self.entries(), fx.sha(self.xattrs), exc=exc))
                self.assertEqual(code, expected)
                self.assertIn("refused:", err)


class DeviceRefusals(RescueCase):
    def test_wrong_model_firmware_or_device(self):
        path = self.capsule()
        for field, value in (("model", "UN50OTHER"), ("firmware", "1300.9"),
                             ("deviceId", "SYNTHETIC-OTHER-DEVICE")):
            with self.subTest(field=field):
                tv = self.tv(facts={**self.facts, field: value})
                fragment = "identifier" if field == "deviceId" else field
                self.assertRefusedNoWrites(path, tv, (ProfileError, CapsuleError), fragment)

    def test_facts_change_between_checks(self):
        changed = {**self.facts, "deviceId": "SYNTHETIC-SWAPPED-DEVICE"}
        tv = self.tv(facts_sequence=[self.facts, changed])
        self.assertRefusedNoWrites(self.capsule(), tv, rescue.RescueRefused, "facts changed")


class V2Refusals(RescueCase):
    def setUp(self):
        super().setUp()
        self.v2 = parse_profile(json.loads(V2_PROFILE.read_text(encoding="utf-8")))
        self.v2_facts = {**self.v2.match, "deviceId": "SYNTHETIC-CONSUMER-DEVICE-0001"}
        self.v2_manifest = one_package_manifest(self.device_block(self.v2, self.v2_facts),
                                                self.payload, self.xattrs, self.members)

    # capsule.validate refuses capsule/1 with any tvkit-profile/2 profile (require_capsule1_profile),
    # so Rescue's first gate step stops these before the adapter is asked anything.
    V2_REFUSAL = "cannot bind firmware build or region"

    def assertRefusedBeforeDevice(self, run, tv):
        path = self.capsule(self.v2_manifest)
        before = tree_hash(path)
        with self.assertRaises(CapsuleError) as ctx:
            run(path, self.v2, tv)
        self.assertIn(self.V2_REFUSAL, str(ctx.exception))
        self.assertEqual(tv.reads, [], "no facts, inspection or registry read before the refusal")
        self.assertEqual(tv.runs, [], "a refused run must not call run() at all")
        self.assertEqual(tree_hash(path), before, "the capsule must not change")

    def test_wrong_build_or_region_refused_before_device_is_asked(self):
        for field in ("firmwareBuild", "region"):
            with self.subTest(field=field):
                tv = self.tv(facts={**self.v2_facts, field: "SYNTHETIC-OTHER"})
                self.assertRefusedBeforeDevice(rescue.restore, tv)

    def test_correct_v2_facts_still_refused_until_v2_capsules(self):
        for run in (rescue.restore, rescue.rehearse):
            with self.subTest(run=run.__name__):
                self.assertRefusedBeforeDevice(run, self.tv(facts=self.v2_facts))

    def test_cli_refuses_v2_profile_offline(self):
        path = self.capsule(self.v2_manifest)
        for command in ("validate", "plan", "restore", "rehearse"):
            with self.subTest(command=command):
                err = io.StringIO()
                with redirect_stdout(io.StringIO()), redirect_stderr(err):
                    code = rescue.main([command, "--capsule", str(path), "--profile-file", str(V2_PROFILE)])
                self.assertEqual(code, rescue.EXIT_REFUSED)
                self.assertIn(self.V2_REFUSAL, err.getvalue())


class CapsuleRefusals(RescueCase):
    def test_partial_backup_refused_before_transport_is_touched(self):
        path = self.capsule(payload=self.payload[:-512])
        tv = self.tv()
        self.assertRefusedNoWrites(path, tv, CapsuleError, "")
        self.assertEqual(tv.reads, [])

    def test_missing_payload_file(self):
        path = self.capsule()
        (path / "payload" / f"{PID}.tar").unlink()
        self.assertRefusedNoWrites(path, self.tv(), CapsuleError, "missing or unexpected")

    def test_our_packages_refused(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["ourPackages"] = [{"packageId": fx.OUR_PACKAGE}]
        self.assertRefusedNoWrites(self.capsule(manifest), self.tv(), rescue.RescueRefused, "ourPackages")

    def test_boot_key_changes_refused(self):
        from tvkit.capsule import system_key_record
        manifest = copy.deepcopy(self.manifest)
        manifest["systemKeyChanges"] = [system_key_record("db/appfw/home_appid", "org.example.home", "x.y")]
        self.assertRefusedNoWrites(self.capsule(manifest), self.tv(), rescue.RescueRefused, "boot changes")


class TargetRefusals(RescueCase):
    def test_updated_package_present_refused_with_version(self):
        entries = list(self.entries())
        entries[2] = type(entries[2])(**{**entries[2].__dict__, "sha256": "0" * 64})
        evidence = rescue.TargetEvidence(PID, ROOT, "present", tuple(entries), fx.sha(self.xattrs), "2.0.1")
        error = self.assertRefusedNoWrites(self.capsule(), self.tv(evidence=evidence),
                                           rescue.RescueRefused, "present and differs")
        self.assertIn("2.0.1", str(error))

    def test_same_tree_different_xattrs_refused(self):
        evidence = rescue.TargetEvidence(PID, ROOT, "present", self.entries(), "f" * 64)
        self.assertRefusedNoWrites(self.capsule(), self.tv(evidence=evidence),
                                   rescue.RescueRefused, "present and differs")

    def test_untyped_or_mismatched_evidence_refused(self):
        bad = (
            {"state": "absent"},
            mock.Mock(spec=rescue.TargetEvidence),
            rescue.TargetEvidence("org.example.bravo", ROOT, "absent"),
            rescue.TargetEvidence(PID, ROOT, "unknown"),
            rescue.TargetEvidence(PID, ROOT, "absent", self.entries()),
        )
        for evidence in bad:
            with self.subTest(evidence=evidence):
                self.assertRefusedNoWrites(self.capsule(),
                                           self.tv(evidence=evidence), rescue.RescueRefused, "")


class TransportRefusals(RescueCase):
    def test_missing_capability_refused_before_any_inspection(self):
        for capability in ("xattr-restore", "inspect-path", "move-into-place"):
            with self.subTest(capability=capability):
                tv = self.tv(caps=RESTORE_CAPS - {capability})
                self.assertRefusedNoWrites(self.capsule(), tv, TransportUnavailable, capability)
                self.assertFalse(any(r.startswith("inspect") for r in tv.reads))

    def test_unimplemented_mock_and_plain_transports_refused(self):
        path = self.capsule()
        unimplemented = self.tv()
        unimplemented.implemented = False
        mocked = mock.Mock(spec=rescue.RescueTransport)
        mocked.capabilities = RESTORE_CAPS

        class Plain(Transport):
            implemented = True
            capabilities = RESTORE_CAPS

        for tv in (unimplemented, mocked, Plain()):
            with self.subTest(tv=type(tv).__name__):
                with self.assertRaises(TransportUnavailable):
                    rescue.restore(path, self.profile, tv)
        self.assertEqual(unimplemented.runs, [])
        mocked.run.assert_not_called()
        mocked.read_device_facts.assert_not_called()


class Cli(RescueCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = rescue.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_banner_and_dry_run(self):
        code, out, err = self.run_cli("restore", "--capsule", str(self.capsule()), "--profile", fx.PROFILE_ID)
        self.assertEqual(code, rescue.EXIT_OK)
        self.assertIn("No hard-brick guarantee", err)
        self.assertIn("CANNOT recover a real TV by itself", err)
        self.assertIn("DRY RUN", out)
        self.assertIn("never marks it passed", out)

    def test_apply_refuses_with_empty_registry(self):
        self.assertEqual(rescue.TRANSPORTS, {})
        for name in (None, "synthetic-fake"):
            argv = ["restore", "--capsule", str(self.capsule()), "--profile", fx.PROFILE_ID, "--apply"]
            if name:
                argv += ["--transport", name]
            code, _, err = self.run_cli(*argv)
            self.assertEqual(code, rescue.EXIT_NO_ADAPTER)
            self.assertIn("Nothing was changed", err)

    def test_pending_owner_exit_code_on_apply(self):
        path = self.capsule()
        tv = self.tv()
        with mock.patch.object(rescue, "get_transport", return_value=tv):
            first = self.run_cli("restore", "--capsule", str(path), "--profile", fx.PROFILE_ID,
                                 "--apply", "--transport", "synthetic-fake")
            second = self.run_cli("restore", "--capsule", str(path), "--profile", fx.PROFILE_ID,
                                  "--apply", "--transport", "synthetic-fake")
        for code, out, _ in (first, second):
            self.assertEqual(code, rescue.EXIT_PENDING_OWNER)
            self.assertIn("pending owner check: verify-control", out)
            self.assertNotIn("restored", out.replace("pending", ""))

    def test_validate_with_wrong_facts_refused(self):
        facts = fx.write_facts(self.tmp / "facts.json", firmware="9999.9")
        code, _, err = self.run_cli("validate", "--capsule", str(self.capsule()), "--profile", fx.PROFILE_ID,
                                    "--device-facts", str(facts))
        self.assertEqual(code, rescue.EXIT_REFUSED)
        self.assertIn("firmware", err)

    def test_plan_refuses_our_packages(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["ourPackages"] = [{"packageId": fx.OUR_PACKAGE}]
        code, _, _ = self.run_cli("plan", "--capsule", str(self.capsule(manifest)), "--profile", fx.PROFILE_ID)
        self.assertEqual(code, rescue.EXIT_REFUSED)


if __name__ == "__main__":
    unittest.main()

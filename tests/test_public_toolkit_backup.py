"""D2 offline readback gate. Synthetic capsules only; no device, network or real data."""

from __future__ import annotations

import ast
import copy
import dataclasses
import hashlib
import inspect
import json
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_public_toolkit_fixtures as fx  # noqa: E402
from tvkit import backup, transport  # noqa: E402
from tvkit.assemble import bundle  # noqa: E402
from tvkit.backup import GateResult, ReadbackRefused, readback_gate  # noqa: E402
from tvkit.capsule import CapsuleError, system_key_record, validate  # noqa: E402

BACKUP_SOURCE = fx.TOOLKIT / "tvkit" / "backup.py"


def device_hashes(payloads: dict[str, bytes], xattrs: dict[str, bytes]) -> dict[str, str]:
    """What the TV would report: SHA-256 of each file it produced, computed here independently."""
    result = {}
    for pid in fx.PACKAGE_IDS:
        result[f"payload/{pid}.tar"] = fx.sha(payloads[pid])
        result[f"xattrs/{pid}.json"] = fx.sha(xattrs[pid])
    return result


def tree_state(root: Path) -> dict[str, tuple[str, int]]:
    state = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            full = Path(dirpath) / name
            rel = full.relative_to(root).as_posix()
            if full.is_file():
                state[rel] = (hashlib.sha256(full.read_bytes()).hexdigest(), full.stat().st_mtime_ns)
            else:
                state[rel] = ("dir", 0)
    return state


class GateCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.profile = fx.profile()
        self.members, self.payloads, self.xattrs = fx.build_parts()
        self.manifest = fx.capsule_manifest(self.payloads, self.xattrs, self.members)
        self.hashes = device_hashes(self.payloads, self.xattrs)
        self.cap = fx.write_capsule(self.tmp / "cap", self.manifest)

    def tearDown(self):
        self._tmp.cleanup()

    def assertRefused(self, fragment, path=None, hashes=None, facts=None, **kwargs):
        with self.assertRaises(ReadbackRefused) as ctx:
            readback_gate(path if path is not None else self.cap,
                          self.hashes if hashes is None else hashes,
                          fx.FACTS if facts is None else facts, **kwargs)
        self.assertIn(fragment, str(ctx.exception))
        return ctx.exception

    def altered_capsule(self, name: str) -> tuple[Path, dict, dict]:
        """A different but fully self-consistent capsule: one payload byte changed, hashes recomputed."""
        members = copy.deepcopy(self.members)
        target = next(m for m in members[fx.PACKAGE_IDS[0]] if m["path"] == "bin/app")
        target["data"] = target["data"].replace(b"echo", b"ECHO")
        payloads = {pid: fx.make_tar(members[pid]) for pid in fx.PACKAGE_IDS}
        xattrs = {pid: fx.xattr_manifest(pid, members[pid]) for pid in fx.PACKAGE_IDS}
        manifest = fx.capsule_manifest(payloads, xattrs, members)
        return fx.write_capsule(self.tmp / name, manifest, payloads, xattrs), payloads, xattrs


class Passing(GateCase):
    def test_directory_capsule_passes_with_exact_evidence(self):
        result = readback_gate(self.cap, self.hashes, fx.FACTS)
        self.assertIsInstance(result, GateResult)
        self.assertEqual(result.profile_id, fx.PROFILE_ID)
        self.assertEqual(result.storage_kind, "directory")
        self.assertIsNone(result.bundle_sha256)
        self.assertEqual({f.name: f.sha256 for f in result.files}, self.hashes)
        self.assertEqual({f.name: f.size for f in result.files},
                         {**{f"payload/{p}.tar": len(self.payloads[p]) for p in fx.PACKAGE_IDS},
                          **{f"xattrs/{p}.json": len(self.xattrs[p]) for p in fx.PACKAGE_IDS}})
        manifest_bytes = (self.cap / "capsule.json").read_bytes()
        self.assertEqual(result.manifest_sha256, fx.sha(manifest_bytes))
        self.assertEqual(dict(result.counts)["entries"], 18)
        self.assertEqual(result.baseline, "current-state")
        self.assertEqual(result.baseline_basis, "declared")
        self.assertRegex(result.identity_sha256, r"^[0-9a-f]{64}$")
        self.assertEqual(dict(result.device)["deviceIdSha256"], fx.device_id_sha256(fx.DEVICE_ID))

    def test_evidence_never_holds_the_raw_device_id(self):
        result = readback_gate(self.cap, self.hashes, fx.FACTS)
        self.assertNotIn(fx.DEVICE_ID, json.dumps(result.to_json()))
        self.assertNotIn(fx.DEVICE_ID, repr(result))

    def test_bundle_gives_same_identity_as_directory(self):
        folder = readback_gate(self.cap, self.hashes, fx.FACTS)
        digest = bundle(self.cap, self.tmp / "cap.tar", self.profile)
        packed = readback_gate(self.tmp / "cap.tar", self.hashes, fx.FACTS)
        self.assertEqual(packed.storage_kind, "bundle")
        self.assertEqual(packed.bundle_sha256, digest)
        self.assertEqual(packed.identity_sha256, folder.identity_sha256)

    def test_identity_is_independently_reproducible(self):
        result = readback_gate(self.cap, self.hashes, fx.FACTS)
        document = {
            "domain": "tvkit-backup-identity/1",
            "capsuleSchema": "tvkit-capsule/1",
            "profileId": fx.PROFILE_ID,
            "device": {**{k: fx.FACTS[k] for k in ("model", "firmware", "arch", "osName", "osVersion")},
                       "deviceIdSha256": fx.device_id_sha256(fx.DEVICE_ID)},
            "manifestSha256": fx.sha((self.cap / "capsule.json").read_bytes()),
            "files": [[name, self.hashes[name], size] for name, size in
                      sorted((f.name, f.size) for f in result.files)],
            "baseline": "current-state",
            "baselineBasis": "declared",
        }
        expected = hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":"))
                                  .encode("ascii")).hexdigest()
        self.assertEqual(result.identity_sha256, expected)

    def test_identity_changes_with_label_and_with_bytes(self):
        current = readback_gate(self.cap, self.hashes, fx.FACTS)
        stock = readback_gate(self.cap, self.hashes, fx.FACTS, baseline="stock")
        self.assertNotEqual(current.identity_sha256, stock.identity_sha256)
        other, payloads, xattrs = self.altered_capsule("other")
        moved = readback_gate(other, device_hashes(payloads, xattrs), fx.FACTS)
        self.assertNotEqual(moved.identity_sha256, current.identity_sha256)

    def test_zero_writes_on_pass_and_on_refusal(self):
        before = tree_state(self.tmp)
        readback_gate(self.cap, self.hashes, fx.FACTS)
        with self.assertRaises(ReadbackRefused):
            readback_gate(self.cap, {**self.hashes, "payload/org.example.alpha.tar": "0" * 64}, fx.FACTS)
        self.assertEqual(tree_state(self.tmp), before)

    def test_legacy_validate_unchanged(self):
        self.assertFalse(validate(self.cap, self.profile).device_verified)
        self.assertTrue(validate(self.cap, self.profile, fx.FACTS).device_verified)


class DeviceHashSet(GateCase):
    def test_missing_entry(self):
        hashes = dict(self.hashes)
        del hashes["xattrs/org.example.bravo.json"]
        self.assertRefused("no device hash for: xattrs/org.example.bravo.json", hashes=hashes)

    def test_extra_entry(self):
        self.assertRefused("does not hold: payload/org.example.delta.tar",
                           hashes={**self.hashes, "payload/org.example.delta.tar": "a" * 64})

    def test_manifest_is_not_a_device_file(self):
        self.assertRefused("does not hold: capsule.json",
                           hashes={**self.hashes, "capsule.json": fx.sha((self.cap / "capsule.json").read_bytes())})

    def test_changed_hash_named_by_file(self):
        name = "payload/org.example.charlie.tar"
        flipped = ("0" if self.hashes[name][0] != "0" else "1") + self.hashes[name][1:]
        error = self.assertRefused(f"{name}: stored bytes differ from what the TV hashed",
                                   hashes={**self.hashes, name: flipped})
        self.assertNotIn(flipped, str(error))
        self.assertNotIn(self.hashes[name], str(error))

    def test_key_is_exact_not_casefolded(self):
        hashes = dict(self.hashes)
        hashes["Payload/org.example.alpha.tar"] = hashes.pop("payload/org.example.alpha.tar")
        self.assertRefused("no device hash for: payload/org.example.alpha.tar", hashes=hashes)

    def test_malformed_hash_values_and_shapes(self):
        name = "payload/org.example.alpha.tar"
        for bad in (self.hashes[name].upper(), self.hashes[name][:63], True, None, 1, ""):
            with self.subTest(value=bad):
                self.assertRefused("is not 64 lowercase hex", hashes={**self.hashes, name: bad})
        self.assertRefused("are empty", hashes={})
        self.assertRefused("must be a mapping", hashes=list(self.hashes.items()))
        self.assertRefused("not a capsule file name", hashes={**self.hashes, 7: "a" * 64})

    def test_self_consistent_capsule_with_different_bytes_is_refused(self):
        """validate() accepts this capsule; only the TV-hash comparison stops it."""
        other, _, _ = self.altered_capsule("swapped")
        self.assertTrue(validate(other, self.profile, fx.FACTS).payload_verified)
        self.assertRefused("stored bytes differ from what the TV hashed", path=other)

    def test_negative_control_comparison_removed_accepts_the_swap(self):
        """Guard off: with the comparison disabled the swapped capsule passes. Restored: refused."""
        other, _, _ = self.altered_capsule("swapped")
        with mock.patch.object(backup, "compare_device_hashes", lambda *args: None):
            self.assertIsInstance(readback_gate(other, self.hashes, fx.FACTS), GateResult)
        self.assertRefused("stored bytes differ", path=other)


class WrongOrDamagedStorage(GateCase):
    def test_truncated_payload_file(self):
        target = self.cap / "payload" / "org.example.alpha.tar"
        target.write_bytes(target.read_bytes()[:700])
        self.assertRefused("stored capsule refused")

    def test_truncated_bundle(self):
        bundle(self.cap, self.tmp / "cap.tar", self.profile)
        data = (self.tmp / "cap.tar").read_bytes()
        with tarfile.open(self.tmp / "cap.tar") as archive:
            content_end = max(m.offset_data + m.size for m in archive.getmembers())
        for cut in (len(data) // 2, content_end - 1, 600):
            with self.subTest(cut=cut):
                path = self.tmp / f"cut-{cut}.tar"
                path.write_bytes(data[:cut])
                self.assertRefused("", path=path)

    def test_missing_and_extra_stored_files(self):
        (self.cap / "xattrs" / "org.example.bravo.json").unlink()
        self.assertRefused("stored capsule refused")
        fresh = fx.write_capsule(self.tmp / "extra", self.manifest)
        (fresh / "notes.txt").write_text("x", encoding="utf-8")
        self.assertRefused("missing or unexpected files", path=fresh)

    def test_wrong_file_given(self):
        for path in (self.cap / "payload" / "org.example.alpha.tar", self.cap / "capsule.json",
                     self.tmp / "does-not-exist", self.cap / "payload"):
            with self.subTest(path=path.name):
                self.assertRefused("", path=path)

    def test_bytes_changing_between_reads_refused(self):
        real = backup._read_stored
        calls = []

        def drifting(path):
            snapshot = real(path)
            calls.append(path)
            if len(calls) == 2:
                name, digest, size = snapshot.files[0]
                return dataclasses.replace(snapshot, files=((name, "f" * 64, size),) + snapshot.files[1:])
            return snapshot

        with mock.patch.object(backup, "_read_stored", drifting):
            self.assertRefused("changed while it was being read")


class Facts(GateCase):
    def test_facts_are_required(self):
        with self.assertRaises(ReadbackRefused) as ctx:
            readback_gate(self.cap, self.hashes, None)
        self.assertIn("facts are required", str(ctx.exception))

    def test_wrong_device_id(self):
        self.assertRefused("device identifier does not match", facts={**fx.FACTS, "deviceId": "OTHER-DEVICE"})

    def test_facts_matching_no_profile(self):
        for name, value in (("firmware", "1300.9"), ("model", "un43u8000fbxza"), ("arch", "armv7")):
            with self.subTest(field=name):
                self.assertRefused("exactly one supported profile", facts={**fx.FACTS, name: value})

    def test_malformed_facts(self):
        missing = dict(fx.FACTS)
        del missing["deviceId"]
        self.assertRefused("device facts", facts=missing)
        self.assertRefused("unknown properties", facts={**fx.FACTS, "notAFact": "x"})
        self.assertRefused("must be a mapping", facts=[("model", "x")])
        self.assertRefused("must be a string", facts={**fx.FACTS, "osVersion": 9})


class Baseline(GateCase):
    def contradicted_capsule(self, original: str) -> Path:
        self._made = getattr(self, "_made", 0) + 1
        manifest = copy.deepcopy(self.manifest)
        manifest["systemKeyChanges"][1] = system_key_record("db/appfw/home_appid", original, "org.example.home")
        return fx.write_capsule(self.tmp / f"customized-{self._made}", manifest)

    def test_stock_declaration_is_marked_declared(self):
        result = readback_gate(self.cap, self.hashes, fx.FACTS, baseline="stock")
        self.assertEqual((result.baseline, result.baseline_basis), ("stock", "declared"))
        self.assertTrue(any("not device-proven" in line for line in result.limitations))

    def test_label_must_be_exact(self):
        for bad in ("Stock", "stock ", "", "current", True, None, 1):
            with self.subTest(label=bad):
                self.assertRefused("baseline must be exactly one of", baseline=bad)

    def test_customized_tv_cannot_be_labelled_stock(self):
        for original in (fx.OUR_PACKAGE, fx.OUR_PACKAGE.lower()):
            with self.subTest(original=original):
                path = self.contradicted_capsule(original)
                self.assertEqual(readback_gate(path, self.hashes, fx.FACTS).baseline, "current-state")
                self.assertRefused("baseline 'stock' contradicted", path=path, baseline="stock")

    def test_negative_control_contradiction_check_removed(self):
        path = self.contradicted_capsule(fx.OUR_PACKAGE)
        with mock.patch.object(backup, "check_baseline", lambda *args: None):
            self.assertEqual(readback_gate(path, self.hashes, fx.FACTS, baseline="stock").baseline, "stock")
        self.assertRefused("contradicted", path=path, baseline="stock")


class NoBypass(GateCase):
    def test_signature_has_no_bypass_switch(self):
        params = inspect.signature(readback_gate).parameters
        self.assertEqual(list(params), ["stored_path", "device_hashes", "facts", "baseline"])
        self.assertEqual(params["baseline"].kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertEqual(params["baseline"].default, "current-state")

    def test_result_is_immutable_and_unforgeable(self):
        result = readback_gate(self.cap, self.hashes, fx.FACTS)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.baseline = "stock"  # type: ignore[misc]
        with self.assertRaises(TypeError):
            dataclasses.replace(result, baseline="stock")
        with self.assertRaises(TypeError):
            dataclasses.replace(result, files=result.files[1:])
        fields = {f.name: getattr(result, f.name) for f in dataclasses.fields(result) if f.name != "_seal"}
        with self.assertRaises(TypeError):
            GateResult(**fields)
        for name in ("passed", "ok", "verified", "force", "skip"):
            self.assertFalse(hasattr(result, name))
        for value in (result.device, result.files, result.counts, result.limitations):
            self.assertIsInstance(value, tuple)

    def test_refusal_is_a_capsule_error(self):
        self.assertTrue(issubclass(ReadbackRefused, CapsuleError))

    def test_module_has_no_device_network_or_write_path(self):
        tree = ast.parse(BACKUP_SOURCE.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(("." * node.level) + (node.module or ""))
        self.assertEqual(imported, {"__future__", "hashlib", "json", "tarfile", "dataclasses", "pathlib",
                                    "typing", ".capsule", ".paths", ".profile"})
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "open":
                self.assertEqual([a.value for a in node.args[1:]], ["rb"])
            if isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, {"write", "write_bytes", "write_text", "mkdir", "unlink", "rename",
                                             "rmtree", "remove", "touch", "chmod"})
        self.assertEqual(transport.TRANSPORTS, {})


if __name__ == "__main__":
    unittest.main()

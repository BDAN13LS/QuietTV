"""Streaming assemble gate. Synthetic fixtures only; no device, network or real data."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tempfile
import tracemalloc
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_public_toolkit_fixtures as fx  # noqa: E402
from tvkit import assemble as asm  # noqa: E402
from tvkit import capsule as cap  # noqa: E402
from tvkit.capsule import CapsuleError  # noqa: E402

BIG_DATA = b"\xa5" * (8 * 1024 * 1024)


def tree_hashes(root: Path) -> dict[str, str]:
    result = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        result[rel] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "dir"
    return result


def big_parts():
    """Two independent packages whose payload tars each carry one ~8 MiB file."""
    pids = fx.PACKAGE_IDS[:2]
    members = {}
    payloads = {}
    xattrs = {}
    for pid in pids:
        package = fx.package_members(pid)
        package.append({
            "path": "res/big.bin", "kind": "file", "uid": 0, "gid": 0, "mode": 0o644,
            "data": BIG_DATA,
        })
        members[pid] = package
        payloads[pid] = fx.make_tar(package)
        xattrs[pid] = fx.xattr_manifest(pid, package)
    return pids, members, payloads, xattrs


def expected_manifest(pids, members, payloads, xattrs):
    default_members, default_payloads, default_xattrs = fx.build_parts()
    manifest = fx.capsule_manifest(default_payloads, default_xattrs, default_members)
    packages = []
    for pid in pids:
        packages.append({
            "packageId": pid,
            "installRoot": f"/opt/usr/apps/{pid}",
            "payload": {"file": f"payload/{pid}.tar", "sha256": fx.sha(payloads[pid]),
                        "size": len(payloads[pid])},
            "xattrManifest": {"file": f"xattrs/{pid}.json", "sha256": fx.sha(xattrs[pid]),
                              "size": len(xattrs[pid])},
            "entries": fx.capsule_entries(members[pid]),
        })
    manifest["packages"] = packages
    return manifest


class AssembleCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.profile = fx.profile()

    def tearDown(self):
        self._tmp.cleanup()

    def write_capture(self, name, pids, payloads, xattrs, manifest):
        src = self.tmp / name
        src.mkdir()
        packages = []
        for pid in pids:
            (src / f"{pid}.tar").write_bytes(payloads[pid])
            (src / f"{pid}.json").write_bytes(xattrs[pid])
            packages.append({"packageId": pid, "installRoot": f"/opt/usr/apps/{pid}",
                             "payload": f"{pid}.tar", "xattrManifest": f"{pid}.json"})
        spec = {
            "schema": "tvkit-assemble-spec/1",
            "packages": packages,
            "registryChanges": manifest["registryChanges"],
            "systemKeys": [{"key": k["key"], "original": k["original"],
                            "expectedCurrent": k["expectedCurrent"]}
                           for k in manifest["systemKeyChanges"]],
            "ourPackages": manifest["ourPackages"],
        }
        (src / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
        return src, src / "spec.json"

    def test_two_8mib_payloads_assemble_with_peak_below_8mib(self):
        pids, members, payloads, xattrs = big_parts()
        expected = expected_manifest(pids, members, payloads, xattrs)
        src, spec = self.write_capture("captured", pids, payloads, xattrs, expected)
        out = self.tmp / "assembled"

        tracemalloc.start()
        try:
            result = asm.assemble(spec, self.profile, fx.FACTS, out)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

        print(f"PEAK-BYTES {peak}")
        self.assertLess(peak, len(BIG_DATA), f"peak traced memory was {peak} bytes")
        self.assertTrue(result.payload_verified)
        self.assertTrue(result.device_verified)
        self.assertEqual(result.counts["packages"], 2)
        self.assertEqual(result.counts["entries"], 14)
        self.assertEqual(result.counts["xattrs"], 14)

        built = json.loads((out / "capsule.json").read_text(encoding="utf-8"))
        self.assertEqual(built, expected)
        for pid in pids:
            self.assertEqual(fx.sha((out / "payload" / f"{pid}.tar").read_bytes()),
                             fx.sha(payloads[pid]))
            self.assertEqual((out / "xattrs" / f"{pid}.json").read_bytes(), xattrs[pid])
            package = next(p for p in built["packages"] if p["packageId"] == pid)
            entry = next(e for e in package["entries"] if e["path"] == "res/big.bin")
            self.assertEqual(entry["sha256"], fx.sha(BIG_DATA))
            self.assertEqual(entry["size"], len(BIG_DATA))

    def test_source_mutated_during_second_pass_copy_is_refused(self):
        members, payloads, xattrs = fx.build_parts()
        manifest = fx.capsule_manifest(payloads, xattrs, members)
        pids = fx.PACKAGE_IDS
        src, spec = self.write_capture("captured", pids, payloads, xattrs, manifest)
        out = self.tmp / "mutated"
        first_payload = src / f"{pids[0]}.tar"
        real_copy = asm._copy_chunks

        def mutate_after_copy(handle, target, limit, label):
            copied = real_copy(handle, target, limit, label)
            with open(first_payload, "ab") as growing:
                growing.write(b"grew after the descriptor was taken")
            return copied

        with mock.patch.object(asm, "_copy_chunks", mutate_after_copy):
            with self.assertRaises(CapsuleError) as ctx:
                asm.assemble(spec, self.profile, fx.FACTS, out)
        self.assertIn("changed", str(ctx.exception))
        self.assertFalse((out / "capsule.json").exists())

    def test_source_swapped_before_copy_is_refused(self):
        members, payloads, xattrs = fx.build_parts()
        manifest = fx.capsule_manifest(payloads, xattrs, members)
        src, spec = self.write_capture("captured", fx.PACKAGE_IDS, payloads, xattrs, manifest)
        out = self.tmp / "swapped"
        real_copy = asm._copy_verified
        swapped = False

        def swap_then_copy(source, target, label):
            nonlocal swapped
            if not swapped:
                replacement = src / "replacement.tar"
                replacement.write_bytes(source.path.read_bytes())
                os.replace(replacement, source.path)
                swapped = True
            return real_copy(source, target, label)

        with mock.patch.object(asm, "_copy_verified", swap_then_copy):
            with self.assertRaisesRegex(CapsuleError, "changed"):
                asm.assemble(spec, self.profile, fx.FACTS, out)
        self.assertFalse((out / "capsule.json").exists())

    def test_existing_output_is_refused_before_source_inspection(self):
        members, payloads, xattrs = fx.build_parts()
        manifest = fx.capsule_manifest(payloads, xattrs, members)
        pids = fx.PACKAGE_IDS
        src, spec = self.write_capture("captured", pids, payloads, xattrs, manifest)
        out = self.tmp / "assembled"
        asm.assemble(spec, self.profile, fx.FACTS, out)
        before = tree_hashes(out)

        with mock.patch.object(asm, "_inspect_payload", side_effect=AssertionError("source inspection started")):
            with self.assertRaisesRegex(CapsuleError, "must not exist"):
                asm.assemble(spec, self.profile, fx.FACTS, out)
        self.assertEqual(tree_hashes(out), before)

    def test_destination_raced_in_during_copy_is_not_overwritten(self):
        members, payloads, xattrs = fx.build_parts()
        manifest = fx.capsule_manifest(payloads, xattrs, members)
        src, spec = self.write_capture("captured", fx.PACKAGE_IDS, payloads, xattrs, manifest)
        out = self.tmp / "raced"
        real_copy = asm._copy_verified
        raced = []
        copies = 0

        def race_then_copy(source, target, label):
            nonlocal copies
            copies += 1
            if copies == 2:
                target.write_bytes(b"existing owner file")
                raced.append(target)
            return real_copy(source, target, label)

        with mock.patch.object(asm, "_copy_verified", race_then_copy):
            with self.assertRaisesRegex(CapsuleError, "exclusively"):
                asm.assemble(spec, self.profile, fx.FACTS, out)
        self.assertEqual(len(raced), 1)
        self.assertEqual(raced[0].read_bytes(), b"existing owner file")
        self.assertTrue((out / "payload" / f"{fx.PACKAGE_IDS[0]}.tar").exists())
        self.assertFalse((out / "capsule.json").exists())

    def test_real_validator_bounds_both_metadata_read_paths(self):
        members, payloads, xattrs = fx.build_parts()
        manifest = fx.capsule_manifest(payloads, xattrs, members)
        src, spec = self.write_capture("captured", fx.PACKAGE_IDS, payloads, xattrs, manifest)
        out = self.tmp / "validated"
        asm.assemble(spec, self.profile, fx.FACTS, out)
        real_open = cap.DirectorySource.open
        for target in ("capsule.json", f"xattrs/{fx.PACKAGE_IDS[0]}.json"):
            with self.subTest(target=target):
                sizes = []
                class GuardedReader:
                    def __init__(self, handle): self.handle = handle
                    def read(self, size=-1):
                        if not 0 < size <= 64 * 1024:
                            raise AssertionError("validator metadata read is not bounded")
                        sizes.append(size)
                        return self.handle.read(size)
                    def __enter__(self): return self
                    def __exit__(self, *args): self.handle.close()
                def guarded_open(source, name):
                    handle = real_open(source, name)
                    return GuardedReader(handle) if name == target else handle
                with mock.patch.object(cap.DirectorySource, "open", guarded_open):
                    result = cap.validate(out, self.profile, fx.FACTS)
                self.assertTrue(sizes)
                self.assertTrue(result.payload_verified)
                self.assertTrue(result.device_verified)

    def test_oversize_payload_is_refused_before_reading(self):
        members, payloads, xattrs = fx.build_parts()
        manifest = fx.capsule_manifest(payloads, xattrs, members)
        src, spec = self.write_capture("captured", fx.PACKAGE_IDS, payloads, xattrs, manifest)
        (src / f"{fx.PACKAGE_IDS[0]}.tar").write_bytes(b"x" * 4096)
        with mock.patch.object(asm, "MAX_MEMBER_BYTES", 1024), \
                mock.patch.object(asm, "_hash_stream", side_effect=AssertionError("read started")):
            with self.assertRaises(CapsuleError) as ctx:
                asm.assemble(spec, self.profile, fx.FACTS, self.tmp / "out")
        self.assertIn("too large", str(ctx.exception))
        self.assertFalse((self.tmp / "out").exists())

    def test_oversize_xattr_manifest_is_refused_before_reading(self):
        members, payloads, xattrs = fx.build_parts()
        manifest = fx.capsule_manifest(payloads, xattrs, members)
        src, spec = self.write_capture("captured", fx.PACKAGE_IDS, payloads, xattrs, manifest)
        (src / f"{fx.PACKAGE_IDS[0]}.json").write_bytes(b"x" * 4096)
        real_hash = asm._hash_stream

        def hash_except_xattr(handle, limit, label):
            if "xattrManifest" in label:
                raise AssertionError("read started")
            return real_hash(handle, limit, label)

        with mock.patch.object(asm, "MAX_XATTR_MANIFEST_BYTES", 1024), \
                mock.patch.object(asm, "_hash_stream", hash_except_xattr):
            with self.assertRaises(CapsuleError) as ctx:
                asm.assemble(spec, self.profile, fx.FACTS, self.tmp / "out")
        self.assertIn("too large", str(ctx.exception))
        self.assertFalse((self.tmp / "out").exists())


class MetadataReadBounds(unittest.TestCase):
    def test_small_metadata_is_read_in_bounded_chunks(self):
        class RecordingReader(io.BytesIO):
            def read(self, size=-1):
                if not 0 < size <= 64 * 1024:
                    raise AssertionError("unbounded or oversized metadata read")
                return super().read(size)

        data = b"small metadata"
        self.assertEqual(cap._read_capped(RecordingReader(data), 32 << 20, "test"), data)

    def test_growth_after_original_eof_is_not_hidden(self):
        class GrowingReader(io.BytesIO):
            grown = False
            def read(self, size=-1):
                if self.tell() == 5 and not self.grown:
                    self.grown = True
                    self.write(b"extra")
                    self.seek(5)
                return super().read(size)

        with self.assertRaisesRegex(CapsuleError, "too large"):
            cap._read_capped(GrowingReader(b"12345"), 5, "test")
        self.assertEqual(cap._read_capped(io.BytesIO(b"12345"), 5, "test"), b"12345")


if __name__ == "__main__":
    unittest.main()

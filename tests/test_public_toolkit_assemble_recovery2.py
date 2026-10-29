"""recovery/2 assembly (assemble.assemble_recovery2): a captured Tizen package with an
explicitly approved external loader symlink becomes a fully validated recovery/2 capsule.

Synthetic fixtures only: the profile/2 and trusted policies in tests/fixtures, and a
made-up /usr/bin/example-runtime loader target. Payload tars are built here with the shared
fixtures, independently of tvkit. Each new guard is switched off in memory to show it matters,
and the capsule/1 assemble path is proven unchanged (it still refuses a loader-link payload).
"""

from __future__ import annotations

import json
import os
import sys
import tarfile
import tempfile
import tracemalloc
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_public_toolkit_fixtures as fx  # noqa: E402
import test_public_toolkit_profile_v2 as pv2  # noqa: E402
import test_public_toolkit_recovery2 as r2t  # noqa: E402
from test_public_toolkit_recovery2 import BOOT, EVIDENCE, EXT_PKG, LINK, PLAIN_PKG, TARGET, members_for  # noqa: E402
from tvkit import assemble as asm  # noqa: E402
from tvkit import capsule as cap  # noqa: E402
from tvkit import recovery2 as r2  # noqa: E402
from tvkit.capsule import CapsuleError  # noqa: E402
from tvkit.profile import device_id_sha256, parse_profile  # noqa: E402

RECOVERY_SPEC_SCHEMA = "tvkit-assemble-spec/2"


def evidence_map(**overrides):
    return {TARGET: r2.TargetEvidence(**{**EVIDENCE, **overrides})}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.profile = parse_profile(pv2.load("profile-v2.json"))
        self.facts = pv2.consumer_facts()
        self.pol = r2t.policies()
        self.evidence = evidence_map()
        # The owner's captured tree for each package; EXT_PKG carries the approved loader link.
        self.members = {pid: members_for(pid) for pid in (EXT_PKG, PLAIN_PKG)}
        self.count = 0

    def custom_policy(self, target):
        """An ExternalRefPolicy approving one loader link pointing at ``target``."""
        return r2.ExternalRefPolicy(self.profile.profile_id, (
            r2.ExternalApproval(EXT_PKG, LINK, target, r2.TargetEvidence(**EVIDENCE)),))

    def ext_members_linking(self, target):
        """EXT_PKG's tree with its loader link pointing at ``target`` instead of TARGET."""
        base = [m for m in members_for(EXT_PKG) if m["path"] != LINK]
        return base + [{"path": LINK, "kind": "symlink", "uid": 5001, "gid": 100,
                        "mode": 0o777, "linkTarget": target}]

    def write_capture(self, directory, extra_for=None):
        """Stage the captured payload/xattr blobs and a recovery/2 assemble spec.

        ``extra_for`` maps a package id to extra raw tar members (TarInfo or
        (TarInfo, bytes)), for building hostile payloads.
        """
        extra_for = extra_for or {}
        directory.mkdir()
        packages = []
        for pid, members in self.members.items():
            (directory / f"{pid}.tar").write_bytes(fx.make_tar(members, extra_for.get(pid, [])))
            (directory / f"{pid}.json").write_bytes(fx.xattr_manifest(pid, members))
            packages.append({"packageId": pid, "installRoot": f"/opt/usr/apps/{pid}",
                             "payload": f"{pid}.tar", "xattrManifest": f"{pid}.json"})
        spec = {
            "schema": RECOVERY_SPEC_SCHEMA,
            "packages": packages,
            "registryChanges": [{"appId": PLAIN_PKG,
                                 "fields": {"app_disable": {"before": "false", "applied": "true"}}}],
            "systemKeys": [{"key": "db/appfw/home_appid", "original": "org.example.home",
                            "expectedCurrent": fx.OUR_PACKAGE}],
            "bootChanges": [{"target": t, "field": f, "before": b, "ours": o} for t, f, b, o in BOOT],
            "ourPackages": [{"packageId": fx.OUR_PACKAGE}],
        }
        (directory / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
        return directory / "spec.json"

    def spec_dir(self, name="src", extra_for=None):
        self.count += 1
        d = self.tmp / f"{name}{self.count}"
        return self.write_capture(d, extra_for)

    def assemble(self, spec=None, out=None, facts="default", evidence="default", **pol):
        spec = spec if spec is not None else self.spec_dir()
        out = out if out is not None else self.tmp / f"out{self.count}"
        facts = self.facts if facts == "default" else facts
        evidence = self.evidence if evidence == "default" else evidence
        pols = {**self.pol, **pol}
        return asm.assemble_recovery2(spec, self.profile, facts, out,
                                      external_policy=pols["external_policy"],
                                      boot_policy=pols["boot_policy"], target_evidence=evidence)

    def refused(self, fragment=None, **kwargs):
        out = kwargs.get("out") or self.tmp / f"refout{self.count + 1}"
        kwargs["out"] = out
        with self.assertRaises(CapsuleError) as ctx:
            self.assemble(**kwargs)
        if fragment:
            self.assertIn(fragment, str(ctx.exception))
        self.assertFalse(out.exists(), "a refusal must leave no output folder")
        return str(ctx.exception)


# --------------------------------------------------------------------------


class CurrentBehaviour(Base):
    """Reproduce that today's capsule/1 assemble cannot carry a loader-link capture."""

    def capsule1_loader_spec(self, directory):
        directory.mkdir()
        members = {pid: fx.package_members(pid) for pid in fx.PACKAGE_IDS}
        members[fx.PACKAGE_IDS[0]].append(
            {"path": LINK, "kind": "symlink", "uid": 5001, "gid": 100, "mode": 0o777, "linkTarget": TARGET})
        packages = []
        for pid in fx.PACKAGE_IDS:
            (directory / f"{pid}.tar").write_bytes(fx.make_tar(members[pid]))
            (directory / f"{pid}.json").write_bytes(fx.xattr_manifest(pid, members[pid]))
            packages.append({"packageId": pid, "installRoot": f"/opt/usr/apps/{pid}",
                             "payload": f"{pid}.tar", "xattrManifest": f"{pid}.json"})
        spec = {"schema": "tvkit-assemble-spec/1", "packages": packages,
                "registryChanges": [], "systemKeys": [], "ourPackages": []}
        (directory / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
        return directory / "spec.json"

    def test_capsule1_assemble_refuses_a_loader_link_payload(self):
        spec = self.capsule1_loader_spec(self.tmp / "v1loader")
        out = self.tmp / "v1-out"
        with self.assertRaises(CapsuleError) as ctx:
            asm.assemble(spec, fx.profile(), dict(fx.FACTS), out)
        self.assertIn("escapes the package root", str(ctx.exception))
        self.assertFalse(out.exists())

    def test_capsule1_assemble_refuses_profile2(self):
        spec = self.capsule1_loader_spec(self.tmp / "v2loader")
        out = self.tmp / "v2-out"
        with self.assertRaises(CapsuleError) as ctx:
            asm.assemble(spec, self.profile, self.facts, out)
        self.assertIn("cannot bind firmware build or region", str(ctx.exception))
        self.assertFalse(out.exists())


class PositivePath(Base):
    def test_valid_loader_capture_assembles_and_fully_validates(self):
        out = self.tmp / "ok"
        result = self.assemble(out=out)
        self.assertIsInstance(result, r2.Recovery2)
        self.assertTrue(result.device_bound)
        self.assertEqual(result.counts["externalRefs"], 1)
        self.assertEqual(result.counts["bootChanges"], 3)
        self.assertEqual(result.counts["packages"], 2)
        self.assertEqual(result.external_policy_digest, self.pol["external_policy"].digest)
        self.assertEqual(result.boot_policy_digest, self.pol["boot_policy"].digest)

        built = json.loads((out / "capsule.json").read_text(encoding="utf-8"))
        self.assertEqual(built["schema"], "tvkit-recovery/2")
        # Every profile/2 fact is bound from the facts, including build and region and the id hash.
        self.assertEqual(built["device"], {**{k: self.facts[k] for k in self.profile.match_fields},
                                           "deviceIdSha256": device_id_sha256(pv2.DEVICE_ID)})
        self.assertIn("firmwareBuild", built["device"])
        self.assertIn("region", built["device"])
        # The approved loader link is present as a symlink entry pointing at the external target.
        ext_entries = next(p for p in built["packages"] if p["packageId"] == EXT_PKG)["entries"]
        link_entry = next(e for e in ext_entries if e["path"] == LINK)
        self.assertEqual((link_entry["kind"], link_entry["linkTarget"]), ("symlink", TARGET))
        self.assertEqual(built["externalRefs"][0]["target"], TARGET)
        self.assertEqual(built["externalRefs"][0]["access"], "read-only")
        # Source blobs were copied; the on-disk capsule re-validates and dispatches as recovery/2.
        self.assertTrue((out / "payload" / f"{EXT_PKG}.tar").is_file())
        self.assertTrue((out / "xattrs" / f"{PLAIN_PKG}.json").is_file())
        again = r2.validate_any(out, self.profile, self.facts, **self.pol)
        self.assertIsInstance(again, r2.Recovery2)

    def test_boot_values_bound_by_digest(self):
        out = self.tmp / "boot"
        self.assemble(out=out)
        built = json.loads((out / "capsule.json").read_text(encoding="utf-8"))
        for change in built["bootChanges"]:
            self.assertEqual(change["beforeSha256"], fx.sha(change["before"].encode()))
            self.assertEqual(change["oursSha256"], fx.sha(change["ours"].encode()))
            self.assertNotEqual(change["before"], change["ours"])


class Refusals(Base):
    def no_source_read(self):
        return mock.patch.object(asm, "_inspect_payload",
                                 side_effect=AssertionError("a source was read before the refusal"))

    def test_missing_policy_refused_before_any_source_read(self):
        with self.no_source_read():
            with self.assertRaises(CapsuleError):
                self.assemble(out=self.tmp / "nopol", external_policy=None)
            with self.assertRaises(CapsuleError):
                self.assemble(out=self.tmp / "nopol2", boot_policy={"allow": True})
        self.assertFalse((self.tmp / "nopol").exists())
        self.assertFalse((self.tmp / "nopol2").exists())

    def test_wrong_profile_policy_refused_before_any_source_read(self):
        other = r2.ExternalRefPolicy("some-other-profile", self.pol["external_policy"].approvals)
        with self.no_source_read():
            self.refused("different profile", external_policy=other)

    def test_wrong_and_missing_firmware_or_region_refused_before_any_read(self):
        variants = {
            "wrong-build": {**self.facts, "firmwareBuild": "SYNTHETIC-BUILD-0000.2"},
            "wrong-region": {**self.facts, "region": "SYNTHETIC-OTHER"},
            "wrong-model": {**self.facts, "model": "SYNTHETIC-QTV0002"},
            "no-build": {k: v for k, v in self.facts.items() if k != "firmwareBuild"},
            "no-region": {k: v for k, v in self.facts.items() if k != "region"},
            "no-device-id": {k: v for k, v in self.facts.items() if k != "deviceId"},
        }
        for label, facts in variants.items():
            with self.subTest(label), self.no_source_read():
                self.refused(facts=facts)

    def test_unknown_device_fact_refused(self):
        with self.no_source_read():
            self.refused("unknown", facts={**self.facts, "serial": "x"})

    def test_device_block_never_filled_from_profile(self):
        # Negative control: even with match_device off, a dropped region is caught by the explicit
        # missing-fact check and refuses; no value is ever substituted from the expected profile.
        with mock.patch.object(asm, "match_device"):
            with self.assertRaises(CapsuleError):
                self.assemble(out=self.tmp / "nofill",
                              facts={k: v for k, v in self.facts.items() if k != "region"})
        self.assertFalse((self.tmp / "nofill").exists())

    def test_spec_cannot_self_authorize_external_refs_or_allow_flags(self):
        for extra in ("externalRefs", "allow", "device"):
            with self.subTest(extra=extra):
                spec_file = self.spec_dir()
                data = json.loads(spec_file.read_text(encoding="utf-8"))
                data[extra] = [] if extra == "externalRefs" else True
                spec_file.write_text(json.dumps(data), encoding="utf-8")
                self.refused("unknown properties", spec=spec_file)

    def test_wrong_spec_schema_refused(self):
        spec_file = self.spec_dir()
        data = json.loads(spec_file.read_text(encoding="utf-8"))
        data["schema"] = "tvkit-assemble-spec/1"
        spec_file.write_text(json.dumps(data), encoding="utf-8")
        self.refused("spec schema must be", spec=spec_file)

    def test_changed_target_evidence_refused_as_stale(self):
        self.refused("stale", evidence=evidence_map(sha256="0" * 64))
        self.refused("stale", evidence=evidence_map(mode=0o777))

    def test_missing_or_extra_caller_evidence_refused(self):
        self.refused("no caller evidence", evidence={})
        extra = {**evidence_map(), "/usr/bin/other-runtime": r2.TargetEvidence(**EVIDENCE)}
        self.refused("no external reference uses", evidence=extra)

    def test_evidence_must_be_typed(self):
        with self.assertRaises(CapsuleError):
            self.assemble(out=self.tmp / "rawev", evidence={TARGET: dict(EVIDENCE)})

    def test_link_escaping_through_the_approved_link_refused(self):
        # bin/alias -> runtime/.. passes the lexical derivation but the resolved check in the
        # real validator (run before any folder is made) catches it.
        self.members[EXT_PKG] = members_for(EXT_PKG, [
            {"path": "bin/alias", "kind": "symlink", "uid": 5001, "gid": 100, "mode": 0o777,
             "linkTarget": "runtime/.."}])
        self.refused("escapes the package root")

    def test_unapproved_boot_field_refused(self):
        spec_file = self.spec_dir()
        data = json.loads(spec_file.read_text(encoding="utf-8"))
        data["bootChanges"].append({"target": "service:org.example.echo-daemon", "field": "restart",
                                    "before": "always", "ours": "never"})
        spec_file.write_text(json.dumps(data), encoding="utf-8")
        self.refused("does not approve", spec=spec_file)

    def test_approved_but_ancestor_or_devtree_target_refused_before_mkdir(self):
        # The exact-pair exemption keeps the link in the entry table; check_external_target in the
        # real validator (before mkdir) is the only thing that stops a bad approved target.
        for target, fragment in (("/opt/usr", "install root"), ("/dev/mem", "device tree")):
            with self.subTest(target):
                self.members[EXT_PKG] = self.ext_members_linking(target)
                ev = {target: r2.TargetEvidence(**EVIDENCE)}
                out = self.tmp / f"bad{fragment.split()[0]}"
                with self.assertRaises(CapsuleError) as ctx:
                    self.assemble(out=out, evidence=ev, external_policy=self.custom_policy(target))
                self.assertIn(fragment, str(ctx.exception))
                self.assertFalse(out.exists())

    def test_guard_off_check_external_target_accepts_ancestor(self):
        # Both ways: with check_external_target off, the /opt/usr ancestor target is accepted.
        self.members[EXT_PKG] = self.ext_members_linking("/opt/usr")
        ev = {"/opt/usr": r2.TargetEvidence(**EVIDENCE)}
        pol = self.custom_policy("/opt/usr")
        self.refused("install root", evidence=ev, external_policy=pol)
        with mock.patch.object(r2, "check_external_target"):
            self.assertIsInstance(
                self.assemble(out=self.tmp / "ancestorok", evidence=ev, external_policy=pol), r2.Recovery2)

    def test_payload_path_traversal_member_refused_before_mkdir(self):
        spec = self.spec_dir(extra_for={EXT_PKG: [tarfile.TarInfo("../evil")]})
        out = self.tmp / "trav-out"
        with self.assertRaises(CapsuleError) as ctx:
            self.assemble(spec=spec, out=out)
        self.assertIn("outside the package root", str(ctx.exception))
        self.assertFalse(out.exists())

    def test_tar_member_below_the_loader_link_refused_before_mkdir(self):
        info = tarfile.TarInfo(f"{LINK}/evil")
        info.size = 1
        spec = self.spec_dir(extra_for={EXT_PKG: [(info, b"x")]})
        out = self.tmp / "below-out"
        with self.assertRaises(CapsuleError) as ctx:
            self.assemble(spec=spec, out=out)
        self.assertIn("below a symbolic link", str(ctx.exception))
        self.assertFalse(out.exists())


class SourceConsistency(Base):
    def test_source_swapped_before_copy_is_refused(self):
        spec = self.spec_dir()
        out = self.tmp / "swapped"
        real_copy = asm._copy_verified
        swapped = False

        def swap_then_copy(source, target, label):
            nonlocal swapped
            if not swapped:
                replacement = spec.parent / "replacement.tar"
                replacement.write_bytes(source.path.read_bytes())
                os.replace(replacement, source.path)
                swapped = True
            return real_copy(source, target, label)

        with mock.patch.object(asm, "_copy_verified", swap_then_copy):
            with self.assertRaisesRegex(CapsuleError, "changed"):
                self.assemble(spec=spec, out=out)
        self.assertFalse((out / "capsule.json").exists())

    def test_source_mutated_during_copy_is_refused(self):
        spec = self.spec_dir()
        out = self.tmp / "mutated"
        first_payload = spec.parent / f"{EXT_PKG}.tar"
        real_copy = asm._copy_chunks

        def mutate_after_copy(handle, target, limit, label):
            copied = real_copy(handle, target, limit, label)
            with open(first_payload, "ab") as growing:
                growing.write(b"grew after the descriptor was taken")
            return copied

        with mock.patch.object(asm, "_copy_chunks", mutate_after_copy):
            with self.assertRaisesRegex(CapsuleError, "changed"):
                self.assemble(spec=spec, out=out)
        self.assertFalse((out / "capsule.json").exists())

    def test_destination_raced_in_during_copy_is_not_overwritten(self):
        spec = self.spec_dir()
        out = self.tmp / "raced"
        real_copy = asm._copy_verified
        copies = 0
        raced = {}
        owner_bytes = b"existing owner file"

        def race_then_copy(source, target, label):
            nonlocal copies
            copies += 1
            if copies == 2:
                target.write_bytes(owner_bytes)
                raced["path"] = target
            return real_copy(source, target, label)

        with mock.patch.object(asm, "_copy_verified", race_then_copy):
            with self.assertRaisesRegex(CapsuleError, "exclusively"):
                self.assemble(spec=spec, out=out)
        self.assertFalse((out / "capsule.json").exists())
        # The exclusive "xb" open must refuse before writing, so the raced-in owner file
        # keeps its exact bytes — a refusal never clobbers a file that was already there.
        self.assertEqual(raced["path"].read_bytes(), owner_bytes)


class Streaming(Base):
    def test_payload_inspection_and_copy_stream_below_one_payload(self):
        # The recovery/2 path reuses the capsule/1 source-streaming machinery (_collect_packages
        # and _write_capsule), so inspecting and copying two independent ~8 MiB payloads peaks
        # below one payload. This measures only the inspect/copy helpers; the end-to-end peak,
        # including the final recovery2.validate (which now reads the manifest with _read_capped
        # rather than preallocating MAX_CAPSULE_MANIFEST_BYTES), is covered by
        # test_full_assemble_recovery2_peaks_below_one_payload.
        from tvkit import strictjson as sj
        big = b"\xa5" * (8 * 1024 * 1024)
        extra = [{"path": "res", "kind": "directory", "uid": 0, "gid": 0, "mode": 0o755},
                 {"path": "res/big.bin", "kind": "file", "uid": 0, "gid": 0, "mode": 0o644, "data": big}]
        self.members = {pid: members_for(pid, extra) for pid in (EXT_PKG, PLAIN_PKG)}
        spec = self.spec_dir()  # stage the payload tars to disk BEFORE measuring
        base = spec.parent
        external_links_for = {}
        for a in self.pol["external_policy"].approvals:
            external_links_for.setdefault(a.package_id, {})[a.link_path] = a.target
        out = self.tmp / "big"

        tracemalloc.start()
        try:
            spec_obj = sj.require_object(
                sj.load_file(spec, 4 * 1024 * 1024), "assemble spec",
                ("schema", "packages", "registryChanges", "systemKeys", "bootChanges", "ourPackages"))
            packages, sources = asm._collect_packages(spec_obj, base, external_links_for)
            asm._write_capsule(out, sources, {"schema": "scratch", "packages": packages})
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

        print(f"PEAK-BYTES {peak}")
        self.assertLess(peak, len(big), f"streaming copy peak was {peak} bytes")
        # The derived entry carries the big file's real hash and size, and the copy is byte-exact.
        entry = next(e for p in packages if p["packageId"] == EXT_PKG
                     for e in p["entries"] if e["path"] == "res/big.bin")
        self.assertEqual((entry["sha256"], entry["size"]), (fx.sha(big), len(big)))
        self.assertEqual(fx.sha((out / "payload" / f"{EXT_PKG}.tar").read_bytes()),
                         fx.sha((spec.parent / f"{EXT_PKG}.tar").read_bytes()))

    def test_full_assemble_still_validates_with_big_payloads(self):
        big = b"\xa5" * (8 * 1024 * 1024)
        extra = [{"path": "res", "kind": "directory", "uid": 0, "gid": 0, "mode": 0o755},
                 {"path": "res/big.bin", "kind": "file", "uid": 0, "gid": 0, "mode": 0o644, "data": big}]
        self.members = {pid: members_for(pid, extra) for pid in (EXT_PKG, PLAIN_PKG)}
        result = self.assemble(out=self.tmp / "bigok")
        self.assertEqual(result.counts["packages"], 2)
        self.assertEqual(result.counts["externalRefs"], 1)

    def test_full_assemble_recovery2_peaks_below_one_payload(self):
        # End-to-end memory regression: the WHOLE assemble_recovery2, including the final on-disk
        # recovery2.validate (which reads capsule.json and re-hashes both payloads and xattr
        # manifests), must peak below a single ~8 MiB payload. The old shared read_manifest_bytes
        # did handle.read(MAX_CAPSULE_MANIFEST_BYTES + 1), preallocating a 16 MiB buffer inside
        # validate even for a tiny manifest, which pushes this peak past the threshold; the
        # _read_capped reader does not. The threshold is one payload and the validator is NOT
        # skipped: both are the point of the regression. Normal assembly peaks near ~3.2 MB.
        big = b"\xa5" * (8 * 1024 * 1024)
        extra = [{"path": "res", "kind": "directory", "uid": 0, "gid": 0, "mode": 0o755},
                 {"path": "res/big.bin", "kind": "file", "uid": 0, "gid": 0, "mode": 0o644, "data": big}]
        self.members = {pid: members_for(pid, extra) for pid in (EXT_PKG, PLAIN_PKG)}
        spec = self.spec_dir()  # stage both ~8 MiB payload tars to disk BEFORE measuring
        out = self.tmp / "fullbig"

        tracemalloc.start()
        try:
            result = self.assemble(spec=spec, out=out)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

        print(f"FULL-PEAK-BYTES {peak}")
        self.assertEqual(result.counts["packages"], 2)
        self.assertEqual(result.counts["externalRefs"], 1)
        self.assertLess(peak, len(big), f"full assemble peak was {peak} bytes")


class GuardOff(Base):
    def test_empty_policy_refuses_the_loader_link(self):
        # Both ways: the approval is what lets the escaping loader link through.
        empty = r2.ExternalRefPolicy(self.profile.profile_id, ())
        self.refused("escapes the package root", external_policy=empty, evidence={})
        self.assertIsInstance(self.assemble(out=self.tmp / "withpol"), r2.Recovery2)

    def test_guard_off_check_external_ref_accepts_stale_evidence(self):
        # With the policy exact-match check off, caller evidence that differs from the policy
        # (stale) is accepted; with it on, the same capsule is refused.
        stale = evidence_map(sha256="1" * 64)
        self.refused("stale", evidence=stale)
        with mock.patch.object(r2, "check_external_ref"):
            self.assertIsInstance(
                self.assemble(out=self.tmp / "staleok", evidence=stale), r2.Recovery2)


class _RecordingHandle:
    """Wraps a real capsule.json file handle and records every read() size requested.

    It records the *requested* size, not the bytes returned: a tiny manifest read
    with handle.read(16 MiB + 1) returns only a few hundred bytes yet preallocates a
    16 MiB buffer, so only the requested size exposes the old over-allocation.
    """

    def __init__(self, handle, sizes):
        self._handle = handle
        self._sizes = sizes

    def read(self, size=-1, *args, **kwargs):
        self._sizes.append(size)
        return self._handle.read(size, *args, **kwargs)

    def __enter__(self):
        self._handle.__enter__()
        return self

    def __exit__(self, *exc):
        return self._handle.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self._handle, name)


class ManifestBoundedRead(Base):
    """The shared capsule.json reader must read in bounded chunks, not preallocate 16 MiB.

    Spies DirectorySource.open (no production monkeypatch) and asserts every requested
    read of capsule.json is >0 and <=64 KiB. With the old read_manifest_bytes that did
    handle.read(MAX_CAPSULE_MANIFEST_BYTES + 1) this FAILS (a 16 MiB+1 request); with the
    _read_capped reader it PASSES. The both-ways control reverts the production function.
    """

    def _spy_capsule_json_reads(self, sizes):
        real_open = cap.DirectorySource.open

        def spy_open(src_self, name):
            handle = real_open(src_self, name)
            if name != cap.MANIFEST_NAME:
                return handle
            return _RecordingHandle(handle, sizes)

        return mock.patch.object(cap.DirectorySource, "open", spy_open)

    def _assemble_folder(self):
        out = self.tmp / "forread"
        self.assemble(out=out)
        return out

    def test_validate_and_validate_any_and_manifest_schema_read_in_bounded_chunks(self):
        out = self._assemble_folder()
        sizes: list[int] = []
        with self._spy_capsule_json_reads(sizes):
            # All three shared callsites of read_manifest_bytes.
            self.assertEqual(cap.manifest_schema(out), r2.RECOVERY_SCHEMA)
            self.assertIsInstance(
                r2.validate(out, self.profile, self.facts, **self.pol), r2.Recovery2)
            self.assertIsInstance(
                r2.validate_any(out, self.profile, self.facts, **self.pol), r2.Recovery2)
        self.assertTrue(sizes, "capsule.json was never read through the shared reader")
        for requested in sizes:
            self.assertGreater(requested, 0, f"a capsule.json read requested {requested} bytes")
            self.assertLessEqual(requested, 64 * 1024,
                                 f"a capsule.json read requested {requested} bytes (>64 KiB)")


if __name__ == "__main__":
    unittest.main()

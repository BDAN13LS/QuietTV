from __future__ import annotations

import copy
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_public_toolkit_fixtures as fx  # noqa: E402
from tvkit import capsule as cap
from tvkit.assemble import assemble, bundle
from tvkit.capsule import CapsuleError, validate
from tvkit.profile import ProfileError, match_device, parse_profile, device_id_sha256


class CapsuleCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.profile = fx.profile()
        self.members, self.payloads, self.xattrs = fx.build_parts()
        self.manifest = fx.capsule_manifest(self.payloads, self.xattrs, self.members)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, manifest=None, payloads=None, xattrs=None, name="cap") -> Path:
        return fx.write_capsule(self.tmp / name, manifest or self.manifest, payloads, xattrs)

    def assertRefused(self, path, fragment, facts=None):
        with self.assertRaises(CapsuleError) as ctx:
            validate(path, self.profile, facts)
        self.assertIn(fragment, str(ctx.exception))

    def rehash_payload(self, manifest, pid, data):
        package = next(p for p in manifest["packages"] if p["packageId"] == pid)
        package["payload"]["sha256"] = fx.sha(data)
        package["payload"]["size"] = len(data)


class Positive(CapsuleCase):
    def test_valid_directory_capsule_exact_counts(self):
        result = validate(self.write(), self.profile, fx.FACTS)
        self.assertTrue(result.payload_verified)
        self.assertTrue(result.device_verified)
        self.assertEqual(result.counts, {
            "packages": 3, "entries": 18, "xattrs": 18,
            "registryChanges": 2, "systemKeyChanges": 2, "ourPackages": 1,
        })

    def test_without_facts_device_is_not_claimed_verified(self):
        result = validate(self.write(), self.profile)
        self.assertTrue(result.payload_verified)
        self.assertFalse(result.device_verified)

    def test_bundle_round_trip(self):
        directory = self.write()
        digest = bundle(directory, self.tmp / "cap.tar", self.profile)
        self.assertEqual(len(digest), 64)
        self.assertTrue(validate(self.tmp / "cap.tar", self.profile, fx.FACTS).device_verified)

    def test_assemble_matches_independent_fixture(self):
        src = self.tmp / "captured"
        src.mkdir()
        spec_packages = []
        for pid in fx.PACKAGE_IDS:
            (src / f"{pid}.tar").write_bytes(self.payloads[pid])
            (src / f"{pid}.json").write_bytes(self.xattrs[pid])
            spec_packages.append({"packageId": pid, "installRoot": f"/opt/usr/apps/{pid}",
                                  "payload": f"{pid}.tar", "xattrManifest": f"{pid}.json"})
        spec = {
            "schema": "tvkit-assemble-spec/1",
            "packages": spec_packages,
            "registryChanges": self.manifest["registryChanges"],
            "systemKeys": [{"key": k["key"], "original": k["original"], "expectedCurrent": k["expectedCurrent"]}
                           for k in self.manifest["systemKeyChanges"]],
            "ourPackages": self.manifest["ourPackages"],
        }
        example = json.loads((fx.TOOLKIT / "examples" / "three-app-assemble-spec.example.json")
                             .read_text(encoding="utf-8"))
        self.assertEqual(example, spec)
        (src / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
        out = self.tmp / "assembled"
        assemble(src / "spec.json", self.profile, fx.FACTS, out)
        built = json.loads((out / "capsule.json").read_text(encoding="utf-8"))
        self.assertEqual(built, self.manifest)
        with self.assertRaises(CapsuleError):
            assemble(src / "spec.json", self.profile, fx.FACTS, out)  # never overwrites

    def test_example_document_is_current_and_schema_valid(self):
        example = json.loads((fx.TOOLKIT / "examples" / "three-app-capsule.example.json").read_text(encoding="utf-8"))
        self.assertEqual(example, self.manifest)
        parsed = cap.parse_manifest(example, self.profile)
        self.assertEqual(len(parsed.packages), 3)


class DeviceBinding(CapsuleCase):
    def test_wrong_device_identifier(self):
        self.assertRefused(self.write(), "device identifier", {**fx.FACTS, "deviceId": "OTHER-DEVICE"})

    def test_wrong_live_firmware(self):
        self.assertRefused(self.write(), "firmware", {**fx.FACTS, "firmware": "1300.9"})

    def test_capsule_bound_to_other_firmware_model_arch(self):
        for field, value in (("firmware", "1300.80"), ("model", "un43u8000fbxza"),
                             ("model", "UN43U8000FBXZA "), ("arch", "armv7"), ("osVersion", "9.0")):
            manifest = copy.deepcopy(self.manifest)
            manifest["device"][field] = value
            with self.subTest(field=field, value=value):
                self.assertRefused(self.write(manifest, name=f"c-{field}-{len(value)}"), "does not exactly match")

    def test_profile_match_is_exact(self):
        match_device(self.profile, fx.FACTS)
        for field, value in (("firmware", "1300.80"), ("firmware", "1300"), ("model", "un43u8000fbxza"),
                             ("model", " UN43U8000FBXZA"), ("arch", "aarch64"), ("osName", "tizen")):
            with self.subTest(field=field, value=value), self.assertRaises(ProfileError):
                match_device(self.profile, {**fx.FACTS, field: value})

    def test_placeholder_device_id_fails_closed(self):
        with self.assertRaises(ProfileError):
            device_id_sha256("<CONFIGURE: device id>")
        manifest = copy.deepcopy(self.manifest)
        manifest["device"]["deviceIdSha256"] = "<CONFIGURE: hash>"
        self.assertRefused(self.write(manifest), "placeholder")

    def test_profile_with_placeholder_match_rejected(self):
        data = json.loads(next((fx.TOOLKIT / "profiles").glob("*.json")).read_text(encoding="utf-8"))
        data["match"]["model"] = "<CONFIGURE: model>"
        with self.assertRaises(ProfileError):
            parse_profile(data)


class Tampering(CapsuleCase):
    def test_tampered_payload_bytes(self):
        payloads = dict(self.payloads)
        data = bytearray(payloads["org.example.bravo"])
        data[600] ^= 0x01
        payloads["org.example.bravo"] = bytes(data)
        self.assertRefused(self.write(payloads=payloads), "payload bytes do not match")

    def test_tampered_member_with_recomputed_outer_hash(self):
        members = copy.deepcopy(self.members["org.example.alpha"])
        members[2]["data"] = b"#!/bin/sh\necho evil!!!!!!!!!!!!!!!!\n"[: len(members[2]["data"])]
        data = fx.make_tar(members)
        payloads = {**self.payloads, "org.example.alpha": data}
        manifest = copy.deepcopy(self.manifest)
        self.rehash_payload(manifest, "org.example.alpha", data)
        self.assertRefused(self.write(manifest, payloads), "member bytes differ")

    def test_tampered_xattr_manifest(self):
        xattrs = {**self.xattrs, "org.example.charlie": self.xattrs["org.example.charlie"] + b" "}
        self.assertRefused(self.write(xattrs=xattrs), "xattr manifest bytes")

    def test_uid_and_mode_mismatch(self):
        for field, value in (("uid", 0), ("gid", 5000), ("mode", 0o4755)):
            manifest = copy.deepcopy(self.manifest)
            entry = next(e for e in manifest["packages"][0]["entries"] if e["path"] == "bin/app")
            entry[field] = value
            with self.subTest(field=field):
                self.assertRefused(self.write(manifest, name=f"m-{field}"), "differs from the capsule")

    def test_extra_file_in_capsule(self):
        path = self.write()
        (path / "notes.txt").write_text("x", encoding="utf-8")
        self.assertRefused(path, "unexpected files")

    def test_xattr_manifest_disagreement_is_caught(self):
        for mutate, fragment in (
            (lambda d: d.update(rootBasename="org.example.other"), "rootBasename"),
            (lambda d: d["entries"].pop(), "entry set differs"),
            (lambda d: d["entries"][0]["xattrs"].update({"user.x": "%%%"}), "invalid base64"),
            (lambda d: d["entries"][2].update(sha256="0" * 64), "disagrees"),
        ):
            doc = json.loads(self.xattrs["org.example.alpha"])
            mutate(doc)
            data = json.dumps(doc).encode()
            xattrs = {**self.xattrs, "org.example.alpha": data}
            manifest = copy.deepcopy(self.manifest)
            ref = manifest["packages"][0]["xattrManifest"]
            ref["sha256"], ref["size"] = fx.sha(data), len(data)
            with self.subTest(fragment=fragment):
                self.assertRefused(self.write(manifest, xattrs=xattrs, name=f"x-{len(fragment)}"), fragment)


class HostileArchives(CapsuleCase):
    def with_extra(self, extra, entries_extra=()):
        data = fx.make_tar(self.members["org.example.alpha"], extra)
        payloads = {**self.payloads, "org.example.alpha": data}
        manifest = copy.deepcopy(self.manifest)
        self.rehash_payload(manifest, "org.example.alpha", data)
        manifest["packages"][0]["entries"].extend(entries_extra)
        return self.write(manifest, payloads)

    @staticmethod
    def file_member(name, data=b"x"):
        info = tarfile.TarInfo(name)
        info.size, info.uid, info.gid, info.mode = len(data), 5001, 100, 0o644
        return (info, data)

    def test_traversal_member(self):
        self.assertRefused(self.with_extra([self.file_member("../../etc/evil")]), "outside the package root")

    def test_absolute_member(self):
        self.assertRefused(self.with_extra([self.file_member("/etc/evil")]), "outside the package root")

    def test_backslash_member(self):
        self.assertRefused(self.with_extra([self.file_member("bin\\..\\..\\evil")]), "outside the package root")

    def test_symlink_child_escape_in_archive(self):
        # "current" is a symlink to bin; a member below it would be written through the link.
        self.assertRefused(self.with_extra([self.file_member("current/evil")]), "below a symbolic link")

    def test_hardlink_member(self):
        info = tarfile.TarInfo("res/hard")
        info.type, info.linkname = tarfile.LNKTYPE, "bin/app"
        self.assertRefused(self.with_extra([info]), "hard link")

    def test_fifo_and_device_members(self):
        for kind in (tarfile.FIFOTYPE, tarfile.CHRTYPE):
            info = tarfile.TarInfo("res/special")
            info.type = kind
            with self.subTest(kind=kind):
                path = self.with_extra([info])
                self.assertRefused(path, "special member")
                import shutil
                shutil.rmtree(path)

    def test_duplicate_member(self):
        self.assertRefused(self.with_extra([self.file_member("res/strings.txt")]), "duplicate")

    def test_case_colliding_member(self):
        self.assertRefused(self.with_extra([self.file_member("RES/strings.txt")]), "case-colliding")

    def test_member_not_listed_in_capsule(self):
        self.assertRefused(self.with_extra([self.file_member("res/new.txt")]), "does not list")


class SchemaRules(CapsuleCase):
    def refused_manifest(self, mutate, fragment):
        manifest = copy.deepcopy(self.manifest)
        mutate(manifest)
        self.assertRefused(self.write(manifest), fragment)

    def test_entry_path_traversal(self):
        self.refused_manifest(lambda m: m["packages"][0]["entries"].append(
            {"path": "../outside", "kind": "directory", "uid": 0, "gid": 0, "mode": 0o755}), "'..'")

    def test_entry_below_symlink(self):
        self.refused_manifest(lambda m: m["packages"][0]["entries"].append(
            {"path": "current/x", "kind": "directory", "uid": 0, "gid": 0, "mode": 0o755}), "non-directory parent")

    def test_symlink_target_escapes(self):
        for target in ("../../../etc", "/etc/passwd", "/opt/usr/apps/org.example.bravo/bin"):
            def mutate(m, target=target):
                next(e for e in m["packages"][0]["entries"] if e["path"] == "current")["linkTarget"] = target
            with self.subTest(target=target):
                manifest = copy.deepcopy(self.manifest)
                mutate(manifest)
                self.assertRefused(self.write(manifest, name=f"l{len(target)}"), "escapes the package root")

    def test_symlink_escape_through_other_links(self):
        # current -> . ; up -> current/..  : lexically "the root", really the parent folder.
        def chain(m, first, second):
            entries = m["packages"][0]["entries"]
            next(e for e in entries if e["path"] == "current")["linkTarget"] = first
            entries.append({"path": "up", "kind": "symlink", "uid": 0, "gid": 0, "mode": 0o777,
                            "linkTarget": second})
        for first, second in ((".", "current/.."), ("res/..", "current/../x"),
                              ("/opt/usr/apps/org.example.alpha", "current/.."), ("up", "current")):
            manifest = copy.deepcopy(self.manifest)
            chain(manifest, first, second)
            with self.subTest(first=first, second=second):
                self.assertRefused(self.write(manifest, name=f"ch{len(first)}{len(second)}"), "escapes")
        manifest = copy.deepcopy(self.manifest)
        chain(manifest, "bin", "current/../res")  # stays inside: bin/.. is the root
        cap.parse_manifest(manifest, self.profile)

    def test_duplicate_entry_path(self):
        self.refused_manifest(lambda m: m["packages"][0]["entries"].append(
            dict(m["packages"][0]["entries"][1])), "duplicates another entry")

    def test_duplicate_json_property(self):
        path = self.write()
        text = (path / "capsule.json").read_text(encoding="utf-8")
        (path / "capsule.json").write_text(text.replace('"schema":', '"schema": "x", "schema":', 1), encoding="utf-8")
        self.assertRefused(path, "duplicate JSON property")

    def test_nan_constant(self):
        path = self.write()
        text = (path / "capsule.json").read_text(encoding="utf-8")
        (path / "capsule.json").write_text(text.replace('"mode": 493', '"mode": NaN', 1), encoding="utf-8")
        self.assertRefused(path, "non-standard JSON constant")

    def test_bool_is_not_an_integer(self):
        self.refused_manifest(lambda m: m["packages"][0]["entries"][0].update(uid=True), "must be an integer")

    def test_unknown_property(self):
        self.refused_manifest(lambda m: m.update(extra=1), "unknown properties")

    def test_duplicate_and_conflicting_packages(self):
        self.refused_manifest(lambda m: m["packages"].append(copy.deepcopy(m["packages"][0])), "conflicts")
        manifest = copy.deepcopy(self.manifest)
        manifest["ourPackages"] = [{"packageId": "org.example.alpha"}]
        self.assertRefused(self.write(manifest, name="c2"), "conflicts with a restored package")

    def test_install_root_not_allowlisted(self):
        for root in ("/opt/usr/apps/org.example.other", "/etc/org.example.alpha", "/opt/usr/apps/../org.example.alpha"):
            manifest = copy.deepcopy(self.manifest)
            manifest["packages"][0]["installRoot"] = root
            with self.subTest(root=root):
                self.assertRefused(self.write(manifest, name=f"r{len(root)}"), "install root")

    def test_protected_package_and_app_ids(self):
        manifest = copy.deepcopy(self.manifest)
        package = manifest["packages"][0]
        package["packageId"] = "org.tizen.setting"
        package["installRoot"] = "/opt/usr/apps/org.tizen.setting"
        self.assertRefused(self.write(manifest, name="p1"), "protected")
        # The stock Security Center is core security: an informational UI for it is not supported.
        manifest = copy.deepcopy(self.manifest)
        package = manifest["packages"][0]
        package["packageId"] = "com.samsung.tv.SecurityCenter"
        package["installRoot"] = "/opt/usr/apps/com.samsung.tv.SecurityCenter"
        self.assertRefused(self.write(manifest, name="p2"), "protected")
        for app in ("com.samsung.tv.hdmi-cec", "org.tizen.network-manager", "com.example.SecurityCenter",
                    "com.samsung.tv.SecurityCenter"):
            manifest = copy.deepcopy(self.manifest)
            manifest["registryChanges"][0]["appId"] = app
            with self.subTest(app=app):
                self.assertRefused(self.write(manifest, name=f"a{len(app)}-{app[:3]}"), "protected")

    def test_registry_rules(self):
        cases = [
            (lambda m: m["registryChanges"][0]["fields"].update(app_name={"before": "a", "applied": "b"}), "outside the allowlist"),
            (lambda m: m["registryChanges"][0]["fields"]["app_disable"].update(applied="yes"), "outside the allowlist"),
            (lambda m: m["registryChanges"][0]["fields"]["app_disable"].update(applied="false"), "no change"),
            (lambda m: m["registryChanges"][1]["fields"]["app_disable"].update(applied="true"), "mixes numeric"),
            (lambda m: m["registryChanges"].append(copy.deepcopy(m["registryChanges"][0])), "duplicate"),
        ]
        for i, (mutate, fragment) in enumerate(cases):
            manifest = copy.deepcopy(self.manifest)
            mutate(manifest)
            with self.subTest(fragment=fragment):
                self.assertRefused(self.write(manifest, name=f"g{i}"), fragment)

    def test_system_key_rules(self):
        cases = [
            (lambda m: m["systemKeyChanges"][0].update(key="db/menu/network/proxy"), "not an allowlisted"),
            (lambda m: m["systemKeyChanges"][0].update(originalSha256="0" * 64), "does not bind"),
            (lambda m: m["systemKeyChanges"][0].update(expectedCurrent="other.app"), "does not bind"),
            (lambda m: m["systemKeyChanges"].append(copy.deepcopy(m["systemKeyChanges"][0])), "duplicate"),
            (lambda m: m["systemKeyChanges"][0].update(type="Int"), "type"),
        ]
        for i, (mutate, fragment) in enumerate(cases):
            manifest = copy.deepcopy(self.manifest)
            mutate(manifest)
            with self.subTest(fragment=fragment):
                self.assertRefused(self.write(manifest, name=f"k{i}"), fragment)

    def test_wrong_provenance_or_profile(self):
        self.refused_manifest(lambda m: m.update(provenance="samsung-firmware"), "provenance")
        manifest = copy.deepcopy(self.manifest)
        manifest["profileId"] = "SOME-OTHER-TV"
        self.assertRefused(self.write(manifest, name="p2"), "different profile")


class BundleRules(CapsuleCase):
    def test_bundle_with_link_member_refused(self):
        directory = self.write()
        out = self.tmp / "evil.tar"
        with tarfile.open(out, "w") as archive:
            for path in sorted(directory.rglob("*")):
                if path.is_file():
                    archive.add(path, arcname=path.relative_to(directory).as_posix())
            info = tarfile.TarInfo("payload/link")
            info.type, info.linkname = tarfile.SYMTYPE, "/etc"
            archive.addfile(info)
        self.assertRefused(out, "link or special member")

    def test_compressed_bundle_refused(self):
        out = self.tmp / "c.tar.gz"
        with tarfile.open(out, "w:gz") as archive:
            archive.addfile(tarfile.TarInfo("capsule.json"), io.BytesIO(b""))
        self.assertRefused(out, "uncompressed")


if __name__ == "__main__":
    unittest.main()

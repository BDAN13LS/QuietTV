"""tvkit-recovery/2: validator, trusted policies and the pure three-way undo planner.

Synthetic fixtures only: the profile/2 in tests/fixtures/consumer and the
trusted policies in tests/fixtures/recovery2. ``/usr/bin/example-runtime`` is
a made-up target, not a real firmware object. Capsules are built here in
Python, independently of tvkit, and written to scratch folders. Each key
guard is switched off in memory (``mock.patch.object``) to show it matters.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import io
import json
import re
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_public_toolkit_fixtures as fx  # noqa: E402
import test_public_toolkit_profile_v2 as pv2  # noqa: E402
from tvkit import capsule, plan, recovery2 as r2  # noqa: E402
from tvkit.capsule import CapsuleError, system_key_record  # noqa: E402
from tvkit.profile import device_id_sha256, parse_profile  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "recovery2"
EXT_PKG, PLAIN_PKG = "org.example.delta", "org.example.echo"
LINK, TARGET = "bin/runtime", "/usr/bin/example-runtime"
RUNTIME = b"synthetic example runtime\n"
EVIDENCE = {"kind": "file", "sha256": fx.sha(RUNTIME), "size": len(RUNTIME), "uid": 0, "gid": 0, "mode": 0o755}
BOOT = [  # target, field, before, ours
    ("bootcache-record:org.example.delta", "autostart", "true", "false"),
    ("bootcache-record:org.example.delta", "priority", "normal", "low"),
    ("service:org.example.echo-daemon", "enabled", "1", "0"),
]


def raw(name):
    return (FIXTURES / name).read_bytes()


def policies():
    return {"external_policy": r2.external_policy_from_json(raw("external-policy.json")),
            "boot_policy": r2.boot_policy_from_json(raw("boot-policy.json"))}


def members_for(pid, extra=()):
    body = f"#!/bin/sh\necho {pid}\n".encode()
    items = [
        {"path": ".", "kind": "directory", "uid": 5001, "gid": 100, "mode": 0o755},
        {"path": "bin", "kind": "directory", "uid": 5001, "gid": 100, "mode": 0o755},
        {"path": "bin/app", "kind": "file", "uid": 5001, "gid": 100, "mode": 0o750, "data": body},
        {"path": "current", "kind": "symlink", "uid": 5001, "gid": 100, "mode": 0o777, "linkTarget": "bin"},
    ]
    if pid == EXT_PKG:
        items.append({"path": LINK, "kind": "symlink", "uid": 5001, "gid": 100, "mode": 0o777, "linkTarget": TARGET})
    return items + list(extra)


def boot_change(target, field, before, ours):
    return {"target": target, "field": field, "before": before, "ours": ours,
            "beforeSha256": fx.sha(before.encode()), "oursSha256": fx.sha(ours.encode())}


class Builder:
    """A recovery/2 capsule as plain data, written to a scratch folder on demand."""

    def __init__(self, profile, facts):
        self.members = {pid: members_for(pid) for pid in (EXT_PKG, PLAIN_PKG)}
        self.profile, self.facts = profile, facts
        self.payload_override = {}

    def parts(self):
        payloads = {pid: self.payload_override.get(pid) or fx.make_tar(m) for pid, m in self.members.items()}
        xattrs = {pid: fx.xattr_manifest(pid, m) for pid, m in self.members.items()}
        return payloads, xattrs

    def manifest(self, payloads, xattrs):
        packages = []
        for pid in self.members:
            packages.append({
                "packageId": pid,
                "installRoot": f"/opt/usr/apps/{pid}",
                "payload": {"file": f"payload/{pid}.tar", "sha256": fx.sha(payloads[pid]),
                            "size": len(payloads[pid])},
                "xattrManifest": {"file": f"xattrs/{pid}.json", "sha256": fx.sha(xattrs[pid]),
                                  "size": len(xattrs[pid])},
                "entries": fx.capsule_entries(self.members[pid]),
            })
        return {
            "schema": "tvkit-recovery/2",
            "profileId": self.profile.profile_id,
            "provenance": "owner-device-backup",
            "device": {**{k: self.facts[k] for k in self.profile.match_fields},
                       "deviceIdSha256": device_id_sha256(pv2.DEVICE_ID)},
            "packages": packages,
            "externalRefs": [{"packageId": EXT_PKG, "linkPath": LINK, "target": TARGET,
                              "access": "read-only", "targetEvidence": dict(EVIDENCE)}],
            "registryChanges": [{"appId": PLAIN_PKG, "fields": {"app_disable": {"before": "false",
                                                                                 "applied": "true"}}}],
            "systemKeyChanges": [system_key_record("db/appfw/home_appid", "org.example.home", fx.OUR_PACKAGE)],
            "bootChanges": [boot_change(*row) for row in BOOT],
            "ourPackages": [{"packageId": fx.OUR_PACKAGE}],
        }

    def write(self, directory, edit=None, manifest_text=None):
        payloads, xattrs = self.parts()
        manifest = self.manifest(payloads, xattrs)
        if edit:
            edit(manifest)
        (directory / "payload").mkdir(parents=True)
        (directory / "xattrs").mkdir()
        for pid in self.members:
            (directory / "payload" / f"{pid}.tar").write_bytes(payloads[pid])
            (directory / "xattrs" / f"{pid}.json").write_bytes(xattrs[pid])
        text = manifest_text(manifest) if manifest_text else json.dumps(manifest, indent=2)
        (directory / "capsule.json").write_text(text, encoding="utf-8")
        return directory


class Store:
    """An in-memory TV record store: the consumer side, standing in for a typed writer."""

    def __init__(self, state="ours"):
        self.data = {
            "bootcache-record:org.example.delta": {"autostart": "x", "priority": "x", "unlisted": "keep-me"},
            "service:org.example.echo-daemon": {"enabled": "x", "restart": "always"},
            "service:org.example.unrelated": {"enabled": "1"},
        }
        for target, field, before, ours in BOOT:
            self.data[target][field] = ours if state == "ours" else before
        self.writes = 0

    def evidence(self, manifest_sha256, targets=None, **overrides):
        records = []
        for target, field, _, _ in BOOT:
            value = self.data[target][field]
            records.append({"target": target, "field": field, "value": value, "valueSha256": fx.sha(value.encode())})
        doc = {"schema": "tvkit-recovery-evidence/1", "profileId": pv2.load("profile-v2.json")["profileId"],
               "deviceIdSha256": device_id_sha256(pv2.DEVICE_ID), "manifestSha256": manifest_sha256,
               "bootRecords": records,
               "externalTargets": targets if targets is not None else [
                   {"target": TARGET, "targetEvidence": dict(EVIDENCE)}]}
        doc.update(overrides)
        return json.dumps(doc).encode()

    def apply(self, undo):
        for action in undo.actions:
            current = self.data[action.target][action.field]
            assert fx.sha(current.encode()) == action.require_current_sha256 == fx.sha(action.require_current.encode())
            self.data[action.target][action.field] = action.write
            self.writes += 1


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.profile = parse_profile(pv2.load("profile-v2.json"))
        self.facts = pv2.consumer_facts()
        self.pol = policies()
        self.builder = Builder(self.profile, self.facts)
        self.count = 0

    def write(self, **kwargs):
        self.count += 1
        return self.builder.write(self.tmp / f"cap{self.count}", **kwargs)

    def validate(self, path, facts="default", **pol):
        facts = self.facts if facts == "default" else facts
        return r2.validate(path, self.profile, facts, **(pol or self.pol))

    def refused(self, path, fragment=None, facts="default", **pol):
        with self.assertRaises(CapsuleError) as ctx:
            self.validate(path, facts, **pol)
        if fragment:
            self.assertIn(fragment, str(ctx.exception))
        return str(ctx.exception)

    def manifest_sha(self, path):
        return hashlib.sha256((path / "capsule.json").read_bytes()).hexdigest()


# --------------------------------------------------------------------------


class Validation(Base):
    def test_valid_capsule_binds_profile2_device_and_policies(self):
        result = self.validate(self.write())
        self.assertTrue(result.device_bound)
        self.assertEqual(dict(result.device), {**{k: self.facts[k] for k in self.profile.match_fields},
                                               "deviceIdSha256": device_id_sha256(pv2.DEVICE_ID)})
        self.assertIn("firmwareBuild", result.device)
        self.assertIn("region", result.device)
        self.assertEqual(result.counts["externalRefs"], 1)
        self.assertEqual(result.counts["bootChanges"], 3)
        self.assertEqual(result.external_policy_digest, self.pol["external_policy"].digest)
        self.assertFalse(self.validate(self.write(), facts=None).device_bound)

    def test_validate_any_dispatch(self):
        path = self.write()
        self.assertIsInstance(r2.validate_any(path, self.profile, self.facts, **self.pol), r2.Recovery2)
        with self.assertRaises(CapsuleError):  # recovery/2 without policies
            r2.validate_any(path, self.profile, self.facts)
        v1 = fx.write_capsule(self.tmp / "v1")
        self.assertTrue(r2.validate_any(v1, fx.profile(), dict(fx.FACTS)).payload_verified)
        with self.assertRaises(CapsuleError):  # capsule/1 does not take policies
            r2.validate_any(v1, fx.profile(), dict(fx.FACTS), **self.pol)
        other = self.write(edit=lambda m: m.update(schema="tvkit-recovery/3"))
        with self.assertRaises(CapsuleError):
            r2.validate_any(other, self.profile, self.facts, **self.pol)

    def test_capsule1_validator_refuses_recovery2(self):
        path = self.write()
        with self.assertRaises(CapsuleError) as ctx:
            capsule.validate(path, self.profile, self.facts)
        self.assertIn("cannot bind firmware build or region", str(ctx.exception))
        with self.assertRaises(CapsuleError) as ctx:
            capsule.validate(path, fx.profile())
        self.assertIn("unknown properties bootChanges, externalRefs", str(ctx.exception))

    def test_capsule1_still_refuses_the_same_external_archive(self):
        members = {pid: members_for(pid) for pid in fx.PACKAGE_IDS}
        members[fx.PACKAGE_IDS[0]].append(
            {"path": LINK, "kind": "symlink", "uid": 5001, "gid": 100, "mode": 0o777, "linkTarget": TARGET})
        payloads = {pid: fx.make_tar(m) for pid, m in members.items()}
        xattrs = {pid: fx.xattr_manifest(pid, m) for pid, m in members.items()}
        path = fx.write_capsule(self.tmp / "v1ext", fx.capsule_manifest(payloads, xattrs, members), payloads, xattrs)
        with self.assertRaises(CapsuleError) as ctx:
            capsule.validate(path, fx.profile(), dict(fx.FACTS))
        self.assertIn("escapes the package root", str(ctx.exception))

    def test_capsule1_plans_refuse_recovery2_result(self):
        result = self.validate(self.write())
        for build in (lambda: plan.restore_plan(result, self.profile),
                      lambda: plan.setup_plan(result, self.profile)):
            with self.assertRaises(CapsuleError) as ctx:
                build()
            self.assertIn("bootChanges", str(ctx.exception))

    def test_guard_off_plan_refusal_would_silently_drop_boot_changes(self):
        result = self.validate(self.write())
        with mock.patch.object(r2.Recovery2, "payload_verified", True):
            restore = plan.restore_plan(result, self.profile)
        dumped = json.dumps(restore.to_json())
        self.assertNotIn("autostart", dumped)  # the danger the guard prevents

    def test_profile1_refused(self):
        self.assertRaises(CapsuleError, r2.validate, self.write(), fx.profile(), None, **self.pol)

    def test_guard_off_profile2_requirement(self):
        v1 = fx.profile()
        builder = Builder(v1, dict(fx.FACTS))
        pol = {"external_policy": r2.ExternalRefPolicy(v1.profile_id, self.pol["external_policy"].approvals),
               "boot_policy": r2.BootFieldPolicy(v1.profile_id, self.pol["boot_policy"].fields)}
        path = builder.write(self.tmp / "p1")
        with self.assertRaises(CapsuleError):
            r2.validate(path, v1, None, **pol)
        with mock.patch.object(r2, "require_profile2"):
            self.assertIsInstance(r2.validate(path, v1, None, **pol), r2.Recovery2)

    def test_duplicate_json_and_unknown_keys(self):
        path = self.write(manifest_text=lambda m: json.dumps(m)[:-1] + ', "schema": "tvkit-recovery/2"}')
        self.refused(path, "duplicate JSON property")
        for edit in (lambda m: m.update(allow=True),
                     lambda m: m["externalRefs"][0].update(allow=True),
                     lambda m: m["bootChanges"][0].update(approved=True),
                     lambda m: m["device"].update(extra="x")):
            with self.subTest(edit=edit):
                self.refused(self.write(edit=edit), "unknown properties")

    def test_policy_types_and_profile(self):
        path = self.write()
        bad = [{"external_policy": {"allow": True}, "boot_policy": self.pol["boot_policy"]},
               {"external_policy": self.pol["external_policy"], "boot_policy": {"allow": True}},
               {"external_policy": r2.ExternalRefPolicy("other", ()), "boot_policy": self.pol["boot_policy"]}]
        for pol in bad:
            with self.subTest(pol=pol):
                self.refused(path, **pol)
        with self.assertRaises(CapsuleError):
            r2.external_policy_from_json(json.loads(raw("external-policy.json")))  # a dict hides duplicates
        dup = raw("boot-policy.json").replace(b'"schema"', b'"profileId": "x", "schema"', 1)
        with self.assertRaises(CapsuleError):
            r2.boot_policy_from_json(dup)


class DeviceBinding(Base):
    def variants(self):
        return {"wrong-build": {**self.facts, "firmwareBuild": "SYNTHETIC-BUILD-0000.2"},
                "wrong-region": {**self.facts, "region": "SYNTHETIC-OTHER"},
                "wrong-model": {**self.facts, "model": "SYNTHETIC-QTV0002"},
                "no-build": {k: v for k, v in self.facts.items() if k != "firmwareBuild"},
                "no-region": {k: v for k, v in self.facts.items() if k != "region"},
                "wrong-device": {**self.facts, "deviceId": "SYNTHETIC-CONSUMER-DEVICE-0002"}}

    def test_wrong_facts_refused(self):
        path = self.write()
        for label, facts in self.variants().items():
            with self.subTest(label):
                self.refused(path, facts=facts)

    def test_device_block_must_carry_every_match_field(self):
        for field in ("firmwareBuild", "region"):
            with self.subTest(field):
                self.refused(self.write(edit=lambda m, f=field: m["device"].pop(f)), "missing")
                self.refused(self.write(edit=lambda m, f=field: m["device"].update({f: "SYNTHETIC-X"})),
                             "does not exactly match")
        self.refused(self.write(edit=lambda m: m["device"].update(deviceIdSha256="<CONFIGURE>")), "placeholder")

    def test_guard_off_bind_device(self):
        path = self.write()
        with mock.patch.object(r2, "bind_device"):
            for label, facts in self.variants().items():
                with self.subTest(label):
                    self.assertTrue(self.validate(path, facts).device_bound)


class ExternalRefs(Base):
    def test_unknown_and_stale_refs_refused(self):
        path = self.write()
        empty = {**self.pol, "external_policy": r2.ExternalRefPolicy(self.profile.profile_id, ())}
        self.refused(path, "unknown to the trusted policy", **empty)
        approval = self.pol["external_policy"].approvals[0]
        stale_hash = r2.ExternalApproval(approval.package_id, approval.link_path, approval.target,
                                         r2.TargetEvidence(**{**EVIDENCE, "sha256": "0" * 64}))
        stale_target = r2.ExternalApproval(approval.package_id, approval.link_path, "/usr/bin/example-runtime2",
                                           approval.evidence)
        for item in (stale_hash, stale_target):
            with self.subTest(item=item.target):
                self.refused(path, "stale", **{**self.pol, "external_policy": r2.ExternalRefPolicy(
                    self.profile.profile_id, (item,))})

    def test_tampered_ref_evidence_in_manifest_refused(self):
        for key, value in (("sha256", "1" * 64), ("size", 27), ("mode", 0o777), ("uid", 1)):
            with self.subTest(key):
                self.refused(self.write(edit=lambda m, k=key, v=value: m["externalRefs"][0]["targetEvidence"]
                                        .update({k: v})), "stale")

    def test_ref_shape_rules(self):
        cases = {
            "missing-ref": (lambda m: m.update(externalRefs=[]), "escapes the package root"),
            "duplicate": (lambda m: m["externalRefs"].append(copy.deepcopy(m["externalRefs"][0])), "duplicates"),
            "writable": (lambda m: m["externalRefs"][0].update(access="read-write"), "read-only"),
            "root-link": (lambda m: m["externalRefs"][0].update(linkPath="."), "package root"),
            "other-package": (lambda m: m["externalRefs"][0].update(packageId=PLAIN_PKG), "unknown"),
            "relative-target": (lambda m: m["externalRefs"][0].update(target="../../usr/bin/x"), "absolute"),
            "glob-target": (lambda m: m["externalRefs"][0].update(target="/usr/bin/*"), "wildcard"),
            "inside-root": (lambda m: m["externalRefs"][0].update(target="/opt/usr/apps/org.example.x/f"),
                            "install root"),
            "ancestor": (lambda m: m["externalRefs"][0].update(target="/opt/usr"), "install root"),
            "devtree": (lambda m: m["externalRefs"][0].update(target="/dev/mem"), "device tree"),
            "dir-kind": (lambda m: m["externalRefs"][0]["targetEvidence"].update(kind="directory"), "kind"),
        }
        for label, (edit, fragment) in cases.items():
            with self.subTest(label):
                self.refused(self.write(edit=edit), fragment)

    def test_ref_without_matching_symlink_refused(self):
        self.builder.members[EXT_PKG] = [m for m in self.builder.members[EXT_PKG] if m["path"] != LINK]
        self.refused(self.write(), "does not match a symbolic link")

    def test_ref_for_absent_package_refused(self):
        pol = {**self.pol, "external_policy": r2.ExternalRefPolicy(self.profile.profile_id, (
            r2.ExternalApproval("org.example.ghost", LINK, TARGET, r2.TargetEvidence(**EVIDENCE)),))}

        def edit(m):
            m["externalRefs"][0]["packageId"] = "org.example.ghost"
        # delta carries no external link, so the packages parse clean and only the ref check refuses.
        self.builder.members[EXT_PKG] = [m for m in self.builder.members[EXT_PKG] if m["path"] != LINK]
        self.refused(self.write(edit=edit), "does not restore", **pol)

    def link(self, path, target):
        return {"path": path, "kind": "symlink", "uid": 5001, "gid": 100, "mode": 0o777, "linkTarget": target}

    def test_other_links_through_the_external_link_refused(self):
        for target in ("runtime", "runtime/..", "runtime/../..", "./runtime"):
            with self.subTest(target):
                self.builder.members[EXT_PKG] = members_for(EXT_PKG, [self.link("bin/alias", target)])
                self.refused(self.write(), "escapes the package root")

    def test_guard_off_exact_pair(self):
        self.builder.members[EXT_PKG] = members_for(EXT_PKG, [self.link("bin/alias", "runtime")])
        path = self.write()
        self.refused(path, "escapes")
        with mock.patch.object(capsule, "_is_approved_external", lambda links, p, t: links is not None):
            self.assertIsInstance(self.validate(path), r2.Recovery2)

    def test_guard_off_check_external_ref(self):
        path = self.write()
        empty = {**self.pol, "external_policy": r2.ExternalRefPolicy(self.profile.profile_id, ())}
        self.refused(path, "unknown", **empty)
        with mock.patch.object(r2, "check_external_ref"):
            self.assertIsInstance(self.validate(path, **empty), r2.Recovery2)

    def test_entries_below_the_link_refused_in_manifest_and_archive(self):
        child = {"path": f"{LINK}/evil", "kind": "file", "uid": 0, "gid": 0, "mode": 0o644, "data": b"x"}
        self.builder.members[EXT_PKG] = members_for(EXT_PKG, [child])
        self.refused(self.write(), "non-directory parent")  # the entry table: its parent is the link
        # The archive alone: the manifest is clean but the tar has a member under the link.
        self.builder.members[EXT_PKG] = members_for(EXT_PKG)
        info = tarfile.TarInfo(f"{LINK}/evil")
        info.size = 1
        self.builder.payload_override[EXT_PKG] = fx.make_tar(self.builder.members[EXT_PKG], [(info, b"x")])
        self.refused(self.write(), "below a symbolic link")

    def test_hardlinks_and_devices_always_refused(self):
        for kind in (tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.FIFOTYPE):
            with self.subTest(kind=kind):
                info = tarfile.TarInfo("bin/odd")
                info.type = kind
                info.linkname = "bin/app" if kind == tarfile.LNKTYPE else ""
                self.builder.payload_override[EXT_PKG] = fx.make_tar(self.builder.members[EXT_PKG], [info])
                message = self.refused(self.write())
                self.assertTrue("hard link" in message or "special member" in message, message)

    def test_tampered_payload_refused(self):
        self.builder.payload_override[EXT_PKG] = fx.make_tar(self.builder.members[EXT_PKG])
        path = self.write()
        tar = path / "payload" / f"{EXT_PKG}.tar"
        data = bytearray(tar.read_bytes())
        data[-2000] ^= 1
        tar.write_bytes(bytes(data))
        self.refused(path, "payload bytes do not match")


class BootChanges(Base):
    def test_global_cache_patch_refused(self):
        cases = [("bootcache-record:all", "autostart"), ("bootcache-record:org.example.delta", "blob"),
                 ("bootcache-record:org.example.delta", "raw"), ("/opt/usr/dbspace/.boot.cache", "autostart"),
                 ("bootcache:org.example.delta", "autostart"), ("bootcache-record:org.example.delta", "*")]
        for target, field in cases:
            with self.subTest(target=target, field=field):
                def edit(m, t=target, f=field):
                    m["bootChanges"].append(boot_change(t, f, "true", "false"))
                self.refused(self.write(edit=edit))
                with self.assertRaises(CapsuleError):
                    r2.BootFieldPolicy(self.profile.profile_id,
                                       (r2.BootField(target, field, frozenset({"true", "false"})),))

    def test_whole_cache_value_cannot_be_approved(self):
        with self.assertRaises(CapsuleError):
            r2.BootFieldPolicy(self.profile.profile_id, (r2.BootField(
                "bootcache-record:org.example.delta", "autostart", frozenset({"A" * 4096})),))
        with self.assertRaises(CapsuleError):  # unbounded values cannot be expressed: a set is required
            r2.BootFieldPolicy(self.profile.profile_id, (r2.BootField(
                "bootcache-record:org.example.delta", "autostart", None),))

    def test_guard_off_whole_cache(self):
        whole = r2.BootField("bootcache-record:org.example.delta", "blob", frozenset({"AAAA", "BBBB"}))
        with self.assertRaises(CapsuleError):
            r2.BootFieldPolicy(self.profile.profile_id, (whole,))
        with mock.patch.object(r2, "refuse_opaque_whole", lambda t, f: (t, f)):
            policy = r2.BootFieldPolicy(self.profile.profile_id, self.pol["boot_policy"].fields + (whole,))
            path = self.write(edit=lambda m: m["bootChanges"].append(
                boot_change(whole.target, "blob", "AAAA", "BBBB")))
            self.assertEqual(self.validate(path, boot_policy=policy, external_policy=self.pol["external_policy"])
                             .counts["bootChanges"], 4)

    def test_change_rules(self):
        cases = {
            "unapproved-field": (lambda m: m["bootChanges"].append(
                boot_change("service:org.example.echo-daemon", "restart", "always", "never")), "does not approve"),
            "unapproved-value": (lambda m: m["bootChanges"].__setitem__(0, boot_change(
                *BOOT[0][:2], "true", "maybe")), "does not approve"),
            "duplicate-field": (lambda m: m["bootChanges"].append(copy.deepcopy(m["bootChanges"][0])), "duplicates"),
            "no-change": (lambda m: m["bootChanges"].__setitem__(0, boot_change(*BOOT[0][:2], "true", "true")),
                          "no change"),
            "before-digest": (lambda m: m["bootChanges"][0].update(beforeSha256="0" * 64), "digest"),
            "ours-digest": (lambda m: m["bootChanges"][0].update(oursSha256=fx.sha(b"true")), "digest"),
            "protected": (lambda m: m["bootChanges"].append(
                boot_change("service:org.example.security-agent", "enabled", "1", "0")), "protected"),
            "non-string": (lambda m: m["bootChanges"][0].update(before=True), "string"),
        }
        for label, (edit, fragment) in cases.items():
            with self.subTest(label):
                self.refused(self.write(edit=edit), fragment)


class UndoPlanning(Base):
    def plan(self, path, evidence, **pol):
        return r2.plan_undo(path, self.profile, self.facts, evidence, **(pol or self.pol))

    def test_all_ours_gives_exact_field_undo_and_preserves_everything_else(self):
        path, store = self.write(), Store("ours")
        untouched = copy.deepcopy(store.data)
        undo = self.plan(path, store.evidence(self.manifest_sha(path)))
        self.assertEqual([(a.target, a.field, a.require_current, a.write) for a in undo.actions],
                         sorted((t, f, o, b) for t, f, b, o in BOOT))
        self.assertEqual(undo.already_before, ())
        self.assertEqual(undo.external_checks[0].target, TARGET)
        self.assertEqual(undo.manifest_sha256, self.manifest_sha(path))
        self.assertEqual(undo.plan_digest, r2.canonical_digest(undo.body()))
        store.apply(undo)
        for target, field, before, _ in BOOT:
            self.assertEqual(store.data[target][field], before)
            untouched[target][field] = before
        self.assertEqual(store.data, untouched)  # unlisted records and fields byte-identical

    def test_idempotent_inverse(self):
        path, store = self.write(), Store("ours")
        store.apply(self.plan(path, store.evidence(self.manifest_sha(path))))
        again = self.plan(path, store.evidence(self.manifest_sha(path)))
        self.assertEqual(again.actions, ())
        self.assertEqual(len(again.already_before), len(BOOT))
        before_writes = store.writes
        store.apply(again)
        self.assertEqual(store.writes, before_writes)

    def test_mixed_before_is_noop(self):
        path, store = self.write(), Store("ours")
        store.data[BOOT[2][0]][BOOT[2][1]] = BOOT[2][2]
        undo = self.plan(path, store.evidence(self.manifest_sha(path)))
        self.assertEqual(len(undo.actions), 2)
        self.assertEqual(undo.already_before, ((BOOT[2][0], BOOT[2][1]),))

    def test_stale_three_way_refuses_whole_plan_with_no_writes(self):
        path, store = self.write(), Store("ours")
        store.data[BOOT[1][0]][BOOT[1][1]] = "normal-ish"  # someone else changed it
        snapshot = copy.deepcopy(store.data)
        with self.assertRaises(CapsuleError) as ctx:
            store.apply(self.plan(path, store.evidence(self.manifest_sha(path))))
        self.assertIn("refusing the whole plan", str(ctx.exception))
        self.assertEqual(store.data, snapshot)
        self.assertEqual(store.writes, 0)

    def test_guard_off_three_way(self):
        path, store = self.write(), Store("ours")
        store.data[BOOT[1][0]][BOOT[1][1]] = "normal-ish"
        with mock.patch.object(r2, "classify_current", lambda change, current: "undo"):
            undo = self.plan(path, store.evidence(self.manifest_sha(path)))
        self.assertEqual(len(undo.actions), 3)  # it would overwrite a value that is not ours

    def test_evidence_rules(self):
        path, store = self.write(), Store("ours")
        sha = self.manifest_sha(path)
        good = json.loads(store.evidence(sha))
        records = good["bootRecords"]
        cases = {
            "wrong-capsule": store.evidence("0" * 64),
            "wrong-device": store.evidence(sha, deviceIdSha256=device_id_sha256("SYNTHETIC-OTHER")),
            "wrong-profile": store.evidence(sha, profileId="other"),
            "missing-field": store.evidence(sha, bootRecords=records[:-1]),
            "extra-field": store.evidence(sha, bootRecords=records + [
                {"target": "service:org.example.unrelated", "field": "enabled", "value": "1",
                 "valueSha256": fx.sha(b"1")}]),
            "duplicate-field": store.evidence(sha, bootRecords=records + [records[0]]),
            "value-digest": store.evidence(sha, bootRecords=[{**records[0], "valueSha256": "0" * 64}] + records[1:]),
            "whole-cache": store.evidence(sha, bootRecords=records + [
                {"target": "bootcache-record:org.example.delta", "field": "blob", "value": "A",
                 "valueSha256": fx.sha(b"A")}]),
            "missing-external": store.evidence(sha, targets=[]),
            "stale-external": store.evidence(sha, targets=[{"target": TARGET, "targetEvidence": {
                **EVIDENCE, "sha256": "2" * 64}}]),
            "duplicate-json": store.evidence(sha)[:-1] + b', "schema": "tvkit-recovery-evidence/1"}',
            "unknown-key": store.evidence(sha, allow=True),
        }
        for label, evidence in cases.items():
            with self.subTest(label), self.assertRaises(CapsuleError):
                self.plan(path, evidence)
        with self.assertRaises(CapsuleError):  # a dict cannot show duplicates
            self.plan(path, good)

    def test_guard_off_evidence_binding(self):
        path, store = self.write(), Store("ours")
        wrong = store.evidence("0" * 64)
        with self.assertRaises(CapsuleError):
            self.plan(path, wrong)
        with mock.patch.object(r2, "_bind_evidence"):
            self.assertEqual(len(self.plan(path, wrong).actions), 3)

    def test_plan_needs_live_facts_and_revalidates(self):
        path, store = self.write(), Store("ours")
        evidence = store.evidence(self.manifest_sha(path))
        with self.assertRaises(CapsuleError):
            r2.plan_undo(path, self.profile, None, evidence, **self.pol)
        with self.assertRaises(CapsuleError):
            r2.plan_undo(path, self.profile, {**self.facts, "region": "SYNTHETIC-OTHER"}, evidence, **self.pol)
        (path / "xattrs" / f"{PLAIN_PKG}.json").write_bytes(b"{}")
        with self.assertRaises(CapsuleError):
            r2.plan_undo(path, self.profile, self.facts, evidence, **self.pol)

    def test_verify_undo_plan(self):
        path, store = self.write(), Store("ours")
        evidence = store.evidence(self.manifest_sha(path))
        undo = self.plan(path, evidence)
        self.assertEqual(r2.verify_undo_plan(undo, path, self.profile, self.facts, evidence, **self.pol), undo)
        swapped = r2.UndoAction(undo.actions[0].target, undo.actions[0].field, undo.actions[0].require_current,
                                undo.actions[0].require_current_sha256, "evil", fx.sha(b"evil"))
        forged = r2.UndoPlan(**{**undo.__dict__, "actions": (swapped,) + undo.actions[1:]})
        resealed = r2.UndoPlan(**{**forged.__dict__, "plan_digest": r2.canonical_digest(forged.body())})
        other_policy = {**self.pol, "boot_policy": r2.BootFieldPolicy(self.profile.profile_id, (
            *self.pol["boot_policy"].fields,
            r2.BootField("service:org.example.unrelated", "enabled", frozenset({"1", "0"}))))}
        for label, candidate, pol in (("digest", forged, self.pol), ("resealed", resealed, self.pol),
                                      ("policy", undo, other_policy)):
            with self.subTest(label), self.assertRaises(CapsuleError):
                r2.verify_undo_plan(candidate, path, self.profile, self.facts, evidence, **pol)
        with self.assertRaises(Exception):
            undo.actions[0].write = "x"  # frozen

    def test_module_has_no_mutation_or_extraction_functions(self):
        banned = re.compile(r"extract|apply|remove|unlink|delete|install|mkdir|write|transport", re.I)
        names = [n for n, obj in vars(r2).items() if callable(obj) and banned.search(n)]
        self.assertEqual(names, [])
        source = inspect.getsource(r2)
        for token in ("extractall", "open(", "os.remove", "shutil", "subprocess", "TRANSPORTS"):
            self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()

"""factsjoin: join a ProductInfo report to native runtime into profile/2 facts.

Synthetic observations only. The positive path is a full join -> profile/2
match_device -> recovery/2 bind_device against a capsule built in Python. Every
negative (drift, tamper, mismatch, missing, mixed-source, legacy identity) and
the both-ways guard-off controls show the exact matchers do the work and that no
install/remove/restore action is ever enabled.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_public_toolkit_fixtures as fx  # noqa: E402
import test_public_toolkit_profile_v2 as pv2  # noqa: E402
import test_public_toolkit_recovery2 as r2t  # noqa: E402
from tvkit import factsjoin as fj  # noqa: E402
from tvkit import recovery2 as r2  # noqa: E402
from tvkit.capsule import CapsuleError  # noqa: E402
from tvkit.profile import ProfileError, device_id_sha256, match_device, parse_device_facts, parse_profile  # noqa: E402

DUID_DIGEST = hashlib.sha256(b"synthetic-duid-not-a-real-id").hexdigest()


def product_info_dict():
    """A complete, well-formed quiettv-device-facts/1 report matching the builtin policy."""
    def field(value, opaque=False):
        rec = {"api": "webapis.productinfo.x()", "value": value, "status": "observed"}
        if opaque:
            rec["opaque"] = True
        return rec
    return {
        "schema": fj.PRODUCT_INFO_SCHEMA,
        "status": "complete",
        "source": fj.PRODUCT_INFO_SOURCE,
        "apiReference": fj.PRODUCT_INFO_API_REFERENCE,
        "fields": {
            "firmware": field("SYNTHETIC-BUILD-0000.1"),
            "realModel": field("SYNTHETIC-QTV0001"),
            "model": field("SYN_X22U_MODEL"),
            "modelCode": field("SYN_X22U_CODE"),
            "serviceCountry": field("SR"),
            "localSet": field("SR", opaque=True),
            "platformVersion": field("9.0"),
        },
        "identity": {"scheme": fj.FACTS_IDENTITY_SCHEME, "api": "webapis.productinfo.getDuid()",
                     "digest": DUID_DIGEST, "status": "observed"},
    }


def native_doc():
    """A valid quiettv-inspection/1 document whose observedRuntime matches the policy."""
    return {
        "schema": "quiettv-inspection/1", "toolVersion": "1.0.0", "status": "ok",
        "registryDatabase": "/opt/dbspace/.pkgmgr_parser.db",
        "registryRecords": [
            {"Id": "org.example.alpha", "Name": "Alpha", "Installed": True, "Disabled": False,
             "Launchable": True, "PackageId": "pkgalpha", "Version": "1",
             "InstallRoot": "/opt/usr/apps/pkgalpha", "Reason": "installed"}],
        "observedRuntime": {
            "tizenRelease": {"path": "/etc/tizen-release",
                             "lines": ["Tizen9/TV 9.0.0 (arm)", "VERSION=9.0.0",
                                       "BUILD_ID=T-SYNTHETIC-0000-REL-200001010000"]},
            "tizenVersion": "9.0.0", "machine": "armv7l", "model": None, "firmware": None, "region": None},
        "problems": [],
    }


def report_bytes(mutate=None):
    data = product_info_dict()
    if mutate:
        mutate(data)
    return json.dumps(data).encode("utf-8")


def frame_bytes(mutate=None):
    doc = native_doc()
    if mutate:
        mutate(doc)
    frame = fj._frame_module()
    return frame.encode_frame(json.dumps(doc, separators=(",", ":")).encode("utf-8"))


def token():
    return fj.identity_token(fj.FACTS_IDENTITY_SCHEME, DUID_DIGEST)


class JoinBase(unittest.TestCase):
    def setUp(self):
        self.profile = parse_profile(pv2.load("profile-v2.json"))
        self.policy = fj.builtin_join_policies()[self.profile.profile_id]
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def join(self):
        return fj.join(report_bytes(), frame_bytes(), policy=self.policy, profile=self.profile)


class PositiveJoin(JoinBase):
    def test_full_join_then_match_then_bind(self):
        receipt = self.join()
        facts = receipt.device_facts()
        # The 8 profile/2 facts, deviceId a scheme-tagged opaque token (never a raw DUID).
        self.assertEqual(facts, {
            "model": "SYNTHETIC-QTV0001", "firmware": "0000.1", "arch": "armv7l", "osName": "Tizen",
            "osVersion": "9", "firmwareBuild": "SYNTHETIC-BUILD-0000.1", "region": "SYNTHETIC-REGION",
            "deviceId": token()})
        self.assertEqual(parse_device_facts(facts), facts)  # strict facts parser accepts it
        match_device(self.profile, facts)                   # the real exact matcher accepts it
        fj.verify_receipt(receipt)
        self.assertEqual(receipt.identity_scheme, fj.FACTS_IDENTITY_SCHEME)

        capsule = r2t.Builder(self.profile, facts).write(
            self.tmp / "cap", edit=lambda m: m["device"].__setitem__(
                "deviceIdSha256", device_id_sha256(token())))
        result = fj.bind_to_recovery2(receipt, capsule, self.profile, **r2t.policies())
        self.assertTrue(result.device_bound)

    def test_firmware_build_source_and_other_observations_preserved(self):
        receipt = self.join()
        self.assertEqual(receipt.body()["fieldSources"]["firmwareBuild"], "observed:productInfo.firmware")
        other = receipt.other_observations
        # The native BUILD_ID is preserved as a distinct observation, never used as firmwareBuild.
        self.assertIn("BUILD_ID=T-SYNTHETIC-0000-REL-200001010000", other["nativeTizenReleaseLines"])
        self.assertEqual(receipt.facts["firmwareBuild"], "SYNTHETIC-BUILD-0000.1")
        self.assertNotIn("REL-200001010000", receipt.facts["firmwareBuild"])
        self.assertEqual(other["productInfoPlatformVersion"], "9.0")  # kept distinct from osVersion "9"

    def test_receipt_binds_both_input_hashes_policy_and_profile(self):
        receipt = self.join()
        self.assertEqual(receipt.product_info_sha256, hashlib.sha256(report_bytes()).hexdigest())
        self.assertEqual(receipt.inspection_sha256, hashlib.sha256(frame_bytes()).hexdigest())
        self.assertEqual(receipt.policy_digest, self.policy.digest)
        self.assertEqual(receipt.profile_digest, r2.profile_digest(self.profile))


class NoActionSelfEnables(JoinBase):
    def test_preflight_display_enables_nothing_and_hides_identity_token(self):
        preflight = fj.preflight_display(self.join())
        self.assertEqual(preflight["actionsEnabled"], {})
        self.assertEqual(preflight["capabilities"], {})
        self.assertNotIn("deviceId", preflight["fields"])
        self.assertNotIn(token(), json.dumps(preflight))
        self.assertNotIn(DUID_DIGEST, json.dumps(preflight))

    def test_bound_result_cannot_feed_a_capsule1_apply_plan(self):
        receipt = self.join()
        capsule = r2t.Builder(self.profile, receipt.device_facts()).write(
            self.tmp / "cap", edit=lambda m: m["device"].__setitem__(
                "deviceIdSha256", device_id_sha256(token())))
        result = fj.bind_to_recovery2(receipt, capsule, self.profile, **r2t.policies())
        # recovery/2 refuses to expose a capsule/1 apply surface: no install/remove/restore self-enables.
        with self.assertRaises(CapsuleError):
            _ = result.payload_verified


class Negatives(JoinBase):
    def test_drift_refused_only_by_policy_gate(self):
        # platformVersion feeds the INTERPRETED osVersion, so match_device cannot see its drift;
        # the policy-equality gate is the only thing that catches it. Both ways proves it.
        def drift(d):
            d["fields"]["platformVersion"]["value"] = "9.1"
        with self.assertRaises(fj.JoinError):
            fj.join(report_bytes(drift), frame_bytes(), policy=self.policy, profile=self.profile)
        with mock.patch.object(fj, "_require_observations_match_policy"):
            receipt = fj.join(report_bytes(drift), frame_bytes(), policy=self.policy, profile=self.profile)
            self.assertEqual(receipt.facts["osVersion"], "9")  # drift slips through with the gate off

    def test_mixed_source_native_conflict_refused(self):
        def conflict(doc):
            doc["observedRuntime"]["tizenVersion"] = "9.0.1"
        with self.assertRaises(fj.JoinError):
            fj.join(report_bytes(), frame_bytes(conflict), policy=self.policy, profile=self.profile)

    def test_tamper_placeholder_control_and_digest_refused(self):
        for mutate in (
            lambda d: d["fields"]["realModel"].__setitem__("value", "Unknown"),       # placeholder
            lambda d: d["fields"]["realModel"].__setitem__("value", "bad\x07model"),   # control char
            lambda d: d["fields"]["realModel"].__setitem__("value", "x" * 5000),       # oversize
            lambda d: d["fields"]["realModel"].__setitem__("value", " padded"),        # non-concrete
            lambda d: d["identity"].__setitem__("digest", "nothex"),                   # malformed digest
            lambda d: d.__setitem__("source", "network-api"),                          # source mismatch
            lambda d: d.__setitem__("status", "incomplete"),                           # incomplete
            lambda d: d["fields"].__setitem__("extra", {"api": "x", "status": "observed", "value": "y"}),
            lambda d: d["fields"]["realModel"].pop("value"),                           # required not observed
        ):
            with self.subTest(mutate=mutate), self.assertRaises(fj.JoinError):
                fj.join(report_bytes(mutate), frame_bytes(), policy=self.policy, profile=self.profile)

    def test_tampered_receipt_refused_before_match(self):
        receipt = self.join()
        tampered = fj.JoinReceipt(**{**receipt.__dict__,
                                     "facts": {**dict(receipt.facts), "region": "ELSEWHERE"}})
        with self.assertRaises(fj.JoinError):
            fj.verify_receipt(tampered)
        with self.assertRaises(fj.JoinError):
            fj.preflight_display(tampered)

    def test_missing_native_runtime_refused(self):
        with self.assertRaises(fj.JoinError):
            fj.join(report_bytes(), frame_bytes(lambda doc: doc.__setitem__("observedRuntime", None)),
                    policy=self.policy, profile=self.profile)

    def test_profile_mismatch_refused_by_match_device(self):
        wrong = copy.deepcopy(pv2.load("profile-v2.json"))
        wrong["match"]["region"] = "SYNTHETIC-DIFFERENT"
        wrong_profile = parse_profile(wrong)
        # The policy is written for the real synthetic profile id, so the id check fires first;
        # align the ids to prove match_device itself refuses the wrong region.
        aligned = fj.JoinPolicy(wrong_profile.profile_id, self.policy.identity_scheme,
                                dict(self.policy.product_info), dict(self.policy.native),
                                dict(self.policy.interpreted))
        with self.assertRaises(ProfileError):
            fj.join(report_bytes(), frame_bytes(), policy=aligned, profile=wrong_profile)

    def test_v1_profile_refused(self):
        with self.assertRaises(fj.JoinError):
            fj.join(report_bytes(), frame_bytes(), policy=self.policy, profile=fx.profile())


class LegacyIdentityNeverRebound(JoinBase):
    def _capsule(self, device_id_hash):
        return r2t.Builder(self.profile, self.join().device_facts()).write(
            self.tmp / f"cap-{device_id_hash[:8]}",
            edit=lambda m: m["device"].__setitem__("deviceIdSha256", device_id_hash))

    def test_legacy_rawid_capsule_refused_by_bind_device(self):
        receipt = self.join()
        legacy = self._capsule(device_id_sha256("SYNTHETIC-RAW-LEGACY-ID"))  # old tvkit-device-id/1 raw-id domain
        with self.assertRaises(CapsuleError):
            fj.bind_to_recovery2(receipt, legacy, self.profile, **r2t.policies())
        # Both ways: with the real matcher (bind_device) stubbed out, the legacy capsule would bind.
        # This proves the refusal is bind_device's, not a parallel permissive matcher in factsjoin.
        with mock.patch.object(r2, "bind_device"):
            result = fj.bind_to_recovery2(receipt, legacy, self.profile, **r2t.policies())
            self.assertTrue(result.device_bound)  # only the stubbed matcher lets the legacy capsule through

    def test_bind_refused_without_scheme_tagged_identity(self):
        none_policy = fj.JoinPolicy(self.profile.profile_id, fj.IDENTITY_SCHEME_NONE,
                                    dict(self.policy.product_info), dict(self.policy.native),
                                    dict(self.policy.interpreted))
        receipt = fj.join(report_bytes(), frame_bytes(), policy=none_policy, profile=self.profile)
        self.assertNotIn("deviceId", receipt.facts)
        capsule = self._capsule(device_id_sha256(token()))
        with self.assertRaises(fj.JoinError):
            fj.bind_to_recovery2(receipt, capsule, self.profile, **r2t.policies())


if __name__ == "__main__":
    unittest.main()

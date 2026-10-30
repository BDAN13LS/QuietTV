"""factsjoin: join a ProductInfo report to native runtime into profile/2 facts.

Synthetic observations only. The positive path is a full join -> profile/2
match_device -> recovery/2 bind_device against a capsule built in Python.

Verification is re-derivation from the ORIGINAL observations, the trusted
policy and the profile, not a self-checksum: the forged-rehash regressions
(legacy bind and preflight) prove a receipt that recomputed its own digest over
altered facts still fails, and the both-ways controls show re-derivation is what
catches it. Every negative (drift, tamper, mismatch, missing, mixed-source,
failed native run, legacy identity, unapproved API label, placeholder mapping)
and the deep-immutability checks are here too.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import traceback
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
LEGACY_RAW_ID = "SYNTHETIC-RAW-LEGACY-ID"


def product_info_dict():
    """A complete quiettv-device-facts/1 report matching the builtin policy, with the
    REAL per-field Product API getter labels (values stay synthetic)."""
    def field(key, value, opaque=False):
        rec = {"api": fj._FIELD_API[key], "value": value, "status": "observed"}
        if opaque:
            rec["opaque"] = True
        return rec
    return {
        "schema": fj.PRODUCT_INFO_SCHEMA,
        "status": "complete",
        "source": fj.PRODUCT_INFO_SOURCE,
        "apiReference": fj.PRODUCT_INFO_API_REFERENCE,
        "fields": {
            "firmware": field("firmware", "SYNTHETIC-BUILD-0000.1"),
            "realModel": field("realModel", "SYNTHETIC-QTV0001"),
            "model": field("model", "SYN_X22U_MODEL"),
            "modelCode": field("modelCode", "SYN_X22U_CODE"),
            "serviceCountry": field("serviceCountry", "SR"),
            "localSet": field("localSet", "SR", opaque=True),
            "platformVersion": field("platformVersion", "9.0"),
        },
        "identity": {"scheme": fj.FACTS_IDENTITY_SCHEME, "api": fj._IDENTITY_API,
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


def _encode(doc):
    return fj._frame_module().encode_frame(json.dumps(doc, separators=(",", ":")).encode("utf-8"))


def report_bytes(mutate=None):
    data = product_info_dict()
    if mutate:
        mutate(data)
    return json.dumps(data).encode("utf-8")


def frame_bytes(mutate=None):
    doc = native_doc()
    if mutate:
        mutate(doc)
    return _encode(doc)


def failed_frame():
    return _encode({"schema": "quiettv-inspection/1", "toolVersion": "1.0.0", "status": "failed",
                    "registryDatabase": "/opt/dbspace/.pkgmgr_parser.db", "registryRecords": None,
                    "observedRuntime": None, "problems": [{"code": "query-timeout", "subject": "q", "fatal": True}]})


def token():
    return fj.identity_token(fj.FACTS_IDENTITY_SCHEME, DUID_DIGEST)


def forge(receipt, **fact_overrides):
    """A self-consistent forged receipt: altered facts, its OWN digest recomputed."""
    facts = {**dict(receipt.facts), **fact_overrides}
    draft = fj.JoinReceipt(**{**receipt.__dict__, "facts": facts, "receipt_digest": ""})
    return fj.JoinReceipt(**{**draft.__dict__, "receipt_digest": fj._r2.canonical_digest(draft.body())})


class JoinBase(unittest.TestCase):
    def setUp(self):
        self.profile = parse_profile(pv2.load("profile-v2.json"))
        self.policy = fj.builtin_join_policies()[self.profile.profile_id]
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def ctx(self):
        return dict(product_info=report_bytes(), inspection_frame=frame_bytes(), policy=self.policy)

    def join(self):
        return fj.join(report_bytes(), frame_bytes(), policy=self.policy, profile=self.profile)

    def capsule(self, device_id_hash, name="cap"):
        return r2t.Builder(self.profile, self.join().device_facts()).write(
            self.tmp / name, edit=lambda m: m["device"].__setitem__("deviceIdSha256", device_id_hash))


class PositiveJoin(JoinBase):
    def test_full_join_then_match_then_bind(self):
        receipt = self.join()
        facts = receipt.device_facts()
        self.assertEqual(facts, {
            "model": "SYNTHETIC-QTV0001", "firmware": "0000.1", "arch": "armv7l", "osName": "Tizen",
            "osVersion": "9", "firmwareBuild": "SYNTHETIC-BUILD-0000.1", "region": "SYNTHETIC-REGION",
            "deviceId": token()})
        self.assertEqual(parse_device_facts(facts), facts)  # strict facts parser accepts it
        match_device(self.profile, facts)                   # the real exact matcher accepts it

        capsule = self.capsule(device_id_sha256(token()))
        result = fj.bind_to_recovery2(receipt, capsule, self.profile, **self.ctx(), **r2t.policies())
        self.assertTrue(result.device_bound)

    def test_verify_receipt_rederives_and_returns_a_fresh_trusted_receipt(self):
        receipt = self.join()
        fresh = fj.verify_receipt(receipt, report_bytes(), frame_bytes(), policy=self.policy, profile=self.profile)
        self.assertEqual(fresh.body(), receipt.body())
        self.assertEqual(fresh.receipt_digest, receipt.receipt_digest)

    def test_firmware_build_source_and_other_observations_preserved(self):
        receipt = self.join()
        self.assertEqual(receipt.body()["fieldSources"]["firmwareBuild"], "observed:productInfo.firmware")
        other = receipt.other_observations
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


class ForgedReceiptRejected(JoinBase):
    def test_forged_rehash_legacy_bind_refused(self):
        receipt = self.join()
        forged = forge(receipt, deviceId=LEGACY_RAW_ID)       # swap identity to a raw id, rehash the receipt
        self.assertEqual(forged.receipt_digest, fj._r2.canonical_digest(forged.body()))  # self-consistent
        legacy = self.capsule(device_id_sha256(LEGACY_RAW_ID), name="legacy")
        with self.assertRaises(fj.JoinError):                 # re-derivation catches the forgery
            fj.bind_to_recovery2(forged, legacy, self.profile, **self.ctx(), **r2t.policies())
        # Both ways: a self-checksum-only verifier (stubbed) would let the forged legacy bind succeed.
        with mock.patch.object(fj, "verify_receipt", return_value=forged):
            result = fj.bind_to_recovery2(forged, legacy, self.profile, **self.ctx(), **r2t.policies())
            self.assertTrue(result.device_bound)

    def test_forged_rehash_preflight_refused(self):
        receipt = self.join()
        forged = forge(receipt, region="ELSEWHERE")
        with self.assertRaises(fj.JoinError):
            fj.preflight_display(forged, report_bytes(), frame_bytes(), policy=self.policy, profile=self.profile)
        with mock.patch.object(fj, "verify_receipt", return_value=forged):
            shown = fj.preflight_display(forged, report_bytes(), frame_bytes(),
                                         policy=self.policy, profile=self.profile)
            self.assertEqual(shown["fields"]["region"], "ELSEWHERE")  # only without real re-derivation

    def test_verify_refuses_wrong_observations_policy_or_profile(self):
        receipt = self.join()
        with self.assertRaises(fj.JoinError):  # observations do not produce this receipt
            fj.verify_receipt(receipt, report_bytes(lambda d: d["fields"]["realModel"].__setitem__("value", "OTHER")),
                              frame_bytes(), policy=self.policy, profile=self.profile)


class NoActionSelfEnables(JoinBase):
    def test_preflight_display_enables_nothing_and_hides_identity_token(self):
        preflight = fj.preflight_display(self.join(), report_bytes(), frame_bytes(),
                                         policy=self.policy, profile=self.profile)
        self.assertEqual(preflight["actionsEnabled"], {})
        self.assertEqual(preflight["capabilities"], {})
        self.assertNotIn("deviceId", preflight["fields"])
        self.assertNotIn(token(), json.dumps(preflight))
        self.assertNotIn(DUID_DIGEST, json.dumps(preflight))

    def test_bound_result_cannot_feed_a_capsule1_apply_plan(self):
        receipt = self.join()
        result = fj.bind_to_recovery2(receipt, self.capsule(device_id_sha256(token())), self.profile,
                                      **self.ctx(), **r2t.policies())
        with self.assertRaises(CapsuleError):
            _ = result.payload_verified


class Immutability(JoinBase):
    def test_receipt_copies_do_not_mutate_it(self):
        receipt = self.join()
        facts = receipt.device_facts()
        facts["region"] = "X"
        self.assertEqual(receipt.facts["region"], "SYNTHETIC-REGION")
        self.assertIsInstance(receipt.other_observations["nativeTizenReleaseLines"], tuple)
        body = receipt.body()
        body["otherObservations"]["nativeTizenReleaseLines"].append("x")
        body["facts"]["region"] = "X"
        self.assertNotIn("x", receipt.other_observations["nativeTizenReleaseLines"])
        self.assertEqual(receipt.facts["region"], "SYNTHETIC-REGION")

    def test_policy_constructor_freezes_lines(self):
        lines = ["a-line"]
        policy = fj.JoinPolicy("SYNTHETIC-P", fj.FACTS_IDENTITY_SCHEME,
                               {k: "val" for k in fj._PRODUCT_INFO_FIELD_KEYS},
                               {"tizenReleaseLines": lines, "tizenVersion": "9.0.0", "machine": "armv7l"},
                               {f: "val" for f in fj._INTERPRETED_FIELDS})
        lines.append("mutated-after-construction")
        self.assertEqual(policy.native["tizenReleaseLines"], ("a-line",))


class PolicyValidation(JoinBase):
    def test_placeholder_or_nonconcrete_interpreted_value_refused(self):
        for bad in ({"region": "Unknown"}, {"osName": " Tizen"}, {"osVersion": ""}, {"firmware": "a\tb"}):
            interpreted = {**dict(self.policy.interpreted), **bad}
            with self.subTest(bad=bad), self.assertRaises(fj.JoinError):
                fj.JoinPolicy(self.policy.profile_id, fj.FACTS_IDENTITY_SCHEME,
                              dict(self.policy.product_info), dict(self.policy.native), interpreted)

    def test_unknown_interpreted_region_cannot_match_even_if_profile_has_unknown(self):
        # A profile whose region is literally "Unknown" would match "Unknown" facts; the policy
        # layer must refuse an "Unknown" interpretation before it can ever be produced.
        with self.assertRaises(fj.JoinError):
            fj.JoinPolicy(self.policy.profile_id, fj.FACTS_IDENTITY_SCHEME, dict(self.policy.product_info),
                          dict(self.policy.native), {**dict(self.policy.interpreted), "region": "Unknown"})


class Negatives(JoinBase):
    def test_unapproved_api_label_refused(self):
        for mutate in (lambda d: d["fields"]["firmware"].__setitem__("api", "webapis.productinfo.unapprovedSource()"),
                       lambda d: d["identity"].__setitem__("api", "webapis.productinfo.somethingElse()"),
                       lambda d: d["fields"]["localSet"].__setitem__("opaque", "yes")):
            with self.subTest(mutate=mutate), self.assertRaises(fj.JoinError):
                fj.join(report_bytes(mutate), frame_bytes(), policy=self.policy, profile=self.profile)

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
        with self.assertRaises(fj.JoinError):
            fj.join(report_bytes(), frame_bytes(lambda doc: doc["observedRuntime"].__setitem__("tizenVersion", "9.0.1")),
                    policy=self.policy, profile=self.profile)

    def test_failed_native_run_refused(self):
        with self.assertRaises(fj.JoinError):
            fj.join(report_bytes(), failed_frame(), policy=self.policy, profile=self.profile)

    def test_missing_native_runtime_refused(self):
        with self.assertRaises(fj.JoinError):
            fj.join(report_bytes(), frame_bytes(lambda doc: doc.__setitem__("observedRuntime", None)),
                    policy=self.policy, profile=self.profile)

    def test_tamper_placeholder_control_and_digest_refused(self):
        for mutate in (
            lambda d: d["fields"]["realModel"].__setitem__("value", "Unknown"),       # placeholder
            lambda d: d["fields"]["realModel"].__setitem__("value", "bad\x07model"),   # control char
            lambda d: d["fields"]["realModel"].__setitem__("value", "x" * 5000),       # oversize
            lambda d: d["fields"]["realModel"].__setitem__("value", " padded"),        # non-concrete
            lambda d: d["fields"]["realModel"].__setitem__("status", "weird-status"),  # unrecognised status
            lambda d: d["identity"].__setitem__("digest", "nothex"),                   # malformed digest
            lambda d: d["identity"].__setitem__("status", "crypto-unavailable"),       # identity not observed
            lambda d: d.__setitem__("source", "network-api"),                          # source mismatch
            lambda d: d.__setitem__("status", "incomplete"),                           # incomplete
            lambda d: d["fields"].__setitem__("extra", {"api": "x", "status": "observed", "value": "y"}),
            lambda d: d["fields"]["realModel"].pop("value"),                           # required not observed
        ):
            with self.subTest(mutate=mutate), self.assertRaises(fj.JoinError):
                fj.join(report_bytes(mutate), frame_bytes(), policy=self.policy, profile=self.profile)

    def test_exceptions_do_not_echo_received_values(self):
        try:
            fj.join(report_bytes(lambda d: d["fields"]["realModel"].__setitem__("status", "SEKRIT-STATUS")),
                    frame_bytes(), policy=self.policy, profile=self.profile)
            self.fail("expected refusal")
        except fj.JoinError as exc:
            self.assertNotIn("SEKRIT-STATUS", str(exc))

    def test_strict_json_refusal_never_leaks_a_user_controlled_key(self):
        # strictjson's own unknown/duplicate-key messages echo the property name; the
        # factsjoin boundary must sanitize that away (str AND the traceback).
        marker = "SEKRIT-PROPERTY"
        valid = json.dumps(product_info_dict())
        unknown_key = valid[:-1] + f', "{marker}": "x"}}'          # an unexpected top-level property
        duplicate_key = '{"schema": "a", "schema": "b", "' + marker + '": "x"}'  # a duplicate key
        for label, raw in (("unknown", unknown_key), ("duplicate", duplicate_key)):
            with self.subTest(label=label):
                try:
                    fj.parse_product_info(raw.encode("utf-8"))
                    self.fail("expected refusal")
                except fj.JoinError as exc:
                    self.assertNotIn(marker, str(exc))
                    self.assertNotIn(marker, "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
        # The valid report still parses.
        self.assertEqual(fj.parse_product_info(valid.encode("utf-8")).real_model, "SYNTHETIC-QTV0001")

    def test_strict_json_policy_refusal_never_leaks_a_user_controlled_key(self):
        marker = "SEKRIT-POLICY-KEY"
        raw = '{"schema": "x", "schema": "y", "' + marker + '": "z"}'
        try:
            fj.join_policy_from_json(raw.encode("utf-8"))
            self.fail("expected refusal")
        except fj.JoinError as exc:
            self.assertNotIn(marker, str(exc))
            self.assertNotIn(marker, "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))

    def test_profile_mismatch_refused_by_match_device(self):
        wrong = copy.deepcopy(pv2.load("profile-v2.json"))
        wrong["match"]["region"] = "SYNTHETIC-DIFFERENT"
        wrong_profile = parse_profile(wrong)
        aligned = fj.JoinPolicy(wrong_profile.profile_id, self.policy.identity_scheme,
                                dict(self.policy.product_info), dict(self.policy.native),
                                dict(self.policy.interpreted))
        with self.assertRaises(ProfileError):
            fj.join(report_bytes(), frame_bytes(), policy=aligned, profile=wrong_profile)

    def test_v1_profile_refused(self):
        with self.assertRaises(fj.JoinError):
            fj.join(report_bytes(), frame_bytes(), policy=self.policy, profile=fx.profile())


class LegacyIdentityNeverRebound(JoinBase):
    def test_legacy_rawid_capsule_refused_by_bind_device(self):
        receipt = self.join()
        legacy = self.capsule(device_id_sha256(LEGACY_RAW_ID), name="legacy")  # old tvkit-device-id/1 raw-id domain
        with self.assertRaises(CapsuleError):
            fj.bind_to_recovery2(receipt, legacy, self.profile, **self.ctx(), **r2t.policies())
        # Both ways: with the real matcher (bind_device) stubbed out, the legacy capsule would bind.
        # This proves the refusal is bind_device's, not a parallel permissive matcher in factsjoin.
        with mock.patch.object(r2, "bind_device"):
            result = fj.bind_to_recovery2(receipt, legacy, self.profile, **self.ctx(), **r2t.policies())
            self.assertTrue(result.device_bound)


if __name__ == "__main__":
    unittest.main()

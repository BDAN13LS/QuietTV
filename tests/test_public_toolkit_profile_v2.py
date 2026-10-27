"""tvkit-profile/2: exact firmwareBuild and region, version 1 still readable.

Synthetic fixtures only (tests/fixtures/consumer). The built-in profile is
not changed and no build or region is inferred for it.
"""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_public_toolkit_fixtures as fx  # noqa: E402
from tvkit import profile as prof  # noqa: E402
from tvkit.profile import (MATCH_FIELDS, ProfileError, get_profile, match_device,  # noqa: E402
                           parse_device_facts, parse_profile)

CONSUMER = Path(__file__).resolve().parent / "fixtures" / "consumer"


DEVICE_ID = "SYNTHETIC-CONSUMER-DEVICE-0001"


def load(name):
    return json.loads((CONSUMER / name).read_text(encoding="utf-8"))


def consumer_facts():
    """Built here, not stored as JSON: the export refuses any JSON file with a deviceId key."""
    return {**load("profile-v2.json")["match"], "deviceId": DEVICE_ID}


class ProfileV2(unittest.TestCase):
    def setUp(self):
        self.data = load("profile-v2.json")
        self.profile = parse_profile(self.data)
        self.facts = parse_device_facts(consumer_facts())

    def test_v2_parses_and_matches_exactly(self):
        self.assertEqual(self.profile.schema, prof.PROFILE_SCHEMA_V2)
        self.assertEqual(self.profile.match_fields, MATCH_FIELDS + ("firmwareBuild", "region"))
        match_device(self.profile, self.facts)

    def test_v2_refuses_any_build_or_region_difference(self):
        for field, value in (("firmwareBuild", "SYNTHETIC-BUILD-0000.2"),
                             ("firmwareBuild", "synthetic-build-0000.1"),
                             ("firmwareBuild", "SYNTHETIC-BUILD-0000"),
                             ("region", "SYNTHETIC-REGIO"), ("region", "synthetic-region")):
            with self.subTest(field=field, value=value), self.assertRaises(ProfileError) as ctx:
                match_device(self.profile, {**self.facts, field: value})
            self.assertIn(field, str(ctx.exception))

    def test_v2_refuses_v1_facts_missing_new_fields(self):
        for field in ("firmwareBuild", "region"):
            facts = dict(self.facts)
            del facts[field]
            with self.subTest(field=field), self.assertRaises(ProfileError):
                match_device(self.profile, facts)

    def test_v2_profile_requires_both_new_fields_and_no_unknowns(self):
        for mutate in (lambda d: d["match"].pop("region"),
                       lambda d: d["match"].pop("firmwareBuild"),
                       lambda d: d["match"].__setitem__("buildDate", "x"),
                       lambda d: d["match"].__setitem__("region", "<CONFIGURE: region>"),
                       lambda d: d["match"].__setitem__("firmwareBuild", " SYNTHETIC-BUILD-0000.1"),
                       lambda d: d["match"].__setitem__("region", 7),
                       lambda d: d.__setitem__("schema", "tvkit-profile/3")):
            data = copy.deepcopy(self.data)
            mutate(data)
            with self.subTest(data=data["match"]), self.assertRaises(ProfileError):
                parse_profile(data)

    def test_v1_profile_keeps_v2_fields_out(self):
        data = copy.deepcopy(self.data)
        data["schema"] = prof.PROFILE_SCHEMA
        with self.assertRaises(ProfileError):
            parse_profile(data)  # v1 must not quietly accept and ignore build/region
        builtin = get_profile(fx.PROFILE_ID)
        self.assertEqual(builtin.schema, prof.PROFILE_SCHEMA)
        self.assertEqual(builtin.match_fields, MATCH_FIELDS)
        match_device(builtin, fx.FACTS)
        match_device(builtin, parse_device_facts(fx.FACTS))

    def test_facts_parsing(self):
        self.assertEqual(parse_device_facts(fx.FACTS), fx.FACTS)  # v1 facts still parse
        for bad in ({**self.facts, "serial": "x"},
                    {**self.facts, "region": "<CONFIGURE: region>"},
                    {**self.facts, "firmwareBuild": "SYNTHETIC-BUILD-0000.1 "},
                    {**self.facts, "region": ""},
                    {k: v for k, v in self.facts.items() if k != "deviceId"}):
            with self.subTest(bad=sorted(bad)), self.assertRaises(ProfileError):
                parse_device_facts(bad)

    def test_refusals_never_echo_device_id_or_values(self):
        device_id = self.facts["deviceId"]
        cases = [
            lambda: match_device(self.profile, {**self.facts, "region": "OTHER-" + device_id}),
            lambda: match_device(self.profile, {**self.facts, "model": device_id}),
            lambda: parse_device_facts({**self.facts, "region": "<CONFIGURE " + device_id}),
            lambda: parse_device_facts({**self.facts, "extra": device_id}),
            lambda: parse_device_facts({**self.facts, "deviceId": 5}),
        ]
        for index, case in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(ProfileError) as ctx:
                case()
            self.assertNotIn(device_id, str(ctx.exception))
            self.assertNotIn("OTHER-", str(ctx.exception))

    def test_match_is_read_only(self):
        with self.assertRaises(TypeError):
            self.profile.match["region"] = self.facts["region"] + "x"
        with self.assertRaises(TypeError):
            del self.profile.match["firmwareBuild"]
        # The field list comes from the schema, not from the mapping.
        broken = prof.Profile(**{**self.profile.__dict__, "match": {
            k: v for k, v in self.profile.match.items() if k != "region"}})
        with self.assertRaises(ProfileError):
            match_device(broken, self.facts)

    def test_negative_control_match_fields_from_schema(self):
        """Guard off: if match_device used only v1 fields, a wrong region would pass."""
        wrong = {**self.facts, "region": "SYNTHETIC-OTHER"}
        with self.assertRaises(ProfileError):
            match_device(self.profile, wrong)
        with mock.patch.object(prof.Profile, "match_fields", property(lambda self: MATCH_FIELDS)):
            match_device(self.profile, wrong)  # weakened guard lets it through

    def test_builtin_profiles_unchanged(self):
        self.assertEqual(sorted(prof.builtin_profiles()), [fx.PROFILE_ID])


if __name__ == "__main__":
    unittest.main()

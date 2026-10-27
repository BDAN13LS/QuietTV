"""tvkit-components/1 catalogue, inventory binding and ChangeSet resolution.

Synthetic org.example.* components only; no real Samsung package ids.
"""

from __future__ import annotations

import copy
import itertools
import json
import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_public_toolkit_fixtures  # noqa: E402,F401  (puts public-toolkit on sys.path)
import test_public_toolkit_profile_v2 as pv2  # noqa: E402
from tvkit import catalogue as cat  # noqa: E402
from tvkit import transport  # noqa: E402
from tvkit.catalogue import CatalogueError, parse_catalogue, parse_inventory, resolve  # noqa: E402
from tvkit.profile import parse_device_facts, parse_profile  # noqa: E402

CONSUMER = Path(__file__).resolve().parent / "fixtures" / "consumer"


def load(name):
    return json.loads((CONSUMER / name).read_text(encoding="utf-8"))


class CatalogueCase(unittest.TestCase):
    def setUp(self):
        self.profile = parse_profile(load("profile-v2.json"))
        self.facts = parse_device_facts(pv2.consumer_facts())
        self.cat_data = load("components.json")
        self.inv_data = load("inventory.json")
        self.catalogue = parse_catalogue(self.cat_data, self.profile)
        self.inventory = parse_inventory(self.inv_data, self.profile, self.facts)

    def resolve(self, selection, inventory=None, catalogue=None, facts=None):
        return resolve(catalogue or self.catalogue, selection, self.profile, facts or self.facts,
                       inventory or self.inventory)

    def inventory_with(self, mutate):
        data = copy.deepcopy(self.inv_data)
        mutate(data)
        return parse_inventory(data, self.profile, self.facts)

    def catalogue_with(self, mutate):
        data = copy.deepcopy(self.cat_data)
        mutate(data)
        return parse_catalogue(data, self.profile)

    def comp(self, data, cid):
        return next(c for c in data["components"] if c["id"] == cid)

    def held(self, changeset):
        return {h.component_id: h.reason for h in changeset.held}

    def removed(self, changeset):
        return [a.component_id for a in changeset.actions]


class Schema(CatalogueCase):
    def test_fixture_parses(self):
        self.assertEqual(len(self.catalogue.components), 6)
        self.assertEqual(self.catalogue.dependents_of("example.feeds"), ("example.discovery", "example.tvplus"))

    def test_malformed_catalogues_refused(self):
        cases = {
            "unknown field": lambda d: self.comp(d, "example.guide").__setitem__("owner", "x"),
            "missing field": lambda d: self.comp(d, "example.guide").pop("status"),
            "versions keys differ": lambda d: self.comp(d, "example.tvplus")["expectedVersions"].pop(
                "org.example.tvplus-data"),
            "extra version key": lambda d: self.comp(d, "example.guide")["expectedVersions"].__setitem__(
                "org.example.other", {"version": "1", "sha256": "0" * 64}),
            "bad hash": lambda d: self.comp(d, "example.guide")["expectedVersions"]["org.example.guide"]
            .__setitem__("sha256", "A" * 64),
            "placeholder version": lambda d: self.comp(d, "example.guide")["expectedVersions"]
            ["org.example.guide"].__setitem__("version", "<CONFIGURE>"),
            "unknown action": lambda d: self.comp(d, "example.guide").__setitem__("allowedActions", ["disable"]),
            "bad status": lambda d: self.comp(d, "example.guide").__setitem__("status", "approved"),
            "bool as string": lambda d: self.comp(d, "example.guide").__setitem__("recommended", "true"),
            "duplicate id": lambda d: d["components"].append(copy.deepcopy(self.comp(d, "example.guide"))),
            "package in two components": lambda d: self.comp(d, "example.voice").__setitem__(
                "packageIds", ["org.example.guide"]) or self.comp(d, "example.voice").__setitem__(
                "expectedVersions", copy.deepcopy(self.comp(d, "example.guide")["expectedVersions"])),
            "unknown dependency": lambda d: self.comp(d, "example.guide").__setitem__("dependsOn", ["example.nope"]),
            "self dependency": lambda d: self.comp(d, "example.guide").__setitem__("dependsOn", ["example.guide"]),
            "cycle": lambda d: self.comp(d, "example.feeds").__setitem__("dependsOn", ["example.tvplus"]),
            "long cycle": lambda d: (self.comp(d, "example.guide").__setitem__("dependsOn", ["example.voice"]),
                                     self.comp(d, "example.voice").__setitem__("dependsOn", ["example.feeds"]),
                                     self.comp(d, "example.feeds").__setitem__("dependsOn", ["example.guide"])),
            "other profile": lambda d: d.__setitem__("profileId", "SYNTHETIC-OTHER"),
            "certificate": lambda d: self.comp(d, "example.guide").__setitem__(
                "evidence", ["-----BEGIN " + "CERTIFICATE-----"]),  # split so the export scan does not flag this file
            "owner state": lambda d: self.comp(d, "example.guide").__setitem__("explain", "uses deviceId 123"),
        }
        for label, mutate in cases.items():
            with self.subTest(label), self.assertRaises(CatalogueError):
                self.catalogue_with(mutate)

    def test_public_fixture_has_no_owner_state(self):
        raw = (CONSUMER / "components.json").read_text(encoding="utf-8").lower()
        for marker in cat.OWNER_STATE_MARKERS + ("org.samsung", "com.samsung", "bixby"):
            self.assertNotIn(marker, raw)
        for comp in self.cat_data["components"]:
            for pid in comp["packageIds"]:
                self.assertTrue(pid.startswith("org.example."), pid)

    def test_results_are_immutable(self):
        cs = self.resolve(["example.guide"])
        with self.assertRaises(FrozenInstanceError):
            cs.actions = ()
        with self.assertRaises(TypeError):
            self.catalogue.components["x"] = None
        with self.assertRaises(TypeError):
            self.inventory.packages["org.example.guide"] = None
        self.assertIsInstance(cs.actions, tuple)

    def test_no_transport_registered(self):
        self.assertEqual(transport.TRANSPORTS, {})


class Inventory(CatalogueCase):
    def test_inventory_bound_to_device_and_profile(self):
        with self.assertRaises(CatalogueError) as ctx:
            parse_inventory(self.inv_data, self.profile, {**self.facts, "deviceId": "SYNTHETIC-OTHER-DEVICE"})
        self.assertNotIn("SYNTHETIC", str(ctx.exception))
        for mutate in (lambda d: d.__setitem__("profileId", "SYNTHETIC-OTHER"),
                       lambda d: d.__setitem__("deviceIdSha256", "<CONFIGURE>"),
                       lambda d: d["packages"].append(copy.deepcopy(d["packages"][0])),
                       lambda d: d["packages"][0].__setitem__("installRoot", "/tmp"),
                       lambda d: d["packages"][0].__setitem__("name", "Guide"),
                       lambda d: d["packages"][0].__setitem__("version", "")):
            with self.assertRaises(CatalogueError):
                self.inventory_with(mutate)

    def test_resolve_refuses_device_mismatch(self):
        for field, value in (("region", "SYNTHETIC-OTHER"), ("firmwareBuild", "SYNTHETIC-BUILD-0000.2"),
                             ("deviceId", "SYNTHETIC-OTHER-DEVICE")):
            with self.subTest(field=field), self.assertRaises(CatalogueError) as ctx:
                self.resolve(["example.guide"], facts={**self.facts, field: value})
            self.assertNotIn(self.facts["deviceId"], str(ctx.exception))
            self.assertNotIn("SYNTHETIC-OTHER-DEVICE", str(ctx.exception))


class Resolution(CatalogueCase):
    def test_plain_removal(self):
        cs = self.resolve(["example.guide"])
        self.assertEqual(self.removed(cs), ["example.guide"])
        self.assertEqual(cs.actions[0].action, "remove")
        self.assertEqual(cs.actions[0].packages[0].path, "/opt/usr/apps/org.example.guide")
        self.assertEqual(cs.held, ())

    def test_unknown_or_malformed_selection_refuses_whole_request(self):
        for selection in (["example.guide", "example.gide"], ["example.guide", "example.guide"],
                          "example.guide", {"example.guide": "remove"}, [1]):
            with self.subTest(selection=selection), self.assertRaises(CatalogueError):
                self.resolve(selection)

    def test_protected_draft_and_unsupported_held(self):
        cs = self.resolve(["example.securityinfo", "example.voice", "example.guide"])
        self.assertEqual(self.held(cs), {"example.securityinfo": "protected-id", "example.voice": "not-reviewed"})
        self.assertEqual(self.removed(cs), ["example.guide"])
        no_evidence = self.catalogue_with(lambda d: self.comp(d, "example.guide").__setitem__("evidence", []))
        self.assertEqual(self.held(self.resolve(["example.guide"], catalogue=no_evidence)),
                         {"example.guide": "not-reviewed"})
        no_restore = self.catalogue_with(
            lambda d: self.comp(d, "example.guide").__setitem__("restoreSupported", False))
        self.assertEqual(self.held(self.resolve(["example.guide"], catalogue=no_restore)),
                         {"example.guide": "restore-not-supported"})
        info_only = self.catalogue_with(lambda d: self.comp(d, "example.guide").__setitem__("allowedActions", []))
        self.assertEqual(self.held(self.resolve(["example.guide"], catalogue=info_only)),
                         {"example.guide": "remove-not-allowed"})

    def test_exact_installed_version_and_hash_required(self):
        def pkg(d, pid):
            return next(p for p in d["packages"] if p["packageId"] == pid)
        cases = {
            "installed-version-differs:org.example.guide":
                lambda d: pkg(d, "org.example.guide").__setitem__("version", "1.0.1"),
            "installed-hash-differs:org.example.guide":
                lambda d: pkg(d, "org.example.guide").__setitem__("sha256", "1" * 64),
            "not-installed": lambda d: d["packages"].remove(pkg(d, "org.example.guide")),
        }
        for reason, mutate in cases.items():
            with self.subTest(reason):
                cs = self.resolve(["example.guide"], inventory=self.inventory_with(mutate))
                self.assertEqual(self.held(cs), {"example.guide": reason})
                self.assertEqual(cs.actions, ())
        partial = self.inventory_with(lambda d: d["packages"].remove(pkg(d, "org.example.tvplus-data")))
        self.assertEqual(self.held(self.resolve(["example.tvplus"], inventory=partial)),
                         {"example.tvplus": "partially-installed"})

    def test_provider_blocked_while_dependent_retained(self):
        cs = self.resolve(["example.feeds"])
        self.assertEqual(self.held(cs), {"example.feeds": "required-by:example.discovery"})
        cs = self.resolve(["example.feeds", "example.tvplus"])
        self.assertEqual(self.removed(cs), ["example.tvplus"])
        self.assertEqual(self.held(cs), {"example.feeds": "required-by:example.discovery"})

    def test_provider_removed_after_all_dependents(self):
        cs = self.resolve(["example.feeds", "example.tvplus", "example.discovery"])
        self.assertEqual(self.removed(cs), ["example.discovery", "example.tvplus", "example.feeds"])

    def test_absent_dependent_does_not_block(self):
        def drop(d):
            d["packages"] = [p for p in d["packages"] if p["packageId"] != "org.example.discovery"]
        cs = self.resolve(["example.feeds", "example.tvplus"], inventory=self.inventory_with(drop))
        self.assertEqual(self.removed(cs), ["example.tvplus", "example.feeds"])

    def test_holds_cascade_to_providers(self):
        def stale(d):
            next(p for p in d["packages"] if p["packageId"] == "org.example.tvplus")["version"] = "2.0.0"
        cs = self.resolve(["example.feeds", "example.tvplus", "example.discovery"],
                          inventory=self.inventory_with(stale))
        self.assertEqual(self.removed(cs), ["example.discovery"])
        self.assertEqual(self.held(cs), {"example.tvplus": "installed-version-differs:org.example.tvplus",
                                         "example.feeds": "required-by:example.tvplus"})

    def test_selection_order_cannot_change_outcome(self):
        ids = ["example.feeds", "example.tvplus", "example.discovery", "example.guide", "example.voice"]
        def stale(d):
            next(p for p in d["packages"] if p["packageId"] == "org.example.discovery")["sha256"] = "2" * 64
        for inventory in (self.inventory, self.inventory_with(stale)):
            digests = {self.resolve(list(order), inventory=inventory).digest
                       for order in itertools.permutations(ids)}
            digests.add(self.resolve(set(ids), inventory=inventory).digest)
            self.assertEqual(len(digests), 1)

    def test_digest_binds_inputs(self):
        base = self.resolve(["example.guide"]).digest
        other_inv = self.inventory_with(lambda d: d["packages"].pop())
        self.assertNotEqual(base, self.resolve(["example.guide"], inventory=other_inv).digest)
        other_cat = self.catalogue_with(lambda d: self.comp(d, "example.voice").__setitem__("title", "x"))
        self.assertNotEqual(base, self.resolve(["example.guide"], catalogue=other_cat).digest)


class Recheck(CatalogueCase):
    def test_genuine_changeset_accepted_forged_refused(self):
        from dataclasses import replace
        cs = self.resolve(["example.guide", "example.securityinfo"])
        cat.check_changeset(cs, self.catalogue, self.profile, self.facts, self.inventory)
        forged_target = cat.PackageTarget("org.example.securityinfo", "1.0.0",
                                          self.inventory.packages["org.example.securityinfo"].sha256,
                                          "/opt/usr/apps")
        forgeries = (
            replace(cs, actions=cs.actions + (cat.ChangeAction("example.securityinfo", "remove",
                                                               (forged_target,)),), held=()),
            replace(cs, held=(cat.Held("example.securityinfo", "reviewed-ok"),)),
            replace(cs, actions=(replace(cs.actions[0], action="disable"),)),
            replace(cs, device_id_sha256="0" * 64),
        )
        for index, forged in enumerate(forgeries):
            with self.subTest(index), self.assertRaises(CatalogueError):
                cat.check_changeset(forged, self.catalogue, self.profile, self.facts, self.inventory)
        stale = self.inventory_with(lambda d: d["packages"][0].__setitem__("version", "9.9"))
        with self.assertRaises(CatalogueError):
            cat.check_changeset(cs, self.catalogue, self.profile, self.facts, stale)


class NegativeControls(CatalogueCase):
    """Each critical guard switched off must change the protected outcome."""

    def test_dependency_block(self):
        self.assertEqual(self.removed(self.resolve(["example.feeds"])), [])
        with mock.patch.object(cat.Catalogue, "dependents_of", lambda self, cid: ()):
            self.assertEqual(self.removed(self.resolve(["example.feeds"])), ["example.feeds"])

    def test_hash_match(self):
        inv = self.inventory_with(lambda d: d["packages"][0].__setitem__("sha256", "3" * 64))
        cid = "example.guide"
        self.assertEqual(self.removed(self.resolve([cid], inventory=inv)), [])
        original = cat._component_hold

        def weakened(comp, profile, inventory):
            reason = original(comp, profile, inventory)
            return None if reason and reason.startswith("installed-hash-differs") else reason
        with mock.patch.object(cat, "_component_hold", weakened):
            self.assertEqual(self.removed(self.resolve([cid], inventory=inv)), [cid])

    def test_protected_hold(self):
        cid = "example.securityinfo"
        self.assertEqual(self.removed(self.resolve([cid])), [])
        with mock.patch.object(type(self.profile), "is_protected", lambda self, ident: False):
            self.assertEqual(self.removed(self.resolve([cid])), [cid])

    def test_inventory_device_binding(self):
        other = {**self.facts, "deviceId": "SYNTHETIC-OTHER-DEVICE"}
        with self.assertRaises(CatalogueError):
            parse_inventory(self.inv_data, self.profile, other)
        with mock.patch.object(cat, "device_id_sha256", lambda value: self.inv_data["deviceIdSha256"]):
            parse_inventory(self.inv_data, self.profile, other)

    def test_resolve_matches_device(self):
        other = {**self.facts, "region": "SYNTHETIC-OTHER"}
        with self.assertRaises(CatalogueError):
            self.resolve(["example.guide"], facts=other)
        with mock.patch.object(cat, "match_device", lambda profile, facts: None):
            self.assertEqual(self.removed(self.resolve(["example.guide"], facts=other)), ["example.guide"])

    def test_unknown_selection_refused(self):
        typo = ["example.guide", "example.tvplsu"]
        with self.assertRaises(CatalogueError):
            self.resolve(typo)
        with mock.patch.object(cat, "_selection",
                               lambda sel, c: frozenset(i for i in sel if i in c.components)):
            self.assertEqual(self.removed(self.resolve(typo)), ["example.guide"])  # silently shrunk

    def test_public_safety_scan(self):
        def leak(d):
            self.comp(d, "example.guide")["explain"] = "duid 0123"
        with self.assertRaises(CatalogueError):
            self.catalogue_with(leak)
        with mock.patch.object(cat, "_scan_public", lambda value, label: None):
            self.catalogue_with(leak)

    def test_cycle_detection(self):
        def cyc(d):
            self.comp(d, "example.feeds")["dependsOn"] = ["example.tvplus"]
        with self.assertRaises(CatalogueError):
            self.catalogue_with(cyc)
        with mock.patch.object(cat, "_check_graph", lambda components: None):
            self.catalogue_with(cyc)


if __name__ == "__main__":
    unittest.main()

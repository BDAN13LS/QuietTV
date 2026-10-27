"""Write audit: pure comparison of before/after inventories against declared effects."""

from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_public_toolkit_catalogue import CatalogueCase  # noqa: E402
from tvkit import audit as au  # noqa: E402
from tvkit.audit import (AuditError, DeclaredEffects, Difference, Snapshot, WriteAudit,  # noqa: E402
                         declared_from_changeset)


def h(text):
    return hashlib.sha256(text.encode()).hexdigest()


ROOT = "/opt/usr/apps"
GUIDE = f"{ROOT}/org.example.guide"
TREE = {
    ROOT: h("root-meta"),
    GUIDE: h("guide-dir"),
    f"{GUIDE}/bin/app": h("guide-bin"),
    f"{ROOT}/org.example.guide2": h("guide2-dir"),
    f"{ROOT}/org.example.feeds": h("feeds-dir"),
}
REGISTRY = {("org.example.guide", "app_disable"): "false", ("org.example.feeds", "app_disable"): "false"}
KEYS = {"db/appfw/home_appid": h("stock-home")}


def snap(tree=TREE, registry=REGISTRY, keys=KEYS):
    return Snapshot(dict(tree), dict(registry), dict(keys))


class Audit(CatalogueCase):
    def setUp(self):
        super().setUp()
        self.declared = declared_from_changeset(self.resolve(["example.guide"]), self.profile)
        self.removed = {k: v for k, v in TREE.items() if not (k == GUIDE or k.startswith(GUIDE + "/"))}

    def test_declared_removal_is_clean(self):
        self.assertEqual(self.declared.removed_trees, (GUIDE,))
        audit = WriteAudit(snap(), snap(self.removed))
        self.assertEqual(audit.unexpected(self.declared), ())
        self.assertEqual(audit.undone(self.declared), ())

    def test_every_undeclared_difference_listed(self):
        tree = dict(self.removed)
        del tree[f"{ROOT}/org.example.guide2"]  # sibling sharing the name prefix
        tree[ROOT] = h("root-meta-changed")  # parent
        tree[f"{GUIDE}/new"] = h("x")  # addition under the declared path
        tree["/opt/usr/apps/org.example.feeds"] = h("feeds-changed")
        registry = {**REGISTRY, ("org.example.feeds", "app_disable"): "true"}
        keys = {"db/appfw/home_appid": h("our-home")}
        found = WriteAudit(snap(), snap(tree, registry, keys)).unexpected(self.declared)
        self.assertEqual(set(found), {
            Difference("tree-removed", f"{ROOT}/org.example.guide2"),
            Difference("tree-changed", ROOT),
            Difference("tree-added", f"{GUIDE}/new"),
            Difference("tree-changed", f"{ROOT}/org.example.feeds"),
            Difference("registry", "org.example.feeds.app_disable"),
            Difference("key", "db/appfw/home_appid"),
        })
        self.assertNotIn("true", repr(found))  # names, never values

    def test_declared_registry_and_key_values_exact(self):
        declared = DeclaredEffects(registry={("org.example.feeds", "app_disable"): "true"},
                                   keys={"db/appfw/home_appid": h("our-home")})
        ok = snap(registry={**REGISTRY, ("org.example.feeds", "app_disable"): "true"},
                  keys={"db/appfw/home_appid": h("our-home")})
        self.assertEqual(WriteAudit(snap(), ok).unexpected(declared), ())
        wrong = snap(registry={**REGISTRY, ("org.example.feeds", "app_disable"): "1"},
                     keys={"db/appfw/home_appid": h("someone-else")})
        self.assertEqual(len(WriteAudit(snap(), wrong).unexpected(declared)), 2)
        gone = snap(registry={("org.example.guide", "app_disable"): "false"})
        self.assertEqual(WriteAudit(snap(), gone).unexpected(declared),
                         (Difference("registry", "org.example.feeds.app_disable"),))

    def test_undone_removal_reported(self):
        audit = WriteAudit(snap(), snap())
        self.assertEqual(audit.unexpected(self.declared), ())
        self.assertEqual(audit.undone(self.declared), (GUIDE,))

    def test_no_blanket_parent_permits(self):
        roots = self.profile.install_roots
        for path in (ROOT, "/", "/opt", "/opt/usr", f"{GUIDE}/", f"{GUIDE}/bin", "relative/x",
                     f"{ROOT}/../etc", "/tmp/org.example.guide"):
            with self.subTest(path=path), self.assertRaises(AuditError):
                DeclaredEffects(removed_trees=(path,), install_roots=roots)
        with self.assertRaises(AuditError):
            DeclaredEffects(removed_trees=(GUIDE,))  # no roots given: nothing is permitted
        self.assertEqual(DeclaredEffects(removed_trees=(GUIDE,), install_roots=roots).removed_trees, (GUIDE,))

    def test_snapshots_validated_and_immutable(self):
        for bad in (lambda: Snapshot({"rel/path": h("x")}, {}, {}), lambda: Snapshot({GUIDE: "nothex"}, {}, {}),
                    lambda: Snapshot({}, {"org.example.guide": "x"}, {}), lambda: Snapshot({}, {}, {"k": "v"})):
            with self.assertRaises(AuditError):
                bad()
        before = snap()
        with self.assertRaises(TypeError):
            before.tree[GUIDE] = h("y")

    def test_negative_control_exact_prefix(self):
        tree = dict(self.removed)
        del tree[f"{ROOT}/org.example.guide2"]
        audit = WriteAudit(snap(), snap(tree))
        self.assertEqual(len(audit.unexpected(self.declared)), 1)
        # Guard off: a plain prefix match would also permit the sibling.
        with mock.patch.object(au.DeclaredEffects, "permits_removal",
                               lambda self, path: any(path.startswith(t) for t in self.removed_trees)):
            self.assertEqual(audit.unexpected(self.declared), ())


if __name__ == "__main__":
    unittest.main()

"""Tests for the device-facts probe packager. Stdlib only; no TV, network or signing.

Run: python -m unittest tools.tv_device_facts.test_pack  (or from this folder: python -m unittest test_pack)
"""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location("tvdevicefacts_pack", Path(__file__).resolve().parent / "pack.py")
pack = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(pack)

APP_ID = "abcdefghij.DeviceFacts"
PACKAGE = "abcdefghij"


class PackTests(unittest.TestCase):
    def setUp(self):
        # A scratch area OUTSIDE the repository (system temp carries no .git ancestor).
        self.tmp = Path(tempfile.mkdtemp(prefix="tvdevicefacts-pack-"))

    def tearDown(self):
        for root, dirs, files in os.walk(self.tmp, topdown=False):
            for name in files:
                os.remove(os.path.join(root, name))
            for name in dirs:
                os.rmdir(os.path.join(root, name))
        os.rmdir(self.tmp)

    def test_members_and_config_contents(self):
        files = pack.assemble(APP_ID, PACKAGE, "0.1.0")
        self.assertEqual(sorted(files), ["collector.js", "config.xml", "index.html"])
        config = files["config.xml"].decode("utf-8")
        self.assertIn('id="' + APP_ID + '"', config)
        self.assertIn('package="' + PACKAGE + '"', config)
        self.assertIn("http://tizen.org/privilege/filesystem.write", config)
        self.assertIn("http://developer.samsung.com/privilege/productinfo", config)
        # No network, author, boot, service, Home or leftover placeholder.
        for banned in ("privilege/internet", "<access", "<author", "on-boot", "tizen:service", "{{", "}}"):
            self.assertNotIn(banned, config)

    def test_deterministic_bytes(self):
        out1 = self.tmp / "a.wgt"
        out2 = self.tmp / "b.wgt"
        pack.build(APP_ID, PACKAGE, out1)
        pack.build(APP_ID, PACKAGE, out2)
        self.assertEqual(out1.read_bytes(), out2.read_bytes())
        with zipfile.ZipFile(out1) as zf:
            self.assertEqual(sorted(zf.namelist()), ["collector.js", "config.xml", "index.html"])

    def test_invalid_ids_and_version_refused(self):
        for app_id, package, version in (
            ("bad id", PACKAGE, "0.1.0"),
            (APP_ID, "short", "0.1.0"),
            (APP_ID, PACKAGE, "v1"),
            ("abcdefghij", PACKAGE, "0.1.0"),  # app id without a .name
        ):
            with self.subTest(app_id=app_id, package=package, version=version):
                with self.assertRaises(pack.PackError):
                    pack.assemble(app_id, package, version)

    def test_non_wgt_and_overwrite_refused(self):
        with self.assertRaises(pack.PackError):
            pack.build(APP_ID, PACKAGE, self.tmp / "x.zip")
        out = self.tmp / "once.wgt"
        pack.build(APP_ID, PACKAGE, out)
        with self.assertRaises(pack.PackError):
            pack.build(APP_ID, PACKAGE, out)

    def test_output_inside_a_repo_refused(self):
        repo = self.tmp / "fake-repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        with self.assertRaises(pack.PackError):
            pack.build(APP_ID, PACKAGE, repo / "probe.wgt")
        # A git worktree uses a .git FILE marker, not a directory; it must be refused too.
        worktree = self.tmp / "fake-worktree"
        worktree.mkdir()
        (worktree / ".git").write_text("gitdir: /somewhere/.git/worktrees/x\n", encoding="utf-8")
        with self.assertRaises(pack.PackError):
            pack.build(APP_ID, PACKAGE, worktree / "probe.wgt")


if __name__ == "__main__":
    unittest.main()

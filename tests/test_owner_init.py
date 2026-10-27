"""helper/owner_init.py: owner storage only, exact host contract, preview-only owner data.

Every token here is SYNTHETIC (fixed test strings); no real token, key or certificate is created.
Run: python -B -m unittest tests.test_owner_init
"""
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "helper"))
import owner_init as oi  # noqa: E402

PROFILE_ID = "UN43U8000FBXZA-1300.8-armv7l-tizen9"
RAW_DEVICE_ID = "SYNTHETIC-DEVICE-ID-0001"
SYN_TOKEN = "SyntheticTestTokenAbcdefghijklmnopqrstuvwx_0123"
FACTS = {"model": "UN43U8000FBXZA", "firmware": "1300.8", "arch": "armv7l", "osName": "Tizen",
         "osVersion": "9", "deviceId": RAW_DEVICE_ID}
INVENTORY = [
    {"Id": "org.tizen.netflix-app", "Name": "Netflix", "Installed": True, "Disabled": False, "Launchable": True},
    {"Id": "9Ur5IzDKqV.TizenYouTube", "Name": "YouTube", "Installed": True, "Disabled": False, "Launchable": True},
    {"Id": "gone.app", "Name": "Removed", "Installed": False, "Disabled": False, "Launchable": True},
    {"Id": "off.app", "Name": "Disabled", "Installed": True, "Disabled": True, "Launchable": True},
    {"Id": "svc.app", "Name": "Service", "Installed": True, "Disabled": False, "Launchable": False},
    {"Id": "quiet.iptv", "Name": "IPTV", "Installed": True, "Disabled": False, "Launchable": True},
    {"Id": "Qa7Rt2Lm9X.QuietTV", "Name": "Self", "Installed": True, "Disabled": False, "Launchable": True},
]


def inputs(**over):
    base = dict(profile_id=PROFILE_ID, device_facts=dict(FACTS), inventory=[dict(r) for r in INVENTORY],
                package_id="Qa7Rt2Lm9X", owner_uid=5001, input_paths=["/dev/input/event3"],
                runtime_root="/home/owner/share/owner-a/quiet-tv")
    base.update(over)
    return base


def make_junction(link: Path, target: Path) -> bool:
    try:
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(target), str(link))
        else:
            os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, ImportError, AttributeError):
        return False


class OwnerInitTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def all_bytes(self, folder: Path) -> bytes:
        return b"".join(p.read_bytes() for p in sorted(folder.iterdir()))

    def test_generates_preview_owner_data_with_filtered_catalogue(self):
        out = self.tmp / "owner"
        result = oi.generate(out, **inputs())
        self.assertEqual(result["files"], sorted(oi.OWNER_FILES))
        self.assertEqual(result["deviceIdSha256"], oi.tvprofile.device_id_sha256(RAW_DEVICE_ID))
        load = lambda n: json.loads((out / n).read_text("ascii"))
        apps = load("apps.json")
        self.assertEqual([a["Id"] for a in apps], ["org.tizen.netflix-app", "9Ur5IzDKqV.TizenYouTube"])
        self.assertEqual(load("settings.json"), {"Order": [a["Id"] for a in apps], "Favorites": [], "Wallpaper": 0})
        host = oi.validate_host_config(load("host-config.json"))
        self.assertEqual(host["ownAppId"], "Qa7Rt2Lm9X.QuietTV")
        self.assertFalse(host["iptvEnabled"] or host["webUiEnabled"] or host["snapshotEnabled"])
        widget = oi.validate_widget_config(load("widget-config.json"))
        self.assertEqual(widget["backend"], {"enabled": False})
        self.assertEqual((widget["preview"], widget["onBoot"], widget["registerAsHome"]), (True, False, False))
        state = load("owner-state.json")
        self.assertIs(state["consumerApplyEligible"], False)
        blob = self.all_bytes(out)
        self.assertNotIn(RAW_DEVICE_ID.encode(), blob)
        self.assertFalse((out / oi.TOKEN_FILE).exists())

    def test_same_inputs_same_bytes_and_owners_differ(self):
        oi.generate(self.tmp / "a1", **inputs())
        oi.generate(self.tmp / "a2", **inputs())
        for name in oi.OWNER_FILES:
            self.assertEqual((self.tmp / "a1" / name).read_bytes(), (self.tmp / "a2" / name).read_bytes())
        oi.generate(self.tmp / "b", **inputs(package_id="Zx9Kp3Wq7B", owner_uid=5002,
                                             device_facts=dict(FACTS, deviceId="SYNTHETIC-DEVICE-ID-0002")))
        self.assertNotEqual((self.tmp / "a1/host-config.json").read_bytes(), (self.tmp / "b/host-config.json").read_bytes())
        for name in ("host-config.json", "owner-state.json"):
            self.assertNotIn(b"Qa7Rt2Lm9X", (self.tmp / "b" / name).read_bytes())
        self.assertNotEqual((self.tmp / "a1/owner-state.json").read_bytes(), (self.tmp / "b/owner-state.json").read_bytes())

    def test_profile_must_match_exactly(self):
        for facts in (dict(FACTS, firmware="1300.9"), dict(FACTS, model="UN50U8000FBXZA")):
            with self.assertRaisesRegex(oi.OwnerInitError, "does not exactly match"):
                oi.generate(self.tmp / "o", **inputs(device_facts=facts))
        with self.assertRaisesRegex(oi.OwnerInitError, "unsupported profile"):
            oi.generate(self.tmp / "o", **inputs(profile_id="Other-1.0"))
        with self.assertRaises(oi.OwnerInitError):
            oi.generate(self.tmp / "o", **inputs(device_facts=dict(FACTS, extra="x")))
        self.assertFalse((self.tmp / "o").exists())

    def test_existing_destination_refused(self):
        (self.tmp / "o").mkdir()
        with self.assertRaisesRegex(oi.OwnerInitError, "already exists"):
            oi.generate(self.tmp / "o", **inputs())
        self.assertEqual(list((self.tmp / "o").iterdir()), [])

    def test_refuses_any_repository_destination(self):
        with self.assertRaisesRegex(oi.OwnerInitError, "repository"):
            oi.generate(REPO / "owner-should-not-exist", **inputs())
        self.assertFalse((REPO / "owner-should-not-exist").exists())
        for marker_is_file in (False, True):  # a checkout (.git dir) and a worktree (.git file)
            other = self.tmp / f"repo{int(marker_is_file)}"
            (other / "sub").mkdir(parents=True)
            (other / ".git").write_text("gitdir: x") if marker_is_file else (other / ".git").mkdir()
            with self.assertRaisesRegex(oi.OwnerInitError, "repository"):
                oi.generate(other / "sub" / "owner", **inputs())
        for bad in ("relative/owner", str(self.tmp / "x" / ".." / "owner"), str(self.tmp / "missing" / "owner")):
            with self.assertRaises(oi.OwnerInitError):
                oi.generate(bad, **inputs())
        # positive control: the same call outside any repository succeeds
        oi.generate(self.tmp / "fine", **inputs())

    def test_refuses_link_into_a_repository(self):
        repo = self.tmp / "repo"
        (repo / "inner").mkdir(parents=True)
        (repo / ".git").mkdir()
        link = self.tmp / "via-link"
        if not make_junction(link, repo / "inner"):
            self.skipTest("cannot create a junction/symlink here")
        with self.assertRaisesRegex(oi.OwnerInitError, "repository"):
            oi.generate(link / "owner", **inputs())
        with self.assertRaisesRegex(oi.OwnerInitError, "link"):
            oi.generate(link, **inputs())  # an existing link as the destination itself
        self.assertEqual(list((repo / "inner").iterdir()), [])

    def test_private_permissions(self):
        out = self.tmp / "owner"
        result = oi.generate(out, **inputs())
        if os.name == "nt":
            self.assertEqual(result["permissions"], "windows-acl-owner-only")
            acl = subprocess.run(["icacls", str(out)], capture_output=True, text=True).stdout
            self.assertNotIn("(I)", acl)  # inheritance removed
            self.assertNotRegex(acl, r"(?i)\b(Everyone|BUILTIN\\Users|Authenticated Users)\b")
        else:
            self.assertEqual(out.stat().st_mode & 0o777, 0o700)
            self.assertEqual((out / "apps.json").stat().st_mode & 0o777, 0o600)

    def test_backend_token_only_in_owner_storage(self):
        out = self.tmp / "owner"
        oi.generate(out, token_factory=lambda: SYN_TOKEN, **inputs(backend_port=47123))
        self.assertEqual((out / oi.TOKEN_FILE).read_text("ascii"), SYN_TOKEN)
        others = b"".join((out / n).read_bytes() for n in oi.OWNER_FILES)
        self.assertNotIn(SYN_TOKEN.encode(), others)
        self.assertEqual(json.loads((out / "widget-config.json").read_text())["backend"], {"enabled": True, "port": 47123})
        self.assertEqual(oi.load_owner_dir(out)["token"], SYN_TOKEN)
        for port in (4190, 4196, 80, 70000, True):
            with self.assertRaises(oi.OwnerInitError):
                oi.generate(self.tmp / f"p{port}", token_factory=lambda: SYN_TOKEN, **inputs(backend_port=port))
        for token in ("short", "<CONFIGURE-token-here-aaaaaaaaaaaaaaaaaaaa>", "a" * 40, "replace" + "Ab1" * 10):
            with self.assertRaises(oi.OwnerInitError):
                oi.generate(self.tmp / "t", token_factory=lambda t=token: t, **inputs(backend_port=47123))
        self.assertFalse((self.tmp / "t").exists())

    def test_disabled_owner_dir_with_stray_token_refused(self):
        out = self.tmp / "owner"
        oi.generate(out, **inputs())
        oi.load_owner_dir(out)
        (out / oi.TOKEN_FILE).write_text(SYN_TOKEN)
        with self.assertRaisesRegex(oi.OwnerInitError, "backend-token present"):
            oi.load_owner_dir(out)

    def test_strict_inputs(self):
        bad = [
            dict(package_id="Qa7Rt2Lm9"), dict(package_id="aaaaaaaaaa"), dict(package_id="example123"),
            dict(package_id="Qa7Rt2Lm9X\n"), dict(app_name="9bad"), dict(owner_uid=4999), dict(owner_uid=6000),
            dict(owner_uid=True), dict(input_paths=[]), dict(input_paths=[f"/dev/input/event{i}" for i in range(9)]),
            dict(input_paths=["/dev/input/mouse0"]), dict(input_paths=["/dev/input/event1", "/dev/input/event1"]),
            dict(runtime_root="/tmp/quiet-tv"), dict(runtime_root="/home/owner/share/../quiet-tv"),
            dict(runtime_root="/home/owner/share/<CHOSEN>/quiet-tv"), dict(runtime_root="/home/owner/share/a/other"),
            dict(runtime_root="/home/owner/share/a//quiet-tv"), dict(home_key_code=0),
            dict(inventory=[]), dict(inventory=[dict(INVENTORY[0], Installed="true")]),
            dict(inventory=[dict(INVENTORY[0], Extra=1)]), dict(inventory=[INVENTORY[0], INVENTORY[0]]),
            dict(inventory=[dict(INVENTORY[0], Id="../etc")]), dict(inventory=[INVENTORY[2], INVENTORY[5]]),
            # valid for the host but refused by the frontend runtime list (alphanumeric first, <=128)
            dict(inventory=[dict(INVENTORY[0], Id="_lead.app")]), dict(inventory=[dict(INVENTORY[0], Id="a" * 129)]),
        ]
        bad += [
            dict(inventory=[dict(INVENTORY[0], Name="\U0001F600" * 33)]),  # 66 UTF-16 units: C# refuses
            dict(inventory=[dict(INVENTORY[0], Name="Net" + chr(0xD800) + "flix")]),  # lone surrogate
            dict(inventory=[dict(INVENTORY[0], Name="a" * 65)]),
        ]
        for over in bad:
            with self.subTest(over=over), self.assertRaises(oi.OwnerInitError):
                oi.generate(self.tmp / "o", **inputs(**over))
        self.assertFalse((self.tmp / "o").exists())

    def test_name_length_counts_utf16_units_like_csharp(self):
        oi.generate(self.tmp / "emoji", **inputs(inventory=[dict(INVENTORY[0], Name="\U0001F600" * 32)]))
        oi.generate(self.tmp / "bmp", **inputs(inventory=[dict(INVENTORY[0], Name="é" * 64)]))

    def test_default_token_redraws_placeholder_lookalikes(self):
        draws = iter(["todo" + "Ab1x" * 10, "Synthetic" + "Qw3_" * 8])
        saved = oi._default_token
        oi._default_token = lambda: next(draws)
        try:
            oi.generate(self.tmp / "o", **inputs(backend_port=47123))
        finally:
            oi._default_token = saved
        self.assertEqual((self.tmp / "o" / oi.TOKEN_FILE).read_text(), "Synthetic" + "Qw3_" * 8)

    def test_cli_prints_paths_and_hash_only(self):
        facts, inv = self.tmp / "facts.json", self.tmp / "inv.json"
        facts.write_text(json.dumps(FACTS))
        inv.write_text(json.dumps(INVENTORY))
        argv = ["--owner-dir", str(self.tmp / "o"), "--profile-id", PROFILE_ID, "--device-facts", str(facts),
                "--inventory", str(inv), "--package-id", "Qa7Rt2Lm9X", "--owner-uid", "5001",
                "--input-path", "/dev/input/event3", "--runtime-root", "/home/owner/share/a/quiet-tv",
                "--backend-port", "47123"]
        saved = oi._default_token
        oi._default_token = lambda: SYN_TOKEN
        try:
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(oi.main(argv), 0)
                self.assertEqual(oi.main(argv), 2)  # second run: destination exists
        finally:
            oi._default_token = saved
        text = out.getvalue() + err.getvalue()
        self.assertNotIn(RAW_DEVICE_ID, text)
        self.assertNotIn(SYN_TOKEN, text)
        self.assertIn(oi.tvprofile.device_id_sha256(RAW_DEVICE_ID), text)


class HostContractTests(unittest.TestCase):
    """The Python validator is a narrow mirror of launcher/host/HostConfig.cs; fail on drift."""
    CS = REPO / "launcher" / "host" / "HostConfig.cs"

    def setUp(self):
        if not self.CS.is_file():
            self.skipTest("HostConfig.cs not present")
        self.cs = self.CS.read_text("utf-8")

    def test_constants_match_csharp_source(self):
        cs = self.cs
        for pattern in (oi.OWN_APP_ID_RE, oi.INPUT_PATH_RE, oi.ROOT_SEGMENT_RE, oi.ROOT_LEAF_RE, oi.APP_ID_RE):
            self.assertIn('@"^' + pattern + '$"', cs)
        known = re.search(r"Known=\{([^}]*)\}", cs).group(1)
        self.assertEqual(tuple(re.findall(r'"([^"]+)"', known)), oi.HOST_KNOWN)
        placeholders = re.search(r"Placeholders=\{([^}]*)\}", cs).group(1)
        self.assertEqual(tuple(re.findall(r'"([^"]+)"', placeholders)), oi.PLACEHOLDERS)
        prefixes = re.search(r"RootPrefixes=\{([^}]*)\}", cs).group(1)
        self.assertEqual(tuple(re.findall(r'"([^"]+)"', prefixes)), oi.ROOT_PREFIXES)
        self.assertIn('Schema="' + oi.HOST_SCHEMA + '"', cs)
        self.assertIn("uid<5000||uid>5999", cs)
        self.assertIn("home<1||home>0x2ff", cs)
        self.assertIn("paths.Count<1||paths.Count>8", cs)
        self.assertIn("MaxApps=200", cs)
        self.assertIn('IptvAppId="quiet.iptv"', cs)
        for c in oi.PLACEHOLDER_CHARS:
            self.assertIn("'" + c + "'", cs)
        for sid in oi.SYSTEM_LAUNCH_IDS:
            self.assertIn('"' + sid + '"', cs)

    def test_public_example_shape(self):
        example = json.loads((REPO / "launcher" / "host" / "host-config.example.json").read_text())
        with self.assertRaises(oi.OwnerInitError):  # placeholders are refused
            oi.validate_host_config(example)
        filled = dict(example, runtimeRoot="/home/owner/share/owner-a/quiet-tv", ownAppId="Qa7Rt2Lm9X.QuietTV",
                      observedInputPaths=["/dev/input/event3"])
        self.assertEqual(oi.validate_host_config(filled), filled)
        for over in (dict(extra=1), dict(ownerUid=5001.0), dict(remoteGrabEnabled=True),
                     dict(ownAppId="Qa7Rt2Lm9X.Quiet-TV"), dict(iptvEnabled="false")):
            with self.subTest(over=over), self.assertRaises(oi.OwnerInitError):
                oi.validate_host_config(dict(filled, **over))


if __name__ == "__main__":
    unittest.main()

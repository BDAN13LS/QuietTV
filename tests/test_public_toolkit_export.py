from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_public_toolkit_fixtures as fx  # noqa: E402
from tvkit import cli  # noqa: E402
from tvkit import export as ex  # noqa: E402
from tvkit.config import PROJECT_REPO_URL, ConfigError, load_config, require, unconfigured  # noqa: E402

# Planted secrets are assembled at runtime so this file never contains them literally.
MAC = ":".join(["0a", "1b", "2c", "3d", "4e", "5f"])
MAC_DASHED = MAC.replace(":", "-").upper()
LAN_IP = ".".join(["192", "168", "7", "42"])
PEM = "-----BEGIN " + "RSA PRIVATE KEY-----\nabc\n"
WIN_PATH = "C:" + "\\" + "Users" + "\\" + "someone" + "\\" + "secret.txt"
EMAIL = "someone" + "@" + "mail-provider.test"
TOWN = "Spring" + "fieldton"
POSTCODE = "9" + "8765"
OWNER = "Jamie" + "Q" + "Example"


class ExportCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.src = self.tmp / "src"
        (self.src / "kit" / "lib").mkdir(parents=True)
        (self.src / "kit" / "lib" / "a.py").write_text("print('hello')\n", encoding="utf-8")
        (self.src / "kit" / "README.md").write_text("Contact: maintainer@example.com\n", encoding="utf-8")
        (self.src / "kit" / "logo.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>\n", encoding="utf-8")
        (self.src / "LICENSE-UPSTREAM.txt").write_text("MIT License\n", encoding="utf-8")
        (self.src / "secret-stuff").mkdir()
        (self.src / "secret-stuff" / "keys.txt").write_text(PEM, encoding="utf-8")
        self.allow = {"schema": "tvkit-export-allowlist/1",
                      "include": ["kit/", "LICENSE-UPSTREAM.txt"], "exclude": [], "notExported": []}

    def tearDown(self):
        self._tmp.cleanup()

    def allowlist(self, **changes):
        data = {**self.allow, **changes}
        path = self.tmp / "allow.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return ex.load_allowlist(path)

    def deny_file(self, *values):
        path = self.tmp / "deny.txt"
        path.write_text("# owner-only\n" + "\n".join(values) + "\n", encoding="utf-8")
        return path

    def report(self, deny=(), **changes):
        return ex.build_report(self.src, self.allowlist(**changes), deny)

    def rules(self, report):
        return sorted({v.rule for v in report.violations})

    def plant(self, relpath, content, encoding="utf-8"):
        path = self.src / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding=encoding)
        return path


class Positive(ExportCase):
    def test_clean_list_and_write(self):
        report = self.report()
        self.assertEqual(report.violations, [])
        self.assertEqual([p for p, _, _ in report.files],
                         ["LICENSE-UPSTREAM.txt", "kit/README.md", "kit/lib/a.py", "kit/logo.svg"])
        out = self.tmp / "export"
        digest = ex.write_export(self.src, report, out)
        manifest = (out / ex.MANIFEST_NAME).read_bytes()
        self.assertEqual(hashlib.sha256(manifest).hexdigest(), digest)
        listed = json.loads(manifest)["files"]
        for item in listed:
            data = (out / item["path"]).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), item["sha256"])
        written = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
        self.assertEqual(written, sorted([i["path"] for i in listed] + [ex.MANIFEST_NAME]))
        self.assertFalse((out / "secret-stuff").exists())

    def test_declared_exclusion_is_reported_not_silent(self):
        report = self.report(exclude=[{"path": "kit/logo.svg", "reason": "brand art pending licence check"}])
        self.assertEqual(report.excluded, [("kit/logo.svg", "brand art pending licence check")])
        self.assertNotIn("kit/logo.svg", [p for p, _, _ in report.files])

    def test_generated_cache_ignored_and_counted(self):
        self.plant("kit/lib/__pycache__/a.cpython-312.pyc", b"\x00cache")
        report = self.report()
        self.assertEqual(report.violations, [])
        self.assertEqual(report.ignored_generated, 1)

    def test_glob_include(self):
        self.plant("tests/test_kit_one.py", "x = 1\n")
        self.plant("tests/test_other.py", "x = 2\n")
        report = self.report(include=["tests/test_kit_*.py"])
        self.assertEqual([p for p, _, _ in report.files], ["tests/test_kit_one.py"])


class PlantedSecrets(ExportCase):
    def assertRefused(self, report, rule, *secrets):
        self.assertIn(rule, self.rules(report))
        out, err = io.StringIO(), io.StringIO()
        self.assertTrue(report.violations)
        text = json.dumps([vars(v) for v in report.violations])
        for secret in secrets:
            self.assertNotIn(secret, text)

    def test_builtin_patterns(self):
        cases = [
            ("kit/cfg.json", '{"mac": "%s"}' % MAC, "mac-address", MAC),
            ("kit/cfg2.json", '{"mac": "%s"}' % MAC_DASHED, "mac-address", MAC_DASHED),
            ("kit/net.txt", "host=" + LAN_IP + "\n", "private-ipv4", LAN_IP),
            ("kit/k.txt", PEM, "pem-block", "PRIVATE"),
            ("kit/p.txt", "path " + WIN_PATH, "windows-user-path", "someone"),
            ("kit/p2.txt", "path " + WIN_PATH.replace("\\", "\\\\"), "windows-user-path", "someone"),
            ("kit/e.txt", "mail " + EMAIL, "email-address", EMAIL),
        ]
        for relpath, content, rule, secret in cases:
            with self.subTest(rule=rule, relpath=relpath):
                planted = self.plant(relpath, content)
                self.assertRefused(self.report(), rule, secret)
                planted.unlink()

    def test_utf16_and_image_bytes_scanned(self):
        self.plant("kit/w.txt", "ip " + LAN_IP, encoding="utf-16-le")
        self.assertRefused(self.report(), "private-ipv4", LAN_IP)
        (self.src / "kit" / "w.txt").unlink()
        # A malformed PNG is refused outright; a private chunk survives the filter and is scanned.
        png = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x20tEXtComment\x00" + WIN_PATH.encode() + b"\x00" * 8
        self.plant("kit/shot.png", png)
        self.assertTrue(any(r.startswith("transform-refused: PNG refused") for r in self.rules(self.report())))
        self.plant("kit/shot.png", fx.png(extra=[(b"prVt", b"path " + WIN_PATH.encode())]))
        self.assertRefused(self.report(), "windows-user-path")

    def test_unfiltered_image_formats_refused(self):
        for name in ("kit/photo.jpg", "kit/photo.JPEG", "kit/a.webp", "kit/b.heic", "kit/c.gif"):
            with self.subTest(name=name):
                planted = self.plant(name, b"\xff\xd8\xff")
                self.assertTrue(any(r.startswith("image-format-not-filtered") for r in self.rules(self.report())))
                planted.unlink()

    def test_deny_values_refused_without_printing(self):
        deny = ex.load_deny_values(self.deny_file(TOWN, POSTCODE, OWNER, MAC))
        cases = [
            ("kit/weather.js", f"const city = '{TOWN.lower()}';\n"),
            ("kit/zip.txt", f"zip={POSTCODE}\n"),
            ("kit/author.md", f"by {OWNER}\n"),
            ("kit/mac2.txt", MAC.replace(":", "").upper()),
        ]
        for relpath, content in cases:
            with self.subTest(relpath=relpath):
                planted = self.plant(relpath, content)
                report = self.report(deny)
                self.assertTrue(any(r.startswith("deny-value-#") for r in self.rules(report)))
                text = json.dumps([vars(v) for v in report.violations])
                for value in (TOWN, POSTCODE, OWNER, MAC):
                    self.assertNotIn(value, text)
                planted.unlink()

    def test_digit_deny_value_needs_digit_boundaries(self):
        deny = ex.load_deny_values(self.deny_file(POSTCODE))
        self.plant("kit/n.txt", "version 1" + POSTCODE + "2\n")
        self.assertEqual(self.report(deny).violations, [])

    def test_cli_never_prints_deny_values(self):
        self.plant("kit/weather.js", f"const city = '{TOWN}';\n")
        allow = self.tmp / "allow.json"
        allow.write_text(json.dumps(self.allow), encoding="utf-8")
        deny = self.deny_file(TOWN)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["export", "--allowlist", str(allow), "--source", str(self.src),
                             "--deny-values-file", str(deny), "--out", str(self.tmp / "o")])
        self.assertEqual(code, 1)
        self.assertNotIn(TOWN, out.getvalue() + err.getvalue())
        self.assertIn("REFUSED  kit/weather.js: deny-value-#1", out.getvalue())
        self.assertFalse((self.tmp / "o").exists())

    def test_short_deny_value_rejected(self):
        with self.assertRaises(ex.ExportError):
            ex.load_deny_values(self.deny_file("ab"))


class ForbiddenFiles(ExportCase):
    def test_forbidden_paths_and_extensions_refuse_export(self):
        cases = [
            ("kit/.git/config", "forbidden-directory:.git"),
            ("kit/private/notes.md", "forbidden-directory:private"),
            ("kit/.claude/settings.json", "forbidden-directory:.claude"),
            ("kit/build/out.js", "forbidden-directory:build"),
            ("kit/dist/app.js", "forbidden-directory:dist"),
            ("kit/Helper.dll", "forbidden-extension:.dll"),
            ("kit/tool.exe", "forbidden-extension:.exe"),
            ("kit/app.wgt", "forbidden-extension:.wgt"),
            ("kit/author.p12", "forbidden-extension:.p12"),
            ("kit/device.log", "forbidden-extension:.log"),
            ("kit/backup.tar", "forbidden-extension:.tar"),
            ("kit/core.dump", "forbidden-extension:.dump"),
            ("kit/capsule.json", "forbidden-file-name"),
        ]
        for relpath, rule in cases:
            with self.subTest(relpath=relpath):
                planted = self.plant(relpath, "x")
                report = self.report()
                self.assertIn(rule, self.rules(report))
                with self.assertRaises(ex.ExportError):
                    ex.write_export(self.src, report, self.tmp / "never")
                self.assertFalse((self.tmp / "never").exists())
                planted.unlink()

    def test_device_facts_and_filled_config_refused(self):
        facts = self.plant("kit/facts.json", json.dumps({**fx.FACTS, "deviceId": "SERIAL" + "123"}))
        self.assertIn("device-facts-file", self.rules(self.report()))
        facts.unlink()
        template = json.loads((fx.TOOLKIT / "config" / "tvkit.config.example.json").read_text(encoding="utf-8"))
        self.plant("kit/config/template.json", json.dumps(template))
        self.assertEqual(self.report().violations, [])
        for key, value in (("capsuleStore", "\\\\nas\\share\\tv"), ("transport", "my-adapter")):
            planted = self.plant("kit/config/mine.json", json.dumps({**template, key: value}))
            with self.subTest(key=key):
                self.assertIn("filled-user-config", self.rules(self.report()))
            planted.unlink()

    def test_missing_allowlisted_path_and_dead_exclude(self):
        report = self.report(include=["kit/", "nope.txt"],
                             exclude=[{"path": "kit/gone.md", "reason": "x"}])
        self.assertEqual(self.rules(report), ["allowlisted-path-missing", "exclude-entry-matched-nothing"])

    def test_symlink_refused(self):
        try:
            os.symlink(self.src / "secret-stuff", self.src / "kit" / "link")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not permitted for this user")
        self.assertIn("symbolic-link", self.rules(self.report()))

    def test_allowlist_rules(self):
        for bad in (["../outside"], ["/abs"], ["C:/x"], ["a\\b"], ["kit/", "kit/"]):
            with self.subTest(bad=bad), self.assertRaises(ex.ExportError):
                self.allowlist(include=bad)

    def test_output_folder_rules(self):
        report = self.report()
        with self.assertRaises(ex.ExportError):
            ex.write_export(self.src, report, self.src / "export-inside")
        busy = self.tmp / "busy"
        busy.mkdir()
        (busy / "x").write_text("x", encoding="utf-8")
        with self.assertRaises(ex.ExportError):
            ex.write_export(self.src, report, busy)
        with self.assertRaises(ex.ExportError):
            ex.write_export(self.src, report, self.tmp)  # contains the source

    def test_file_changed_after_scan(self):
        report = self.report()
        (self.src / "kit" / "lib" / "a.py").write_text("print('changed " + LAN_IP + "')\n", encoding="utf-8")
        with self.assertRaises(ex.ExportError):
            ex.write_export(self.src, report, self.tmp / "o")


class ShippedFiles(unittest.TestCase):
    """The real toolkit: allowlist passes the generic scan, templates are placeholders."""

    def test_real_allowlist_is_clean(self):
        allowlist = ex.load_allowlist(fx.TOOLKIT / "export-allowlist.json")
        report = ex.build_report(fx.ROOT, allowlist)
        self.assertEqual([(v.path, v.rule) for v in report.violations], [])
        paths = [p for p, _, _ in report.files]
        self.assertIn("public-toolkit/tvkit/capsule.py", paths)
        self.assertIn("THIRD-PARTY-NOTICES.md", paths)
        self.assertFalse([p for p in paths if p.startswith(("private/", "boot/", "frontend/", "vendor/", ".git"))])
        self.assertTrue(report.not_exported)

    def test_templates_fail_closed(self):
        config = load_config(fx.TOOLKIT / "config" / "tvkit.config.example.json")
        self.assertEqual(unconfigured(config), ["capsuleStore", "transport"])
        self.assertEqual(require(config, "releaseRepoUrl"), PROJECT_REPO_URL)
        self.assertEqual(PROJECT_REPO_URL, "https://github.com/BDAN13LS/QuietTV")
        for key in ("capsuleStore", "transport"):
            with self.subTest(key=key), self.assertRaises(ConfigError):
                require(config, key)
        with self.assertRaises(ConfigError):
            require({**config, "releaseRepoUrl": "<CONFIGURE: x>"}, "releaseRepoUrl")
        for url in ("http://github.com/a/b", "https://evil.example/a/b", "https://github.com/a/b?t=1"):
            with self.subTest(url=url), self.assertRaises(ConfigError):
                require({**config, "releaseRepoUrl": url}, "releaseRepoUrl")
        self.assertEqual(require({**config, "releaseRepoUrl": "https://github.com/o/r"}, "releaseRepoUrl"),
                         "https://github.com/o/r")

    def test_setup_script_has_no_download_and_execute(self):
        text = (fx.TOOLKIT / "Setup.ps1").read_text(encoding="utf-8").lower()
        for banned in ("iex", "invoke-expression", "downloadstring", "invoke-webrequest", "invoke-restmethod",
                       "irm ", "iwr ", "| python", "|python", "start-bitstransfer", "net.webclient"):
            self.assertNotIn(banned, text)

    def _pwsh(self):
        exe = shutil.which("pwsh") or shutil.which("powershell")
        if not exe:
            self.skipTest("PowerShell not installed")
        return exe

    def test_setup_script_parses(self):
        script = str(fx.TOOLKIT / "Setup.ps1").replace("'", "''")
        command = ("$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile("
                   f"'{script}', [ref]$null, [ref]$e); if ($e.Count) {{ $e | ForEach-Object {{ $_.Message }}; exit 1 }}")
        result = subprocess.run([self._pwsh(), "-NoProfile", "-NonInteractive", "-Command", command],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_setup_release_url_fails_closed_and_doctor_runs(self):
        exe = self._pwsh()
        script = str(fx.TOOLKIT / "Setup.ps1")
        result = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-File", script, "release-url"],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), PROJECT_REPO_URL + "/releases")
        with tempfile.TemporaryDirectory() as tmp:
            for value in ("<CONFIGURE: https://github.com/OWNER/REPO>", "http://github.com/a/b",
                          "https://github.com/a/b?x=1"):
                cfg = Path(tmp) / "cfg.json"
                cfg.write_text(json.dumps({"releaseRepoUrl": value}), encoding="utf-8")
                with self.subTest(value=value):
                    result = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-File", script, "release-url",
                                             "-Config", str(cfg)], capture_output=True, text=True, timeout=120)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("not configured", result.stdout + result.stderr)
        result = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-File", script, "doctor"],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("NOT CONFIGURED", result.stdout)


if __name__ == "__main__":
    unittest.main()

"""helper/build_widget.py: deterministic UNSIGNED preview WGT, exact inventory, no leaks, owner storage only.

Dist fixtures are generated in a temp folder per test. Tokens are SYNTHETIC test strings.
Run: python -B -m unittest tests.test_widget_build
"""
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "helper"))
sys.path.insert(0, str(REPO / "tests"))
import build_widget as bw  # noqa: E402
import owner_init as oi  # noqa: E402
from test_owner_init import SYN_TOKEN, inputs, make_junction  # noqa: E402

# A built frontend dist for the optional smoke test: REPO/frontend/dist, or QUIETTV_FRONTEND_DIST for a private check.
FRONTEND_DIST = Path(os.environ.get("QUIETTV_FRONTEND_DIST") or REPO / "frontend" / "dist")
INDEX = ('<!doctype html><html><head><meta charset="UTF-8"><title>Quiet TV</title>'
         '<script type="module" crossorigin src="./assets/index-abc.js"></script>'
         '<link rel="stylesheet" crossorigin href="./assets/index-abc.css"></head>'
         '<body><div id="root"></div></body></html>')


def write_dist(root: Path, extra: dict | None = None, index: str = INDEX) -> Path:
    files = {"index.html": index, "assets/index-abc.js": "console.log('dist');\n",
             "assets/index-abc.css": "body{background:url(./bg.png)}.x{background:url(data:image/png;base64,AA==)}\n",
             "assets/bg.png": b"\x89PNG\r\n\x1a\nfixture"}
    files.update(extra or {})
    for name, data in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data if isinstance(data, bytes) else data.encode())
    return root


class WidgetBuildTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.dist = write_dist(self.tmp / "dist")
        self.owner = self.tmp / "owner"
        oi.generate(self.owner, **inputs())

    def tearDown(self):
        self._tmp.cleanup()

    def build(self, name="out.wgt", dist=None, owner=None, **kw):
        return bw.build(dist or self.dist, owner or self.owner, self.tmp / name, **kw)

    def names(self, wgt):
        with zipfile.ZipFile(wgt) as zf:
            return zf

    def test_exact_inventory_and_unsigned_preview_config(self):
        result = self.build()
        with zipfile.ZipFile(result["wgt"]) as zf:
            names = sorted(zf.namelist())
            self.assertEqual(names, sorted(["index.html", "assets/index-abc.js", "assets/index-abc.css", "assets/bg.png",
                                            "config.xml", "quiettv-widget-config.js", "quiettv-manifest.json",
                                            "quiettv-bridge.js", "tv-inputs.js"]))
            for info in zf.infolist():
                self.assertEqual((info.date_time, info.create_system, info.compress_type),
                                 (bw.ZIP_TIME, 3, zipfile.ZIP_STORED))
            files = {n: zf.read(n) for n in names}
        xml = files["config.xml"].decode()
        self.assertIn('<tizen:application id="Qa7Rt2Lm9X.QuietTV" package="Qa7Rt2Lm9X"', xml)
        for absent in ("<author", "privilege/internet", "<access", "on-boot", "tvinputdevice", "<icon"):
            self.assertNotIn(absent, xml)
        self.assertTrue(files["index.html"].startswith(
            b'<!doctype html><html><head><script src="tv-inputs.js"></script>'
            b'<script src="quiettv-widget-config.js"></script><script src="quiettv-bridge.js"></script>'))
        manifest = json.loads(files["quiettv-manifest.json"])
        self.assertTrue(manifest["unsigned"])
        self.assertEqual({f["path"]: f["sha256"] for f in manifest["files"]},
                         {n: hashlib.sha256(d).hexdigest() for n, d in files.items() if n != "quiettv-manifest.json"})
        cfg = files["quiettv-widget-config.js"].decode()
        self.assertIn('"backend":{"enabled":false}', cfg)
        lines = cfg.splitlines()
        self.assertEqual(lines[0], 'window.QuietTVApps=Object.freeze([{"Icon":"","Id":"org.tizen.netflix-app","Name":"Netflix"},'
                                   '{"Icon":"","Id":"9Ur5IzDKqV.TizenYouTube","Name":"YouTube"}].map(Object.freeze));')
        self.assertEqual(lines[1], 'window.QuietTVConfig=Object.freeze({"iptvEnabled":false,"preview":true,"schema":"quiettv-widget/1"});')
        self.assertTrue(lines[2].startswith("window.QuietTVWidgetConfig=") and len(lines) == 3)
        self.assertNotIn("token", cfg)
        self.assertFalse(any("signature" in n.lower() or n.endswith((".pem", ".p12", ".xml")) and n != "config.xml"
                             for n in names))

    def test_same_inputs_same_bytes(self):
        a = self.build("a.wgt", stage_dir=self.tmp / "stage")
        b = self.build("b.wgt")
        self.assertEqual((self.tmp / "a.wgt").read_bytes(), (self.tmp / "b.wgt").read_bytes())
        self.assertEqual(a["sha256"], b["sha256"])
        with zipfile.ZipFile(self.tmp / "a.wgt") as zf:
            for name in zf.namelist():
                self.assertEqual((self.tmp / "stage" / name).read_bytes(), zf.read(name))

    def test_two_owners_differ_without_shared_secrets(self):
        other = self.tmp / "owner-b"
        oi.generate(other, **inputs(package_id="Zx9Kp3Wq7B", owner_uid=5002))
        fa, fb = bw.assemble(self.dist, self.owner), bw.assemble(self.dist, other)
        self.assertNotEqual(fa["config.xml"], fb["config.xml"])
        self.assertNotEqual(fa["quiettv-widget-config.js"], fb["quiettv-widget-config.js"])
        self.assertNotIn(b"Qa7Rt2Lm9X", fb["config.xml"])
        self.assertIn(b'"ownAppId":"Zx9Kp3Wq7B.QuietTV"', fb["quiettv-widget-config.js"])
        for name in ("assets/index-abc.js", "quiettv-bridge.js", "tv-inputs.js", "index.html"):
            self.assertEqual(fa[name], fb[name])

    def test_output_only_outside_repositories(self):
        for bad in (REPO / "preview.wgt", REPO / "helper" / "preview.wgt"):
            with self.assertRaisesRegex(oi.OwnerInitError, "repository"):
                bw.build(self.dist, self.owner, bad)
            self.assertFalse(bad.exists())
        repo = self.tmp / "release"
        repo.mkdir()
        (repo / ".git").write_text("gitdir: elsewhere")
        with self.assertRaisesRegex(oi.OwnerInitError, "repository"):
            bw.build(self.dist, self.owner, repo / "x.wgt")
        with self.assertRaisesRegex(oi.OwnerInitError, "repository"):
            self.build(stage_dir=REPO / "stage-should-not-exist")
        self.assertFalse((self.tmp / "out.wgt").exists())
        link = self.tmp / "link"
        if make_junction(link, repo):
            with self.assertRaisesRegex(oi.OwnerInitError, "repository"):
                bw.build(self.dist, self.owner, link / "x.wgt")
        with self.assertRaisesRegex(oi.OwnerInitError, ".wgt"):
            self.build("out.zip")
        self.build()  # positive control
        with self.assertRaisesRegex(oi.OwnerInitError, "already exists"):
            self.build()

    def test_on_boot_and_home_forbidden(self):
        cfg_path = self.owner / "widget-config.json"
        original = json.loads(cfg_path.read_text())
        for key, value in (("onBoot", True), ("registerAsHome", True), ("preview", False), ("iptvEnabled", True)):
            cfg_path.write_text(json.dumps(dict(original, **{key: value})))
            with self.subTest(key=key), self.assertRaises(oi.OwnerInitError):
                bw.assemble(self.dist, self.owner)
        cfg_path.write_text(json.dumps(original))
        bw.assemble(self.dist, self.owner)  # positive control
        template = (bw.WIDGET_SRC / bw.TEMPLATE).read_text().lower()
        for marker in bw.FORBIDDEN_XML[:-2]:
            self.assertNotIn(marker.lower(), template)

    def test_forbidden_xml_guard_catches_injection(self):
        saved = bw.WIDGET_SRC
        alt = self.tmp / "widget-src"
        alt.mkdir()
        for name in (*bw.WIDGET_FILES, bw.TEMPLATE):
            (alt / name).write_bytes((saved / name).read_bytes())
        text = (alt / bw.TEMPLATE).read_text()
        (alt / bw.TEMPLATE).write_text(text.replace("<tizen:profile", '<tizen:metadata key="on-boot" value="true"/><tizen:profile'))
        bw.WIDGET_SRC = alt
        try:
            with self.assertRaisesRegex(oi.OwnerInitError, "forbidden"):
                bw.assemble(self.dist, self.owner)
        finally:
            bw.WIDGET_SRC = saved

    def test_dist_inventory_refusals(self):
        cases = {
            "key": {"assets/k.js": "-----BEGIN " + "PRIVATE KEY-----\n"},  # split so the source scanner stays clean
            "pem": {"assets/cert.pem": "x"},
            "map": {"assets/index-abc.js.map": "{}"},
            "sig": {"author-signature.xml": "<x/>"},
            "dll": {"assets/x.dll": b"MZ"},
            "dotfile": {".env.js": "x"},
            "name": {"assets/bad name.js": "x"},
            "project": {"package.json": "{}"},
            "srcdir": {"src/main.js": "x"},
            "collide": {"config.xml": "<widget/>"},
            "bridge": {"quiettv-bridge.js": "x"},
        }
        for label, extra in cases.items():
            dist = write_dist(self.tmp / f"d-{label}", extra)
            with self.subTest(label=label), self.assertRaises(oi.OwnerInitError):
                bw.assemble(dist, self.owner)
        refs = {
            "parent": INDEX.replace("./assets/index-abc.js", "../index-abc.js"),
            "absolute": INDEX.replace("./assets/index-abc.js", "/assets/index-abc.js"),
            "remote": INDEX.replace("./assets/index-abc.js", "https://cdn.invalid/x.js"),
            "missing": INDEX.replace("./assets/index-abc.js", "./assets/nope.js"),
            "base": INDEX.replace("<title>", '<base href="./"><title>'),
            "twohead": INDEX + "<head>",
        }
        for label, index in refs.items():
            dist = write_dist(self.tmp / f"r-{label}", index=index)
            with self.subTest(label=label), self.assertRaises(oi.OwnerInitError):
                bw.assemble(dist, self.owner)
        with self.assertRaisesRegex(oi.OwnerInitError, "escapes"):  # refused as escaping, not merely missing
            bw.assemble(write_dist(self.tmp / "esc", index=INDEX.replace("./assets/index-abc.js",
                                                                         "assets/../../index.html")), self.owner)
        css = write_dist(self.tmp / "css", {"assets/index-abc.css": "a{background:url(../../secret.png)}"})
        with self.assertRaises(oi.OwnerInitError):
            bw.assemble(css, self.owner)
        with self.assertRaisesRegex(oi.OwnerInitError, "project root"):
            bw.assemble(REPO, self.owner)  # never stage from the project root

    def test_dist_links_refused(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "x.js").write_text("x")
        if not make_junction(self.dist / "assets" / "linked", outside):
            self.skipTest("cannot create a junction/symlink here")
        with self.assertRaisesRegex(oi.OwnerInitError, "link"):
            bw.assemble(self.dist, self.owner)

    def test_backend_enabled_token_only_in_config_js(self):
        owner = self.tmp / "owner-backend"
        oi.generate(owner, token_factory=lambda: SYN_TOKEN, **inputs(backend_port=47123))
        files = bw.assemble(self.dist, owner)
        result = bw.build(self.dist, owner, self.tmp / "token.wgt")
        if os.name == "nt":
            import subprocess
            self.assertNotIn("(I)", subprocess.run(["icacls", result["wgt"]], capture_output=True, text=True).stdout)
        else:
            self.assertEqual(os.stat(result["wgt"]).st_mode & 0o777, 0o600)
        holders = [n for n, d in files.items() if SYN_TOKEN.encode() in d]
        self.assertEqual(holders, ["quiettv-widget-config.js"])
        xml = files["config.xml"].decode()
        self.assertIn("privilege/internet", xml)
        self.assertIn('<access origin="http://127.0.0.1:47123" subdomains="false"/>', xml)
        leaky = write_dist(self.tmp / "leaky", {"assets/leak.js": "var t='" + SYN_TOKEN + "';"})
        with self.assertRaisesRegex(oi.OwnerInitError, "token leaked"):
            bw.assemble(leaky, owner)
        # the default (disabled) build never carries a token or network privilege
        plain = bw.assemble(self.dist, self.owner)
        self.assertFalse(any(SYN_TOKEN.encode() in d for d in plain.values()))

    def test_widget_sources_are_lf_normalised(self):
        files = bw.assemble(self.dist, self.owner)
        for name in (*bw.WIDGET_FILES, "config.xml"):
            self.assertNotIn(b"\r\n", files[name])

    @unittest.skipUnless((FRONTEND_DIST / "index.html").is_file(), "no built frontend dist (run the frontend build first)")
    def test_smoke_real_frontend_dist(self):
        result = bw.build(FRONTEND_DIST, self.owner, self.tmp / "frontend.wgt")
        with zipfile.ZipFile(result["wgt"]) as zf:
            index = zf.read("index.html")
            self.assertIn(b'<script src="quiettv-bridge.js"></script>', index)
            self.assertFalse(any(n.endswith(".map") for n in zf.namelist()))


WIDGET_EXPORT_FILES = ("helper/owner_init.py", "helper/build_widget.py", "launcher/widget/README.md",
                       "launcher/widget/config.xml.template", "launcher/widget/quiettv-bridge.js",
                       "launcher/widget/tv-inputs.js", "tests/test_owner_init.py", "tests/test_widget_build.py",
                       "tests/test_widget_bridge.py")
OWNED_SUITES = ("test_owner_init.py", "test_widget_build.py", "test_widget_bridge.py")


class ExportSmokeTests(unittest.TestCase):
    """The toolkit export ships exactly these 9 files and their suites run in a fresh copy outside the checkout."""

    def setUp(self):
        sys.path.insert(0, str(REPO / "public-toolkit"))
        from tvkit import export as ex
        self.ex = ex

    def run_suites(self, root: Path):
        # -I: no user site or environment imports; the run sees only the exported copy (no private overrides).
        tests = root / "tests"
        env = {k: v for k, v in os.environ.items() if not k.startswith("QUIETTV_")}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return [subprocess.run([sys.executable, "-I", "-m", "unittest", "discover", "-s", str(tests), "-t", str(tests),
                                "-p", suite], cwd=str(tests), capture_output=True, text=True, timeout=300, env=env)
                for suite in OWNED_SUITES]

    def test_export_ships_widget_sources_and_their_suites_pass(self):
        if (REPO / self.ex.MANIFEST_NAME).exists():  # already an exported tree: check it in place
            for rel in WIDGET_EXPORT_FILES:
                self.assertTrue((REPO / rel).is_file(), rel)
            return
        allow = self.ex.load_allowlist(REPO / "public-toolkit" / "export-allowlist.json")
        self.assertEqual(sorted(e for e in allow.include if e.startswith(("helper/", "launcher/widget/")))
                         + sorted(e for e in allow.include if e.startswith(("tests/test_owner", "tests/test_widget"))),
                         sorted(WIDGET_EXPORT_FILES[:6]) + sorted(WIDGET_EXPORT_FILES[6:]))
        report = self.ex.build_report(REPO, allow)
        self.assertEqual([(v.path, v.rule) for v in report.violations], [])
        shipped = {p for p, _, _ in report.files}
        self.assertEqual(sorted(p for p in shipped if p.startswith(("helper/", "launcher/widget/"))),
                         sorted(WIDGET_EXPORT_FILES[:6]))  # nothing else from helper/ (e.g. helper/d0)
        with tempfile.TemporaryDirectory() as tmp:  # fresh folder outside the checkout, no deny/owner inputs
            out = Path(tmp) / "export"
            self.ex.write_export(REPO, report, out)
            for rel in WIDGET_EXPORT_FILES:
                self.assertEqual((out / rel).read_bytes(), (REPO / rel).read_bytes(), rel)
            self.assertFalse((out / "frontend").exists())
            for result in self.run_suites(out):
                self.assertEqual(result.returncode, 0, result.stderr[-3000:])
                self.assertRegex(result.stderr, r"Ran [1-9]\d* tests")
            # Negative control: the exported copy without the generator cannot run its suites.
            (out / "helper" / "owner_init.py").unlink()
            self.assertTrue(all(r.returncode != 0 for r in self.run_suites(out)))


if __name__ == "__main__":
    unittest.main()

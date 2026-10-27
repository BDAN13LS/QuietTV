from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_public_toolkit_fixtures as fx  # noqa: E402
from tvkit import export as ex  # noqa: E402
from tvkit import frontend_export as fe  # noqa: E402
from tvkit import pngmeta  # noqa: E402

# Planted values are assembled at runtime so this file never contains them literally.
EMAIL = "signer" + "@" + "provenance-vendor.test"
TOWN = "Spring" + "fieldton"
POSTCODE = "9" + "8765"
REGION = "Ex" + "ampleshire"
MARKER = "<!-- quiettv:public -->"


def idat_chunks(data: bytes) -> list[bytes]:
    return [c.raw for c in pngmeta.parse_chunks(data) if c.type == b"IDAT"]


def weather_source(newline: str = "\n") -> str:
    lines = [
        "// Weather module (synthetic test copy).",
        "export const WEATHER_LIMITS=Object.freeze({refreshMs:1});",
        f"export const DEFAULT_WEATHER=Object.freeze({{enabled:true,zip:'{POSTCODE}',unit:'F'}});",
        f"// Resolved by the geocoding API for {POSTCODE} on 1 Jan 2026. Seeds the sunrise fallback.",
        f"export const DEFAULT_PLACE=Object.freeze({{zip:'{POSTCODE}',name:'{TOWN}',region:'{REGION}',"
        "latitude:40.1,longitude:-80.2});",
        "export function loadWeatherSettings(raw){",
        " const value=raw&&typeof raw==='object'?raw:{};",
        " return {enabled:value.enabled!==false,zip:value.zip||DEFAULT_WEATHER.zip,unit:'F'};",
        "}",
        "",
    ]
    return newline.join(lines)


MAIN_SOURCE = ("import netflix from './assets/brand-netflix.svg';"
               "import prime from './assets/Prime-Video.png';import philo from './assets/Philo.png';"
               "import iptv from './assets/iptv.svg';\n"
               "const iconMap={'org.tizen.netflix-app':netflix,'org.tizen.primevideo':prime,"
               "'EYm8vc1St4.Philo':philo,'quiet.iptv':iptv};\n"
               "const kind=id=>id==='org.tizen.primevideo'?'prime':'other';\n")


class PngFilter(unittest.TestCase):
    def test_metadata_and_email_removed_image_bytes_identical(self):
        text = [(b"tEXt", b"Author\x00" + EMAIL.encode()),
                (b"zTXt", b"Comment\x00\x00" + zlib.compress(EMAIL.encode())),
                (b"iTXt", b"Description\x00\x00\x00en\x00\x00" + EMAIL.encode()),
                (b"eXIf", b"MM\x00*" + EMAIL.encode())]
        source = fx.png(before_idat=text, extra=[(b"caBX", b"jumb c2pa " + EMAIL.encode())])
        self.assertIn(EMAIL.encode(), source)
        out, removed = pngmeta.strip_metadata(source)
        self.assertEqual(removed, ["tEXt", "zTXt", "iTXt", "eXIf", "caBX"])
        self.assertNotIn(EMAIL.encode(), out)
        self.assertNotIn(b"signer", out)
        self.assertEqual(idat_chunks(out), idat_chunks(source))
        self.assertEqual(b"".join(c.data for c in pngmeta.parse_chunks(out) if c.type == b"IDAT"), fx.PNG_IDAT)
        self.assertEqual([c.type for c in pngmeta.parse_chunks(out)], [b"IHDR", b"IDAT", b"IDAT", b"IEND"])
        self.assertEqual(ex.scan_bytes(out), [])
        self.assertIn("email-address", ex.scan_bytes(source))

    def test_everything_else_is_byte_for_byte(self):
        keep = [(b"gAMA", b"\x00\x00\xb1\x8f"), (b"pHYs", b"\x00" * 9), (b"tIME", b"\x07\xea\x01\x01\x00\x00\x00")]
        source = fx.png(before_idat=keep + [(b"tEXt", b"Software\x00x")])
        out, _ = pngmeta.strip_metadata(source)
        self.assertEqual(out, fx.png(before_idat=keep))

    def test_copyright_notice_kept(self):
        notice = (b"tEXt", b"Copyright\x00(c) 2026 Example Studio")
        source = fx.png(before_idat=[notice, (b"tEXt", b"Comment\x00drop me")])
        out, removed = pngmeta.strip_metadata(source)
        self.assertIn(fx.png_chunk(*notice), out)
        self.assertNotIn(b"drop me", out)
        self.assertEqual(removed, ["tEXt"])

    def test_damaged_files_refused(self):
        good = fx.png()
        ihdr_end = 8 + 12 + 13
        bad_crc = bytearray(good)
        bad_crc[ihdr_end - 1] ^= 0x01
        idat_flip = bytearray(fx.png())
        idat_flip[ihdr_end + 8] ^= 0x01  # first IDAT data byte, CRC left stale
        cases = {
            "IHDR CRC": bytes(bad_crc),
            "IDAT CRC": bytes(idat_flip),
            "signature": b"\x89PNX" + good[4:],
            "truncated": good[:-6],
            "trailing bytes": fx.png(tail=b"extra"),
            "IHDR not first": b"\x89PNG\r\n\x1a\n" + fx.png_chunk(b"gAMA", b"\x00\x00\xb1\x8f") + good[8:],
            "IEND missing": good[:-12],
            "two IEND": good + fx.png_chunk(b"IEND", b""),
            "no image data": b"\x89PNG\r\n\x1a\n" + good[8:ihdr_end] + fx.png_chunk(b"IEND", b""),
            "bad type": good[:ihdr_end] + fx.png_chunk(b"I D1", b"x") + good[ihdr_end:],
        }
        for label, data in cases.items():
            with self.subTest(label=label), self.assertRaises(pngmeta.PngError):
                pngmeta.strip_metadata(data)


class WeatherTransform(unittest.TestCase):
    def test_personal_defaults_become_unconfigured(self):
        for newline in ("\n", "\r\n"):
            with self.subTest(newline=repr(newline)):
                out = fe.weather_unconfigured(weather_source(newline).encode()).decode()
                for value in (POSTCODE, TOWN, REGION, "40.1", "-80.2"):
                    self.assertNotIn(value, out)
                self.assertIn("export const DEFAULT_WEATHER=Object.freeze({enabled:false,zip:'',unit:'F'});"
                              + newline, out)
                self.assertIn("zip:null", out)
                self.assertIn("typeof value.enabled==='boolean'?value.enabled:DEFAULT_WEATHER.enabled", out)
                self.assertEqual(out.count(newline), weather_source(newline).count(newline))

    def test_anchor_must_match_exactly_once(self):
        source = weather_source()
        doubled = source.replace("export function", "export const DEFAULT_WEATHER=Object.freeze({zip:'1'});\n"
                                 "export function", 1)
        moved = source.replace("export const DEFAULT_PLACE=", "export let DEFAULT_PLACE=")
        for label, text in (("doubled", doubled), ("moved", moved),
                            ("enabled rewritten", source.replace("value.enabled!==false", "!!value.enabled"))):
            with self.subTest(label=label), self.assertRaises(fe.TransformError):
                fe.weather_unconfigured(text.encode())

    def test_brand_png_imports_dropped(self):
        out = fe.drop_extracted_brand_icons(MAIN_SOURCE.encode()).decode()
        self.assertNotIn(".png", out)
        self.assertNotIn(":prime", out)
        self.assertIn("'org.tizen.netflix-app':netflix,'quiet.iptv':iptv", out)
        self.assertIn("?'prime':", out)  # a CSS kind name, not the image
        with self.assertRaises(fe.TransformError):
            fe.drop_extracted_brand_icons((MAIN_SOURCE + "const x=prime;\n").encode())
        with self.assertRaises(fe.TransformError):
            fe.drop_extracted_brand_icons(MAIN_SOURCE.replace("import philo", "import philoArt").encode())


class FrontendExport(unittest.TestCase):
    """A synthetic frontend tree exported through a synthetic spec."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.src = self.tmp / "src"
        fr = self.src / "web"
        self.files = {
            "package.json": b'{"name":"launcher"}\n',
            "index.html": b'<script type="module" src="/src/main.jsx"></script>\n',
            "src/main.jsx": MAIN_SOURCE.encode() + b"import {x} from './weather.mjs';\n",
            "src/weather.mjs": weather_source().encode(),
            "src/assets/brand-netflix.svg": b"<svg xmlns='http://www.w3.org/2000/svg'/>\n",
            "src/assets/iptv.svg": b"<svg xmlns='http://www.w3.org/2000/svg'/>\n",
            "src/assets/wall.png": fx.png(extra=[(b"caBX", b"c2pa " + EMAIL.encode())]),
            "src/assets/Prime-Video.png": fx.png(),
            "src/assets/Philo.png": fx.png(),
            "public/native-icons/org.tizen.netflix-app.png": fx.png(),
            "public/native-icons/x.y.png": fx.png(),
            "tests/policy.test.mjs": b"import '../src/main.jsx';const bad='../../bin/sh';\n",
        }
        for rel, data in self.files.items():
            path = fr / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (self.src / "kit").mkdir()
        (self.src / "kit" / "a.py").write_text("x = 1\n", encoding="utf-8")
        self.spec_data = {
            "schema": fe.SPEC_SCHEMA, "root": "web",
            "include": ["package.json", "index.html", "src/main.jsx", "src/weather.mjs",
                        "src/assets/brand-netflix.svg", "src/assets/iptv.svg", "src/assets/wall.png",
                        "tests/policy.test.mjs"],
            "exclude": [{"path": "public/native-icons/", "reason": "extracted from the TV"},
                        {"path": "node_modules/", "reason": "installed packages"},
                        {"path": "src/assets/Prime-Video.png", "reason": "extracted"},
                        {"path": "src/assets/Philo.png", "reason": "extracted"}],
            "transforms": {"src/weather.mjs": "weather-unconfigured", "src/main.jsx": "drop-extracted-brand-icons"},
            "allowedMissingRefs": [{"file": "tests/policy.test.mjs", "ref": "../../bin/sh", "reason": "hostile id"}],
        }
        self.allow = {"schema": ex.ALLOWLIST_SCHEMA, "include": ["kit/"], "exclude": [],
                      "notExported": [{"path": "web/", "reason": "only with --with-frontend"}]}

    def tearDown(self):
        self._tmp.cleanup()

    def spec(self, **changes):
        path = self.tmp / "spec.json"
        path.write_text(json.dumps({**self.spec_data, **changes}), encoding="utf-8")
        return fe.load_spec(path)

    def allowlist(self, **changes):
        path = self.tmp / "allow.json"
        path.write_text(json.dumps({**self.allow, **changes}), encoding="utf-8")
        return ex.load_allowlist(path)

    def report(self, deny=(), spec=None, **changes):
        return ex.build_report(self.src, self.allowlist(**changes), deny, spec or self.spec())

    def rules(self, report):
        return sorted((v.path, v.rule) for v in report.violations)

    def test_clean_export_transformed_and_recorded(self):
        deny = ex.load_deny_values(self._deny(POSTCODE, TOWN, REGION, EMAIL))
        report = self.report(deny)
        self.assertEqual(self.rules(report), [])
        paths = [p for p, _, _ in report.files]
        self.assertFalse([p for p in paths if "native-icons" in p or p.endswith(("Prime-Video.png", "Philo.png"))])
        self.assertIn(("web/public/native-icons/", "extracted from the TV (2 files)"), report.excluded)
        self.assertIn(("web/node_modules/", "installed packages (not present)"), report.excluded)
        self.assertEqual(report.not_exported, [])  # the frontend entry is replaced by the real export
        out = self.tmp / "out"
        ex.write_export(self.src, report, out, deny)
        manifest = json.loads((out / ex.MANIFEST_NAME).read_text(encoding="utf-8"))
        items = {i["path"]: i for i in manifest["files"]}
        self.assertEqual(items["web/src/weather.mjs"]["transforms"], ["weather-unconfigured"])
        self.assertEqual(items["web/src/assets/wall.png"]["transforms"], ["png-metadata-strip"])
        self.assertEqual(items["web/src/weather.mjs"]["sourceSha256"],
                         hashlib.sha256(self.files["src/weather.mjs"]).hexdigest())
        self.assertNotIn("transforms", items["web/package.json"])
        for rel, item in items.items():
            data = (out / rel).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), item["sha256"])
            for value in (POSTCODE, TOWN, REGION, EMAIL):
                self.assertNotIn(value.encode(), data)
        wall = (out / "web/src/assets/wall.png").read_bytes()
        self.assertEqual(idat_chunks(wall), idat_chunks(self.files["src/assets/wall.png"]))
        self.assertFalse((out / "web/public").exists())

    def _deny(self, *values):
        path = self.tmp / "deny.txt"
        path.write_text("\n".join(values) + "\n", encoding="utf-8")
        return path

    def test_unaccounted_file_refused(self):
        (self.src / "web" / "src" / "notes.txt").write_text("todo\n", encoding="utf-8")
        self.assertIn(("web/src/notes.txt", "frontend-file-not-accounted"), self.rules(self.report()))

    def test_missing_included_file_refused(self):
        (self.src / "web" / "src" / "assets" / "iptv.svg").unlink()
        rules = self.rules(self.report())
        self.assertIn(("web/src/assets/iptv.svg", "allowlisted-path-missing"), rules)

    def test_broken_reference_refused(self):
        (self.src / "web" / "src" / "assets" / "Philo.png").unlink()
        spec = self.spec(include=self.spec_data["include"],
                         transforms={"src/weather.mjs": "weather-unconfigured"})
        rules = self.rules(self.report(spec=spec))
        self.assertIn(("web/src/main.jsx", "unresolved-reference:./assets/Prime-Video.png"), rules)
        spec = self.spec(allowedMissingRefs=[{"file": "tests/policy.test.mjs", "ref": "../x", "reason": "r"}])
        rules = self.rules(self.report(spec=spec))
        self.assertIn(("web/tests/policy.test.mjs", "unresolved-reference:../../bin/sh"), rules)
        self.assertIn(("web/tests/policy.test.mjs", "allowed-missing-ref-unused"), rules)

    def test_reference_naming_a_secret_is_withheld(self):
        (self.src / "web" / "src" / "main.jsx").write_bytes(
            self.files["src/main.jsx"] + f"import f from './fixtures/{POSTCODE}.json';\n".encode())
        deny = ex.load_deny_values(self._deny(POSTCODE))
        report = self.report(deny)
        self.assertIn(("web/src/main.jsx", "unresolved-reference:<withheld>"), self.rules(report))
        self.assertNotIn(POSTCODE, json.dumps([vars(v) for v in report.violations]))

    def test_transform_drift_refused(self):
        weather = self.src / "web" / "src" / "weather.mjs"
        weather.write_text(weather_source().replace("export const DEFAULT_PLACE=", "export var DEFAULT_PLACE="),
                           encoding="utf-8")
        rules = self.rules(self.report())
        self.assertTrue(any(p == "web/src/weather.mjs" and r.startswith("transform-refused: DEFAULT_PLACE")
                            for p, r in rules))

    def test_source_changed_after_scan_refused(self):
        report = self.report()
        (self.src / "web" / "src" / "assets" / "wall.png").write_bytes(fx.png(extra=[(b"prVt", EMAIL.encode())]))
        with self.assertRaises(ex.ExportError):
            ex.write_export(self.src, report, self.tmp / "o")

    def test_spec_rules(self):
        for bad in ({"transforms": {"src/weather.mjs": "png-metadata-strip"}},
                    {"transforms": {"src/weather.mjs": "nope"}},
                    {"transforms": {"src/other.mjs": "weather-unconfigured"}},
                    {"include": ["src/*.mjs"]}, {"include": ["../x"]}, {"root": "/abs"},
                    {"include": ["a", "a"]}, {"extra": 1}):
            with self.subTest(bad=bad), self.assertRaises(fe.TransformError):
                self.spec(**bad)


class OptionalIncludes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.src = Path(self._tmp.name) / "src"
        (self.src / "kit").mkdir(parents=True)
        (self.src / "kit" / "a.py").write_text("x = 1\n", encoding="utf-8")
        self.allow = {"schema": ex.ALLOWLIST_SCHEMA, "include": ["kit/"], "exclude": [], "notExported": [],
                      "includeIfPresent": ["README.md", "public-llm/", "docs/screenshots/"],
                      "requireMarker": [{"path": "README.md", "marker": MARKER, "reason": "no public marker"}]}

    def tearDown(self):
        self._tmp.cleanup()

    def report(self, deny=()):
        path = Path(self._tmp.name) / "allow.json"
        path.write_text(json.dumps(self.allow), encoding="utf-8")
        return ex.build_report(self.src, ex.load_allowlist(path), deny)

    def test_absent_entries_listed(self):
        report = self.report()
        self.assertEqual(report.violations, [])
        self.assertEqual(report.absent_optional, ["README.md", "public-llm/", "docs/screenshots/"])
        self.assertEqual(report.manifest()["absentOptional"], ["README.md", "public-llm/", "docs/screenshots/"])

    def test_present_entries_scanned_and_shipped(self):
        (self.src / "public-llm" / "server").mkdir(parents=True)
        (self.src / "public-llm" / "server" / "main.py").write_text("print('mcp')\n", encoding="utf-8")
        (self.src / "docs" / "screenshots").mkdir(parents=True)
        (self.src / "docs" / "screenshots" / "home.png").write_bytes(
            fx.png(before_idat=[(b"tEXt", b"Comment\x00" + EMAIL.encode())]))
        (self.src / "docs" / "notes.md").write_text("private notes\n", encoding="utf-8")
        report = self.report()
        self.assertEqual(report.violations, [])
        paths = [p for p, _, _ in report.files]
        self.assertIn("public-llm/server/main.py", paths)
        self.assertIn("docs/screenshots/home.png", paths)
        self.assertNotIn("docs/notes.md", paths)
        self.assertEqual(report.origins["docs/screenshots/home.png"][1], ("png-metadata-strip",))
        (self.src / "public-llm" / "server" / "key.txt").write_text(
            "contact " + EMAIL + "\n", encoding="utf-8")
        self.assertIn(("public-llm/server/key.txt", "email-address"),
                      [(v.path, v.rule) for v in self.report().violations])
        (self.src / "docs" / "screenshots" / "phone.jpg").write_bytes(b"\xff\xd8\xff")
        self.assertIn(("docs/screenshots/phone.jpg", "image-format-not-filtered:.jpg"),
                      [(v.path, v.rule) for v in self.report().violations])

    def test_readme_needs_public_marker(self):
        readme = self.src / "README.md"
        readme.write_text(f"# Private workspace of {TOWN}\n", encoding="utf-8")
        report = self.report()
        self.assertEqual(report.violations, [])
        self.assertIn(("README.md", "no public marker"), report.excluded)
        self.assertNotIn("README.md", [p for p, _, _ in report.files])
        readme.write_text(f"{MARKER}\n# QuietTV\n", encoding="utf-8")
        report = self.report()
        self.assertIn("README.md", [p for p, _, _ in report.files])
        readme.write_text(f"{MARKER}\n# QuietTV by {TOWN}\n", encoding="utf-8")
        deny = ex.load_deny_values(self._deny(TOWN))
        self.assertIn(("README.md", "deny-value-#1"), [(v.path, v.rule) for v in self.report(deny).violations])

    def _deny(self, *values):
        path = Path(self._tmp.name) / "deny.txt"
        path.write_text("\n".join(values) + "\n", encoding="utf-8")
        return path

    def test_allowlist_rules(self):
        for bad in ({"includeIfPresent": ["docs/*.png"]}, {"includeIfPresent": ["kit/"]},
                    {"requireMarker": [{"path": "README.md", "marker": "short", "reason": "r"}]},
                    {"requireMarker": [{"path": "README.md", "marker": 12345678, "reason": "r"}]}):
            path = Path(self._tmp.name) / "allow.json"
            path.write_text(json.dumps({**self.allow, **bad}), encoding="utf-8")
            with self.subTest(bad=bad), self.assertRaises(ex.ExportError):
                ex.load_allowlist(path)


class RealFrontend(unittest.TestCase):
    """The shipped spec over the real frontend: read-only, written only to a temp folder.

    Inside an exported copy (EXPORT-MANIFEST.json at the root) the frontend is
    already transformed, so the shipped invariants are checked in place instead.
    """

    BRAND_PNGS = ("Netflix", "YouTube", "Prime-Video", "Philo")

    def test_real_frontend(self):
        if (fx.ROOT / ex.MANIFEST_NAME).exists():
            self.check_shipped(fx.ROOT / "frontend")
            return
        spec = fe.load_spec(fx.TOOLKIT / "frontend-export.json")
        allowlist = ex.load_allowlist(fx.TOOLKIT / "export-allowlist.json")
        report = ex.build_report(fx.ROOT, allowlist, (), spec)
        self.assertEqual([(v.path, v.rule) for v in report.violations], [])
        paths = [p for p, _, _ in report.files]
        self.assertFalse([p for p in paths if "native-icons" in p or p.startswith(("frontend/dist/", "boot/",
                                                                                     "app/", "iptv/"))])
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "export"
            ex.write_export(fx.ROOT, report, out)
            for wall in ("day", "dusk", "night"):
                rel = f"frontend/src/assets/cinematic-{wall}.png"
                self.assertEqual(idat_chunks((out / rel).read_bytes()), idat_chunks((fx.ROOT / rel).read_bytes()))
            self.check_shipped(out / "frontend")

    def check_shipped(self, front: Path):
        self.assertFalse((front / "public" / "native-icons").exists())
        # A developer may have built the preview locally. Verify the shipped
        # manifest excludes outputs rather than rejecting a generated dist folder.
        manifest_path = front.parent / ex.MANIFEST_NAME
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            shipped = [item["path"] for item in manifest["files"]]
            self.assertFalse([name for name in shipped if name.startswith(
                ("frontend/dist/", "frontend/node_modules/", "frontend/public/native-icons/"))])
        for name in self.BRAND_PNGS:
            self.assertFalse((front / "src" / "assets" / f"{name}.png").exists(), name)
        pngs = sorted((front / "src" / "assets").glob("*.png"))
        self.assertEqual(len(pngs), 3)
        for path in pngs:
            chunks = pngmeta.parse_chunks(path.read_bytes())
            self.assertEqual([c.type for c in chunks if c.type in pngmeta.DROPPED_TYPES], [], path.name)
            self.assertNotIn(b"@", b"".join(c.data for c in chunks if c.type != b"IDAT"))
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed; transformed module behaviour not executed")
        check = ("import * as w from './src/weather.mjs';import {sunFor} from './src/appearance.mjs';"
                 "const s=w.loadWeatherSettings(null),c=w.loadWeatherCache(null),now=Date.now();"
                 "const r={s,due:w.dueForFetch({...s,cache:c,now}),place:w.placeFor(s.zip,c),"
                 "sun:sunFor(now,null,w.placeFor(s.zip,c)).source,view:w.weatherView({...s,cache:c,now}).status,"
                 "kept:w.loadWeatherSettings({enabled:true,zip:'12345',unit:'C'})};"
                 "console.log(JSON.stringify(r));")
        result = subprocess.run([node, "--input-type=module", "-e", check], cwd=front,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {
            "s": {"enabled": False, "zip": "", "unit": "F"}, "due": False, "place": None,
            "sun": "fixed", "view": "off", "kept": {"enabled": True, "zip": "12345", "unit": "C"}})
        tests = sorted(str(p.relative_to(front)) for p in (front / "tests").glob("*.test.mjs"))
        result = subprocess.run([node, "--test", *tests], cwd=front, capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stdout[-3000:] + result.stderr[-2000:])


if __name__ == "__main__":
    unittest.main()

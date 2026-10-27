"""QuietTV preview widget packager (D5b): deterministic, UNSIGNED .wgt from a sanitized dist + owner dir.

Inputs are explicit: a built, sanitized frontend dist root (no npm runs here) and an owner directory
made by helper/owner_init.py. Output is an unsigned WGT (and optionally a stage folder), both of which
must be new paths OUTSIDE every source/release repository. Same inputs give the same bytes.

The package is a normal app: no on-boot, no Home registration, no service, no signature files.
Only the launcher/widget/ files below and an exact, positive dist inventory are packed.

API:
    assemble(dist_root, owner_dir) -> dict[str, bytes]      (archive name -> bytes, nothing written)
    build(dist_root, owner_dir, out_wgt, stage_dir=None) -> dict  (paths + hashes, never the token)
CLI: python helper/build_widget.py --dist DIR --owner-dir DIR --out FILE.wgt [--stage DIR]
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import argparse
import hashlib
import io
import json
import os
import re
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parent))
import owner_init  # noqa: E402
from owner_init import OwnerInitError  # noqa: E402

WIDGET_SRC = owner_init.REPO_ROOT / "launcher" / "widget"
WIDGET_FILES = ("tv-inputs.js", "quiettv-bridge.js")  # index.html loads tv-inputs.js, then the config, then the bridge
CONFIG_JS = "quiettv-widget-config.js"
MANIFEST = "quiettv-manifest.json"
TEMPLATE = "config.xml.template"
GENERATED = frozenset({"config.xml", CONFIG_JS, MANIFEST, *WIDGET_FILES})
DISPLAY_NAME = "QuietTV Preview"

DIST_EXTS = frozenset({".html", ".js", ".css", ".png", ".svg", ".jpg", ".jpeg", ".webp", ".woff", ".woff2"})
TEXT_EXTS = frozenset({".html", ".js", ".css", ".svg"})
SEGMENT_RE = r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}"
# Top-level names that mark a project or toolkit checkout (private or exported), never a built dist.
NOT_A_DIST = frozenset({".git", "package.json", "package-lock.json", "node_modules", "src", ".env", "private",
                        "EXPORT-MANIFEST.json", "public-toolkit", "launcher", "helper", "tests"})
MAX_FILES, MAX_FILE, MAX_TOTAL, MAX_DEPTH = 400, 16 * 1024 * 1024, 64 * 1024 * 1024, 4
LEAK_MARKERS = (b"-----BEGIN", b"PRIVATE KEY", b"author-signature", b"signature1.xml", b"<ds:Signature")
FORBIDDEN_XML = ("<author", "app-control", "tizen:service", 'background-support="enable"', 'origin="*"',
                 "on-boot", "onboot", "home_appid", "startup_appid", "{{", "}}")
ZIP_TIME = (1980, 1, 1, 0, 0, 0)
REF_RE = re.compile(r"""\b(?:src|href)\s*=\s*["']([^"']*)["']""", re.I)
CSS_URL_RE = re.compile(r"""url\(\s*['"]?([^'")]*)['"]?\s*\)""", re.I)


def _fail(message: str) -> None:
    raise OwnerInitError(message)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def collect_dist(dist_root: str | os.PathLike) -> dict[str, bytes]:
    """Exact positive inventory: safe names, allowed types, no links, nothing that looks like a project."""
    root = Path(os.path.abspath(os.fspath(dist_root)))
    if owner_init._is_link(str(root)) or not root.is_dir():
        _fail("dist root is missing or a link")
    files: dict[str, bytes] = {}
    total = 0

    def walk(folder: Path, rel: tuple[str, ...]) -> None:
        nonlocal total
        with os.scandir(folder) as it:
            entries = sorted(it, key=lambda e: e.name)
        if not rel:  # before any per-file rule, so a project root is always named as one
            marker = next((e.name for e in entries if e.name in NOT_A_DIST), None)
            if marker:
                _fail("dist root looks like a project root, not a built dist: " + marker)
        for entry in entries:
            if entry.is_symlink() or owner_init._is_link(entry.path):
                _fail("dist contains a symlink or junction")
            if not re.fullmatch(SEGMENT_RE, entry.name):
                _fail("dist has an unsupported file name")
            parts = rel + (entry.name,)
            if entry.is_dir(follow_symlinks=False):
                if len(parts) > MAX_DEPTH:
                    _fail("dist is nested too deeply")
                walk(Path(entry.path), parts)
                continue
            if not entry.is_file(follow_symlinks=False):
                _fail("dist contains a special file")
            name = "/".join(parts)
            if Path(entry.name).suffix.lower() not in DIST_EXTS:
                _fail("dist has an unsupported file type: " + Path(entry.name).suffix)
            if name in GENERATED:
                _fail("dist file collides with a widget file: " + name)
            size = entry.stat(follow_symlinks=False).st_size
            total += size
            if size > MAX_FILE or total > MAX_TOTAL or len(files) >= MAX_FILES:
                _fail("dist is too large")
            files[name] = Path(entry.path).read_bytes()

    walk(root, ())
    if "index.html" not in files:
        _fail("dist root has no index.html")
    return files


def _check_ref(ref: str, base: str, names: set[str], what: str) -> None:
    ref = ref.strip()
    if ref.startswith("data:") and what == "css":
        return
    if (not ref or ":" in ref or ref.startswith(("/", "#")) or "\\" in ref or "%" in ref
            or "?" in ref or "#" in ref):
        _fail(f"{what} reference escapes the package or is unsupported")
    parts = [p for p in (base.split("/")[:-1] + ref.split("/")) if p not in ("", ".")]
    if ".." in parts:
        _fail(f"{what} reference escapes the package")
    if "/".join(parts) not in names:
        _fail(f"{what} reference points at a file that is not in the dist")


def check_references(files: dict[str, bytes]) -> None:
    names = set(files)
    html = files["index.html"].decode("utf-8")
    if re.search(r"<base\b", html, re.I):
        _fail("index.html must not set <base>")
    for ref in REF_RE.findall(html):
        _check_ref(ref, "index.html", names, "html")
    for name, data in files.items():
        if name.endswith((".css", ".html")):
            for ref in CSS_URL_RE.findall(data.decode("utf-8")):
                _check_ref(ref, name, names, "css")


def _scan_leaks(files: dict[str, bytes], token: str | None) -> None:
    for name, data in files.items():
        if any(m in data for m in LEAK_MARKERS):
            _fail("a key, certificate or signature marker is in " + name)
        if token is not None and token.encode() in data and name != CONFIG_JS:
            _fail("the backend token leaked into " + name)


def _config_xml(widget: dict) -> bytes:
    template = (WIDGET_SRC / TEMPLATE).read_text("utf-8")
    backend = widget["backend"]
    extra = ""
    if backend["enabled"]:
        extra = ('    <tizen:privilege name="http://tizen.org/privilege/internet"/>\n'
                 f'    <access origin="http://127.0.0.1:{int(backend["port"])}" subdomains="false"/>\n')
    values = {"{{APP_ID}}": widget["ownAppId"], "{{PACKAGE_ID}}": widget["packageId"],
              "{{VERSION}}": widget["version"], "{{DISPLAY_NAME}}": DISPLAY_NAME}
    for key, value in values.items():
        if key not in template:
            _fail("config.xml template lacks " + key)
        template = template.replace(key, escape(value, {'"': "&quot;", "'": "&apos;"}))
    if template.count("{{BACKEND_PRIVILEGES}}") != 1:
        _fail("config.xml template lacks one {{BACKEND_PRIVILEGES}}")
    xml = template.replace("{{BACKEND_PRIVILEGES}}", extra)
    lowered = xml.lower()
    if any(marker.lower() in lowered for marker in FORBIDDEN_XML):
        _fail("config.xml would carry a forbidden element (author/on-boot/service/Home/wildcard access)")
    if not backend["enabled"] and "privilege/internet" in xml:
        _fail("backend is disabled but config.xml asks for network access")
    return xml.replace("\r\n", "\n").encode("utf-8")


def _config_js(widget: dict, token: str | None) -> bytes:
    cfg = dict(widget)
    if widget["backend"]["enabled"]:
        cfg["backend"] = {"enabled": True, "port": widget["backend"]["port"], "token": token}
    # Frontend runtime contract (runtime-catalog.mjs): QuietTVApps replaces the compiled catalogue and
    # QuietTVConfig marks fail-closed owner mode. Both come from the same validated owner list.
    apps = [{"Id": a["Id"], "Name": a["Name"], "Icon": ""} for a in widget["apps"]]
    runtime = {"schema": owner_init.RUNTIME_SCHEMA, "preview": True, "iptvEnabled": False}

    def dump(value: object) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return ("window.QuietTVApps=Object.freeze(" + dump(apps) + ".map(Object.freeze));\n"
            "window.QuietTVConfig=Object.freeze(" + dump(runtime) + ");\n"
            "window.QuietTVWidgetConfig=Object.freeze(" + dump(cfg) + ");\n").encode("ascii")


def _inject(index: bytes) -> bytes:
    html = index.decode("utf-8")
    if html.count("<head>") != 1:
        _fail("index.html must contain exactly one <head>")
    tags = "".join(f'<script src="{n}"></script>' for n in (WIDGET_FILES[0], CONFIG_JS, WIDGET_FILES[1]))
    return html.replace("<head>", "<head>" + tags, 1).encode("utf-8")


def assemble(dist_root: str | os.PathLike, owner_dir: str | os.PathLike) -> dict[str, bytes]:
    owner = owner_init.load_owner_dir(owner_dir)
    widget, token = owner["widget"], owner["token"]
    dist = collect_dist(dist_root)
    check_references(dist)
    _scan_leaks(dist, token)
    files = dict(dist)
    files["index.html"] = _inject(dist["index.html"])
    for name in WIDGET_FILES:
        path = WIDGET_SRC / name
        if owner_init._is_link(str(path)):
            _fail("widget source is a link")
        # LF on every platform, so a Windows autocrlf checkout packs the same bytes.
        files[name] = path.read_bytes().replace(b"\r\n", b"\n")
    files["config.xml"] = _config_xml(widget)
    files[CONFIG_JS] = _config_js(widget, token)
    manifest = {"schema": "quiettv-widget-manifest/1", "unsigned": True, "preview": True,
                "ownAppId": widget["ownAppId"], "backendEnabled": widget["backend"]["enabled"],
                "files": [{"path": n, "sha256": _sha(files[n]), "size": len(files[n])} for n in sorted(files)]}
    files[MANIFEST] = owner_init.json_bytes(manifest)
    _scan_leaks(files, token)
    return files


def zip_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=ZIP_TIME)
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_STORED  # deflate output varies between zlib builds
            zf.writestr(info, files[name])
    return buf.getvalue()


def build(dist_root: str | os.PathLike, owner_dir: str | os.PathLike, out_wgt: str | os.PathLike,
          stage_dir: str | os.PathLike | None = None) -> dict:
    if not os.fspath(out_wgt).endswith(".wgt"):
        _fail("output must be a .wgt file")
    out = owner_init.check_outside_repos(out_wgt)
    stage = owner_init.check_outside_repos(stage_dir) if stage_dir is not None else None
    files = assemble(dist_root, owner_dir)
    data = zip_bytes(files)
    try:
        owner_init._write_new(out, data)
    except FileExistsError:
        _fail("output already exists")
    if '"token":' in files[CONFIG_JS].decode("ascii"):
        try:
            owner_init.make_private_file(out)
        except Exception:
            out.unlink()  # never leave a token-bearing WGT with inherited access
            raise
    if stage is not None:
        owner_init.make_private_dir(stage)
        owner_init.check_outside_repos(stage, must_not_exist=False)
        for name in sorted(files):
            target = stage.joinpath(*name.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            owner_init._write_new(target, files[name])
    return {"wgt": str(out), "sha256": _sha(data), "files": len(files), "unsigned": True,
            "manifestSha256": _sha(files[MANIFEST]), "stage": str(stage) if stage else None}


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build an UNSIGNED preview QuietTV widget (no install, no signing)")
    ap.add_argument("--dist", required=True, help="built, sanitized frontend dist root")
    ap.add_argument("--owner-dir", required=True, help="directory made by helper/owner_init.py")
    ap.add_argument("--out", required=True, help="new .wgt path outside every repository")
    ap.add_argument("--stage", default=None, help="optional new stage folder outside every repository")
    args = ap.parse_args(argv)
    try:
        result = build(args.dist, args.owner_dir, args.out, args.stage)
    except (OwnerInitError, OSError, UnicodeDecodeError) as exc:
        print("build refused: " + (str(exc) if isinstance(exc, OwnerInitError) else type(exc).__name__),
              file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

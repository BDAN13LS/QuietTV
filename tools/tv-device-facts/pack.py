"""Deterministic, UNSIGNED packager for the QuietTV device-facts probe widget.

Takes an owner-supplied Tizen application id and package id, fills the unsigned
``config.xml.template``, and writes a deterministic ``.wgt`` (a ZIP) holding exactly
``config.xml``, ``index.html`` and ``collector.js``. Same inputs give the same bytes.

It does NOT sign, install, run or remove anything, and it touches no TV, network,
certificate or private file. The output path must be new and OUTSIDE every git
checkout or worktree, so an owner artifact is never committed.

CLI:
    python pack.py --app-id <id> --package <pkg> --out <new path>.wgt [--version 0.1.0]
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import argparse
import io
import os
import re
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

SRC = Path(__file__).resolve().parent
TEMPLATE = SRC / "config.xml.template"
# Exactly the files packed into the widget. The template becomes config.xml.
PACKED_SOURCES = ("index.html", "collector.js")
DISPLAY_NAME = "QuietTV Device Facts Probe"
ZIP_TIME = (1980, 1, 1, 0, 0, 0)

# Owner ids are validated so a bad value cannot slip unescaped markup into config.xml.
APP_ID_RE = re.compile(r"[A-Za-z0-9]{10}\.[A-Za-z0-9]{1,52}\Z")
PACKAGE_RE = re.compile(r"[A-Za-z0-9]{10}\Z")
VERSION_RE = re.compile(r"[0-9]{1,3}(\.[0-9]{1,3}){1,3}\Z")

# Markup that must never appear in the generated config.xml (no author, boot, service, Home,
# network/wildcard access, or any unfilled placeholder).
FORBIDDEN_XML = (
    "<author", "app-control", "tizen:service", 'background-support="enable"', 'origin="*"',
    "on-boot", "onboot", "home_appid", "startup_appid", "privilege/internet", "<access", "{{", "}}",
)


class PackError(Exception):
    pass


def _require_outside_repos(out: Path) -> Path:
    """Refuse an output inside any git checkout or worktree (ancestor .git dir OR file marker)."""
    resolved = Path(os.path.realpath(out))
    for candidate in {out, resolved}:
        for ancestor in [candidate, *candidate.parents]:
            if os.path.lexists(ancestor / ".git"):
                raise PackError("output is inside a git checkout/worktree; choose a path outside every repo")
    return out


def _config_xml(app_id: str, package: str, version: str) -> bytes:
    if not APP_ID_RE.fullmatch(app_id):
        raise PackError("app id must be <10 alphanumerics>.<name>, e.g. abcdefghij.DeviceFacts")
    if not PACKAGE_RE.fullmatch(package):
        raise PackError("package id must be exactly 10 alphanumerics")
    # Tizen requires the application id to be prefixed by its package id (<package>.<name>). A
    # mismatched pair would not install, so refuse it here rather than emit a dead config.
    if app_id.split(".", 1)[0] != package:
        raise PackError("app id prefix must equal the package id (<package>.<name>)")
    if not VERSION_RE.fullmatch(version):
        raise PackError("version must look like 0.1.0")
    text = TEMPLATE.read_text("utf-8")
    values = {
        "{{APP_ID}}": app_id,
        "{{PACKAGE_ID}}": package,
        "{{VERSION}}": version,
        "{{DISPLAY_NAME}}": DISPLAY_NAME,
    }
    for key, value in values.items():
        if key not in text:
            raise PackError("config template lacks " + key)
        text = text.replace(key, escape(value, {'"': "&quot;", "'": "&apos;"}))
    lowered = text.lower()
    for marker in FORBIDDEN_XML:
        if marker.lower() in lowered:
            raise PackError("generated config.xml would carry a forbidden element: " + marker)
    return text.replace("\r\n", "\n").encode("utf-8")


def assemble(app_id: str, package: str, version: str) -> dict[str, bytes]:
    """archive name -> bytes. Nothing is written. LF-normalised, so a CRLF checkout packs the same."""
    files: dict[str, bytes] = {"config.xml": _config_xml(app_id, package, version)}
    for name in PACKED_SOURCES:
        files[name] = (SRC / name).read_bytes().replace(b"\r\n", b"\n")
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


def build(app_id: str, package: str, out_wgt: str | os.PathLike, version: str = "0.1.0") -> dict:
    out = Path(out_wgt)
    if not os.fspath(out).endswith(".wgt"):
        raise PackError("output must be a .wgt file")
    _require_outside_repos(out)
    data = zip_bytes(assemble(app_id, package, version))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL  # refuse to overwrite an existing artifact
    try:
        fd = os.open(out, flags, 0o600)
    except FileExistsError:
        raise PackError("output already exists") from None
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    return {"wgt": str(out), "bytes": len(data), "unsigned": True, "files": sorted(assemble(app_id, package, version))}


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="Pack an UNSIGNED QuietTV device-facts probe widget (no signing/install)")
    ap.add_argument("--app-id", required=True, help="owner Tizen application id, e.g. abcdefghij.DeviceFacts")
    ap.add_argument("--package", required=True, help="owner Tizen package id (10 alphanumerics)")
    ap.add_argument("--out", required=True, help="new .wgt path OUTSIDE every repository")
    ap.add_argument("--version", default="0.1.0", help="widget version (default 0.1.0)")
    args = ap.parse_args(argv)
    try:
        result = build(args.app_id, args.package, args.out, args.version)
    except (PackError, OSError) as exc:
        print("pack refused: " + (str(exc) if isinstance(exc, PackError) else type(exc).__name__), file=sys.stderr)
        return 2
    import json

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

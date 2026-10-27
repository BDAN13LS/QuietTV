"""Synthetic fixtures for the public toolkit tests. No real device data.

Builds three tiny fake packages as payload tars plus XattrBackup v1
manifests, and writes a capsule.json computed here independently of
``tvkit.assemble`` so the validator is not checked against itself.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLKIT = ROOT / "public-toolkit"
if str(TOOLKIT) not in sys.path:
    sys.path.insert(0, str(TOOLKIT))

from tvkit.capsule import system_key_record  # noqa: E402
from tvkit.profile import device_id_sha256, get_profile  # noqa: E402

PROFILE_ID = "UN43U8000FBXZA-1300.8-armv7l-tizen9"
DEVICE_ID = "SYNTHETIC-TEST-DEVICE-0001"
FACTS = {
    "model": "UN43U8000FBXZA",
    "firmware": "1300.8",
    "arch": "armv7l",
    "osName": "Tizen",
    "osVersion": "9",
    "deviceId": DEVICE_ID,
}
PACKAGE_IDS = ("org.example.alpha", "org.example.bravo", "org.example.charlie")
OUR_PACKAGE = "Example2026.Launcher"


def profile():
    return get_profile(PROFILE_ID)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def png_chunk(ctype: bytes, data: bytes, crc: int | None = None) -> bytes:
    import struct
    import zlib
    value = zlib.crc32(ctype + data) & 0xFFFFFFFF if crc is None else crc
    return struct.pack(">I", len(data)) + ctype + data + struct.pack(">I", value)


PNG_IDAT = bytes(range(7, 200, 3)) * 3  # opaque bytes: the filter never decodes image data


def png(before_idat=(), extra=(), idat: bytes = PNG_IDAT, tail: bytes = b"") -> bytes:
    """A structurally valid 1x1 PNG; ``before_idat``/``extra`` are (type, data) chunks."""
    import struct
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    body = png_chunk(b"IHDR", ihdr)
    body += b"".join(png_chunk(t, d) for t, d in before_idat)
    body += png_chunk(b"IDAT", idat[:40]) + png_chunk(b"IDAT", idat[40:])
    body += b"".join(png_chunk(t, d) for t, d in extra)
    return b"\x89PNG\r\n\x1a\n" + body + png_chunk(b"IEND", b"") + tail


def package_members(package_id: str) -> list[dict]:
    """The tree of one fake package: root, two dirs, two files, one symlink."""
    body = f"#!/bin/sh\necho {package_id}\n".encode()
    res = f"resource for {package_id}\n".encode()
    return [
        {"path": ".", "kind": "directory", "uid": 5001, "gid": 100, "mode": 0o755},
        {"path": "bin", "kind": "directory", "uid": 5001, "gid": 100, "mode": 0o755},
        {"path": "bin/app", "kind": "file", "uid": 5001, "gid": 100, "mode": 0o750, "data": body},
        {"path": "res", "kind": "directory", "uid": 5001, "gid": 100, "mode": 0o755},
        {"path": "res/strings.txt", "kind": "file", "uid": 0, "gid": 0, "mode": 0o644, "data": res},
        {"path": "current", "kind": "symlink", "uid": 5001, "gid": 100, "mode": 0o777, "linkTarget": "bin"},
    ]


def make_tar(members: list[dict], extra: list[tarfile.TarInfo | tuple] = ()) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for m in members:
            name = "./" if m["path"] == "." else m["path"]
            info = tarfile.TarInfo(name)
            info.uid, info.gid, info.mode, info.mtime = m["uid"], m["gid"], m["mode"], 0
            data = None
            if m["kind"] == "directory":
                info.type = tarfile.DIRTYPE
            elif m["kind"] == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = m["linkTarget"]
            else:
                data = m["data"]
                info.size = len(data)
            archive.addfile(info, io.BytesIO(data) if data is not None else None)
        for item in extra:
            if isinstance(item, tuple):
                info, data = item
                archive.addfile(info, io.BytesIO(data))
            else:
                archive.addfile(item)
    return buffer.getvalue()


def capsule_entries(members: list[dict]) -> list[dict]:
    entries = []
    for m in members:
        e = {k: m[k] for k in ("path", "kind", "uid", "gid", "mode")}
        if m["kind"] == "file":
            e["size"] = len(m["data"])
            e["sha256"] = sha(m["data"])
        if m["kind"] == "symlink":
            e["linkTarget"] = m["linkTarget"]
        entries.append(e)
    return sorted(entries, key=lambda e: e["path"])


def xattr_manifest(package_id: str, members: list[dict]) -> bytes:
    label = base64.b64encode(b"User::Pkg::" + package_id.encode()).decode()
    entries = []
    for m in members:
        e = {"path": m["path"], "kind": m["kind"], "xattrs": {"security.SMACK64": label}}
        if m["kind"] == "file":
            e["sha256"] = sha(m["data"])
        if m["kind"] == "symlink":
            e["linkTarget"] = m["linkTarget"]
        entries.append(e)
    return json.dumps({"version": 1, "rootBasename": package_id, "entries": entries}, indent=2).encode()


def capsule_manifest(payloads: dict[str, bytes], xattrs: dict[str, bytes], members: dict[str, list]) -> dict:
    packages = []
    for pid in PACKAGE_IDS:
        packages.append({
            "packageId": pid,
            "installRoot": f"/opt/usr/apps/{pid}",
            "payload": {"file": f"payload/{pid}.tar", "sha256": sha(payloads[pid]), "size": len(payloads[pid])},
            "xattrManifest": {"file": f"xattrs/{pid}.json", "sha256": sha(xattrs[pid]), "size": len(xattrs[pid])},
            "entries": capsule_entries(members[pid]),
        })
    return {
        "schema": "tvkit-capsule/1",
        "profileId": PROFILE_ID,
        "provenance": "owner-device-backup",
        "device": {**{k: FACTS[k] for k in ("model", "firmware", "arch", "osName", "osVersion")},
                   "deviceIdSha256": device_id_sha256(DEVICE_ID)},
        "packages": packages,
        "registryChanges": [
            {"appId": "org.example.alpha", "fields": {"app_disable": {"before": "false", "applied": "true"},
                                                      "app_onboot": {"before": "true", "applied": "false"}}},
            {"appId": "org.example.bravo", "fields": {"app_disable": {"before": "0", "applied": "1"}}},
        ],
        "systemKeyChanges": [
            system_key_record("db/appfw/startup_appid", "org.example.viewer", OUR_PACKAGE),
            system_key_record("db/appfw/home_appid", "org.example.home", OUR_PACKAGE),
        ],
        "ourPackages": [{"packageId": OUR_PACKAGE}],
    }


def build_parts():
    members = {pid: package_members(pid) for pid in PACKAGE_IDS}
    payloads = {pid: make_tar(members[pid]) for pid in PACKAGE_IDS}
    xattrs = {pid: xattr_manifest(pid, members[pid]) for pid in PACKAGE_IDS}
    return members, payloads, xattrs


def write_capsule(directory: Path, manifest: dict | None = None, payloads=None, xattrs=None) -> Path:
    members, default_payloads, default_xattrs = build_parts()
    payloads = payloads or default_payloads
    xattrs = xattrs or default_xattrs
    if manifest is None:
        manifest = capsule_manifest(payloads, xattrs, members)
    (directory / "payload").mkdir(parents=True)
    (directory / "xattrs").mkdir()
    for pid in PACKAGE_IDS:
        (directory / "payload" / f"{pid}.tar").write_bytes(payloads[pid])
        (directory / "xattrs" / f"{pid}.json").write_bytes(xattrs[pid])
    (directory / "capsule.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return directory


def write_facts(path: Path, **overrides) -> Path:
    path.write_text(json.dumps({**FACTS, **overrides}), encoding="utf-8")
    return path

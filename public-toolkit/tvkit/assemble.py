"""Assemble a capsule on the PC from what the owner's transport captured.

Inputs (named in an assemble spec, paths relative to the spec file):
  * a payload tar per package, made on the TV with ownership and modes kept
  * the XattrBackup ``--dump`` manifest for the same tree
  * the registry before/applied fields and startup/Home key values
  * the device facts read from the TV (the device id is stored only hashed)

The entry table is derived from the tar itself, then the finished capsule is
put through the full validator before this returns. The output folder must
be new; nothing is overwritten.
"""

from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path
from typing import Any, Mapping

from . import strictjson as sj
from .capsule import (
    CAPSULE_SCHEMA, MANIFEST_NAME, PROVENANCE, Capsule, CapsuleError,
    entries_from_payload, system_key_record, validate,
)
from .paths import is_identifier
from .profile import MATCH_FIELDS, Profile, device_id_sha256, match_device

SPEC_SCHEMA = "tvkit-assemble-spec/1"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def assemble(spec_path: str | Path, profile: Profile, device_facts: Mapping[str, str],
             out_dir: str | Path) -> Capsule:
    spec_file = Path(spec_path)
    base = spec_file.parent
    try:
        spec = sj.require_object(
            sj.load_file(spec_file, 4 * 1024 * 1024), "assemble spec",
            ("schema", "packages", "registryChanges", "systemKeys", "ourPackages"),
        )
    except sj.StrictJsonError as exc:
        raise CapsuleError(str(exc)) from exc
    if spec["schema"] != SPEC_SCHEMA:
        raise CapsuleError(f"assemble spec schema must be {SPEC_SCHEMA}")
    match_device(profile, device_facts)

    out = Path(out_dir)
    if out.exists() or out.is_symlink():
        raise CapsuleError("capsule output folder must not exist yet")

    packages: list[dict[str, Any]] = []
    files: dict[str, bytes] = {}
    for position, item in enumerate(sj.require_list(spec["packages"], "packages", max_items=64)):
        obj = sj.require_object(item, f"packages[{position}]",
                                ("packageId", "installRoot", "payload", "xattrManifest"))
        package_id = obj["packageId"]
        if not is_identifier(package_id):
            raise CapsuleError(f"packages[{position}].packageId is not a valid package id")
        payload = (base / sj.require_str(obj["payload"], "payload")).read_bytes()
        xattrs = (base / sj.require_str(obj["xattrManifest"], "xattrManifest")).read_bytes()
        entries = entries_from_payload(io.BytesIO(payload), obj["installRoot"])
        payload_name = f"payload/{package_id}.tar"
        xattr_name = f"xattrs/{package_id}.json"
        files[payload_name] = payload
        files[xattr_name] = xattrs
        packages.append({
            "packageId": package_id,
            "installRoot": obj["installRoot"],
            "payload": {"file": payload_name, "sha256": _sha256_bytes(payload), "size": len(payload)},
            "xattrManifest": {"file": xattr_name, "sha256": _sha256_bytes(xattrs), "size": len(xattrs)},
            "entries": entries,
        })

    system_keys = []
    for position, item in enumerate(sj.require_list(spec["systemKeys"], "systemKeys", max_items=16)):
        obj = sj.require_object(item, f"systemKeys[{position}]", ("key", "original", "expectedCurrent"))
        system_keys.append(system_key_record(obj["key"], obj["original"], obj["expectedCurrent"]))

    manifest = {
        "schema": CAPSULE_SCHEMA,
        "profileId": profile.profile_id,
        "provenance": PROVENANCE,
        "device": {**{f: device_facts[f] for f in MATCH_FIELDS},
                   "deviceIdSha256": device_id_sha256(device_facts["deviceId"])},
        "packages": packages,
        "registryChanges": spec["registryChanges"],
        "systemKeyChanges": system_keys,
        "ourPackages": spec["ourPackages"],
    }
    out.mkdir(parents=False)
    for name, data in files.items():
        target = out / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "xb") as handle:
            handle.write(data)
    with open(out / MANIFEST_NAME, "x", encoding="utf-8", newline="\n") as handle:
        handle.write(sj.dumps(manifest))
    return validate(out, profile, device_facts)


def bundle(capsule_dir: str | Path, out_file: str | Path, profile: Profile) -> str:
    """Pack a validated capsule folder into one uncompressed tar. Returns its SHA-256."""
    validate(capsule_dir, profile)
    src = Path(capsule_dir)
    out = Path(out_file)
    if out.exists() or out.is_symlink():
        raise CapsuleError("bundle output must not exist yet")
    names = sorted(p.relative_to(src).as_posix() for p in src.rglob("*") if p.is_file())
    partial = out.with_name(out.name + ".partial")
    with tarfile.open(partial, mode="x", format=tarfile.PAX_FORMAT) as archive:
        for name in names:
            data = (src / name).read_bytes()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o600
            info.mtime = 0
            archive.addfile(info, io.BytesIO(data))
    partial.replace(out)
    validate(out, profile)
    return hashlib.sha256(out.read_bytes()).hexdigest()

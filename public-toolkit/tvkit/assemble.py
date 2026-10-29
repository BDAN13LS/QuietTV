"""Assemble a capsule on the PC from what the owner's transport captured.

Inputs (named in an assemble spec, paths relative to the spec file):
  * a payload tar per package, made on the TV with ownership and modes kept
  * the XattrBackup ``--dump`` manifest for the same tree
  * the registry before/applied fields and startup/Home key values
  * the device facts read from the TV (the device id is stored only hashed)

The entry table is derived from the tar itself, then the finished capsule is
put through the full validator before this returns. The output folder must
be new; nothing is overwritten.

During assembly, payload files are never retained in memory: each source is opened once to
stream its size and SHA-256 (and to derive the payload entry table), then
opened again to stream-copy into the new capsule while the copied bytes are
rehashed and the source identity is rechecked. Payload memory is therefore
independent of archive size. Entry tables and the manifest are still held in
memory, so this is not a promise about total process RAM for very large
trees; only the archive bytes are streamed.
"""

from __future__ import annotations

import hashlib
import io
import os
import stat
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Mapping

from . import strictjson as sj
from .capsule import (
    CAPSULE_SCHEMA, MANIFEST_NAME, MAX_MEMBER_BYTES, MAX_XATTR_MANIFEST_BYTES,
    PROVENANCE, Capsule, CapsuleError, entries_from_payload,
    require_capsule1_profile, system_key_record, validate,
)
from .paths import is_identifier
from .profile import MATCH_FIELDS, Profile, device_id_sha256, match_device

SPEC_SCHEMA = "tvkit-assemble-spec/1"
CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class _SourceFile:
    """A captured source file, identified without holding its bytes."""

    path: Path
    size: int
    sha256: str
    identity: tuple[int, int, int, int, int]


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _visible_identity(info: os.stat_result) -> tuple[int, int, int, int]:
    """The lstat-visible part of an identity.

    Windows can report a stale creation time through lstat immediately after a
    write while fstat is current, so ctime is compared only between fstats.
    """
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _open_verified(path: Path, limit: int, label: str,
                   expected: tuple[int, int, int, int, int] | None = None
                   ) -> tuple[BinaryIO, tuple[int, int, int, int, int]]:
    """Open one ordinary regular source file and bind its identity before reading.

    A missing path keeps the caller-visible ``FileNotFoundError`` it had before
    streaming. A link, special file, oversize file or a file that changes while
    being opened is a ``CapsuleError``.
    """
    before = os.lstat(path)
    if stat.S_ISLNK(before.st_mode):
        raise CapsuleError(f"{label}: source must not be a symbolic link")
    if not stat.S_ISREG(before.st_mode):
        raise CapsuleError(f"{label}: source must be an ordinary regular file")
    if before.st_size > limit:
        raise CapsuleError(f"{label}: source file is too large")
    handle = open(path, "rb")
    try:
        opened = os.fstat(handle.fileno())
        if not stat.S_ISREG(opened.st_mode):
            raise CapsuleError(f"{label}: source must be an ordinary regular file")
        if opened.st_size > limit:
            raise CapsuleError(f"{label}: source file is too large")
        current = _identity(opened)
        if _visible_identity(opened) != _visible_identity(before):
            raise CapsuleError(f"{label}: source file changed while opening")
        if expected is not None and current != expected:
            raise CapsuleError(f"{label}: source file changed during assembly")
        return handle, current
    except BaseException:
        handle.close()
        raise


def _hash_stream(handle: BinaryIO, limit: int, label: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    try:
        while True:
            chunk = handle.read(CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise CapsuleError(f"{label}: source file is larger than allowed")
            digest.update(chunk)
    except OSError as exc:
        raise CapsuleError(f"{label}: source file could not be read") from exc
    return digest.hexdigest(), total


def _inspect_blob(path: Path, limit: int, label: str) -> _SourceFile:
    handle, identity = _open_verified(path, limit, label)
    try:
        digest, size = _hash_stream(handle, limit, label)
        after = os.fstat(handle.fileno())
    finally:
        handle.close()
    current = os.lstat(path)
    if _identity(after) != identity or _visible_identity(current) != identity[:4]:
        raise CapsuleError(f"{label}: source file changed during assembly")
    if size != identity[2] or size > limit:
        raise CapsuleError(f"{label}: source file changed during assembly")
    return _SourceFile(path, size, digest, identity)


def _inspect_payload(path: Path, limit: int, label: str,
                     install_root: str) -> tuple[_SourceFile, list[dict[str, Any]]]:
    handle, identity = _open_verified(path, limit, label)
    try:
        digest, size = _hash_stream(handle, limit, label)
        handle.seek(0)
        try:
            entries = entries_from_payload(handle, install_root)
        except OSError as exc:
            raise CapsuleError(f"{label}: source file could not be read") from exc
        after = os.fstat(handle.fileno())
    finally:
        handle.close()
    current = os.lstat(path)
    if _identity(after) != identity or _visible_identity(current) != identity[:4]:
        raise CapsuleError(f"{label}: source file changed during assembly")
    if size != identity[2] or size > limit:
        raise CapsuleError(f"{label}: source file changed during assembly")
    return _SourceFile(path, size, digest, identity), entries


def _copy_chunks(handle: BinaryIO, target: BinaryIO, limit: int,
                 label: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    try:
        while True:
            chunk = handle.read(CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise CapsuleError(f"{label}: source file grew during assembly")
            digest.update(chunk)
            target.write(chunk)
    except OSError as exc:
        raise CapsuleError(f"{label}: source file could not be copied") from exc
    return digest.hexdigest(), total


def _copy_verified(source: _SourceFile, target: Path, label: str) -> None:
    handle, identity = _open_verified(source.path, source.size, label, source.identity)
    try:
        try:
            with open(target, "xb") as out_handle:
                digest, size = _copy_chunks(handle, out_handle, source.size, label)
        except OSError as exc:
            raise CapsuleError(f"{label}: destination could not be written exclusively") from exc
        after = os.fstat(handle.fileno())
    finally:
        handle.close()
    current = os.lstat(source.path)
    if _identity(after) != source.identity or _visible_identity(current) != source.identity[:4]:
        raise CapsuleError(f"{label}: source file changed during assembly")
    if size != source.size or digest != source.sha256:
        raise CapsuleError(f"{label}: source file bytes changed during assembly")


def assemble(spec_path: str | Path, profile: Profile, device_facts: Mapping[str, str],
             out_dir: str | Path) -> Capsule:
    # capsule/1 cannot bind build or region: refuse before reading anything or creating a folder.
    require_capsule1_profile(profile)
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
    sources: list[tuple[_SourceFile, str, str]] = []
    for position, item in enumerate(sj.require_list(spec["packages"], "packages", max_items=64)):
        obj = sj.require_object(item, f"packages[{position}]",
                                ("packageId", "installRoot", "payload", "xattrManifest"))
        package_id = obj["packageId"]
        if not is_identifier(package_id):
            raise CapsuleError(f"packages[{position}].packageId is not a valid package id")
        payload_path = base / sj.require_str(obj["payload"], "payload")
        xattr_path = base / sj.require_str(obj["xattrManifest"], "xattrManifest")
        payload_label = f"packages[{position}].payload"
        xattr_label = f"packages[{position}].xattrManifest"
        payload, entries = _inspect_payload(
            payload_path, MAX_MEMBER_BYTES, payload_label, obj["installRoot"])
        xattrs = _inspect_blob(xattr_path, MAX_XATTR_MANIFEST_BYTES, xattr_label)
        payload_name = f"payload/{package_id}.tar"
        xattr_name = f"xattrs/{package_id}.json"
        sources.append((payload, payload_name, payload_label))
        sources.append((xattrs, xattr_name, xattr_label))
        packages.append({
            "packageId": package_id,
            "installRoot": obj["installRoot"],
            "payload": {"file": payload_name, "sha256": payload.sha256, "size": payload.size},
            "xattrManifest": {"file": xattr_name, "sha256": xattrs.sha256, "size": xattrs.size},
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
    for source, name, label in sources:
        target = out / name
        target.parent.mkdir(parents=True, exist_ok=True)
        _copy_verified(source, target, label)
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

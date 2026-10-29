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

from . import recovery2 as r2
from . import strictjson as sj
from .capsule import (
    CAPSULE_SCHEMA, MANIFEST_NAME, MAX_MEMBER_BYTES, MAX_XATTR_MANIFEST_BYTES,
    PROVENANCE, Capsule, CapsuleError, entries_from_payload,
    require_capsule1_profile, system_key_record, validate,
)
from .paths import is_identifier
from .profile import (MATCH_FIELDS, Profile, ProfileError, device_id_sha256, match_device,
                      parse_device_facts)
from .recovery2 import Recovery2

SPEC_SCHEMA = "tvkit-assemble-spec/1"
# recovery/2 has its own spec schema: it binds firmwareBuild/region and carries
# externalRefs and bootChanges that capsule/1 cannot represent.
RECOVERY_SPEC_SCHEMA = "tvkit-assemble-spec/2"
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


def _inspect_payload(path: Path, limit: int, label: str, install_root: str,
                     external_links: Mapping[str, str] | None = None
                     ) -> tuple[_SourceFile, list[dict[str, Any]]]:
    handle, identity = _open_verified(path, limit, label)
    try:
        digest, size = _hash_stream(handle, limit, label)
        handle.seek(0)
        try:
            entries = entries_from_payload(handle, install_root, external_links)
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


def _collect_packages(spec: Mapping[str, Any], base: Path,
                      external_links_for: Mapping[str, Mapping[str, str]] | None = None
                      ) -> tuple[list[dict[str, Any]], list[tuple[_SourceFile, str, str]]]:
    """Inspect every package's payload and xattr source once, deriving the entry table.

    Shared by capsule/1 and recovery/2 assembly. ``external_links_for`` maps a
    package id to its exact approved external links (recovery/2 only; from a
    trusted policy); capsule/1 passes nothing, so every escaping link is
    refused. Nothing is written here: the returned sources are stream-copied
    afterwards, exactly as before.
    """
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
        external = (external_links_for or {}).get(package_id)
        payload, entries = _inspect_payload(
            payload_path, MAX_MEMBER_BYTES, payload_label, obj["installRoot"], external)
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
    return packages, sources


def _system_key_records(spec: Mapping[str, Any]) -> list[dict[str, str]]:
    records = []
    for position, item in enumerate(sj.require_list(spec["systemKeys"], "systemKeys", max_items=16)):
        obj = sj.require_object(item, f"systemKeys[{position}]", ("key", "original", "expectedCurrent"))
        records.append(system_key_record(obj["key"], obj["original"], obj["expectedCurrent"]))
    return records


def _write_capsule(out: Path, sources: list[tuple[_SourceFile, str, str]], manifest: Mapping[str, Any]) -> None:
    """Create the output folder, stream-copy every source, write the manifest. No overwrite."""
    out.mkdir(parents=False)
    for source, name, label in sources:
        target = out / name
        target.parent.mkdir(parents=True, exist_ok=True)
        _copy_verified(source, target, label)
    with open(out / MANIFEST_NAME, "x", encoding="utf-8", newline="\n") as handle:
        handle.write(sj.dumps(manifest))


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

    packages, sources = _collect_packages(spec, base)
    manifest = {
        "schema": CAPSULE_SCHEMA,
        "profileId": profile.profile_id,
        "provenance": PROVENANCE,
        "device": {**{f: device_facts[f] for f in MATCH_FIELDS},
                   "deviceIdSha256": device_id_sha256(device_facts["deviceId"])},
        "packages": packages,
        "registryChanges": spec["registryChanges"],
        "systemKeyChanges": _system_key_records(spec),
        "ourPackages": spec["ourPackages"],
    }
    _write_capsule(out, sources, manifest)
    return validate(out, profile, device_facts)


def _boot_change_record(target: str, field: str, before: str, ours: str) -> dict[str, str]:
    """A recovery/2 bootChanges item with the value-binding digests computed here."""
    return {"target": target, "field": field, "before": before, "ours": ours,
            "beforeSha256": hashlib.sha256(before.encode("utf-8")).hexdigest(),
            "oursSha256": hashlib.sha256(ours.encode("utf-8")).hexdigest()}


def assemble_recovery2(spec_path: str | Path, profile: Profile, device_facts: Mapping[str, str],
                       out_dir: str | Path, *, external_policy: r2.ExternalRefPolicy,
                       boot_policy: r2.BootFieldPolicy,
                       target_evidence: Mapping[str, r2.TargetEvidence]) -> Recovery2:
    """Assemble a ``tvkit-recovery/2`` capsule from a capture whose approved external
    loader links and boot-field changes are named by caller-supplied trusted policies.

    The capsule and the spec cannot authorize anything: an external link is kept
    only when a payload symlink matches an :class:`ExternalRefPolicy` approval
    exactly, its file evidence comes from the caller's ``target_evidence`` (never
    from the policy, and covering exactly the referenced targets), and a boot
    change is accepted only for a field a :class:`BootFieldPolicy` approves. The
    device block binds every profile/2 fact (including firmwareBuild, region and
    the hashed device id) from the given facts; a missing or unknown fact refuses
    and no value is ever taken from the expected profile.

    Every profile, policy, fact, spec and manifest refusal happens before the
    output folder exists (the finished manifest goes through the real
    :mod:`recovery2` validator before :func:`_write_capsule`). A source that
    changes during the copy, or a destination file that races in, is refused
    without writing ``capsule.json`` and without overwriting anything; a partial
    copy may remain in the new folder, exactly as for capsule/1.
    """
    # Profile, policies and facts are checked before any source read or folder creation.
    r2.require_profile2(profile)
    r2._require_policies(profile, external_policy, boot_policy)
    try:
        facts = parse_device_facts(device_facts)
        missing = [f for f in profile.match_fields if f not in facts]
        if missing:
            raise CapsuleError("device facts are missing " + ", ".join(sorted(missing)))
        match_device(profile, facts)
        device_hash = device_id_sha256(facts.get("deviceId"))
    except ProfileError as exc:
        raise CapsuleError(str(exc)) from exc
    if not isinstance(target_evidence, Mapping) or not all(
            isinstance(ev, r2.TargetEvidence) for ev in target_evidence.values()):
        raise CapsuleError("recovery/2 assembly needs a mapping of caller TargetEvidence; refusing")

    spec_file = Path(spec_path)
    base = spec_file.parent
    try:
        spec = sj.require_object(
            sj.load_file(spec_file, 4 * 1024 * 1024), "assemble spec",
            ("schema", "packages", "registryChanges", "systemKeys", "bootChanges", "ourPackages"),
        )
    except sj.StrictJsonError as exc:
        raise CapsuleError(str(exc)) from exc
    if spec["schema"] != RECOVERY_SPEC_SCHEMA:
        raise CapsuleError(f"assemble spec schema must be {RECOVERY_SPEC_SCHEMA}")

    out = Path(out_dir)
    if out.exists() or out.is_symlink():
        raise CapsuleError("capsule output folder must not exist yet")

    # The only external links the entry derivation may keep come from the trusted policy.
    external_links_for: dict[str, dict[str, str]] = {}
    for approval in external_policy.approvals:
        external_links_for.setdefault(approval.package_id, {})[approval.link_path] = approval.target

    packages, sources = _collect_packages(spec, base, external_links_for)

    # Derive external refs from the payload symlinks that exactly match an approval; take
    # their file evidence from the caller, which must cover exactly the referenced targets.
    entries_by_id = {p["packageId"]: p["entries"] for p in packages}
    external_refs: list[dict[str, Any]] = []
    used_targets: set[str] = set()
    for approval in external_policy.approvals:
        entries = entries_by_id.get(approval.package_id)
        if entries is None:
            continue
        match = next((e for e in entries if e["path"] == approval.link_path
                      and e["kind"] == "symlink" and e.get("linkTarget") == approval.target), None)
        if match is None:
            continue
        evidence = target_evidence.get(approval.target)
        if evidence is None:
            raise CapsuleError("no caller evidence for an approved external target; refusing")
        external_refs.append({"packageId": approval.package_id, "linkPath": approval.link_path,
                              "target": approval.target, "access": r2.ACCESS_READ_ONLY,
                              "targetEvidence": evidence.to_json()})
        used_targets.add(approval.target)
    if set(target_evidence) != used_targets:
        raise CapsuleError("caller evidence covers a target no external reference uses; refusing")

    boot_changes = []
    for position, item in enumerate(sj.require_list(spec["bootChanges"], "bootChanges", max_items=256)):
        obj = sj.require_object(item, f"bootChanges[{position}]", ("target", "field", "before", "ours"))
        where = f"bootChanges[{position}]"
        boot_changes.append(_boot_change_record(
            sj.require_str(obj["target"], f"{where}.target"), sj.require_str(obj["field"], f"{where}.field"),
            sj.require_str(obj["before"], f"{where}.before"), sj.require_str(obj["ours"], f"{where}.ours")))

    manifest = {
        "schema": r2.RECOVERY_SCHEMA,
        "profileId": profile.profile_id,
        "provenance": PROVENANCE,
        "device": {**{f: facts[f] for f in profile.match_fields}, "deviceIdSha256": device_hash},
        "packages": packages,
        "externalRefs": external_refs,
        "registryChanges": spec["registryChanges"],
        "systemKeyChanges": _system_key_records(spec),
        "bootChanges": boot_changes,
        "ourPackages": spec["ourPackages"],
    }

    # Reuse the full recovery/2 validator on the in-memory manifest first, so an unknown or
    # stale external ref, a boot-policy violation, or a link that escapes THROUGH the approved
    # link is refused before any folder is created.
    r2.parse_manifest(manifest, profile, external_policy, boot_policy)

    _write_capsule(out, sources, manifest)
    return r2.validate(out, profile, facts, external_policy=external_policy, boot_policy=boot_policy)


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

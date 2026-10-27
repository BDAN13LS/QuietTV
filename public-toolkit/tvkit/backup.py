"""Off-TV readback gate: prove the stored backup is the one the TV hashed.

D2 of docs/superpowers/plans/2026-10-27-consumer-install.md, offline slice.
``readback_gate`` re-reads the capsule from the owner's storage (a folder or
an uncompressed ``.tar`` bundle), runs the unchanged ``capsule.validate``
against live device facts, and compares every file the TV produced with the
SHA-256 the TV computed before the copy left it. It returns evidence only
when all of that holds; every refusal raises ``ReadbackRefused``.

Nothing here opens a device connection, writes a file or registers a
transport. There is no option that skips a check. See
public-toolkit/docs/D2-READBACK.md for the contract and its limits.
"""

from __future__ import annotations

import hashlib
import json
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .capsule import (
    CAPSULE_SCHEMA, MANIFEST_NAME, MAX_MEMBER_BYTES, BundleSource, Capsule, CapsuleError,
    FileRef, _hash_stream, open_source, validate,
)
from .paths import PathRuleError, is_sha256
from .profile import MATCH_FIELDS, Profile, ProfileError, builtin_profiles, match_device, parse_device_facts

BASELINES = ("stock", "current-state")
BASELINE_BASIS = "declared"
IDENTITY_DOMAIN = "tvkit-backup-identity/1"
MAX_BUNDLE_BYTES = 64 * MAX_MEMBER_BYTES

LIMITATIONS = (
    "capsule/1 scope only: app directories, listed registry fields, two startup/Home keys; no boot changes.",
    "The baseline label is declared by the caller, checked only against the contradiction capsule/1 can show; "
    "it is not device-proven, and packages removed before capture are invisible to it.",
    "Device hashes are taken as given; this gate does not prove how or where they were computed.",
    "No encryption: age vault integration is deferred.",
    "Readback only: no rehearsal, no restore, no physical capture.",
)


class ReadbackRefused(CapsuleError):
    """The stored backup is not proven to be the one the TV hashed. Nothing was written."""


_SEAL = object()


@dataclass(frozen=True)
class StoredFile:
    name: str
    sha256: str
    size: int


@dataclass(frozen=True)
class GateResult:
    """Evidence that one stored backup passed the readback gate.

    Only ``readback_gate`` builds one; a refusal raises instead, so there is
    no pass/fail field to forge. Every field is immutable.
    """

    profile_id: str
    device: tuple[tuple[str, str], ...]  # match fields + deviceIdSha256, never the raw id
    storage_kind: str  # "directory" | "bundle"
    bundle_sha256: str | None
    manifest_sha256: str
    files: tuple[StoredFile, ...]  # every file the TV produced, sorted by name
    counts: tuple[tuple[str, int], ...]
    baseline: str
    baseline_basis: str
    identity_sha256: str
    limitations: tuple[str, ...] = LIMITATIONS
    _seal: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _SEAL:
            raise TypeError("GateResult is only produced by readback_gate")
        # dataclasses.replace() copies the seal; this catches an edited copy.
        if self.identity_sha256 != _identity(self.profile_id, dict(self.device), self.manifest_sha256,
                                             self.files, self.baseline, self.baseline_basis):
            raise TypeError("GateResult fields do not match its backup identity")

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": IDENTITY_DOMAIN,
            "capsuleSchema": CAPSULE_SCHEMA,
            "profileId": self.profile_id,
            "device": dict(self.device),
            "storageKind": self.storage_kind,
            "bundleSha256": self.bundle_sha256,
            "manifestSha256": self.manifest_sha256,
            "files": [{"file": f.name, "sha256": f.sha256, "size": f.size} for f in self.files],
            "counts": dict(self.counts),
            "baseline": self.baseline,
            "baselineBasis": self.baseline_basis,
            "identitySha256": self.identity_sha256,
            "limitations": list(self.limitations),
        }


# --------------------------------------------------------------------------
# Inputs


def _check_baseline_label(baseline: Any) -> str:
    if type(baseline) is not str or baseline not in BASELINES:
        raise ReadbackRefused(f"baseline must be exactly one of {', '.join(BASELINES)}")
    return baseline


def _copy_device_hashes(device_hashes: Any) -> dict[str, str]:
    """Snapshot the TV-side hash mapping so later caller changes cannot matter."""
    if not isinstance(device_hashes, Mapping):
        raise ReadbackRefused("device hashes must be a mapping of capsule file name to SHA-256")
    copied: dict[str, str] = {}
    for name, value in device_hashes.items():
        if type(name) is not str or name == "":
            raise ReadbackRefused("device hashes have a key that is not a capsule file name")
        if not is_sha256(value):
            raise ReadbackRefused(f"device hash for {name} is not 64 lowercase hex characters")
        copied[name] = value
    if not copied:
        raise ReadbackRefused("device hashes are empty")
    return copied


def _resolve_profile(facts: Any) -> tuple[Profile, dict[str, str]]:
    """Pick the one built-in profile the live facts match exactly. The capsule never chooses."""
    if facts is None:
        raise ReadbackRefused("live device facts are required; the gate never runs unbound")
    if not isinstance(facts, Mapping):
        raise ReadbackRefused("device facts must be a mapping")
    try:
        parsed = parse_device_facts(dict(facts))
        profiles = builtin_profiles()
    except ProfileError as exc:
        raise ReadbackRefused(f"device facts: {exc}") from exc
    matches = []
    for profile in profiles.values():
        try:
            match_device(profile, parsed)
        except ProfileError:
            continue
        matches.append(profile)
    if len(matches) != 1:
        raise ReadbackRefused("device facts do not match exactly one supported profile; refusing")
    return matches[0], parsed


# --------------------------------------------------------------------------
# Reading the stored bytes


@dataclass(frozen=True)
class _Snapshot:
    kind: str
    bundle_sha256: str | None
    files: tuple[tuple[str, str, int], ...]  # (name, sha256, size), sorted

    def by_name(self) -> dict[str, tuple[str, int]]:
        return {name: (digest, size) for name, digest, size in self.files}


def _hash_file(path: Path, limit: int) -> str:
    with open(path, "rb") as handle:
        digest, _ = _hash_stream(handle, limit)
    return digest


def _read_stored(path: Path) -> _Snapshot:
    """Hash every file in the stored capsule through the capsule source rules."""
    source = open_source(path)
    try:
        kind = "bundle" if isinstance(source, BundleSource) else "directory"
        files = []
        for name in sorted(source.names()):
            with source.open(name) as handle:
                digest, size = _hash_stream(handle, MAX_MEMBER_BYTES)
            files.append((name, digest, size))
    finally:
        source.close()
    bundle_sha = _hash_file(path, MAX_BUNDLE_BYTES) if kind == "bundle" else None
    return _Snapshot(kind, bundle_sha, tuple(files))


# --------------------------------------------------------------------------
# The two checks this gate adds to capsule.validate


def device_produced_files(capsule: Capsule) -> dict[str, FileRef]:
    """The files the TV itself produced: each package's payload tar and xattr dump.

    ``capsule.json`` is excluded: it is assembled on the PC.
    """
    refs: dict[str, FileRef] = {}
    for package in capsule.packages:
        refs[package.payload.file] = package.payload
        refs[package.xattr_manifest.file] = package.xattr_manifest
    return refs


def compare_device_hashes(refs: Mapping[str, FileRef], stored: Mapping[str, tuple[str, int]],
                          device_hashes: Mapping[str, str]) -> None:
    """Exact set and exact hash: TV hash == re-read stored bytes == manifest reference."""
    missing = sorted(set(refs) - set(device_hashes))
    extra = sorted(set(device_hashes) - set(refs))
    if missing:
        raise ReadbackRefused("no device hash for: " + ", ".join(missing))
    if extra:
        raise ReadbackRefused("device hash names a file the capsule does not hold: " + ", ".join(extra))
    for name in sorted(refs):
        ref = refs[name]
        if name not in stored:
            raise ReadbackRefused(f"{name} is missing from the stored copy")
        digest, size = stored[name]
        if size != ref.size or digest != ref.sha256:
            raise ReadbackRefused(f"{name}: stored bytes do not match the capsule manifest")
        if device_hashes[name] != digest:
            raise ReadbackRefused(f"{name}: stored bytes differ from what the TV hashed; refusing")


def check_baseline(baseline: str, capsule: Capsule) -> None:
    """Refuse a "stock" label that capsule/1 itself contradicts.

    The only signal capsule/1 carries: a startup/Home key whose captured
    original value is already one of our own packages means our launcher was
    Home when the backup was taken, so the TV was not stock.
    """
    if baseline != "stock":
        return
    ours = {package_id.casefold() for package_id in capsule.our_packages}
    for change in capsule.system_key_changes:
        if change.original.casefold() in ours:
            raise ReadbackRefused(
                f"baseline 'stock' contradicted: {change.key} already pointed at our package at capture; "
                "use 'current-state'")


def _identity(profile_id: str, device: Mapping[str, str], manifest_sha256: str,
              files: tuple[StoredFile, ...], baseline: str, basis: str) -> str:
    document = {
        "domain": IDENTITY_DOMAIN,
        "capsuleSchema": CAPSULE_SCHEMA,
        "profileId": profile_id,
        "device": {name: device[name] for name in MATCH_FIELDS + ("deviceIdSha256",)},
        "manifestSha256": manifest_sha256,
        "files": [[f.name, f.sha256, f.size] for f in files],
        "baseline": baseline,
        "baselineBasis": basis,
    }
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


# --------------------------------------------------------------------------
# The gate


def readback_gate(stored_path: str | Path, device_hashes: Mapping[str, str], facts: Mapping[str, str],
                  *, baseline: str = "current-state") -> GateResult:
    """Re-read the stored backup, validate it against live facts, compare the TV's hashes.

    ``device_hashes`` maps each capsule file name the TV produced
    (``payload/<id>.tar``, ``xattrs/<id>.json``) to the SHA-256 computed on
    the TV. ``facts`` are the live device facts (``profile.parse_device_facts``
    shape). ``baseline`` is the caller's declaration, ``stock`` or
    ``current-state``. Raises ``ReadbackRefused`` (a ``CapsuleError``) on any
    mismatch; returns evidence only on a full pass. Writes nothing.
    """
    label = _check_baseline_label(baseline)
    hashes = _copy_device_hashes(device_hashes)
    profile, live = _resolve_profile(facts)
    path = Path(stored_path)
    try:
        first = _read_stored(path)
        capsule = validate(path, profile, live)
        second = _read_stored(path)
    except ReadbackRefused:
        raise
    except CapsuleError as exc:
        raise ReadbackRefused(f"stored capsule refused: {exc}") from exc
    except (OSError, tarfile.TarError, EOFError, PathRuleError, KeyError) as exc:
        raise ReadbackRefused(f"stored capsule could not be re-read: {type(exc).__name__}") from exc
    if not (capsule.payload_verified and capsule.device_verified):
        raise ReadbackRefused("capsule validation did not verify both payload and device")
    if first != second:
        raise ReadbackRefused("stored capsule changed while it was being read; refusing")

    stored = first.by_name()
    refs = device_produced_files(capsule)
    if set(stored) != set(refs) | {MANIFEST_NAME}:
        raise ReadbackRefused("stored copy holds missing or unexpected files")
    compare_device_hashes(refs, stored, hashes)
    check_baseline(label, capsule)

    files = tuple(StoredFile(name, stored[name][0], stored[name][1]) for name in sorted(refs))
    manifest_sha256 = stored[MANIFEST_NAME][0]
    identity = _identity(capsule.profile_id, capsule.device, manifest_sha256, files, label, BASELINE_BASIS)
    return GateResult(
        profile_id=capsule.profile_id,
        device=tuple((name, capsule.device[name]) for name in MATCH_FIELDS + ("deviceIdSha256",)),
        storage_kind=first.kind,
        bundle_sha256=first.bundle_sha256,
        manifest_sha256=manifest_sha256,
        files=files,
        counts=tuple(sorted(capsule.counts.items())),
        baseline=label,
        baseline_basis=BASELINE_BASIS,
        identity_sha256=identity,
        _seal=_SEAL,
    )

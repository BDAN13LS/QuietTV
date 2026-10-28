"""Typed read-only backup capture: read a device through an injected backend,
assemble an owner-storage capsule, and prove it with the existing readback gate.

D2 of docs/superpowers/plans/2026-10-27-consumer-install.md, offline capture
slice. This is the seam between a device-reading adapter and the capsule the
engine already understands. It adds NO new schema: it drives ``assemble.assemble``
(so the capsule is the unchanged ``tvkit-capsule/1``) and ``backup.readback_gate``
(so the proof is the unchanged ``GateResult``).

What it does, for one declared capture request:

1. Reads device facts through the injected backend and binds them to exactly one
   built-in profile and the hashed owner-TV fingerprint. An unsupported or
   incomplete identity refuses here, before anything is captured.
2. Captures each declared optional package's payload tar and its XattrBackup
   dump, plus the current values of the declared registry fields and startup/Home
   keys, through the backend. Every blob carries the digest the TV computed for
   itself (``hash-on-device``); that digest, never a PC re-hash, is what the gate
   is given.
3. Assembles the capsule into the owner's own storage, which must be a path
   OUTSIDE this repository, through the real ``assemble`` validator.
4. Runs ``readback_gate`` against the TV-side digests and returns its
   ``GateResult`` as the capture evidence.

Boundaries this slice holds:

* **Read-only, injected backend.** ``CaptureBackend`` has no write or run method.
  The toolkit ships no implementation and registers none; a real adapter is
  constructed and passed in by the owner's own reviewed code. ``transport.TRANSPORTS``
  stays empty. A fake backend is a test double, never a stand-in for a real SDB
  or root transport.
* **Zero TV writes, zero repo writes.** The backend only reads. The engine writes
  the capsule to owner storage and nothing else; its working files live in a
  temporary directory that is removed. Nothing here mutates or removes anything on
  a device.
* **Refuse before trusting.** Unsupported/incomplete identity, device mismatch,
  missing metadata, path traversal or escaping symlinks, a size the TV did not
  report, an oversize blob, and bytes that do not match the TV's own digest all
  raise before a passing result exists. The hash comparison and the traversal and
  symlink rules are the existing capsule/readback checks, not re-implemented here.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from . import assemble as _assemble
from .backup import GateResult, readback_gate
from .capsule import (
    CAPSULE_SCHEMA, MANIFEST_NAME, MAX_MEMBER_BYTES, MAX_XATTR_MANIFEST_BYTES, CapsuleError,
)
from .paths import check_relative, is_identifier, is_sha256
from .profile import (
    MATCH_FIELDS, Profile, ProfileError, builtin_profiles, match_device, parse_device_facts,
)

# Capabilities the backend must declare, mirroring the plan's D2 capture capabilities.
CAP_FACTS = "read-device-facts"
CAP_PACKAGE = "capture-package"
CAP_XATTR = "xattr-dump"
CAP_HASH = "hash-on-device"
CAP_REGISTRY = "registry-read"
CAP_SYSTEM_KEY = "system-key-read"
BASE_CAPABILITIES = frozenset({CAP_FACTS, CAP_PACKAGE, CAP_XATTR, CAP_HASH})

# The repository root, so owner storage can be required to live outside it.
REPO_ROOT = Path(__file__).resolve().parents[2]

_SPEC_SCHEMA = _assemble.SPEC_SCHEMA


class CaptureRefused(CapsuleError):
    """A reason to refuse a capture. Nothing was captured, assembled or proven."""


# --------------------------------------------------------------------------
# The injected read-only adapter contract and its typed request


@dataclass(frozen=True)
class CapturedFile:
    """One file the TV produced, with the digest the TV computed for itself.

    ``device_sha256`` is the ``hash-on-device`` result, computed on the TV
    independently of the transfer. It is the attestation the gate checks the
    stored bytes against; the engine never substitutes a PC re-hash for it.
    """

    data: bytes
    device_sha256: str
    device_size: int


class CaptureBackend:
    """A read-only device reader, injected by the owner's reviewed code.

    This toolkit ships no implementation. Subclasses implement the read methods
    and declare which capabilities they support. There is deliberately no write,
    run or mutate method: capture never changes a device.
    """

    name = "abstract"
    read_only = True
    capabilities: frozenset[str] = frozenset()

    def read_device_facts(self) -> Mapping[str, str]:
        raise CaptureRefused("backend cannot read device facts")

    def capture_package(self, package_id: str, install_root: str) -> CapturedFile:
        raise CaptureRefused("backend cannot capture a package payload")

    def dump_xattrs(self, package_id: str, install_root: str) -> CapturedFile:
        raise CaptureRefused("backend cannot dump xattrs")

    def read_registry_field(self, app_id: str, field: str) -> str:
        raise CaptureRefused("backend cannot read a registry field")

    def read_system_key(self, key: str) -> str:
        raise CaptureRefused("backend cannot read a startup/Home key")


@dataclass(frozen=True)
class PackageRequest:
    package_id: str
    install_root: str


@dataclass(frozen=True)
class RegistryRequest:
    app_id: str
    fields: Mapping[str, str]  # field -> the value setup intends to apply


@dataclass(frozen=True)
class SystemKeyRequest:
    key: str
    expected_current: str  # the value setup intends to write (our launcher)


@dataclass(frozen=True)
class CaptureRequest:
    """What the owner declared to back up. The intended (``applied`` /
    ``expectedCurrent``) side is caller intent from the catalogue; the current
    (``before`` / ``original``) side is read from the device here."""

    packages: tuple[PackageRequest, ...] = ()
    registry: tuple[RegistryRequest, ...] = ()
    system_keys: tuple[SystemKeyRequest, ...] = ()
    our_packages: tuple[str, ...] = ()


@dataclass(frozen=True)
class CaptureResult:
    """Evidence that a capture was written to owner storage and passed the gate.

    The proof is the unforgeable ``GateResult`` the existing engine produced; this
    only pairs it with where the capsule was written. It exists only when the gate
    returned, so there is no pass/fail flag to set.
    """

    capsule_dir: str
    baseline: str
    gate: GateResult


# --------------------------------------------------------------------------
# Binding


def _resolve_profile(facts: Mapping[str, str]) -> tuple[Profile, dict[str, str]]:
    """Exactly one built-in profile must match the live facts, or refuse."""
    try:
        parsed = parse_device_facts(dict(facts))
    except ProfileError as exc:
        raise CaptureRefused(f"device facts: {exc}") from exc
    matches = []
    for profile in builtin_profiles().values():
        try:
            match_device(profile, parsed)
        except ProfileError:
            continue
        matches.append(profile)
    if len(matches) != 1:
        raise CaptureRefused("device facts do not match exactly one supported profile; refusing")
    return matches[0], parsed


def _check_capabilities(backend: CaptureBackend, request: CaptureRequest) -> None:
    needed = set(BASE_CAPABILITIES)
    if request.registry:
        needed.add(CAP_REGISTRY)
    if request.system_keys:
        needed.add(CAP_SYSTEM_KEY)
    have = frozenset(getattr(backend, "capabilities", frozenset()))
    missing = needed - set(have)
    if missing:
        raise CaptureRefused(
            f"backend {getattr(backend, 'name', '?')!r} lacks capabilities: {', '.join(sorted(missing))}"
        )
    if not getattr(backend, "read_only", False):
        raise CaptureRefused("capture requires a read-only backend")


def _install_root_for(profile: Profile, package_id: str, install_root: str) -> str:
    """The install root must be an allowlisted root for this exact package id."""
    if not is_identifier(package_id):
        raise CaptureRefused("package id is not a valid identifier")
    if profile.is_protected(package_id):
        raise CaptureRefused(f"package {package_id} is protected and is never captured for removal")
    allowed = {f"{root}/{package_id}" for root in profile.install_roots}
    if install_root not in allowed:
        raise CaptureRefused(f"install root for {package_id} is not an allowlisted root; refusing")
    return install_root


# --------------------------------------------------------------------------
# Capture


def _blob(backend_call, limit: int, what: str) -> CapturedFile:
    captured = backend_call()
    if not isinstance(captured, CapturedFile):
        raise CaptureRefused(f"backend returned no typed {what} blob")
    if not is_sha256(captured.device_sha256):
        raise CaptureRefused(f"{what}: device digest is not 64 lowercase hex characters")
    if type(captured.device_size) is not int or captured.device_size < 0:
        raise CaptureRefused(f"{what}: device size is not a non-negative integer")
    # Bound on the TV's own claim first, so an oversize file is refused without
    # ever holding that many bytes.
    if captured.device_size > limit or len(captured.data) > limit:
        raise CaptureRefused(f"{what}: blob is larger than the capsule limit; refusing")
    if len(captured.data) != captured.device_size:
        raise CaptureRefused(f"{what}: transferred bytes are not the size the TV reported; refusing")
    return captured


def _device_digest(captured: CapturedFile) -> str:
    """The digest the gate is given for a captured file: always the TV's own.

    This is the crux of the capture proof. Returning a PC re-hash of the
    transferred bytes here (``sha256(captured.data)``) would make the readback
    gate compare the bytes against themselves and silently accept a transfer the
    TV never attested. The gate is only meaningful because this stays the
    ``hash-on-device`` value. The guard-off test replaces exactly this.
    """
    return captured.device_sha256


def _read_value(backend_call, what: str) -> str:
    value = backend_call()
    if type(value) is not str:
        raise CaptureRefused(f"{what}: backend returned a non-string value")
    return value


def capture_backup(
    backend: CaptureBackend,
    request: CaptureRequest,
    out_dir: str | Path,
    *,
    baseline: str = "current-state",
) -> CaptureResult:
    """Capture a backup through ``backend`` into ``out_dir`` and prove it.

    ``out_dir`` is the owner's own storage. It must not already exist and must
    live outside this repository. ``baseline`` is the caller's honest declaration
    (``stock`` or ``current-state``); the gate enforces the one contradiction
    capsule/1 can show and records the label as ``declared``.

    Returns a ``CaptureResult`` only on a full pass. Every failure raises a
    ``CapsuleError`` (``CaptureRefused`` for this engine's own guards,
    ``ReadbackRefused`` or ``CapsuleError`` from the validator and gate) before a
    result exists. Writes only to owner storage and a temporary working folder;
    never to a device or to this repository.
    """
    if not request.packages:
        raise CaptureRefused("a capture must declare at least one package")

    out = Path(out_dir)
    resolved_out = out.resolve()
    if resolved_out == REPO_ROOT or REPO_ROOT in resolved_out.parents:
        raise CaptureRefused("owner storage must be outside the repository; refusing")
    if out.exists() or out.is_symlink():
        raise CaptureRefused("owner storage folder must not exist yet")
    if not out.parent.is_dir() or out.parent.is_symlink():
        raise CaptureRefused("owner storage parent folder must be a real existing directory")

    _check_capabilities(backend, request)

    facts = backend.read_device_facts()
    if not isinstance(facts, Mapping):
        raise CaptureRefused("backend device facts must be a mapping")
    profile, parsed_facts = _resolve_profile(facts)

    seen: set[str] = set()
    spec_packages = []
    device_hashes: dict[str, str] = {}
    blobs: dict[str, bytes] = {}
    for package in request.packages:
        install_root = _install_root_for(profile, package.package_id, package.install_root)
        folded = package.package_id.casefold()
        if folded in seen:
            raise CaptureRefused(f"package {package.package_id} is declared twice")
        seen.add(folded)
        payload = _blob(lambda p=package, r=install_root: backend.capture_package(p.package_id, r),
                        MAX_MEMBER_BYTES, f"payload {package.package_id}")
        xattrs = _blob(lambda p=package, r=install_root: backend.dump_xattrs(p.package_id, r),
                       MAX_XATTR_MANIFEST_BYTES, f"xattr manifest {package.package_id}")
        payload_name = f"{package.package_id}.payload.tar"
        xattr_name = f"{package.package_id}.xattrs.json"
        blobs[payload_name] = payload.data
        blobs[xattr_name] = xattrs.data
        device_hashes[f"payload/{package.package_id}.tar"] = _device_digest(payload)
        device_hashes[f"xattrs/{package.package_id}.json"] = _device_digest(xattrs)
        spec_packages.append({
            "packageId": package.package_id,
            "installRoot": install_root,
            "payload": payload_name,
            "xattrManifest": xattr_name,
        })

    registry_changes = []
    for item in request.registry:
        if not is_identifier(item.app_id):
            raise CaptureRefused("registry change names an invalid app id")
        if not item.fields:
            raise CaptureRefused(f"registry change for {item.app_id} lists no fields")
        fields = {}
        for field, applied in item.fields.items():
            if type(applied) is not str:
                raise CaptureRefused(f"registry intent for {item.app_id}.{field} is not a string")
            before = _read_value(lambda a=item.app_id, f=field: backend.read_registry_field(a, f),
                                 f"registry {item.app_id}.{field}")
            fields[field] = {"before": before, "applied": applied}
        registry_changes.append({"appId": item.app_id, "fields": fields})

    system_keys = []
    for item in request.system_keys:
        if type(item.expected_current) is not str:
            raise CaptureRefused("system key intent is not a string")
        original = _read_value(lambda k=item.key: backend.read_system_key(k), f"system key {item.key}")
        system_keys.append({
            "key": item.key,
            "original": original,
            "expectedCurrent": item.expected_current,
        })

    our_packages = [{"packageId": pid} for pid in request.our_packages]

    spec = {
        "schema": _SPEC_SCHEMA,
        "packages": spec_packages,
        "registryChanges": registry_changes,
        "systemKeys": system_keys,
        "ourPackages": our_packages,
    }

    # Everything is built and proven in a temporary folder that is removed. Only a
    # capsule that passes the gate is moved into owner storage, so a refusal at any
    # step leaves owner storage untouched. The repository and the device are never
    # written to.
    with tempfile.TemporaryDirectory(prefix="tvkit-capture-") as scratch:
        scratch_dir = Path(scratch)
        src = scratch_dir / "src"
        src.mkdir()
        for name, data in blobs.items():
            check_relative(name, "capture working file")
            with open(src / name, "xb") as handle:
                handle.write(data)
        spec_path = src / "assemble-spec.json"
        with open(spec_path, "x", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(spec, indent=2))
        built = scratch_dir / "capsule"
        # assemble runs the full capsule validator (traversal, symlink escape,
        # byte and metadata cross-checks) and writes the capsule into scratch.
        _assemble.assemble(spec_path, profile, parsed_facts, built)
        # The proof: the TV's own digests against the assembled bytes.
        gate = readback_gate(built, device_hashes, parsed_facts, baseline=baseline)
        # Proven: place it into owner storage as the last step.
        shutil.move(str(built), str(out))

    return CaptureResult(capsule_dir=str(out), baseline=gate.baseline, gate=gate)

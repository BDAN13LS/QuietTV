"""Typed read-only backup capture: read a device through an injected backend,
assemble an owner-storage capsule, and prove the actual owner-storage copy with
the existing readback gate.

D2 of docs/superpowers/plans/2026-10-27-consumer-install.md, offline capture
slice. This is the seam between a device-reading adapter and the capsule the
engine already understands. It adds NO new schema: it drives ``assemble.assemble``
(so the capsule is the unchanged ``tvkit-capsule/1``) and ``backup.readback_gate``
(so the proof is the unchanged ``GateResult``).

What it does, for one declared capture request:

1. Reads device facts through the injected backend and binds them to exactly one
   built-in profile and the hashed owner-TV fingerprint. An unsupported or
   incomplete identity refuses here, before anything is captured.
2. Validates the WHOLE request (package ids, roots and count, registry field
   names, startup/Home key names, ``ourPackages``) against the profile and capsule
   allowlists BEFORE any package, registry or system-key read, so an unknown
   field or key never reaches the backend.
3. Streams each declared package's payload tar and its XattrBackup dump through
   the backend one blob at a time, staging each to disk under the owner-storage
   parent with per-chunk and aggregate size bounds; it never holds every payload
   in memory. It reads the current values of the declared registry fields and
   startup/Home keys. Every blob carries the digest the TV computed for itself
   (``hash-on-device``); that digest, never a PC re-hash, is what the gate is
   given.
4. Re-reads the device facts and refuses if the identity or fingerprint changed
   during the capture (an adapter reconnect or device switch must not pass
   unnoticed).
5. Assembles the capsule in the staging area through the real ``assemble``
   validator, gates the staged copy, then publishes it to owner storage by a
   same-volume rename that never overwrites a preexisting or raced destination.
6. Runs ``readback_gate`` against the TV-side digests on the ACTUAL owner-storage
   copy and returns its ``GateResult`` as the capture evidence, so a corruption
   introduced by publication cannot pass as proven.

Boundaries this slice holds:

* **Read-only, injected backend.** ``CaptureBackend`` has no write or run method.
  The toolkit ships no implementation and registers none; a real adapter is
  constructed and passed in by the owner's own reviewed code. ``transport.TRANSPORTS``
  stays empty. A fake backend is a test double, never a stand-in for a real SDB
  or root transport.
* **Bounded transfer.** The backend yields size-capped chunks with a declared
  size and a TV digest. Each blob is streamed to disk one at a time under
  explicit per-chunk and aggregate limits (the existing capsule schema maxima);
  the iterator is closed on every path, including a refusal. Real adapters MUST
  enforce the transfer limit themselves; the fixture helper materialises bytes
  for tests only.
* **Zero TV writes.** The backend only reads. The engine stages and publishes to
  owner storage and removes only the scratch and output it created in this
  operation; it never touches the device or a preexisting/raced destination.
* **Refuse before trusting.** Unsupported/incomplete identity, device mismatch,
  an identity change mid-capture, missing metadata, path traversal or escaping
  symlinks, a size the TV did not report, an oversize blob, and bytes that do not
  match the TV's own digest all raise before a passing result exists. The hash
  comparison and the traversal and symlink rules are the existing capsule/readback
  checks, not re-implemented here.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from . import assemble as _assemble
from .backup import GateResult, readback_gate
from .capsule import MAX_MEMBER_BYTES, MAX_PACKAGES, MAX_XATTR_MANIFEST_BYTES, CapsuleError
from .paths import check_relative, is_identifier, is_sha256
from .profile import (
    Profile, ProfileError, builtin_profiles, match_device, parse_device_facts,
)

# Capabilities the backend must declare, mirroring the plan's D2 capture capabilities.
CAP_FACTS = "read-device-facts"
CAP_PACKAGE = "capture-package"
CAP_XATTR = "xattr-dump"
CAP_HASH = "hash-on-device"
CAP_REGISTRY = "registry-read"
CAP_SYSTEM_KEY = "system-key-read"
BASE_CAPABILITIES = frozenset({CAP_FACTS, CAP_PACKAGE, CAP_XATTR, CAP_HASH})

# A single transferred chunk may not exceed this. It bounds one transfer unit so a
# backend cannot defeat streaming by handing over one enormous chunk; it is NOT a
# package-size limit (a package is many chunks, bounded only by the schema maxima
# MAX_MEMBER_BYTES / MAX_XATTR_MANIFEST_BYTES).
MAX_CHUNK_BYTES = 16 * 1024 * 1024
DEFAULT_CHUNK_BYTES = 1024 * 1024

# The repository root, so owner storage can be required to live outside it.
REPO_ROOT = Path(__file__).resolve().parents[2]

_SPEC_SCHEMA = _assemble.SPEC_SCHEMA


class CaptureRefused(CapsuleError):
    """A reason to refuse a capture. Nothing was captured, assembled or proven."""


# --------------------------------------------------------------------------
# The injected read-only adapter contract and its typed request


@dataclass(frozen=True)
class CapturedStream:
    """One file the TV produced, as a bounded, lazy byte stream.

    ``device_sha256`` is the ``hash-on-device`` result, computed on the TV
    independently of the transfer; it is the attestation the gate checks the
    stored bytes against, and the engine never substitutes a PC re-hash for it.
    ``device_size`` is the size the TV reported. ``chunks`` is an iterable of
    ``bytes`` yielded lazily; the engine iterates it once, bounds every chunk and
    the running total, and closes it on every path.
    """

    device_sha256: str
    device_size: int
    chunks: Iterable[bytes]


def bytes_stream(data: bytes, *, device_sha256: str | None = None, device_size: int | None = None,
                 chunk_size: int = DEFAULT_CHUNK_BYTES) -> CapturedStream:
    """Wrap in-memory bytes as a chunked stream. For fixtures/tests and small
    reads only; a real adapter streams from the device and enforces the limit
    itself rather than materialising the whole file. ``device_sha256`` and
    ``device_size`` may be overridden to inject a fault in tests."""
    if chunk_size < 1:
        raise ValueError("chunk_size must be positive")
    digest = device_sha256 if device_sha256 is not None else hashlib.sha256(data).hexdigest()
    size = device_size if device_size is not None else len(data)

    def generate() -> Iterable[bytes]:
        for start in range(0, len(data), chunk_size):
            yield data[start:start + chunk_size]

    return CapturedStream(device_sha256=digest, device_size=size, chunks=generate())


class CaptureBackend:
    """A read-only device reader, injected by the owner's reviewed code.

    This toolkit ships no implementation. Subclasses implement the read methods
    and declare which capabilities they support. There is deliberately no write,
    run or mutate method: capture never changes a device. The payload and xattr
    reads return a bounded ``CapturedStream`` and are told the maximum number of
    bytes they are permitted to transfer.
    """

    name = "abstract"
    read_only = True
    capabilities: frozenset[str] = frozenset()

    def read_device_facts(self) -> Mapping[str, str]:
        raise CaptureRefused("backend cannot read device facts")

    def capture_package(self, package_id: str, install_root: str, *, max_bytes: int) -> CapturedStream:
        raise CaptureRefused("backend cannot capture a package payload")

    def dump_xattrs(self, package_id: str, install_root: str, *, max_bytes: int) -> CapturedStream:
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

    The proof is the unforgeable ``GateResult`` the existing engine produced from
    the ACTUAL owner-storage copy; this only pairs it with where the capsule was
    written. It exists only when the gate returned, so there is no pass/fail flag."""

    capsule_dir: str
    baseline: str
    gate: GateResult


# --------------------------------------------------------------------------
# Binding and request validation (all before any package/registry/key read)


def _resolve_profile(facts: Mapping[str, str]) -> tuple[Profile, dict[str, str]]:
    """Exactly one built-in profile must match the live facts, or refuse."""
    if not isinstance(facts, Mapping):
        raise CaptureRefused("backend device facts must be a mapping")
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


def _validate_request(profile: Profile, request: CaptureRequest) -> list[tuple[PackageRequest, str]]:
    """Check every declared id/root/field/key against the allowlists up front.

    Returns the validated (package, install_root) pairs. Raises before the caller
    performs any backend package, registry or system-key read, so an arbitrary
    key or field is never handed to the device reader.
    """
    if not request.packages:
        raise CaptureRefused("a capture must declare at least one package")
    if len(request.packages) > MAX_PACKAGES:
        raise CaptureRefused(f"a capture may declare at most {MAX_PACKAGES} packages; refusing")

    seen: set[str] = set()
    packages: list[tuple[PackageRequest, str]] = []
    for package in request.packages:
        if not is_identifier(package.package_id):
            raise CaptureRefused("package id is not a valid identifier")
        if profile.is_protected(package.package_id):
            raise CaptureRefused(f"package {package.package_id} is protected and is never captured for removal")
        allowed = {f"{root}/{package.package_id}" for root in profile.install_roots}
        if package.install_root not in allowed:
            raise CaptureRefused(f"install root for {package.package_id} is not an allowlisted root; refusing")
        folded = package.package_id.casefold()
        if folded in seen:
            raise CaptureRefused(f"package {package.package_id} is declared twice")
        seen.add(folded)
        packages.append((package, package.install_root))

    reg_seen: set[str] = set()
    for item in request.registry:
        if not is_identifier(item.app_id):
            raise CaptureRefused("registry change names an invalid app id")
        if profile.is_protected(item.app_id):
            raise CaptureRefused(f"registry change for {item.app_id} is protected; refusing")
        if item.app_id in reg_seen:
            raise CaptureRefused(f"registry change for {item.app_id} is declared twice")
        reg_seen.add(item.app_id)
        if not item.fields:
            raise CaptureRefused(f"registry change for {item.app_id} lists no fields")
        for field, applied in item.fields.items():
            if field not in profile.registry_fields:
                raise CaptureRefused(f"registry field {field!r} is not in the profile allowlist; refusing")
            if type(applied) is not str:
                raise CaptureRefused(f"registry intent for {item.app_id}.{field} is not a string")

    key_seen: set[str] = set()
    for item in request.system_keys:
        if item.key not in profile.system_keys:
            raise CaptureRefused(f"startup/Home key {item.key!r} is not an allowlisted key; refusing")
        if item.key in key_seen:
            raise CaptureRefused(f"startup/Home key {item.key} is declared twice")
        key_seen.add(item.key)
        if type(item.expected_current) is not str:
            raise CaptureRefused("system key intent is not a string")

    for pid in request.our_packages:
        if not is_identifier(pid) or profile.is_protected(pid):
            raise CaptureRefused("ourPackages names an id that is protected or invalid; refusing")

    return packages


# --------------------------------------------------------------------------
# Streaming one blob to disk, bounded, with the iterator always closed


def _stage_blob(captured: object, dest: Path, limit: int, what: str) -> CapturedStream:
    """Stream one captured blob to ``dest`` under explicit bounds.

    Bounds the TV's declared size against ``limit`` before reading, then every
    chunk against ``MAX_CHUNK_BYTES`` and the running total against both the
    declared size and ``limit`` as it writes. The lazy iterator is closed on
    every path, including a refusal. Nothing is held whole in memory.
    """
    if not isinstance(captured, CapturedStream):
        raise CaptureRefused(f"backend returned no typed {what} stream")
    if not is_sha256(captured.device_sha256):
        raise CaptureRefused(f"{what}: device digest is not 64 lowercase hex characters")
    if type(captured.device_size) is not int or captured.device_size < 0:
        raise CaptureRefused(f"{what}: device size is not a non-negative integer")
    if captured.device_size > limit:
        raise CaptureRefused(f"{what}: device reports a size larger than the capsule limit; refusing")
    reader = iter(captured.chunks)
    total = 0
    try:
        with open(dest, "xb") as handle:
            for chunk in reader:
                if type(chunk) is not bytes:
                    raise CaptureRefused(f"{what}: a transfer chunk is not bytes")
                if len(chunk) > MAX_CHUNK_BYTES:
                    raise CaptureRefused(f"{what}: a transfer chunk exceeds the per-chunk limit; refusing")
                total += len(chunk)
                if total > captured.device_size or total > limit:
                    raise CaptureRefused(f"{what}: transfer exceeds the size the TV reported; refusing")
                handle.write(chunk)
    finally:
        close = getattr(reader, "close", None)
        if callable(close):
            close()
    if total != captured.device_size:
        raise CaptureRefused(f"{what}: transferred bytes are not the size the TV reported; refusing")
    return captured


def _device_digest(captured: CapturedStream, staged_path: Path) -> str:
    """The digest the gate is given for a captured file: always the TV's own.

    This is the crux of the capture proof. Returning a PC re-hash of the staged
    bytes here (``sha256(staged_path.read_bytes())``) would make the readback
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


def _inside(child: Path, parent: Path) -> bool:
    """True if ``child`` is ``parent`` or lives under it (case-normalised, absolute)."""
    try:
        c = os.path.normcase(os.path.abspath(child))
        p = os.path.normcase(os.path.abspath(parent))
        return os.path.commonpath([c, p]) == p
    except ValueError:  # different drives
        return False


def _require_outside_any_repo(out: Path) -> None:
    """Refuse owner storage inside THIS repository or ANY git checkout/worktree.

    Reuses the proven ``helper/owner_init.check_outside_repos`` policy: it checks
    the path and its symlink-resolved real path for containment in this repo, and
    walks every ancestor for a ``.git`` entry -- a directory OR the FILE marker a
    git worktree uses (``os.path.lexists`` catches both). Without this, a capture
    in a worktree could land in the parent repo or another checkout and risk the
    owner's stock data being committed. Runs before any backend read or output
    creation.
    """
    real = Path(os.path.realpath(out))
    for candidate in (out, real):
        for root in (str(REPO_ROOT), os.path.realpath(REPO_ROOT)):
            if _inside(candidate, Path(root)):
                raise CaptureRefused("owner storage must be outside the repository; refusing")
        for ancestor in [candidate, *candidate.parents]:
            if os.path.lexists(ancestor / ".git"):
                raise CaptureRefused("owner storage is inside a git checkout/worktree; refusing")


def _publish(staged_capsule: Path, out: Path) -> tuple[int, int]:
    """Reserve ``out`` exclusively, then populate it; never replace an existing entry.

    ``os.rename`` is NOT a no-overwrite operation: on POSIX it silently replaces an
    EMPTY destination directory created in the race after an earlier existence
    check (Windows rejects it, but the public CLI/CI also runs on Linux). So this
    does not rely on rename to avoid clobbering. Instead ``os.mkdir`` is an atomic
    create-or-fail on both platforms: if anything -- empty or populated -- is
    already at ``out``, it raises ``FileExistsError`` and we refuse, leaving the
    raced destination untouched. The staged capsule lives under ``out``'s parent,
    so each member then moves into the reserved directory by a same-volume rename.

    Returns the ``(st_dev, st_ino)`` identity of the directory this call created,
    so cleanup can prove it is removing its own output and not a later replacement.
    """
    if out.is_symlink():
        raise CaptureRefused("owner storage destination is a symlink; refusing")
    try:
        os.mkdir(out)
    except FileExistsError:
        raise CaptureRefused(
            "owner storage destination appeared before publication; refusing to overwrite") from None
    created = os.lstat(out)
    for child in sorted(os.listdir(staged_capsule)):
        os.rename(staged_capsule / child, out / child)
    return (created.st_dev, created.st_ino)


def _remove_created_output(out: Path, identity: tuple[int, int]) -> None:
    """Remove the output this operation created -- only if it is still that exact
    directory. If a replacement with a different inode took its place during
    validation, or it is now a symlink, leave it untouched rather than deleting an
    unrelated directory."""
    try:
        current = os.lstat(out)
    except OSError:
        return
    if stat.S_ISLNK(current.st_mode):
        return
    if (current.st_dev, current.st_ino) != identity:
        return
    shutil.rmtree(out, ignore_errors=True)


# --------------------------------------------------------------------------
# The engine


def capture_backup(
    backend: CaptureBackend,
    request: CaptureRequest,
    out_dir: str | Path,
    *,
    baseline: str = "current-state",
) -> CaptureResult:
    """Capture a backup through ``backend`` into ``out_dir`` and prove it.

    ``out_dir`` is the owner's own storage. It must not already exist and must
    live outside this repository; its parent must be a real existing directory
    (capture stages beside it and publishes by a same-volume rename). ``baseline``
    is the caller's honest declaration (``stock`` or ``current-state``); the gate
    enforces the one contradiction capsule/1 can show and records the label as
    ``declared``.

    Returns a ``CaptureResult`` only on a full pass, built from a readback of the
    ACTUAL owner-storage copy. Every failure raises a ``CapsuleError``
    (``CaptureRefused`` for this engine's own guards, ``ReadbackRefused`` or
    ``CapsuleError`` from the validator and gate) before a result exists. Only the
    scratch and output this operation created are removed on a refusal; a
    preexisting or raced destination is preserved. Never writes to a device.
    """
    out = Path(out_dir)
    _require_outside_any_repo(out)
    if out.exists() or out.is_symlink():
        raise CaptureRefused("owner storage folder must not exist yet")
    if not out.parent.is_dir() or out.parent.is_symlink():
        raise CaptureRefused("owner storage parent folder must be a real existing directory")

    _check_capabilities(backend, request)

    facts = backend.read_device_facts()
    profile, parsed_facts = _resolve_profile(facts)
    validated_packages = _validate_request(profile, request)

    staging: Path | None = None
    created_identity: tuple[int, int] | None = None
    try:
        staging = Path(tempfile.mkdtemp(prefix=f".{out.name}.tvkit-staging-", dir=out.parent))
        src = staging / "src"
        src.mkdir()

        device_hashes: dict[str, str] = {}
        spec_packages = []
        for package, install_root in validated_packages:
            payload_name = f"{package.package_id}.payload.tar"
            xattr_name = f"{package.package_id}.xattrs.json"
            payload_path = src / payload_name
            xattr_path = src / xattr_name
            payload = _stage_blob(
                backend.capture_package(package.package_id, install_root, max_bytes=MAX_MEMBER_BYTES),
                payload_path, MAX_MEMBER_BYTES, f"payload {package.package_id}")
            xattrs = _stage_blob(
                backend.dump_xattrs(package.package_id, install_root, max_bytes=MAX_XATTR_MANIFEST_BYTES),
                xattr_path, MAX_XATTR_MANIFEST_BYTES, f"xattr manifest {package.package_id}")
            device_hashes[f"payload/{package.package_id}.tar"] = _device_digest(payload, payload_path)
            device_hashes[f"xattrs/{package.package_id}.json"] = _device_digest(xattrs, xattr_path)
            spec_packages.append({
                "packageId": package.package_id,
                "installRoot": install_root,
                "payload": payload_name,
                "xattrManifest": xattr_name,
            })

        registry_changes = []
        for item in request.registry:
            fields = {}
            for field in item.fields:
                before = _read_value(lambda a=item.app_id, f=field: backend.read_registry_field(a, f),
                                     f"registry {item.app_id}.{field}")
                fields[field] = {"before": before, "applied": item.fields[field]}
            registry_changes.append({"appId": item.app_id, "fields": fields})

        system_keys = []
        for item in request.system_keys:
            original = _read_value(lambda k=item.key: backend.read_system_key(k), f"system key {item.key}")
            system_keys.append({
                "key": item.key,
                "original": original,
                "expectedCurrent": item.expected_current,
            })

        # The identity must not have changed under us during the capture.
        after = backend.read_device_facts()
        if not isinstance(after, Mapping):
            raise CaptureRefused("backend device facts must be a mapping")
        try:
            after_parsed = parse_device_facts(dict(after))
        except ProfileError as exc:
            raise CaptureRefused(f"device facts changed to an invalid shape during capture: {exc}") from exc
        if after_parsed != parsed_facts:
            raise CaptureRefused("device identity or fingerprint changed during capture; refusing")

        spec = {
            "schema": _SPEC_SCHEMA,
            "packages": spec_packages,
            "registryChanges": registry_changes,
            "systemKeys": system_keys,
            "ourPackages": [{"packageId": pid} for pid in request.our_packages],
        }
        spec_path = src / "assemble-spec.json"
        with open(spec_path, "x", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(spec, indent=2))

        built = staging / "capsule"
        # assemble runs the full capsule validator (traversal, symlink escape,
        # byte and metadata cross-checks) and writes the capsule into staging.
        _assemble.assemble(spec_path, profile, parsed_facts, built)
        # Gate the staged copy, so a mutated transfer or a bad baseline is refused
        # before owner storage is ever created.
        readback_gate(built, device_hashes, parsed_facts, baseline=baseline)

        # Reserve and populate the output, then prove the ACTUAL owner-storage copy.
        created_identity = _publish(built, out)
        gate = readback_gate(out, device_hashes, parsed_facts, baseline=baseline)
        return CaptureResult(capsule_dir=str(out), baseline=gate.baseline, gate=gate)
    except BaseException:
        # Remove only what this operation created: the output we reserved (if any,
        # and only while it is still that exact directory) and the staging folder.
        # A preexisting, raced, or replaced destination is left untouched.
        if created_identity is not None:
            _remove_created_output(out, created_identity)
        raise
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)

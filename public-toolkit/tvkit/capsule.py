"""Recovery capsule: schema, strict validator and payload verifier.

A capsule holds the OWNER'S OWN original files and before-states, captured
from the owner's TV. It is not a redistributable Samsung firmware bundle and
it must stay off the TV (PC, NAS or a private URL).

Layout, either as a directory or as one uncompressed ``.tar`` bundle::

    capsule.json                    the manifest validated here
    payload/<packageId>.tar         tar of the package tree (bytes, UID/GID, modes)
    xattrs/<packageId>.json         XattrBackup manifest version 1 for that tree

The xattr manifest is the unchanged NativeXattrBackup contract
(tools/xattr-backup/README.md). It has no UID/GID/mode, so the capsule keeps
its own entry table with those fields and requires the two tables to agree
exactly on entry set, kinds, file hashes and link targets.

Nothing here extracts an archive to disk. Archives are listed and streamed.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import os
import posixpath
import stat
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Mapping

from . import strictjson as sj
from .paths import PathRuleError, ancestors, check_relative, is_identifier, is_sha256, parent_of
from .profile import (MATCH_FIELDS, PROFILE_SCHEMA, Profile, ProfileError, device_id_sha256,
                      is_placeholder, match_device)

CAPSULE_SCHEMA = "tvkit-capsule/1"
MANIFEST_NAME = "capsule.json"
PROVENANCE = "owner-device-backup"
SYSTEM_KEY_TYPE = "String"

MAX_ENTRIES = 15_000
MAX_XATTR_BYTES_PER_ENTRY = 1024 * 1024
MAX_XATTR_MANIFEST_BYTES = 32 * 1024 * 1024
MAX_CAPSULE_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_PACKAGES = 64
MAX_MEMBER_BYTES = 4 * 1024 * 1024 * 1024
MAX_ID = 0xFFFFFFFF
KINDS = ("file", "directory", "symlink")


class CapsuleError(ValueError):
    """Any reason to refuse a capsule. Messages never include file bytes or xattr values."""


@dataclass(frozen=True)
class Entry:
    path: str
    kind: str
    uid: int
    gid: int
    mode: int
    size: int | None = None
    sha256: str | None = None
    link_target: str | None = None


@dataclass(frozen=True)
class FileRef:
    file: str
    sha256: str
    size: int


@dataclass(frozen=True)
class Package:
    package_id: str
    install_root: str
    payload: FileRef
    xattr_manifest: FileRef
    entries: tuple[Entry, ...]


@dataclass(frozen=True)
class RegistryChange:
    app_id: str
    fields: Mapping[str, tuple[str, str]]  # field -> (before, applied)


@dataclass(frozen=True)
class SystemKeyChange:
    key: str
    original: str
    expected_current: str
    original_sha256: str
    expected_current_sha256: str


@dataclass
class Capsule:
    profile_id: str
    device: Mapping[str, str]
    packages: tuple[Package, ...]
    registry_changes: tuple[RegistryChange, ...]
    system_key_changes: tuple[SystemKeyChange, ...]
    our_packages: tuple[str, ...]
    payload_verified: bool = False
    device_verified: bool = False
    counts: dict[str, int] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Capsule sources: a directory or an uncompressed tar bundle


class CapsuleSource:
    def names(self) -> set[str]:
        raise NotImplementedError

    def size(self, name: str) -> int:
        raise NotImplementedError

    def open(self, name: str) -> BinaryIO:
        raise NotImplementedError

    def close(self) -> None:
        pass


def _casefold_unique(names: list[str], label: str) -> None:
    seen: dict[str, str] = {}
    for name in names:
        folded = name.casefold()
        if folded in seen:
            raise CapsuleError(f"{label} has duplicate or case-colliding entries")
        seen[folded] = name


class DirectorySource(CapsuleSource):
    def __init__(self, root: Path):
        self.root = root
        if root.is_symlink() or not root.is_dir():
            raise CapsuleError("capsule directory must be a real directory")
        found: list[str] = []
        stack = [(root, "")]
        while stack:
            current, prefix = stack.pop()
            with os.scandir(current) as it:
                for item in it:
                    rel = f"{prefix}{item.name}"
                    if item.is_symlink():
                        raise CapsuleError("capsule directory contains a symbolic link")
                    if item.is_dir(follow_symlinks=False):
                        stack.append((Path(item.path), rel + "/"))
                    elif item.is_file(follow_symlinks=False):
                        found.append(rel)
                    else:
                        raise CapsuleError("capsule directory contains a special file")
        for name in found:
            check_relative(name, "capsule file name")
        _casefold_unique(found, "capsule directory")
        self._names = set(found)

    def names(self) -> set[str]:
        return set(self._names)

    def size(self, name: str) -> int:
        return (self.root / name).stat().st_size

    def open(self, name: str) -> BinaryIO:
        if name not in self._names:
            raise CapsuleError("capsule file is missing")
        return open(self.root / name, "rb")


class BundleSource(CapsuleSource):
    def __init__(self, path: Path):
        if path.is_symlink() or not path.is_file():
            raise CapsuleError("capsule bundle must be a regular file")
        try:
            self._tar = tarfile.open(path, mode="r:")
        except (tarfile.TarError, OSError) as exc:
            raise CapsuleError("capsule bundle is not an uncompressed tar archive") from exc
        try:
            self._index()
        except BaseException:
            self._tar.close()
            raise

    def _index(self) -> None:
        self._members: dict[str, tarfile.TarInfo] = {}
        names: list[str] = []
        for member in self._tar.getmembers():
            name = _normalize_member_name(member.name)
            if member.isdir() and name != ".":
                check_relative(name, "bundle member")
                continue
            if name == ".":
                continue
            if not member.isreg():
                raise CapsuleError("capsule bundle contains a link or special member")
            check_relative(name, "bundle member")
            if name in self._members:
                raise CapsuleError("capsule bundle has duplicate members")
            self._members[name] = member
            names.append(name)
        _casefold_unique(names, "capsule bundle")

    def names(self) -> set[str]:
        return set(self._members)

    def size(self, name: str) -> int:
        return self._members[name].size

    def open(self, name: str) -> BinaryIO:
        if name not in self._members:
            raise CapsuleError("capsule file is missing")
        handle = self._tar.extractfile(self._members[name])
        if handle is None:
            raise CapsuleError("capsule bundle member is not readable")
        return handle  # type: ignore[return-value]

    def close(self) -> None:
        self._tar.close()


def open_source(path: str | Path) -> CapsuleSource:
    target = Path(path)
    if target.is_dir() and not target.is_symlink():
        return DirectorySource(target)
    return BundleSource(target)


# --------------------------------------------------------------------------
# Helpers


def _normalize_member_name(name: str) -> str:
    """``./a/b/`` -> ``a/b``; ``./`` -> ``.``. Anything else is left for the path rules."""
    if name in (".", "./"):
        return "."
    if name.startswith("./"):
        name = name[2:]
    if name.endswith("/"):
        name = name[:-1]
    return name


def _hash_stream(handle: BinaryIO, limit: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    while True:
        chunk = handle.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise CapsuleError("file is larger than its recorded size")
        digest.update(chunk)
    return digest.hexdigest(), total


def _wrap(label: str, func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except (sj.StrictJsonError, PathRuleError, ProfileError) as exc:
        raise CapsuleError(f"{label}: {exc}") from exc


def _link_escapes(entry_path: str, target: str, install_root: str) -> bool:
    """True if the link target resolves outside the package root.

    Restore never follows links; this keeps a capsule from planting a link
    that points the owner's tree at something else on the TV.
    """
    if "\x00" in target or target == "":
        return True
    if target.startswith("/"):
        normalized = posixpath.normpath(target)
        return not (normalized == install_root or normalized.startswith(install_root + "/"))
    base = posixpath.dirname(entry_path) if entry_path != "." else ""
    joined = posixpath.normpath(posixpath.join("/__root__", base, target))
    return not (joined == "/__root__" or joined.startswith("/__root__/"))


MAX_LINK_HOPS = 40


def _resolved_link_escapes(entry_path: str, kinds: Mapping[str, str], links: Mapping[str, str],
                           install_root: str) -> bool:
    """Resolve a link the way the kernel would, through links inside the tree.

    The lexical check alone misses ``current -> .`` plus ``up -> current/..``:
    ``current/..`` looks like the root but really climbs out of it. Here each
    component is walked, and any in-tree symlink met on the way is expanded
    (``..`` after a link is applied to the link's resolved location).
    """
    hops = 0
    stack = entry_path.split("/")[:-1]  # the directory holding the link
    pending = links[entry_path].split("/")
    if links[entry_path].startswith("/"):
        if not (links[entry_path] == install_root or links[entry_path].startswith(install_root + "/")):
            return True
        stack = []
        pending = links[entry_path][len(install_root):].split("/")
    while pending:
        part = pending.pop(0)
        if part in ("", "."):
            continue
        if part == "..":
            if not stack:
                return True
            stack.pop()
            continue
        stack.append(part)
        current = "/".join(stack)
        if kinds.get(current) == "symlink":
            hops += 1
            if hops > MAX_LINK_HOPS:
                return True
            target = links[current]
            stack.pop()
            if target.startswith("/"):
                if not (target == install_root or target.startswith(install_root + "/")):
                    return True
                stack = []
                target = target[len(install_root):]
            pending = target.split("/") + pending
    return False


# --------------------------------------------------------------------------
# Schema


def _parse_fileref(value: Any, label: str) -> FileRef:
    obj = sj.require_object(value, label, ("file", "sha256", "size"))
    name = check_relative(obj["file"], f"{label}.file")
    if name == "." or name == MANIFEST_NAME:
        raise CapsuleError(f"{label}.file is not a payload file name")
    if not is_sha256(obj["sha256"]):
        raise CapsuleError(f"{label}.sha256 must be 64 lowercase hex characters")
    size = sj.require_int(obj["size"], f"{label}.size", maximum=MAX_MEMBER_BYTES)
    return FileRef(name, obj["sha256"], size)


def _parse_entries(value: Any, label: str, install_root: str) -> tuple[Entry, ...]:
    items = sj.require_list(value, label, max_items=MAX_ENTRIES)
    entries: dict[str, Entry] = {}
    for position, item in enumerate(items):
        where = f"{label}[{position}]"
        kind = item.get("kind") if isinstance(item, dict) else None
        optional = {"file": ("size", "sha256"), "symlink": ("linkTarget",)}.get(kind, ())
        obj = sj.require_object(item, where, ("path", "kind", "uid", "gid", "mode") + optional)
        if kind not in KINDS:
            raise CapsuleError(f"{where}.kind must be one of {', '.join(KINDS)}")
        path = check_relative(obj["path"], f"{where}.path")
        if path in entries:
            raise CapsuleError(f"{where} duplicates another entry path")
        entry = Entry(
            path=path,
            kind=kind,
            uid=sj.require_int(obj["uid"], f"{where}.uid", maximum=MAX_ID),
            gid=sj.require_int(obj["gid"], f"{where}.gid", maximum=MAX_ID),
            mode=sj.require_int(obj["mode"], f"{where}.mode", maximum=0o7777),
            size=sj.require_int(obj["size"], f"{where}.size", maximum=MAX_MEMBER_BYTES)
            if kind == "file" else None,
            sha256=obj.get("sha256"),
            link_target=obj.get("linkTarget"),
        )
        if kind == "file" and not is_sha256(entry.sha256):
            raise CapsuleError(f"{where}.sha256 must be 64 lowercase hex characters")
        if kind == "symlink":
            sj.require_str(entry.link_target, f"{where}.linkTarget")
            if _link_escapes(path, entry.link_target, install_root):
                raise CapsuleError(f"{where} is a symbolic link that escapes the package root")
        entries[path] = entry
    _casefold_unique(list(entries), label)
    if "." not in entries or entries["."].kind != "directory":
        raise CapsuleError(f"{label} must contain the root entry '.' as a directory")
    for path, entry in entries.items():
        if path == ".":
            continue
        parent = entries.get(parent_of(path))
        if parent is None or parent.kind != "directory":
            raise CapsuleError(f"{label} entry has a missing or non-directory parent")
        for ancestor in ancestors(path):
            if entries[ancestor].kind == "symlink":
                raise CapsuleError(f"{label} entry is below a symbolic link")
    kinds = {path: entry.kind for path, entry in entries.items()}
    links = {path: entry.link_target for path, entry in entries.items() if entry.kind == "symlink"}
    for path in links:
        if _resolved_link_escapes(path, kinds, links, install_root):
            raise CapsuleError(f"{label} has a symbolic link that escapes the package root through other links")
    return tuple(entries[path] for path in sorted(entries))


def _check_install_root(profile: Profile, package_id: str, install_root: Any, label: str) -> str:
    if type(install_root) is not str:
        raise CapsuleError(f"{label} must be a string")
    allowed = {f"{root}/{package_id}" for root in profile.install_roots}
    if install_root not in allowed:
        raise CapsuleError(f"{label} is not an allowlisted install root for this package")
    return install_root


def _parse_registry(profile: Profile, value: Any) -> tuple[RegistryChange, ...]:
    items = sj.require_list(value, "registryChanges", max_items=256)
    changes: list[RegistryChange] = []
    seen: set[str] = set()
    for position, item in enumerate(items):
        where = f"registryChanges[{position}]"
        obj = sj.require_object(item, where, ("appId", "fields"))
        app_id = obj["appId"]
        if not is_identifier(app_id):
            raise CapsuleError(f"{where}.appId is not a valid app id")
        if profile.is_protected(app_id):
            raise CapsuleError(f"{where}.appId is protected (settings/HDMI/audio/network/security)")
        if app_id in seen:
            raise CapsuleError(f"{where}.appId is a duplicate")
        seen.add(app_id)
        if not isinstance(obj["fields"], dict) or not obj["fields"]:
            raise CapsuleError(f"{where}.fields must be a non-empty object")
        fields: dict[str, tuple[str, str]] = {}
        for name, pair in obj["fields"].items():
            if name not in profile.registry_fields:
                raise CapsuleError(f"{where}.fields has a field outside the allowlist")
            pair_obj = sj.require_object(pair, f"{where}.fields.{name}", ("before", "applied"))
            before, applied = pair_obj["before"], pair_obj["applied"]
            for v in (before, applied):
                if type(v) is not str or v not in profile.registry_values:
                    raise CapsuleError(f"{where}.fields.{name} has a value outside the allowlist")
            if (before in ("0", "1")) != (applied in ("0", "1")):
                raise CapsuleError(f"{where}.fields.{name} mixes numeric and text representations")
            if before == applied:
                raise CapsuleError(f"{where}.fields.{name} records no change")
            fields[name] = (before, applied)
        changes.append(RegistryChange(app_id, fields))
    return tuple(changes)


def _value_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _parse_system_keys(profile: Profile, value: Any) -> tuple[SystemKeyChange, ...]:
    items = sj.require_list(value, "systemKeyChanges", max_items=16)
    changes: list[SystemKeyChange] = []
    seen: set[str] = set()
    for position, item in enumerate(items):
        where = f"systemKeyChanges[{position}]"
        obj = sj.require_object(
            item, where,
            ("key", "type", "original", "expectedCurrent", "originalSha256", "expectedCurrentSha256"),
        )
        key = obj["key"]
        if key not in profile.system_keys:
            raise CapsuleError(f"{where}.key is not an allowlisted startup/Home key")
        if key in seen:
            raise CapsuleError(f"{where}.key is a duplicate")
        seen.add(key)
        if obj["type"] != SYSTEM_KEY_TYPE:
            raise CapsuleError(f"{where}.type must be {SYSTEM_KEY_TYPE}")
        for name in ("original", "expectedCurrent"):
            if not is_identifier(obj[name]):
                raise CapsuleError(f"{where}.{name} is not a valid app id")
        if obj["original"] == obj["expectedCurrent"]:
            raise CapsuleError(f"{where} records no change")
        if _value_sha256(obj["original"]) != obj["originalSha256"]:
            raise CapsuleError(f"{where}.originalSha256 does not bind the original value")
        if _value_sha256(obj["expectedCurrent"]) != obj["expectedCurrentSha256"]:
            raise CapsuleError(f"{where}.expectedCurrentSha256 does not bind the expected value")
        changes.append(SystemKeyChange(
            key, obj["original"], obj["expectedCurrent"],
            obj["originalSha256"], obj["expectedCurrentSha256"],
        ))
    return tuple(changes)


def require_capsule1_profile(profile: Profile) -> None:
    """capsule/1 can only bind the version 1 match fields.

    Its manifest has no place for firmwareBuild or region, so it cannot
    authorize recovery under a profile that matches on them (profile/2). Any
    schema other than tvkit-profile/1 is refused rather than half-checked.
    """
    if profile.schema != PROFILE_SCHEMA:
        raise CapsuleError(
            f"{CAPSULE_SCHEMA} cannot bind firmware build or region, so it cannot be used "
            f"with {profile.schema} profile {profile.profile_id}; refusing"
        )


def parse_manifest(data: Any, profile: Profile) -> Capsule:
    try:
        require_capsule1_profile(profile)
        return _parse_manifest(data, profile)
    except (sj.StrictJsonError, PathRuleError, ProfileError) as exc:
        raise CapsuleError(str(exc)) from exc


def _parse_manifest(data: Any, profile: Profile) -> Capsule:
    top = sj.require_object(
        data, "capsule",
        ("schema", "profileId", "provenance", "device", "packages",
         "registryChanges", "systemKeyChanges", "ourPackages"),
    )
    if top["schema"] != CAPSULE_SCHEMA:
        raise CapsuleError(f"capsule schema must be {CAPSULE_SCHEMA}")
    if top["profileId"] != profile.profile_id:
        raise CapsuleError("capsule was made for a different profile")
    if top["provenance"] != PROVENANCE:
        raise CapsuleError(f"capsule provenance must be {PROVENANCE}")
    device = sj.require_object(top["device"], "capsule.device", MATCH_FIELDS + ("deviceIdSha256",))
    for name in MATCH_FIELDS:
        if device[name] != profile.match[name]:
            raise CapsuleError(f"capsule.device.{name} does not exactly match the profile")
    if is_placeholder(device["deviceIdSha256"]) or not is_sha256(device["deviceIdSha256"]):
        raise CapsuleError("capsule.device.deviceIdSha256 is missing or a placeholder")

    packages: list[Package] = []
    ids: set[str] = set()
    files: set[str] = set()
    for position, item in enumerate(sj.require_list(top["packages"], "packages", max_items=MAX_PACKAGES)):
        where = f"packages[{position}]"
        obj = sj.require_object(item, where, ("packageId", "installRoot", "payload", "xattrManifest", "entries"))
        package_id = obj["packageId"]
        if not is_identifier(package_id):
            raise CapsuleError(f"{where}.packageId is not a valid package id")
        if profile.is_protected(package_id):
            raise CapsuleError(f"{where}.packageId is protected (settings/HDMI/audio/network/security)")
        if package_id.casefold() in ids:
            raise CapsuleError(f"{where}.packageId conflicts with another package")
        ids.add(package_id.casefold())
        install_root = _check_install_root(profile, package_id, obj["installRoot"], f"{where}.installRoot")
        payload = _parse_fileref(obj["payload"], f"{where}.payload")
        xattrs = _parse_fileref(obj["xattrManifest"], f"{where}.xattrManifest")
        for ref in (payload, xattrs):
            if ref.file.casefold() in files:
                raise CapsuleError(f"{where} reuses a file another entry already names")
            files.add(ref.file.casefold())
        entries = _parse_entries(obj["entries"], f"{where}.entries", install_root)
        packages.append(Package(package_id, install_root, payload, xattrs, entries))

    ours: list[str] = []
    for position, item in enumerate(sj.require_list(top["ourPackages"], "ourPackages", max_items=8)):
        where = f"ourPackages[{position}]"
        obj = sj.require_object(item, where, ("packageId",))
        if not is_identifier(obj["packageId"]) or profile.is_protected(obj["packageId"]):
            raise CapsuleError(f"{where}.packageId is not removable")
        if obj["packageId"].casefold() in ids:
            raise CapsuleError(f"{where}.packageId conflicts with a restored package")
        ids.add(obj["packageId"].casefold())
        ours.append(obj["packageId"])

    return Capsule(
        profile_id=profile.profile_id,
        device={name: device[name] for name in MATCH_FIELDS + ("deviceIdSha256",)},
        packages=tuple(packages),
        registry_changes=_parse_registry(profile, top["registryChanges"]),
        system_key_changes=_parse_system_keys(profile, top["systemKeyChanges"]),
        our_packages=tuple(ours),
    )


# --------------------------------------------------------------------------
# XattrBackup manifest v1 (tools/xattr-backup/README.md)


def parse_xattr_manifest(data: Any) -> tuple[str, dict[str, dict[str, Any]]]:
    top = sj.require_object(data, "xattr manifest", ("version", "rootBasename", "entries"))
    if type(top["version"]) is not int or top["version"] != 1:
        raise CapsuleError("xattr manifest version must be 1")
    root_basename = sj.require_str(top["rootBasename"], "xattr manifest rootBasename")
    result: dict[str, dict[str, Any]] = {}
    for position, item in enumerate(sj.require_list(top["entries"], "xattr manifest entries", max_items=MAX_ENTRIES)):
        where = f"xattr manifest entries[{position}]"
        kind = item.get("kind") if isinstance(item, dict) else None
        optional = {"file": ("sha256",), "symlink": ("linkTarget",)}.get(kind, ())
        obj = sj.require_object(item, where, ("path", "kind", "xattrs") + optional)
        if kind not in KINDS:
            raise CapsuleError(f"{where}.kind is invalid")
        path = check_relative(obj["path"], f"{where}.path")
        if path in result:
            raise CapsuleError(f"{where} duplicates another entry path")
        if not isinstance(obj["xattrs"], dict):
            raise CapsuleError(f"{where}.xattrs must be an object")
        total = 0
        for name, encoded in obj["xattrs"].items():
            if name == "" or "\x00" in name or type(encoded) is not str:
                raise CapsuleError(f"{where} has an invalid attribute")
            try:
                raw = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise CapsuleError(f"{where} has an attribute with invalid base64") from exc
            total += len(name.encode("utf-8")) + len(raw)
        if total > MAX_XATTR_BYTES_PER_ENTRY:
            raise CapsuleError(f"{where} exceeds the per-entry attribute limit")
        result[path] = {
            "kind": kind,
            "sha256": obj.get("sha256"),
            "linkTarget": obj.get("linkTarget"),
            "xattrCount": len(obj["xattrs"]),
        }
    return root_basename, result


def _cross_check_xattrs(package: Package, source: CapsuleSource) -> int:
    if source.size(package.xattr_manifest.file) > MAX_XATTR_MANIFEST_BYTES:
        raise CapsuleError("xattr manifest is too large")
    with source.open(package.xattr_manifest.file) as handle:
        raw = handle.read(MAX_XATTR_MANIFEST_BYTES + 1)
    if len(raw) != package.xattr_manifest.size or hashlib.sha256(raw).hexdigest() != package.xattr_manifest.sha256:
        raise CapsuleError(f"{package.package_id}: xattr manifest bytes do not match the capsule hash")
    root_basename, xentries = _wrap("xattr manifest", lambda: parse_xattr_manifest(sj.loads(raw)))
    if root_basename != package.package_id:
        raise CapsuleError(f"{package.package_id}: xattr manifest rootBasename does not match")
    expected = {entry.path: entry for entry in package.entries}
    if set(xentries) != set(expected):
        raise CapsuleError(f"{package.package_id}: xattr manifest entry set differs from the capsule")
    xattr_total = 0
    for path, x in xentries.items():
        entry = expected[path]
        if x["kind"] != entry.kind or x["sha256"] != entry.sha256 or x["linkTarget"] != entry.link_target:
            raise CapsuleError(f"{package.package_id}: xattr manifest disagrees with the capsule entry table")
        xattr_total += x["xattrCount"]
    return xattr_total


# --------------------------------------------------------------------------
# Payload tar verification


def iter_payload_members(handle: BinaryIO) -> Iterator[tuple[tarfile.TarFile, tarfile.TarInfo, str]]:
    try:
        archive = tarfile.open(fileobj=handle, mode="r:")
    except tarfile.TarError as exc:
        raise CapsuleError("payload is not an uncompressed tar archive") from exc
    seen: set[str] = set()
    folded: set[str] = set()
    kinds: dict[str, str] = {}
    with archive:
        for member in archive:
            name = _normalize_member_name(member.name)
            try:
                check_relative(name, "payload member")
            except PathRuleError as exc:
                raise CapsuleError(f"payload member would write outside the package root: {exc}") from exc
            if member.islnk():
                raise CapsuleError("payload contains a hard link")
            if member.issparse() or not (member.isreg() or member.isdir() or member.issym()):
                raise CapsuleError("payload contains a device, FIFO or other special member")
            if name in seen or name.casefold() in folded:
                raise CapsuleError("payload contains duplicate or case-colliding members")
            for ancestor in ancestors(name):
                if kinds.get(ancestor) == "symlink":
                    raise CapsuleError("payload member is below a symbolic link")
            seen.add(name)
            folded.add(name.casefold())
            kinds[name] = "file" if member.isreg() else "directory" if member.isdir() else "symlink"
            yield archive, member, name


def verify_payload(package: Package, source: CapsuleSource) -> int:
    ref = package.payload
    with source.open(ref.file) as handle:
        digest, size = _hash_stream(handle, ref.size)
    if size != ref.size or digest != ref.sha256:
        raise CapsuleError(f"{package.package_id}: payload bytes do not match the capsule hash")
    expected = {entry.path: entry for entry in package.entries}
    found: set[str] = set()
    with source.open(ref.file) as handle:
        data = handle if handle.seekable() else io.BytesIO(handle.read())
        for archive, member, name in iter_payload_members(data):
            entry = expected.get(name)
            if entry is None:
                raise CapsuleError(f"{package.package_id}: payload has a member the capsule does not list")
            kind = "file" if member.isreg() else "directory" if member.isdir() else "symlink"
            if kind != entry.kind:
                raise CapsuleError(f"{package.package_id}: payload member kind differs from the capsule")
            if member.uid != entry.uid or member.gid != entry.gid:
                raise CapsuleError(f"{package.package_id}: payload member UID/GID differs from the capsule")
            if stat.S_IMODE(member.mode) != entry.mode:
                raise CapsuleError(f"{package.package_id}: payload member mode differs from the capsule")
            if kind == "symlink" and member.linkname != entry.link_target:
                raise CapsuleError(f"{package.package_id}: payload link target differs from the capsule")
            if kind == "file":
                if member.size != entry.size:
                    raise CapsuleError(f"{package.package_id}: payload member size differs from the capsule")
                stream = archive.extractfile(member)
                if stream is None:
                    raise CapsuleError(f"{package.package_id}: payload member is unreadable")
                member_digest, member_size = _hash_stream(stream, entry.size or 0)
                if member_digest != entry.sha256 or member_size != entry.size:
                    raise CapsuleError(f"{package.package_id}: payload member bytes differ from the capsule")
            found.add(name)
    if found != set(expected):
        raise CapsuleError(f"{package.package_id}: payload is missing entries the capsule lists")
    return len(found)


# --------------------------------------------------------------------------
# Top level


def validate(
    path: str | Path,
    profile: Profile,
    device_facts: Mapping[str, str] | None = None,
) -> Capsule:
    """Full validation: manifest, device binding, every byte, every entry.

    Raises CapsuleError on the first problem. ``device_verified`` is only
    true when live device facts were supplied and matched.
    """
    source = open_source(path)
    try:
        names = source.names()
        if MANIFEST_NAME not in names:
            raise CapsuleError("capsule has no capsule.json")
        if source.size(MANIFEST_NAME) > MAX_CAPSULE_MANIFEST_BYTES:
            raise CapsuleError("capsule.json is too large")
        with source.open(MANIFEST_NAME) as handle:
            data = _wrap("capsule.json", sj.loads, handle.read())
        capsule = parse_manifest(data, profile)
        referenced = {MANIFEST_NAME}
        for package in capsule.packages:
            referenced.update((package.payload.file, package.xattr_manifest.file))
        if names != referenced:
            raise CapsuleError("capsule contains missing or unexpected files")
        if device_facts is not None:
            _wrap("device facts", match_device, profile, device_facts)
            for name in MATCH_FIELDS:
                if device_facts.get(name) != capsule.device[name]:
                    raise CapsuleError(f"device {name} does not match the capsule; refusing")
            if _wrap("device facts", device_id_sha256, device_facts.get("deviceId")) != capsule.device["deviceIdSha256"]:
                raise CapsuleError("device identifier does not match the capsule; refusing")
            capsule.device_verified = True
        entries = xattrs = 0
        for package in capsule.packages:
            xattrs += _cross_check_xattrs(package, source)
            entries += verify_payload(package, source)
        capsule.payload_verified = True
        capsule.counts = {
            "packages": len(capsule.packages),
            "entries": entries,
            "xattrs": xattrs,
            "registryChanges": len(capsule.registry_changes),
            "systemKeyChanges": len(capsule.system_key_changes),
            "ourPackages": len(capsule.our_packages),
        }
        return capsule
    except (PathRuleError, sj.StrictJsonError) as exc:
        raise CapsuleError(str(exc)) from exc
    finally:
        source.close()


def entries_from_payload(handle: BinaryIO, install_root: str) -> list[dict[str, Any]]:
    """Derive the capsule entry table from a captured payload tar (used by assemble)."""
    result: list[dict[str, Any]] = []
    for archive, member, name in iter_payload_members(handle):
        item: dict[str, Any] = {
            "path": name,
            "kind": "file" if member.isreg() else "directory" if member.isdir() else "symlink",
            "uid": member.uid,
            "gid": member.gid,
            "mode": stat.S_IMODE(member.mode),
        }
        if member.isreg():
            stream = archive.extractfile(member)
            assert stream is not None
            item["sha256"], item["size"] = _hash_stream(stream, member.size)
        if member.issym():
            if _link_escapes(name, member.linkname, install_root):
                raise CapsuleError("payload contains a symbolic link that escapes the package root")
            item["linkTarget"] = member.linkname
        result.append(item)
    result.sort(key=lambda item: item["path"])
    return result


def system_key_record(key: str, original: str, expected_current: str) -> dict[str, str]:
    return {
        "key": key,
        "type": SYSTEM_KEY_TYPE,
        "original": original,
        "expectedCurrent": expected_current,
        "originalSha256": _value_sha256(original),
        "expectedCurrentSha256": _value_sha256(expected_current),
    }

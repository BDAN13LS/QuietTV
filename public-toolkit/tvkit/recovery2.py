"""Recovery capsule ``tvkit-recovery/2``: strict validator and a pure undo planner.

recovery/2 is the versioned extension that capsule/1 cannot hold
(docs/RECOVERY2.md). It adds, over the capsule/1 layout and verifier:

* a device block bound to EVERY ``profile.match_fields`` (profile/2 only, so
  firmwareBuild and region too) plus ``deviceIdSha256``;
* ``externalRefs``: a symlink inside a package that points at an exact,
  read-only file outside it, accepted only when a caller-supplied
  :class:`ExternalRefPolicy` approves that exact package, link, target and
  target evidence;
* ``bootChanges``: per target/field ``{target, field, before, ours,
  beforeSha256, oursSha256}``, accepted only for names and values a separate
  caller-supplied :class:`BootFieldPolicy` approves.

Both policies come from bundled or owner code, never from the capsule. The
capsule cannot mark anything allowed: every object has an exact key set.

Nothing here extracts, writes, removes or talks to a device. Payloads are
streamed through the capsule/1 verifier. :func:`plan_undo` turns explicit
reader evidence into a data-only plan bound to the capsule, profile, device,
policies and evidence by digest; :func:`verify_undo_plan` lets a consumer
re-derive it. capsule/1 plans (``tvkit.plan``) refuse recovery/2 results.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from . import capsule as cap
from . import strictjson as sj
from .capsule import CapsuleError, Package, RegistryChange, SystemKeyChange
from .paths import PathRuleError, check_absolute_posix, check_relative, is_identifier, is_sha256
from .profile import (PROFILE_SCHEMA_V2, Profile, ProfileError, device_id_sha256, is_placeholder,
                      match_device)

RECOVERY_SCHEMA = "tvkit-recovery/2"
EVIDENCE_SCHEMA = "tvkit-recovery-evidence/1"
UNDO_SCHEMA = "tvkit-recovery-undo/1"
EXTERNAL_POLICY_SCHEMA = "tvkit-external-ref-policy/1"
BOOT_POLICY_SCHEMA = "tvkit-boot-field-policy/1"
ACCESS_READ_ONLY = "read-only"
EXTERNAL_KINDS = ("file",)
BOOT_TARGET_KINDS = ("bootcache-record", "service")
# Names that would mean "the whole thing". A field is one typed value, never a cache or blob.
WHOLE_NAMES = frozenset({
    "*", "all", "any", "whole", "entire", "blob", "raw", "bytes", "binary", "data", "content",
    "contents", "file", "cache", "image", "body", "db", "database", "dump",
})
# Never valid external targets: kernel and device trees.
FORBIDDEN_TARGET_ROOTS = ("/dev", "/proc", "/sys")
MAX_EXTERNAL_REFS = 64
MAX_BOOT_CHANGES = 256
MAX_ALLOWED_VALUES = 64
MAX_VALUE_CHARS = 256
MAX_EVIDENCE_BYTES = 1024 * 1024
MAX_POLICY_BYTES = 1024 * 1024

_TARGET = re.compile(r"^(?P<kind>[a-z][a-z0-9-]{0,31}):(?P<record>[A-Za-z0-9][A-Za-z0-9._-]{0,127})$")
_FIELD = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_GLOB = re.compile(r"[*?\[\]{}]")

UNDO_INVARIANTS = (
    "Writes a field only if its current value equals ours (hash-checked); equal to before is a no-op.",
    "Any field that is neither ours nor before refuses the WHOLE plan before any write.",
    "Only the listed target/field pairs; every other record and field is left untouched.",
    "Never replaces a whole boot cache, registry or service file.",
    "External link targets are read-only references; nothing is extracted or written through them.",
)


def _wrapped(func):
    def inner(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except (sj.StrictJsonError, PathRuleError, ProfileError) as exc:
            raise CapsuleError(str(exc)) from exc
    inner.__name__ = func.__name__
    inner.__doc__ = func.__doc__
    return inner


def canonical_digest(value: Any) -> str:
    """SHA-256 of sorted, compact, UTF-8 JSON. Every binding digest uses this."""
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _value_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def profile_digest(profile: Profile) -> str:
    return canonical_digest({
        "schema": profile.schema,
        "profileId": profile.profile_id,
        "match": dict(profile.match),
        "installRoots": list(profile.install_roots),
        "registry": {"table": profile.registry_table, "key": profile.registry_key,
                     "fields": list(profile.registry_fields), "values": sorted(profile.registry_values)},
        "systemKeys": list(profile.system_keys),
        "protectedIdSubstrings": list(profile.protected_substrings),
    })


# --------------------------------------------------------------------------
# Guards (module level, so a test can switch each one off and see it matter)


def require_profile2(profile: Profile) -> None:
    """recovery/2 binds firmwareBuild and region, so it needs a profile/2."""
    if not isinstance(profile, Profile) or profile.schema != PROFILE_SCHEMA_V2:
        raise CapsuleError(f"{RECOVERY_SCHEMA} needs a {PROFILE_SCHEMA_V2} profile; refusing")


def refuse_opaque_whole(target: Any, field: Any) -> tuple[str, str]:
    """A boot change names ONE typed field of ONE record, never a whole cache or blob.

    Binary cache parsing is deferred: a complete cache can never be approved as
    an opaque field, whatever a policy or a manifest says.
    """
    if type(target) is not str or type(field) is not str:
        raise CapsuleError("boot target and field must be strings")
    match = _TARGET.match(target)
    if not match or match.group("kind") not in BOOT_TARGET_KINDS:
        raise CapsuleError(
            f"boot target must be <{'|'.join(BOOT_TARGET_KINDS)}>:<recordId>, not a path or whole cache")
    if match.group("record").lower() in WHOLE_NAMES:
        raise CapsuleError("boot target names a whole cache, not one record; refusing")
    if not _FIELD.match(field) or field.lower() in WHOLE_NAMES:
        raise CapsuleError("boot field must be one named field, never a whole cache/blob; refusing")
    return target, field


def check_external_target(target: Any, profile: Profile) -> str:
    """An exact absolute file path outside every install root and never an ancestor of one."""
    path = check_absolute_posix(target, "external target")
    if _GLOB.search(path):
        raise CapsuleError("external target contains a wildcard; only exact paths are allowed")
    for root in FORBIDDEN_TARGET_ROOTS:
        if path == root or path.startswith(root + "/"):
            raise CapsuleError("external target is in a kernel or device tree; refusing")
    for root in profile.install_roots:
        if path == root or path.startswith(root + "/") or root.startswith(path + "/"):
            raise CapsuleError("external target is inside, or an ancestor of, an install root; refusing")
    return path


# --------------------------------------------------------------------------
# Trusted policies (caller supplied; never read from a capsule)


@dataclass(frozen=True)
class TargetEvidence:
    kind: str
    sha256: str
    size: int
    uid: int
    gid: int
    mode: int

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "sha256": self.sha256, "size": self.size,
                "uid": self.uid, "gid": self.gid, "mode": self.mode}


def _parse_target_evidence(value: Any, label: str) -> TargetEvidence:
    obj = sj.require_object(value, label, ("kind", "sha256", "size", "uid", "gid", "mode"))
    if obj["kind"] not in EXTERNAL_KINDS:
        raise CapsuleError(f"{label}.kind must be one of {', '.join(EXTERNAL_KINDS)}")
    if not is_sha256(obj["sha256"]):
        raise CapsuleError(f"{label}.sha256 must be 64 lowercase hex characters")
    return TargetEvidence(
        kind=obj["kind"], sha256=obj["sha256"],
        size=sj.require_int(obj["size"], f"{label}.size", maximum=cap.MAX_MEMBER_BYTES),
        uid=sj.require_int(obj["uid"], f"{label}.uid", maximum=cap.MAX_ID),
        gid=sj.require_int(obj["gid"], f"{label}.gid", maximum=cap.MAX_ID),
        mode=sj.require_int(obj["mode"], f"{label}.mode", maximum=0o7777),
    )


@dataclass(frozen=True)
class ExternalApproval:
    """One exact approved external reference: this package, this link, this target, this file."""
    package_id: str
    link_path: str
    target: str
    evidence: TargetEvidence

    def to_json(self) -> dict[str, Any]:
        return {"packageId": self.package_id, "linkPath": self.link_path, "target": self.target,
                "targetEvidence": self.evidence.to_json()}


@dataclass(frozen=True)
class ExternalRefPolicy:
    profile_id: str
    approvals: tuple[ExternalApproval, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.profile_id, str) or not self.profile_id:
            raise CapsuleError("external policy needs a profile id")
        seen: set[tuple[str, str]] = set()
        for item in self.approvals:
            if not isinstance(item, ExternalApproval) or not isinstance(item.evidence, TargetEvidence):
                raise CapsuleError("external policy entries must be ExternalApproval objects")
            if not is_identifier(item.package_id):
                raise CapsuleError("external policy packageId is not a valid package id")
            if _wrapped(check_relative)(item.link_path, "external policy linkPath") == ".":
                raise CapsuleError("external policy linkPath cannot be the package root")
            _wrapped(check_absolute_posix)(item.target, "external policy target")
            if _GLOB.search(item.target):
                raise CapsuleError("external policy target contains a wildcard")
            _parse_target_evidence(item.evidence.to_json(), "external policy targetEvidence")
            key = (item.package_id, item.link_path)
            if key in seen:
                raise CapsuleError("external policy approves the same link twice")
            seen.add(key)
        object.__setattr__(self, "approvals",
                           tuple(sorted(self.approvals, key=lambda a: (a.package_id, a.link_path))))

    @property
    def digest(self) -> str:
        return canonical_digest({"schema": EXTERNAL_POLICY_SCHEMA, "profileId": self.profile_id,
                                 "approvals": [a.to_json() for a in self.approvals]})

    def lookup(self, package_id: str, link_path: str) -> ExternalApproval | None:
        for item in self.approvals:
            if item.package_id == package_id and item.link_path == link_path:
                return item
        return None


@dataclass(frozen=True)
class BootField:
    """One approved field of one record, with the exact values it may hold."""
    target: str
    field: str
    values: frozenset[str]

    def to_json(self) -> dict[str, Any]:
        return {"target": self.target, "field": self.field, "values": sorted(self.values)}


def _check_value(value: Any, label: str) -> str:
    if type(value) is not str or value == "" or len(value) > MAX_VALUE_CHARS:
        raise CapsuleError(f"{label} must be a non-empty string of at most {MAX_VALUE_CHARS} characters")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise CapsuleError(f"{label} contains a control or NUL character")
    return value


@dataclass(frozen=True)
class BootFieldPolicy:
    profile_id: str
    fields: tuple[BootField, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.profile_id, str) or not self.profile_id:
            raise CapsuleError("boot policy needs a profile id")
        seen: set[tuple[str, str]] = set()
        for item in self.fields:
            if not isinstance(item, BootField) or not isinstance(item.values, frozenset):
                raise CapsuleError("boot policy entries must be BootField objects with a frozenset of values")
            refuse_opaque_whole(item.target, item.field)
            if not 0 < len(item.values) <= MAX_ALLOWED_VALUES:
                raise CapsuleError(f"boot policy field must list 1 to {MAX_ALLOWED_VALUES} exact values")
            for value in item.values:
                _check_value(value, "boot policy value")
            if (item.target, item.field) in seen:
                raise CapsuleError("boot policy approves the same field twice")
            seen.add((item.target, item.field))
        object.__setattr__(self, "fields", tuple(sorted(self.fields, key=lambda f: (f.target, f.field))))

    @property
    def digest(self) -> str:
        return canonical_digest({"schema": BOOT_POLICY_SCHEMA, "profileId": self.profile_id,
                                 "fields": [f.to_json() for f in self.fields]})

    def lookup(self, target: str, field: str) -> BootField | None:
        for item in self.fields:
            if item.target == target and item.field == field:
                return item
        return None


def _load_bytes(raw: Any, label: str, limit: int) -> Any:
    if not isinstance(raw, (bytes, str)):
        raise CapsuleError(f"{label} must be passed as raw JSON bytes, so duplicate keys can be refused")
    if len(raw) > limit:
        raise CapsuleError(f"{label} is too large")
    return sj.loads(raw)


@_wrapped
def external_policy_from_json(raw: bytes | str) -> ExternalRefPolicy:
    top = sj.require_object(_load_bytes(raw, "external policy", MAX_POLICY_BYTES), "external policy",
                            ("schema", "profileId", "approvals"))
    if top["schema"] != EXTERNAL_POLICY_SCHEMA:
        raise CapsuleError(f"external policy schema must be {EXTERNAL_POLICY_SCHEMA}")
    approvals = []
    for position, item in enumerate(sj.require_list(top["approvals"], "approvals", max_items=MAX_EXTERNAL_REFS)):
        where = f"approvals[{position}]"
        obj = sj.require_object(item, where, ("packageId", "linkPath", "target", "targetEvidence"))
        approvals.append(ExternalApproval(
            sj.require_str(obj["packageId"], f"{where}.packageId"),
            sj.require_str(obj["linkPath"], f"{where}.linkPath"),
            sj.require_str(obj["target"], f"{where}.target"),
            _parse_target_evidence(obj["targetEvidence"], f"{where}.targetEvidence")))
    return ExternalRefPolicy(sj.require_str(top["profileId"], "profileId"), tuple(approvals))


@_wrapped
def boot_policy_from_json(raw: bytes | str) -> BootFieldPolicy:
    top = sj.require_object(_load_bytes(raw, "boot policy", MAX_POLICY_BYTES), "boot policy",
                            ("schema", "profileId", "fields"))
    if top["schema"] != BOOT_POLICY_SCHEMA:
        raise CapsuleError(f"boot policy schema must be {BOOT_POLICY_SCHEMA}")
    fields = []
    for position, item in enumerate(sj.require_list(top["fields"], "fields", max_items=MAX_BOOT_CHANGES)):
        where = f"fields[{position}]"
        obj = sj.require_object(item, where, ("target", "field", "values"))
        values = sj.require_list(obj["values"], f"{where}.values", max_items=MAX_ALLOWED_VALUES)
        if len(set(v for v in values if type(v) is str)) != len(values):
            raise CapsuleError(f"{where}.values has duplicates or non-string values")
        fields.append(BootField(obj["target"], obj["field"], frozenset(values)))
    return BootFieldPolicy(sj.require_str(top["profileId"], "profileId"), tuple(fields))


def _require_policies(profile: Profile, external_policy: Any, boot_policy: Any) -> None:
    if not isinstance(external_policy, ExternalRefPolicy) or not isinstance(boot_policy, BootFieldPolicy):
        raise CapsuleError(
            f"{RECOVERY_SCHEMA} needs explicit trusted ExternalRefPolicy and BootFieldPolicy objects; refusing")
    for policy in (external_policy, boot_policy):
        if policy.profile_id != profile.profile_id:
            raise CapsuleError("a trusted policy was written for a different profile; refusing")


# --------------------------------------------------------------------------
# Manifest


@dataclass(frozen=True)
class ExternalRef:
    package_id: str
    link_path: str
    target: str
    evidence: TargetEvidence
    access: str = ACCESS_READ_ONLY

    def to_json(self) -> dict[str, Any]:
        return {"packageId": self.package_id, "linkPath": self.link_path, "target": self.target,
                "access": self.access, "targetEvidence": self.evidence.to_json()}


@dataclass(frozen=True)
class BootChange:
    target: str
    field: str
    before: str
    ours: str
    before_sha256: str
    ours_sha256: str


@dataclass(frozen=True)
class Recovery2:
    """A validated recovery/2 capsule. Not usable by the capsule/1 plans (see payload_verified)."""
    profile_id: str
    device: Mapping[str, str]
    packages: tuple[Package, ...]
    external_refs: tuple[ExternalRef, ...]
    registry_changes: tuple[RegistryChange, ...]
    system_key_changes: tuple[SystemKeyChange, ...]
    boot_changes: tuple[BootChange, ...]
    our_packages: tuple[str, ...]
    manifest_sha256: str
    profile_digest: str
    external_policy_digest: str
    boot_policy_digest: str
    device_bound: bool
    counts: Mapping[str, int]

    @property
    def payload_verified(self) -> bool:
        # tvkit.plan.restore_plan/setup_plan read this first. They know nothing of bootChanges
        # or externalRefs, so they must refuse rather than silently drop them.
        raise CapsuleError(
            f"{RECOVERY_SCHEMA} carries bootChanges and externalRefs that the capsule/1 setup and "
            "restore plans cannot represent; refusing (use tvkit.recovery2.plan_undo)")


def check_external_ref(ref: ExternalRef, policy: ExternalRefPolicy, profile: Profile) -> None:
    """The ref must equal a trusted approval exactly: unknown or stale refuses."""
    check_external_target(ref.target, profile)
    approval = policy.lookup(ref.package_id, ref.link_path)
    if approval is None:
        raise CapsuleError("external reference is unknown to the trusted policy; holding this component")
    if approval.target != ref.target or approval.evidence != ref.evidence:
        raise CapsuleError("external reference is stale: target or its evidence differs from the trusted policy")


def _parse_external_refs(profile: Profile, value: Any, policy: ExternalRefPolicy) -> tuple[ExternalRef, ...]:
    refs: dict[tuple[str, str], ExternalRef] = {}
    for position, item in enumerate(sj.require_list(value, "externalRefs", max_items=MAX_EXTERNAL_REFS)):
        where = f"externalRefs[{position}]"
        obj = sj.require_object(item, where, ("packageId", "linkPath", "target", "access", "targetEvidence"))
        if not is_identifier(obj["packageId"]):
            raise CapsuleError(f"{where}.packageId is not a valid package id")
        link_path = check_relative(obj["linkPath"], f"{where}.linkPath")
        if link_path == ".":
            raise CapsuleError(f"{where}.linkPath cannot be the package root")
        if obj["access"] != ACCESS_READ_ONLY:
            raise CapsuleError(f"{where}.access must be {ACCESS_READ_ONLY}")
        ref = ExternalRef(obj["packageId"], link_path, sj.require_str(obj["target"], f"{where}.target"),
                          _parse_target_evidence(obj["targetEvidence"], f"{where}.targetEvidence"))
        if (ref.package_id, ref.link_path) in refs:
            raise CapsuleError(f"{where} duplicates another external reference")
        check_external_ref(ref, policy, profile)
        refs[(ref.package_id, ref.link_path)] = ref
    return tuple(refs[key] for key in sorted(refs))


def parse_boot_changes(profile: Profile, value: Any, policy: BootFieldPolicy) -> tuple[BootChange, ...]:
    changes: dict[tuple[str, str], BootChange] = {}
    for position, item in enumerate(sj.require_list(value, "bootChanges", max_items=MAX_BOOT_CHANGES)):
        where = f"bootChanges[{position}]"
        obj = sj.require_object(item, where, ("target", "field", "before", "ours", "beforeSha256", "oursSha256"))
        target, field = refuse_opaque_whole(obj["target"], obj["field"])
        if profile.is_protected(target.split(":", 1)[1]):
            raise CapsuleError(f"{where}.target names a protected record")
        if (target, field) in changes:
            raise CapsuleError(f"{where} duplicates another boot change for the same field")
        approved = policy.lookup(target, field)
        if approved is None:
            raise CapsuleError(f"{where} names a field the trusted boot policy does not approve")
        before = _check_value(obj["before"], f"{where}.before")
        ours = _check_value(obj["ours"], f"{where}.ours")
        if before not in approved.values or ours not in approved.values:
            raise CapsuleError(f"{where} has a value the trusted boot policy does not approve")
        if before == ours:
            raise CapsuleError(f"{where} records no change")
        if obj["beforeSha256"] != _value_sha256(before) or obj["oursSha256"] != _value_sha256(ours):
            raise CapsuleError(f"{where} digest does not bind its value")
        changes[(target, field)] = BootChange(target, field, before, ours, obj["beforeSha256"], obj["oursSha256"])
    return tuple(changes[key] for key in sorted(changes))


def _parse_device(profile: Profile, value: Any) -> dict[str, str]:
    fields = profile.match_fields
    device = sj.require_object(value, "capsule.device", fields + ("deviceIdSha256",))
    for name in fields:
        if type(device[name]) is not str or device[name] != profile.match[name]:
            raise CapsuleError(f"capsule.device.{name} does not exactly match the profile")
    if is_placeholder(device["deviceIdSha256"]) or not is_sha256(device["deviceIdSha256"]):
        raise CapsuleError("capsule.device.deviceIdSha256 is missing or a placeholder")
    return {name: device[name] for name in fields + ("deviceIdSha256",)}


@_wrapped
def parse_manifest(data: Any, profile: Profile, external_policy: ExternalRefPolicy,
                   boot_policy: BootFieldPolicy) -> dict[str, Any]:
    require_profile2(profile)
    _require_policies(profile, external_policy, boot_policy)
    top = sj.require_object(
        data, "capsule",
        ("schema", "profileId", "provenance", "device", "packages", "externalRefs",
         "registryChanges", "systemKeyChanges", "bootChanges", "ourPackages"),
    )
    if top["schema"] != RECOVERY_SCHEMA:
        raise CapsuleError(f"capsule schema must be {RECOVERY_SCHEMA}")
    if top["profileId"] != profile.profile_id:
        raise CapsuleError("capsule was made for a different profile")
    if top["provenance"] != cap.PROVENANCE:
        raise CapsuleError(f"capsule provenance must be {cap.PROVENANCE}")
    device = _parse_device(profile, top["device"])
    refs = _parse_external_refs(profile, top["externalRefs"], external_policy)
    links: dict[str, dict[str, str]] = {}
    for ref in refs:
        links.setdefault(ref.package_id, {})[ref.link_path] = ref.target
    packages, ids = cap._parse_packages(profile, top["packages"], links)
    by_id = {package.package_id: package for package in packages}
    for ref in refs:
        package = by_id.get(ref.package_id)
        if package is None:
            raise CapsuleError("external reference names a package this capsule does not restore")
        entry = next((e for e in package.entries if e.path == ref.link_path), None)
        if entry is None or entry.kind != "symlink" or entry.link_target != ref.target:
            raise CapsuleError("external reference does not match a symbolic link in its package")
    ours = cap._parse_our_packages(profile, top["ourPackages"], ids)
    return {
        "device": device,
        "packages": tuple(packages),
        "external_refs": refs,
        "registry_changes": cap._parse_registry(profile, top["registryChanges"]),
        "system_key_changes": cap._parse_system_keys(profile, top["systemKeyChanges"]),
        "boot_changes": parse_boot_changes(profile, top["bootChanges"], boot_policy),
        "our_packages": tuple(ours),
    }


def bind_device(profile: Profile, device: Mapping[str, str], device_facts: Mapping[str, str]) -> None:
    """Live facts must match the profile AND the capsule on every match field, plus the device id."""
    match_device(profile, device_facts)
    for name in profile.match_fields:
        if device_facts.get(name) != device[name]:
            raise CapsuleError(f"device {name} does not match the capsule; refusing")
    if device_id_sha256(device_facts.get("deviceId")) != device["deviceIdSha256"]:
        raise CapsuleError("device identifier does not match the capsule; refusing")


@_wrapped
def validate(path: str | Path, profile: Profile, device_facts: Mapping[str, str] | None = None, *,
             external_policy: ExternalRefPolicy, boot_policy: BootFieldPolicy) -> Recovery2:
    """Full validation of a recovery/2 folder or bundle: manifest, policies, device, every byte."""
    require_profile2(profile)
    _require_policies(profile, external_policy, boot_policy)
    source = cap.open_source(path)
    try:
        raw = cap.read_manifest_bytes(source)
        parsed = parse_manifest(sj.loads(raw), profile, external_policy, boot_policy)
        referenced = {cap.MANIFEST_NAME}
        for package in parsed["packages"]:
            referenced.update((package.payload.file, package.xattr_manifest.file))
        if source.names() != referenced:
            raise CapsuleError("capsule contains missing or unexpected files")
        bound = False
        if device_facts is not None:
            bind_device(profile, parsed["device"], device_facts)
            bound = True
        entries = xattrs = 0
        for package in parsed["packages"]:
            xattrs += cap._cross_check_xattrs(package, source)
            entries += cap.verify_payload(package, source)
    finally:
        source.close()
    counts = {
        "packages": len(parsed["packages"]), "entries": entries, "xattrs": xattrs,
        "externalRefs": len(parsed["external_refs"]), "registryChanges": len(parsed["registry_changes"]),
        "systemKeyChanges": len(parsed["system_key_changes"]), "bootChanges": len(parsed["boot_changes"]),
        "ourPackages": len(parsed["our_packages"]),
    }
    return Recovery2(
        profile_id=profile.profile_id,
        device=MappingProxyType(dict(parsed["device"])),
        packages=parsed["packages"],
        external_refs=parsed["external_refs"],
        registry_changes=parsed["registry_changes"],
        system_key_changes=parsed["system_key_changes"],
        boot_changes=parsed["boot_changes"],
        our_packages=parsed["our_packages"],
        manifest_sha256=hashlib.sha256(raw).hexdigest(),
        profile_digest=profile_digest(profile),
        external_policy_digest=external_policy.digest,
        boot_policy_digest=boot_policy.digest,
        device_bound=bound,
        counts=MappingProxyType(counts),
    )


def validate_any(path: str | Path, profile: Profile, device_facts: Mapping[str, str] | None = None, *,
                 external_policy: ExternalRefPolicy | None = None,
                 boot_policy: BootFieldPolicy | None = None):
    """Dispatch on the manifest's exact schema string; anything else is refused.

    ``tvkit-capsule/1`` goes to the unchanged ``capsule.validate`` (policies must
    not be passed: capsule/1 cannot use them). ``tvkit-recovery/2`` goes to
    :func:`validate` and needs both trusted policies. Each validator re-checks
    the schema itself, so a file swapped between the two reads is refused.
    """
    schema = cap.manifest_schema(path)
    if schema == cap.CAPSULE_SCHEMA:
        if external_policy is not None or boot_policy is not None:
            raise CapsuleError(f"{cap.CAPSULE_SCHEMA} has no external references or boot changes; "
                               "refusing policies it cannot use")
        return cap.validate(path, profile, device_facts)
    if schema == RECOVERY_SCHEMA:
        return validate(path, profile, device_facts, external_policy=external_policy, boot_policy=boot_policy)
    raise CapsuleError("capsule schema is not supported; refusing")


# --------------------------------------------------------------------------
# Reader evidence and the pure undo plan


@dataclass(frozen=True)
class Evidence:
    profile_id: str
    device_id_sha256: str
    manifest_sha256: str
    boot_values: Mapping[tuple[str, str], str]
    external_targets: Mapping[str, TargetEvidence]
    digest: str


@_wrapped
def parse_evidence(raw: bytes | str) -> Evidence:
    """What a reader saw on the TV, as raw strict JSON. Exact keys; every digest re-checked."""
    data = _load_bytes(raw, "reader evidence", MAX_EVIDENCE_BYTES)
    top = sj.require_object(data, "evidence", ("schema", "profileId", "deviceIdSha256", "manifestSha256",
                                               "bootRecords", "externalTargets"))
    if top["schema"] != EVIDENCE_SCHEMA:
        raise CapsuleError(f"evidence schema must be {EVIDENCE_SCHEMA}")
    for name in ("deviceIdSha256", "manifestSha256"):
        if not is_sha256(top[name]):
            raise CapsuleError(f"evidence.{name} must be 64 lowercase hex characters")
    values: dict[tuple[str, str], str] = {}
    for position, item in enumerate(sj.require_list(top["bootRecords"], "bootRecords", max_items=MAX_BOOT_CHANGES)):
        where = f"evidence.bootRecords[{position}]"
        obj = sj.require_object(item, where, ("target", "field", "value", "valueSha256"))
        key = refuse_opaque_whole(obj["target"], obj["field"])
        value = _check_value(obj["value"], f"{where}.value")
        if obj["valueSha256"] != _value_sha256(value):
            raise CapsuleError(f"{where}.valueSha256 does not bind its value")
        if key in values:
            raise CapsuleError(f"{where} duplicates another record field")
        values[key] = value
    targets: dict[str, TargetEvidence] = {}
    for position, item in enumerate(sj.require_list(top["externalTargets"], "externalTargets",
                                                    max_items=MAX_EXTERNAL_REFS)):
        where = f"evidence.externalTargets[{position}]"
        obj = sj.require_object(item, where, ("target", "targetEvidence"))
        target = check_absolute_posix(obj["target"], f"{where}.target")
        if target in targets:
            raise CapsuleError(f"{where} duplicates another external target")
        targets[target] = _parse_target_evidence(obj["targetEvidence"], f"{where}.targetEvidence")
    return Evidence(sj.require_str(top["profileId"], "evidence.profileId"), top["deviceIdSha256"],
                    top["manifestSha256"], MappingProxyType(values), MappingProxyType(targets),
                    canonical_digest(data))


def classify_current(change: BootChange, current: str) -> str:
    """Three-way compare. ``undo`` if current is ours, ``noop`` if before, otherwise refuse."""
    digest = _value_sha256(current)
    if current == change.ours and digest == change.ours_sha256:
        return "undo"
    if current == change.before and digest == change.before_sha256:
        return "noop"
    raise CapsuleError(
        f"{change.target} {change.field}: current value is neither ours nor before; "
        "refusing the whole plan, nothing written")


@dataclass(frozen=True)
class UndoAction:
    target: str
    field: str
    require_current: str
    require_current_sha256: str
    write: str
    write_sha256: str
    op: str = "compare-and-set-field"

    def to_json(self) -> dict[str, str]:
        return {"op": self.op, "target": self.target, "field": self.field,
                "requireCurrent": self.require_current, "requireCurrentSha256": self.require_current_sha256,
                "write": self.write, "writeSha256": self.write_sha256}


@dataclass(frozen=True)
class ExternalCheck:
    package_id: str
    link_path: str
    target: str
    evidence: TargetEvidence
    op: str = "verify-external-target-read-only"

    def to_json(self) -> dict[str, Any]:
        return {"op": self.op, "packageId": self.package_id, "linkPath": self.link_path,
                "target": self.target, "targetEvidence": self.evidence.to_json()}


@dataclass(frozen=True)
class UndoPlan:
    """Data only. Consumers re-derive it with :func:`verify_undo_plan` before acting on it."""
    profile_id: str
    profile_digest: str
    device_id_sha256: str
    manifest_sha256: str
    external_policy_digest: str
    boot_policy_digest: str
    evidence_digest: str
    actions: tuple[UndoAction, ...]
    already_before: tuple[tuple[str, str], ...]
    external_checks: tuple[ExternalCheck, ...]
    plan_digest: str

    def body(self) -> dict[str, Any]:
        return {
            "schema": UNDO_SCHEMA,
            "profileId": self.profile_id,
            "profileDigest": self.profile_digest,
            "deviceIdSha256": self.device_id_sha256,
            "manifestSha256": self.manifest_sha256,
            "externalPolicyDigest": self.external_policy_digest,
            "bootPolicyDigest": self.boot_policy_digest,
            "evidenceDigest": self.evidence_digest,
            "invariants": list(UNDO_INVARIANTS),
            "actions": [a.to_json() for a in self.actions],
            "alreadyBefore": [{"target": t, "field": f} for t, f in self.already_before],
            "externalChecks": [c.to_json() for c in self.external_checks],
        }

    def to_json(self) -> dict[str, Any]:
        return {**self.body(), "planDigest": self.plan_digest}


def _bind_evidence(record: Recovery2, evidence: Evidence) -> None:
    if evidence.profile_id != record.profile_id:
        raise CapsuleError("reader evidence is for a different profile; refusing")
    if evidence.device_id_sha256 != record.device["deviceIdSha256"]:
        raise CapsuleError("reader evidence is from a different device; refusing")
    if evidence.manifest_sha256 != record.manifest_sha256:
        raise CapsuleError("reader evidence was taken for a different capsule; refusing")
    wanted = {(c.target, c.field) for c in record.boot_changes}
    if set(evidence.boot_values) != wanted:
        raise CapsuleError("reader evidence does not cover exactly the capsule's boot fields; refusing")
    refs = {ref.target: ref for ref in record.external_refs}
    if set(evidence.external_targets) != set(refs):
        raise CapsuleError("reader evidence does not cover exactly the capsule's external targets; refusing")
    for target, seen in evidence.external_targets.items():
        if seen != refs[target].evidence:
            raise CapsuleError("external target on the device differs from its recorded evidence (stale); refusing")


def plan_undo(path: str | Path, profile: Profile, device_facts: Mapping[str, str], evidence: bytes | str, *,
              external_policy: ExternalRefPolicy, boot_policy: BootFieldPolicy) -> UndoPlan:
    """Re-validate the capsule against live facts, then three-way compare every boot field.

    Every field is classified before any action is built, so one stale field
    refuses the whole plan. Takes the capsule PATH, never a validated object a
    caller could have constructed.
    """
    if device_facts is None:
        raise CapsuleError("planning an undo needs live device facts; refusing")
    record = validate(path, profile, device_facts, external_policy=external_policy, boot_policy=boot_policy)
    seen = parse_evidence(evidence)
    _bind_evidence(record, seen)
    decisions = [(change, classify_current(change, seen.boot_values[(change.target, change.field)]))
                 for change in record.boot_changes]
    actions = tuple(UndoAction(c.target, c.field, c.ours, c.ours_sha256, c.before, c.before_sha256)
                    for c, kind in decisions if kind == "undo")
    already = tuple((c.target, c.field) for c, kind in decisions if kind == "noop")
    checks = tuple(ExternalCheck(r.package_id, r.link_path, r.target, r.evidence) for r in record.external_refs)
    draft = UndoPlan(record.profile_id, record.profile_digest, record.device["deviceIdSha256"],
                     record.manifest_sha256, record.external_policy_digest, record.boot_policy_digest,
                     seen.digest, actions, already, checks, "")
    return UndoPlan(**{**draft.__dict__, "plan_digest": canonical_digest(draft.body())})


def verify_undo_plan(plan: UndoPlan, path: str | Path, profile: Profile, device_facts: Mapping[str, str],
                     evidence: bytes | str, *, external_policy: ExternalRefPolicy,
                     boot_policy: BootFieldPolicy) -> UndoPlan:
    """Re-derive the plan from the same inputs; any difference (or a bad digest) refuses."""
    if not isinstance(plan, UndoPlan):
        raise CapsuleError("not an undo plan")
    if plan.plan_digest != canonical_digest(plan.body()):
        raise CapsuleError("undo plan digest does not bind its contents; refusing")
    fresh = plan_undo(path, profile, device_facts, evidence,
                      external_policy=external_policy, boot_policy=boot_policy)
    if fresh.to_json() != plan.to_json():
        raise CapsuleError("undo plan does not match the capsule, profile, device, policies or evidence; refusing")
    return fresh

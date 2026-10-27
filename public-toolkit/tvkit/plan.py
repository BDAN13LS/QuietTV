"""Ordered setup and restore plans built from a verified capsule.

Plans are data: a numbered list of steps, each naming the transport
capability it needs. Nothing here touches a device. ``transport.apply``
runs a plan only through an implemented adapter.

Restore order (each step gated on the previous one):
  preflight -> payload bytes -> UID/GID -> modes -> xattrs -> re-check modes
  -> verify tree -> move into place -> only the changed registry flags
  -> startup/Home keys (hash-bound compare-and-set) -> verify control
  -> remove our own packages LAST.

The registry SQL borrows the guard pattern of tools/plan_optional_apps.py:
BEGIN IMMEDIATE, a temp guard table with CHECK (ok = 1), every assertion
before any UPDATE, and only the fields the capsule recorded as changed.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping

from .capsule import Capsule, CapsuleError, Package, RegistryChange, SystemKeyChange
from .paths import is_identifier, is_sha256
from .profile import Profile

STAGING_DIR = ".tvkit-restore"
REHEARSAL_DIR = ".tvkit-rehearsal"
GUARD_TABLE = "__tvkit_guard"

# Actions a plan may never contain, whatever produced it.
FORBIDDEN_ACTIONS = frozenset({
    "replace-registry-database",
    "restore-global-cache",
    "flash-firmware",
    "write-unlisted-key",
})

PLAN_INVARIANTS = (
    "Never replaces a whole registry database; only the changed fields of the listed app ids.",
    "Never restores a stale global cache file.",
    "Never writes a startup/Home key unless its current value hashes to the value the capsule expects.",
    "Never touches Settings, HDMI, audio, network or core security ids (profile protected list).",
    "Removes the owner's own launcher packages last, only after control is verified.",
)


@dataclass
class Step:
    number: int
    phase: str
    action: str
    target: str
    requires: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class Plan:
    kind: str
    profile_id: str
    steps: list[Step]
    invariants: tuple[str, ...] = PLAN_INVARIANTS
    manual_prerequisites: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "profileId": self.profile_id,
            "invariants": list(self.invariants),
            "manualPrerequisites": list(self.manual_prerequisites),
            "steps": [asdict(step) for step in self.steps],
        }

    def capabilities(self) -> set[str]:
        return {step.requires for step in self.steps if step.requires != "local"}


class _Builder:
    def __init__(self) -> None:
        self.steps: list[Step] = []

    def add(self, phase: str, action: str, target: str, requires: str, **detail: Any) -> None:
        if action in FORBIDDEN_ACTIONS:
            raise CapsuleError(f"plan action {action} is forbidden")
        self.steps.append(Step(len(self.steps) + 1, phase, action, target, requires, detail))


# --------------------------------------------------------------------------
# Registry SQL (guarded, changed fields only)


def _literal(value: str) -> str:
    if value in ("0", "1"):
        return value
    return "'" + value.replace("'", "''") + "'"


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def registry_sql(profile: Profile, changes: Iterable[RegistryChange], direction: str) -> str:
    """``direction`` is ``restore`` (applied -> before) or ``apply`` (before -> applied)."""
    if direction not in ("restore", "apply"):
        raise ValueError("direction must be restore or apply")
    changes = list(changes)
    table, key = profile.registry_table, profile.registry_key
    lines = [
        "BEGIN IMMEDIATE;",
        f"CREATE TEMP TABLE IF NOT EXISTS {GUARD_TABLE} (\n  ok INTEGER NOT NULL CHECK (ok = 1)\n);",
    ]
    for change in changes:
        expect = {f: (pair[1] if direction == "restore" else pair[0]) for f, pair in change.fields.items()}
        app = _quote(change.app_id)
        matches = "\n".join(f"        AND {f} IS {_literal(v)}" for f, v in sorted(expect.items()))
        lines.append(
            f"INSERT INTO {GUARD_TABLE} (ok)\nVALUES (\n  CASE WHEN\n"
            f"    (SELECT COUNT(*) FROM {table} WHERE {key} = {app}) = 1\n"
            f"    AND EXISTS (\n      SELECT 1 FROM {table}\n      WHERE {key} = {app}\n{matches}\n    )\n"
            "  THEN 1 ELSE 0 END\n);"
        )
    for change in changes:
        new = {f: (pair[0] if direction == "restore" else pair[1]) for f, pair in change.fields.items()}
        assignments = ", ".join(f"{f} = {_literal(v)}" for f, v in sorted(new.items()))
        lines.append(f"UPDATE {table}\nSET {assignments}\nWHERE {key} = {_quote(change.app_id)};")
    lines.append("COMMIT;")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Startup/Home keys: exact-line compare (format from docs/STARTUP-SELECTOR-TRIAL.md)

_KEY_LINE = re.compile(r"^\[system\] (?P<key>\S+) = String: (?P<value>.*)$")


def system_key_line(key: str, value: str) -> str:
    return f"[system] {key} = String: {value}"


def check_key_line(line: str, key: str, expected_sha256: str) -> str:
    """Parse one exact key line and require the value to hash to ``expected_sha256``.

    Returns the value. Raises naming only the key, never the value. A trailing
    newline is the only tolerated difference; nothing else is normalized.
    """
    if line.endswith("\n"):
        line = line[:-1]
    match = _KEY_LINE.match(line)
    if not match or match.group("key") != key:
        raise CapsuleError(f"{key}: current line is not in the exact expected format; refusing")
    value = match.group("value")
    if hashlib.sha256(value.encode("utf-8")).hexdigest() != expected_sha256:
        raise CapsuleError(f"{key}: current value is not the one the capsule expects; refusing")
    return value


def _key_step_detail(change: SystemKeyChange, direction: str) -> dict[str, Any]:
    if direction == "restore":
        before, before_hash, after, after_hash = (
            change.expected_current, change.expected_current_sha256, change.original, change.original_sha256)
    else:
        before, before_hash, after, after_hash = (
            change.original, change.original_sha256, change.expected_current, change.expected_current_sha256)
    return {
        "key": change.key,
        "requireCurrentLine": system_key_line(change.key, before),
        "requireCurrentSha256": before_hash,
        "write": after,
        "requireAfterLine": system_key_line(change.key, after),
        "requireAfterSha256": after_hash,
        "otherKeys": "untouched",
    }


# --------------------------------------------------------------------------
# Package steps


def _staging(package: Package, area: str) -> str:
    parent = package.install_root.rsplit("/", 1)[0]
    return f"{parent}/{area}/{package.package_id}"


def _restore_package_steps(b: _Builder, package: Package, area: str, phase: str, final: bool) -> None:
    staging = _staging(package, area)
    pid = package.package_id
    b.add(phase, "stage-files", pid, "stage-file",
          files=[package.payload.file, package.xattr_manifest.file],
          sha256=[package.payload.sha256, package.xattr_manifest.sha256],
          verifyOnDevice=True)
    b.add(phase, "extract-bytes", pid, "extract-tar",
          into=staging, requireAbsent=True, noSameOwner=True, noSamePermissions=True,
          payloadSha256=package.payload.sha256)
    b.add(phase, "restore-ownership", pid, "chown-entries",
          root=staging, entries=len(package.entries), noFollow=True)
    b.add(phase, "restore-modes", pid, "chmod-entries", root=staging, skipSymlinks=True)
    b.add(phase, "restore-xattrs", pid, "xattr-restore", root=staging,
          helper="XattrBackup --restore", manifestSha256=package.xattr_manifest.sha256)
    b.add(phase, "recheck-modes", pid, "chmod-entries", root=staging, reason="ACL xattrs can change group bits")
    b.add(phase, "verify-tree", pid, "verify-tree", root=staging,
          compare=["sha256", "uid", "gid", "mode", "linkTarget", "xattrs"])
    if final:
        b.add(phase, "move-into-place", pid, "move-into-place",
              source=staging, destination=package.install_root, requireDestinationAbsent=True)
        b.add(phase, "verify-installed", pid, "verify-tree", root=package.install_root,
              compare=["sha256", "uid", "gid", "mode", "linkTarget", "xattrs"])
    else:
        b.add(phase, "remove-rehearsal-copy", pid, "remove-scratch", root=staging,
              mustBeBelow=staging.rsplit("/", 1)[0])


def restore_plan(capsule: Capsule, profile: Profile) -> Plan:
    if not capsule.payload_verified:
        raise CapsuleError("refusing to plan from a capsule whose payload was not verified")
    b = _Builder()
    b.add("preflight", "verify-capsule", "capsule", "local", counts=dict(capsule.counts))
    b.add("preflight", "match-device", "device", "read-device-facts",
          profileId=profile.profile_id, deviceIdSha256=capsule.device["deviceIdSha256"])
    for package in capsule.packages:
        b.add("preflight", "check-target", package.package_id, "inspect-path",
              path=package.install_root, require="absent",
              ifPresent="refuse unless byte/metadata identical (then skip this package)")
    for package in capsule.packages:
        _restore_package_steps(b, package, STAGING_DIR, "restore-payload", final=True)
    if capsule.registry_changes:
        b.add("restore-registry", "registry-restore-changed-fields", profile.registry_table,
              "run-guarded-sql", sql=registry_sql(profile, capsule.registry_changes, "restore"),
              appIds=[c.app_id for c in capsule.registry_changes])
    for change in capsule.system_key_changes:
        b.add("restore-startup", "system-key-compare-and-set", change.key, "system-key",
              **_key_step_detail(change, "restore"))
    b.add("verify-control", "verify-control", "owner", "manual-acceptance",
          checks=["Home appears", "remote Up/Down/Left/Right/Enter/Back", "Settings overlay",
                  "TV and HDMI sources", "Store opens", "restored apps launch"])
    for package_id in capsule.our_packages:
        b.add("remove-ours", "remove-our-package", package_id, "remove-package",
              onlyAfter="verify-control passed")
    if capsule.our_packages:
        b.add("verify-control", "verify-control-final", "owner", "manual-acceptance",
              checks=["stock Home appears without our launcher", "remote and Settings work"])
    return Plan("restore", profile.profile_id, b.steps, manual_prerequisites=profile.manual_prerequisites)


def setup_plan(capsule: Capsule, profile: Profile, install: Mapping[str, Any] | None = None) -> Plan:
    """Backup and restore rehearsal BEFORE anything is installed or changed.

    ``capsule`` is the backup produced by the capture phase and already
    validated on the PC; the plan rehearses restoring it into a scratch area
    on the TV, then installs and applies.
    """
    if not capsule.payload_verified:
        raise CapsuleError("refusing to plan setup without a verified backup capsule")
    if install:
        package_id = install.get("packageId")
        if not is_identifier(package_id) or profile.is_protected(package_id):
            raise CapsuleError("install package id is invalid or protected")
        if not is_sha256(install.get("sha256")):
            raise CapsuleError("installing needs the launcher package's pinned SHA-256 (--install-sha256)")
    b = _Builder()
    b.add("preflight", "match-device", "device", "read-device-facts",
          profileId=profile.profile_id, deviceIdSha256=capsule.device["deviceIdSha256"])
    b.add("backup", "verify-backup-capsule", "capsule", "local", counts=dict(capsule.counts),
          storage="off-TV (PC, NAS or private URL)")
    for package in capsule.packages:
        _restore_package_steps(b, package, REHEARSAL_DIR, "rehearsal", final=False)
    if capsule.registry_changes:
        b.add("rehearsal", "registry-rehearsal-on-copy", profile.registry_table, "run-guarded-sql-on-copy",
              sql=registry_sql(profile, capsule.registry_changes, "apply"),
              thenRestoreSql=registry_sql(profile, capsule.registry_changes, "restore"),
              requireRoundTripIdentical=True)
    for change in capsule.system_key_changes:
        b.add("rehearsal", "system-key-compare-only", change.key, "system-key-read",
              requireCurrentLine=system_key_line(change.key, change.original),
              requireCurrentSha256=change.original_sha256)
    b.add("gate", "rehearsal-passed", "plan", "local", require="every rehearsal step passed")
    if install:
        b.add("install", "install-our-package", str(install.get("packageId", "")), "install-package",
              artifactSha256=install.get("sha256"), signedBy="owner certificate (manual prerequisite)")
    if capsule.registry_changes:
        b.add("apply", "registry-apply-changed-fields", profile.registry_table, "run-guarded-sql",
              sql=registry_sql(profile, capsule.registry_changes, "apply"))
    for change in capsule.system_key_changes:
        b.add("apply", "system-key-compare-and-set", change.key, "system-key",
              **_key_step_detail(change, "apply"))
    b.add("verify-control", "verify-control", "owner", "manual-acceptance",
          checks=["our Home appears", "remote navigation", "Settings overlay", "sources", "Store"])
    return Plan("setup", profile.profile_id, b.steps, manual_prerequisites=profile.manual_prerequisites)

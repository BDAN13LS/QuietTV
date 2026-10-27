"""Component catalogue (``tvkit-components/1``) and change-set resolution.

A catalogue describes optional components for ONE profile. It never holds
certificates, device identifiers or owner state. Which packages are actually
on a TV, at which version and payload hash, cannot be derived from device
facts, so ``resolve`` also takes a ``tvkit-inventory/1`` document read from
the device by the owner's transport and bound to that device's id hash. An
inventory entry is matched by exact package id only, never by name or icon.

Nothing here touches a device. ``resolve`` returns an immutable ChangeSet:
data that a later plan builder turns into steps.

Rules (see public-toolkit/docs/D1-ENGINE.md):
- Whole-request refusals (``CatalogueError``, no ChangeSet): device facts do
  not match the profile, catalogue or inventory bound to another profile or
  device, a selection id the catalogue does not know, a malformed selection.
- Per-component holds (listed in ``ChangeSet.held`` with a reason): protected
  id, not reviewed, removal not allowed, restore not supported, not
  installed, installed version or payload hash differs, or a retained
  component still depends on it. Holds cascade until nothing changes, so the
  order of the selection cannot change the result.
- The only action is ``remove``. Nothing is ever downgraded to "disable".
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from . import strictjson as sj
from .paths import is_identifier, is_sha256
from .profile import Profile, ProfileError, device_id_sha256, match_device

CATALOGUE_SCHEMA = "tvkit-components/1"
INVENTORY_SCHEMA = "tvkit-inventory/1"
CHANGESET_SCHEMA = "tvkit-changeset/1"
ACTIONS = frozenset({"remove"})
STATUSES = frozenset({"reviewed", "draft"})
MAX_COMPONENTS = 256
MAX_INVENTORY = 4096
_CATEGORY = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}$")
# Markers that must never appear in a public catalogue.
OWNER_STATE_MARKERS = ("-----begin", "private key", "deviceid", "duid", "certificate:")

COMPONENT_FIELDS = (
    "id", "packageIds", "expectedVersions", "title", "explain", "category", "recommended",
    "allowedActions", "dependsOn", "consequences", "restoreSupported", "evidence", "status",
)


class CatalogueError(ValueError):
    pass


def canonical_sha256(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ExpectedVersion:
    version: str
    sha256: str


@dataclass(frozen=True)
class Component:
    component_id: str
    package_ids: tuple[str, ...]
    expected: Mapping[str, ExpectedVersion]
    title: str
    explain: str
    category: str
    recommended: bool
    allowed_actions: frozenset[str]
    depends_on: tuple[str, ...]
    consequences: tuple[str, ...]
    restore_supported: bool
    evidence: tuple[str, ...]
    status: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "expected", MappingProxyType(dict(self.expected)))


@dataclass(frozen=True)
class Catalogue:
    profile_id: str
    components: Mapping[str, Component]
    sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "components", MappingProxyType(dict(self.components)))

    def dependents_of(self, component_id: str) -> tuple[str, ...]:
        return tuple(sorted(c.component_id for c in self.components.values() if component_id in c.depends_on))


@dataclass(frozen=True)
class InstalledPackage:
    package_id: str
    version: str
    sha256: str
    install_root: str


@dataclass(frozen=True)
class Inventory:
    profile_id: str
    device_id_sha256: str
    packages: Mapping[str, InstalledPackage]
    sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "packages", MappingProxyType(dict(self.packages)))


@dataclass(frozen=True)
class PackageTarget:
    package_id: str
    version: str
    sha256: str
    install_root: str

    @property
    def path(self) -> str:
        return f"{self.install_root}/{self.package_id}"


@dataclass(frozen=True)
class ChangeAction:
    component_id: str
    action: str
    packages: tuple[PackageTarget, ...]


@dataclass(frozen=True)
class Held:
    component_id: str
    reason: str


@dataclass(frozen=True)
class ChangeSet:
    profile_id: str
    device_id_sha256: str
    catalogue_sha256: str
    inventory_sha256: str
    actions: tuple[ChangeAction, ...]
    held: tuple[Held, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": CHANGESET_SCHEMA,
            "profileId": self.profile_id,
            "deviceIdSha256": self.device_id_sha256,
            "catalogueSha256": self.catalogue_sha256,
            "inventorySha256": self.inventory_sha256,
            "actions": [
                {"componentId": a.component_id, "action": a.action,
                 "packages": [{"packageId": p.package_id, "version": p.version, "sha256": p.sha256,
                               "installRoot": p.install_root} for p in a.packages]}
                for a in self.actions
            ],
            "held": [{"componentId": h.component_id, "reason": h.reason} for h in self.held],
        }

    @property
    def digest(self) -> str:
        return canonical_sha256(self.to_json())


# --------------------------------------------------------------------------
# Parsing


def _strings(value: Any, label: str, max_items: int, *, allow_empty_list: bool = True) -> tuple[str, ...]:
    items = sj.require_list(value, label, max_items=max_items)
    if not allow_empty_list and not items:
        raise CatalogueError(f"{label} must not be empty")
    return tuple(sj.require_str(item, f"{label}[]") for item in items)


def _bool(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise CatalogueError(f"{label} must be true or false")
    return value


def _scan_public(value: Any, label: str) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            _scan_public(key, label)
            _scan_public(item, label)
    elif isinstance(value, list):
        for item in value:
            _scan_public(item, label)
    elif isinstance(value, str):
        lowered = value.lower()
        if any(marker in lowered for marker in OWNER_STATE_MARKERS):
            raise CatalogueError(f"{label} holds certificate or owner-state material; public catalogues never do")


def _parse_component(data: Any, where: str) -> Component:
    top = sj.require_object(data, where, COMPONENT_FIELDS)
    cid = sj.require_str(top["id"], f"{where}.id")
    if not is_identifier(cid):
        raise CatalogueError(f"{where}.id is not a valid identifier")
    package_ids = _strings(top["packageIds"], f"{where}.packageIds", 16, allow_empty_list=False)
    if len(set(package_ids)) != len(package_ids):
        raise CatalogueError(f"{where}.packageIds has duplicates")
    for pid in package_ids:
        if not is_identifier(pid):
            raise CatalogueError(f"{where}.packageIds has an invalid package id")
    versions = sj.require_object(top["expectedVersions"], f"{where}.expectedVersions", package_ids)
    expected: dict[str, ExpectedVersion] = {}
    for pid in package_ids:
        item = sj.require_object(versions[pid], f"{where}.expectedVersions.{pid}", ("version", "sha256"))
        version = sj.require_str(item["version"], f"{where}.expectedVersions.{pid}.version")
        if not _VERSION.match(version):
            raise CatalogueError(f"{where}.expectedVersions.{pid}.version is not a concrete version")
        if not is_sha256(item["sha256"]):
            raise CatalogueError(f"{where}.expectedVersions.{pid}.sha256 is not a lowercase SHA-256")
        expected[pid] = ExpectedVersion(version, item["sha256"])
    category = sj.require_str(top["category"], f"{where}.category")
    if not _CATEGORY.match(category):
        raise CatalogueError(f"{where}.category is not a lowercase token")
    actions = _strings(top["allowedActions"], f"{where}.allowedActions", 4)
    if len(set(actions)) != len(actions) or not set(actions) <= ACTIONS:
        raise CatalogueError(f"{where}.allowedActions may only list: {', '.join(sorted(ACTIONS))}")
    depends = _strings(top["dependsOn"], f"{where}.dependsOn", 32)
    if len(set(depends)) != len(depends):
        raise CatalogueError(f"{where}.dependsOn has duplicates")
    if cid in depends:
        raise CatalogueError(f"{where} depends on itself")
    status = sj.require_str(top["status"], f"{where}.status")
    if status not in STATUSES:
        raise CatalogueError(f"{where}.status must be reviewed or draft")
    return Component(
        component_id=cid,
        package_ids=package_ids,
        expected=expected,
        title=sj.require_str(top["title"], f"{where}.title"),
        explain=sj.require_str(top["explain"], f"{where}.explain"),
        category=category,
        recommended=_bool(top["recommended"], f"{where}.recommended"),
        allowed_actions=frozenset(actions),
        depends_on=depends,
        consequences=_strings(top["consequences"], f"{where}.consequences", 16),
        restore_supported=_bool(top["restoreSupported"], f"{where}.restoreSupported"),
        evidence=_strings(top["evidence"], f"{where}.evidence", 16),
        status=status,
    )


def _check_graph(components: Mapping[str, Component]) -> None:
    for comp in components.values():
        for dep in comp.depends_on:
            if dep not in components:
                raise CatalogueError(f"component {comp.component_id} depends on unknown component {dep}")
    state: dict[str, int] = {}  # 1 = on the current path, 2 = done

    def visit(cid: str) -> None:
        state[cid] = 1
        for dep in components[cid].depends_on:
            if state.get(dep) == 1:
                raise CatalogueError(f"dependency cycle through {dep}")
            if state.get(dep) is None:
                visit(dep)
        state[cid] = 2

    for cid in sorted(components):
        if cid not in state:
            visit(cid)


def parse_catalogue(data: Any, profile: Profile) -> Catalogue:
    try:
        top = sj.require_object(data, "catalogue", ("schema", "profileId", "components"))
        if top["schema"] != CATALOGUE_SCHEMA:
            raise CatalogueError(f"catalogue schema must be {CATALOGUE_SCHEMA}")
        if top["profileId"] != profile.profile_id:
            raise CatalogueError("catalogue was made for a different profile")
        _scan_public(data, "catalogue")
        components: dict[str, Component] = {}
        owners: dict[str, str] = {}
        for index, item in enumerate(sj.require_list(top["components"], "catalogue.components",
                                                     max_items=MAX_COMPONENTS)):
            comp = _parse_component(item, f"catalogue.components[{index}]")
            if comp.component_id in components:
                raise CatalogueError(f"duplicate component id {comp.component_id}")
            for pid in comp.package_ids:
                if pid in owners:
                    raise CatalogueError(f"package {pid} is listed by two components")
                owners[pid] = comp.component_id
            components[comp.component_id] = comp
        _check_graph(components)
        return Catalogue(profile.profile_id, components, canonical_sha256(data))
    except sj.StrictJsonError as exc:
        raise CatalogueError(str(exc)) from exc


def load_catalogue(path: str | Path, profile: Profile) -> Catalogue:
    try:
        return parse_catalogue(sj.load_file(path, max_bytes=1024 * 1024), profile)
    except sj.StrictJsonError as exc:
        raise CatalogueError(str(exc)) from exc


def parse_inventory(data: Any, profile: Profile, facts: Mapping[str, str]) -> Inventory:
    """Validate an installed-package inventory and bind it to this device.

    Raises naming the field, never the device identifier.
    """
    try:
        top = sj.require_object(data, "inventory", ("schema", "profileId", "deviceIdSha256", "packages"))
        if top["schema"] != INVENTORY_SCHEMA:
            raise CatalogueError(f"inventory schema must be {INVENTORY_SCHEMA}")
        if top["profileId"] != profile.profile_id:
            raise CatalogueError("inventory was read for a different profile")
        try:
            expected_hash = device_id_sha256(facts.get("deviceId"))
        except ProfileError as exc:
            raise CatalogueError("device identifier is missing or a placeholder") from exc
        if not is_sha256(top["deviceIdSha256"]) or top["deviceIdSha256"] != expected_hash:
            raise CatalogueError("inventory was read from a different device; refusing")
        packages: dict[str, InstalledPackage] = {}
        for index, item in enumerate(sj.require_list(top["packages"], "inventory.packages",
                                                     max_items=MAX_INVENTORY)):
            where = f"inventory.packages[{index}]"
            entry = sj.require_object(item, where, ("packageId", "version", "sha256", "installRoot"))
            pid = entry["packageId"]
            if not is_identifier(pid):
                raise CatalogueError(f"{where}.packageId is not a valid package id")
            if pid in packages:
                raise CatalogueError(f"inventory lists package {pid} twice")
            version = sj.require_str(entry["version"], f"{where}.version")
            if not _VERSION.match(version):
                raise CatalogueError(f"{where}.version is not a concrete version")
            if not is_sha256(entry["sha256"]):
                raise CatalogueError(f"{where}.sha256 is not a lowercase SHA-256")
            if entry["installRoot"] not in profile.install_roots:
                raise CatalogueError(f"{where}.installRoot is not one of the profile's install roots")
            packages[pid] = InstalledPackage(pid, version, entry["sha256"], entry["installRoot"])
        return Inventory(profile.profile_id, top["deviceIdSha256"], packages, canonical_sha256(data))
    except sj.StrictJsonError as exc:
        raise CatalogueError(str(exc)) from exc


# --------------------------------------------------------------------------
# Resolution


def _selection(selection: Any, catalogue: Catalogue) -> frozenset[str]:
    if isinstance(selection, (str, bytes)) or not isinstance(selection, (list, tuple, set, frozenset)):
        raise CatalogueError("selection must be a list of component ids")
    items = list(selection)
    if any(type(item) is not str for item in items):
        raise CatalogueError("selection must be a list of component ids")
    if len(set(items)) != len(items):
        raise CatalogueError("selection lists a component twice")
    unknown = sorted(set(items) - set(catalogue.components))
    if unknown:
        # A typo must never silently shrink the approved list.
        raise CatalogueError(f"selection has {len(unknown)} unknown component id(s); refusing")
    return frozenset(items)


def _component_hold(comp: Component, profile: Profile, inventory: Inventory) -> str | None:
    if profile.is_protected(comp.component_id) or any(profile.is_protected(p) for p in comp.package_ids):
        return "protected-id"
    if comp.status != "reviewed" or not comp.evidence:
        return "not-reviewed"
    if "remove" not in comp.allowed_actions:
        return "remove-not-allowed"
    if not comp.restore_supported:
        return "restore-not-supported"
    present = [p for p in comp.package_ids if p in inventory.packages]
    if not present:
        return "not-installed"
    if len(present) != len(comp.package_ids):
        return "partially-installed"
    for pid in comp.package_ids:
        installed, expected = inventory.packages[pid], comp.expected[pid]
        if installed.version != expected.version:
            return f"installed-version-differs:{pid}"
        if installed.sha256 != expected.sha256:
            return f"installed-hash-differs:{pid}"
    return None


def _is_present(comp: Component, inventory: Inventory) -> bool:
    return any(p in inventory.packages for p in comp.package_ids)


def _removal_order(ids: Iterable[str], catalogue: Catalogue) -> list[str]:
    """Dependents before the components they depend on; ties by id."""
    remaining = set(ids)
    order: list[str] = []
    while remaining:
        ready = sorted(
            cid for cid in remaining
            if not any(cid in catalogue.components[other].depends_on for other in remaining if other != cid)
        )
        if not ready:  # unreachable: parse_catalogue refuses cycles
            raise CatalogueError("dependency cycle in removal set")
        order.append(ready[0])
        remaining.discard(ready[0])
    return order


def resolve(catalogue: Catalogue, selection: Any, profile: Profile, facts: Mapping[str, str],
            inventory: Inventory) -> ChangeSet:
    """Turn the owner's selection (component ids to remove) into an immutable ChangeSet."""
    if catalogue.profile_id != profile.profile_id or inventory.profile_id != profile.profile_id:
        raise CatalogueError("catalogue, inventory and profile are for different profiles")
    try:
        match_device(profile, facts)
        device_hash = device_id_sha256(facts.get("deviceId"))
    except ProfileError as exc:
        raise CatalogueError(str(exc)) from exc
    if inventory.device_id_sha256 != device_hash:
        raise CatalogueError("inventory was read from a different device; refusing")
    chosen = _selection(selection, catalogue)

    held: dict[str, str] = {}
    for cid in sorted(chosen):
        reason = _component_hold(catalogue.components[cid], profile, inventory)
        if reason:
            held[cid] = reason
    removing = set(chosen) - set(held)
    changed = True
    while changed:
        changed = False
        for cid in sorted(removing):
            blockers = [
                dep for dep in catalogue.dependents_of(cid)
                if dep not in removing and _is_present(catalogue.components[dep], inventory)
            ]
            if blockers:
                held[cid] = f"required-by:{blockers[0]}"
                removing.discard(cid)
                changed = True

    actions = []
    for cid in _removal_order(removing, catalogue):
        comp = catalogue.components[cid]
        targets = tuple(
            PackageTarget(pid, inventory.packages[pid].version, inventory.packages[pid].sha256,
                          inventory.packages[pid].install_root)
            for pid in comp.package_ids
        )
        actions.append(ChangeAction(cid, "remove", targets))
    return ChangeSet(
        profile_id=profile.profile_id,
        device_id_sha256=device_hash,
        catalogue_sha256=catalogue.sha256,
        inventory_sha256=inventory.sha256,
        actions=tuple(actions),
        held=tuple(Held(cid, held[cid]) for cid in sorted(held)),
    )


def check_changeset(changeset: ChangeSet, catalogue: Catalogue, profile: Profile, facts: Mapping[str, str],
                    inventory: Inventory) -> None:
    """Refuse a ChangeSet that a fresh ``resolve`` over the same inputs would not produce.

    A ChangeSet is plain data and can be built by hand; anything that acts on
    one (plan builder, audit, states) calls this first.
    """
    selection = [a.component_id for a in changeset.actions] + [h.component_id for h in changeset.held]
    if resolve(catalogue, selection, profile, facts, inventory).digest != changeset.digest:
        raise CatalogueError("change set does not match a fresh resolution; refusing")

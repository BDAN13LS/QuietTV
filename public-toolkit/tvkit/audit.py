"""Write audit: compare two immutable inventories of the TV and list every
difference the change set did not declare.

Pure comparison. Nothing here captures a snapshot from a device; a later
phase's transport produces the before/after inventories. Tree entries are
per-path digests; a directory entry must describe the directory's own
metadata (mode, owner, xattrs), not its listing, so removing a child does not
change its parent's entry. Differences name the path, registry field or key,
never the values.

A declared removal must be one package directory directly below one of the
profile's install roots. It permits exactly that path and the entries below
it to disappear. It permits no additions, no content change, no sibling that
shares a name prefix and nothing in the parent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Iterable, Mapping

from .catalogue import ChangeSet
from .paths import PathRuleError, check_absolute_posix, is_identifier, is_sha256
from .profile import Profile


class AuditError(ValueError):
    pass


def _path(value: object, label: str) -> str:
    try:
        return check_absolute_posix(value, label)
    except PathRuleError as exc:
        raise AuditError(str(exc)) from exc


@dataclass(frozen=True)
class Snapshot:
    tree: Mapping[str, str]  # absolute path -> SHA-256 of the entry (bytes and metadata)
    registry: Mapping[tuple[str, str], str]  # (app id, field) -> value
    keys: Mapping[str, str]  # system key -> SHA-256 of its value

    def __post_init__(self) -> None:
        tree = {}
        for path, digest in dict(self.tree).items():
            if not is_sha256(digest):
                raise AuditError("snapshot tree digest is not a SHA-256")
            tree[_path(path, "snapshot tree path")] = digest
        registry = {}
        for where, value in dict(self.registry).items():
            if (type(where) is not tuple or len(where) != 2 or not all(type(x) is str and x for x in where)
                    or type(value) is not str):
                raise AuditError("snapshot registry entries are (app id, field) -> string")
            registry[where] = value
        keys = {}
        for key, digest in dict(self.keys).items():
            if type(key) is not str or not key or not is_sha256(digest):
                raise AuditError("snapshot keys map a key name to a SHA-256")
            keys[key] = digest
        object.__setattr__(self, "tree", MappingProxyType(tree))
        object.__setattr__(self, "registry", MappingProxyType(registry))
        object.__setattr__(self, "keys", MappingProxyType(keys))


@dataclass(frozen=True)
class DeclaredEffects:
    removed_trees: tuple[str, ...] = ()
    registry: Mapping[tuple[str, str], str] = field(default_factory=dict)  # (app id, field) -> value after
    keys: Mapping[str, str] = field(default_factory=dict)  # key -> SHA-256 of value after
    install_roots: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        trees = []
        for path in self.removed_trees:
            path = _path(path, "declared removal")
            parent, _, name = path.rpartition("/")
            if parent not in self.install_roots or not is_identifier(name):
                raise AuditError("a declared removal must be one package directory directly below an install root")
            trees.append(path)
        object.__setattr__(self, "removed_trees", tuple(sorted(set(trees))))
        object.__setattr__(self, "registry", MappingProxyType(dict(self.registry)))
        object.__setattr__(self, "keys", MappingProxyType(dict(self.keys)))

    def permits_removal(self, path: str) -> bool:
        return any(path == tree or path.startswith(tree + "/") for tree in self.removed_trees)


def declared_from_changeset(changeset: ChangeSet, profile: Profile) -> DeclaredEffects:
    """D1 change sets only remove packages, so only package paths may disappear."""
    if changeset.profile_id != profile.profile_id:
        raise AuditError("change set was resolved for a different profile")
    return DeclaredEffects(removed_trees=tuple(
        target.path for action in changeset.actions if action.action == "remove" for target in action.packages
    ), install_roots=profile.install_roots)


@dataclass(frozen=True)
class Difference:
    kind: str  # tree-added | tree-removed | tree-changed | registry | key
    where: str


@dataclass(frozen=True)
class WriteAudit:
    before: Snapshot
    after: Snapshot

    def unexpected(self, declared: DeclaredEffects) -> tuple[Difference, ...]:
        found: list[Difference] = []
        before, after = self.before.tree, self.after.tree
        for path in sorted(set(before) | set(after)):
            if path in before and path in after:
                if before[path] != after[path]:
                    found.append(Difference("tree-changed", path))
            elif path in before:
                if not declared.permits_removal(path):
                    found.append(Difference("tree-removed", path))
            else:
                found.append(Difference("tree-added", path))
        found += _mapping_diff(self.before.registry, self.after.registry, declared.registry, "registry",
                               lambda where: f"{where[0]}.{where[1]}")
        found += _mapping_diff(self.before.keys, self.after.keys, declared.keys, "key", str)
        return tuple(found)

    def undone(self, declared: DeclaredEffects) -> tuple[str, ...]:
        """Declared removals that did not happen (the path is still there)."""
        return tuple(tree for tree in declared.removed_trees if tree in self.after.tree)


def _mapping_diff(before: Mapping, after: Mapping, declared: Mapping, kind: str, name) -> Iterable[Difference]:
    for where in sorted(set(before) | set(after), key=name):
        if before.get(where) == after.get(where):
            continue
        if where in after and where in declared and after[where] == declared[where]:
            continue
        yield Difference(kind, name(where))

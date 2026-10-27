"""Relative-path rules, copied from the XattrBackup manifest contract.

A path is relative to a package root, uses ``/``, and the root itself is
``"."``. Rejected: absolute, drive or UNC paths, backslashes, empty or ``.``
components (except the root entry), ``..``, NUL and control characters.
"""

from __future__ import annotations

import re

_DRIVE = re.compile(r"^[A-Za-z]:")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
# Tizen package ids and app ids: letters, digits, dot, dash, underscore.
_IDENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class PathRuleError(ValueError):
    pass


def check_relative(path: object, label: str = "path") -> str:
    if type(path) is not str or path == "":
        raise PathRuleError(f"{label} must be a non-empty string")
    if path == ".":
        return path
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in path):
        raise PathRuleError(f"{label} contains a control or NUL character")
    if "\\" in path:
        raise PathRuleError(f"{label} contains a backslash")
    if path.startswith("/") or _DRIVE.match(path) or path.startswith("//"):
        raise PathRuleError(f"{label} is absolute")
    for part in path.split("/"):
        if part in ("", "."):
            raise PathRuleError(f"{label} has an empty or '.' component")
        if part == "..":
            raise PathRuleError(f"{label} contains '..'")
    return path


def parent_of(path: str) -> str:
    if path == ".":
        raise PathRuleError("root has no parent")
    head, _, _ = path.rpartition("/")
    return head or "."


def ancestors(path: str) -> list[str]:
    """Every proper ancestor of ``path``, nearest last, excluding the root."""
    if path == ".":
        return []
    parts = path.split("/")
    return ["/".join(parts[:i]) for i in range(1, len(parts))]


def is_sha256(value: object) -> bool:
    return type(value) is str and bool(_SHA256.match(value))


def is_identifier(value: object) -> bool:
    return type(value) is str and bool(_IDENT.match(value)) and ".." not in value


def check_absolute_posix(path: object, label: str) -> str:
    """An absolute TV-side path: ``/`` separated, no ``..`` or empty parts."""
    if type(path) is not str or not path.startswith("/") or path == "/":
        raise PathRuleError(f"{label} must be an absolute POSIX path below /")
    check_relative(path[1:], label)
    return path

"""Strict JSON loading shared by every toolkit input.

Python's ``json`` module silently keeps the last of two duplicate keys and
accepts ``NaN``/``Infinity``. Both are rejected here, matching the
"duplicate JSON properties" rule of the XattrBackup manifest reader.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping


class StrictJsonError(ValueError):
    """Raised when a document is not strict JSON or has the wrong shape."""


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StrictJsonError(f"duplicate JSON property: {key!r}")
        result[key] = value
    return result


def _reject_constant(name: str) -> Any:
    raise StrictJsonError(f"non-standard JSON constant: {name}")


def loads(text: str | bytes) -> Any:
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise StrictJsonError("document is not UTF-8") from exc
    if text.startswith("﻿"):
        raise StrictJsonError("document starts with a byte-order mark")
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise StrictJsonError(f"invalid JSON: {exc.msg} at line {exc.lineno}") from exc


def load_file(path: str | Path, max_bytes: int = 32 * 1024 * 1024) -> Any:
    target = Path(path)
    try:
        size = target.stat().st_size
    except OSError as exc:
        raise StrictJsonError(f"cannot read {target.name}: {exc.strerror}") from exc
    if size > max_bytes:
        raise StrictJsonError(f"{target.name} is larger than {max_bytes} bytes")
    return loads(target.read_bytes())


def dumps(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, sort_keys=False) + "\n"


def require_object(
    value: Any,
    label: str,
    required: Iterable[str],
    optional: Iterable[str] = (),
) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise StrictJsonError(f"{label} must be a JSON object")
    required_set = set(required)
    allowed = required_set | set(optional)
    missing = sorted(required_set - set(value))
    unknown = sorted(set(value) - allowed)
    if missing:
        raise StrictJsonError(f"{label} is missing {', '.join(missing)}")
    if unknown:
        raise StrictJsonError(f"{label} has unknown properties {', '.join(unknown)}")
    return value


def require_str(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if type(value) is not str:
        raise StrictJsonError(f"{label} must be a string")
    if not allow_empty and value == "":
        raise StrictJsonError(f"{label} must not be empty")
    return value


def require_int(value: Any, label: str, *, minimum: int = 0, maximum: int | None = None) -> int:
    # bool is a subclass of int; JSON true must not pass as 1.
    if type(value) is not int:
        raise StrictJsonError(f"{label} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        raise StrictJsonError(f"{label} is out of range")
    return value


def require_list(value: Any, label: str, *, max_items: int) -> list[Any]:
    if not isinstance(value, list):
        raise StrictJsonError(f"{label} must be a JSON list")
    if len(value) > max_items:
        raise StrictJsonError(f"{label} has more than {max_items} items")
    return value

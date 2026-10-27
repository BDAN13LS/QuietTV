#!/usr/bin/env python3
"""Build offline SQL plans for optional app flags.

The planner is intentionally offline. It reads a JSON list captured by the
caller from the television and writes three files:

* ``apply.sql`` applies the optional-app flag changes.
* ``restore.sql`` reverses only those flag changes.
* ``before.json`` records the captured state used by both plans.

The generated SQL is meant to be run with ``sqlite3 -bail``. Each script
starts an immediate transaction, checks every target before changing anything,
and commits only after all checks pass. If a check fails, ``sqlite3`` exits and
the caller's connection close rolls the transaction back.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, NamedTuple, Sequence

TABLE = "package_app_info"
FIELDS = ("app_disable", "app_onboot", "app_autorestart")
RECORD_KEYS = ("app_id",) + FIELDS
ALLOWED_VALUES = frozenset(("true", "false", "1", "0"))
TARGET_TEXT = {
    "app_disable": "true",
    "app_onboot": "false",
    "app_autorestart": "false",
}
TARGET_BOOL = {
    "app_disable": True,
    "app_onboot": False,
    "app_autorestart": False,
}
OUTPUT_NAMES = ("apply.sql", "restore.sql", "before.json")
APPLY_GUARD = "__optional_apps_apply_guard"
RESTORE_GUARD = "__optional_apps_restore_guard"


class PlanError(ValueError):
    """Raised when the input or output request is not safe to plan."""


class Plan(NamedTuple):
    apply_sql: str
    restore_sql: str
    before_json: str


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _uses_numeric_representation(value: str) -> bool:
    return value in ("0", "1")


def _before_literal(value: str) -> str:
    if _uses_numeric_representation(value):
        return value
    return _quote(value)


def _applied_literal(field: str, before_value: str) -> str:
    if _uses_numeric_representation(before_value):
        return "1" if TARGET_BOOL[field] else "0"
    return _quote(TARGET_TEXT[field])


def validate_records(data: Any) -> list[dict[str, str]]:
    if not isinstance(data, list):
        raise PlanError("input must be a JSON list")

    records: list[dict[str, str]] = []
    seen_ids: set[str] = set()

    for position, item in enumerate(data):
        label = f"input[{position}]"
        if not isinstance(item, dict):
            raise PlanError(f"{label} must be a JSON object")

        item_keys = set(item)
        required_keys = set(RECORD_KEYS)
        if item_keys != required_keys:
            missing = sorted(required_keys - item_keys)
            unknown = sorted(item_keys - required_keys)
            details = []
            if missing:
                details.append("missing " + ", ".join(missing))
            if unknown:
                details.append("unknown " + ", ".join(unknown))
            raise PlanError(
                f"{label} must contain exactly {', '.join(RECORD_KEYS)}"
                f" ({'; '.join(details)})"
            )

        app_id = item["app_id"]
        if not isinstance(app_id, str):
            raise PlanError(f"{label}.app_id must be a string")
        if app_id == "":
            raise PlanError(f"{label}.app_id must not be empty")
        if "\x00" in app_id:
            raise PlanError(f"{label}.app_id must not contain a NUL character")
        if app_id in seen_ids:
            raise PlanError(f"{label}.app_id is a duplicate: {app_id!r}")
        seen_ids.add(app_id)

        record = {"app_id": app_id}
        for field in FIELDS:
            value = item[field]
            if not isinstance(value, str):
                raise PlanError(
                    f"{label}.{field} must be one of the exact strings"
                    " 'true', 'false', '1', or '0'"
                )
            if value not in ALLOWED_VALUES:
                raise PlanError(
                    f"{label}.{field} must be one of the exact strings"
                    " 'true', 'false', '1', or '0'"
                )
            record[field] = value
        records.append(record)

    return records


def _guard_sql(table_name: str) -> str:
    return (
        f"CREATE TEMP TABLE IF NOT EXISTS {table_name} (\n"
        "  ok INTEGER NOT NULL CHECK (ok = 1)\n"
        ");"
    )


def _assertion_sql(
    guard_table: str,
    record: Mapping[str, str],
    expected_literals: Mapping[str, str],
) -> str:
    app_id = _quote(record["app_id"])
    matches = "\n".join(
        f"        AND {field} IS {expected_literals[field]}" for field in FIELDS
    )
    return (
        f"INSERT INTO {guard_table} (ok)\n"
        "VALUES (\n"
        "  CASE WHEN\n"
        f"    (SELECT COUNT(*) FROM {TABLE} WHERE app_id = {app_id}) = 1\n"
        "    AND EXISTS (\n"
        f"      SELECT 1 FROM {TABLE}\n"
        f"      WHERE app_id = {app_id}\n"
        f"{matches}\n"
        "    )\n"
        "  THEN 1 ELSE 0 END\n"
        ");"
    )


def _update_sql(
    record: Mapping[str, str],
    new_literals: Mapping[str, str],
) -> str:
    assignments = ", ".join(
        f"{field} = {new_literals[field]}" for field in FIELDS
    )
    return (
        f"UPDATE {TABLE}\n"
        f"SET {assignments}\n"
        f"WHERE app_id = {_quote(record['app_id'])};"
    )


def generate_plan(data: Any) -> Plan:
    records = validate_records(data)

    apply_lines = ["BEGIN IMMEDIATE;", _guard_sql(APPLY_GUARD)]
    restore_lines = ["BEGIN IMMEDIATE;", _guard_sql(RESTORE_GUARD)]

    for record in records:
        before_literals = {
            field: _before_literal(record[field]) for field in FIELDS
        }
        applied_literals = {
            field: _applied_literal(field, record[field]) for field in FIELDS
        }
        apply_lines.append(
            _assertion_sql(APPLY_GUARD, record, before_literals)
        )
        restore_lines.append(
            _assertion_sql(RESTORE_GUARD, record, applied_literals)
        )

    for record in records:
        before_literals = {
            field: _before_literal(record[field]) for field in FIELDS
        }
        applied_literals = {
            field: _applied_literal(field, record[field]) for field in FIELDS
        }
        apply_lines.append(_update_sql(record, applied_literals))
        restore_lines.append(_update_sql(record, before_literals))

    apply_lines.append("COMMIT;")
    restore_lines.append("COMMIT;")

    before_json = json.dumps(records, indent=2, ensure_ascii=False) + "\n"
    return Plan(
        apply_sql="\n".join(apply_lines) + "\n",
        restore_sql="\n".join(restore_lines) + "\n",
        before_json=before_json,
    )


def load_input(path: str | Path) -> Any:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise PlanError(f"cannot read input file: {exc}") from exc

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise PlanError(f"input is not valid JSON: {exc}") from exc


def _close_and_remove(
    handles: Sequence[tuple[Any, str]],
    created: Sequence[Path],
) -> None:
    for handle, _ in handles:
        try:
            handle.close()
        except OSError:
            pass
    for target in created:
        try:
            target.unlink()
        except OSError:
            pass


def write_outputs(plan: Plan, outdir: str | Path) -> tuple[Path, ...]:
    directory = Path(outdir)
    if directory.exists() and not directory.is_dir():
        raise PlanError(f"output path is not a directory: {directory}")

    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PlanError(f"cannot create output directory: {exc}") from exc

    payloads = {
        "apply.sql": plan.apply_sql,
        "restore.sql": plan.restore_sql,
        "before.json": plan.before_json,
    }
    handles: list[tuple[Any, str]] = []
    created: list[Path] = []

    try:
        for name in OUTPUT_NAMES:
            target = directory / name
            handle = open(target, "x", encoding="utf-8", newline="\n")
            handles.append((handle, name))
            created.append(target)

        for handle, name in handles:
            handle.write(payloads[name])
            handle.close()
    except FileExistsError as exc:
        _close_and_remove(handles, created)
        raise PlanError(
            f"refusing to overwrite existing output: {exc.filename}"
        ) from exc
    except OSError as exc:
        _close_and_remove(handles, created)
        raise PlanError(f"cannot write output files: {exc}") from exc

    return tuple(created)


def write_plan(input_path: str | Path, outdir: str | Path) -> tuple[Path, ...]:
    plan = generate_plan(load_input(input_path))
    return write_outputs(plan, outdir)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build offline apply/restore SQL plans for optional app flags."
        )
    )
    parser.add_argument("input_json", help="JSON list of captured app records")
    parser.add_argument("outdir", help="directory for apply.sql, restore.sql, before.json")
    args = parser.parse_args(argv)

    try:
        outputs = write_plan(args.input_json, args.outdir)
    except PlanError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    for output in outputs:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

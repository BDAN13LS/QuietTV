import contextlib
import io
import json
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tools.plan_optional_apps as planner
from tools.plan_optional_apps import (
    FIELDS,
    TABLE,
    PlanError,
    generate_plan,
    main,
    validate_records,
    write_plan,
)

KEYED_SCHEMA = (
    f"{TABLE} ("
    "app_id TEXT PRIMARY KEY, "
    "app_disable, "
    "app_onboot, "
    "app_autorestart, "
    "note TEXT)"
)
UNKEYED_SCHEMA = (
    f"{TABLE} ("
    "app_id TEXT, "
    "app_disable, "
    "app_onboot, "
    "app_autorestart, "
    "note TEXT)"
)


def make_conn(rows, schema=KEYED_SCHEMA):
    conn = sqlite3.connect(":memory:")
    conn.execute(f"CREATE TABLE {schema}")
    conn.executemany(
        f"INSERT INTO {TABLE}"
        " (app_id, app_disable, app_onboot, app_autorestart, note)"
        " VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return conn


def fetch_one(conn, app_id):
    return conn.execute(
        f"SELECT app_id, app_disable, app_onboot, app_autorestart, note"
        f" FROM {TABLE} WHERE app_id = ?",
        (app_id,),
    ).fetchone()


def fetch_all(conn, app_id):
    return conn.execute(
        f"SELECT app_id, app_disable, app_onboot, app_autorestart, note"
        f" FROM {TABLE} WHERE app_id = ? ORDER BY rowid",
        (app_id,),
    ).fetchall()


def execute_failing_plan(testcase, conn, sql):
    with testcase.assertRaises(sqlite3.IntegrityError):
        conn.executescript(sql)
    conn.rollback()


class OptionalAppPlanTests(unittest.TestCase):
    def test_apply_then_restore_preserves_unrelated_data(self):
        rows = [
            ("num", 1, 0, 1, "num-note"),
            ("txt", "true", "false", "true", "txt-note"),
            ("other", 1, 1, 1, "other-note"),
        ]
        data = [
            {
                "app_id": "num",
                "app_disable": "1",
                "app_onboot": "0",
                "app_autorestart": "1",
            },
            {
                "app_id": "txt",
                "app_disable": "true",
                "app_onboot": "false",
                "app_autorestart": "true",
            },
        ]

        plan = generate_plan(data)
        conn = make_conn(rows)

        conn.executescript(plan.apply_sql)

        num = fetch_one(conn, "num")
        self.assertEqual(num[1:4], (1, 0, 0))
        self.assertIsInstance(num[1], int)
        self.assertIsInstance(num[2], int)
        self.assertIsInstance(num[3], int)
        self.assertEqual(num[4], "num-note")

        txt = fetch_one(conn, "txt")
        self.assertEqual(txt[1:4], ("true", "false", "false"))
        self.assertIsInstance(txt[1], str)
        self.assertIsInstance(txt[2], str)
        self.assertIsInstance(txt[3], str)
        self.assertEqual(txt[4], "txt-note")

        conn.execute(
            f"UPDATE {TABLE} SET note = ? WHERE app_id = ?",
            ("num-note-after-concurrent-edit", "num"),
        )
        conn.execute(
            f"UPDATE {TABLE} SET note = ? WHERE app_id = ?",
            ("other-note-after-concurrent-edit", "other"),
        )
        conn.execute(
            f"INSERT INTO {TABLE}"
            " (app_id, app_disable, app_onboot, app_autorestart, note)"
            " VALUES (?, ?, ?, ?, ?)",
            ("added", 1, 1, 1, "added-note"),
        )
        conn.commit()

        conn.executescript(plan.restore_sql)

        num = fetch_one(conn, "num")
        self.assertEqual(num[1:4], (1, 0, 1))
        self.assertEqual(num[4], "num-note-after-concurrent-edit")

        txt = fetch_one(conn, "txt")
        self.assertEqual(txt[1:4], ("true", "false", "true"))
        self.assertEqual(txt[4], "txt-note")

        other = fetch_one(conn, "other")
        self.assertEqual(other[1:4], (1, 1, 1))
        self.assertEqual(other[4], "other-note-after-concurrent-edit")

        added = fetch_one(conn, "added")
        self.assertEqual(added[1:4], (1, 1, 1))
        self.assertEqual(added[4], "added-note")

    def test_all_assertions_run_before_any_update_on_missing_row(self):
        rows = [
            ("present", 1, 0, 1, "present-note"),
            ("other", 1, 1, 1, "other-note"),
        ]
        data = [
            {
                "app_id": "present",
                "app_disable": "1",
                "app_onboot": "0",
                "app_autorestart": "1",
            },
            {
                "app_id": "missing",
                "app_disable": "1",
                "app_onboot": "0",
                "app_autorestart": "1",
            },
        ]

        plan = generate_plan(data)
        conn = make_conn(rows)
        execute_failing_plan(self, conn, plan.apply_sql)

        present = fetch_one(conn, "present")
        self.assertEqual(present[1:4], (1, 0, 1))
        self.assertEqual(present[4], "present-note")

        other = fetch_one(conn, "other")
        self.assertEqual(other[1:4], (1, 1, 1))
        self.assertEqual(other[4], "other-note")

    def test_all_assertions_run_before_any_update_on_target_drift(self):
        rows = [
            ("clean", 1, 0, 1, "clean-note"),
            ("drifted", 1, 0, 1, "drifted-note"),
        ]
        data = [
            {
                "app_id": "clean",
                "app_disable": "1",
                "app_onboot": "0",
                "app_autorestart": "1",
            },
            {
                "app_id": "drifted",
                "app_disable": "1",
                "app_onboot": "0",
                "app_autorestart": "1",
            },
        ]

        conn = make_conn(rows)
        conn.execute(
            f"UPDATE {TABLE} SET app_onboot = 1 WHERE app_id = ?",
            ("drifted",),
        )
        conn.commit()

        plan = generate_plan(data)
        execute_failing_plan(self, conn, plan.apply_sql)

        clean = fetch_one(conn, "clean")
        self.assertEqual(clean[1:4], (1, 0, 1))
        self.assertEqual(clean[4], "clean-note")

        drifted = fetch_one(conn, "drifted")
        self.assertEqual(drifted[1:4], (1, 1, 1))
        self.assertEqual(drifted[4], "drifted-note")

    def test_duplicate_database_rows_are_refused(self):
        rows = [
            ("dupe", 1, 0, 1, "first-note"),
            ("dupe", 1, 0, 1, "second-note"),
        ]
        data = [
            {
                "app_id": "dupe",
                "app_disable": "1",
                "app_onboot": "0",
                "app_autorestart": "1",
            }
        ]

        conn = make_conn(rows, schema=UNKEYED_SCHEMA)
        plan = generate_plan(data)
        execute_failing_plan(self, conn, plan.apply_sql)

        rows_after = fetch_all(conn, "dupe")
        self.assertEqual(len(rows_after), 2)
        self.assertEqual(rows_after[0][1:4], (1, 0, 1))
        self.assertEqual(rows_after[0][4], "first-note")
        self.assertEqual(rows_after[1][1:4], (1, 0, 1))
        self.assertEqual(rows_after[1][4], "second-note")

    def test_restore_refuses_target_drift_and_does_not_restore_other_rows(self):
        rows = [
            ("first", 1, 0, 1, "first-note"),
            ("second", "true", "false", "true", "second-note"),
        ]
        data = [
            {
                "app_id": "first",
                "app_disable": "1",
                "app_onboot": "0",
                "app_autorestart": "1",
            },
            {
                "app_id": "second",
                "app_disable": "true",
                "app_onboot": "false",
                "app_autorestart": "true",
            },
        ]

        conn = make_conn(rows)
        plan = generate_plan(data)
        conn.executescript(plan.apply_sql)

        conn.execute(
            f"UPDATE {TABLE} SET app_disable = 0 WHERE app_id = ?",
            ("second",),
        )
        conn.commit()

        execute_failing_plan(self, conn, plan.restore_sql)

        first = fetch_one(conn, "first")
        self.assertEqual(first[1:4], (1, 0, 0))
        self.assertEqual(first[4], "first-note")

        second = fetch_one(conn, "second")
        self.assertEqual(second[1:4], (0, "false", "false"))
        self.assertEqual(second[4], "second-note")

    def test_sql_quotes_in_app_ids_are_safe(self):
        evil_id = "a'b; DROP TABLE package_app_info; --"
        rows = [
            (evil_id, 1, 0, 1, "evil-note"),
            ("safe", 1, 1, 1, "safe-note"),
        ]
        data = [
            {
                "app_id": evil_id,
                "app_disable": "1",
                "app_onboot": "0",
                "app_autorestart": "1",
            }
        ]

        plan = generate_plan(data)
        conn = make_conn(rows)

        conn.executescript(plan.apply_sql)

        evil = fetch_one(conn, evil_id)
        self.assertEqual(evil[1:4], (1, 0, 0))
        self.assertEqual(evil[4], "evil-note")

        safe = fetch_one(conn, "safe")
        self.assertEqual(safe[1:4], (1, 1, 1))
        self.assertEqual(safe[4], "safe-note")

        conn.executescript(plan.restore_sql)

        evil = fetch_one(conn, evil_id)
        self.assertEqual(evil[1:4], (1, 0, 1))
        self.assertEqual(evil[4], "evil-note")

    def test_regression_real_package_app_info_text_schema_with_quoted_id(self):
        self.assertEqual(TABLE, "package_app_info")

        quoted_id = "owner's app"
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE package_app_info("
            "app_id TEXT, "
            "app_disable TEXT, "
            "app_onboot TEXT, "
            "app_autorestart TEXT, "
            "note TEXT)"
        )
        conn.executemany(
            "INSERT INTO package_app_info"
            " (app_id, app_disable, app_onboot, app_autorestart, note)"
            " VALUES (?, ?, ?, ?, ?)",
            [
                (quoted_id, "true", "true", "true", "owner-note"),
                ("other", "true", "true", "true", "other-note"),
            ],
        )
        conn.commit()

        data = [
            {
                "app_id": quoted_id,
                "app_disable": "true",
                "app_onboot": "true",
                "app_autorestart": "true",
            }
        ]
        plan = generate_plan(data)

        conn.executescript(plan.apply_sql)

        applied = conn.execute(
            "SELECT app_id, app_disable, app_onboot, app_autorestart, note"
            " FROM package_app_info WHERE app_id = ?",
            (quoted_id,),
        ).fetchone()
        self.assertEqual(applied[1:4], ("true", "false", "false"))
        self.assertEqual(applied[4], "owner-note")
        for value in applied[1:4]:
            self.assertIsInstance(value, str)

        other = conn.execute(
            "SELECT app_id, app_disable, app_onboot, app_autorestart, note"
            " FROM package_app_info WHERE app_id = ?",
            ("other",),
        ).fetchone()
        self.assertEqual(other[1:4], ("true", "true", "true"))
        self.assertEqual(other[4], "other-note")

        conn.executescript(plan.restore_sql)

        restored = conn.execute(
            "SELECT app_id, app_disable, app_onboot, app_autorestart, note"
            " FROM package_app_info WHERE app_id = ?",
            (quoted_id,),
        ).fetchone()
        self.assertEqual(restored[1:4], ("true", "true", "true"))
        self.assertEqual(restored[4], "owner-note")
        for value in restored[1:4]:
            self.assertIsInstance(value, str)

        other = conn.execute(
            "SELECT app_id, app_disable, app_onboot, app_autorestart, note"
            " FROM package_app_info WHERE app_id = ?",
            ("other",),
        ).fetchone()
        self.assertEqual(other[1:4], ("true", "true", "true"))
        self.assertEqual(other[4], "other-note")

    def test_invalid_inputs_are_refused(self):
        valid = {
            "app_id": "a",
            "app_disable": "true",
            "app_onboot": "false",
            "app_autorestart": "1",
        }
        cases = {
            "root object": {},
            "root null": None,
            "non-object item": ["not-an-object"],
            "missing key": [
                {
                    "app_id": "a",
                    "app_disable": "true",
                    "app_onboot": "false",
                }
            ],
            "unknown key": [dict(valid, extra="x")],
            "null app id": [dict(valid, app_id=None)],
            "empty app id": [dict(valid, app_id="")],
            "nul app id": [dict(valid, app_id="a\x00b")],
            "non-string app id": [dict(valid, app_id=1)],
            "duplicate app id": [valid, dict(valid)],
            "null value": [dict(valid, app_onboot=None)],
            "integer value": [dict(valid, app_onboot=1)],
            "capitalized value": [dict(valid, app_onboot="False")],
            "padded value": [dict(valid, app_onboot=" false")],
            "word value": [dict(valid, app_disable="yes")],
            "empty value": [dict(valid, app_autorestart="")],
        }

        for label, data in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(PlanError):
                    generate_plan(data)

    def test_exact_values_are_accepted_independently_per_field(self):
        data = [
            {
                "app_id": "mixed",
                "app_disable": "true",
                "app_onboot": "1",
                "app_autorestart": "false",
            }
        ]

        records = validate_records(data)
        self.assertEqual(records, data)
        plan = generate_plan(data)
        self.assertIn("app_disable", plan.apply_sql)
        self.assertIn("app_onboot", plan.apply_sql)
        self.assertIn("app_autorestart", plan.apply_sql)

    def test_generated_sql_has_no_broad_or_destructive_operations(self):
        data = [
            {
                "app_id": "one",
                "app_disable": "1",
                "app_onboot": "0",
                "app_autorestart": "1",
            },
            {
                "app_id": "two",
                "app_disable": "true",
                "app_onboot": "false",
                "app_autorestart": "true",
            },
        ]

        plan = generate_plan(data)

        for sql in (plan.apply_sql, plan.restore_sql):
            upper = sql.upper()
            self.assertNotIn("DROP", upper)
            self.assertNotIn("DELETE", upper)
            self.assertNotIn("VACUUM", upper)
            self.assertNotIn("ATTACH", upper)
            updates = re.findall(
                rf"UPDATE {re.escape(TABLE)}\nSET .*?\nWHERE app_id = .*?;",
                sql,
                re.DOTALL,
            )
            self.assertEqual(len(updates), len(data))
            self.assertEqual(sql.count(f"UPDATE {TABLE}"), len(data))

        first_guard = plan.apply_sql.index("INSERT INTO " + planner.APPLY_GUARD)
        first_update = plan.apply_sql.index(f"UPDATE {TABLE}")
        last_guard = plan.apply_sql.rindex(
            "INSERT INTO " + planner.APPLY_GUARD
        )
        self.assertLess(last_guard, first_update)
        self.assertGreater(first_update, first_guard)

    def test_generator_is_offline_and_does_not_create_a_database(self):
        self.assertFalse(hasattr(planner, "sqlite3"))

        data = [
            {
                "app_id": "offline",
                "app_disable": "1",
                "app_onboot": "0",
                "app_autorestart": "1",
            }
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            input_path = temp / "input.json"
            outdir = temp / "out"
            input_path.write_text(json.dumps(data), encoding="utf-8")

            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                with contextlib.redirect_stderr(stderr):
                    result = main([str(input_path), str(outdir)])

            self.assertEqual(result, 0)
            self.assertEqual(stderr.getvalue(), "")
            self.assertEqual(
                sorted(path.name for path in outdir.iterdir()),
                ["apply.sql", "before.json", "restore.sql"],
            )
            before = json.loads(
                (outdir / "before.json").read_text(encoding="utf-8")
            )
            self.assertEqual(before, data)

    def test_cli_refuses_to_overwrite_existing_outputs(self):
        data = [
            {
                "app_id": "a",
                "app_disable": "1",
                "app_onboot": "0",
                "app_autorestart": "1",
            }
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            input_path = temp / "input.json"
            outdir = temp / "out"
            input_path.write_text(json.dumps(data), encoding="utf-8")

            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main([str(input_path), str(outdir)]), 0)
            original = {
                name: (outdir / name).read_text(encoding="utf-8")
                for name in ("apply.sql", "restore.sql", "before.json")
            }

            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                with contextlib.redirect_stderr(stderr):
                    result = main([str(input_path), str(outdir)])

            self.assertNotEqual(result, 0)
            self.assertIn("refusing to overwrite", stderr.getvalue())
            for name, text in original.items():
                self.assertEqual(
                    (outdir / name).read_text(encoding="utf-8"), text
                )

    def test_partial_existing_output_leaves_no_new_files(self):
        data = [
            {
                "app_id": "a",
                "app_disable": "1",
                "app_onboot": "0",
                "app_autorestart": "1",
            }
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            input_path = temp / "input.json"
            outdir = temp / "out"
            outdir.mkdir()
            input_path.write_text(json.dumps(data), encoding="utf-8")
            sentinel = outdir / "restore.sql"
            sentinel.write_text("sentinel", encoding="utf-8")

            with self.assertRaises(PlanError):
                write_plan(input_path, outdir)

            self.assertFalse((outdir / "apply.sql").exists())
            self.assertFalse((outdir / "before.json").exists())
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "sentinel")

    def test_legacy_unguarded_plan_changes_drifted_target_then_new_plan_refuses(
        self,
    ):
        rows = [
            ("target", 1, 0, 1, "target-note"),
            ("other", 1, 1, 1, "other-note"),
        ]
        data = [
            {
                "app_id": "target",
                "app_disable": "1",
                "app_onboot": "0",
                "app_autorestart": "1",
            }
        ]

        legacy_conn = make_conn(rows)
        legacy_conn.execute(
            f"UPDATE {TABLE} SET app_disable = 0 WHERE app_id = ?",
            ("target",),
        )
        legacy_conn.commit()

        legacy_sql = (
            "BEGIN IMMEDIATE;\n"
            f"UPDATE {TABLE}\n"
            "SET app_disable = 1, app_onboot = 0, app_autorestart = 0\n"
            "WHERE app_id = 'target';\n"
            "COMMIT;\n"
        )
        legacy_conn.executescript(legacy_sql)

        legacy_target = fetch_one(legacy_conn, "target")
        self.assertEqual(legacy_target[1:4], (1, 0, 0))
        self.assertEqual(legacy_target[4], "target-note")

        new_conn = make_conn(rows)
        new_conn.execute(
            f"UPDATE {TABLE} SET app_disable = 0 WHERE app_id = ?",
            ("target",),
        )
        new_conn.commit()

        plan = generate_plan(data)
        execute_failing_plan(self, new_conn, plan.apply_sql)

        new_target = fetch_one(new_conn, "target")
        self.assertEqual(new_target[1:4], (0, 0, 1))
        self.assertEqual(new_target[4], "target-note")

        other = fetch_one(new_conn, "other")
        self.assertEqual(other[1:4], (1, 1, 1))
        self.assertEqual(other[4], "other-note")


if __name__ == "__main__":
    unittest.main()

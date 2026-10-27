"""Rescue stands alone: no launcher/app/boot/agent imports, and no network.

The import graph is checked in a clean ``python -I`` subprocess so nothing
the test runner already imported can hide a dependency. The network guard
blocks every outbound socket and is shown to block (positive control)
before the dry run and the synthetic restore run under it.
"""

from __future__ import annotations

import ast
import json
import socket
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parent
RESCUE = REPO / "rescue"
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(REPO))

import test_rescue_refusals as rr  # noqa: E402
from rescue import main as rescue  # noqa: E402

FORBIDDEN_PREFIXES = ("app", "boot", "launcher", "agent", "controller", "controller_policy",
                      "root_compat", "run_root", "receive_backup", "helper", "probe", "iptv",
                      "tvkit.cli", "tvkit.fetch", "tvkit.export", "tvkit.config",
                      "tvkit.frontend_export", "tvkit.backup")
ALLOWED_TOP_LEVEL = {"tvkit", "rescue"}


class NetworkBlocked(AssertionError):
    pass


def _blocked(*args, **kwargs):
    raise NetworkBlocked("network use during Rescue")


def network_guard():
    return mock.patch.multiple(socket, create_connection=_blocked, getaddrinfo=_blocked), \
        mock.patch.object(socket.socket, "connect", _blocked), \
        mock.patch.object(socket.socket, "connect_ex", _blocked)


def forbidden(name: str) -> bool:
    return any(name == p or name.startswith(p + ".") for p in FORBIDDEN_PREFIXES)


class ImportGraph(unittest.TestCase):
    def test_clean_interpreter_loads_no_launcher_app_boot_or_agent_module(self):
        code = (
            "import sys, json; sys.path.insert(0, sys.argv[1]);"
            "import rescue.main;"
            "print(json.dumps(sorted(sys.modules)))"
        )
        out = subprocess.run([sys.executable, "-I", "-c", code, str(REPO)],
                             capture_output=True, text=True, check=True, cwd=str(RESCUE))
        modules = json.loads(out.stdout)
        self.assertIn("rescue.main", modules)
        self.assertIn("tvkit.transport", modules)
        self.assertEqual([m for m in modules if forbidden(m)], [])

    def test_static_imports_are_stdlib_or_tvkit_only(self):
        stdlib = set(sys.stdlib_module_names) | {"__future__"}
        for path in RESCUE.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [node.module or ""]
                for name in names:
                    top = name.split(".")[0]
                    with self.subTest(file=path.name, module=name):
                        self.assertTrue(top in stdlib or top in ALLOWED_TOP_LEVEL, name)
                        self.assertFalse(forbidden(name), name)
                if isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) in (
                        "import_module", "__import__", "exec", "eval", "spec_from_file_location"):
                    self.fail(f"{path.name} loads code dynamically")


class WanBlocked(rr.RescueCase):
    def run_guarded(self, func):
        guards = network_guard()
        with guards[0], guards[1], guards[2]:
            return func()

    def test_guard_positive_control(self):
        def attempt():
            socket.create_connection(("127.0.0.1", 9), timeout=0.1)
        with self.assertRaises(NetworkBlocked):
            self.run_guarded(attempt)

        def attempt_raw():
            with socket.socket() as s:
                s.connect(("127.0.0.1", 9))
        with self.assertRaises(NetworkBlocked):
            self.run_guarded(attempt_raw)

    def test_dry_run_and_validate_work_offline(self):
        path = self.capsule()
        facts = rr.fx.write_facts(self.tmp / "facts.json")
        for argv in (["validate", "--capsule", str(path), "--profile", rr.fx.PROFILE_ID,
                      "--device-facts", str(facts)],
                     ["plan", "--capsule", str(path), "--profile", rr.fx.PROFILE_ID],
                     ["restore", "--capsule", str(path), "--profile", rr.fx.PROFILE_ID],
                     ["rehearse", "--capsule", str(path), "--profile", rr.fx.PROFILE_ID]):
            with self.subTest(command=argv[0]):
                with mock.patch("sys.stdout"), mock.patch("sys.stderr"):
                    code = self.run_guarded(lambda: rescue.main(argv))
                self.assertEqual(code, rescue.EXIT_OK)

    def test_synthetic_restore_and_rehearsal_work_offline(self):
        path = self.capsule()
        result = self.run_guarded(lambda: rescue.restore(path, self.profile, self.tv()))
        self.assertEqual(result.status, "pending-owner-acceptance")
        result = self.run_guarded(lambda: rescue.rehearse(path, self.profile, self.tv()))
        self.assertEqual(result.status, "rehearsed")


if __name__ == "__main__":
    unittest.main()

"""Windows owner-only ACL preparation for helper/owner_init.py.

Proves the defect in the old icacls `/inheritance:r /grant:r` preparation: an explicit
Everyone (S-1-1-0) grant planted before tightening SURVIVED it, so the object was not
owner-only and tvkit.provider.config's strict checker refused it. The new single
DACL-replacement write removes every stray ACE, so the checker accepts.

All data here is harmless temporary bytes; no real token, key or certificate is created.
Real-ACL tests run only on Windows; test setup uses the absolute System32 icacls.exe and
grants by SID (never by the localised name "Everyone"). The checker is the oracle.

Run: python -B -m unittest tests.test_owner_privacy_windows
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling tests (test_owner_init)
sys.path.insert(0, str(REPO / "helper"))
sys.path.insert(0, str(REPO / "public-toolkit"))
import owner_init as oi  # noqa: E402
from tvkit.provider import config as pconfig  # noqa: E402

WORLD_SID = "S-1-1-0"  # the "Everyone" group, by SID so the test is locale-independent
_SYSTEM32 = Path(os.environ.get("SystemRoot") or r"C:\Windows") / "System32"


def _icacls(*args: str) -> subprocess.CompletedProcess:
    """Trusted absolute icacls.exe for test SETUP only (planting the regression ACE)."""
    return subprocess.run((str(_SYSTEM32 / "icacls.exe"), *args),
                          capture_output=True, text=True, timeout=30, check=True)


def _grant_world(path: Path, *, is_dir: bool) -> None:
    # icacls needs a leading '*' to read the argument as a SID, not an account name.
    _icacls(str(path), "/grant", "*" + WORLD_SID + (":(OI)(CI)(R)" if is_dir else ":(R)"))


def _read_acl(path: Path) -> dict:
    """Owner SID + ACEs exactly as the production checker sees them (its own script)."""
    env = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
    env["QTV_CONFIG_PATH"] = str(path)
    powershell = (Path(os.environ.get("SystemRoot") or r"C:\Windows")
                  / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")
    result = subprocess.run(
        (str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive",
         "-ExecutionPolicy", "Bypass", "-Command", pconfig._WINDOWS_ACL_SCRIPT),
        capture_output=True, text=True, timeout=30, check=True, env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return json.loads(result.stdout)


def _checker_accepts(path: Path, *, is_dir: bool) -> bool:
    """True if the strict checker accepts the object as owner-private, False if it refuses."""
    try:
        if is_dir:
            pconfig._owner_private_windows(path)  # _owner_private rejects non-regular files
        else:
            pconfig._owner_private(path)
        return True
    except pconfig.ProviderConfigError:
        return False


@unittest.skipUnless(os.name == "nt", "Windows ACL preparation")
class WindowsOwnerAclTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_file_with_surviving_world_ace_is_fixed(self):
        path = self.tmp / "config.json"
        path.write_bytes(b"{}")
        _grant_world(path, is_dir=False)
        # Negative control: a failed plant would turn this test into a silent pass.
        self.assertFalse(_checker_accepts(path, is_dir=False),
                         "a planted world-readable file must be refused before the fix")
        owner_before = _read_acl(path)["ownerSid"]
        self.assertEqual(oi.make_private_file(path), "windows-acl-owner-only")
        self.assertTrue(_checker_accepts(path, is_dir=False))
        doc = _read_acl(path)
        self.assertEqual(doc["ownerSid"], owner_before)  # owner preserved, not SetOwner'd
        self.assertEqual([a["sid"] for a in doc["aces"]], [doc["currentUserSid"]])
        self.assertNotIn(WORLD_SID, [a["sid"] for a in doc["aces"]])

    def test_directory_with_surviving_world_ace_is_fixed(self):
        path = self.tmp / "existing-dir"
        path.mkdir()
        # Sanity: the checker accepts a known-good directory before any tampering.
        oi._owner_only_acl(path, inherit=True)
        self.assertTrue(_checker_accepts(path, is_dir=True))
        _grant_world(path, is_dir=True)
        self.assertFalse(_checker_accepts(path, is_dir=True),
                         "a planted world-readable directory must be refused before the fix")
        owner_before = _read_acl(path)["ownerSid"]
        oi._owner_only_acl(path, inherit=True)  # the exact call make_private_dir makes
        self.assertTrue(_checker_accepts(path, is_dir=True))
        doc = _read_acl(path)
        self.assertEqual(doc["ownerSid"], owner_before)
        self.assertEqual([a["sid"] for a in doc["aces"]], [doc["currentUserSid"]])

    def test_make_private_dir_child_inherits_only_current_user(self):
        path = self.tmp / "owner"
        self.assertEqual(oi.make_private_dir(path), "windows-acl-owner-only")
        self.assertTrue(_checker_accepts(path, is_dir=True))
        # A child created AFTER tightening (as generate() writes its files) inherits the
        # directory's single current-user ACE and nothing else.
        child = path / "apps.json"
        child.write_bytes(b"[]")
        doc = _read_acl(child)
        self.assertTrue(_checker_accepts(child, is_dir=False))
        self.assertEqual([a["sid"] for a in doc["aces"]], [doc["currentUserSid"]])

    def test_generate_produces_owner_private_storage(self):
        from test_owner_init import inputs  # same synthetic inputs as the main suite
        out = self.tmp / "gen"
        result = oi.generate(out, **inputs())
        self.assertEqual(result["permissions"], "windows-acl-owner-only")
        self.assertTrue(_checker_accepts(out, is_dir=True))
        for name in oi.OWNER_FILES:
            self.assertTrue(_checker_accepts(out / name, is_dir=False), name)


class OwnerAclFailureTests(unittest.TestCase):
    """The helper fails closed on any command or readback error (mocked; no real ACL)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _patch(self, **kw):
        return mock.patch.object(oi.subprocess, "run", **kw)

    def test_nonzero_exit_is_refused(self):
        result = subprocess.CompletedProcess([], 5, stdout="", stderr="")
        with self._patch(return_value=result):
            with self.assertRaisesRegex(oi.OwnerInitError, "owner-only ACL"):
                oi._owner_only_acl(self.tmp / "x", inherit=False)

    def test_wrong_stdout_is_refused(self):
        result = subprocess.CompletedProcess([], 0, stdout="SURPRISE", stderr="")
        with self._patch(return_value=result):
            with self.assertRaises(oi.OwnerInitError):
                oi._owner_only_acl(self.tmp / "x", inherit=True)

    def test_subprocess_errors_are_refused(self):
        for exc in (OSError("boom"), subprocess.TimeoutExpired(cmd="ps", timeout=30)):
            with self._patch(side_effect=exc):
                with self.assertRaises(oi.OwnerInitError):
                    oi._owner_only_acl(self.tmp / "x", inherit=False)

    def test_invocation_shape_is_hidden_system32_powershell(self):
        result = subprocess.CompletedProcess([], 0, stdout=oi._ACL_OK, stderr="")
        # CREATE_NO_WINDOW exists only on Windows; patch a synthetic value (create=True)
        # so the hidden-window assertion runs on every CI platform, Linux included.
        no_window = 0x08000000
        with mock.patch.object(oi.subprocess, "CREATE_NO_WINDOW", no_window, create=True), \
                mock.patch.dict(oi.os.environ, {"PSMODULEPATH": r"C:\outer\7\Modules"}, clear=False):
            with self._patch(return_value=result) as run:
                oi._owner_only_acl(self.tmp / "unit", inherit=True)
            # The parent environment must be left untouched while the child drops it.
            self.assertEqual(oi.os.environ.get("PSMODULEPATH"), r"C:\outer\7\Modules")
            self.assertTrue(run.call_args.kwargs["creationflags"] & no_window)
        argv = run.call_args.args[0]
        self.assertTrue(str(argv[0]).lower().endswith("powershell.exe"))
        self.assertIn("System32", str(argv[0]))
        for flag in ("-NoProfile", "-NonInteractive", "-ExecutionPolicy"):
            self.assertIn(flag, argv)
        env = run.call_args.kwargs["env"]
        self.assertFalse(any(k.upper() == "PSMODULEPATH" for k in env))
        self.assertEqual(env["QTV_PRIVATE_KIND"], "dir")
        self.assertEqual(env["QTV_PRIVATE_PATH"], str(self.tmp / "unit"))

    @unittest.skipUnless(os.name == "nt", "make_private_dir ACL step is Windows-only")
    def test_make_private_dir_leaves_no_directory_on_failure(self):
        result = subprocess.CompletedProcess([], 5, stdout="", stderr="")
        out = self.tmp / "doomed"
        with self._patch(return_value=result):
            with self.assertRaises(oi.OwnerInitError):
                oi.make_private_dir(out)
        self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()

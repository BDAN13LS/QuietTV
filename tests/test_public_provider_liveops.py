"""Focused tests for the reviewed public provider and its source-build helper.

Everything here is an offline subprocess fake. No TV, SDK device command,
network target, signing credential or root operation is used.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "helper"))
sys.path.insert(0, str(REPO / "public-toolkit"))
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO / "tools" / "tv-collect"))
sys.path.insert(0, str(REPO / "tools" / "tv-device-facts"))

import collect  # noqa: E402
import owner_init as oi  # noqa: E402
import provider_build as build_helper  # noqa: E402
from tvkit import livecollect as lc  # noqa: E402
from tvkit import liveops  # noqa: E402
from tvkit import provider  # noqa: E402
from tvkit.provider import channel as pchannel  # noqa: E402
from tvkit.provider import commands  # noqa: E402
from tvkit.provider import config as pconfig  # noqa: E402
from tvkit.provider import proc  # noqa: E402
from tvkit.provider import session as psession  # noqa: E402
from tvkit.provider import worker as pworker  # noqa: E402
from test_helper_live_collect import (  # noqa: E402
    DEPS, DLL, PACKAGE, RUNTIMECONFIG, SECRET_MARKER, LiveCase, make_wgt,
    report_bytes, sha,
)
from test_public_toolkit_factsjoin import _encode, native_doc  # noqa: E402

RUN_ID = "0123456789abcdef0123456789abcdef"
DUID = "SYNTH-DUID-01"
TARGET = ".".join(map(str, (10, 20, 30, 50)))  # synthetic private fixture
CALLBACK = ".".join(map(str, (10, 20, 30, 10)))
NATIVE_NAMES = ("QuietTVInspect.dll", "QuietTVInspect.runtimeconfig.json",
                "QuietTVInspect.deps.json")


def _config(**over):
    values = dict(
        target=TARGET, callback_host=CALLBACK, expected_sdk_duid=DUID,
        sdk_executable=Path("/nonexistent/sdb"),
        sdk_sha256="0" * 64,
        archive_root_dll=Path("/nonexistent/SamsungTvArchiveRoot.dll"),
        archive_root_sha256="0" * 64,
        root_agent_dll=Path("/nonexistent/SamsungTvRootAgent.dll"),
        root_agent_sha256="0" * 64,
        openssl_executable=Path("/nonexistent/openssl"),
        openssl_sha256="0" * 64,
        sdk_seconds=10.0, probe_seconds=60.0, command_seconds=10.0,
        native_seconds=30.0, report_attempts=1)
    values.update(over)
    return pconfig.ProviderConfig(**values)


class FakeChannel:
    """A stateful fake of the SDK/root channel, at the real channel interface."""

    def __init__(self, config, *, run_id=RUN_ID, report=None, frame=None,
                 auth=True, identity=0, dirty_system=False, staging_dirty=False,
                 probe_fail=False, duid_error=False, duid_value=None,
                 install_rc=0, install_reported=False, install_raise=False,
                 install_no_package=False, install_package_present=False,
                 uninstall_rc=0, uninstall_reported=False, uninstall_raise=False,
                 push_error=False, push_error_at=None, preexisting_package=False,
                 preexisting_data=None,
                 preexisting_run_dir=False, create_mode="770", create_group=None,
                 group=100, report_absent=False, report_symlink=False,
                 report_oversize=False, native_exit=0, native_link=False,
                 native_nonregular=False):
        self.config = config
        self.run_dir = liveops.remote_run_dir(run_id) if run_id else None
        self.report = report if report is not None else report_bytes()
        self.frame = frame if frame is not None else _encode(native_doc())
        self.auth = auth
        self.identity = identity
        self.dirty_system = dirty_system
        self.staging_dirty = staging_dirty
        self.probe_fail = probe_fail
        self.duid_error = duid_error
        self.duid_value = duid_value if duid_value is not None else config.expected_sdk_duid
        self.install_rc = install_rc
        self.install_reported = install_reported
        self.install_raise = install_raise
        self.install_no_package = install_no_package
        self.install_package_present = install_package_present
        self.uninstall_rc = uninstall_rc
        self.uninstall_reported = uninstall_reported
        self.uninstall_raise = uninstall_raise
        self.push_error = push_error
        self.push_error_at = push_error_at
        self.push_count = 0
        self.create_mode = create_mode
        self.create_uid = 1000
        self.create_group = group if create_group is None else create_group
        self.group = group
        self.report_absent = report_absent
        self.report_symlink = report_symlink
        self.report_oversize = report_oversize
        self.native_exit = native_exit
        self.native_link = native_link
        self.native_nonregular = native_nonregular
        self.installed = preexisting_package
        self.dirs = set()
        self.files = {}
        self.data = dict(preexisting_data or {})
        self.links = set()
        if preexisting_run_dir:
            self.dirs.add(self.run_dir)
        self.guards = 0
        self.probes = []
        self.probe_timeouts = []
        self.pushes = []
        self.installs = []
        self.install_paths = []
        self.uninstalls = []
        self.closed = 0
        self.sdk_calls = []

    def getduid(self):
        self.guards += 1
        self.sdk_calls.append(("shell", "0 getduid"))
        if self.duid_error:
            raise RuntimeError(SECRET_MARKER)
        return self.duid_value

    def group_id(self):
        self.sdk_calls.append(("capture", "id -g"))
        return self.group

    def push_bytes(self, data, remote_path):
        self.push_count += 1
        if self.push_error or (self.push_error_at is not None
                               and self.push_count >= self.push_error_at):
            raise RuntimeError(SECRET_MARKER)
        self.pushes.append((remote_path, data))
        self.files[remote_path] = data

    def install(self, package_id, remote_wgt):
        self.installs.append(package_id)
        self.install_paths.append(remote_wgt)
        if self.install_raise:
            raise RuntimeError(SECRET_MARKER)
        if self.install_package_present:
            self.installed = True
        elif (self.install_rc == 0 and not self.install_reported
              and not self.install_no_package):
            self.installed = True
        return pchannel.SdkOutcome(self.install_rc, self.install_reported)

    def uninstall(self, package_id):
        self.uninstalls.append(package_id)
        if self.uninstall_raise:
            raise RuntimeError(SECRET_MARKER)
        if self.uninstall_rc == 0 and not self.uninstall_reported:
            self.installed = False
        return pchannel.SdkOutcome(self.uninstall_rc, self.uninstall_reported)

    def close(self):
        self.closed += 1

    # ---- fake remote shell ----

    def _report_for(self):
        if self.report_absent:
            return None
        if self.report_oversize:
            return b"x" * (liveops.MAX_REPORT_BYTES + 1)
        return self.report

    def _present(self, path):
        return path in self.files or path in self.data or path in self.dirs

    def _exec(self, command):
        if command.startswith("/usr/bin/sqlite3"):
            if "package_app_info" in command:
                return "1:1" if self.installed else "0:0"
            return "1" if self.installed else "0"
        if "sha256sum -c -" in command:
            for digest, path in re.findall(
                    r"echo '([0-9a-f]{64})  ([^']+)' \| sha256sum -c -", command):
                blob = self.files.get(path)
                if blob is None or sha(blob) != digest:
                    return "!bundle-mismatch!"
            if self.native_link:
                return "!output-link!"
            if self.native_nonregular:
                return "!output-nonregular!"
            match = re.search(
                r"/usr/bin/dotnet (\S+)/QuietTVInspect\.dll --inventory-framed",
                command)
            self.run_dir = match.group(1)
            self.files[f"{self.run_dir}/native.out"] = self.frame
            return f"EXIT:{self.native_exit}"
        if "--inventory-framed" in command:
            match = re.search(
                r"/usr/bin/dotnet (\S+)/QuietTVInspect\.dll --inventory-framed",
                command)
            self.run_dir = match.group(1)
            self.files[f"{self.run_dir}/native.out"] = self.frame
            return f"EXIT:{self.native_exit}"
        if "wc -c < " in command:
            path = re.search(r"wc -c < ([^ ;]+)", command).group(1)
            if path in self.links or self.native_link:
                return "!symlink!"
            if self.native_nonregular:
                return "!nonregular!"
            if path not in self.files:
                return "!missing!"
            return str(len(self.files[path]))
        match = re.search(r"dd if=(\S+) bs=(\d+) skip=(\d+)", command)
        if match:
            path, size, index = match.group(1), int(match.group(2)), int(match.group(3))
            if path in self.links or self.native_link:
                return "!symlink!"
            if self.native_nonregular:
                return "!nonregular!"
            blob = self.files.get(path, b"")
            return base64.b64encode(blob[index * size:(index + 1) * size]).decode()
        match = re.match(r"if mkdir (\S+); then", command)
        if match:
            path = match.group(1)
            if "/quiettv-collect-" in path:
                self.run_dir = path
            if self._present(path):
                return "EXISTS"
            self.dirs.add(path)
            return (f"1000:{self.create_uid}:{self.create_group}:"
                    f"{self.create_mode}")
        match = re.match(r"if \[ ! -e (\S+) \]", command)
        if match:
            path = match.group(1)
            if path not in self.dirs:
                return "ABSENT"
            children = [item for item in (*self.files, *self.data, *self.dirs)
                        if item.startswith(path + "/")]
            if children:
                return "NOTEMPTY"
            self.dirs.remove(path)
            return "REMOVED"
        match = re.match(r"if \[ -L (\S+) \]; then", command)
        if match:
            path = match.group(1)
            if "base64" in command and "head -c" in command:
                if path in self.links:
                    return "!symlink!"
                blob = self.data.get(path)
                if blob is None:
                    return "!absent!"
                return base64.b64encode(
                    blob[:liveops.MAX_REPORT_READ_BYTES]).decode()
            if "sha256sum" in command:
                digest = re.search(r"grep -q '\^([0-9a-f]{64}) ", command).group(1)
                if path in self.links:
                    return "LINK"
                if path not in self.files:
                    return "ABSENT"
                if sha(self.files[path]) != digest:
                    return "MISMATCH"
                del self.files[path]
                return "REMOVED"
            if "rm -f" in command:
                if path in self.links:
                    return "LINK"
                if path in self.files:
                    del self.files[path]
                    return "REMOVED"
                if path in self.data:
                    del self.data[path]
                    return "REMOVED"
                if path in self.dirs:
                    return "MISMATCH"
                return "ABSENT"
            if path in self.links:
                return "LINK"
            return "PRESENT" if self._present(path) else "ABSENT"
        match = re.fullmatch(r"/usr/bin/app_launcher -s (\S+)", command)
        if match:
            if not self.installed:
                return "launch-refused"
            data_paths = liveops.data_paths(PACKAGE)
            if self.report_symlink:
                self.links.add(data_paths[0])
            else:
                blob = self._report_for()
                if blob is not None:
                    self.data[data_paths[0]] = blob
            self.sdk_calls.append(("shell", command))
            return ""
        raise AssertionError(f"unhandled fake command: {command}")

    def probe(self, remote_commands, *, command_seconds=None):
        self.probes.append(tuple(remote_commands))
        self.probe_timeouts.append(command_seconds)
        if self.probe_fail:
            raise RuntimeError(SECRET_MARKER)
        if not self.auth or self.identity != 0 or self.dirty_system or self.staging_dirty:
            raise RuntimeError(SECRET_MARKER)
        outputs = tuple(pchannel.CommandEvidence(0, False, self._exec(command))
                        for command in remote_commands)
        return pchannel.ProbeEvidence(True, 0, 0, 0, 0, "6.0", True, True, outputs)


def _ops(channel):
    return provider.ProviderOps(_config(), channel_factory=lambda config: channel)


def _uploads():
    return (
        liveops.Upload(f"{liveops.remote_run_dir(RUN_ID)}/QuietTVInspect.dll",
                       sha(DLL), DLL),
        liveops.Upload(f"{liveops.remote_run_dir(RUN_ID)}/QuietTVInspect.runtimeconfig.json",
                       sha(RUNTIMECONFIG), RUNTIMECONFIG),
        liveops.Upload(f"{liveops.remote_run_dir(RUN_ID)}/QuietTVInspect.deps.json",
                       sha(DEPS), DEPS),
    )


def _owned(installed=True):
    wgt = make_wgt()
    return liveops.OwnedRun(
        run_id=RUN_ID, package_id=PACKAGE, absence_confirmed=True,
        install_attempted=installed,
        uploads=(liveops.Upload(
            liveops.remote_wgt_path(liveops.remote_run_dir(RUN_ID), PACKAGE),
            sha(wgt), wgt), *_uploads()))


# ---------------------------------------------------------------- exact commands

class CommandBoundaryTests(unittest.TestCase):
    def test_every_fixed_command_is_exact(self):
        run_dir = liveops.remote_run_dir(RUN_ID)
        wgt = liveops.remote_wgt_path(run_dir, PACKAGE)
        self.assertEqual(commands.sdb_serial(TARGET), TARGET + ":26101")
        self.assertEqual(
            commands.package_count_command(PACKAGE),
            "/usr/bin/sqlite3 -init /dev/null -readonly -batch -bail -noheader -list "
            "/opt/dbspace/.pkgmgr_parser.db <<'QTVSQL'\n"
            f"SELECT count(*) FROM package_info WHERE package='{PACKAGE}';\nQTVSQL\n")
        self.assertIn("package_app_info", commands.package_and_app_count_command(PACKAGE))
        self.assertIn("WHERE app_id='A1B2C3D4E5.Facts'",
                      commands.package_and_app_count_command(PACKAGE))
        self.assertEqual(
            commands.create_run_dir_command(run_dir, 100),
            f"if mkdir {run_dir}; then set -- $(stat -c %u "
            f"/home/owner/share/tmp/sdk_tools); if [ $# -eq 1 ] && "
            f"chown $1:100 {run_dir} && chmod 770 {run_dir}; then "
            f"echo $1:$(stat -c %u:%g:%a {run_dir}); else echo INVALID; fi; "
            "else echo EXISTS; fi")
        self.assertEqual(commands.launch_command(PACKAGE),
                         "/usr/bin/app_launcher -s A1B2C3D4E5.Facts")
        self.assertEqual(commands.install_shell_argument(PACKAGE, wgt),
                         f"0 vd_appinstall {PACKAGE} {wgt}")
        self.assertEqual(commands.uninstall_shell_argument(PACKAGE),
                         f"0 vd_appuninstall {PACKAGE}")
        self.assertEqual(
            commands.native_run_command(run_dir),
            f"if [ -L {run_dir}/native.out ]; then echo '!output-link!'; "
            f"elif [ -e {run_dir}/native.out ] && [ ! -f {run_dir}/native.out ]; "
            f"then echo '!output-nonregular!'; else "
            f"/usr/bin/dotnet {run_dir}/QuietTVInspect.dll --inventory-framed "
            f"> {run_dir}/native.out 2>/dev/null; echo EXIT:$?; fi")
        self.assertEqual(
            commands.native_guard_run_command(run_dir, "a" * 64, "b" * 64, "c" * 64),
            f"if echo '{'a' * 64}  {run_dir}/QuietTVInspect.dll' | sha256sum -c - "
            ">/dev/null 2>&1 && "
            f"echo '{'b' * 64}  {run_dir}/QuietTVInspect.runtimeconfig.json' | "
            "sha256sum -c - >/dev/null 2>&1 && "
            f"echo '{'c' * 64}  {run_dir}/QuietTVInspect.deps.json' | "
            "sha256sum -c - >/dev/null 2>&1; then "
            f"if [ -L {run_dir}/native.out ]; then echo '!output-link!'; "
            f"elif [ -e {run_dir}/native.out ] && [ ! -f {run_dir}/native.out ]; "
            "then echo '!output-nonregular!'; else "
            f"/usr/bin/dotnet {run_dir}/QuietTVInspect.dll --inventory-framed "
            f"> {run_dir}/native.out 2>/dev/null; echo EXIT:$?; fi; "
            "else echo '!bundle-mismatch!'; fi")
        self.assertEqual(
            commands.native_chunk_command(run_dir, 2),
            f"if [ -L {run_dir}/native.out ]; then echo '!symlink!'; "
            f"elif [ ! -f {run_dir}/native.out ]; then echo '!nonregular!'; "
            f"else dd if={run_dir}/native.out bs=699051 skip=2 count=1 "
            f"2>/dev/null | base64; fi")
        report = commands.report_read_command(wgt)
        self.assertIn(f"head -c {liveops.MAX_REPORT_READ_BYTES}", report)
        self.assertIn("!nonregular!", report)
        self.assertNotIn(f"base64 {wgt}", report)
        self.assertIn("!symlink!", commands.native_size_command(run_dir))
        self.assertIn("!nonregular!", commands.native_chunk_command(run_dir, 0))
        self.assertIn("rm -f", commands.remove_file_command(wgt, "a" * 64))
        self.assertIn("rmdir", commands.rmdir_command(run_dir))
        for command in (commands.package_count_command(PACKAGE),
                        commands.report_read_command(wgt),
                        commands.native_run_command(run_dir)):
            self.assertNotIn("\\", command)
            self.assertNotIn('"', command)

    def test_malformed_inputs_are_refused(self):
        with self.assertRaises(commands.CommandError):
            commands.package_count_command("SHORT")
        with self.assertRaises(commands.CommandError):
            commands.install_shell_argument(PACKAGE, "relative.wgt")
        with self.assertRaises(commands.CommandError):
            commands.install_shell_argument("SHORT", "/a.wgt")
        with self.assertRaises(commands.CommandError):
            commands.uninstall_shell_argument("SHORT")
        with self.assertRaises(commands.CommandError):
            commands.create_run_dir_command(liveops.remote_run_dir(RUN_ID), -1)
        with self.assertRaises(commands.CommandError):
            commands.native_chunk_command(liveops.remote_run_dir(RUN_ID), 7)
        with self.assertRaises(commands.CommandError):
            commands.native_guard_run_command(
                liveops.remote_run_dir(RUN_ID), "a" * 63, "b" * 64, "c" * 64)
        with self.assertRaises(commands.CommandError):
            commands.path_presence_command("/tmp/x\nrm")


# ---------------------------------------------------------------- full collection

class ProviderCollectionTests(LiveCase):
    def setUp(self):
        super().setUp()
        self.config = _config()
        self.channel = FakeChannel(self.config)
        self.ops = provider.ProviderOps(
            self.config, channel_factory=lambda config: self.channel)

    def test_real_provider_reaches_collect_reports_and_cleans_its_namespace(self):
        summary = lc.collect_reports(**self.kwargs(run_id=RUN_ID))
        self.assertEqual(summary["status"], "collected")
        self.assertEqual(summary["cleanup"], "complete")
        for path in self.files().values():
            self.assertTrue(path.exists(), path.name)
        self.assertEqual(self.channel.uninstalls, [PACKAGE])
        self.assertFalse(self.channel.installed)
        self.assertEqual(self.channel.closed, 1)
        self.assertFalse([path for path in self.channel.files])
        self.assertFalse([path for path in self.channel.data])
        self.assertEqual(self.channel.dirs, set())

        run_dir = liveops.remote_run_dir(RUN_ID)
        paths = liveops.data_paths(PACKAGE)
        self.assertEqual(self.channel.probes[0], (
            commands.package_count_command(PACKAGE),
            commands.path_presence_command(paths[0]),
            commands.path_presence_command(paths[1])))
        self.assertIn((commands.create_run_dir_command(run_dir, 100),),
                      self.channel.probes)
        self.assertIn((commands.package_and_app_count_command(PACKAGE),),
                      self.channel.probes)
        self.assertIn((commands.launch_command(PACKAGE),), self.channel.probes)
        self.assertIn((commands.report_read_command(paths[0]),
                       commands.report_read_command(paths[1])), self.channel.probes)
        native_probe = (commands.native_guard_run_command(
            run_dir, sha(DLL), sha(RUNTIMECONFIG), sha(DEPS)),)
        self.assertIn(native_probe, self.channel.probes)
        native_index = self.channel.probes.index(native_probe)
        self.assertEqual(self.channel.probe_timeouts[native_index],
                         self.config.native_seconds)
        self.assertEqual(
            [path for path, _ in self.channel.pushes],
            [liveops.remote_wgt_path(run_dir, PACKAGE),
             *(f"{run_dir}/{name}" for name in NATIVE_NAMES)])
        self.assertEqual(self.channel.pushes[0][1], self.wgt_bytes)
        self.assertGreaterEqual(self.channel.guards, 9)

    def test_absence_collision_refuses_before_any_write(self):
        self.channel.installed = True
        result = _ops(self.channel).session(RUN_ID).assert_absent(
            PACKAGE, liveops.data_paths(PACKAGE))
        self.assertFalse(result.absent)
        self.assertEqual(self.channel.pushes, [])
        self.assertEqual(self.channel.installs, [])
        self.assertEqual(self.channel.dirs, set())

    def test_reported_data_collision_refuses_before_any_write(self):
        path = liveops.data_paths(PACKAGE)[0]
        self.channel.data[path] = b"previous"
        result = _ops(self.channel).session(RUN_ID).assert_absent(
            PACKAGE, liveops.data_paths(PACKAGE))
        self.assertFalse(result.data_absent)
        self.assertEqual(self.channel.pushes, [])
        self.assertEqual(self.channel.installs, [])

    def test_run_directory_collision_is_not_touched(self):
        self.channel.dirs.add(liveops.remote_run_dir(RUN_ID))
        session = _ops(self.channel).session(RUN_ID)
        self.assertTrue(session.assert_absent(PACKAGE, liveops.data_paths(PACKAGE)).absent)
        with self.assertRaises(liveops.LiveOpsError):
            session.install_widget(PACKAGE, self.wgt_bytes, sha(self.wgt_bytes),
                                   liveops.remote_wgt_path(
                                       liveops.remote_run_dir(RUN_ID), PACKAGE))
        self.assertEqual(self.channel.pushes, [])
        self.assertEqual(self.channel.installs, [])
        report = session.close_owned_run(liveops.OwnedRun(
            run_id=RUN_ID, package_id=PACKAGE, absence_confirmed=True,
            install_attempted=True, uploads=(liveops.Upload(
                liveops.remote_wgt_path(liveops.remote_run_dir(RUN_ID), PACKAGE),
                sha(self.wgt_bytes), self.wgt_bytes),)))
        self.assertFalse(report.recovery_needed)
        self.assertTrue(report.package_absent_after)
        self.assertEqual(report.removed_uploads, (
            liveops.remote_wgt_path(liveops.remote_run_dir(RUN_ID), PACKAGE),))
        self.assertIn(liveops.remote_run_dir(RUN_ID), self.channel.dirs)

    def test_run_directory_owner_uid_mismatch_refuses_before_upload(self):
        self.channel.create_uid = 1001
        session = _ops(self.channel).session(RUN_ID)
        session.assert_absent(PACKAGE, liveops.data_paths(PACKAGE))
        with self.assertRaises(liveops.LiveOpsError):
            session.install_widget(
                PACKAGE, self.wgt_bytes, sha(self.wgt_bytes),
                liveops.remote_wgt_path(liveops.remote_run_dir(RUN_ID), PACKAGE))
        self.assertEqual(self.channel.pushes, [])
        self.assertEqual(self.channel.installs, [])

    def test_run_directory_must_be_group_writable_770(self):
        self.channel.create_mode = "700"
        session = _ops(self.channel).session(RUN_ID)
        session.assert_absent(PACKAGE, liveops.data_paths(PACKAGE))
        with self.assertRaises(liveops.LiveOpsError):
            session.install_widget(
                PACKAGE, self.wgt_bytes, sha(self.wgt_bytes),
                liveops.remote_wgt_path(liveops.remote_run_dir(RUN_ID), PACKAGE))
        self.assertEqual(self.channel.pushes, [])
        self.assertEqual(self.channel.installs, [])

    def test_run_directory_reported_group_must_match_the_sdk_group(self):
        self.channel.create_group = 200
        session = _ops(self.channel).session(RUN_ID)
        session.assert_absent(PACKAGE, liveops.data_paths(PACKAGE))
        with self.assertRaises(liveops.LiveOpsError):
            session.install_widget(
                PACKAGE, self.wgt_bytes, sha(self.wgt_bytes),
                liveops.remote_wgt_path(liveops.remote_run_dir(RUN_ID), PACKAGE))
        self.assertEqual(self.channel.pushes, [])
        self.assertEqual(self.channel.installs, [])

    def test_sdk_group_capture_runs_after_the_duid_guard(self):
        session = _ops(self.channel).session(RUN_ID)
        session.assert_absent(PACKAGE, liveops.data_paths(PACKAGE))
        session.install_widget(
            PACKAGE, self.wgt_bytes, sha(self.wgt_bytes),
            liveops.remote_wgt_path(liveops.remote_run_dir(RUN_ID), PACKAGE))
        self.assertEqual(self.channel.sdk_calls[0], ("shell", "0 getduid"))
        self.assertIn(("capture", "id -g"), self.channel.sdk_calls)
        capture_index = self.channel.sdk_calls.index(("capture", "id -g"))
        self.assertTrue(any(call == ("shell", "0 getduid")
                            for call in self.channel.sdk_calls[:capture_index]))

    def test_cli_with_provider_config_runs_only_the_fixed_name(self):
        out = self.root / "cli-out"
        args = [
            "--adapter", provider.PROVIDER_NAME, "--provider-config", str(self.root / "unused.json"),
            "--out-dir", str(out), "--signed-wgt", str(self.wgt_path),
            "--wgt-sha256", sha(self.wgt_bytes),
            "--inspector-dll", str(self.native["dll"]), "--dll-sha256", sha(DLL),
            "--inspector-runtimeconfig", str(self.native["runtimeconfig"]),
            "--runtimeconfig-sha256", sha(RUNTIMECONFIG),
            "--inspector-deps", str(self.native["deps"]), "--deps-sha256", sha(DEPS),
            "--package-id", PACKAGE, "--policy", str(self.sources["policy"]),
            "--profile", str(self.sources["profile"]),
            "--operator-trust-policy-and-profile", "--operator-paired-from-same-tv",
        ]
        with mock.patch.object(collect.provider, "load_provider", return_value=self.ops):
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = collect.main(args)
        self.assertEqual(code, 0, stderr.getvalue())
        self.assertEqual(json.loads(stdout.getvalue())["status"], "collected")
        self.assertEqual(self.channel.uninstalls, [PACKAGE])

    def test_cli_refuses_an_unknown_provider_name_with_a_config(self):
        with mock.patch.object(collect.provider, "load_provider",
                               side_effect=AssertionError("must not load")):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = collect.main([
                    "--adapter", "other-provider", "--provider-config", "x",
                    "--out-dir", "x", "--signed-wgt", "x", "--wgt-sha256", "x" * 64,
                    "--inspector-dll", "x", "--dll-sha256", "x" * 64,
                    "--inspector-runtimeconfig", "x", "--runtimeconfig-sha256", "x" * 64,
                    "--inspector-deps", "x", "--deps-sha256", "x" * 64,
                    "--package-id", PACKAGE, "--policy", "x", "--profile", "x",
                    "--operator-trust-policy-and-profile", "--operator-paired-from-same-tv"])
        self.assertEqual(code, 2)
        self.assertIn("unknown live-operations provider name", stderr.getvalue())


# ---------------------------------------------------------------- guard and drift

class GuardTests(unittest.TestCase):
    def setUp(self):
        self.channel = FakeChannel(_config())
        self.ops = _ops(self.channel)
        self.paths = liveops.data_paths(PACKAGE)
        self.wgt = make_wgt()
        self.wgt_path = liveops.remote_wgt_path(liveops.remote_run_dir(RUN_ID), PACKAGE)

    def test_target_swap_before_first_write_mutates_nothing(self):
        session = self.ops.session(RUN_ID)
        self.assertTrue(session.assert_absent(PACKAGE, self.paths).absent)
        self.channel.duid_value = "OTHER-TARGET-DUID"
        with self.assertRaises(liveops.LiveOpsError):
            session.install_widget(PACKAGE, self.wgt, sha(self.wgt), self.wgt_path)
        self.assertEqual(self.channel.pushes, [])
        self.assertEqual(self.channel.installs, [])
        self.assertEqual(self.channel.dirs, set())

    def test_target_swap_before_cleanup_leaves_package_and_uploads(self):
        session = self.ops.session(RUN_ID)
        uploads = _uploads()
        self.assertTrue(session.assert_absent(PACKAGE, self.paths).absent)
        session.install_widget(PACKAGE, self.wgt, sha(self.wgt), self.wgt_path)
        session.launch_facts(PACKAGE)
        session.read_report(PACKAGE, self.paths)
        session.push_native(uploads, liveops.remote_run_dir(RUN_ID))
        session.run_native(f"{liveops.remote_run_dir(RUN_ID)}/QuietTVInspect.dll")
        self.channel.duid_value = "OTHER-TARGET-DUID"
        with self.assertRaises(liveops.LiveOpsError):
            session.close_owned_run(_owned())
        self.assertEqual(self.channel.uninstalls, [])
        self.assertTrue(self.channel.installed)
        self.assertTrue(self.channel.files)

    def test_duid_read_failure_poisons_without_leaking(self):
        self.channel.duid_error = True
        session = self.ops.session(RUN_ID)
        with self.assertRaises(liveops.LiveOpsError) as caught:
            session.assert_absent(PACKAGE, self.paths)
        text = str(caught.exception)
        self.assertNotIn(DUID, text)
        self.assertNotIn(TARGET, text)
        self.assertNotIn(SECRET_MARKER, text)
        self.assertNotIn("nonexistent", text)

    def test_missing_evidence_refuses_before_the_next_write(self):
        self.channel.auth = False
        session = self.ops.session(RUN_ID)
        with self.assertRaises(liveops.LiveOpsError):
            session.assert_absent(PACKAGE, self.paths)
        self.assertEqual(self.channel.pushes, [])
        self.assertEqual(self.channel.installs, [])


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.paths = liveops.data_paths(PACKAGE)

    def test_non_root_unauthenticated_and_dirty_probes_fail_cleanup(self):
        for option in ("identity", "auth", "dirty_system", "staging_dirty"):
            channel = FakeChannel(_config())
            channel.installed = True
            value = {"identity": 1, "auth": False}.get(option, True)
            setattr(channel, option, value)
            session = _ops(channel).session(RUN_ID)
            with self.assertRaises(liveops.LiveOpsError):
                session.close_owned_run(_owned())
            self.assertEqual(channel.uninstalls, [], option)
            self.assertTrue(channel.installed, option)

    def test_absent_namespace_is_confirmed_clean_without_uninstalling(self):
        channel = FakeChannel(_config())
        report = _ops(channel).session(RUN_ID).close_owned_run(liveops.OwnedRun(
            run_id=RUN_ID, package_id=PACKAGE, absence_confirmed=True,
            install_attempted=False, uploads=()))
        self.assertFalse(report.package_removed)
        self.assertTrue(report.package_absent_after)
        self.assertFalse(report.recovery_needed)
        self.assertEqual(channel.uninstalls, [])

    def test_preexisting_package_is_never_uninstalled(self):
        channel = FakeChannel(_config(), preexisting_package=True)
        report = _ops(channel).session(RUN_ID).close_owned_run(liveops.OwnedRun(
            run_id=RUN_ID, package_id=PACKAGE, absence_confirmed=False,
            install_attempted=True, uploads=()))
        self.assertTrue(report.recovery_needed)
        self.assertFalse(report.package_absent_after)
        self.assertEqual(channel.uninstalls, [])


# ---------------------------------------------------------------- report/native bounds

class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.channel = FakeChannel(_config())
        self.ops = _ops(self.channel)
        self.paths = liveops.data_paths(PACKAGE)
        self.wgt = make_wgt()
        self.wgt_path = liveops.remote_wgt_path(liveops.remote_run_dir(RUN_ID), PACKAGE)

    def _install(self):
        session = self.ops.session(RUN_ID)
        self.assertTrue(session.assert_absent(PACKAGE, self.paths).absent)
        session.install_widget(PACKAGE, self.wgt, sha(self.wgt), self.wgt_path)
        return session

    def test_symlink_report_is_refused(self):
        self.channel.report_symlink = True
        session = self._install()
        session.launch_facts(PACKAGE)
        with self.assertRaises(liveops.LiveOpsError):
            session.read_report(PACKAGE, self.paths)

    def test_oversize_report_is_refused(self):
        self.channel.report_oversize = True
        session = self._install()
        session.launch_facts(PACKAGE)
        with self.assertRaises(liveops.LiveOpsError):
            session.read_report(PACKAGE, self.paths)

    def test_absent_report_is_refused_after_attempts(self):
        self.channel.report_absent = True
        session = self._install()
        session.launch_facts(PACKAGE)
        with self.assertRaises(liveops.LiveOpsError):
            session.read_report(PACKAGE, self.paths)

    def test_oversize_native_output_is_refused(self):
        self.channel.frame = b"x" * (liveops.MAX_NATIVE_FRAME_BYTES + 1)
        session = self._install()
        session.push_native(_uploads(), liveops.remote_run_dir(RUN_ID))
        with self.assertRaises(liveops.LiveOpsError):
            session.run_native(f"{liveops.remote_run_dir(RUN_ID)}/QuietTVInspect.dll")

    def test_large_native_output_within_the_bound_is_accepted_exactly(self):
        frame = b"x" * 1_000_000
        self.channel.frame = frame
        session = self._install()
        session.push_native(_uploads(), liveops.remote_run_dir(RUN_ID))
        result = session.run_native(f"{liveops.remote_run_dir(RUN_ID)}/QuietTVInspect.dll")
        self.assertEqual(result.stdout, frame)

    def test_symlinked_native_output_is_refused(self):
        self.channel.native_link = True
        session = self._install()
        session.push_native(_uploads(), liveops.remote_run_dir(RUN_ID))
        with self.assertRaises(liveops.LiveOpsError):
            session.run_native(f"{liveops.remote_run_dir(RUN_ID)}/QuietTVInspect.dll")

    def test_nonregular_native_output_is_refused(self):
        self.channel.native_nonregular = True
        session = self._install()
        session.push_native(_uploads(), liveops.remote_run_dir(RUN_ID))
        with self.assertRaises(liveops.LiveOpsError):
            session.run_native(f"{liveops.remote_run_dir(RUN_ID)}/QuietTVInspect.dll")

    def test_failed_pushes_refuse_before_install_and_native(self):
        self.channel.push_error = True
        session = self.ops.session(RUN_ID)
        self.assertTrue(session.assert_absent(PACKAGE, self.paths).absent)
        with self.assertRaises(liveops.LiveOpsError):
            session.install_widget(PACKAGE, self.wgt, sha(self.wgt), self.wgt_path)
        self.assertEqual(self.channel.installs, [])
        self.assertEqual(self.channel.pushes, [])

    def test_native_push_requires_this_sessions_created_directory(self):
        session = self.ops.session(RUN_ID)
        session.assert_absent(PACKAGE, self.paths)
        with self.assertRaises(liveops.LiveOpsError):
            session.push_native(_uploads(), liveops.remote_run_dir(RUN_ID))
        self.assertEqual(self.channel.pushes, [])

    def test_native_execution_requires_a_pushed_bundle(self):
        session = self._install()
        with self.assertRaises(liveops.LiveOpsError):
            session.run_native(f"{liveops.remote_run_dir(RUN_ID)}/QuietTVInspect.dll")
        self.assertFalse(any("--inventory-framed" in command
                             for probe in self.channel.probes
                             for command in probe))

    def test_partial_native_push_leaves_no_trusted_bundle(self):
        self.channel.push_error_at = 2
        session = self._install()
        with self.assertRaises(liveops.LiveOpsError):
            session.push_native(_uploads(), liveops.remote_run_dir(RUN_ID))
        with self.assertRaises(liveops.LiveOpsError):
            session.run_native(f"{liveops.remote_run_dir(RUN_ID)}/QuietTVInspect.dll")
        self.assertFalse(any("--inventory-framed" in command
                             for probe in self.channel.probes
                             for command in probe))

    def test_replaced_uploaded_bundle_refuses_before_dotnet(self):
        session = self._install()
        session.push_native(_uploads(), liveops.remote_run_dir(RUN_ID))
        dll_path = f"{liveops.remote_run_dir(RUN_ID)}/QuietTVInspect.dll"
        self.channel.files[dll_path] = b"replaced-by-someone-else"
        with self.assertRaises(liveops.LiveOpsError):
            session.run_native(f"{liveops.remote_run_dir(RUN_ID)}/QuietTVInspect.dll")
        self.assertNotIn(f"{liveops.remote_run_dir(RUN_ID)}/native.out",
                         self.channel.files)
        self.assertEqual(self.channel.files[dll_path], b"replaced-by-someone-else")

    def test_install_reported_error_is_not_accepted(self):
        self.channel.install_reported = True
        session = self._install_no_assert()
        result = session.install_widget(PACKAGE, self.wgt, sha(self.wgt), self.wgt_path)
        self.assertFalse(result.installed)
        self.assertFalse(self.channel.installed)

    def test_install_exit_zero_without_package_rows_is_not_accepted(self):
        self.channel.install_no_package = True
        session = self._install_no_assert()
        result = session.install_widget(PACKAGE, self.wgt, sha(self.wgt), self.wgt_path)
        self.assertTrue(result.pushed)
        self.assertTrue(result.install_attempted)
        self.assertFalse(result.installed)
        self.assertFalse(self.channel.installed)

    def test_results_echo_one_stable_opaque_pair(self):
        session = self._install_no_assert()
        first = session.assert_absent(PACKAGE, self.paths)
        result = session.install_widget(PACKAGE, self.wgt, sha(self.wgt), self.wgt_path)
        launch = session.launch_facts(PACKAGE)
        for item in (first, result, launch):
            self.assertEqual(item.run_id, RUN_ID)
            self.assertEqual(item.session_token, session.session_token)
            self.assertIs(item.binding, session.binding)

    def _install_no_assert(self):
        session = self.ops.session(RUN_ID)
        session.assert_absent(PACKAGE, self.paths)
        return session


# ---------------------------------------------------------------- cleanup ownership

class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.channel = FakeChannel(_config())
        self.ops = _ops(self.channel)
        self.paths = liveops.data_paths(PACKAGE)
        self.wgt = make_wgt()
        self.wgt_path = liveops.remote_wgt_path(liveops.remote_run_dir(RUN_ID), PACKAGE)

    def _run_to_installed(self, uploads=True):
        session = self.ops.session(RUN_ID)
        self.assertTrue(session.assert_absent(PACKAGE, self.paths).absent)
        session.install_widget(PACKAGE, self.wgt, sha(self.wgt), self.wgt_path)
        session.launch_facts(PACKAGE)
        session.read_report(PACKAGE, self.paths)
        if uploads:
            session.push_native(_uploads(), liveops.remote_run_dir(RUN_ID))
            session.run_native(f"{liveops.remote_run_dir(RUN_ID)}/QuietTVInspect.dll")
        return session

    def test_replaced_upload_is_left_intact_and_reported(self):
        session = self._run_to_installed()
        dll_path = f"{liveops.remote_run_dir(RUN_ID)}/QuietTVInspect.dll"
        self.channel.files[dll_path] = b"replaced-by-someone-else"
        report = session.close_owned_run(_owned())
        self.assertTrue(report.recovery_needed)
        self.assertIn(dll_path, report.unconfirmed_uploads)
        self.assertEqual(self.channel.files[dll_path], b"replaced-by-someone-else")
        self.assertNotIn(dll_path, report.removed_uploads)
        self.assertFalse(self.channel.installed)

    def test_uninstall_error_is_refused_without_upload_removal(self):
        self.channel.uninstall_raise = True
        session = self._run_to_installed()
        with self.assertRaises(liveops.LiveOpsError):
            session.close_owned_run(_owned())
        self.assertTrue(self.channel.installed)
        self.assertTrue(self.channel.files)

    def test_failed_install_with_package_present_is_left_for_recovery(self):
        self.channel.install_rc = 1
        self.channel.install_package_present = True
        session = self.ops.session(RUN_ID)
        self.assertTrue(session.assert_absent(PACKAGE, self.paths).absent)
        result = session.install_widget(PACKAGE, self.wgt, sha(self.wgt), self.wgt_path)
        self.assertFalse(result.installed)
        self.channel.data[self.paths[0]] = b"partial-report"
        report = session.close_owned_run(_owned())
        self.assertTrue(report.recovery_needed)
        self.assertEqual(self.channel.uninstalls, [])
        self.assertTrue(self.channel.installed)
        self.assertEqual(self.channel.data[self.paths[0]], b"partial-report")

    def test_cleanup_removes_only_owned_paths(self):
        session = self._run_to_installed()
        report = session.close_owned_run(_owned())
        self.assertFalse(report.recovery_needed)
        self.assertTrue(report.package_removed)
        self.assertTrue(report.package_absent_after)
        self.assertEqual(set(report.removed_uploads),
                         {upload.remote_path for upload in _owned().uploads})
        self.assertEqual(self.channel.files, {})
        self.assertEqual(self.channel.data, {})
        self.assertEqual(self.channel.dirs, set())


# ---------------------------------------------------------------- raw channel

class ChannelTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="quiettv-channel-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, ignore_errors=True))
        self.sdk = self.root / "sdb.exe"
        self.sdk.write_bytes(b"fake-sdk")
        self.archive = self.root / "SamsungTvArchiveRoot.dll"
        self.archive.write_bytes(b"archive")
        self.agent = self.root / "SamsungTvRootAgent.dll"
        self.agent.write_bytes(b"agent")
        self.openssl = self.root / ("openssl.exe" if os.name == "nt" else "openssl")
        self.openssl.write_bytes(b"openssl")
        self.config = _config(
            sdk_executable=self.sdk, sdk_sha256=sha(self.sdk.read_bytes()),
            archive_root_dll=self.archive, archive_root_sha256=sha(b"archive"),
            root_agent_dll=self.agent, root_agent_sha256=sha(b"agent"),
            openssl_executable=self.openssl,
            openssl_sha256=sha(self.openssl.read_bytes()))
        self.channel = pchannel.LiveChannel(self.config)
        self.addCleanup(self.channel.close)
        self.calls = []

    def _fake(self, stdout=b"", stderr=b"", returncode=0, inspect=None):
        def run(argv, **kwargs):
            self.calls.append((tuple(argv), kwargs))
            if inspect is not None:
                inspect(tuple(argv), kwargs)
            return proc.ProcessResult(returncode, stdout, stderr)
        return run

    def test_raw_sdk_arguments_are_fixed_and_never_stop_a_server(self):
        with mock.patch.object(pchannel.proc, "run", side_effect=self._fake(b"DUID-1\n")):
            self.assertEqual(self.channel.getduid(), "DUID-1")
        self.assertEqual(self.calls[0][0],
                         (str(self.sdk), "connect", TARGET + ":26101"))
        argv, kwargs = self.calls[-1]
        self.assertEqual(argv, (str(self.sdk), "-s", TARGET + ":26101", "shell", "0 getduid"))
        self.assertTrue(kwargs["env"]["PATH"].split(os.pathsep)[0] == str(self.sdk.parent))

        group_request = {}
        def inspect(argv, kwargs):
            group_request.update(json.loads(kwargs["input_bytes"]))
        with mock.patch.object(
                pchannel.proc, "run",
                side_effect=self._fake(json.dumps({"ok": True, "group": 100}).encode(),
                                       inspect=inspect)):
            self.assertEqual(self.channel.group_id(), 100)
        self.assertEqual(group_request["operation"], "sdk-group")
        self.assertNotIn("commands", group_request)
        for call, _ in self.calls:
            self.assertNotIn("id -g", call)

        with mock.patch.object(pchannel.proc, "run", side_effect=self._fake()):
            outcome = self.channel.install(PACKAGE, "/home/owner/share/tmp/sdk_tools/run/P.wgt")
            self.assertEqual(outcome.returncode, 0)
            self.assertFalse(outcome.reported_error)
            self.channel.uninstall(PACKAGE)
        self.assertEqual(self.calls[-2][0][-1],
                         f"0 vd_appinstall {PACKAGE} "
                         "/home/owner/share/tmp/sdk_tools/run/P.wgt")
        self.assertEqual(self.calls[-1][0][-1], f"0 vd_appuninstall {PACKAGE}")
        for argv, _ in self.calls:
            joined = " ".join(argv)
            self.assertNotIn("kill-server", joined)
            self.assertNotIn("disconnect", joined)

    def test_getduid_accepts_only_transport_line_terminators(self):
        accepted = (b"SYNTH-DUID-01\n", b"SYNTH-DUID-01\r\n",
                    b"SYNTH-DUID-01\r\r\n", b"SYNTH-DUID-01")
        refused = (b"SYNTH-DUID-01\r\nOTHER\r\n", b"SYNTH-DUID-01\r\nleading\n",
                   b"prefix SYNTH-DUID-01\r\n", b"SYNTH-DUID-01 suffix\r\n",
                   b"\r\nSYNTH-DUID-01\r\n", b"SYNTH\r\n-DUID-01\r\n")
        with mock.patch.object(pchannel.proc, "run",
                               side_effect=self._fake(b"SYNTH-DUID-01\r\r\n")):
            self.assertEqual(self.channel.getduid(), "SYNTH-DUID-01")
        self.assertEqual(self.calls[0][0],
                         (str(self.sdk), "connect", TARGET + ":26101"))
        self.assertEqual(self.calls[-1][0],
                         (str(self.sdk), "-s", TARGET + ":26101", "shell",
                          "0 getduid"))
        for payload in accepted:
            channel = pchannel.LiveChannel(self.config)
            self.addCleanup(channel.close)
            with mock.patch.object(pchannel.proc, "run",
                                   side_effect=self._fake(payload)):
                self.assertEqual(channel.getduid(), "SYNTH-DUID-01", payload)
        for payload in refused:
            channel = pchannel.LiveChannel(self.config)
            self.addCleanup(channel.close)
            with mock.patch.object(pchannel.proc, "run",
                                   side_effect=self._fake(payload)):
                with self.assertRaises(pchannel.ChannelError):
                    channel.getduid()

    def test_failed_connect_refuses_before_duid_or_push(self):
        cases = (dict(returncode=1), dict(stderr=b"error: no route\n"))
        for case in cases:
            self.calls.clear()
            with mock.patch.object(pchannel.proc, "run",
                                   side_effect=self._fake(b"", **case)):
                with self.assertRaises(pchannel.ChannelError):
                    self.channel.getduid()
            self.assertEqual(len(self.calls), 1)
            self.assertEqual(self.calls[0][0],
                             (str(self.sdk), "connect", TARGET + ":26101"))

        self.calls.clear()
        with mock.patch.object(pchannel.proc, "run",
                               side_effect=self._fake(returncode=1)):
            with self.assertRaises(pchannel.ChannelError):
                self.channel.push_bytes(b"x", "/home/owner/share/tmp/sdk_tools/run/a.wgt")
        self.assertEqual([call[0][-1] for call in self.calls], [TARGET + ":26101"])

    def test_connect_is_issued_exactly_once_and_never_disconnects(self):
        with mock.patch.object(pchannel.proc, "run",
                               side_effect=self._fake(b"DUID-1\n")):
            self.channel.getduid()
            self.channel.install(PACKAGE, "/home/owner/share/tmp/sdk_tools/run/P.wgt")
            self.channel.uninstall(PACKAGE)
        connects = [call for call, _ in self.calls
                    if call[1:] == ("connect", TARGET + ":26101")]
        self.assertEqual(len(connects), 1)
        for argv, _ in self.calls:
            joined = " ".join(argv)
            self.assertNotIn("kill-server", joined)
            self.assertNotIn("disconnect", joined)

    def test_push_uses_exact_bytes_and_removes_its_local_copy(self):
        seen = {}

        def effect(argv, **kwargs):
            self.calls.append((tuple(argv), kwargs))
            if tuple(argv[1:4]) == ("-s", TARGET + ":26101", "push"):
                local = Path(argv[4])
                seen["bytes"] = local.read_bytes()
                seen["existed_during_push"] = local.exists()
                return proc.ProcessResult(0, b"", b"")
            evidence = {"ok": True, "authenticated": True, "uid": 0, "euid": 0,
                        "gid": 0, "egid": 0, "runtime": "6.0",
                        "system_files_unchanged": True, "staging_cleaned": True,
                        "commands": [{"exit_code": 0, "timed_out": False,
                                      "stdout": sha(b"exact-bytes") + "  a.wgt\n"}]}
            return proc.ProcessResult(0, json.dumps(evidence).encode(), b"")
        with mock.patch.object(pchannel.proc, "run", side_effect=effect):
            self.channel.push_bytes(b"exact-bytes", "/home/owner/share/tmp/sdk_tools/run/a.wgt")
        self.assertEqual(seen["bytes"], b"exact-bytes")
        self.assertTrue(seen["existed_during_push"])
        push_index = next(index for index, (argv, _) in enumerate(self.calls)
                          if argv[1:4] == ("-s", TARGET + ":26101", "push"))
        self.assertFalse(Path(self.calls[push_index][0][4]).exists())
        request = json.loads(self.calls[-1][1]["input_bytes"])
        self.assertTrue(request["commands"][0].startswith("sha256sum "))

    def test_upload_hash_readback_mismatch_is_refused(self):
        def effect(argv, **kwargs):
            self.calls.append((tuple(argv), kwargs))
            if tuple(argv[1:4]) == ("-s", TARGET + ":26101", "push"):
                return proc.ProcessResult(0, b"", b"")
            evidence = {"ok": True, "authenticated": True, "uid": 0, "euid": 0,
                        "gid": 0, "egid": 0, "runtime": "6.0",
                        "system_files_unchanged": True, "staging_cleaned": True,
                        "commands": [{"exit_code": 0, "timed_out": False,
                                      "stdout": "0" * 64 + "  a.wgt\n"}]}
            return proc.ProcessResult(0, json.dumps(evidence).encode(), b"")
        with mock.patch.object(pchannel.proc, "run", side_effect=effect):
            with self.assertRaises(pchannel.ChannelError):
                self.channel.push_bytes(b"exact-bytes", "/home/owner/share/tmp/sdk_tools/run/a.wgt")

    def test_push_reported_error_with_exit_zero_is_refused(self):
        def effect(argv, **kwargs):
            self.calls.append((tuple(argv), kwargs))
            if tuple(argv[1:4]) == ("-s", TARGET + ":26101", "push"):
                return proc.ProcessResult(0, b"error: closed\n", b"")
            return proc.ProcessResult(0, b"", b"")
        with mock.patch.object(pchannel.proc, "run", side_effect=effect):
            with self.assertRaises(pchannel.ChannelError):
                self.channel.push_bytes(b"x", "/home/owner/share/tmp/sdk_tools/run/a.wgt")
        push_index = next(index for index, (argv, _) in enumerate(self.calls)
                          if argv[1:4] == ("-s", TARGET + ":26101", "push"))
        self.assertFalse(Path(self.calls[push_index][0][4]).exists())

    def test_probe_launches_the_isolated_worker_with_exact_request(self):
        evidence = {
            "ok": True, "authenticated": True, "uid": 0, "euid": 0,
            "gid": 0, "egid": 0, "runtime": "6.0",
            "system_files_unchanged": True, "staging_cleaned": True,
            "commands": [{"exit_code": 0, "timed_out": False, "stdout": "ok"}],
        }
        request_seen = {}

        def inspect(argv, kwargs):
            if "input_bytes" in kwargs:
                request_seen["request"] = json.loads(kwargs["input_bytes"])
        with mock.patch.object(pchannel.proc, "run",
                               side_effect=self._fake(json.dumps(evidence).encode(),
                                                      inspect=inspect)):
            result = self.channel.probe(("fixed-command",))
        self.assertEqual(result.commands[0].stdout, "ok")
        argv = self.calls[-1][0]
        self.assertEqual(Path(argv[0]).name, Path(sys.executable).name)
        self.assertEqual(argv[1:3], ("-I", "-B"))
        self.assertTrue(argv[3].endswith("worker.py"))
        request = request_seen["request"]
        self.assertEqual(request["commands"], ["fixed-command"])
        self.assertEqual(request["target"], TARGET)
        self.assertEqual(request["callbackHost"], CALLBACK)
        self.assertEqual(request["payloadSha256"]["SamsungTvArchiveRoot.dll"],
                         sha(b"archive"))
        payload_dir = Path(request["payloadDirectory"])
        self.assertEqual((payload_dir / "SamsungTvArchiveRoot.dll").read_bytes(), b"archive")
        self.assertEqual((payload_dir / "SamsungTvRootAgent.dll").read_bytes(), b"agent")

    def test_probe_refuses_unauthenticated_or_incomplete_evidence(self):
        bad = [{"ok": True, "authenticated": False},
               {"ok": False},
               {"ok": True, "authenticated": True, "uid": 1, "euid": 0, "gid": 0,
                "egid": 0, "runtime": "6.0", "system_files_unchanged": True,
                "staging_cleaned": True, "commands": []}]
        for evidence in bad:
            with mock.patch.object(pchannel.proc, "run",
                                   side_effect=self._fake(json.dumps(evidence).encode())):
                with self.assertRaises(pchannel.ChannelError):
                    self.channel.probe(("fixed-command",))

    def test_sdk_group_read_returns_only_a_bounded_integer(self):
        for value in (True, "100", -1, 2**31, None):
            result = {"ok": True, "group": value} if value is not None else {"ok": True}
            with mock.patch.object(pchannel.proc, "run",
                                   side_effect=self._fake(json.dumps(result).encode())):
                with self.assertRaises(pchannel.ChannelError):
                    self.channel.group_id()
        with mock.patch.object(pchannel.proc, "run",
                               side_effect=self._fake(
                                   json.dumps({"ok": True, "group": 100}).encode())):
            self.assertEqual(self.channel.group_id(), 100)


class WorkerTests(unittest.TestCase):
    def _sdk_group_request(self, **over):
        request = {"target": TARGET, "callbackHost": CALLBACK,
                   "captureSeconds": 30.0}
        request.update(over)
        return request

    def test_group_capture_is_one_fixed_command_not_raw_shell(self):
        seen = {}

        class Client:
            def __init__(self, sdk, target, timeout=None):
                seen["client"] = (str(sdk), target, timeout)

            def capture(self, command, *, callback_host, bind_host, timeout):
                seen["capture"] = (command, callback_host, bind_host, timeout)
                return SimpleNamespace(transport_returncode=0,
                                       output="5001\n[exit:0]\n")

        module = SimpleNamespace(SdbClient=Client)
        sdk = Path("/pinned/sdb")
        result = pworker._sdk_group(
            self._sdk_group_request(), module, sdk)
        self.assertEqual(result, {"ok": True, "group": 5001})
        self.assertEqual(seen["client"], (str(sdk), TARGET, 30.0))
        self.assertEqual(seen["capture"],
                         ("id -g", CALLBACK, CALLBACK, 30.0))
        # No root/probe fields are present in a group result.
        self.assertEqual(set(result), {"ok", "group"})

    def test_worker_applies_measured_injection_timing_in_the_child(self):
        seen = {}
        module_sdb = SimpleNamespace(
            SHELL_INJECTION_DELAY_SECONDS=2.0, SHELL_INJECTION_MINIMUM_DELTA=1.5,
            SDB_APPINSTALL_MAX_SAFE_BYTES=510)

        class Client:
            def __init__(self, *args, **kwargs):
                pass

            def capture(self, command, *, callback_host, bind_host, timeout):
                seen["delay"] = module_sdb.SHELL_INJECTION_DELAY_SECONDS
                seen["delta"] = module_sdb.SHELL_INJECTION_MINIMUM_DELTA
                seen["limit"] = module_sdb.SDB_APPINSTALL_MAX_SAFE_BYTES
                return SimpleNamespace(transport_returncode=0,
                                       output="100\n[exit:0]\n")

        module_sdb.SdbClient = Client
        request = {"operation": "sdk-group",
                   "vendorDirectory": "/vendor/samsung_tv_root",
                   "sdkExecutable": "/pinned/sdb", "sdkSha256": "0" * 64,
                   "target": TARGET, "callbackHost": CALLBACK,
                   "captureSeconds": 30.0}
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                pworker, "_load_vendor", return_value=SimpleNamespace()))
            stack.enter_context(mock.patch.object(
                pworker.importlib, "import_module", return_value=module_sdb))
            stack.enter_context(mock.patch.object(
                pworker, "_verify", return_value=Path("/verified")))
            result = asyncio.run(pworker._run(request))
        self.assertEqual(result, {"ok": True, "group": 100})
        self.assertEqual(seen, {"delay": 6.0, "delta": 4.0, "limit": 300})
        self.assertEqual(module_sdb.SHELL_INJECTION_DELAY_SECONDS, 6.0)
        self.assertEqual(module_sdb.SHELL_INJECTION_MINIMUM_DELTA, 4.0)
        self.assertEqual(module_sdb.SDB_APPINSTALL_MAX_SAFE_BYTES, 300)

    def test_group_capture_accepts_transport_one_only_with_a_clean_callback(self):
        def group(output, transport):
            class Client:
                def __init__(self, *args, **kwargs):
                    pass

                def capture(self, *args, **kwargs):
                    return SimpleNamespace(output=output,
                                           transport_returncode=transport)

            return pworker._sdk_group(self._sdk_group_request(),
                                      SimpleNamespace(SdbClient=Client),
                                      Path("/pinned/sdb"))

        # The reference launcher reports transport 1 for the deliberately
        # invalid package name; the strict body and [exit:0] prove success.
        self.assertEqual(group("1000\n[exit:0]\n", 1), {"ok": True, "group": 1000})
        self.assertEqual(group("1000\n[exit:0]\n", 0), {"ok": True, "group": 1000})
        for output, transport in (("1000\n[exit:0]\n", 2),
                                  ("1000\n[exit:1]\n", 0),
                                  ("1000\n[exit:1]\n", 1),
                                  ("1000\n[exit:2]\n", 1),
                                  ("5001\n5002\n[exit:0]\n", 1),
                                  ("not-a-group\n[exit:0]\n", 1),
                                  ("1000\n[exit:0]\n1001\n", 1),
                                  ("", 1)):
            with self.assertRaises(RuntimeError):
                group(output, transport)

    def test_group_capture_rejects_failed_transport_and_bad_requests(self):
        class Failed:
            def __init__(self, *args, **kwargs):
                pass

            def capture(self, *args, **kwargs):
                return SimpleNamespace(transport_returncode=2, output="")

        with self.assertRaises(RuntimeError):
            pworker._sdk_group(self._sdk_group_request(),
                               SimpleNamespace(SdbClient=Failed),
                               Path("/pinned/sdb"))
        for over in ({"target": None}, {"callbackHost": 1},
                     {"captureSeconds": 0}, {"captureSeconds": 121},
                     {"captureSeconds": "30"}):
            with self.assertRaises(RuntimeError):
                pworker._sdk_group(self._sdk_group_request(**over),
                                   SimpleNamespace(SdbClient=Failed),
                                   Path("/pinned/sdb"))

    def test_group_output_parser_is_bounded_and_requires_exit_zero(self):
        self.assertEqual(pworker._parse_group_capture("100\n[exit:0]\n"), 100)
        self.assertEqual(pworker._parse_group_capture("0\n[exit:0]"), 0)
        bad = ("", "100\n", "100\n[exit:1]\n", "x\n[exit:0]\n",
               "1\n2\n[exit:0]\n", "-1\n[exit:0]\n",
               "y" * (pworker._MAX_GROUP_OUTPUT_BYTES + 1) + "\n[exit:0]\n")
        for output in bad:
            with self.assertRaises(RuntimeError):
                pworker._parse_group_capture(output)

    def test_only_runtime_6_is_accepted(self):
        pworker._require_net6("6.0")
        for runtime in ("3.1", "5.0", "6.0.0", 6, None, "7.0"):
            with self.assertRaises(RuntimeError):
                pworker._require_net6(runtime)

    def _fallback_archive(self, listing):
        class ArchiveRootError(RuntimeError):
            pass

        def original(client, command, timeout=15.0):
            if command == "/usr/bin/dotnet --list-runtimes":
                raise ArchiveRootError("Unknown option --list-runtimes")
            if command.startswith("ls -d "):
                return listing
            if command.endswith("echo verified"):
                return "verified"
            raise AssertionError(command)

        archive = SimpleNamespace(_capture=original,
                                  ArchiveRootError=ArchiveRootError)
        return archive

    def test_runtime_fallback_never_substitutes_net31(self):
        module = SimpleNamespace(SDB_APPINSTALL_MAX_SAFE_BYTES=900)
        base = "/usr/share/dotnet/shared/Microsoft.NETCore.App/"
        archive = self._fallback_archive(f"{base}3.1.0\n{base}6.0.12")
        pworker._runtime_fallback(archive, module)
        self.assertEqual(module.SDB_APPINSTALL_MAX_SAFE_BYTES, 300)
        self.assertEqual(archive._capture(None, "/usr/bin/dotnet --list-runtimes"),
                         "Microsoft.NETCore.App 6.0.12 [" + base.rstrip("/") + "]")

        only31 = self._fallback_archive(f"{base}3.1.0")
        with self.assertRaises(only31.ArchiveRootError):
            pworker._runtime_fallback(only31, module)
            only31._capture(None, "/usr/bin/dotnet --list-runtimes")

    def _run_request(self):
        return {
            "operation": "root-probe",
            "vendorDirectory": "/vendor/samsung_tv_root",
            "sdkExecutable": "/pinned/sdb", "sdkSha256": "0" * 64,
            "opensslExecutable": "/pinned/openssl", "opensslSha256": "1" * 64,
            "payloadDirectory": "/payloads",
            "payloadSha256": {"SamsungTvArchiveRoot.dll": "2" * 64,
                              "SamsungTvRootAgent.dll": "3" * 64},
            "target": TARGET, "callbackHost": CALLBACK,
            "sdbSeconds": 10.0, "acceptSeconds": 30.0,
            "commandSeconds": 10.0, "commands": ["fixed-command"],
        }

    def _run_with_runtime(self, runtime):
        calls = []

        class Client:
            def __init__(self, *args, **kwargs):
                pass

        def check_target(client):
            calls.append("check")
            return SimpleNamespace(runtime=runtime)

        async def probe(*args, **kwargs):
            calls.append("probe")
            return {"authenticated": True, "uid": 0, "euid": 0, "gid": 0,
                    "egid": 0, "runtime": "6.0",
                    "system_files_unchanged": True, "staging_cleaned": True,
                    "commands": [{"exit_code": 0, "timed_out": False,
                                  "stdout": "ok"}]}

        archive = SimpleNamespace(_check_target=check_target, probe=probe)
        module_sdb = SimpleNamespace(SdbClient=Client)
        modules = {"samsung_tv_root.sdb": module_sdb,
                   "samsung_tv_root.archive_root": archive}
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                pworker, "_load_vendor", return_value=SimpleNamespace()))
            stack.enter_context(mock.patch.object(
                pworker.importlib, "import_module",
                side_effect=lambda name: modules[name]))
            stack.enter_context(mock.patch.object(
                pworker, "_verify", return_value=Path("/verified")))
            stack.enter_context(mock.patch.object(
                pworker, "_runtime_fallback", return_value=None))
            stack.enter_context(mock.patch.object(
                pworker, "_explicit_openssl", return_value=lambda: None))
            try:
                result = asyncio.run(pworker._run(self._run_request()))
            except RuntimeError:
                result = None
        return result, calls, archive

    def test_net31_is_rejected_before_payload_staging_or_probe(self):
        result, calls, _archive = self._run_with_runtime("3.1")
        self.assertIsNone(result)
        self.assertEqual(calls, ["check"])

    def test_net6_proceeds_to_the_probe_after_the_runtime_check(self):
        result, calls, _archive = self._run_with_runtime("6.0")
        self.assertEqual(calls, ["check", "probe"])
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["commands"][0]["stdout"], "ok")


class BoundedReadTests(unittest.TestCase):
    def test_report_command_is_head_bounded_and_refuses_nonregular(self):
        command = commands.report_read_command("/data/productinfo")
        self.assertIn(f"head -c {liveops.MAX_REPORT_READ_BYTES} ", command)
        self.assertIn("!symlink!", command)
        self.assertIn("!nonregular!", command)
        self.assertNotIn("base64 /data/productinfo", command)
        self.assertIn("!symlink!", commands.native_size_command(
            liveops.remote_run_dir(RUN_ID)))
        self.assertIn("!nonregular!", commands.native_chunk_command(
            liveops.remote_run_dir(RUN_ID), 0))

    def test_base64_decoder_is_strict_on_every_platform(self):
        self.assertEqual(psession._decode_base64("aGVsbG8="), b"hello")
        self.assertEqual(psession._decode_base64("aGVs\nbG8="), b"hello")
        for text in ("", "!!!!", "aGVsbG8=x", "a"):
            with self.assertRaises(liveops.LiveOpsError):
                psession._decode_base64(text)

    @unittest.skipUnless(os.name != "nt", "POSIX shell fixture")
    def test_local_shell_report_read_is_truly_bounded(self):
        root = Path(tempfile.mkdtemp(prefix="quiettv-read-"))
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        target = root / "report.bin"
        target.write_bytes(b"r" * (liveops.MAX_REPORT_READ_BYTES + 4096))
        result = subprocess.run(
            ("sh", "-c", commands.report_read_command(str(target))),
            capture_output=True, timeout=20, check=True)
        decoded = base64.b64decode(result.stdout)
        self.assertEqual(len(decoded), liveops.MAX_REPORT_READ_BYTES)
        self.assertGreater(len(decoded), liveops.MAX_REPORT_BYTES)

    @unittest.skipUnless(os.name != "nt", "POSIX shell fixture")
    def test_local_shell_refuses_symlink_fifo_and_directory(self):
        root = Path(tempfile.mkdtemp(prefix="quiettv-read-"))
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        real = root / "real.bin"
        real.write_bytes(b"x")
        link = root / "link.bin"
        os.symlink(real, link)
        fifo = root / "fifo.bin"
        os.mkfifo(fifo)
        folder = root / "folder.bin"
        folder.mkdir()
        absent = root / "absent.bin"
        expectations = ((link, b"!symlink!"), (fifo, b"!nonregular!"),
                        (folder, b"!nonregular!"), (absent, b"!absent!"))
        for path, expected in expectations:
            result = subprocess.run(
                ("sh", "-c", commands.report_read_command(str(path))),
                capture_output=True, timeout=20, check=True)
            self.assertEqual(result.stdout.strip(), expected)

    @unittest.skipUnless(os.name != "nt", "POSIX shell fixture")
    def test_local_shell_read_cannot_outrun_a_growing_file(self):
        root = Path(tempfile.mkdtemp(prefix="quiettv-read-"))
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        target = root / "growing.bin"
        target.write_bytes(b"g" * (liveops.MAX_REPORT_READ_BYTES + 1))
        writer = subprocess.Popen(
            (sys.executable, "-c",
             "import sys,time\n"
             f"h = open({str(target)!r}, 'ab')\n"
             "end = time.time() + 3\n"
             "while time.time() < end:\n"
             "    h.write(b'x' * 65536); h.flush()\n"),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            result = subprocess.run(
                ("sh", "-c", commands.report_read_command(str(target))),
                capture_output=True, timeout=20, check=True)
        finally:
            writer.terminate()
            writer.wait(timeout=10)
        self.assertEqual(len(base64.b64decode(result.stdout)),
                         liveops.MAX_REPORT_READ_BYTES)


class ProcessTests(unittest.TestCase):
    def test_output_cap_is_enforced_without_succeeding(self):
        with self.assertRaises(proc.ProcessLimit):
            proc.run((sys.executable, "-c", "print('x' * 4096)"), timeout=10,
                     max_stdout=64, max_stderr=64)

    def test_timeout_kills_its_own_child_and_is_finite(self):
        started = time.monotonic()
        with self.assertRaises(proc.ProcessTimeout):
            proc.run((sys.executable, "-c", "import time; time.sleep(30)"),
                     timeout=0.7, max_stdout=64, max_stderr=64)
        self.assertLess(time.monotonic() - started, 10)

    def test_windows_children_are_grouped_without_a_visible_console(self):
        expected = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                    | getattr(subprocess, "CREATE_NO_WINDOW", 0))
        with mock.patch.object(proc.os, "name", "nt"):
            self.assertEqual(proc._creation_flags(), expected)

    def test_windows_taskkill_is_absolute_and_hidden(self):
        process = mock.Mock(pid=4321)
        create_no_window = 0x08000000
        missing = object()
        original = getattr(proc.subprocess, "CREATE_NO_WINDOW", missing)
        if original is not missing:
            delattr(proc.subprocess, "CREATE_NO_WINDOW")
        try:
            self.assertFalse(hasattr(proc.subprocess, "CREATE_NO_WINDOW"))
            with mock.patch.object(proc.os, "name", "nt"), \
                    mock.patch.object(proc.subprocess, "CREATE_NO_WINDOW",
                                      create_no_window, create=True), \
                    mock.patch.object(proc.subprocess, "run") as run:
                proc._kill_tree(process)
            argv = run.call_args.args[0]
            creationflags = run.call_args.kwargs["creationflags"]
        finally:
            if original is not missing:
                setattr(proc.subprocess, "CREATE_NO_WINDOW", original)
        self.assertTrue(argv[0].lower().endswith("taskkill.exe"))
        self.assertIn("System32", argv[0])
        self.assertTrue(creationflags & create_no_window)
        process.wait.assert_called_once()

    def test_input_is_delivered(self):
        result = proc.run((sys.executable, "-c",
                           "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"),
                          timeout=10, max_stdout=1024, max_stderr=1024,
                          input_bytes=b"hello")
        self.assertEqual(result.stdout, b"hello")

    def _producer(self, stream):
        return ("import sys, time\n"
                f"stream = sys.{stream}\n"
                "while True:\n"
                "    stream.write('x' * 8192)\n"
                "    stream.flush()\n")

    def test_running_stdout_producer_is_killed_at_the_cap_before_timeout(self):
        started = time.monotonic()
        with self.assertRaises(proc.ProcessLimit):
            proc.run((sys.executable, "-c", self._producer("stdout")),
                     timeout=20, max_stdout=4096, max_stderr=4096)
        self.assertLess(time.monotonic() - started, 10)

    def test_running_stderr_producer_is_killed_at_the_cap_before_timeout(self):
        started = time.monotonic()
        with self.assertRaises(proc.ProcessLimit):
            proc.run((sys.executable, "-c", self._producer("stderr")),
                     timeout=20, max_stdout=4096, max_stderr=4096)
        self.assertLess(time.monotonic() - started, 10)

    def test_cap_kill_removes_the_own_descendant_tree(self):
        marker = Path(tempfile.mkdtemp(prefix="quiettv-descendant-")) / "alive"
        self.addCleanup(lambda: shutil.rmtree(marker.parent, ignore_errors=True))
        code = (
            "import subprocess, sys\n"
            "subprocess.Popen([sys.executable, '-c', "
            f"\"import time; time.sleep(6); open({str(marker)!r}, 'w').write('x')\"])\n"
            + self._producer("stdout"))
        with self.assertRaises(proc.ProcessLimit):
            proc.run((sys.executable, "-c", code),
                     timeout=20, max_stdout=4096, max_stderr=4096)
        time.sleep(7.0)
        self.assertFalse(marker.exists())


# ---------------------------------------------------------------- owner config

class ProviderConfigTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="quiettv-config-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, ignore_errors=True))
        self.sdk = self.root / ("sdb.exe" if os.name == "nt" else "sdb")
        self.sdk.write_bytes(b"fake-sdk")
        self.archive = self.root / "SamsungTvArchiveRoot.dll"
        self.archive.write_bytes(b"archive")
        self.agent = self.root / "SamsungTvRootAgent.dll"
        self.agent.write_bytes(b"agent")
        self.openssl = self.root / ("openssl.exe" if os.name == "nt" else "openssl")
        self.openssl.write_bytes(b"openssl")

    def _document(self, **over):
        document = {
            "schema": pconfig.CONFIG_SCHEMA,
            "target": TARGET, "callbackHost": CALLBACK, "expectedSdkDuid": DUID,
            "sdk": {"executable": str(self.sdk), "sha256": sha(b"fake-sdk")},
            "rootPayloads": {
                "archiveRootDll": {"path": str(self.archive), "sha256": sha(b"archive")},
                "rootAgentDll": {"path": str(self.agent), "sha256": sha(b"agent")}},
            "openssl": {"executable": str(self.openssl), "sha256": sha(b"openssl")},
            "timeouts": {"sdkSeconds": 30, "probeSeconds": 120,
                         "commandSeconds": 30, "nativeSeconds": 60,
                         "reportAttempts": 2},
        }
        document.update(over)
        return document

    def _write(self, document, *, private=True):
        path = self.root / "provider.json"
        path.write_bytes(json.dumps(document).encode("utf-8"))
        if private:
            oi.make_private_file(path)
        return path

    def test_valid_config_loads_and_pins_every_artifact(self):
        config = pconfig.load_config(str(self._write(self._document())))
        self.assertEqual(config.target, TARGET)
        self.assertEqual(config.expected_sdk_duid, DUID)
        self.assertEqual(config.report_attempts, 2)

    def test_unknown_duplicate_and_out_of_range_documents_are_refused(self):
        bad = [
            self._document(extra=True),
            self._document(target="127.0.0.1"),
            self._document(target="8.8.8.8"),
            self._document(expectedSdkDuid="changeme"),
            self._document(timeouts={"sdkSeconds": 1, "probeSeconds": 2,
                                     "commandSeconds": 2, "nativeSeconds": 10,
                                     "reportAttempts": 1}),
            self._document(sdk={"executable": str(self.sdk), "sha256": "0" * 64}),
        ]
        for document in bad:
            path = self._write(document)
            with self.assertRaises(pconfig.ProviderConfigError):
                pconfig.load_config(str(path))
        path = self.root / "duplicate.json"
        path.write_text('{"schema":"x","schema":"y"}', encoding="utf-8")
        oi.make_private_file(path)
        with self.assertRaises(pconfig.ProviderConfigError):
            pconfig.load_config(str(path))

    def test_missing_artifact_and_in_repo_config_are_refused(self):
        document = self._document()
        document["rootPayloads"]["archiveRootDll"]["path"] = str(self.root / "missing.dll")
        with self.assertRaises(pconfig.ProviderConfigError):
            pconfig.load_config(str(self._write(document)))
        with self.assertRaises(pconfig.ProviderConfigError):
            pconfig.load_config(str(REPO / "README.md"))

    USER_SID = "S-1-5-21-1111111111-2222222222-3333333333-1001"
    OTHER_SID = "S-1-5-21-1111111111-2222222222-3333333333-1002"
    SYSTEM_SID = "S-1-5-18"
    ADMIN_SID = "S-1-5-32-544"

    def _acl_file(self):
        path = self.root / "acl.json"
        path.write_bytes(b"{}")
        return path

    def _acl_document(self, **over):
        document = {
            "ownerSid": self.USER_SID,
            "currentUserSid": self.USER_SID,
            "isAdmin": False,
            "aces": [{"sid": self.USER_SID, "type": "Allow"},
                     {"sid": self.SYSTEM_SID, "type": "Allow"},
                     {"sid": self.ADMIN_SID, "type": "Allow"}],
        }
        document.update(over)
        return document

    def _run_acl(self, document, *, returncode=0, stdout=None):
        path = self._acl_file()
        body = json.dumps(document) if stdout is None else stdout
        result = SimpleNamespace(returncode=returncode, stdout=body, stderr="")
        with mock.patch.object(pconfig.subprocess, "run", return_value=result) as run:
            try:
                pconfig._owner_private(path)
                outcome = "accepted"
            except pconfig.ProviderConfigError:
                outcome = "refused"
        return outcome, run

    @unittest.skipUnless(os.name == "nt", "Windows ACL validation")
    def test_current_owner_system_and_administrators_acl_is_accepted(self):
        outcome, run = self._run_acl(self._acl_document())
        self.assertEqual(outcome, "accepted")
        argv = run.call_args.args[0]
        self.assertTrue(str(argv[0]).lower().endswith("powershell.exe"))
        self.assertIn("System32", str(argv[0]))
        self.assertTrue(run.call_args.kwargs["creationflags"]
                        & getattr(subprocess, "CREATE_NO_WINDOW", 0))

    @unittest.skipUnless(os.name == "nt", "Windows ACL validation")
    def test_administrators_owner_with_current_user_allow_is_accepted(self):
        document = self._acl_document(ownerSid=self.ADMIN_SID,
                                      currentUserSid=self.USER_SID)
        outcome, _ = self._run_acl(document)
        self.assertEqual(outcome, "accepted")

    @unittest.skipUnless(os.name == "nt", "Windows ACL validation")
    def test_system_owner_with_current_user_allow_is_accepted(self):
        document = self._acl_document(ownerSid=self.SYSTEM_SID,
                                      currentUserSid=self.USER_SID)
        outcome, _ = self._run_acl(document)
        self.assertEqual(outcome, "accepted")

    @unittest.skipUnless(os.name == "nt", "Windows ACL validation")
    def test_unrelated_owner_is_refused_even_with_current_user_allow(self):
        document = self._acl_document(ownerSid=self.OTHER_SID,
                                      currentUserSid=self.USER_SID)
        outcome, _ = self._run_acl(document)
        self.assertEqual(outcome, "refused")

    @unittest.skipUnless(os.name == "nt", "Windows ACL validation")
    def test_administrators_owner_with_other_allow_is_refused(self):
        document = self._acl_document(
            ownerSid=self.ADMIN_SID, currentUserSid=self.USER_SID,
            aces=[{"sid": self.USER_SID, "type": "Allow"},
                  {"sid": self.OTHER_SID, "type": "Allow"}])
        outcome, _ = self._run_acl(document)
        self.assertEqual(outcome, "refused")

    @unittest.skipUnless(os.name == "nt", "Windows ACL validation")
    def test_acl_child_drops_inherited_psmodulepath_only(self):
        with mock.patch.dict(pconfig.os.environ,
                             {"PSMODULEPATH": r"C:\outer\PowerShell\7\Modules"},
                             clear=False):
            outcome, run = self._run_acl(self._acl_document())
            self.assertEqual(outcome, "accepted")
            environment = run.call_args.kwargs["env"]
            self.assertFalse(any(key.upper() == "PSMODULEPATH"
                                 for key in environment))
            self.assertEqual(environment["QTV_CONFIG_PATH"],
                             str(self.root / "acl.json"))
            self.assertTrue(run.call_args.kwargs["creationflags"]
                            & getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertEqual(pconfig.os.environ.get("PSMODULEPATH"),
                             r"C:\outer\PowerShell\7\Modules")

    @unittest.skipUnless(os.name == "nt", "Windows ACL validation")
    def test_other_named_user_or_world_allow_ace_is_refused(self):
        for sid in (self.OTHER_SID, "S-1-1-0", "S-1-5-32-545"):
            document = self._acl_document(
                aces=[{"sid": self.USER_SID, "type": "Allow"},
                      {"sid": sid, "type": "Allow"}])
            outcome, _ = self._run_acl(document)
            self.assertEqual(outcome, "refused", sid)

    @unittest.skipUnless(os.name == "nt", "Windows ACL validation")
    def test_unparseable_acls_and_missing_access_are_refused(self):
        cases = [
            {"stdout": "not-json"},
            {"stdout": json.dumps({"ownerSid": self.USER_SID})},
            {"document": self._acl_document(aces=[{"sid": "guest", "type": "Allow"}])},
            {"document": self._acl_document(aces=[{"sid": self.USER_SID,
                                                   "type": "Full"}])},
            {"document": self._acl_document(ownerSid="junk")},
            {"document": self._acl_document(
                ownerSid=self.OTHER_SID, currentUserSid=self.USER_SID,
                aces=[{"sid": self.SYSTEM_SID, "type": "Allow"}])},
            {"returncode": 1},
        ]
        for case in cases:
            document = case.get("document", self._acl_document())
            outcome, _ = self._run_acl(
                document, returncode=case.get("returncode", 0),
                stdout=case.get("stdout"))
            self.assertEqual(outcome, "refused", case)

    @unittest.skipIf(os.name == "nt", "POSIX mode privacy is a POSIX-only check")
    def test_world_readable_config_is_refused(self):
        path = self._write(self._document(), private=False)
        os.chmod(path, 0o644)
        with self.assertRaises(pconfig.ProviderConfigError):
            pconfig.load_config(str(path))


# ---------------------------------------------------------------- build helper

class BuildHelperTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="quiettv-build-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, ignore_errors=True))
        self.dotnet = self.root / ("dotnet.exe" if os.name == "nt" else "dotnet")
        self.dotnet.write_bytes(b"fake-dotnet")

    def test_dry_run_writes_nothing_and_names_the_fixed_artifacts(self):
        out = self.root / "build"
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = build_helper.main(["--dotnet", str(self.dotnet),
                                      "--out-dir", str(out), "--dry-run"])
        self.assertEqual(code, 0)
        self.assertFalse(out.exists())
        plan = json.loads(stdout.getvalue())
        self.assertEqual(plan["schema"], build_helper.MANIFEST_SCHEMA)
        self.assertEqual(plan["artifacts"], list(build_helper.ARTIFACT_NAMES))

    def test_dry_run_refuses_an_output_inside_a_repository(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = build_helper.main(["--dotnet", str(self.dotnet),
                                      "--out-dir", str(REPO / "build-here"),
                                      "--dry-run"])
        self.assertEqual(code, 2)
        self.assertFalse((REPO / "build-here").exists())

    def test_check_verifies_manifest_and_provider_payload_pins(self):
        artifacts_dir = self.root / "artifacts"
        artifacts_dir.mkdir()
        artifacts = {}
        for name in build_helper.ARTIFACT_NAMES:
            data = ("artifact-" + name).encode()
            path = artifacts_dir / name
            path.write_bytes(data)
            artifacts[name] = {"path": str(path), "sha256": sha(data), "bytes": len(data)}
        manifest = {"schema": build_helper.MANIFEST_SCHEMA,
                    "builtBy": {"dotnet": str(self.dotnet), "sha256": sha(b"fake-dotnet"),
                                "version": "6.0.0"},
                    "sources": {}, "artifacts": artifacts}
        manifest_path = self.root / "provider-artifacts.json"
        manifest_path.write_bytes(oi.json_bytes(manifest))
        oi.make_private_file(manifest_path)

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = build_helper.main(["--check", str(manifest_path)])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(stdout.getvalue())["ok"])

        artifacts["QuietTVInspect.dll"]["sha256"] = "0" * 64
        manifest_path.write_bytes(oi.json_bytes(manifest))
        oi.make_private_file(manifest_path)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(build_helper.main(["--check", str(manifest_path)]), 2)

    def test_check_compares_a_provider_config_payload_pin(self):
        artifacts_dir = self.root / "artifacts"
        artifacts_dir.mkdir()
        artifacts = {}
        for name in build_helper.ARTIFACT_NAMES:
            data = ("artifact-" + name).encode()
            path = artifacts_dir / name
            path.write_bytes(data)
            artifacts[name] = {"path": str(path), "sha256": sha(data), "bytes": len(data)}
        manifest = {"schema": build_helper.MANIFEST_SCHEMA,
                    "builtBy": {"dotnet": str(self.dotnet), "sha256": sha(b"fake-dotnet"),
                                "version": "6.0.0"},
                    "sources": {}, "artifacts": artifacts}
        manifest_path = self.root / "provider-artifacts.json"
        manifest_path.write_bytes(oi.json_bytes(manifest))
        oi.make_private_file(manifest_path)

        sdk = self.root / "sdb.exe"
        sdk.write_bytes(b"sdk")
        openssl = self.root / ("openssl.exe" if os.name == "nt" else "openssl")
        openssl.write_bytes(b"openssl")
        config_doc = {
            "schema": pconfig.CONFIG_SCHEMA,
            "target": TARGET, "callbackHost": CALLBACK, "expectedSdkDuid": DUID,
            "sdk": {"executable": str(sdk), "sha256": sha(b"sdk")},
            "rootPayloads": {
                "archiveRootDll": {"path": artifacts["SamsungTvArchiveRoot.dll"]["path"],
                                   "sha256": artifacts["SamsungTvArchiveRoot.dll"]["sha256"]},
                "rootAgentDll": {"path": artifacts["SamsungTvRootAgent.dll"]["path"],
                                 "sha256": artifacts["SamsungTvRootAgent.dll"]["sha256"]}},
            "openssl": {"executable": str(openssl), "sha256": sha(b"openssl")},
            "timeouts": {"sdkSeconds": 30, "probeSeconds": 120,
                         "commandSeconds": 30, "nativeSeconds": 60,
                         "reportAttempts": 2},
        }
        config_path = self.root / "provider.json"
        config_path.write_bytes(json.dumps(config_doc).encode())
        oi.make_private_file(config_path)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = build_helper.main(["--check", str(manifest_path),
                                      "--provider-config", str(config_path)])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(stdout.getvalue())["providerConfigPayloadsMatch"])


if __name__ == "__main__":
    unittest.main()

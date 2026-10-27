"""Public native host source (launcher/host): seam, fail-closed defaults, offline build.

Static checks run everywhere. The build checks need the pinned .NET SDK 6.0.428;
point QUIETTV_DOTNET at its dotnet.exe (or have it on PATH as `dotnet`). Without
it those tests are reported as skipped, never as passed.

The guard-off tests copy the source under launcher/host/bin (ignored), remove one
guard, rebuild, and require the self-test to FAIL on the claim that guard protects:
the self-test is shown to catch the regression, not just to pass.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOST = REPO / "launcher" / "host"
SCRIPT = REPO / "tools" / "build-public-host.ps1"
SOURCES = ["Canvas.cs", "HostConfig.cs", "HostEffects.cs", "IptvSession.cs", "Native.cs",
           "Program.cs", "RemoteGate.cs", "SelfTest.cs", "State.cs", "WebFrontend.cs"]
UPSTREAM = "4ad2c4d3b0d0e0da4b5b7ebe4a787e94596f416b"
OLD_ROOT = "/home/owner/share/tmp/sdk_tools/quiet-tv"
BUILD_FILES = ["QuietTVHost.csproj", "NuGet.Config", "global.json"]


def _dotnet() -> str | None:
    candidate = os.environ.get("QUIETTV_DOTNET") or shutil.which("dotnet")
    if not candidate:
        return None
    try:
        out = subprocess.run([candidate, "--list-sdks"], capture_output=True, text=True, timeout=60)
    except OSError:
        return None
    return candidate if "6.0.428" in out.stdout else None


DOTNET = _dotnet()
PWSH = shutil.which("pwsh") or shutil.which("powershell")


def _build(project_dir: Path = HOST, out_name: str = "release") -> subprocess.CompletedProcess:
    env = dict(os.environ, QUIETTV_DOTNET=DOTNET or "")
    return subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File", str(SCRIPT),
                           "-ProjectDir", str(project_dir), "-OutName", out_name],
                          capture_output=True, text=True, env=env, timeout=600)


def _code(name: str) -> str:
    return (HOST / name).read_text(encoding="utf-8")


class SourceShape(unittest.TestCase):
    def test_expected_files_only(self):
        tracked = {p.name for p in HOST.iterdir() if p.is_file()}
        expected = set(SOURCES) | set(BUILD_FILES) | {".gitignore",
                                   "README.md", "NOTICE.md", "host-config.example.json"}
        self.assertEqual(tracked, expected)

    def test_no_hardcoded_device_values_outside_tests(self):
        for name in SOURCES:
            if name == "SelfTest.cs":
                continue
            code = _code(name)
            # State.Tests holds synthetic fixtures; strip it before scanning.
            code = re.split(r"public static void Tests\(", code)[0] if name == "State.cs" else code
            for literal in (OLD_ROOT, "sdk_tools", "QtHome2026", '"5001"', "/run/user/5001",
                            "wt61p807", "Smart Control", "/dev/input/event2", "/dev/input/event4",
                            "9Ur5IzDKqV.TizenYouTube\",\"EYm8vc1St4"):
                self.assertNotIn(literal, code, f"{literal} hardcoded in {name}")
            self.assertNotIn("const string Root", code, name)

    def test_no_shell_or_environment_config(self):
        for name in SOURCES:
            code = _code(name)
            self.assertNotIn("UseShellExecute=true", code.replace(" ", ""), name)
            self.assertNotIn("GetEnvironmentVariable", code, name)
            if name != "HostEffects.cs":  # SystemProcessStarter is the single spawn point
                self.assertNotRegex(code, r"Process\.Start\(", f"{name} must spawn through IProcessStarter")
        self.assertNotIn("Directory.CreateDirectory", _code("Program.cs"))

    def test_main_validates_before_writes_and_native_calls(self):
        main = _code("Program.cs").split("static int Main(", 1)[1]
        load = main.index("OwnerData.Load(")
        for effect in ("File.WriteAllText", "new Canvas(", "NativeLibrary.Load", "HostStartup.Run("):
            self.assertLess(load, main.index(effect), f"{effect} runs before validation")

    def test_native_effects_only_reached_through_the_seams(self):
        # Complements the self-test seam proofs: Program must not bypass HostStartup/LaunchGate.
        program = _code("Program.cs")
        effects = program.split("sealed class NativeHostEffects", 1)[1].split("internal static string ConfigPath", 1)[0]
        outside = program.replace(effects, "")
        self.assertNotIn("new RemoteGate(", outside)
        self.assertEqual(outside.count("SnapshotStart"), 2, "SnapshotStart: field + HostStartup.Run argument only")
        self.assertIn("HostStartup.Run(config,flags,new NativeHostEffects(),Heartbeat,SnapshotStart,hidden)", outside)
        tick = outside.split("static int Tick(", 1)[1].split("static int CheckLaunch(", 1)[0]
        self.assertEqual(outside.count("new WebFrontend("), 1, "WebFrontend: StartWebUi + Tick restart only")
        self.assertEqual(tick.count("new WebFrontend("), 1)
        self.assertEqual(outside.count("iptv.Start()"), 1)
        self.assertIn("if(pendingRoute==LaunchRoute.Iptv){iptv.Start();", outside)
        launch = outside.split("static bool LaunchApp(", 1)[1].split("static void StopApp(", 1)[0]
        self.assertIn("LaunchGate.Route(", launch)
        self.assertLess(launch.index("LaunchGate.Route("), launch.index("StopApp("))
        for call in re.findall(r"LaunchApp\([^)]*\)", outside.split("static bool LaunchApp(", 1)[0] + tick):
            self.assertNotIn("quiet.iptv", call)

    def test_session_files_only_through_the_seam(self):
        # IptvSession/WebFrontend never touch the disk directly; the self-test never builds a disk seam.
        for name in ("IptvSession.cs", "WebFrontend.cs"):
            code = _code(name)
            for direct in ("File.", "FileInfo", "Directory."):
                self.assertNotIn(direct, code, f"{name} uses {direct} directly")
            self.assertIn("ISessionFiles files", code)
        self_test = _code("SelfTest.cs")
        for direct in ("File.", "FileInfo", "Directory.", "DiskSessionFiles", "DiskOwnerFiles", "LoadFile(\""):
            self.assertNotIn(direct, self_test)
        self.assertNotIn("selftest-absent", self_test, "no 'path cannot exist' assumption")
        for name in SOURCES:
            if name != "Program.cs":
                self.assertNotIn("new DiskSessionFiles(", _code(name), name)
        main = _code("Program.cs").split("static int Main(", 1)[1]
        self.assertLess(main.index("SelfTest.Run()"), main.index("HostConfig.LoadFile("))

    def test_attribution_preserved(self):
        self.assertIn(UPSTREAM, _code("RemoteGate.cs"))
        self.assertIn("Unlicense", _code("RemoteGate.cs"))
        notice = _code("NOTICE.md")
        self.assertIn(UPSTREAM, notice)
        self.assertIn("payloads/common/SamsungTvRemoteInputAgent.cs", notice)
        self.assertIn(UPSTREAM, (REPO / "THIRD-PARTY-NOTICES.md").read_text(encoding="utf-8"))

    def test_project_is_reproducible_and_offline(self):
        proj = _code("QuietTVHost.csproj")
        for setting in ("<TargetFramework>net6.0</TargetFramework>", "<UseAppHost>false</UseAppHost>",
                        "<Deterministic>true</Deterministic>",
                        "<ContinuousIntegrationBuild>true</ContinuousIntegrationBuild>", "<PathMap>"):
            self.assertIn(setting, proj)
        self.assertNotIn("PackageReference", proj)
        self.assertIn("<clear />", _code("NuGet.Config"))

    def test_example_config_holds_placeholders_and_safe_defaults(self):
        example = json.loads(_code("host-config.example.json"))
        self.assertIn("<", example["runtimeRoot"])
        for flag in ("remoteGrabEnabled", "iptvEnabled", "webUiEnabled", "snapshotEnabled"):
            self.assertIs(example[flag], False)

    def test_readme_names_what_is_missing_and_claims_no_install(self):
        readme = _code("README.md").lower()
        for part in ("service", "widget", "config generator", "root", "signing", "firmware"):
            self.assertIn(part, readme)
        self.assertIn("not a consumer install", readme)


@unittest.skipUnless(DOTNET and PWSH, "needs pwsh and .NET SDK 6.0.428 (set QUIETTV_DOTNET)")
class OfflineBuild(unittest.TestCase):
    dll: Path

    @classmethod
    def setUpClass(cls):
        cls.result = _build()
        cls.dll = HOST / "bin" / "release" / "QuietTV.dll"

    @classmethod
    def tearDownClass(cls):
        for extra in ("repro", "scratch"):
            shutil.rmtree(HOST / "bin" / extra, ignore_errors=True)

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([DOTNET, str(self.dll), *args], capture_output=True, text=True, timeout=120)

    def test_build_and_self_test_pass(self):
        self.assertEqual(self.result.returncode, 0, self.result.stdout + self.result.stderr)
        out = self.result.stdout
        for line in ("PASS 37 state assertions", "PASS 15 remote forwarding and device match assertions",
                     "PASS 11 web action assertions", "PASS 9 IPTV readiness and process identity assertions",
                     "SELF-TEST PASS", "BUILD-PUBLIC-HOST OK"):
            self.assertIn(line, out)
        self.assertRegex(out, r"PASS \d{3,} host seam assertions")
        self.assertFalse((HOST / "obj").exists(), "intermediate caches left behind")

    def test_output_has_no_absolute_host_paths(self):
        needles = {str(REPO), str(REPO).replace("\\", "/"), "C:\\Users", "C:/Users"}
        for path in (HOST / "bin" / "release").rglob("*"):
            if not path.is_file():
                continue
            data = path.read_bytes().lower()
            for n in needles:
                for enc in ("utf-8", "utf-16-le"):
                    self.assertNotIn(n.lower().encode(enc), data, f"{n} in {path.name}")

    def test_reproducible_bytes(self):
        second = _build(out_name="repro")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        a = (HOST / "bin" / "release" / "QuietTV.dll").read_bytes()
        b = (HOST / "bin" / "repro" / "QuietTV.dll").read_bytes()
        self.assertEqual(a, b, "two clean builds differ")

    def test_reproducible_from_fresh_crlf_copy(self):
        # A checkout elsewhere with CRLF line endings must still give identical bytes.
        copy = HOST / "bin" / "fresh-copy"
        shutil.rmtree(copy, ignore_errors=True)
        copy.mkdir(parents=True)
        for item in SOURCES + BUILD_FILES:
            text = (HOST / item).read_text(encoding="utf-8").replace("\r\n", "\n").replace("\n", "\r\n")
            (copy / item).write_bytes(text.encode("utf-8"))
        r = _build(copy, "fresh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        fresh = (copy / "bin" / "fresh" / "QuietTV.dll").read_bytes()
        shutil.rmtree(copy)
        self.assertEqual(fresh, self.dll.read_bytes(), "CRLF copy in another folder built different bytes")

    def test_missing_config_fails_cleanly(self):
        r = self._run()
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("config_rejected=--config PATH is required", r.stdout)
        self.assertNotIn("fatal=", r.stdout)

    def test_example_config_rejected_unchanged(self):
        r = self._run("--config", str(HOST / "host-config.example.json"))
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("config_rejected=", r.stdout)

    def test_valid_config_without_owner_files_fails_before_any_write(self):
        # On Windows the POSIX root maps to <drive>:\home\owner\...; it must stay uncreated.
        scratch = HOST / "bin" / "scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        cfg = scratch / "config.json"
        cfg.write_text(json.dumps({"schema": "quiettv-host-config/1",
                                   "runtimeRoot": "/home/owner/share/selftest-missing/quiet-tv",
                                   "ownerUid": 5001, "ownAppId": "TestPkg001.QuietTV", "homeKeyCode": 63,
                                   "observedInputPaths": ["/dev/input/event2"]}), encoding="utf-8")
        mapped = Path(os.path.abspath("/home/owner/share/selftest-missing"))
        self.assertFalse(mapped.exists())
        r = self._run("--config", str(cfg))
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("config_rejected=apps.json missing", r.stdout)
        self.assertNotIn("fatal=", r.stdout)
        self.assertFalse(mapped.exists(), "host created the runtime root")


def _script(project_dir: str, out_name: str = "release") -> subprocess.CompletedProcess:
    """Run the build script with raw (unnormalised) arguments."""
    env = dict(os.environ, QUIETTV_DOTNET=DOTNET or "")
    r = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File", str(SCRIPT),
                        "-ProjectDir", project_dir, "-OutName", out_name],
                       capture_output=True, text=True, env=env, timeout=600)
    r.output = r.stdout + r.stderr  # type: ignore[attr-defined]
    return r


def _make_junction(link: Path, target: Path) -> bool:
    if os.name != "nt":
        return False
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
    return r.returncode == 0 and link.exists()


def _remove_junction(link: Path) -> None:
    # os.rmdir removes a junction itself without following it.
    if os.path.lexists(link):
        os.rmdir(link)


@unittest.skipUnless(PWSH, "needs pwsh")
class BuildScriptCleanupGuards(unittest.TestCase):
    """The script's recursive deletes must never reach outside launcher/host's own obj/ and bin/<OutName>."""

    def setUp(self):
        import tempfile
        self.outside = Path(tempfile.mkdtemp(prefix="quiettv-sentinel-"))
        self.sentinels: list[Path] = []
        self.junctions: list[Path] = []
        self.copies: list[Path] = []

    def tearDown(self):
        for link in self.junctions:
            _remove_junction(link)
        for copy in self.copies:
            shutil.rmtree(copy, ignore_errors=True)
        shutil.rmtree(self.outside, ignore_errors=True)

    def _sentinel(self, folder: Path) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        s = folder / "SENTINEL.txt"
        s.write_text("must survive", encoding="utf-8")
        self.sentinels.append(s)
        return s

    def _fake_project(self, folder: Path) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "QuietTVHost.csproj").write_text("<Project />", encoding="utf-8")

    def _assert_refused(self, r, message: str):
        self.assertNotEqual(r.returncode, 0, r.output)
        self.assertIn(message, r.output)
        self.assertNotIn("BUILD-PUBLIC-HOST OK", r.output)
        for s in self.sentinels:
            self.assertTrue(s.exists(), f"sentinel deleted: {s}")
            self.assertEqual(s.read_text(encoding="utf-8"), "must survive")

    def _copy_dir(self, name: str) -> Path:
        copy = HOST / "bin" / name
        shutil.rmtree(copy, ignore_errors=True)
        self.copies.append(copy)
        return copy

    def test_outname_traversal_rejected(self):
        target = self.outside / "out-target"
        self._sentinel(target)
        rel = os.path.relpath(target, HOST / "bin")
        for name in (rel, str(target), "..", "../release", "a/b", "release/.."):
            with self.subTest(out_name=name):
                self._assert_refused(_script(str(HOST), name), "OutName must be a simple folder name")

    def test_projectdir_traversal_rejected(self):
        self._fake_project(self.outside)
        self._sentinel(self.outside / "obj")
        self._sentinel(self.outside / "bin" / "release")
        via_bin = str(HOST / "bin" / "x") + os.sep + os.path.relpath(self.outside, HOST / "bin" / "x")
        for project in (str(self.outside), via_bin, str(REPO / "tools"), str(REPO),
                        str(HOST / "bin"), str(HOST / "bin" / ".." / ".." / "host-other")):
            with self.subTest(project=project):
                self._assert_refused(_script(project), "ProjectDir must be launcher/host or a copy")

    def _junction_or_skip(self, link: Path, target: Path):
        if not _make_junction(link, target):
            self.skipTest("cannot create a directory junction on this platform/account")
        self.junctions.append(link)

    def test_junctioned_project_dir_refused(self):
        self._fake_project(self.outside)
        sentinel = self._sentinel(self.outside / "obj")
        link = self._copy_dir("junction-project")
        self._junction_or_skip(link, self.outside)
        self.assertTrue((link / "obj" / sentinel.name).exists(), "positive control: junction redirects outside")
        self._assert_refused(_script(str(link)), "reparse point")

    def test_junctioned_obj_refused(self):
        copy = self._copy_dir("junction-obj")
        self._fake_project(copy)
        sentinel = self._sentinel(self.outside / "obj-target")
        self._junction_or_skip(copy / "obj", self.outside / "obj-target")
        self.assertTrue((copy / "obj" / sentinel.name).exists(), "positive control: junction redirects outside")
        self._assert_refused(_script(str(copy)), "reparse point")

    def test_junctioned_output_refused(self):
        copy = self._copy_dir("junction-out")
        self._fake_project(copy)
        (copy / "bin").mkdir()
        sentinel = self._sentinel(self.outside / "out-target")
        self._junction_or_skip(copy / "bin" / "release", self.outside / "out-target")
        self.assertTrue((copy / "bin" / "release" / sentinel.name).exists(), "positive control")
        self._assert_refused(_script(str(copy)), "reparse point")

    def test_junction_inside_obj_refused(self):
        copy = self._copy_dir("junction-inner")
        self._fake_project(copy)
        (copy / "obj").mkdir()
        sentinel = self._sentinel(self.outside / "inner-target")
        self._junction_or_skip(copy / "obj" / "escape", self.outside / "inner-target")
        self.assertTrue((copy / "obj" / "escape" / sentinel.name).exists(), "positive control")
        self._assert_refused(_script(str(copy)), "reparse point inside")

    def test_junctioned_bin_ancestor_refused(self):
        # A junction higher up (bin/<copy>/nested) redirects everything below it.
        copy = self._copy_dir("junction-ancestor")
        copy.mkdir()
        self._fake_project(self.outside / "proj")
        sentinel = self._sentinel(self.outside / "proj" / "obj")
        self._junction_or_skip(copy / "nested", self.outside / "proj")
        self.assertTrue((copy / "nested" / "obj" / sentinel.name).exists(), "positive control")
        self._assert_refused(_script(str(copy / "nested")), "reparse point")


class SdkPin(unittest.TestCase):
    """launcher/host/global.json pins SDK 6.0.428 and every dotnet command runs from ProjectDir.

    Limitation: this machine has exactly one SDK (6.0.428), so "SDK 6 plus a newer
    SDK" cannot be reproduced here. Instead the tests show (a) the script runs every
    dotnet command with ProjectDir as working directory even when called from the repo
    root, and (b) the real dotnet host reads that global.json and honours
    rollForward:disable (a pin one patch below the installed SDK fails, while the same
    pin with latestPatch rolls to 6.0.428).
    """

    def test_global_json_is_exact_pin(self):
        pin = json.loads(_code("global.json"))
        self.assertEqual(pin, {"sdk": {"version": "6.0.428", "rollForward": "disable", "allowPrerelease": False}})

    def _copy(self, name: str, pin: dict | None = None) -> Path:
        copy = HOST / "bin" / name
        shutil.rmtree(copy, ignore_errors=True)
        copy.mkdir(parents=True)
        for item in SOURCES + BUILD_FILES:
            shutil.copy2(HOST / item, copy / item)
        if pin is not None:
            (copy / "global.json").write_text(json.dumps({"sdk": pin}), encoding="utf-8")
        self.addCleanup(shutil.rmtree, copy, True)
        return copy

    @unittest.skipUnless(PWSH and os.name == "nt", "needs pwsh on Windows (cmd shim)")
    def test_dotnet_commands_run_in_project_dir_and_caller_state_is_restored(self):
        import tempfile
        shim_dir = Path(tempfile.mkdtemp(prefix="quiettv-shim-"))
        self.addCleanup(shutil.rmtree, shim_dir, True)
        log = shim_dir / "calls.log"
        shim = shim_dir / "dotnet.cmd"
        shim.write_text('@echo off\r\n>>"%QUIETTV_SHIM_LOG%" echo %CD%^|%*\r\n'
                        'if "%1"=="--version" (echo 6.0.428& exit /b 0)\r\nexit /b 7\r\n', encoding="ascii")
        copy = self._copy("shim-copy")
        command = (f"$env:QUIETTV_SHIM_LOG='{log}'; $env:DOTNET_CLI_HOME='caller-value'; Set-Location -LiteralPath '{REPO}'; "
                   f"try {{ & '{SCRIPT}' -Dotnet '{shim}' -ProjectDir '{copy}' -OutName shim }} catch {{ 'CAUGHT=' + $_ }}; "
                   "'AFTER=' + (Get-Location).Path; 'ENV=' + $env:DOTNET_CLI_HOME")
        r = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", command],
                           capture_output=True, text=True, timeout=300)
        out = r.stdout + r.stderr
        self.assertIn("CAUGHT=restore failed (7)", out)
        self.assertIn(f"AFTER={REPO}", out, "caller location not restored after a failed build")
        self.assertIn("ENV=caller-value", out, "caller environment not restored")
        calls = [line.split("|", 1) for line in log.read_text(encoding="ascii").splitlines()]
        self.assertEqual([c[1].split()[0] for c in calls], ["--version", "restore"])
        for cwd, _ in calls:
            self.assertEqual(os.path.normcase(cwd.strip()), os.path.normcase(str(copy)), "dotnet ran outside ProjectDir")

    @unittest.skipUnless(PWSH, "needs pwsh")
    def test_script_refuses_missing_or_loose_pin(self):
        for name, mutate in (("pin-missing", None), ("pin-loose", {"version": "6.0.428", "rollForward": "latestMajor", "allowPrerelease": False}),
                             ("pin-prerelease", {"version": "6.0.428", "rollForward": "disable", "allowPrerelease": True})):
            with self.subTest(case=name):
                copy = self._copy(name, mutate)
                if mutate is None:
                    (copy / "global.json").unlink()
                r = _script(str(copy), "pin")
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("global.json", r.output)
                self.assertNotIn("BUILD-PUBLIC-HOST OK", r.output)

    @unittest.skipUnless(DOTNET, "needs .NET SDK 6.0.428 (set QUIETTV_DOTNET)")
    def test_real_host_honours_pin_from_project_dir(self):
        def version(cwd: Path) -> subprocess.CompletedProcess:
            return subprocess.run([DOTNET, "--version"], cwd=cwd, capture_output=True, text=True, timeout=60)
        self.assertEqual(version(HOST).stdout.strip(), "6.0.428")
        strict = self._copy("pin-strict", {"version": "6.0.427", "rollForward": "disable", "allowPrerelease": False})
        loose = self._copy("pin-latestpatch", {"version": "6.0.427", "rollForward": "latestPatch", "allowPrerelease": False})
        self.assertNotEqual(version(strict).returncode, 0, "rollForward:disable was not honoured")
        self.assertEqual(version(loose).stdout.strip(), "6.0.428", "positive control: latestPatch rolls forward")
        # Combined with the shim test (every dotnet call runs with cwd = ProjectDir), this shows the
        # script's commands resolve the SDK through launcher/host/global.json.


GUARDS = {
    # name: (file, exact guard text, mutated text, self-test claim that must fail)
    "iptv-start": ("IptvSession.cs",
                   'if(!config.IptvEnabled)throw new InvalidOperationException("IPTV is disabled by the host config");',
                   "", "IPTV off: QuietIPTV.dll is never started"),
    "iptv-route": ("HostEffects.cs", "return config.IptvEnabled?LaunchRoute.Iptv:LaunchRoute.Rejected;",
                   "return LaunchRoute.Iptv;", "IPTV off rejects quiet.iptv even when the catalogue lists it"),
    "snapshot": ("HostEffects.cs", "if(config.SnapshotEnabled&&!flags.QuietStart)", "if(!flags.QuietStart)",
                 "defaults schedule no snapshot timer"),
    "remote-grab": ("HostEffects.cs", "bool grab=!flags.SystemHome&&config.RemoteGrabEnabled;",
                    "bool grab=!flags.SystemHome;", "defaults never grab the remote"),
    "webui": ("HostEffects.cs", "if(config.WebUiEnabled&&config.IptvEnabled)fx.StartWebUi(startHidden);",
              "if(true)fx.StartWebUi(startHidden);", "--web-ui alone does not start the web viewer"),
    # Reproduces the reviewed bypass: web viewer (QuietIPTV.dll) started with IPTV off.
    "webui-needs-iptv": ("HostEffects.cs", "if(config.WebUiEnabled&&config.IptvEnabled)fx.StartWebUi(startHidden);",
                         "if(config.WebUiEnabled)fx.StartWebUi(startHidden);",
                         "IPTV off + Web UI on: startup does not start the web viewer"),
    "webui-start": ("WebFrontend.cs", "if(!config.WebUiEnabled||!config.IptvEnabled)throw",
                    "if(false)throw", "Web UI off: QuietIPTV.dll --dashboard is never started"),
    "webui-start-needs-iptv": ("WebFrontend.cs", "if(!config.WebUiEnabled||!config.IptvEnabled)throw",
                               "if(!config.WebUiEnabled)throw",
                               "IPTV off + Web UI on: QuietIPTV.dll --dashboard is never started"),
    # Reproduces the reviewed self-test hazard: stale-session cleanup on the real disk.
    "iptv-cleanup-on-disk": ("IptvSession.cs", 'if(files.Exists(Root+"/"+name))files.Delete(Root+"/"+name);',
                             'if(File.Exists(Root+"/"+name))File.Delete(Root+"/"+name);',
                             "stale-session cleanup goes through the session-file seam (memory only)"),
    "web-cleanup-on-disk": ("WebFrontend.cs", 'if(files.Exists(Root+"/"+name))files.Delete(Root+"/"+name);',
                            'if(File.Exists(Root+"/"+name))File.Delete(Root+"/"+name);',
                            "stale-session cleanup goes through the session-file seam (memory only)"),
    "inputs-tile": ("HostEffects.cs", "if(id==InputsTileId)return LaunchRoute.Rejected;", "",
                    "synthetic Inputs tile id is refused even if the catalogue lists it"),
}


@unittest.skipUnless(DOTNET and PWSH, "needs pwsh and .NET SDK 6.0.428 (set QUIETTV_DOTNET)")
class GuardOffRegression(unittest.TestCase):
    def test_each_guard_removed_makes_self_test_fail(self):
        for name, (file, guard, mutant, claim) in GUARDS.items():
            with self.subTest(guard=name):
                source = _code(file)
                self.assertEqual(source.count(guard), 1, f"guard text for {name} not found exactly once")
                copy = HOST / "bin" / "mutants" / name
                if copy.exists():
                    shutil.rmtree(copy)
                copy.mkdir(parents=True)
                for item in SOURCES + BUILD_FILES:
                    shutil.copy2(HOST / item, copy / item)
                (copy / file).write_text(source.replace(guard, mutant), encoding="utf-8")
                r = _build(copy, "mutant")
                self.assertIn("Build succeeded", r.stdout, f"mutant {name} must compile; only the self-test may fail")
                self.assertNotEqual(r.returncode, 0, f"self-test still passed with {name} guard removed")
                self.assertIn("FAIL " + claim, r.stdout, r.stdout[-2000:])
                shutil.rmtree(copy)
        shutil.rmtree(HOST / "bin" / "mutants", ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

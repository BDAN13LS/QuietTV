"""Read-only TV inventory collector (tools/tv-inspect): source guards, offline build, guard-off proofs.

Static checks run everywhere. Build checks need the pinned .NET SDK 6.0.428: point
QUIETTV_DOTNET at its dotnet.exe (or have it on PATH as `dotnet`). Without it those tests
are reported as skipped, never as passed.

The guard-off tests copy the source under tools/tv-inspect/bin (ignored), remove one
guard, rebuild, and require the NAMED self-test check to FAIL: each self-test is shown to
catch the defect it claims to, not just to pass. Nothing here touches a TV or the network.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TOOL = REPO / "tools" / "tv-inspect"
SOURCES = ["Program.cs", "Inspector.cs", "SelfTests.cs"]
BUILD_FILES = ["QuietTVInspect.csproj", "NuGet.Config", "global.json"]
OWNED = sorted(SOURCES + BUILD_FILES + ["README.md", ".gitignore", "frame.py"])
DLL = "QuietTVInspect.dll"
FRAME_PATH = TOOL / "frame.py"


def _load_frame(replacements=(), case: unittest.TestCase | None = None):
    """frame.py as a fresh in-memory module, optionally with guards replaced (each must be unique)."""
    source = FRAME_PATH.read_text(encoding="utf-8")
    for old, new in replacements:
        count = source.count(old)
        if case is not None:
            case.assertEqual(count, 1, f"frame guard snippet not unique: {old!r}")
        elif count != 1:
            raise AssertionError(f"frame guard snippet not unique: {old!r}")
        source = source.replace(old, new)
    module = types.ModuleType("quiettv_frame_variant" if replacements else "quiettv_frame")
    module.__file__ = str(FRAME_PATH)
    exec(compile(source, str(FRAME_PATH), "exec"), module.__dict__)
    return module


FRAME = _load_frame()
# Synthetic shapes of Samsung's runtime GC lines (from the brief, not a captured log).
_GC_START = b"[GC_INFO] GC_Start #1: Gen(0), Time(10 ms), Reason(0)"
_GC_END = b"[GC_INFO] GC_End #1: Gen(0), Time(11 ms)"

# ---- owned cleanup ------------------------------------------------------------------------
# Every recursive delete in this file goes through _remove_owned. A target is accepted only
# when its lexical absolute path is one of the generated folders below, nothing from it up to
# the repository is a junction/symlink/reparse point (checked with lstat BEFORE any resolve,
# which would lose that information), its resolved path is where the lexical path says, and
# nothing inside it is a reparse point. Generated namespace, relative to the tool folder:
#   obj | bin/<name> | bin/<name>/obj | bin/<name>/bin/<name>
# The tool folder itself, its bin/ and every source file are never targets.

_SIMPLE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_RMTREE = shutil.rmtree  # indirection so the guard tests can record instead of delete


class CleanupRefused(Exception):
    def __init__(self, reason: str, path: str):
        super().__init__(f"refusing cleanup ({reason}): {path}")
        self.reason = reason


def _norm(path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _is_reparse(path: str) -> bool:
    """True for a symlink, junction or other reparse point. lstat never follows the link."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & _REPARSE)


def _in_generated_namespace(lexical: str, tool: str) -> bool:
    try:
        rel = os.path.relpath(lexical, tool)
    except ValueError:  # another drive
        return False
    parts = rel.split(os.sep)
    simple = all(_SIMPLE.match(p) for p in parts)
    return simple and (parts == ["obj"]
                       or (len(parts) == 2 and parts[0] == "bin")
                       or (len(parts) == 3 and parts[0] == "bin" and parts[2] == "obj")
                       or (len(parts) == 4 and parts[0] == "bin" and parts[2] == "bin"))


def _refuse_reparse_chain(lexical: str, repo: str) -> None:
    p = lexical
    while True:
        if _is_reparse(p):
            raise CleanupRefused("reparse-point", p)
        if p == repo:
            return
        parent = os.path.dirname(p)
        if parent == p:
            raise CleanupRefused("outside-repository", lexical)
        p = parent


def _refuse_resolved_escape(lexical: str, tool: str) -> None:
    expected = os.path.normcase(os.path.normpath(os.path.join(os.path.realpath(tool), os.path.relpath(lexical, tool))))
    if os.path.normcase(os.path.realpath(lexical)) != expected:
        raise CleanupRefused("resolved-escape", lexical)


def _refuse_reparse_inside(lexical: str) -> None:
    for dirpath, dirnames, filenames in os.walk(lexical, followlinks=False):
        for name in dirnames + filenames:
            if _is_reparse(os.path.join(dirpath, name)):
                raise CleanupRefused("reparse-point-inside", os.path.join(dirpath, name))


def _remove_owned(target, tool=TOOL, repo=REPO) -> None:
    raw = os.fspath(target)
    if any(part == ".." for part in re.split(r"[\\/]", raw)):
        raise CleanupRefused("traversal", raw)
    lexical, tool_n, repo_n = _norm(raw), _norm(tool), _norm(repo)
    if not _in_generated_namespace(lexical, tool_n):
        raise CleanupRefused("outside-generated-namespace", lexical)
    _refuse_reparse_chain(lexical, repo_n)
    _refuse_resolved_escape(lexical, tool_n)
    if not os.path.lexists(lexical):
        return
    if not os.path.isdir(lexical):
        raise CleanupRefused("not-a-directory", lexical)
    _refuse_reparse_inside(lexical)
    _RMTREE(lexical)

# ---- end of owned cleanup -----------------------------------------------------------------


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


def _env(project_dir: Path) -> dict:
    # Every SDK and NuGet cache stays inside the project's ignored obj/ folder.
    cache = project_dir / "obj" / ".cli"
    env = dict(os.environ)
    env.update({
        "DOTNET_CLI_HOME": str(cache),
        "NUGET_PACKAGES": str(cache / "packages"),
        "NUGET_HTTP_CACHE_PATH": str(cache / "http"),
        "NUGET_SCRATCH": str(cache / "scratch"),
        "DOTNET_MULTILEVEL_LOOKUP": "0",
        "DOTNET_NOLOGO": "1",
        "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
        "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1",
        "DOTNET_GENERATE_ASPNET_CERTIFICATE": "false",
        "MSBUILDDISABLENODEREUSE": "1",
    })
    return env


def _build(project_dir: Path, out_name: str) -> tuple[subprocess.CompletedProcess, Path]:
    """Restore and build with cwd = project_dir, so its global.json pins the SDK."""
    out = project_dir / "bin" / out_name
    _remove_owned(out)
    _remove_owned(project_dir / "obj")
    env = _env(project_dir)
    restore = subprocess.run([DOTNET, "restore", "QuietTVInspect.csproj", "--configfile", "NuGet.Config",
                              "-nodeReuse:false"], cwd=project_dir, capture_output=True, text=True,
                             env=env, timeout=600)
    if restore.returncode != 0:
        return restore, out / DLL
    build = subprocess.run([DOTNET, "build", "QuietTVInspect.csproj", "--no-restore", "-c", "Release",
                            "-o", str(out), "-nodeReuse:false", "-p:UseSharedCompilation=false"],
                           cwd=project_dir, capture_output=True, text=True, env=env, timeout=600)
    return build, out / DLL


def _run(dll: Path, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([DOTNET, str(dll), *args], capture_output=True, timeout=120, cwd=cwd)


def _copy(name: str, crlf: bool = False) -> Path:
    copy = TOOL / "bin" / name
    _remove_owned(copy)
    _refuse_reparse_chain(_norm(copy), _norm(REPO))  # never write sources through a link either
    copy.mkdir(parents=True)
    for item in SOURCES + BUILD_FILES:
        text = (TOOL / item).read_text(encoding="utf-8")
        if crlf:
            text = text.replace("\r\n", "\n").replace("\n", "\r\n")
        (copy / item).write_bytes(text.encode("utf-8"))
    return copy


def _strict_json(data: bytes):
    def pairs(items):
        keys = [k for k, _ in items]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate JSON key")
        return dict(items)

    def constant(value):
        raise ValueError(f"non-standard JSON constant {value}")

    return json.loads(data.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)


def _code(name: str) -> str:
    return (TOOL / name).read_text(encoding="utf-8")


class SourceShape(unittest.TestCase):
    def test_owned_files_only(self):
        present = sorted(p.name for p in TOOL.iterdir() if p.is_file())
        self.assertEqual(present, OWNED)

    def test_project_is_pure_bcl_reproducible_net6(self):
        proj = _code("QuietTVInspect.csproj")
        for needle in ("<TargetFramework>net6.0</TargetFramework>", "<UseAppHost>false</UseAppHost>",
                       "<Deterministic>true</Deterministic>", "<DebugType>none</DebugType>",
                       "<DebugSymbols>false</DebugSymbols>", "<PathMap>$(MSBuildProjectDirectory)=/_/tv-inspect/</PathMap>",
                       "<ContinuousIntegrationBuild>true</ContinuousIntegrationBuild>",
                       "<InvariantGlobalization>true</InvariantGlobalization>", "<Platforms>AnyCPU</Platforms>",
                       "<EnableDefaultCompileItems>false</EnableDefaultCompileItems>"):
            self.assertIn(needle, proj)
        for banned in ("PackageReference", "RuntimeIdentifier", "AllowUnsafeBlocks", "PublishAot", "NativeLibrary"):
            self.assertNotIn(banned, proj)
        self.assertEqual(json.loads(_code("global.json")),
                         {"sdk": {"version": "6.0.428", "rollForward": "disable", "allowPrerelease": False}})
        self.assertIn("<clear />", _code("NuGet.Config"))
        self.assertNotIn("<add ", _code("NuGet.Config"))

    def test_no_write_native_network_or_shell_surface(self):
        banned = [
            r"DllImport", r"LibraryImport", r"\bunsafe\s*\{", r"System\.Net", r"HttpClient", r"Socket",
            r"UseShellExecute\s*=\s*true", r"\.Arguments\s*=", r"File\.(Write|Append|Delete|Move|Copy|Create|Replace|SetAttributes)",
            r"Directory\.(Create|Delete|Move|EnumerateFileSystemEntries|GetFileSystemEntries|EnumerateFiles|GetFiles)",
            r"FileMode\.(Create|CreateNew|Append|OpenOrCreate|Truncate)", r"FileAccess\.(Write|ReadWrite)",
            r"ZipFile", r"TarFile", r"GZipStream", r"Environment\.GetEnvironmentVariable", r"Environment\.SetEnvironmentVariable",
            r"ResolveLinkTarget", r"Assembly\.Load", r"Process\.GetProcesses", r"Process\.GetProcessById",
        ]
        for name in ("Program.cs", "Inspector.cs"):
            code = _code(name)
            for pattern in banned:
                self.assertIsNone(re.search(pattern, code), f"{pattern} in {name}")

    def test_process_adapter_is_fixed(self):
        program = _code("Program.cs")
        self.assertIn("UseShellExecute = false", program)
        self.assertIn("info.ArgumentList.Add(arg)", program)
        self.assertIn("info.Environment.Clear()", program)
        self.assertIn("process.Kill(entireProcessTree: true)", program)
        self.assertEqual(program.count("new ProcessStartInfo("), 1)
        self.assertEqual(program.count("Process.Start("), 1)
        # Executables appear only in the adapter's fixed map, never in the logic or tests.
        for exe in ('"/usr/bin/sqlite3"', '"/usr/bin/uname"', '"/bin/uname"'):
            self.assertEqual(program.count(exe), 1, exe)
            self.assertNotIn(exe, _code("Inspector.cs"))
        for flag in ('"-init", "/dev/null"', '"-readonly"', '"-batch"', '"-bail"'):
            self.assertIn(flag, program)
        # The CLI takes exactly one of three fixed flags and nothing else.
        self.assertIn('args.Length == 1 && args[0] == "--self-test"', program)
        self.assertIn('args.Length == 1 && args[0] == "--inventory";', program)
        self.assertIn('args.Length == 1 && args[0] == "--inventory-framed";', program)
        self.assertNotRegex(program, r"args\[1\]")
        # The framed bytes are one buffer written once.
        self.assertEqual(program.count("stdout.Write("), 1)
        self.assertNotIn("Console.Write(", program)
        self.assertNotIn("Console.Out", program)

    def test_frame_constants_match_decoder(self):
        code = _code("Inspector.cs")
        self.assertIn(f'Tag = "{FRAME.FRAME_TAG.decode()}";', code)
        self.assertIn(f'EndTag = "{FRAME.END_TAG.decode()}";', code)
        self.assertIn("internal const int MaxPayloadBytes = 8 << 20;", code)
        self.assertEqual(FRAME.MAX_PAYLOAD_BYTES, 8 << 20)
        self.assertIn(f'Schema = "{FRAME.SCHEMA}"', code)

    def test_decoder_schema_matches_renderer_literals(self):
        # A reason or problem code added on one side only would make real TV output refuse.
        code = _code("Inspector.cs")
        reasons = set(re.findall(r'new InstallVerdict\((?:true|false), [^,()]+, "([^"]+)"\)', code))
        self.assertEqual(reasons, FRAME._REASONS)
        codes = set(re.findall(r'new Problem\("([^"]+)"', code)) | set(re.findall(r'Fatal\("([^"]+)"', code))
        start = code.index("class StderrClassifier")
        classifier = code[start:code.index("\n}\n", start)]
        stderr_codes = set(re.findall(r'return "([^"]+)";', classifier))
        self.assertGreaterEqual(len(codes), 30)
        self.assertGreaterEqual(len(stderr_codes), 5)
        for c in sorted(codes | stderr_codes):
            self.assertRegex(c, FRAME._CODE, c)

    def test_frame_module_is_stdlib_library_only(self):
        source = FRAME_PATH.read_text(encoding="utf-8")
        imports = sorted(set(re.findall(r"(?m)^(?:from|import) ([A-Za-z_.]+)", source)))
        self.assertEqual(imports, ["__future__", "hashlib", "json", "re"])
        for banned in ("raw_decode", "open(", "subprocess", "os.", "sys.", "__main__", "errors=\"ignore\"",
                       "errors=\"replace\"", "eval(", "exec("):
            self.assertNotIn(banned, source)

    def test_every_recursive_delete_goes_through_the_guard(self):
        code = Path(__file__).read_text(encoding="utf-8")
        self.assertEqual(code.count("shutil." + "rmtree"), 1, "only the _RMTREE indirection may name rmtree")
        self.assertNotIn("ignore_" + "errors", code)
        self.assertEqual(code.count("_RMTREE(" + "lexical)"), 1)
        self.assertNotIn("subprocess.run([\"cmd\"", code)

    def test_queries_are_fixed_selects_on_known_tables(self):
        code = _code("Inspector.cs")
        for column in ("a.app_id", "a.app_component", "a.app_exec", "a.app_nodisplay", "a.app_disable",
                       "a.app_root_path", "a.package", "p.root_path", "p.package_disable", "p.package_version",
                       "app_locale", "app_label"):
            self.assertIn(f'Enc("{column}")', code)
        self.assertIn("FROM package_app_info a JOIN package_info p ON a.package = p.package", code)
        self.assertIn("FROM package_app_localized_info", code)
        self.assertIn('DatabasePath = "/opt/dbspace/.pkgmgr_parser.db"', code)
        self.assertIn('TizenReleasePath = "/etc/tizen-release"', code)
        for word in ("INSERT", "UPDATE ", "DELETE", "DROP", "ATTACH", "PRAGMA", "VACUUM", "ALTER"):
            self.assertNotIn(word, code)


@unittest.skipUnless(DOTNET, "needs .NET SDK 6.0.428 (set QUIETTV_DOTNET)")
class OfflineBuild(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result, cls.dll = _build(TOOL, "release")

    @classmethod
    def tearDownClass(cls):
        for extra in ("fresh-copy", "cli-scratch"):
            _remove_owned(TOOL / "bin" / extra)

    def test_build_and_self_test_pass(self):
        self.assertEqual(self.result.returncode, 0, self.result.stdout + self.result.stderr)
        r = _run(self.dll, "--self-test")
        out = r.stdout.decode("utf-8")
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("SELF-TEST PASS", out)
        self.assertNotIn("FAIL ", out)
        self.assertRegex(out, r"passed=(3\d|[4-9]\d) failed=0")

    def test_self_test_output_is_reproducible(self):
        a = _run(self.dll, "--self-test").stdout
        b = _run(self.dll, "--self-test").stdout
        self.assertEqual(a, b)

    def test_output_is_dll_only_and_has_no_host_paths(self):
        out = self.dll.parent
        names = sorted(p.name for p in out.iterdir())
        self.assertEqual(names, ["QuietTVInspect.deps.json", "QuietTVInspect.dll", "QuietTVInspect.runtimeconfig.json"])
        needles = {str(REPO), str(REPO).replace("\\", "/"), "C:\\Users", "C:/Users", "C:\\Dev", "C:/Dev"}
        for path in out.iterdir():
            data = path.read_bytes().lower()
            for n in needles:
                for enc in ("utf-8", "utf-16-le"):
                    self.assertNotIn(n.lower().encode(enc), data, f"{n} in {path.name}")

    def test_reproducible_from_fresh_crlf_copy(self):
        # A checkout elsewhere with CRLF line endings must build identical bytes (positive control too).
        copy = _copy("fresh-copy", crlf=True)
        r, dll = _build(copy, "fresh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(dll.read_bytes(), self.dll.read_bytes(), "CRLF copy in another folder built different bytes")
        st = _run(dll, "--self-test")
        self.assertEqual(st.returncode, 0, st.stdout.decode("utf-8"))

    def test_usage_errors(self):
        for args in ((), ("--inventory", "extra"), ("--inventory", "--self-test"), ("--sql", "SELECT 1"),
                     ("--path", "/etc/passwd"), ("--INVENTORY",), ("inventory",), ("--inventory-framed", "x"),
                     ("--inventory-framed", "--inventory"), ("--inventory-FRAMED",), ("--inventory-framed=1",)):
            r = _run(self.dll, *args)
            self.assertEqual(r.returncode, 2, args)
            self.assertEqual(r.stdout, b"", args)

    @unittest.skipUnless(os.name == "nt", "the refusal path is the Windows behaviour")
    def test_inventory_refuses_off_linux_without_touching_anything(self):
        drive = Path(os.path.abspath("/"))
        watched = [drive / "opt", drive / "usr", drive / "etc", drive / "bin"]
        before = {p: p.exists() for p in watched}
        scratch = TOOL / "bin" / "cli-scratch"
        _remove_owned(scratch)
        scratch.mkdir(parents=True)
        r = _run(self.dll, "--inventory", cwd=scratch)
        self.assertEqual(r.returncode, 2)
        doc = _strict_json(r.stdout)
        self.assertEqual(doc["schema"], "quiettv-inspection/1")
        self.assertEqual(doc["status"], "failed")
        self.assertIsNone(doc["registryRecords"])
        self.assertIsNone(doc["observedRuntime"])
        self.assertEqual(doc["problems"], [{"code": "platform-unsupported", "subject": "inventory", "fatal": True}])
        self.assertTrue(r.stdout.endswith(b"}\n") and r.stdout.count(b"\n") == 1, "expected one compact document")
        self.assertEqual(list(scratch.iterdir()), [], "inventory wrote into its working directory")
        self.assertEqual({p: p.exists() for p in watched}, before, "inventory created a root-level directory")
        self.assertEqual(_run(self.dll, "--inventory", cwd=scratch).stdout, r.stdout, "refusal not deterministic")

    @unittest.skipUnless(os.name == "nt", "the refusal path is the Windows behaviour")
    def test_framed_refusal_decodes_strictly_and_matches_raw(self):
        scratch = TOOL / "bin" / "cli-scratch"
        _remove_owned(scratch)
        scratch.mkdir(parents=True)
        raw = _run(self.dll, "--inventory", cwd=scratch)
        framed = _run(self.dll, "--inventory-framed", cwd=scratch)
        self.assertEqual(framed.returncode, 2)
        self.assertEqual(framed.stderr, b"")
        # The real tool's bytes are exactly the reference encoding of the raw document.
        self.assertEqual(framed.stdout, FRAME.encode_frame(raw.stdout[:-1]))
        doc = FRAME.decode_frame(framed.stdout)
        self.assertEqual(doc, _strict_json(raw.stdout))
        self.assertEqual(doc["problems"], [{"code": "platform-unsupported", "subject": "inventory", "fatal": True}])
        # With synthetic GC lines around it, the real frame still decodes; raw output never does.
        gc = _GC_START + b"\n" + _GC_END + b"\n"
        self.assertEqual(FRAME.decode_frame(gc + framed.stdout + gc), doc)
        with self.assertRaises(FRAME.FrameError):
            FRAME.decode_frame(raw.stdout)
        self.assertEqual(list(scratch.iterdir()), [], "framed inventory wrote into its working directory")
        self.assertEqual(_run(self.dll, "--inventory-framed", cwd=scratch).stdout, framed.stdout)


# Each guard: (file, exact snippet, compile-valid replacement, self-test check that must FAIL).
GUARDS = [
    ("duplicate-app", "Inspector.cs", 'throw Fatal("duplicate-app", appId);', "byId[appId] = row;",
     "fatal-duplicate-app"),
    ("case-clash-app", "Inspector.cs", 'throw Fatal("case-clash-app", appId);', "idFold.Remove(appId);",
     "fatal-case-clash-app"),
    ("case-clash-package", "Inspector.cs", 'throw Fatal("case-clash-package", row.Package);',
     "packageFold.Remove(row.Package);", "fatal-case-clash-package"),
    ("exact-flags", "Inspector.cs", 'throw Fatal("malformed-flag", appId);', "return false;",
     "fatal-malformed-flag"),
    ("unexpected-row-key", "Inspector.cs", 'throw Fatal("unexpected-row-key", "app_id");',
     'appId = "placeholder.id";', "fatal-unexpected-row-key"),
    ("duplicate-label", "Inspector.cs", 'throw Fatal("duplicate-label", appId!);', "continue;",
     "fatal-duplicate-label"),
    ("row-count-oversize", "Inspector.cs", 'throw Fatal("row-count-oversize", what);', "break;",
     "fatal-row-count-oversize"),
    ("registry-empty-not-success", "Inspector.cs", 'throw Fatal("registry-empty", "app-query");',
     "return new List<RegistryRecord>();", "fatal-registry-empty"),
    ("symlink-root", "Inspector.cs", 'return new InstallVerdict(false, root, "symlink-root-refused");',
     'return new InstallVerdict(true, root, "installed");', "symlink-root-refused"),
    ("tombstone", "Inspector.cs", 'return new InstallVerdict(false, root, "root-absent-tombstone");',
     'return new InstallVerdict(true, root, "installed");', "tombstone-not-installed"),
    ("external-root", "Inspector.cs", "if (!Rules.IsStrictInstallRoot(root, row.Package))",
     "if (root.Length < 0)", "external-root-refused"),
    ("path-traversal", "Inspector.cs", "if (!Rules.IsSafeAbsolute(root))", "if (root.Length < 0)",
     "path-traversal-refused"),
    ("unsafe-label", "Inspector.cs", "if (!Rules.IsSafeLabel(best.Label))", "if (best.Label is null)",
     "label-unsafe-held"),
    ("regex-end-anchor", "Inspector.cs", r'new Regex(@"^[a-z][a-z0-9_-]{0,31}\z"',
     r'new Regex(@"^[a-z][a-z0-9_-]{0,31}$"', "fatal-malformed-component"),
    ("stderr-not-echoed", "Inspector.cs", 'return "query-failed";\n    }\n}',
     'return "query-failed:" + text;\n    }\n}', "stderr-classified-not-echoed"),
    ("frame-one-line", "Inspector.cs", 'throw new InvalidOperationException("frame payload must be one line");',
     "continue;", "frame-one-line-payload"),
    ("frame-size-cap", "Inspector.cs", "if (payload.Length == 0 || payload.Length > MaxPayloadBytes)",
     "if (payload.Length < 0)", "frame-size-cap"),
    ("frame-oversize-fails-closed", "Inspector.cs", "&& json.Length - 1 <= MaxPayloadBytes;", ";",
     "frame-oversize-fails-closed"),
]


@unittest.skipUnless(DOTNET, "needs .NET SDK 6.0.428 (set QUIETTV_DOTNET)")
class GuardOff(unittest.TestCase):
    """Remove one guard per copy; the named self-test check must FAIL and the run must exit 1."""

    def _guard_off(self, name: str, file: str, snippet: str, replacement: str, check: str):
        copy = _copy("guardoff-" + name)
        try:
            target = copy / file
            text = target.read_text(encoding="utf-8")
            self.assertEqual(text.count(snippet), 1, f"guard snippet for {name} is not unique; update the test")
            target.write_text(text.replace(snippet, replacement), encoding="utf-8")
            r, dll = _build(copy, "out")
            self.assertEqual(r.returncode, 0, f"guard-off copy {name} did not build:\n{r.stdout}{r.stderr}")
            st = _run(dll, "--self-test")
            out = st.stdout.decode("utf-8")
            self.assertEqual(st.returncode, 1, f"{name}: self-test still passed with the guard removed\n{out}")
            self.assertRegex(out, rf"(?m)^FAIL {re.escape(check)}:", f"{name}: {check} did not catch it\n{out}")
            self.assertIn("SELF-TEST FAIL", out)
        finally:
            _remove_owned(copy)


def _make_guard_test(guard):
    name, file, snippet, replacement, check = guard

    def test(self):
        self._guard_off(name, file, snippet, replacement, check)

    test.__name__ = "test_guard_off_" + name.replace("-", "_")
    return test


for _guard in GUARDS:
    setattr(GuardOff, "test_guard_off_" + _guard[0].replace("-", "_"), _make_guard_test(_guard))


def _make_link(link: Path, target: Path) -> bool:
    """A directory junction on Windows (no shell involved), a directory symlink elsewhere."""
    try:
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(target), str(link))
        else:
            os.symlink(target, link, target_is_directory=True)
    except (OSError, ImportError, AttributeError):
        return False
    return _is_reparse(str(link))


def _unlink_link(link: Path) -> None:
    """Remove a link itself, never what it points at."""
    if not _is_reparse(str(link)):
        if os.path.lexists(link):
            raise CleanupRefused("not-a-link", str(link))
        return
    if os.name == "nt":
        os.rmdir(link)  # removes the junction / directory symlink only
    else:
        os.unlink(link)


_SENTINEL_PREFIX = "tvinspect-sentinel-"


def _remove_sentinel_dir(folder: Path) -> None:
    """Delete one test-made temp folder bottom-up, after proving it holds no links."""
    path = _norm(folder)
    if (not os.path.basename(path).startswith(_SENTINEL_PREFIX)
            or os.path.dirname(path) != _norm(tempfile.gettempdir())):
        raise CleanupRefused("not-a-sentinel-folder", path)
    if not os.path.lexists(path):
        return
    if _is_reparse(path):
        raise CleanupRefused("reparse-point", path)
    _refuse_reparse_inside(path)
    for dirpath, dirnames, filenames in os.walk(path, topdown=False, followlinks=False):
        for name in filenames:
            os.remove(os.path.join(dirpath, name))
        for name in dirnames:
            os.rmdir(os.path.join(dirpath, name))
    os.rmdir(path)


class _CleanupFixture(unittest.TestCase):
    """Sentinels live in a fresh temp folder outside the repository; links point at them."""

    def setUp(self):
        self.outside = Path(tempfile.mkdtemp(prefix=_SENTINEL_PREFIX))
        self.links: list[Path] = []
        self.copies: list[Path] = []
        self.files: list[Path] = []
        self.sentinels: list[Path] = []

    def tearDown(self):
        for link in reversed(self.links):  # links first, so nothing below recurses through one
            _unlink_link(link)
        for f in self.files:
            if os.path.lexists(f) and not _is_reparse(str(f)):
                os.remove(f)
        for copy in reversed(self.copies):
            _remove_owned(copy)
        _remove_sentinel_dir(self.outside)

    @staticmethod
    def _this():
        return sys.modules[__name__]

    def _sentinel(self, folder: Path) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        s = folder / "SENTINEL.txt"
        s.write_text("must survive", encoding="utf-8")
        self.sentinels.append(s)
        return s

    def _copy_dir(self, name: str) -> Path:
        copy = TOOL / "bin" / name
        _remove_owned(copy)
        self.copies.append(copy)
        return copy

    def _link(self, link: Path, target: Path) -> None:
        link.parent.mkdir(parents=True, exist_ok=True)
        if not _make_link(link, target):
            self.skipTest("cannot create a directory junction/symlink on this platform/account")
        self.links.append(link)

    def _attempt(self, mod, target, **kw):
        """Run mod._remove_owned with deletion recorded, never performed."""
        calls: list[str] = []
        saved = mod._RMTREE
        mod._RMTREE = calls.append
        try:
            mod._remove_owned(target, **kw)
            return "would-delete", None, calls
        except mod.CleanupRefused as refused:
            return "refused", refused.reason, calls
        finally:
            mod._RMTREE = saved

    def _assert_survivors(self):
        for s in self.sentinels:
            self.assertTrue(s.exists(), f"sentinel deleted: {s}")
            self.assertEqual(s.read_text(encoding="utf-8"), "must survive")
        for name in OWNED:
            self.assertTrue((TOOL / name).is_file(), f"source deleted: {name}")

    def _refused(self, target, reason: str, **kw):
        outcome, got, calls = self._attempt(self._this(), target, **kw)
        self.assertEqual((outcome, got), ("refused", reason), str(target))
        self.assertEqual(calls, [], "refused cleanup still reached the delete")
        self._assert_survivors()

    # Scenarios shared by the guard tests and their guard-off proofs. Each builds its fixture
    # and returns (target, kwargs) for _remove_owned.

    def _scenario_ancestor_link(self):
        copy = self._copy_dir("cg-ancestor")
        sentinel = self._sentinel(self.outside / "proj" / "obj")
        self._link(copy, self.outside / "proj")
        self.assertTrue((copy / "obj" / sentinel.name).exists(), "positive control: link redirects outside")
        return copy / "obj", {}

    def _scenario_inner_link(self):
        copy = self._copy_dir("cg-inner")
        (copy / "obj").mkdir(parents=True)
        sentinel = self._sentinel(self.outside / "inner")
        self._link(copy / "obj" / "escape", self.outside / "inner")
        self.assertTrue((copy / "obj" / "escape" / sentinel.name).exists(), "positive control")
        return copy / "obj", {}


class CleanupGuards(_CleanupFixture):
    """Every recursive delete in this file stays inside the tool's own generated folders."""

    def test_source_root_and_outside_paths_refused(self):
        self._sentinel(self.outside)
        for target in (TOOL, TOOL / "bin", REPO, REPO / "tools", TOOL / "Program.cs", TOOL / "bin" / "x" / "bin",
                       TOOL / "bin" / "a b", TOOL / "bin" / "x" / "src", TOOL / "bin" / "x" / "obj" / "deep",
                       TOOL / "bin" / "x" / "bin" / "y" / "z", TOOL / "src" / "obj", self.outside,
                       Path(os.path.abspath(os.sep))):
            with self.subTest(target=str(target)):
                self._refused(target, "outside-generated-namespace")

    def test_traversal_refused(self):
        self._sentinel(self.outside / "t")
        via_bin = str(TOOL / "bin" / "x") + os.sep + os.path.relpath(self.outside / "t", TOOL / "bin" / "x")
        for target in (via_bin, str(TOOL / "bin" / "x") + "/../y", str(TOOL / "bin" / ".." / "bin" / "y"),
                       str(TOOL / "bin" / "y" / ".."), "tools/tv-inspect/bin/x/../../..",
                       str(TOOL / "bin" / "x" / "obj") + "\\..\\..\\..\\Program.cs"):
            with self.subTest(target=target):
                self._refused(target, "traversal")

    def test_leaf_link_refused(self):
        copy = self._copy_dir("cg-leaf")
        sentinel = self._sentinel(self.outside / "leaf")
        self._link(copy, self.outside / "leaf")
        self.assertTrue((copy / sentinel.name).exists(), "positive control: link redirects outside")
        self._refused(copy, "reparse-point")
        self.assertTrue(_is_reparse(str(copy)), "the link itself was removed by a refused cleanup")

    def test_dangling_leaf_link_refused(self):
        copy = self._copy_dir("cg-dangling")
        gone = self.outside / "gone"
        gone.mkdir()
        self._link(copy, gone)
        os.rmdir(gone)
        self.assertTrue(_is_reparse(str(copy)) and not copy.exists(), "positive control: dangling link")
        self._refused(copy, "reparse-point")

    def test_ancestor_link_refused(self):
        target, kw = self._scenario_ancestor_link()
        self._refused(target, "reparse-point", **kw)
        self._sentinel(self.outside / "proj" / "bin" / "out")
        self._refused(target.parent / "bin" / "out", "reparse-point", **kw)

    def test_bin_ancestor_link_refused(self):
        # A link at <tool>/bin itself redirects every copy below it.
        fake_tool = self._copy_dir("cg-tool")
        fake_tool.mkdir()
        sentinel = self._sentinel(self.outside / "fake-bin" / "x")
        self._link(fake_tool / "bin", self.outside / "fake-bin")
        self.assertTrue((fake_tool / "bin" / "x" / sentinel.name).exists(), "positive control")
        self._refused(fake_tool / "bin" / "x", "reparse-point", tool=fake_tool)
        self._refused(fake_tool / "bin" / "x" / "obj", "reparse-point", tool=fake_tool)

    def test_inner_link_refused(self):
        target, kw = self._scenario_inner_link()
        self._refused(target, "reparse-point-inside", **kw)

    def test_each_link_layer_refuses_alone(self):
        # The lstat chain and the resolved-path check are independent: with either one removed,
        # the other still refuses the ancestor link.
        target, kw = self._scenario_ancestor_link()
        no_chain = _weakened(self, [_G_CHAIN], ["    pass"])
        no_resolve = _weakened(self, [_G_RESOLVED], ["    pass"])
        self.assertEqual(self._attempt(no_chain, target, **kw)[:2], ("refused", "resolved-escape"))
        self.assertEqual(self._attempt(no_resolve, target, **kw)[:2], ("refused", "reparse-point"))
        self._assert_survivors()

    def test_own_copy_cleanup_positive(self):
        copy = self._copy_dir("cg-positive")
        (copy / "obj" / "x").mkdir(parents=True)
        (copy / "obj" / "x" / "a.txt").write_text("a", encoding="utf-8")
        (copy / "bin" / "out").mkdir(parents=True)
        (copy / "bin" / "out" / "b.txt").write_text("b", encoding="utf-8")
        (copy / "keep.txt").write_text("k", encoding="utf-8")
        _remove_owned(copy / "obj")
        self.assertFalse(os.path.lexists(copy / "obj"))
        self.assertTrue((copy / "keep.txt").exists())
        _remove_owned(copy / "bin" / "out")
        self.assertFalse(os.path.lexists(copy / "bin" / "out"))
        self.assertTrue((copy / "bin").is_dir(), "bin/ of a copy is not a target")
        _remove_owned(copy)
        self.assertFalse(os.path.lexists(copy))
        _remove_owned(copy)  # missing target: no-op
        a_file = TOOL / "bin" / "cg-file"
        a_file.parent.mkdir(parents=True, exist_ok=True)
        a_file.write_text("x", encoding="utf-8")
        self.files.append(a_file)
        self._refused(a_file, "not-a-directory")
        self._assert_survivors()


# Exact snippets of each cleanup guard in _remove_owned, split so these literals never match.
_G_NAMESPACE = "    if not _in_generated_" + "namespace(lexical, tool_n):"
_G_TRAVERSAL = '    if any(part == ".." for part in re.split(r"[' + "\\\\/" + ']", raw)):'
_G_CHAIN = "    _refuse_reparse_" + "chain(lexical, repo_n)"
_G_RESOLVED = "    _refuse_resolved_" + "escape(lexical, tool_n)"
_G_INSIDE = "    _refuse_reparse_" + "inside(lexical)\n    _RMTREE"


def _weakened(case: unittest.TestCase, snippets: list[str], replacements: list[str]):
    """This module's source with guards replaced, loaded as a separate in-memory module."""
    source = Path(__file__).read_text(encoding="utf-8")
    for old, new in zip(snippets, replacements):
        case.assertEqual(source.count(old), 1, f"cleanup guard snippet not unique: {old!r}")
        source = source.replace(old, new)
    module = types.ModuleType("tv_inspector_cleanup_guard_off")
    module.__file__ = __file__
    exec(compile(source, __file__, "exec"), module.__dict__)
    return module


class CleanupGuardOff(_CleanupFixture):
    """Remove each cleanup guard in a copy of this module: the delete must then be reached.

    Deletion is recorded, never performed, so a guard-off run cannot harm anything."""

    def _reached(self, mod, target, **kw) -> list[str]:
        outcome, reason, calls = self._attempt(mod, target, **kw)
        self.assertEqual(outcome, "would-delete", f"guard-off copy still refused ({reason}); test proves nothing")
        self.assertEqual(len(calls), 1)
        self._assert_survivors()
        return calls

    def test_guard_off_namespace_reaches_source_root(self):
        calls = self._reached(_weakened(self, [_G_NAMESPACE], ["    if False:"]), TOOL)
        self.assertEqual(calls[0], _norm(TOOL))

    def test_guard_off_traversal_reaches_rewritten_path(self):
        real = self._copy_dir("cg-traversal")
        real.mkdir()
        calls = self._reached(_weakened(self, [_G_TRAVERSAL], ["    if False:"]), str(TOOL / "bin" / "x") + "/../cg-traversal")
        self.assertEqual(calls[0], _norm(real))

    def test_guard_off_link_checks_reach_outside(self):
        target, kw = self._scenario_ancestor_link()
        weak = _weakened(self, [_G_CHAIN, _G_RESOLVED], ["    pass", "    pass"])
        calls = self._reached(weak, target, **kw)
        resolved = _norm(os.path.realpath(calls[0]))
        self.assertTrue(resolved.startswith(_norm(self.outside) + os.sep), f"expected an outside target, got {resolved}")

    def test_guard_off_inner_link_reaches_delete(self):
        target, kw = self._scenario_inner_link()
        self._reached(_weakened(self, [_G_INSIDE], ["    _RMTREE"]), target, **kw)


# ---- frame decoder (frame.py) -------------------------------------------------------------

def _doc() -> dict:
    """A valid quiettv-inspection/1 document in the shape the C# renderer writes."""
    return {
        "schema": "quiettv-inspection/1", "toolVersion": "1.0.0", "status": "ok",
        "registryDatabase": "/opt/dbspace/.pkgmgr_parser.db",
        "registryRecords": [
            {"Id": "org.example.alpha", "Name": "Alpha", "Installed": True, "Disabled": False, "Launchable": True,
             "PackageId": "pkgalpha", "Version": "1.2.3", "InstallRoot": "/opt/usr/apps/pkgalpha", "Reason": "installed"},
            {"Id": "org.example.beta", "Name": None, "Installed": False, "Disabled": True, "Launchable": False,
             "PackageId": "pkgbeta", "Version": None, "InstallRoot": "/opt/usr/apps/pkgbeta",
             "Reason": "root-absent-tombstone"},
            {"Id": "org.example.gamma", "Name": "Gamma", "Installed": True, "Disabled": False, "Launchable": False,
             "PackageId": "pkggamma", "Version": "2", "InstallRoot": "/usr/apps/pkggamma", "Reason": "installed"},
            {"Id": "org.example.wrt", "Name": "Web", "Installed": False, "Disabled": False, "Launchable": True,
             "PackageId": "pkgwrt", "Version": "1", "InstallRoot": None, "Reason": "external-root-refused"},
        ],
        "observedRuntime": {"tizenRelease": {"path": "/etc/tizen-release", "lines": ["DISTRIB_ID=Tizen", "VERSION=9.0.0"]},
                            "tizenVersion": "9.0.0", "machine": "armv7l", "model": None, "firmware": None, "region": None},
        "problems": [{"code": "model-not-observed", "subject": "observedRuntime", "fatal": False},
                     {"code": "label-missing", "subject": "org.example.beta", "fatal": False}],
    }


def _failed_doc() -> dict:
    return {"schema": "quiettv-inspection/1", "toolVersion": "1.0.0", "status": "failed",
            "registryDatabase": "/opt/dbspace/.pkgmgr_parser.db", "registryRecords": None, "observedRuntime": None,
            "problems": [{"code": "query-timeout", "subject": "app-query", "fatal": True}]}


def _payload(doc) -> bytes:
    return json.dumps(doc, separators=(",", ":")).encode("utf-8")


def _framed(doc=None, mod=None) -> bytes:
    return (mod or FRAME).encode_frame(_payload(_doc() if doc is None else doc))


def _raw_frame(payload: bytes, length=None, digest=None, end=None) -> bytes:
    """A frame built by hand, so the decoder can be fed headers encode_frame would never write."""
    length = len(payload) if length is None else length
    digest = hashlib.sha256(payload).hexdigest() if digest is None else digest
    end = FRAME.END_TAG if end is None else end
    return b"QUIETTV-FRAME/1 %d %s\n" % (length, digest.encode("ascii")) + payload + b"\n" + end + b"\n"


def _gc(n: int) -> bytes:
    return b"".join((_GC_START if i % 2 == 0 else _GC_END) + b"\n" for i in range(n))


class FrameDecoder(unittest.TestCase):
    def assertRefused(self, data, code: str, mod=None):
        mod = mod or FRAME
        with self.assertRaises(mod.FrameError) as caught:
            mod.decode_frame(data)
        self.assertEqual(caught.exception.code, code, repr(data)[:160])

    # -- accepted -------------------------------------------------------------------------

    def test_roundtrip_clean(self):
        self.assertEqual(FRAME.decode_frame(_framed()), _doc())
        self.assertEqual(FRAME.decode_frame(_framed(_failed_doc())), _failed_doc())

    def test_encode_matches_format_exactly(self):
        p = _payload(_doc())
        expected = (b"QUIETTV-FRAME/1 " + str(len(p)).encode() + b" " + hashlib.sha256(p).hexdigest().encode()
                    + b"\n" + p + b"\nQUIETTV-FRAME-END/1\n")
        self.assertEqual(FRAME.encode_frame(p), expected)
        self.assertEqual(_raw_frame(p), expected)

    def test_gc_noise_before_after_and_both(self):
        frame = _framed()
        for before, after in ((_gc(2), b""), (b"", _gc(2)), (_gc(3), _gc(5)), (_GC_END + b"\n", _GC_START + b"\n")):
            with self.subTest(before=before, after=after):
                self.assertEqual(FRAME.decode_frame(before + frame + after), _doc())

    def test_gc_shapes_accepted_within_bounds(self):
        frame = _framed()
        for line in (b"[GC_INFO] GC_Start #4294967295: Gen(2), Time(1234567890 ms), Reason(255)",
                     b"[GC_INFO] GC_End #0: Gen(9), Time(0 ms)"):
            with self.subTest(line=line):
                self.assertEqual(FRAME.decode_frame(line + b"\n" + frame), _doc())

    def test_noise_line_cap_is_exact(self):
        frame = _framed()
        half = FRAME.MAX_NOISE_LINES // 2
        self.assertEqual(FRAME.decode_frame(_gc(half) + frame + _gc(FRAME.MAX_NOISE_LINES - half)), _doc())
        self.assertRefused(_gc(half) + frame + _gc(FRAME.MAX_NOISE_LINES - half + 1), "noise-oversize")
        self.assertRefused(_gc(FRAME.MAX_NOISE_LINES + 1) + frame, "noise-oversize")

    # -- refused: framing -----------------------------------------------------------------

    def test_raw_document_and_empty_input_refused(self):
        self.assertRefused(_payload(_doc()) + b"\n", "unknown-noise")
        self.assertRefused(_payload(_doc()), "no-frame")
        self.assertRefused(_GC_START + b"\n" + _payload(_doc()) + b"\n" + _GC_END + b"\n", "unknown-noise")
        self.assertRefused(b"", "no-frame")
        self.assertRefused(_gc(4), "no-frame")

    def test_multiple_frames_refused(self):
        frame = _framed()
        self.assertRefused(frame + frame, "unknown-noise")
        self.assertRefused(frame + _gc(1) + _framed(_failed_doc()), "unknown-noise")
        # A frame hidden inside the payload of another is just payload: it fails the JSON checks.
        self.assertRefused(FRAME.encode_frame(b"QUIETTV-FRAME/1 2 " + b"0" * 64), "payload-not-object")

    def test_truncated_refused(self):
        frame = _framed()
        header_end = frame.index(b"\n") + 1
        self.assertRefused(frame[:header_end + 10], "truncated")
        self.assertRefused(frame[:header_end], "truncated")
        self.assertRefused(frame[:-1], "end-tag-mismatch")
        self.assertRefused(frame[:-8], "end-tag-mismatch")
        self.assertRefused(frame[:header_end - 1], "no-frame")

    def test_length_off_by_one_refused(self):
        p = _payload(_doc())
        self.assertRefused(_raw_frame(p, length=len(p) - 1), "end-tag-mismatch")
        self.assertRefused(_raw_frame(p, length=len(p) + 1), "end-tag-mismatch")
        self.assertRefused(_raw_frame(p, length=0), "end-tag-mismatch")

    def test_hash_corrupt_and_payload_flip_refused(self):
        p = _payload(_doc())
        good = hashlib.sha256(p).hexdigest()
        bad = ("0" if good[0] != "0" else "1") + good[1:]
        self.assertRefused(_raw_frame(p, digest=bad), "hash-mismatch")
        flipped = p.replace(b'"1.2.3"', b'"1.2.4"')
        self.assertEqual(len(flipped), len(p))
        self.assertRefused(_raw_frame(flipped, digest=good), "hash-mismatch")
        self.assertRefused(_raw_frame(p, digest=good.upper()), "unknown-noise")  # uppercase hash: not a header

    def test_bad_end_tag_refused(self):
        p = _payload(_doc())
        for end in (b"QUIETTV-FRAME-END/2", b"QUIETTV-FRAME-END/1 ", b"quiettv-frame-end/1", b"QUIETTV-FRAME/1"):
            with self.subTest(end=end):
                self.assertRefused(_raw_frame(p, end=end), "end-tag-mismatch")
        frame = _framed()
        self.assertRefused(frame[:-1] + b"\r\n", "end-tag-mismatch")

    def test_malformed_headers_refused(self):
        p = _payload(_doc())
        h = hashlib.sha256(p).hexdigest().encode()
        n = str(len(p)).encode()
        tail = p + b"\n" + FRAME.END_TAG + b"\n"
        for header in (b"QUIETTV-FRAME/1 0" + n + b" " + h, b"QUIETTV-FRAME/1  " + n + b" " + h,
                       b"QUIETTV-FRAME/1 " + n + b" " + h + b" ", b"QUIETTV-FRAME/1 " + n + b" " + h + b"\r",
                       b"QUIETTV-FRAME/1\t" + n + b" " + h, b"QUIETTV-FRAME/2 " + n + b" " + h,
                       b"QUIETTV-FRAME/1 " + n, b"QUIETTV-FRAME/1 " + n + b" " + h[:-1],
                       b" QUIETTV-FRAME/1 " + n + b" " + h, b"QUIETTV-FRAME/1 +" + n + b" " + h,
                       b"QUIETTV-FRAME/1 " + n + b" " + h + b"00"):
            with self.subTest(header=header):
                self.assertRefused(header + b"\n" + tail, "unknown-noise")
        self.assertRefused(b"QUIETTV-FRAME/1 999999999 " + h + b"\n" + tail, "payload-oversize")
        self.assertRefused(b"QUIETTV-FRAME/1 1234567890 " + h + b"\n" + tail, "unknown-noise")

    # -- refused: noise -------------------------------------------------------------------

    def test_unknown_noise_refused_before_and_after(self):
        frame = _framed()
        for junk in (b"hello", b"", b" ", b"[exit:0]", b"[GC_INFO]", b"Unhandled exception.",
                     b'{"schema":"quiettv-inspection/1"}', b"\x00", b"\xff\xfe"):
            with self.subTest(junk=junk):
                self.assertRefused(junk + b"\n" + frame, "unknown-noise")
                self.assertRefused(frame + junk + b"\n", "unknown-noise")

    def test_exit_trailer_is_not_stripped_by_the_decoder(self):
        frame = _framed()
        self.assertRefused(frame + b"[exit:0]", "unterminated-noise")
        self.assertRefused(frame + b"[exit:0]\n", "unknown-noise")
        self.assertRefused(frame + _gc(1) + b"[exit:1]", "unterminated-noise")

    def test_near_miss_gc_lines_refused(self):
        frame = _framed()
        for line in (_GC_START + b" extra", _GC_START + b"\r", b" " + _GC_START, b"x" + _GC_END,
                     _GC_START.lower(), _GC_START.replace(b"#1", b"#-1"), _GC_START.replace(b"Gen(0)", b"Gen(10)"),
                     _GC_END.replace(b"11 ms", b"12345678901 ms"), _GC_START.replace(b"Reason(0)", b"Reason(1000)"),
                     _GC_END + b", Reason(0)", _GC_START.replace(b"GC_Start", b"GC_Begin"),
                     _GC_END + _GC_END, _GC_START + b"\x00", b"\t" + _GC_END):
            with self.subTest(line=line):
                self.assertRefused(line + b"\n" + frame, "unknown-noise")
                self.assertRefused(frame + line + b"\n", "unknown-noise")
        self.assertRefused(frame + _GC_END, "unterminated-noise")
        # The per-line byte cap holds even if a future regex widened.
        self.assertGreater(FRAME.MAX_NOISE_LINE_BYTES, len(b"[GC_INFO] GC_Start #4294967295: Gen(2), Time(1234567890 ms), Reason(255)"))

    def test_gc_line_inside_frame_refused(self):
        p = _payload(_doc())
        self.assertRefused(_raw_frame(_GC_START + b"\n" + p), "payload-not-one-line")
        self.assertRefused(_raw_frame(p[:10] + b"\n" + _GC_END + b"\n" + p[10:]), "payload-not-one-line")
        self.assertRefused(_raw_frame(p.replace(b",", b",\r", 1)), "payload-not-one-line")

    def test_input_bounds(self):
        self.assertRefused(b"x" * (FRAME.MAX_INPUT_BYTES + 1), "input-oversize")
        for bad in (_framed().decode("ascii"), bytearray(_framed()), memoryview(_framed()), None):
            with self.subTest(kind=type(bad).__name__):
                self.assertRefused(bad, "input-not-bytes")

    def test_encode_refuses_what_decode_would(self):
        for bad in (b"", b"{}\n", b"{\r}", b"x" * (FRAME.MAX_PAYLOAD_BYTES + 1), "{}"):
            with self.subTest(size=len(bad)):
                with self.assertRaises(FRAME.FrameError):
                    FRAME.encode_frame(bad)

    # -- refused: payload -----------------------------------------------------------------

    def test_strict_json_refusals(self):
        valid = _payload(_doc())
        cases = [
            (b'{"schema":"a","schema":"b"}', "json-duplicate-key"),
            (valid[:1] + b'"status":"failed",' + valid[1:], "json-duplicate-key"),
            (b'{"a":NaN}', "json-non-finite"), (b'{"a":Infinity}', "json-non-finite"), (b'{"a":-Infinity}', "json-non-finite"),
            (b'{"a":1.5}', "json-float"), (b'{"a":1e3}', "json-float"),
            (b'[1]', "payload-not-object"), (b'"x"', "payload-not-object"), (b' {}', "payload-not-object"),
            (b"\xef\xbb\xbf{}", "payload-not-object"), (b'{} ', "payload-not-object"),
            (b'{"a":"\xff"}', "payload-not-utf8"), (b'{"a":"\xed\xa0\x80"}', "payload-not-utf8"),
            (b'{} {}', "payload-not-json"), (b'{"a":}', "payload-not-json"), (b"{" + b'"a":[' * 100000 + b"}", "payload-not-json"),
            (b'{}', "schema-top-level"),
        ]
        for payload, code in cases:
            with self.subTest(payload=payload[:40]):
                self.assertRefused(_raw_frame(payload), code)

    def test_schema_violations_refused(self):
        def mutate(fn):
            d = _doc()
            fn(d)
            return d

        rec = lambda i: (lambda d: d["registryRecords"][i])  # noqa: E731
        cases = [
            (lambda d: d.pop("problems"), "schema-top-level"),
            (lambda d: d.update(extra=1), "schema-top-level"),
            (lambda d: d.update(schema="quiettv-inspection/2"), "schema-identity"),
            (lambda d: d.update(toolVersion=""), "schema-identity"),
            (lambda d: d.update(registryRecords=None), "schema-status-inconsistent"),
            (lambda d: d.update(status="failed"), "schema-status-inconsistent"),
            (lambda d: d.update(status="OK"), "schema-status-inconsistent"),
            (lambda d: d["problems"].append({"code": "query-failed", "subject": "x", "fatal": True}), "schema-status-inconsistent"),
            (lambda d: d["problems"].append({"code": "Bad", "subject": "x", "fatal": False}), "schema-problem"),
            (lambda d: d["problems"].append({"code": "x", "subject": "x", "fatal": "true"}), "schema-problem"),
            (lambda d: d["problems"].append({"code": "x", "subject": "", "fatal": False}), "schema-problem"),
            (lambda d: d["problems"].append({"code": "x", "subject": "x", "fatal": False, "more": 1}), "schema-problem"),
            (lambda d: d.update(registryRecords={}), "schema-status-inconsistent"),
            (lambda d: rec(0)(d).update(Extra=1), "schema-record-keys"),
            (lambda d: rec(0)(d).pop("Reason"), "schema-record-keys"),
            (lambda d: rec(0)(d).update(Installed=1), "schema-record-flags"),
            (lambda d: rec(1)(d).update(Disabled=None), "schema-record-flags"),
            (lambda d: rec(0)(d).update(Id="org..example"), "schema-record-id"),
            (lambda d: rec(0)(d).update(PackageId="../etc"), "schema-record-id"),
            (lambda d: rec(0)(d).update(Name=""), "schema-record-text"),
            (lambda d: rec(0)(d).update(Version=3), "schema-record-text"),
            (lambda d: rec(0)(d).update(Reason="maybe"), "schema-record-reason"),
            (lambda d: rec(1)(d).update(Reason="installed"), "schema-record-reason"),
            (lambda d: rec(0)(d).update(Installed=False), "schema-record-reason"),
            (lambda d: rec(0)(d).update(InstallRoot="/usr/bin/wrt"), "schema-record-root"),
            (lambda d: rec(0)(d).update(InstallRoot="/opt/usr/apps/pkgalpha/"), "schema-record-root"),
            (lambda d: rec(0)(d).update(InstallRoot=None), "schema-record-root"),
            (lambda d: d["registryRecords"].reverse(), "schema-record-order"),
            (lambda d: d["registryRecords"].append(dict(d["registryRecords"][-1])), "schema-record-order"),
            (lambda d: d["registryRecords"].insert(0, dict(rec(0)(d), Id="ORG.example.alpha")), "schema-record-duplicate"),
            (lambda d: d.update(observedRuntime=[]), "schema-runtime"),
            (lambda d: d["observedRuntime"].update(model="QN65"), "schema-runtime-guessed"),
            (lambda d: d["observedRuntime"].update(region="US"), "schema-runtime-guessed"),
            (lambda d: d["observedRuntime"].update(firmware="T-1"), "schema-runtime-guessed"),
            (lambda d: d["observedRuntime"].pop("machine"), "schema-runtime"),
            (lambda d: d["observedRuntime"]["tizenRelease"].update(path="/etc/passwd"), "schema-runtime-release"),
            (lambda d: d["observedRuntime"]["tizenRelease"]["lines"].append("badé"), "schema-runtime-release"),
            (lambda d: d["observedRuntime"]["tizenRelease"].update(lines=["x"] * 33), "schema-runtime-release"),
        ]
        for fn, code in cases:
            d = mutate(fn)
            with self.subTest(code=code):
                self.assertRefused(_framed(d), code)

    def test_ok_inventory_must_be_nonempty(self):
        d = _doc()
        d["registryRecords"] = []
        self.assertRefused(_framed(d), "schema-records-empty")
        d["registryRecords"] = _doc()["registryRecords"][:1]
        self.assertEqual(FRAME.decode_frame(_framed(d)), d)  # one record is the smallest valid inventory

    def test_registry_database_is_the_exact_constant(self):
        self.assertEqual(FRAME.decode_frame(_framed(_doc()))["registryDatabase"], "/opt/dbspace/.pkgmgr_parser.db")
        for bad in ("/opt/dbspace/other.db", "/opt/dbspace/.pkgmgr_parser.db ", "x", "/opt/dbspace/.pkgmgr_parser.db/../x", ""):
            for make in (_doc, _failed_doc):
                d = make()
                d["registryDatabase"] = bad
                with self.subTest(db=bad, kind=make.__name__):
                    self.assertRefused(_framed(d), "schema-identity")  # _framed recomputes the hash

    def test_failed_document_rules(self):
        d = _failed_doc()
        d["registryRecords"] = []
        self.assertRefused(_framed(d), "schema-status-inconsistent")  # [] would read as "nothing installed"
        d = _failed_doc()
        d["problems"][0]["fatal"] = False
        self.assertRefused(_framed(d), "schema-status-inconsistent")
        d = _failed_doc()
        d["observedRuntime"] = {"x": 1}
        self.assertRefused(_framed(d), "schema-runtime")


# Exact snippets of each decoder guard in frame.py, with what removes it, and an input the
# real decoder refuses (code) that the weakened one must then accept.
def _gc_suffix_input():
    return _GC_START + b" smuggled\n" + _framed()


FRAME_GUARDS = [
    ("hash", 'if hashlib.sha256(payload).hexdigest() != digest:', "if False:",
     lambda: _raw_frame(_payload(_doc()).replace(b'"1.2.3"', b'"1.2.4"'), digest=hashlib.sha256(_payload(_doc())).hexdigest()),
     "hash-mismatch"),
    ("end-tag", "if data[pos:pos + len(_TRAILER)] != _TRAILER:", "if False:",
     lambda: _raw_frame(_payload(_doc()), end=b"QUIETTV-FRAME-END/2"), "end-tag-mismatch"),
    ("noise", "if len(line) > MAX_NOISE_LINE_BYTES or not _GC_LINE.match(line):", "if False:",
     lambda: b"hello\n" + _framed() + b"[exit:0]\n", "unknown-noise"),
    ("noise-start-anchor", "not _GC_LINE.match(line)", "not _GC_LINE.search(line)",
     lambda: b"junk" + _GC_END + b"\n" + _framed(), "unknown-noise"),
    ("noise-end-anchor", r'Reason\([0-9]{1,3}\)\Z"', r'Reason\([0-9]{1,3}\)"', _gc_suffix_input, "unknown-noise"),
    ("noise-count", "if count + 1 > MAX_NOISE_LINES:", "if False:",
     lambda: _gc(FRAME.MAX_NOISE_LINES + 1) + _framed(), "noise-oversize"),
    ("header-anchor", r'([0-9a-f]{64})\Z")', r'([0-9a-f]{64})")',
     lambda: FRAME.encode_frame(_payload(_doc())).replace(b"\n", b" x\n", 1), "unknown-noise"),
    ("one-line-payload", 'def _strict_json(payload: bytes):\n    if b"\\n" in payload or b"\\r" in payload:',
     "def _strict_json(payload: bytes):\n    if False:",
     lambda: _raw_frame(_payload(_doc()).replace(b',"toolVersion"', b',\n"toolVersion"')), "payload-not-one-line"),
    ("duplicate-key", "if len(keys) != len(set(keys)):", "if False:",
     lambda: _raw_frame(b'{"status":"failed",' + _payload(_doc())[1:]), "json-duplicate-key"),
    ("schema-call", "return validate_document(_strict_json(payload))", "return _strict_json(payload)",
     lambda: _raw_frame(b'{"anything":1}'), "schema-top-level"),
    ("status-consistency", "    if not (ok or failed):", "    if False:",
     lambda: _framed(dict(_doc(), registryRecords=None)), "schema-status-inconsistent"),
    ("records-nonempty", "    if not records:", "    if False:",
     lambda: _framed(dict(_doc(), registryRecords=[])), "schema-records-empty"),
    ("registry-database-exact", 'or doc["registryDatabase"] != REGISTRY_DATABASE:', "or False:",
     lambda: _framed(dict(_doc(), registryDatabase="/opt/dbspace/other.db")), "schema-identity"),
    ("record-root", 'if r["Installed"] and r["InstallRoot"] not in', 'if False and r["InstallRoot"] not in',
     lambda: _framed(_with_record(0, InstallRoot="/usr/bin/wrt")), "schema-record-root"),
    ("record-order", 'if previous is not None and not previous < r["Id"]:', "if False:",
     lambda: _framed(dict(_doc(), registryRecords=_doc()["registryRecords"][::-1])), "schema-record-order"),
    ("runtime-not-guessed",
     '    if runtime["model"] is not None or runtime["firmware"] is not None or runtime["region"] is not None:',
     "    if False:", lambda: _framed(_with_runtime(model="QN65")), "schema-runtime-guessed"),
]


def _with_record(i: int, **fields) -> dict:
    d = _doc()
    d["registryRecords"][i].update(fields)
    return d


def _with_runtime(**fields) -> dict:
    d = _doc()
    d["observedRuntime"].update(fields)
    return d


class FrameGuardOff(unittest.TestCase):
    """Remove one decoder guard in an in-memory copy of frame.py: its malicious input is then accepted.

    The unmodified decoder refusing the same input with the named code is the positive control."""

    def _guard_off(self, snippet, replacement, make_input, code):
        data = make_input()
        with self.assertRaises(FRAME.FrameError) as caught:
            FRAME.decode_frame(data)
        self.assertEqual(caught.exception.code, code, "positive control: the real decoder must refuse with this code")
        weak = _load_frame([(snippet, replacement)], case=self)
        try:
            weak.decode_frame(data)
        except weak.FrameError as e:
            self.fail(f"guard-off copy still refused ({e.code}); the test proves nothing")
        # The weakened copy still decodes a clean frame: only the one guard was removed.
        self.assertEqual(weak.decode_frame(_framed()), _doc())

    def test_strict_json_constants_and_floats_guard_off(self):
        for snippet, replacement, payload, code in (
                ("parse_constant=constant, ", "", b'{"a":NaN}', "json-non-finite"),
                (", parse_float=no_float", "", b'{"a":1.5}', "json-float")):
            with self.subTest(code=code):
                with self.assertRaises(FRAME.FrameError) as caught:
                    FRAME._strict_json(payload)
                self.assertEqual(caught.exception.code, code)
                weak = _load_frame([(snippet, replacement)], case=self)
                self.assertIsInstance(weak._strict_json(payload), dict)

    def test_input_cap_is_checked_before_scanning(self):
        # Nothing valid can exceed the input cap, so this bound is shown by order, not by guard-off:
        # an oversize input is refused before a single line is read.
        self.assertRefusedFirst(b"hello\n" * (FRAME.MAX_INPUT_BYTES // 6 + 1))

    def assertRefusedFirst(self, data):
        with self.assertRaises(FRAME.FrameError) as caught:
            FRAME.decode_frame(data)
        self.assertEqual(caught.exception.code, "input-oversize")


def _make_frame_guard_test(guard):
    name, snippet, replacement, make_input, code = guard

    def test(self):
        self._guard_off(snippet, replacement, make_input, code)

    test.__name__ = "test_guard_off_" + name.replace("-", "_")
    return test


for _guard in FRAME_GUARDS:
    setattr(FrameGuardOff, "test_guard_off_" + _guard[0].replace("-", "_"), _make_frame_guard_test(_guard))


if __name__ == "__main__":
    unittest.main()

using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Reflection;
using System.Text;
using System.Text.Json;

namespace QuietTVInspect;

// Memory-only checks: every observation goes through FakeSystem, so no file, process or
// network is touched. Each check prints PASS or FAIL with its name; the Python guard-off tests
// remove one guard and require the named check to FAIL.
internal static class SelfTests
{
    private static int _passed;
    private static int _failed;

    internal static int Run()
    {
        Check("representative-inventory", TestRepresentativeInventory);
        Check("deterministic-output", TestDeterministicOutput);
        Check("row-order-independent", TestRowOrderIndependent);
        Check("launchable-is-registry-declaration", TestLaunchableDefinition);
        Check("component-difference", TestComponentDifference);
        Check("disabled-flags", TestDisabledFlags);
        Check("tombstone-not-installed", TestTombstone);
        Check("symlink-root-refused", TestSymlinkRoot);
        Check("external-root-refused", TestExternalRoot);
        Check("path-traversal-refused", TestPathTraversal);
        Check("root-not-directory", TestRootNotDirectory);
        Check("app-root-mismatch", TestAppRootMismatch);
        Check("probes-only-strict-roots", TestProbesOnlyStrictRoots);
        Check("locale-preference", TestLocalePreference);
        Check("label-unsafe-held", TestUnsafeLabelHeld);
        Check("label-missing-null", TestLabelMissing);
        Check("version-unsafe-null", TestVersionUnsafe);
        Check("exec-unsafe-not-launchable", TestExecUnsafe);
        Check("fatal-table-unsupported", TestTableUnsupported);
        Check("fatal-query-failures", TestQueryFailures);
        Check("fatal-malformed-flag", TestMalformedFlag);
        Check("fatal-unknown-flag", TestUnknownFlag);
        Check("fatal-duplicate-app", TestDuplicateApp);
        Check("fatal-case-clash-app", TestCaseClashApp);
        Check("fatal-case-clash-package", TestCaseClashPackage);
        Check("fatal-unexpected-row-key", TestUnexpectedRowKey);
        Check("fatal-malformed-output", TestMalformedOutput);
        Check("fatal-row-count-oversize", TestRowCountOversize);
        Check("fatal-output-oversize", TestOutputOversize);
        Check("fatal-duplicate-label", TestDuplicateLabel);
        Check("fatal-registry-empty", TestRegistryEmpty);
        Check("fatal-malformed-component", TestMalformedComponent);
        Check("runtime-evidence", TestRuntimeEvidence);
        Check("runtime-missing-explicit-null", TestRuntimeMissing);
        Check("runtime-unsafe-rejected", TestRuntimeUnsafe);
        Check("stderr-classified-not-echoed", TestStderrClassifier);
        Check("bounded-capture", TestBoundedCapture);
        Check("platform-refusal-document", TestPlatformRefusal);
        Check("no-write-surface", TestNoWriteSurface);
        Check("frame-roundtrip", TestFrameRoundtrip);
        Check("frame-one-line-payload", TestFrameOneLinePayload);
        Check("frame-size-cap", TestFrameSizeCap);
        Check("frame-oversize-fails-closed", TestFrameOversizeFailsClosed);

        Console.WriteLine("self-test: passed=" + _passed + " failed=" + _failed);
        Console.WriteLine(_failed == 0 ? "SELF-TEST PASS" : "SELF-TEST FAIL");
        return _failed == 0 ? 0 : 1;
    }

    private static void Check(string name, Action test)
    {
        try
        {
            test();
            _passed++;
            Console.WriteLine("PASS " + name);
        }
        catch (Exception ex)
        {
            _failed++;
            Console.WriteLine("FAIL " + name + ": " + ex.Message);
        }
    }

    // ---- fixture -------------------------------------------------------------------------

    private sealed class FakeSystem : IInspectSystem
    {
        internal readonly Dictionary<FixedCommand, CommandResult> Results = new Dictionary<FixedCommand, CommandResult>();
        internal readonly Dictionary<string, RootKind> Roots = new Dictionary<string, RootKind>(StringComparer.Ordinal);
        internal FileReadResult Release = new FileReadResult(
            ReadStatus.Ok, Encoding.ASCII.GetBytes("Tizen 6.5 (armv7l)\nVERSION = 6.5\nCODENAME = Tizen\n"));
        internal readonly List<string> Calls = new List<string>();

        public CommandResult Run(FixedCommand command)
        {
            Calls.Add("run:" + command);
            return Results.TryGetValue(command, out CommandResult? result) ? result : CommandResult.Failed(CommandStatus.NotFound);
        }

        public FileReadResult ReadTizenRelease()
        {
            Calls.Add("read:tizen-release");
            return Release;
        }

        public RootKind ProbeRoot(string absoluteRoot)
        {
            Calls.Add("probe:" + absoluteRoot);
            return Roots.TryGetValue(absoluteRoot, out RootKind kind) ? kind : RootKind.Missing;
        }
    }

    private static string Enc(string? value)
    {
        if (value is null)
        {
            return "N";
        }

        return "X" + Convert.ToHexString(Encoding.UTF8.GetBytes(value));
    }

    private static string App(
        string? id, string? component, string? exec, string? nodisplay, string? disable,
        string? appRoot, string? package, string? root, string? pkgDisable, string? version) =>
        string.Join("|", Enc(id), Enc(component), Enc(exec), Enc(nodisplay), Enc(disable),
            Enc(appRoot), Enc(package), Enc(root), Enc(pkgDisable), Enc(version)) + "\n";

    // A typical installed native ui app in /opt/usr/apps.
    private static string UiApp(string id, string package, string root = "", string version = "1.0.0") =>
        App(id, "uiapp", "/opt/usr/apps/" + package + "/bin/" + id, "false", "false",
            root.Length == 0 ? "/opt/usr/apps/" + package : root, package,
            root.Length == 0 ? "/opt/usr/apps/" + package : root, "false", version);

    private static string Label(string? id, string? locale, string? label) =>
        string.Join("|", Enc(id), Enc(locale), Enc(label)) + "\n";

    private static FakeSystem Fake(string apps, string labels)
    {
        FakeSystem fake = new FakeSystem();
        fake.Results[FixedCommand.AppQuery] = CommandResult.Ok(Encoding.ASCII.GetBytes(apps));
        fake.Results[FixedCommand.LabelQuery] = CommandResult.Ok(Encoding.ASCII.GetBytes(labels));
        fake.Results[FixedCommand.UnameMachine] = CommandResult.Ok(Encoding.ASCII.GetBytes("armv7l\n"));
        return fake;
    }

    private static string RepresentativeApps() =>
        UiApp("com.example.alpha", "com.example.alpha", version: "1.2.3") +
        // Web app: exec is the shared /usr/bin/wrt engine, a read-only reference that is never run.
        App("WebPkg0001.Player", "uiapp", "/usr/bin/wrt", "false", "false",
            "/opt/usr/apps/WebPkg0001", "WebPkg0001", "/opt/usr/apps/WebPkg0001", "false", "2.0.0") +
        App("WebPkg0001.Service", "svcapp", "/usr/bin/wrt", "true", "false",
            "/opt/usr/apps/WebPkg0001", "WebPkg0001", "/opt/usr/apps/WebPkg0001", "false", "2.0.0") +
        App("com.example.appoff", "uiapp", "/opt/usr/apps/com.example.appoff/bin/x", "false", "true",
            "/opt/usr/apps/com.example.appoff", "com.example.appoff", "/opt/usr/apps/com.example.appoff", "false", "1.0.0") +
        App("com.example.pkgoff", "uiapp", "/opt/usr/apps/com.example.pkgoff/bin/x", "false", "false",
            "/opt/usr/apps/com.example.pkgoff", "com.example.pkgoff", "/opt/usr/apps/com.example.pkgoff", "true", "1.0.0") +
        UiApp("com.example.gone", "com.example.gone") +
        App("com.example.engine", "uiapp", "/usr/bin/aria-engine", "false", "false",
            "/usr/share/aria-engine", "com.example.engine", "/usr/share/aria-engine", "false", "3.1") +
        UiApp("com.example.linked", "com.example.linked") +
        App("org.example.system", "uiapp", "/usr/apps/org.example.system/bin/sys", "false", "false",
            "/usr/apps/org.example.system", "org.example.system", "/usr/apps/org.example.system", "false", "5.0.0") +
        UiApp("com.example.trav", "com.example.trav", root: "/opt/usr/apps/../../etc") +
        App("com.example.hidden", "uiapp", "/opt/usr/apps/com.example.hidden/bin/h", "true", "false",
            "/opt/usr/apps/com.example.hidden", "com.example.hidden", "/opt/usr/apps/com.example.hidden", "false", "1.0.0") +
        App("com.example.noexec", "uiapp", "", "false", "false",
            "/opt/usr/apps/com.example.noexec", "com.example.noexec", "/opt/usr/apps/com.example.noexec", "false", "1.0.0") +
        UiApp("com.example.bidi", "com.example.bidi");

    private static string RepresentativeLabels() =>
        Label("com.example.alpha", "No Locale", "Alpha default") +
        Label("com.example.alpha", "en-us", "Alpha") +
        Label("com.example.alpha", "ko-kr", "알파") +
        Label("WebPkg0001.Player", "No Locale", "Player") +
        Label("com.example.appoff", "en-us", "App Off") +
        Label("com.example.pkgoff", "en-us", "Package Off") +
        Label("com.example.gone", "en-us", "Gone") +
        Label("com.example.engine", "en-us", "Engine") +
        Label("com.example.linked", "en-us", "Linked") +
        Label("org.example.system", "ko-kr", "시스템") +
        Label("org.example.system", "de-de", "System") +
        Label("com.example.trav", "en-us", "Trav") +
        Label("com.example.hidden", "en-us", "Hidden") +
        Label("com.example.noexec", "en-us", "No Exec") +
        Label("com.example.bidi", "en-us", "Evil‮gnp.exe") +
        Label("com.example.bidi", "No Locale", "Safe Fallback") +
        // Labels for apps outside the registry join are validated but ignored.
        Label("com.example.orphan", "en-us", "Orphan");

    private static FakeSystem Representative()
    {
        FakeSystem fake = Fake(RepresentativeApps(), RepresentativeLabels());
        foreach (string root in new[]
                 {
                     "/opt/usr/apps/com.example.alpha", "/opt/usr/apps/WebPkg0001", "/opt/usr/apps/com.example.appoff",
                     "/opt/usr/apps/com.example.pkgoff", "/usr/apps/org.example.system", "/opt/usr/apps/com.example.hidden",
                     "/opt/usr/apps/com.example.noexec", "/opt/usr/apps/com.example.bidi",
                     // Traps: these exist, and must still never be probed or counted.
                     "/usr/share/aria-engine", "/opt/usr/apps/../../etc", "/etc",
                 })
        {
            fake.Roots[root] = RootKind.Directory;
        }

        fake.Roots["/opt/usr/apps/com.example.linked"] = RootKind.Symlink;
        return fake;
    }

    private sealed class Doc
    {
        internal JsonElement Root;
        internal Dictionary<string, JsonElement> Records = new Dictionary<string, JsonElement>(StringComparer.Ordinal);
        internal List<(string Code, string Subject, bool Fatal)> Problems = new List<(string, string, bool)>();
    }

    private static Doc Parse(InspectionOutcome outcome)
    {
        JsonDocument document = JsonDocument.Parse(outcome.Json, new JsonDocumentOptions
        {
            AllowTrailingCommas = false,
            CommentHandling = JsonCommentHandling.Disallow,
        });
        Doc doc = new Doc { Root = document.RootElement.Clone() };
        Expect(doc.Root.GetProperty("schema").GetString() == InspectContract.Schema, "schema");
        Expect(doc.Root.GetProperty("toolVersion").GetString() == InspectContract.ToolVersion, "toolVersion");
        JsonElement records = doc.Root.GetProperty("registryRecords");
        if (records.ValueKind == JsonValueKind.Array)
        {
            foreach (JsonElement record in records.EnumerateArray())
            {
                doc.Records.Add(record.GetProperty("Id").GetString()!, record);
            }
        }

        foreach (JsonElement p in doc.Root.GetProperty("problems").EnumerateArray())
        {
            doc.Problems.Add((p.GetProperty("code").GetString()!, p.GetProperty("subject").GetString()!, p.GetProperty("fatal").GetBoolean()));
        }

        return doc;
    }

    private static Doc Ok(FakeSystem fake)
    {
        InspectionOutcome outcome = Inspector.Inspect(fake);
        Doc doc = Parse(outcome);
        string status = doc.Root.GetProperty("status").GetString()!;
        Expect(outcome.Ok && status == "ok", "expected inventory success, got " + status + " " + FatalCodes(doc));
        return doc;
    }

    private static Doc Failed(FakeSystem fake, string code)
    {
        InspectionOutcome outcome = Inspector.Inspect(fake);
        Doc doc = Parse(outcome);
        Expect(!outcome.Ok, "inventory succeeded but " + code + " was expected");
        Expect(doc.Root.GetProperty("status").GetString() == "failed", "status is not failed");
        Expect(doc.Root.GetProperty("registryRecords").ValueKind == JsonValueKind.Null, "registryRecords not null on failure");
        Expect(doc.Problems.Exists(p => p.Fatal && p.Code == code), "missing fatal " + code + ", got " + FatalCodes(doc));
        foreach (string call in fake.Calls)
        {
            Expect(!call.StartsWith("probe:", StringComparison.Ordinal), "probed a root before rejecting the registry");
        }

        return doc;
    }

    private static string FatalCodes(Doc doc)
    {
        List<string> codes = new List<string>();
        foreach ((string code, string _, bool fatal) in doc.Problems)
        {
            if (fatal)
            {
                codes.Add(code);
            }
        }

        return "[" + string.Join(",", codes) + "]";
    }

    private static void Record(
        Doc doc, string id, string? name, bool installed, bool disabled, bool launchable,
        string package, string? version, string? installRoot, string reason)
    {
        Expect(doc.Records.TryGetValue(id, out JsonElement r), "record " + id + " missing");
        ExpectField(r, id, "Name", name);
        ExpectBool(r, id, "Installed", installed);
        ExpectBool(r, id, "Disabled", disabled);
        ExpectBool(r, id, "Launchable", launchable);
        ExpectField(r, id, "PackageId", package);
        ExpectField(r, id, "Version", version);
        ExpectField(r, id, "InstallRoot", installRoot);
        ExpectField(r, id, "Reason", reason);
        int count = 0;
        foreach (JsonProperty _ in r.EnumerateObject())
        {
            count++;
        }

        Expect(count == 9, id + " has " + count + " fields, expected 9");
    }

    private static void ExpectField(JsonElement r, string id, string field, string? expected)
    {
        JsonElement value = r.GetProperty(field);
        string? actual = value.ValueKind == JsonValueKind.Null ? null : value.GetString();
        Expect(actual == expected, id + "." + field + " = " + (actual ?? "null") + ", expected " + (expected ?? "null"));
    }

    private static void ExpectBool(JsonElement r, string id, string field, bool expected)
    {
        bool actual = r.GetProperty(field).GetBoolean();
        Expect(actual == expected, id + "." + field + " = " + actual + ", expected " + expected);
    }

    private static void Expect(bool condition, string message)
    {
        if (!condition)
        {
            throw new InvalidOperationException(message);
        }
    }

    private static bool HasProblem(Doc doc, string code, string subject) =>
        doc.Problems.Exists(p => p.Code == code && p.Subject == subject);

    // ---- positive inventory ----------------------------------------------------------------

    private static void TestRepresentativeInventory()
    {
        Doc doc = Ok(Representative());
        Expect(doc.Records.Count == 13, "expected 13 records, got " + doc.Records.Count);
        Record(doc, "com.example.alpha", "Alpha", true, false, true, "com.example.alpha", "1.2.3", "/opt/usr/apps/com.example.alpha", "installed");
        Record(doc, "WebPkg0001.Player", "Player", true, false, true, "WebPkg0001", "2.0.0", "/opt/usr/apps/WebPkg0001", "installed");
        Record(doc, "WebPkg0001.Service", null, true, false, false, "WebPkg0001", "2.0.0", "/opt/usr/apps/WebPkg0001", "installed");
        Record(doc, "com.example.appoff", "App Off", true, true, true, "com.example.appoff", "1.0.0", "/opt/usr/apps/com.example.appoff", "installed");
        Record(doc, "com.example.pkgoff", "Package Off", true, true, true, "com.example.pkgoff", "1.0.0", "/opt/usr/apps/com.example.pkgoff", "installed");
        Record(doc, "com.example.gone", "Gone", false, false, true, "com.example.gone", "1.0.0", "/opt/usr/apps/com.example.gone", "root-absent-tombstone");
        Record(doc, "com.example.engine", "Engine", false, false, true, "com.example.engine", "3.1", null, "external-root-refused");
        Record(doc, "com.example.linked", "Linked", false, false, true, "com.example.linked", "1.0.0", "/opt/usr/apps/com.example.linked", "symlink-root-refused");
        Record(doc, "org.example.system", "System", true, false, true, "org.example.system", "5.0.0", "/usr/apps/org.example.system", "installed");
        Record(doc, "com.example.trav", "Trav", false, false, true, "com.example.trav", "1.0.0", null, "root-path-unsafe");
        Record(doc, "com.example.hidden", "Hidden", true, false, false, "com.example.hidden", "1.0.0", "/opt/usr/apps/com.example.hidden", "installed");
        Record(doc, "com.example.noexec", "No Exec", true, false, false, "com.example.noexec", "1.0.0", "/opt/usr/apps/com.example.noexec", "installed");
        Record(doc, "com.example.bidi", null, true, false, true, "com.example.bidi", "1.0.0", "/opt/usr/apps/com.example.bidi", "installed");
        Expect(!doc.Records.ContainsKey("com.example.orphan"), "a label alone created a record");
        Expect(!doc.Problems.Exists(p => p.Fatal), "fatal problem in a valid inventory");

        List<string> order = new List<string>(doc.Records.Keys);
        List<string> sorted = new List<string>(order);
        sorted.Sort(StringComparer.Ordinal);
        Expect(string.Join(",", order) == string.Join(",", sorted), "records not in ordinal Id order");
    }

    private static void TestDeterministicOutput()
    {
        byte[] first = Inspector.Inspect(Representative()).Json;
        byte[] second = Inspector.Inspect(Representative()).Json;
        Expect(first.AsSpan().SequenceEqual(second), "two runs on the same data differ");
    }

    private static void TestRowOrderIndependent()
    {
        string[] apps = RepresentativeApps().Split('\n', StringSplitOptions.RemoveEmptyEntries);
        string[] labels = RepresentativeLabels().Split('\n', StringSplitOptions.RemoveEmptyEntries);
        Array.Reverse(apps);
        Array.Reverse(labels);
        FakeSystem reversed = Representative();
        reversed.Results[FixedCommand.AppQuery] = CommandResult.Ok(Encoding.ASCII.GetBytes(string.Join("\n", apps) + "\n"));
        reversed.Results[FixedCommand.LabelQuery] = CommandResult.Ok(Encoding.ASCII.GetBytes(string.Join("\n", labels) + "\n"));
        Expect(Inspector.Inspect(reversed).Json.AsSpan().SequenceEqual(Inspector.Inspect(Representative()).Json),
            "row order changed the output");
    }

    private static void TestLaunchableDefinition()
    {
        Doc doc = Ok(Representative());
        // Launchable is the registry declaration; it is not folded with Installed or Disabled.
        Record(doc, "com.example.gone", "Gone", false, false, true, "com.example.gone", "1.0.0", "/opt/usr/apps/com.example.gone", "root-absent-tombstone");
        Record(doc, "com.example.hidden", "Hidden", true, false, false, "com.example.hidden", "1.0.0", "/opt/usr/apps/com.example.hidden", "installed");
        Record(doc, "com.example.noexec", "No Exec", true, false, false, "com.example.noexec", "1.0.0", "/opt/usr/apps/com.example.noexec", "installed");
    }

    private static void TestComponentDifference()
    {
        string apps = App("pkg.ui", "uiapp", "/usr/bin/wrt", "false", "false", "/opt/usr/apps/pkg", "pkg", "/opt/usr/apps/pkg", "false", "1") +
                      App("pkg.svc", "svcapp", "/usr/bin/wrt", "false", "false", "/opt/usr/apps/pkg", "pkg", "/opt/usr/apps/pkg", "false", "1") +
                      App("pkg.widget", "widgetapp", "/usr/bin/wrt", "false", "false", "/opt/usr/apps/pkg", "pkg", "/opt/usr/apps/pkg", "false", "1");
        FakeSystem fake = Fake(apps, Label("pkg.ui", "en-us", "UI") + Label("pkg.svc", "en-us", "Svc") + Label("pkg.widget", "en-us", "W"));
        fake.Roots["/opt/usr/apps/pkg"] = RootKind.Directory;
        Doc doc = Ok(fake);
        Record(doc, "pkg.ui", "UI", true, false, true, "pkg", "1", "/opt/usr/apps/pkg", "installed");
        Record(doc, "pkg.svc", "Svc", true, false, false, "pkg", "1", "/opt/usr/apps/pkg", "installed");
        Record(doc, "pkg.widget", "W", true, false, false, "pkg", "1", "/opt/usr/apps/pkg", "installed");
        int probes = fake.Calls.FindAll(c => c == "probe:/opt/usr/apps/pkg").Count;
        Expect(probes == 1, "shared package root probed " + probes + " times");
    }

    private static void TestDisabledFlags()
    {
        Doc doc = Ok(Representative());
        Expect(doc.Records["com.example.appoff"].GetProperty("Disabled").GetBoolean(), "app_disable=true not disabled");
        Expect(doc.Records["com.example.pkgoff"].GetProperty("Disabled").GetBoolean(), "package_disable=true not disabled");
        Expect(!doc.Records["com.example.alpha"].GetProperty("Disabled").GetBoolean(), "enabled app reported disabled");
    }

    private static void TestTombstone()
    {
        FakeSystem fake = Fake(UiApp("com.example.gone", "com.example.gone"), Label("com.example.gone", "en-us", "Gone"));
        Doc doc = Ok(fake);
        Record(doc, "com.example.gone", "Gone", false, false, true, "com.example.gone", "1.0.0", "/opt/usr/apps/com.example.gone", "root-absent-tombstone");
    }

    private static void TestSymlinkRoot()
    {
        FakeSystem fake = Fake(UiApp("com.example.linked", "com.example.linked"), Label("com.example.linked", "en-us", "Linked"));
        fake.Roots["/opt/usr/apps/com.example.linked"] = RootKind.Symlink;
        Doc doc = Ok(fake);
        Record(doc, "com.example.linked", "Linked", false, false, true, "com.example.linked", "1.0.0", "/opt/usr/apps/com.example.linked", "symlink-root-refused");
    }

    private static void TestExternalRoot()
    {
        string apps =
            App("com.example.wrt", "uiapp", "/usr/bin/wrt", "false", "false", "/usr/bin/wrt", "com.example.wrt", "/usr/bin/wrt", "false", "1") +
            App("com.example.engine", "uiapp", "/usr/bin/aria-engine", "false", "false", "/usr/share/aria-engine", "com.example.engine", "/usr/share/aria-engine", "false", "1") +
            // Another package's root is not this package's install.
            App("com.example.borrow", "uiapp", "/x/y", "false", "false", "/opt/usr/apps/com.example.alpha", "com.example.borrow", "/opt/usr/apps/com.example.alpha", "false", "1") +
            // Trailing slash and nested paths are not the strict root.
            App("com.example.slash", "uiapp", "/x/y", "false", "false", null, "com.example.slash", "/opt/usr/apps/com.example.slash/", "false", "1") +
            App("com.example.deep", "uiapp", "/x/y", "false", "false", null, "com.example.deep", "/opt/usr/apps/com.example.deep/sub", "false", "1");
        FakeSystem fake = Fake(apps, string.Empty);
        foreach (string root in new[] { "/usr/bin/wrt", "/usr/share/aria-engine", "/opt/usr/apps/com.example.alpha", "/opt/usr/apps/com.example.deep/sub" })
        {
            fake.Roots[root] = RootKind.Directory;
        }

        Doc doc = Ok(fake);
        foreach (string id in new[] { "com.example.wrt", "com.example.engine", "com.example.borrow", "com.example.deep" })
        {
            Expect(doc.Records[id].GetProperty("Reason").GetString() == "external-root-refused", id + " not refused as external");
            Expect(!doc.Records[id].GetProperty("Installed").GetBoolean(), id + " installed from an external root");
            Expect(doc.Records[id].GetProperty("InstallRoot").ValueKind == JsonValueKind.Null, id + " reported an external InstallRoot");
        }

        Expect(doc.Records["com.example.slash"].GetProperty("Reason").GetString() == "root-path-unsafe", "trailing slash root accepted");
        foreach (string call in fake.Calls)
        {
            Expect(!call.StartsWith("probe:", StringComparison.Ordinal), "external root was probed: " + call);
        }
    }

    private static void TestPathTraversal()
    {
        string[] hostile =
        {
            "/opt/usr/apps/../../etc", "/opt/usr/apps/./com.example.t", "/opt/usr//apps/com.example.t",
            "opt/usr/apps/com.example.t", "\\opt\\usr\\apps\\com.example.t", "/opt/usr/apps/com.example.t\n/etc",
            "/opt/usr/apps/com.example.t\u0000", "/", "/opt/usr/apps/com.example.t/..",
        };
        foreach (string root in hostile)
        {
            FakeSystem fake = Fake(
                App("com.example.t", "uiapp", "/x/y", "false", "false", null, "com.example.t", root, "false", "1"), string.Empty);
            fake.Roots[root] = RootKind.Directory;
            Doc doc = Ok(fake);
            JsonElement r = doc.Records["com.example.t"];
            Expect(!r.GetProperty("Installed").GetBoolean(), "hostile root installed: " + JsonSerializer.Serialize(root));
            Expect(r.GetProperty("Reason").GetString() == "root-path-unsafe", "hostile root reason: " + r.GetProperty("Reason").GetString());
            Expect(!fake.Calls.Exists(c => c.StartsWith("probe:", StringComparison.Ordinal)), "hostile root probed");
        }
    }

    private static void TestRootNotDirectory()
    {
        FakeSystem fake = Fake(UiApp("com.example.f", "com.example.f"), string.Empty);
        fake.Roots["/opt/usr/apps/com.example.f"] = RootKind.NotDirectory;
        Doc doc = Ok(fake);
        Expect(doc.Records["com.example.f"].GetProperty("Reason").GetString() == "root-not-directory", "file root accepted");
        fake.Roots["/opt/usr/apps/com.example.f"] = RootKind.Unreadable;
        doc = Ok(fake);
        Expect(doc.Records["com.example.f"].GetProperty("Reason").GetString() == "root-unreadable", "unreadable root accepted");
        Expect(!doc.Records["com.example.f"].GetProperty("Installed").GetBoolean(), "unreadable root installed");
    }

    private static void TestAppRootMismatch()
    {
        string apps = App("com.example.m", "uiapp", "/x/y", "false", "false", "/opt/usr/apps/other",
            "com.example.m", "/opt/usr/apps/com.example.m", "false", "1");
        FakeSystem fake = Fake(apps, string.Empty);
        fake.Roots["/opt/usr/apps/com.example.m"] = RootKind.Directory;
        Doc doc = Ok(fake);
        Expect(doc.Records["com.example.m"].GetProperty("Reason").GetString() == "app-root-mismatch", "app/package root mismatch accepted");
        Expect(!doc.Records["com.example.m"].GetProperty("Installed").GetBoolean(), "mismatched metadata installed");

        FakeSystem missing = Fake(App("com.example.n", "uiapp", "/x/y", "false", "false", null, "com.example.n", null, "false", "1"), string.Empty);
        doc = Ok(missing);
        Expect(doc.Records["com.example.n"].GetProperty("Reason").GetString() == "registry-root-missing", "null root accepted");
    }

    private static void TestProbesOnlyStrictRoots()
    {
        FakeSystem fake = Representative();
        Ok(fake);
        foreach (string call in fake.Calls)
        {
            if (!call.StartsWith("probe:", StringComparison.Ordinal))
            {
                continue;
            }

            string root = call.Substring(6);
            bool strict = (root.StartsWith("/opt/usr/apps/", StringComparison.Ordinal) || root.StartsWith("/usr/apps/", StringComparison.Ordinal)) &&
                          Rules.IsSafeAbsolute(root) && root.Split('/').Length == (root.StartsWith("/opt/", StringComparison.Ordinal) ? 5 : 4);
            Expect(strict, "probed a non-strict root " + root);
        }
    }

    // ---- labels, versions, exec --------------------------------------------------------------

    private static void TestLocalePreference()
    {
        string apps = UiApp("a.en", "a.en") + UiApp("a.enus", "a.enus") + UiApp("a.none", "a.none") + UiApp("a.ord", "a.ord") + UiApp("a.nl", "a.nl");
        string labels =
            Label("a.en", "No Locale", "Default") + Label("a.en", "en-us", "English") + Label("a.en", "aa-aa", "First") +
            Label("a.enus", "en_US", "Underscore") + Label("a.enus", "No Locale", "Default") +
            Label("a.none", "No Locale", "Default") + Label("a.none", "de-de", "German") +
            Label("a.ord", "zh-cn", "Chinese") + Label("a.ord", "fr-fr", "French") + Label("a.ord", "ko-kr", "Korean") +
            Label("a.nl", "NoLocale", "Compact") + Label("a.nl", "fr-fr", "French");
        Doc doc = Ok(Fake(apps, labels));
        Expect(doc.Records["a.en"].GetProperty("Name").GetString() == "English", "en-us not preferred");
        Expect(doc.Records["a.enus"].GetProperty("Name").GetString() == "Underscore", "en_US not preferred");
        Expect(doc.Records["a.none"].GetProperty("Name").GetString() == "Default", "No Locale not second");
        Expect(doc.Records["a.ord"].GetProperty("Name").GetString() == "French", "ordinal fallback not used");
        Expect(doc.Records["a.nl"].GetProperty("Name").GetString() == "Compact", "NoLocale spelling not accepted");
    }

    private static void TestUnsafeLabelHeld()
    {
        string[] unsafeLabels =
        {
            "Evil‮gnp.exe", "Tab\tName", "Line\nBreak", "Nul\u0000", "Zero​Width", " Leading", "Trailing ",
            "Del\u007F", "C1\u0085", "Private", "Repl�", new string('x', InspectContract.MaxLabelChars + 1), "",
        };
        foreach (string bad in unsafeLabels)
        {
            FakeSystem fake = Fake(UiApp("com.example.l", "com.example.l"),
                Label("com.example.l", "en-us", bad) + Label("com.example.l", "No Locale", "Safe"));
            Doc doc = Ok(fake);
            Expect(doc.Records["com.example.l"].GetProperty("Name").ValueKind == JsonValueKind.Null,
                "unsafe label emitted or fell back: " + JsonSerializer.Serialize(bad));
            Expect(HasProblem(doc, "label-unsafe", "com.example.l"), "unsafe label has no problem entry");
        }

        Doc ok = Ok(Fake(UiApp("com.example.l", "com.example.l"), Label("com.example.l", "en-us", "Café 🎬 TV")));
        Expect(ok.Records["com.example.l"].GetProperty("Name").GetString() == "Café 🎬 TV", "safe non-ASCII label rejected");
    }

    private static void TestLabelMissing()
    {
        Doc doc = Ok(Fake(UiApp("com.example.l", "com.example.l"), string.Empty));
        Expect(doc.Records["com.example.l"].GetProperty("Name").ValueKind == JsonValueKind.Null, "missing label not null");
        Expect(HasProblem(doc, "label-missing", "com.example.l"), "missing label has no problem entry");
        doc = Ok(Fake(UiApp("com.example.l", "com.example.l"), Label("com.example.l", "en-us", null)));
        Expect(HasProblem(doc, "label-unsafe", "com.example.l"), "NULL label not held");
    }

    private static void TestVersionUnsafe()
    {
        string apps = UiApp("a.bad", "a.bad", version: "1.0\n2") +
                      App("a.null", "uiapp", "/x/y", "false", "false", null, "a.null", "/opt/usr/apps/a.null", "false", null);
        Doc doc = Ok(Fake(apps, string.Empty));
        Expect(doc.Records["a.bad"].GetProperty("Version").ValueKind == JsonValueKind.Null, "unsafe version emitted");
        Expect(HasProblem(doc, "version-unsafe", "a.bad"), "unsafe version has no problem");
        Expect(doc.Records["a.null"].GetProperty("Version").ValueKind == JsonValueKind.Null, "null version guessed");
        Expect(HasProblem(doc, "version-missing", "a.null"), "null version has no problem");
    }

    private static void TestExecUnsafe()
    {
        string apps = App("a.x", "uiapp", "/usr/bin/../../tmp/x", "false", "false", null, "a.x", "/opt/usr/apps/a.x", "false", "1") +
                      App("a.y", "uiapp", "relative/exec", "false", "false", null, "a.y", "/opt/usr/apps/a.y", "false", "1");
        Doc doc = Ok(Fake(apps, string.Empty));
        // Launchable follows the contract (non-empty exec); the unsafe exec is flagged, never used.
        Expect(doc.Records["a.x"].GetProperty("Launchable").GetBoolean(), "Launchable stricter than the contract");
        Expect(doc.Records["a.y"].GetProperty("Launchable").GetBoolean(), "Launchable stricter than the contract");
        Expect(HasProblem(doc, "exec-unsafe", "a.x"), "unsafe exec has no problem");
        Expect(HasProblem(doc, "exec-unsafe", "a.y"), "relative exec has no problem");
        Expect(Encoding.UTF8.GetString(Inspector.Inspect(Fake(apps, string.Empty)).Json).IndexOf("tmp/x", StringComparison.Ordinal) < 0,
            "exec value leaked into output");
    }

    // ---- fatal registry problems -------------------------------------------------------------

    private static void TestTableUnsupported()
    {
        FakeSystem fake = Representative();
        fake.Results[FixedCommand.AppQuery] = CommandResult.Failed(CommandStatus.NonZeroExit, "table-unsupported");
        Failed(fake, "table-unsupported");
        fake = Representative();
        fake.Results[FixedCommand.LabelQuery] = CommandResult.Failed(CommandStatus.NonZeroExit, "column-unsupported");
        Failed(fake, "column-unsupported");
    }

    private static void TestQueryFailures()
    {
        (CommandStatus Status, string Code)[] cases =
        {
            (CommandStatus.NotFound, "sqlite3-unavailable"),
            (CommandStatus.StartFailed, "query-start-failed"),
            (CommandStatus.TimedOut, "query-timeout"),
            (CommandStatus.OutputTooLarge, "query-output-oversize"),
        };
        foreach ((CommandStatus status, string code) in cases)
        {
            FakeSystem fake = Representative();
            fake.Results[FixedCommand.AppQuery] = CommandResult.Failed(status);
            Failed(fake, code);
            fake = Representative();
            fake.Results[FixedCommand.LabelQuery] = CommandResult.Failed(status);
            Failed(fake, code);
        }

        FakeSystem plain = Representative();
        plain.Results[FixedCommand.LabelQuery] = CommandResult.Failed(CommandStatus.NonZeroExit);
        Failed(plain, "query-failed");
    }

    private static void TestMalformedFlag()
    {
        foreach (string?[] flags in new[]
                 {
                     new string?[] { "TRUE", "false", "false" }, new string?[] { "false", "1", "false" },
                     new string?[] { "false", "false", null }, new string?[] { "", "false", "false" },
                     new string?[] { "false", " true", "false" }, new string?[] { "false", "false", "False" },
                 })
        {
            string apps = UiApp("com.example.alpha", "com.example.alpha") +
                          App("a.flag", "uiapp", "/x/y", flags[0], flags[1], null, "a.flag", "/opt/usr/apps/a.flag", flags[2], "1");
            Failed(Fake(apps, string.Empty), "malformed-flag");
        }
    }

    private static void TestUnknownFlag()
    {
        foreach (string unknown in new[] { "yes", "no", "0", "enabled", "truefalse" })
        {
            string apps = App("a.flag", "uiapp", "/x/y", "false", unknown, null, "a.flag", "/opt/usr/apps/a.flag", "false", "1");
            Failed(Fake(apps, string.Empty), "malformed-flag");
        }
    }

    private static void TestDuplicateApp()
    {
        string apps = UiApp("com.example.alpha", "com.example.alpha") + UiApp("com.example.beta", "com.example.beta") +
                      UiApp("com.example.alpha", "com.example.alpha", version: "9.9");
        Failed(Fake(apps, string.Empty), "duplicate-app");
    }

    private static void TestCaseClashApp()
    {
        string apps = UiApp("com.example.alpha", "pkg.one") + UiApp("com.Example.Alpha", "pkg.two");
        Failed(Fake(apps, string.Empty), "case-clash-app");
    }

    private static void TestCaseClashPackage()
    {
        string apps = UiApp("a.one", "com.example.pkg") + UiApp("a.two", "com.Example.PKG");
        Failed(Fake(apps, string.Empty), "case-clash-package");
    }

    private static void TestUnexpectedRowKey()
    {
        string?[] badIds = { null, "", "../etc", "a b", "a/b", ".hidden", "a..b", "a\u0000b", new string('a', 129), "éclair" };
        foreach (string? id in badIds)
        {
            string apps = App(id, "uiapp", "/x/y", "false", "false", null, "pkg", "/opt/usr/apps/pkg", "false", "1");
            Failed(Fake(apps, string.Empty), "unexpected-row-key");
            string badPackage = App("a.ok", "uiapp", "/x/y", "false", "false", null, id, "/opt/usr/apps/pkg", "false", "1");
            Failed(Fake(badPackage, string.Empty), "unexpected-row-key");
            Failed(Fake(UiApp("a.ok", "a.ok"), Label(id, "en-us", "X")), "unexpected-row-key");
        }
    }

    private static void TestMalformedOutput()
    {
        string good = UiApp("a.ok", "a.ok");
        string[] bad =
        {
            good.TrimEnd('\n'),                         // no final newline
            good.Substring(0, good.LastIndexOf('|')) + "\n", // nine fields
            "XAB|" + good,                              // extra field
            good.Replace("X", "x", StringComparison.Ordinal), // lowercase marker
            good.ToLowerInvariant(),                    // lowercase hex
            "X6|" + good.Substring(good.IndexOf('|') + 1), // odd hex
            "XFF|" + good.Substring(good.IndexOf('|') + 1), // invalid UTF-8
            good + "\n",                                // empty line
            good.Replace("\n", "\r\n", StringComparison.Ordinal), // CR
            "a.ok|uiapp|/x|false|false|N|a.ok|/opt/usr/apps/a.ok|false|1\n", // raw text
            "",                                         // empty field placeholder replaced below
        };
        bad[bad.Length - 1] = "|" + good.Substring(good.IndexOf('|') + 1);
        foreach (string output in bad)
        {
            Failed(Fake(output, string.Empty), "malformed-output");
        }

        Failed(Fake(good, Label("a.ok", "en-us", "A").TrimEnd('\n')), "malformed-output");
        Failed(Fake(good, "X41|N\n"), "malformed-output");
    }

    private static void TestRowCountOversize()
    {
        StringBuilder apps = new StringBuilder();
        for (int i = 0; i <= InspectContract.MaxApps; i++)
        {
            apps.Append(UiApp("a.n" + i.ToString("D5", CultureInfo.InvariantCulture), "p.n" + i.ToString("D5", CultureInfo.InvariantCulture)));
        }

        Failed(Fake(apps.ToString(), string.Empty), "row-count-oversize");

        StringBuilder exact = new StringBuilder();
        for (int i = 0; i < InspectContract.MaxApps; i++)
        {
            exact.Append(UiApp("a.n" + i.ToString("D5", CultureInfo.InvariantCulture), "p.n" + i.ToString("D5", CultureInfo.InvariantCulture)));
        }

        Doc doc = Ok(Fake(exact.ToString(), string.Empty));
        Expect(doc.Records.Count == InspectContract.MaxApps, "row limit rejected an inventory at the limit");
    }

    private static void TestOutputOversize()
    {
        FakeSystem fake = Representative();
        fake.Results[FixedCommand.AppQuery] = CommandResult.Failed(CommandStatus.OutputTooLarge);
        Failed(fake, "query-output-oversize");
    }

    private static void TestDuplicateLabel()
    {
        string apps = UiApp("a.ok", "a.ok");
        Failed(Fake(apps, Label("a.ok", "en-us", "A") + Label("a.ok", "en-us", "B")), "duplicate-label");
        Failed(Fake(apps, Label("a.ok", "en-us", "A") + Label("a.ok", "EN-US", "B")), "case-clash-label");
        Failed(Fake(apps, Label("a.ok", null, "A")), "malformed-label-row");
        Failed(Fake(apps, Label("a.ok", "en-us;drop", "A")), "malformed-label-row");
    }

    private static void TestRegistryEmpty()
    {
        Failed(Fake(string.Empty, string.Empty), "registry-empty");
    }

    private static void TestMalformedComponent()
    {
        foreach (string? component in new[] { null, "", "UIAPP", "ui app", "uiapp\n" })
        {
            string apps = App("a.c", component, "/x/y", "false", "false", null, "a.c", "/opt/usr/apps/a.c", "false", "1");
            Failed(Fake(apps, string.Empty), "malformed-field");
        }
    }

    // ---- runtime evidence ------------------------------------------------------------------

    private static void TestRuntimeEvidence()
    {
        Doc doc = Ok(Representative());
        JsonElement runtime = doc.Root.GetProperty("observedRuntime");
        Expect(runtime.GetProperty("tizenVersion").GetString() == "6.5", "tizen version not read");
        Expect(runtime.GetProperty("machine").GetString() == "armv7l", "machine not read");
        Expect(runtime.GetProperty("tizenRelease").GetProperty("path").GetString() == "/etc/tizen-release", "release path");
        Expect(runtime.GetProperty("tizenRelease").GetProperty("lines").GetArrayLength() == 3, "release lines");
        foreach (string field in new[] { "model", "firmware", "region" })
        {
            Expect(runtime.GetProperty(field).ValueKind == JsonValueKind.Null, field + " was guessed");
            Expect(HasProblem(doc, field + "-not-observed", "observedRuntime"), field + " has no problem entry");
        }
    }

    private static void TestRuntimeMissing()
    {
        FakeSystem fake = Representative();
        fake.Release = new FileReadResult(ReadStatus.Missing, Array.Empty<byte>());
        fake.Results.Remove(FixedCommand.UnameMachine);
        Doc doc = Ok(fake);
        JsonElement runtime = doc.Root.GetProperty("observedRuntime");
        Expect(runtime.GetProperty("tizenRelease").ValueKind == JsonValueKind.Null, "missing release not null");
        Expect(runtime.GetProperty("tizenVersion").ValueKind == JsonValueKind.Null, "missing version not null");
        Expect(runtime.GetProperty("machine").ValueKind == JsonValueKind.Null, "missing machine not null");
        Expect(HasProblem(doc, "tizen-release-missing", "/etc/tizen-release"), "no release problem");
        Expect(HasProblem(doc, "uname-unavailable", "uname -m"), "no uname problem");

        fake.Release = new FileReadResult(ReadStatus.TooLarge, Array.Empty<byte>());
        fake.Results[FixedCommand.UnameMachine] = CommandResult.Failed(CommandStatus.TimedOut);
        doc = Ok(fake);
        Expect(HasProblem(doc, "tizen-release-oversize", "/etc/tizen-release"), "no oversize problem");
        Expect(HasProblem(doc, "machine-not-observed", "uname -m"), "no machine problem");

        fake.Release = new FileReadResult(ReadStatus.Ok, Encoding.ASCII.GetBytes("Tizen\nVERSION = 6.5\nVERSION = 7.0\n"));
        doc = Ok(fake);
        Expect(doc.Root.GetProperty("observedRuntime").GetProperty("tizenVersion").ValueKind == JsonValueKind.Null, "ambiguous version picked");
    }

    private static void TestRuntimeUnsafe()
    {
        FakeSystem fake = Representative();
        fake.Release = new FileReadResult(ReadStatus.Ok, Encoding.UTF8.GetBytes("Tizen \u001B[31m\nVERSION = 6.5\n"));
        fake.Results[FixedCommand.UnameMachine] = CommandResult.Ok(Encoding.UTF8.GetBytes("arm v7é\n"));
        Doc doc = Ok(fake);
        JsonElement runtime = doc.Root.GetProperty("observedRuntime");
        Expect(runtime.GetProperty("tizenRelease").ValueKind == JsonValueKind.Null, "unsafe release emitted");
        Expect(runtime.GetProperty("machine").ValueKind == JsonValueKind.Null, "unsafe machine emitted");
        Expect(HasProblem(doc, "tizen-release-unsafe", "/etc/tizen-release"), "no unsafe release problem");
        Expect(HasProblem(doc, "machine-unsafe", "uname -m"), "no unsafe machine problem");
    }

    // ---- adapters ---------------------------------------------------------------------------

    private static void TestStderrClassifier()
    {
        (string Stderr, string Code)[] cases =
        {
            ("Error: in prepare, no such table: package_app_info (1)", "table-unsupported"),
            ("Error: no such column: a.app_nodisplay", "column-unsupported"),
            ("Error: unable to open database \"/opt/dbspace/.pkgmgr_parser.db\": unable to open", "database-open-failed"),
            ("Error: file is not a database", "database-open-failed"),
            ("sqlite3: Error: unknown option: -readonly", "sqlite3-option-unsupported"),
            ("Error: /home/owner/apps_rw/secret/token.txt: weird", "query-failed"),
        };
        foreach ((string stderr, string code) in cases)
        {
            string actual = StderrClassifier.Classify(Encoding.ASCII.GetBytes(stderr));
            Expect(actual == code, "stderr classified as " + actual + ", expected " + code);
        }

        FakeSystem fake = Representative();
        fake.Results[FixedCommand.AppQuery] = CommandResult.Failed(
            CommandStatus.NonZeroExit, StderrClassifier.Classify(Encoding.ASCII.GetBytes("Error: /home/owner/apps_rw/secret/token.txt")));
        InspectionOutcome outcome = Inspector.Inspect(fake);
        string json = Encoding.UTF8.GetString(outcome.Json);
        Expect(json.IndexOf("/home/owner", StringComparison.Ordinal) < 0, "stderr path leaked into output");
    }

    private static void TestBoundedCapture()
    {
        (byte[] bytes, bool overflow) = BoundedCapture.Read(new MemoryStream(new byte[100]), 100);
        Expect(!overflow && bytes.Length == 100, "capture at cap rejected");
        (bytes, overflow) = BoundedCapture.Read(new MemoryStream(new byte[101]), 100);
        Expect(overflow && bytes.Length == 0, "capture over cap accepted");
        (bytes, overflow) = BoundedCapture.Read(new MemoryStream(new byte[40000]), 39999);
        Expect(overflow, "multi-chunk overflow missed");
        (bytes, overflow) = BoundedCapture.Read(new MemoryStream(Array.Empty<byte>()), 10);
        Expect(!overflow && bytes.Length == 0, "empty capture");
    }

    private static void TestPlatformRefusal()
    {
        InspectionOutcome outcome = Inspector.PlatformRefusal();
        Doc doc = Parse(outcome);
        Expect(!outcome.Ok, "refusal reported ok");
        Expect(doc.Root.GetProperty("status").GetString() == "failed", "refusal status");
        Expect(doc.Root.GetProperty("registryRecords").ValueKind == JsonValueKind.Null, "refusal has records");
        Expect(doc.Problems.Exists(p => p.Fatal && p.Code == "platform-unsupported"), "refusal code");
        Expect(Parse(Inspector.InternalError()).Problems.Exists(p => p.Fatal && p.Code == "internal-error"), "internal error code");
    }

    private static void TestNoWriteSurface()
    {
        FakeSystem fake = Representative();
        Ok(fake);
        foreach (string call in fake.Calls)
        {
            bool allowed = call.StartsWith("probe:", StringComparison.Ordinal) || call == "read:tizen-release" ||
                           call == "run:AppQuery" || call == "run:LabelQuery" || call == "run:UnameMachine";
            Expect(allowed, "unexpected system call " + call);
        }

        Expect(fake.Calls.FindAll(c => c.StartsWith("run:", StringComparison.Ordinal)).Count == 3, "expected exactly 3 fixed commands");
        foreach (MethodInfo method in typeof(IInspectSystem).GetMethods())
        {
            Expect(method.Name == "Run" || method.Name == "ReadTizenRelease" || method.Name == "ProbeRoot",
                "system seam grew a member " + method.Name);
        }

        Expect(InspectContract.AppQuery.StartsWith("SELECT ", StringComparison.Ordinal) &&
               InspectContract.LabelQuery.StartsWith("SELECT ", StringComparison.Ordinal), "queries must be SELECT");
        foreach (string sql in new[] { InspectContract.AppQuery, InspectContract.LabelQuery })
        {
            string upper = sql.ToUpperInvariant();
            foreach (string word in new[] { "INSERT", "UPDATE", "DELETE", "DROP", "ATTACH", "PRAGMA", "CREATE", "REPLACE", "VACUUM", "ALTER" })
            {
                Expect(upper.IndexOf(word, StringComparison.Ordinal) < 0, "query contains " + word);
            }

            Expect(sql.IndexOf(';') == sql.Length - 1, "query has more than one statement");
        }
    }

    // ---- framed output ------------------------------------------------------------------------

    // Independent reader of one frame: header line, exact length, hash, end tag, nothing extra.
    private static byte[] Unframe(byte[] frame)
    {
        int newline = Array.IndexOf(frame, (byte)'\n');
        Expect(newline > 0, "frame has no header line");
        string header = Encoding.ASCII.GetString(frame, 0, newline);
        string[] parts = header.Split(' ');
        Expect(parts.Length == 3 && parts[0] == "QUIETTV-FRAME/1", "frame header shape: " + header);
        Expect(parts[1] == "0" || (parts[1].Length > 0 && parts[1][0] != '0'), "frame length has leading zeros");
        int length = int.Parse(parts[1], NumberStyles.None, CultureInfo.InvariantCulture);
        byte[] payload = new byte[length];
        Expect(frame.Length == newline + 1 + length + "\nQUIETTV-FRAME-END/1\n".Length, "frame total length");
        Array.Copy(frame, newline + 1, payload, 0, length);
        string trailer = Encoding.ASCII.GetString(frame, newline + 1 + length, frame.Length - newline - 1 - length);
        Expect(trailer == "\nQUIETTV-FRAME-END/1\n", "frame end tag");
        string hash = Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(payload)).ToLowerInvariant();
        Expect(parts[2] == hash, "frame hash");
        return payload;
    }

    private static void TestFrameRoundtrip()
    {
        InspectionOutcome outcome = Inspector.Inspect(Representative());
        byte[] frame = Frame.ForOutcome(outcome, out bool ok);
        Expect(ok, "valid outcome not framed");
        byte[] payload = Unframe(frame);
        Expect(payload.AsSpan().SequenceEqual(outcome.Json.AsSpan(0, outcome.Json.Length - 1)), "payload is not the document");
        Expect(Array.IndexOf(payload, (byte)'\n') < 0, "payload is not one line");
        Doc doc = Parse(new InspectionOutcome(true, payload));
        Expect(doc.Records.Count == 13, "framed document lost records");
        Expect(Frame.ForOutcome(Inspector.Inspect(Representative()), out _).AsSpan().SequenceEqual(frame), "framing not deterministic");

        byte[] refusal = Frame.ForOutcome(Inspector.PlatformRefusal(), out bool refusalOk);
        Expect(refusalOk, "refusal document not framed");
        Expect(Parse(new InspectionOutcome(false, Unframe(refusal))).Problems.Exists(p => p.Code == "platform-unsupported"),
            "framed refusal lost its problem");
    }

    private static void TestFrameOneLinePayload()
    {
        foreach (string bad in new[] { "{\"a\":1}\n", "{\"a\":\n1}", "{\"a\":1}\r", "\n" })
        {
            bool threw = false;
            try
            {
                Frame.Encode(Encoding.ASCII.GetBytes(bad));
            }
            catch (InvalidOperationException)
            {
                threw = true;
            }

            Expect(threw, "multi-line payload framed: " + JsonSerializer.Serialize(bad));
        }
    }

    private static void TestFrameSizeCap()
    {
        foreach (int size in new[] { 0, Frame.MaxPayloadBytes + 1 })
        {
            bool threw = false;
            try
            {
                Frame.Encode(new byte[size]);
            }
            catch (InvalidOperationException)
            {
                threw = true;
            }

            Expect(threw, "payload of " + size + " bytes framed");
        }

        byte[] atCap = new byte[Frame.MaxPayloadBytes];
        Array.Fill(atCap, (byte)'x');
        Expect(Unframe(Frame.Encode(atCap)).Length == Frame.MaxPayloadBytes, "payload at the cap rejected");
    }

    private static void TestFrameOversizeFailsClosed()
    {
        byte[] huge = new byte[Frame.MaxPayloadBytes + 2];
        Array.Fill(huge, (byte)'x');
        huge[huge.Length - 1] = (byte)'\n';
        byte[] frame = Frame.ForOutcome(new InspectionOutcome(true, huge), out bool ok);
        Expect(!ok, "oversize document reported ok");
        Doc doc = Parse(new InspectionOutcome(false, Unframe(frame)));
        Expect(doc.Root.GetProperty("status").GetString() == "failed", "oversize not failed");
        Expect(doc.Root.GetProperty("registryRecords").ValueKind == JsonValueKind.Null, "oversize kept records");
        Expect(doc.Problems.Exists(p => p.Fatal && p.Code == "output-oversize"), "oversize code missing");

        byte[] multiLine = Encoding.ASCII.GetBytes("{\"a\":\n1}\n");
        frame = Frame.ForOutcome(new InspectionOutcome(true, multiLine), out ok);
        Expect(!ok, "unframeable document reported ok");
        Expect(Parse(new InspectionOutcome(false, Unframe(frame))).Problems.Exists(p => p.Fatal && p.Code == "internal-error"),
            "unframeable document not replaced by a failure");
    }
}

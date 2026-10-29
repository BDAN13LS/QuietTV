using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace XattrBackup;

internal static class SelfTests
{
    private static int _passed;
    private static int _failed;
    private static int _skipped;

    internal static int Run()
    {
        Console.WriteLine("self-test: running (no native xattr calls on this platform)");

        Check("hostile-relative-paths", TestHostilePaths);
        Check("manifest-duplicate-keys", TestDuplicateJsonKeys);
        Check("manifest-duplicate-xattr", TestDuplicateXattrName);
        Check("manifest-duplicate-path", TestDuplicateEntryPath);
        Check("manifest-version-and-shape", TestManifestShape);
        Check("manifest-entry-count-bound", TestEntryCountBound);
        Check("base64-validation", TestBase64Validation);
        Check("xattr-entry-bound", TestXattrEntryBound);
        Check("manifest-size-bound", TestManifestSizeBound);
        Check("linux-privilege-parser", TestPrivilegeParser);
        Check("dump-json-binary-roundtrip", TestDumpRoundtrip);
        Check("dump-stream-matches-file", TestDumpToStreamMatchesFile);
        Check("dump-stream-output-guards", TestDumpToStreamOutputGuards);
        Check("dump-stream-write-failure", TestDumpToStreamWriteFailure);
        Check("dump-raw-xattr-aggregate-bound", TestRawXattrAggregateBound);
        Check("restore-synchronizes-exactly", TestRestoreSynchronizes);
        Check("restore-removes-kernel-created-extra", TestRestoreRemovesKernelCreatedExtra);
        Check("restore-reapplies-kernel-overwritten-expected", TestRestoreReappliesKernelOverwrittenExpected);
        Check("restore-reconcile-bounded-failure", TestReconcileGivesUpOnNonConvergingSideEffects);
        Check("preflight-content-mismatch-no-writes", TestContentMismatchRejectsWholePreflight);
        Check("preflight-count-preserving-rename", TestCountPreservingRenameRejects);
        Check("preflight-membership", TestMembershipMismatch);
        Check("preflight-kind-mismatch", TestKindMismatch);
        Check("preflight-link-target-mismatch", TestLinkTargetMismatch);
        Check("root-basename-guard", TestRootBasenameGuard);
        Check("root-guards", TestRootGuards);
        Check("manifest-inside-root", TestManifestInsideRoot);
        Check("atomic-manifest-write", TestAtomicWriteLeavesNoTemp);

        if (!OperatingSystem.IsLinux())
        {
            Check("non-linux-cli-refuses-cleanly", TestNonLinuxCliRefuses);
        }

        Console.WriteLine("self-test: passed=" + _passed + " failed=" + _failed + " skipped=" + _skipped);
        return _failed == 0 ? 0 : 1;
    }

    private static void TestHostilePaths()
    {
        string[] allowed = { ".", "a", "a/b", "a b", "ab:c", "ümlaut/файл" };
        foreach (string path in allowed)
        {
            PathRules.ValidateManifestRelativePath(path, allowRoot: true, "test");
        }

        string[] rejected =
        {
            "",
            "/etc/passwd",
            "//server/share",
            "\\server\\share",
            "C:\\x",
            "C:/x",
            "C:relative",
            "a:b",
            "a\\b",
            "./a",
            "a/./b",
            "../x",
            "a/../b",
            "a//b",
            "a/",
            "a\u0000b",
            "a\nb",
        };

        foreach (string path in rejected)
        {
            ExpectThrows(() => PathRules.ValidateManifestRelativePath(path, allowRoot: true, "test"), null);
        }

        ExpectThrows(() => PathRules.ValidateManifestRelativePath(".", allowRoot: false, "test"), null);
    }

    private static void TestDuplicateJsonKeys()
    {
        string raw = "{\"version\":1,\"version\":1,\"rootBasename\":\"app\",\"entries\":[]}";
        ExpectThrows(() => ManifestCodec.Parse(Encoding.UTF8.GetBytes(raw)), "duplicate");
    }

    private static void TestDuplicateXattrName()
    {
        string raw = "{\"version\":1,\"rootBasename\":\"app\",\"entries\":[" +
                     "{\"path\":\".\",\"kind\":\"directory\",\"xattrs\":{\"user.a\":\"\",\"user.a\":\"\"}}]}";
        ExpectThrows(() => ManifestCodec.Parse(Encoding.UTF8.GetBytes(raw)), "duplicate");
    }

    private static void TestDuplicateEntryPath()
    {
        using Fixture fixture = new Fixture();
        Manifest manifest = new Manifest { Version = 1, RootBasename = fixture.RootBasename };
        manifest.Entries.Add(DirectoryEntry("."));
        manifest.Entries.Add(FileEntry("dup", new byte[] { 1 }));
        manifest.Entries.Add(FileEntry("dup", new byte[] { 1 }));
        string path = Path.Combine(fixture.BaseDir, "duplicate.json");
        File.WriteAllBytes(path, ManifestCodec.Serialize(manifest));

        FakeXattrStore store = new FakeXattrStore();
        ExpectThrows(() => Restorer.Restore(fixture.Root, path, store), "duplicate");
    }

    private static void TestManifestShape()
    {
        string versionTwo = "{\"version\":2,\"rootBasename\":\"app\",\"entries\":[]}";
        ExpectThrows(() => ManifestCodec.Parse(Encoding.UTF8.GetBytes(versionTwo)), "version");

        string unknownProperty = "{\"version\":1,\"rootBasename\":\"app\",\"entries\":[],\"extra\":1}";
        ExpectThrows(() => ManifestCodec.Parse(Encoding.UTF8.GetBytes(unknownProperty)), "unknown");

        string notFirst = "{\"version\":1,\"rootBasename\":\"app\",\"entries\":[" +
                          "{\"path\":\"a\",\"kind\":\"file\",\"xattrs\":{},\"sha256\":\"" + new string('0', 64) + "\"}," +
                          "{\"path\":\".\",\"kind\":\"directory\",\"xattrs\":{}}]}";
        ExpectThrows(() => ManifestCodec.Parse(Encoding.UTF8.GetBytes(notFirst)), "first");

        string badSha = "{\"version\":1,\"rootBasename\":\"app\",\"entries\":[" +
                        "{\"path\":\".\",\"kind\":\"file\",\"xattrs\":{},\"sha256\":\"abc\"}]}";
        ExpectThrows(() => ManifestCodec.Parse(Encoding.UTF8.GetBytes(badSha)), "sha256");

        string symlinkAncestor = "{\"version\":1,\"rootBasename\":\"app\",\"entries\":[" +
                                 "{\"path\":\".\",\"kind\":\"directory\",\"xattrs\":{}}," +
                                 "{\"path\":\"link\",\"kind\":\"symlink\",\"xattrs\":{},\"linkTarget\":\"target\"}," +
                                 "{\"path\":\"link/child\",\"kind\":\"file\",\"xattrs\":{},\"sha256\":\"" +
                                 new string('0', 64) + "\"}]}";
        ExpectThrows(() => ManifestCodec.Parse(Encoding.UTF8.GetBytes(symlinkAncestor)), "non-directory ancestor");
    }

    private static void TestEntryCountBound()
    {
        StringBuilder builder = new StringBuilder();
        builder.Append("{\"version\":1,\"rootBasename\":\"app\",\"entries\":[");
        for (int i = 0; i <= Limits.MaxEntries; i++)
        {
            if (i > 0)
            {
                builder.Append(',');
            }

            builder.Append("{\"path\":\"x\",\"kind\":\"file\",\"xattrs\":{},\"sha256\":\"");
            builder.Append('0', 64);
            builder.Append("\"}");
        }

        builder.Append("]}");
        ExpectThrows(() => ManifestCodec.Parse(Encoding.UTF8.GetBytes(builder.ToString())), "entry limit");
    }

    private static void TestBase64Validation()
    {
        string invalid = "{\"version\":1,\"rootBasename\":\"app\",\"entries\":[" +
                         "{\"path\":\".\",\"kind\":\"directory\",\"xattrs\":{\"user.a\":\"!!!!\"}}]}";
        ExpectThrows(() => ManifestCodec.Parse(Encoding.UTF8.GetBytes(invalid)), "base64");

        string escapedPath = "{\"version\":1,\"rootBasename\":\"app\",\"entries\":[" +
                             "{\"path\":\"../escape\",\"kind\":\"directory\",\"xattrs\":{}}]}";
        ExpectThrows(() => ManifestCodec.Parse(Encoding.UTF8.GetBytes(escapedPath)), "..");
    }

    private static void TestXattrEntryBound()
    {
        byte[] atLimit = new byte[Limits.MaxEntryXattrBytes - 1];
        string atLimitJson = RawSingleAttrManifest("u", atLimit);
        Manifest parsed = ManifestCodec.Parse(Encoding.UTF8.GetBytes(atLimitJson));
        Expect(parsed.Entries[0].Xattrs["u"].Length == atLimit.Length, "exact 1 MiB entry should parse");

        byte[] overLimit = new byte[Limits.MaxEntryXattrBytes];
        string overLimitJson = RawSingleAttrManifest("u", overLimit);
        ExpectThrows(() => ManifestCodec.Parse(Encoding.UTF8.GetBytes(overLimitJson)), "1 MiB");

        Dictionary<string, byte[]> tooLarge = new Dictionary<string, byte[]>
        {
            ["user.a"] = new byte[Limits.MaxEntryXattrBytes],
        };
        ExpectThrows(() => XattrLimits.Validate(tooLarge, "test"), "1 MiB");
    }

    private static void TestManifestSizeBound()
    {
        using Fixture fixture = new Fixture();
        string huge = Path.Combine(fixture.BaseDir, "huge.json");
        using (FileStream stream = new FileStream(huge, FileMode.CreateNew, FileAccess.Write))
        {
            stream.SetLength(Limits.MaxManifestBytes + 1);
        }

        FakeXattrStore store = new FakeXattrStore();
        Dictionary<string, Dictionary<string, byte[]>> snapshot = store.Snapshot();
        ExpectThrows(() => ManifestCodec.ReadFile(huge), "32 MiB");
        ExpectThrows(() => Restorer.Restore(fixture.Root, huge, store), "32 MiB");
        Expect(StoreEquals(store.Snapshot(), snapshot), "failed preflight must not write xattrs");
    }

    private static void TestPrivilegeParser()
    {
        LinuxPrivilegeStatus rootWithCap = LinuxPrivilegeStatus.Parse(
            "Name:\tXattrBackup\nUid:\t0\t0\t0\t0\nCapEff:\t0000000000200000\n");
        Expect(rootWithCap.EffectiveUid == 0, "effective UID 0 must be parsed");
        Expect(rootWithCap.IsEffectiveRoot, "root status must be recognized");
        Expect(rootWithCap.HasCapSysAdmin, "CAP_SYS_ADMIN bit 21 must be recognized");
        Expect(rootWithCap.IsSatisfied, "effective root with CAP_SYS_ADMIN must be accepted");

        LinuxPrivilegeStatus rootFullMask = LinuxPrivilegeStatus.Parse(
            "Uid:\t0\t0\t0\t0\nCapEff:\t000001ffffffffff\n");
        Expect(rootFullMask.IsSatisfied, "full capability mask must be accepted");

        LinuxPrivilegeStatus rootWithoutCap = LinuxPrivilegeStatus.Parse(
            "Uid:\t0\t0\t0\t0\nCapEff:\t00000000001fffff\n");
        Expect(rootWithoutCap.IsEffectiveRoot, "root without capability is still root");
        Expect(!rootWithoutCap.HasCapSysAdmin, "bit 21 must not be inferred from lower bits");
        Expect(!rootWithoutCap.IsSatisfied, "root without CAP_SYS_ADMIN must be rejected");

        LinuxPrivilegeStatus nonRootWithCap = LinuxPrivilegeStatus.Parse(
            "Uid:\t1000\t1000\t1000\t1000\nCapEff:\t0000000000200000\n");
        Expect(!nonRootWithCap.IsEffectiveRoot, "non-root effective UID must be detected");
        Expect(nonRootWithCap.HasCapSysAdmin, "non-root capability must still be parsed");
        Expect(!nonRootWithCap.IsSatisfied, "CAP_SYS_ADMIN without effective UID 0 must be rejected");

        LinuxPrivilegeStatus realRootEffectiveUser = LinuxPrivilegeStatus.Parse(
            "Uid:\t0\t1000\t0\t0\nCapEff:\t0000000000200000\n");
        Expect(realRootEffectiveUser.EffectiveUid == 1000, "the second Uid field is the effective UID");
        Expect(!realRootEffectiveUser.IsSatisfied, "real UID 0 with effective UID 1000 must be rejected");

        ExpectThrows(() => LinuxPrivilegeStatus.Parse("CapEff:\t1\n"), "Uid");
        ExpectThrows(() => LinuxPrivilegeStatus.Parse("Uid:\t0\t0\t0\t0\n"), "CapEff");
        ExpectThrows(() => LinuxPrivilegeStatus.Parse("Uid:\t0\tnotanumber\t0\t0\nCapEff:\t1\n"), "malformed");
        ExpectThrows(() => LinuxPrivilegeStatus.Parse("Uid:\t0\t0\t0\t0\nCapEff:\tzz\n"), "malformed");
        ExpectThrows(() => LinuxPrivilegeStatus.Parse(
            "Uid:\t0\t0\t0\t0\nUid:\t0\t0\t0\t0\nCapEff:\t1\n"), "duplicate");
    }

    private static void TestDumpRoundtrip()
    {
        using Fixture fixture = new Fixture();
        bool fileLink = TryCreateFileLink(Path.Combine(fixture.Root, "link-file"), Path.Combine(fixture.Root, "binary.bin"));
        bool dirLink = TryCreateDirectoryLink(Path.Combine(fixture.Root, "link-dir"), fixture.OutsideDir);
        if (!fileLink && !dirLink)
        {
            _skipped++;
        }

        FakeXattrStore store = SeedStore(fixture);
        DumpResult result = Dumper.Dump(fixture.Root, fixture.ManifestPath, store);
        Manifest manifest = ManifestCodec.ReadFile(fixture.ManifestPath);

        Expect(result.Entries == manifest.Entries.Count, "dump result entry count must match manifest");
        Expect(manifest.Version == 1, "manifest version must be 1");
        Expect(manifest.RootBasename == fixture.RootBasename, "manifest root basename mismatch");

        Dictionary<string, ManifestEntry> byPath = manifest.Entries.ToDictionary(e => e.Path, StringComparer.Ordinal);
        Expect(byPath.ContainsKey("."), "manifest must contain root entry '.'");
        Expect(byPath["."].Kind == EntryKind.Directory, "root entry must be a directory");
        Expect(byPath["."].Xattrs.TryGetValue("user.root", out byte[]? rootValue) &&
               rootValue.AsSpan().SequenceEqual(Utf8("root")), "root xattr did not roundtrip");
        Expect(byPath.ContainsKey("sub/config.txt"), "nested file path missing");
        Expect(byPath["sub"].Kind == EntryKind.Directory, "sub must be a directory");
        Expect(byPath["emptydir"].Kind == EntryKind.Directory, "empty directory missing");

        byte[] binaryBytes = File.ReadAllBytes(Path.Combine(fixture.Root, "binary.bin"));
        Expect(byPath["binary.bin"].Sha256 == Sha256(binaryBytes), "binary file hash mismatch");
        Expect(byPath["binary.bin"].Xattrs.TryGetValue("user.a", out byte[]? a) &&
               a.AsSpan().SequenceEqual(new byte[] { 0, 1, 2, 255 }), "binary xattr did not roundtrip");
        Expect(byPath["binary.bin"].Xattrs.TryGetValue("user.b", out byte[]? b) && b.Length == 0,
            "empty xattr value did not roundtrip");

        if (fileLink)
        {
            Expect(byPath.ContainsKey("link-file"), "file symlink entry missing");
            Expect(byPath["link-file"].Kind == EntryKind.Symlink, "file symlink kind mismatch");
            Expect(byPath["link-file"].LinkTarget == Path.Combine(fixture.Root, "binary.bin"),
                "file symlink target mismatch");
        }

        if (dirLink)
        {
            Expect(byPath.ContainsKey("link-dir"), "directory symlink entry missing");
            Expect(byPath["link-dir"].Kind == EntryKind.Symlink, "directory symlink kind mismatch");
            Expect(byPath["link-dir"].LinkTarget == fixture.OutsideDir, "directory symlink target mismatch");
            Expect(!manifest.Entries.Any(e => e.Path.StartsWith("link-dir/", StringComparison.Ordinal)),
                "scanner must not descend directory symlinks");
            Expect(!byPath.ContainsKey("link-dir/outside.txt"), "symlinked directory contents leaked into manifest");
        }

        string[] temporary = Directory.GetFiles(fixture.BaseDir, ".manifest.json.tmp-*");
        Expect(temporary.Length == 0, "atomic manifest write left temporary files");
        Expect(new FileInfo(fixture.ManifestPath).Length > 0, "manifest is empty");
    }

    private static void TestDumpToStreamMatchesFile()
    {
        using Fixture fixture = new Fixture();
        TryCreateFileLink(Path.Combine(fixture.Root, "link-file"), Path.Combine(fixture.Root, "binary.bin"));
        TryCreateDirectoryLink(Path.Combine(fixture.Root, "link-dir"), fixture.OutsideDir);
        FakeXattrStore store = SeedStore(fixture);
        DumpResult fileResult = Dumper.Dump(fixture.Root, fixture.ManifestPath, store);
        byte[] fileBytes = File.ReadAllBytes(fixture.ManifestPath);

        int readsBefore = store.Reads;
        string[] filesBefore = Directory.GetFiles(fixture.BaseDir, "*", SearchOption.AllDirectories);
        using MemoryStream output = new MemoryStream();
        DumpResult streamResult = Dumper.DumpToStream(fixture.Root, output, store);

        Expect(output.CanWrite, "stream sink must remain writable");
        byte[] streamBytes = output.ToArray();
        Expect(streamBytes.AsSpan().SequenceEqual(fileBytes), "stream sink bytes must equal file dump bytes");
        Expect(streamResult.Entries == fileResult.Entries, "stream entry count must match file dump");
        Expect(streamResult.Xattrs == fileResult.Xattrs, "stream xattr count must match file dump");
        Expect(streamResult.ManifestBytes == fileBytes.Length, "stream byte count must match file dump");
        Expect(streamResult.ManifestBytes == streamBytes.Length, "stream result must report actual bytes written");
        Expect(store.Reads > readsBefore, "stream sink must read the fake store");

        output.WriteByte(0x5a);
        Expect(output.Length == streamBytes.Length + 1L, "stream sink must stay open and writable");

        string[] filesAfter = Directory.GetFiles(fixture.BaseDir, "*", SearchOption.AllDirectories);
        Array.Sort(filesBefore, StringComparer.Ordinal);
        Array.Sort(filesAfter, StringComparer.Ordinal);
        Expect(filesBefore.SequenceEqual(filesAfter, StringComparer.Ordinal),
            "stream sink must not create a manifest or temporary file");
        Expect(Directory.GetFiles(fixture.BaseDir, ".manifest.json.tmp-*").Length == 0,
            "stream sink left a temporary manifest file");
    }

    private static void TestDumpToStreamOutputGuards()
    {
        using Fixture fixture = new Fixture();
        FakeXattrStore store = SeedStore(fixture);
        int readsBefore = store.Reads;

        ExpectThrows(() => Dumper.DumpToStream(fixture.Root, null!, store), "output stream");
        Expect(store.Reads == readsBefore, "null output must be rejected before reading xattrs");

        using MemoryStream readOnly = new MemoryStream(Array.Empty<byte>(), writable: false);
        ExpectThrows(() => Dumper.DumpToStream(fixture.Root, readOnly, store), "not writable");
        Expect(store.Reads == readsBefore, "read-only output must be rejected before reading xattrs");
        Expect(!File.Exists(fixture.ManifestPath), "rejected stream sink must not create a manifest");
        Expect(Directory.GetFiles(fixture.BaseDir, ".manifest.json.tmp-*").Length == 0,
            "rejected stream sink left a temporary manifest file");
    }

    private static void TestDumpToStreamWriteFailure()
    {
        using Fixture fixture = new Fixture();
        FakeXattrStore store = SeedStore(fixture);
        using ThrowingWriteStream output = new ThrowingWriteStream();
        ExpectThrows<IOException>(() => Dumper.DumpToStream(fixture.Root, output, store), "sink");
        Expect(output.WriteAttempts == 1, "stream write failure must propagate without a success result");
        Expect(!File.Exists(fixture.ManifestPath), "failed stream sink must not create a manifest");
    }

    private static void TestRawXattrAggregateBound()
    {
        using Fixture fixture = new Fixture();
        const int fileCount = 40;
        for (int i = 0; i < fileCount; i++)
        {
            File.WriteAllText(Path.Combine(fixture.Root, "raw" + i.ToString("D2") + ".txt"), "x");
        }

        const string name = "user.big";
        byte[] repeated = new byte[Limits.MaxEntryXattrBytes - 64];
        Array.Fill(repeated, (byte)0x5a);
        long entryRawBytes = TextRules.StrictUtf8.GetByteCount(name) + repeated.LongLength;
        Expect(entryRawBytes <= Limits.MaxEntryXattrBytes,
            "raw aggregate fixture entry must stay within the 1 MiB per-entry limit");
        XattrLimits.Validate(new Dictionary<string, byte[]> { [name] = repeated }, "raw aggregate fixture");
        Expect(entryRawBytes * (fileCount + 1) > Limits.MaxManifestBytes,
            "raw aggregate fixture must exceed the 32 MiB raw limit");

        SharedValueXattrStore store = new SharedValueXattrStore(name, repeated);
        using MemoryStream output = new MemoryStream();
        ExpectThrows(() => Dumper.DumpToStream(fixture.Root, output, store), "raw extended-attribute bytes");
        Expect(output.Length == 0, "raw aggregate guard must reject before writing manifest bytes");
        Expect(store.Reads > 0, "raw aggregate fixture must read the fake store");
        Expect(store.Reads < fileCount + 1,
            "raw aggregate guard must stop before reading every entry; a disabled guard would read all entries");
    }

    private static void TestRestoreSynchronizes()
    {
        using Fixture fixture = new Fixture();
        TryCreateFileLink(Path.Combine(fixture.Root, "link-file"), Path.Combine(fixture.Root, "binary.bin"));

        FakeXattrStore store = SeedStore(fixture);
        Dumper.Dump(fixture.Root, fixture.ManifestPath, store);
        Manifest manifest = ManifestCodec.ReadFile(fixture.ManifestPath);

        string binary = fixture.Abs("binary.bin");
        store.RemoveAttribute(binary, "user.a");
        store.SeedAttribute(binary, "user.b", new byte[] { 9, 9 });
        store.SeedAttribute(binary, "user.extra", new byte[] { 7 });
        store.RemoveAttribute(fixture.Abs("."), "user.root");
        store.SeedAttribute(fixture.Abs("sub"), "user.extra-dir", new byte[] { 8 });
        int mutationsBefore = store.Mutations;

        RestoreResult result = Restorer.Restore(fixture.Root, fixture.ManifestPath, store);
        Expect(result.Entries == manifest.Entries.Count, "restore entry count mismatch");
        Expect(result.XattrsRemoved == 2, "restore should remove exactly the two extra attributes");
        Expect(result.XattrsSet == 3, "restore should set exactly the three missing or changed attributes");
        Expect(result.XattrsUnchanged == 2, "restore should report exactly the two unchanged attributes");
        Expect(store.Mutations - mutationsBefore == 5, "restore should perform exactly five attribute writes");
        Expect(store.Mutations - mutationsBefore == result.XattrsSet + result.XattrsRemoved,
            "attribute write count must match set plus removed");

        foreach (ManifestEntry entry in manifest.Entries)
        {
            Dictionary<string, byte[]> actual = store.Read(fixture.Abs(entry.Path), entry.Path);
            Expect(actual.Count == entry.Xattrs.Count, "xattr count mismatch after restore for '" + entry.Path + "'");
            foreach (KeyValuePair<string, byte[]> expected in entry.Xattrs)
            {
                Expect(actual.TryGetValue(expected.Key, out byte[]? value) &&
                       value.AsSpan().SequenceEqual(expected.Value),
                    "xattr value mismatch after restore for '" + entry.Path + "'");
            }
        }

        HashSet<string> expectedPaths = new HashSet<string>(
            manifest.Entries.Select(e => fixture.Abs(e.Path)),
            StringComparer.Ordinal);
        foreach (string key in store.Snapshot().Keys)
        {
            Expect(expectedPaths.Contains(key), "restore touched an unexpected path");
        }
    }

    private static void TestRestoreRemovesKernelCreatedExtra()
    {
        using Fixture fixture = new Fixture();
        FakeXattrStore store = new FakeXattrStore();
        string target = fixture.Abs("binary.bin");
        store.SeedAttribute(target, "security.SMACK64", Utf8("original"));
        Dumper.Dump(fixture.Root, fixture.ManifestPath, store);

        store.SeedAttribute(target, "security.SMACK64", Utf8("stale"));
        store.SideEffects = (path, name, value) =>
        {
            Dictionary<string, byte[]> effects = new Dictionary<string, byte[]>(StringComparer.Ordinal);
            if (path == target && name == "security.SMACK64")
            {
                effects["security.SMACK64PROCNAME"] = Utf8("kernel-" + Encoding.UTF8.GetString(value));
            }

            return effects;
        };

        RestoreResult result = Restorer.Restore(fixture.Root, fixture.ManifestPath, store);
        Dictionary<string, byte[]> actual = store.Read(target, "binary.bin");
        Expect(actual.Count == 1, "kernel-created extra attribute must be removed");
        Expect(actual.TryGetValue("security.SMACK64", out byte[]? smack) &&
               smack.AsSpan().SequenceEqual(Utf8("original")),
            "SMACK64 must be restored exactly");
        Expect(!actual.ContainsKey("security.SMACK64PROCNAME"), "extra PROCNAME must not remain");
        Expect(result.XattrsRemoved == 1, "PROCNAME removal must be reported exactly once");
    }

    private static void TestRestoreReappliesKernelOverwrittenExpected()
    {
        using Fixture fixture = new Fixture();
        FakeXattrStore store = new FakeXattrStore();
        string target = fixture.Abs("binary.bin");
        store.SeedAttribute(target, "security.SMACK64", Utf8("original"));
        store.SeedAttribute(target, "security.SMACK64PROCNAME", Utf8("manual"));
        Dumper.Dump(fixture.Root, fixture.ManifestPath, store);

        store.SeedAttribute(target, "security.SMACK64", Utf8("stale"));
        store.SideEffects = (path, name, value) =>
        {
            Dictionary<string, byte[]> effects = new Dictionary<string, byte[]>(StringComparer.Ordinal);
            if (path == target && name == "security.SMACK64")
            {
                effects["security.SMACK64PROCNAME"] = Utf8("kernel-" + Encoding.UTF8.GetString(value));
            }

            return effects;
        };

        Restorer.Restore(fixture.Root, fixture.ManifestPath, store);
        Dictionary<string, byte[]> actual = store.Read(target, "binary.bin");
        Expect(actual.TryGetValue("security.SMACK64", out byte[]? smack) &&
               smack.AsSpan().SequenceEqual(Utf8("original")),
            "SMACK64 must be restored exactly");
        Expect(actual.TryGetValue("security.SMACK64PROCNAME", out byte[]? procname) &&
               procname.AsSpan().SequenceEqual(Utf8("manual")),
            "expected PROCNAME overwritten by a SMACK64 set must be restored");
        Expect(actual.Count == 2, "no unexpected attributes may remain");
    }

    private static void TestReconcileGivesUpOnNonConvergingSideEffects()
    {
        using Fixture fixture = new Fixture();
        FakeXattrStore store = new FakeXattrStore();
        string target = fixture.Abs("binary.bin");
        store.SeedAttribute(target, "security.SMACK64", Utf8("original"));
        store.SeedAttribute(target, "security.SMACK64PROCNAME", Utf8("manual"));
        Dumper.Dump(fixture.Root, fixture.ManifestPath, store);

        store.SeedAttribute(target, "security.SMACK64", Utf8("stale"));
        store.SeedAttribute(target, "security.SMACK64PROCNAME", Utf8("stale"));
        int counter = 0;
        store.SideEffects = (path, name, value) =>
        {
            Dictionary<string, byte[]> effects = new Dictionary<string, byte[]>(StringComparer.Ordinal);
            if (path != target)
            {
                return effects;
            }

            counter++;
            if (name == "security.SMACK64")
            {
                effects["security.SMACK64PROCNAME"] = Utf8("auto-" + counter);
            }
            else if (name == "security.SMACK64PROCNAME")
            {
                effects["security.SMACK64"] = Utf8("auto-" + counter);
            }

            return effects;
        };

        int mutationsBefore = store.Mutations;
        ExpectThrows(() => Restorer.Restore(fixture.Root, fixture.ManifestPath, store), "did not converge");
        Expect(store.Mutations - mutationsBefore <= 64, "non-converging reconciliation must stay bounded");
        Expect(counter <= 32, "side-effect hook must be called a bounded number of times");
    }

    private static void TestCountPreservingRenameRejects()
    {
        using Fixture fixture = new Fixture();
        FakeXattrStore store = SeedStore(fixture);
        Dumper.Dump(fixture.Root, fixture.ManifestPath, store);
        Manifest manifest = ManifestCodec.ReadFile(fixture.ManifestPath);

        foreach (ManifestEntry entry in manifest.Entries)
        {
            store.SeedAttribute(fixture.Abs(entry.Path), "user.sentinel", new byte[] { 6, 1 });
        }

        int mutationsBefore = store.Mutations;
        Dictionary<string, Dictionary<string, byte[]>> snapshot = store.Snapshot();

        string binary = Path.Combine(fixture.Root, "binary.bin");
        string config = Path.Combine(fixture.Root, "sub", "config.txt");
        string swap = Path.Combine(fixture.BaseDir, "swap.tmp");
        File.Move(binary, swap);
        File.Move(config, binary);
        File.Move(swap, config);

        List<TreeEntry> after = TreeScanner.Scan(fixture.Root);
        Expect(after.Count == manifest.Entries.Count, "rename swap must preserve the entry count");
        HashSet<string> beforePaths = new HashSet<string>(
            manifest.Entries.Select(e => e.Path),
            StringComparer.Ordinal);
        HashSet<string> afterPaths = new HashSet<string>(
            after.Select(e => e.RelativePath),
            StringComparer.Ordinal);
        Expect(beforePaths.SetEquals(afterPaths), "rename swap must preserve the exact path set");

        ExpectThrows(() => Restorer.Restore(fixture.Root, fixture.ManifestPath, store), "hash mismatch");
        Expect(store.Mutations == mutationsBefore,
            "count-preserving rename preflight must perform ZERO attribute writes");
        Expect(StoreEquals(store.Snapshot(), snapshot),
            "count-preserving rename preflight must leave every attribute unchanged");
    }

    private static void TestContentMismatchRejectsWholePreflight()
    {
        using Fixture fixture = new Fixture();
        FakeXattrStore store = SeedStore(fixture);
        Dumper.Dump(fixture.Root, fixture.ManifestPath, store);
        Manifest manifest = ManifestCodec.ReadFile(fixture.ManifestPath);

        foreach (ManifestEntry entry in manifest.Entries)
        {
            store.SeedAttribute(fixture.Abs(entry.Path), "user.sentinel", new byte[] { 4, 2 });
        }

        Dictionary<string, Dictionary<string, byte[]>> snapshot = store.Snapshot();
        File.WriteAllText(Path.Combine(fixture.Root, "sub", "config.txt"), "tampered\n");

        ExpectThrows(() => Restorer.Restore(fixture.Root, fixture.ManifestPath, store), "hash mismatch");
        Expect(StoreEquals(store.Snapshot(), snapshot), "failed content preflight must not write any xattrs");
    }

    private static void TestMembershipMismatch()
    {
        using (Fixture fixture = new Fixture())
        {
            FakeXattrStore store = SeedStore(fixture);
            Dumper.Dump(fixture.Root, fixture.ManifestPath, store);
            Dictionary<string, Dictionary<string, byte[]>> snapshot = store.Snapshot();
            File.WriteAllText(Path.Combine(fixture.Root, "extra.txt"), "extra");
            ExpectThrows(() => Restorer.Restore(fixture.Root, fixture.ManifestPath, store), "differ");
            Expect(StoreEquals(store.Snapshot(), snapshot), "extra entry must not cause xattr writes");
        }

        using (Fixture fixture = new Fixture())
        {
            FakeXattrStore store = SeedStore(fixture);
            Dumper.Dump(fixture.Root, fixture.ManifestPath, store);
            Dictionary<string, Dictionary<string, byte[]>> snapshot = store.Snapshot();
            File.Delete(Path.Combine(fixture.Root, "sub", "config.txt"));
            ExpectThrows(() => Restorer.Restore(fixture.Root, fixture.ManifestPath, store), "differ");
            Expect(StoreEquals(store.Snapshot(), snapshot), "missing entry must not cause xattr writes");
        }
    }

    private static void TestKindMismatch()
    {
        using Fixture fixture = new Fixture();
        FakeXattrStore store = SeedStore(fixture);
        Dumper.Dump(fixture.Root, fixture.ManifestPath, store);

        Manifest manifest = ManifestCodec.ReadFile(fixture.ManifestPath);
        ManifestEntry entry = manifest.Entries.First(e => e.Path == "sub/config.txt");
        entry.Kind = EntryKind.Symlink;
        entry.Sha256 = null;
        entry.LinkTarget = "somewhere-else";

        string path = Path.Combine(fixture.BaseDir, "kind.json");
        File.WriteAllBytes(path, ManifestCodec.Serialize(manifest));
        Dictionary<string, Dictionary<string, byte[]>> snapshot = store.Snapshot();
        ExpectThrows(() => Restorer.Restore(fixture.Root, path, store), "kind mismatch");
        Expect(StoreEquals(store.Snapshot(), snapshot), "kind mismatch must reject the whole preflight");
    }

    private static void TestLinkTargetMismatch()
    {
        using Fixture fixture = new Fixture();
        string target = Path.Combine(fixture.Root, "binary.bin");
        if (!TryCreateFileLink(Path.Combine(fixture.Root, "link-file"), target))
        {
            _skipped++;
            return;
        }

        FakeXattrStore store = SeedStore(fixture);
        Dumper.Dump(fixture.Root, fixture.ManifestPath, store);
        Manifest manifest = ManifestCodec.ReadFile(fixture.ManifestPath);
        ManifestEntry link = manifest.Entries.First(e => e.Path == "link-file");
        link.LinkTarget = target + ".different";

        string path = Path.Combine(fixture.BaseDir, "link.json");
        File.WriteAllBytes(path, ManifestCodec.Serialize(manifest));
        Dictionary<string, Dictionary<string, byte[]>> snapshot = store.Snapshot();
        ExpectThrows(() => Restorer.Restore(fixture.Root, path, store), "symlink target mismatch");
        Expect(StoreEquals(store.Snapshot(), snapshot), "link mismatch must reject the whole preflight");
    }

    private static void TestRootBasenameGuard()
    {
        using Fixture fixture = new Fixture();
        FakeXattrStore store = SeedStore(fixture);
        Dumper.Dump(fixture.Root, fixture.ManifestPath, store);

        string other = Path.Combine(fixture.BaseDir, "other");
        Directory.CreateDirectory(other);
        FakeXattrStore rejectedStore = new FakeXattrStore();
        Dictionary<string, Dictionary<string, byte[]>> snapshot = rejectedStore.Snapshot();
        ExpectThrows(() => Restorer.Restore(other, fixture.ManifestPath, rejectedStore), "basename");
        Expect(StoreEquals(rejectedStore.Snapshot(), snapshot), "wrong-root rejection must not write xattrs");
    }

    private static void TestRootGuards()
    {
        using Fixture fixture = new Fixture();
        FakeXattrStore store = SeedStore(fixture);

        string pathRoot = Path.GetPathRoot(Path.GetFullPath(fixture.Root)) ?? "/";
        ExpectThrows(() => Dumper.Dump(pathRoot, fixture.ManifestPath, store), "filesystem root");

        string linkRoot = Path.Combine(fixture.BaseDir, "app-link");
        if (TryCreateDirectoryLink(linkRoot, fixture.Root))
        {
            ExpectThrows(() => Dumper.Dump(linkRoot, Path.Combine(fixture.BaseDir, "link-root.json"), store), "symbolic link");
        }
        else
        {
            _skipped++;
        }
    }

    private static void TestManifestInsideRoot()
    {
        using Fixture fixture = new Fixture();
        FakeXattrStore store = SeedStore(fixture);
        string inside = Path.Combine(fixture.Root, "manifest.json");
        ExpectThrows(() => Dumper.Dump(fixture.Root, inside, store), "outside ROOT");
        Expect(!File.Exists(inside), "dump must not create a manifest inside ROOT");

        string nested = Path.Combine(fixture.Root, "sub", "manifest.json");
        ExpectThrows(() => Dumper.Dump(fixture.Root, nested, store), "outside ROOT");
        Expect(!File.Exists(nested), "dump must not create a nested manifest inside ROOT");

        string alias = Path.Combine(fixture.BaseDir, "root-alias");
        if (TryCreateDirectoryLink(alias, fixture.Root))
        {
            string viaLink = Path.Combine(alias, "manifest.json");
            ExpectThrows(() => Dumper.Dump(fixture.Root, viaLink, store), "outside ROOT");
            Expect(!File.Exists(viaLink), "symlinked manifest path inside ROOT must not be written");
        }
        else
        {
            _skipped++;
        }
    }

    private static void TestAtomicWriteLeavesNoTemp()
    {
        using Fixture fixture = new Fixture();
        FakeXattrStore store = SeedStore(fixture);
        Dumper.Dump(fixture.Root, fixture.ManifestPath, store);
        Dumper.Dump(fixture.Root, fixture.ManifestPath, store);
        string[] temporary = Directory.GetFiles(fixture.BaseDir, ".manifest.json.tmp-*");
        Expect(temporary.Length == 0, "temporary manifest files remain");
        Manifest manifest = ManifestCodec.ReadFile(fixture.ManifestPath);
        Expect(manifest.Entries.Count > 0, "overwritten manifest is unreadable");
    }

    private static void TestNonLinuxCliRefuses()
    {
        using Fixture fixture = new Fixture();
        string manifest = fixture.ManifestPath;
        DateTime rootWriteTime = Directory.GetLastWriteTimeUtc(fixture.Root);

        TextWriter originalError = Console.Error;
        StringWriter captured = new StringWriter();
        int dumpCode;
        int restoreCode;
        try
        {
            Console.SetError(captured);
            dumpCode = Program.Main(new[] { "--dump", fixture.Root, manifest });
            restoreCode = Program.Main(new[] { "--restore", fixture.Root, manifest });
        }
        finally
        {
            Console.SetError(originalError);
        }

        Expect(dumpCode == 2, "dump on non-Linux must exit 2");
        Expect(restoreCode == 2, "restore on non-Linux must exit 2");
        Expect(!File.Exists(manifest), "non-Linux CLI must not create a manifest");
        Expect(Directory.GetLastWriteTimeUtc(fixture.Root) == rootWriteTime, "non-Linux CLI must not touch ROOT");
        Expect(captured.ToString().Contains("Linux", StringComparison.Ordinal), "non-Linux CLI must explain the failure");
    }

    private static string RawSingleAttrManifest(string name, byte[] value)
    {
        string base64 = Convert.ToBase64String(value);
        return "{\"version\":1,\"rootBasename\":\"app\",\"entries\":[" +
               "{\"path\":\".\",\"kind\":\"directory\",\"xattrs\":{\"" + name + "\":\"" + base64 + "\"}}]}";
    }

    private static ManifestEntry DirectoryEntry(string path) => new ManifestEntry
    {
        Path = path,
        Kind = EntryKind.Directory,
    };

    private static ManifestEntry FileEntry(string path, byte[] content) => new ManifestEntry
    {
        Path = path,
        Kind = EntryKind.File,
        Sha256 = Sha256(content),
    };

    private static FakeXattrStore SeedStore(Fixture fixture)
    {
        FakeXattrStore store = new FakeXattrStore();
        store.SeedAttribute(fixture.Abs("."), "user.root", Utf8("root"));
        store.SeedAttribute(fixture.Abs("binary.bin"), "user.a", new byte[] { 0, 1, 2, 255 });
        store.SeedAttribute(fixture.Abs("binary.bin"), "user.b", Array.Empty<byte>());
        store.SeedAttribute(fixture.Abs("sub"), "user.dir", Utf8("sub"));
        store.SeedAttribute(fixture.Abs("sub/config.txt"), "user.config", Utf8("config"));
        return store;
    }

    private static string Sha256(byte[] content)
    {
        using SHA256 sha = SHA256.Create();
        return Convert.ToHexString(sha.ComputeHash(content)).ToLowerInvariant();
    }

    private static byte[] Utf8(string value) => Encoding.UTF8.GetBytes(value);

    private static bool TryCreateFileLink(string linkPath, string targetPath)
    {
        try
        {
            File.CreateSymbolicLink(linkPath, targetPath);
            return true;
        }
        catch (Exception ex) when (ex is IOException || ex is UnauthorizedAccessException || ex is PlatformNotSupportedException)
        {
            return false;
        }
    }

    private static bool TryCreateDirectoryLink(string linkPath, string targetPath)
    {
        try
        {
            Directory.CreateSymbolicLink(linkPath, targetPath);
            return true;
        }
        catch (Exception ex) when (ex is IOException || ex is UnauthorizedAccessException || ex is PlatformNotSupportedException)
        {
            return false;
        }
    }

    private static void Check(string name, Action test)
    {
        try
        {
            test();
            _passed++;
        }
        catch (Exception ex)
        {
            _failed++;
            Console.Error.WriteLine("FAIL " + name + ": " + ex.Message);
        }
    }

    private static void Expect(bool condition, string message)
    {
        if (!condition)
        {
            throw new Exception(message);
        }
    }

    private static void ExpectThrows(Action action, string? messageFragment)
    {
        try
        {
            action();
        }
        catch (BackupException ex)
        {
            if (messageFragment is not null &&
                ex.Message.IndexOf(messageFragment, StringComparison.OrdinalIgnoreCase) < 0)
            {
                throw new Exception(
                    "expected error containing '" + messageFragment + "', got '" + ex.Message + "'");
            }

            return;
        }
        catch (Exception ex)
        {
            throw new Exception("expected BackupException, got " + ex.GetType().Name + ": " + ex.Message);
        }

        throw new Exception("expected an error, but the operation succeeded");
    }

    private static void ExpectThrows<T>(Action action, string? messageFragment) where T : Exception
    {
        try
        {
            action();
        }
        catch (T ex)
        {
            if (messageFragment is not null &&
                ex.Message.IndexOf(messageFragment, StringComparison.OrdinalIgnoreCase) < 0)
            {
                throw new Exception(
                    "expected error containing '" + messageFragment + "', got '" + ex.Message + "'");
            }

            return;
        }
        catch (Exception ex)
        {
            throw new Exception("expected " + typeof(T).Name + ", got " + ex.GetType().Name + ": " + ex.Message);
        }

        throw new Exception("expected an error, but the operation succeeded");
    }

    private static bool StoreEquals(
        Dictionary<string, Dictionary<string, byte[]>> left,
        Dictionary<string, Dictionary<string, byte[]>> right)
    {
        if (left.Count != right.Count)
        {
            return false;
        }

        foreach (KeyValuePair<string, Dictionary<string, byte[]>> item in left)
        {
            if (!right.TryGetValue(item.Key, out Dictionary<string, byte[]>? other) ||
                other.Count != item.Value.Count)
            {
                return false;
            }

            foreach (KeyValuePair<string, byte[]> attr in item.Value)
            {
                if (!other.TryGetValue(attr.Key, out byte[]? value) || !value.AsSpan().SequenceEqual(attr.Value))
                {
                    return false;
                }
            }
        }

        return true;
    }

    private sealed class Fixture : IDisposable
    {
        private readonly byte[] _binaryPayload;

        internal Fixture()
        {
            BaseDir = Path.Combine(Path.GetTempPath(), "XattrBackupSelfTest-" + Guid.NewGuid().ToString("N"));
            Root = Path.Combine(BaseDir, "app");
            OutsideDir = Path.Combine(BaseDir, "external");
            Directory.CreateDirectory(Root);
            Directory.CreateDirectory(OutsideDir);
            Directory.CreateDirectory(Path.Combine(Root, "sub"));
            Directory.CreateDirectory(Path.Combine(Root, "emptydir"));

            _binaryPayload = new byte[512];
            for (int i = 0; i < _binaryPayload.Length; i++)
            {
                _binaryPayload[i] = (byte)(i % 256);
            }

            _binaryPayload[10] = 0;
            File.WriteAllBytes(Path.Combine(Root, "binary.bin"), _binaryPayload);
            File.WriteAllText(Path.Combine(Root, "sub", "config.txt"), "config\n");
            File.WriteAllText(Path.Combine(OutsideDir, "outside.txt"), "outside\n");
        }

        internal string BaseDir { get; }

        internal string Root { get; }

        internal string OutsideDir { get; }

        internal string RootBasename => Path.GetFileName(Root);

        internal string ManifestPath => Path.Combine(BaseDir, "manifest.json");

        internal string Abs(string relativePath) =>
            relativePath == "."
                ? Root
                : Path.Combine(Root, relativePath.Replace('/', Path.DirectorySeparatorChar));

        public void Dispose()
        {
            try
            {
                Directory.Delete(BaseDir, recursive: true);
            }
            catch (Exception)
            {
                // Temporary test data only; leave it for the OS to clean up if deletion fails.
            }
        }
    }

    private sealed class FakeXattrStore : IXattrStore
    {
        private readonly Dictionary<string, SortedDictionary<string, byte[]>> _store =
            new Dictionary<string, SortedDictionary<string, byte[]>>(StringComparer.Ordinal);

        internal int Mutations { get; private set; }

        internal int Reads { get; private set; }

        internal Func<string, string, byte[], Dictionary<string, byte[]>>? SideEffects { get; set; }

        public Dictionary<string, byte[]> Read(string absolutePath, string relativePath)
        {
            Reads++;
            Dictionary<string, byte[]> copy = new Dictionary<string, byte[]>(StringComparer.Ordinal);
            if (_store.TryGetValue(absolutePath, out SortedDictionary<string, byte[]>? attrs))
            {
                foreach (KeyValuePair<string, byte[]> attr in attrs)
                {
                    copy.Add(attr.Key, (byte[])attr.Value.Clone());
                }
            }

            return copy;
        }

        public void Set(string absolutePath, string relativePath, string name, byte[] value)
        {
            Mutations++;
            if (!_store.TryGetValue(absolutePath, out SortedDictionary<string, byte[]>? attrs))
            {
                attrs = new SortedDictionary<string, byte[]>(StringComparer.Ordinal);
                _store.Add(absolutePath, attrs);
            }

            attrs[name] = (byte[])value.Clone();

            if (SideEffects is not null)
            {
                foreach (KeyValuePair<string, byte[]> effect in SideEffects(absolutePath, name, value))
                {
                    attrs[effect.Key] = (byte[])effect.Value.Clone();
                    Mutations++;
                }
            }
        }

        public void Remove(string absolutePath, string relativePath, string name)
        {
            Mutations++;
            if (_store.TryGetValue(absolutePath, out SortedDictionary<string, byte[]>? attrs))
            {
                attrs.Remove(name);
            }
        }

        internal void SeedAttribute(string absolutePath, string name, byte[] value) =>
            Set(absolutePath, string.Empty, name, value);

        internal void RemoveAttribute(string absolutePath, string name) =>
            Remove(absolutePath, string.Empty, name);

        internal Dictionary<string, Dictionary<string, byte[]>> Snapshot()
        {
            Dictionary<string, Dictionary<string, byte[]>> snapshot =
                new Dictionary<string, Dictionary<string, byte[]>>(StringComparer.Ordinal);
            foreach (KeyValuePair<string, SortedDictionary<string, byte[]>> item in _store)
            {
                Dictionary<string, byte[]> attrs = new Dictionary<string, byte[]>(StringComparer.Ordinal);
                foreach (KeyValuePair<string, byte[]> attr in item.Value)
                {
                    attrs.Add(attr.Key, (byte[])attr.Value.Clone());
                }

                snapshot.Add(item.Key, attrs);
            }

            return snapshot;
        }
    }

    private sealed class SharedValueXattrStore : IXattrStore
    {
        private readonly string _name;
        private readonly byte[] _value;

        internal SharedValueXattrStore(string name, byte[] value)
        {
            _name = name;
            _value = value;
        }

        internal int Reads { get; private set; }

        public Dictionary<string, byte[]> Read(string absolutePath, string relativePath)
        {
            Reads++;
            return new Dictionary<string, byte[]>(StringComparer.Ordinal)
            {
                [_name] = _value,
            };
        }

        public void Set(string absolutePath, string relativePath, string name, byte[] value) =>
            throw new InvalidOperationException("shared-value store is read-only");

        public void Remove(string absolutePath, string relativePath, string name) =>
            throw new InvalidOperationException("shared-value store is read-only");
    }

    private sealed class ThrowingWriteStream : MemoryStream
    {
        internal int WriteAttempts { get; private set; }

        public override void Write(byte[] buffer, int offset, int count)
        {
            WriteAttempts++;
            throw new IOException("test sink write failure");
        }

        public override void WriteByte(byte value)
        {
            WriteAttempts++;
            throw new IOException("test sink write failure");
        }
    }
}

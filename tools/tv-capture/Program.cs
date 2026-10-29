using System.Diagnostics;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using XattrBackup;

internal static class Program
{
    internal const string Tag = "QUIETTV-CAPTURE/1 ";
    internal const long Maximum = 4L << 30;
    internal const long MaximumXattrs = 32L << 20;
    internal const int Chunk = 32768;
    private static string phase = "request";
    // Exact protected token list in the supported profile. Tests check equality.
    internal static readonly string[] Protected = { "setting", "hdmi", "audio", "sound", "volume", "network", "wifi", "wlan", "ethernet", "bluetooth", "security", "csfs", "firmware", "update", "store", "webengine", "chromium", "ewk", "drm", "tv-viewer", "source" };
    internal enum CaptureKind { Package, Xattrs }
    internal sealed record Request(CaptureKind Kind, string Id, string Parent, long Limit);
    internal static Request Parse(string[] a)
    {
        if (a.Length != 4) throw new InvalidDataException();
        CaptureKind kind;
        long maximum;
        if (a[0] == "--package") { kind = CaptureKind.Package; maximum = Maximum; }
        else if (a[0] == "--xattrs") { kind = CaptureKind.Xattrs; maximum = MaximumXattrs; }
        else throw new InvalidDataException();
        if (!Regex.IsMatch(a[1], @"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\z")
            || a[1] is "." or ".." || Protected.Any(t => a[1].Contains(t, StringComparison.OrdinalIgnoreCase))
            || a[2] is not ("opt" or "rom") || !Regex.IsMatch(a[3], @"\A[1-9][0-9]{0,10}\z")
            || !long.TryParse(a[3], NumberStyles.None, CultureInfo.InvariantCulture, out long n) || n > maximum)
            throw new InvalidDataException();
        return new Request(kind, a[1], a[2] == "opt" ? "/opt/usr/apps" : "/usr/apps", n);
    }
    internal static async Task<int> Main(string[] a)
    {
        try
        {
            if (a.SequenceEqual(new[] { "--self-test" })) return SelfTest();
            Request r = Parse(a); // Reject every invalid request before OS inspection/child execution.
            if (!OperatingSystem.IsLinux()) throw new PlatformNotSupportedException();
            if (r.Kind == CaptureKind.Xattrs) CaptureXattrs(r);
            else await Capture(r);
            return 0;
        }
        catch (Exception ex)
        {
            // Paths, package contents and child stderr are never diagnostics.
            Console.Error.WriteLine("capture-refused:" + phase + ":" + ex.GetType().Name);
            return 1;
        }
    }

    // Linux statx has a fixed, architecture-independent kernel ABI (including ARMv7).
    // A libc without this symbol refuses; never guess an architecture-dependent stat layout.
    [StructLayout(LayoutKind.Explicit, Size = 256)]
    internal struct Stat
    {
        [FieldOffset(0)] public uint Mask;
        [FieldOffset(16)] public uint Nlink;
        [FieldOffset(20)] public uint Uid;
        [FieldOffset(24)] public uint Gid;
        [FieldOffset(28)] public ushort Mode;
        [FieldOffset(32)] public ulong Inode;
        [FieldOffset(40)] public ulong Size;
        [FieldOffset(96)] public long CTime;
        [FieldOffset(104)] public uint CTimeNs;
        [FieldOffset(112)] public long MTime;
        [FieldOffset(120)] public uint MTimeNs;
        [FieldOffset(136)] public uint DevMajor;
        [FieldOffset(140)] public uint DevMinor;
    }
    [DllImport("libc.so.6", EntryPoint = "statx", SetLastError = true)]
    private static extern int Statx(int dir, [MarshalAs(UnmanagedType.LPUTF8Str)] string path, int flags, uint mask, out Stat result);
    private static Stat Observe(string path)
    {
        if (Statx(-100, path, 0x100 | 0x800, 0x7ff, out Stat s) != 0 || (s.Mask & 0x7ff) != 0x7ff)
            throw new IOException();
        CheckStat(s);
        return s;
    }
    private static void CheckStat(Stat s)
    {
        int kind = s.Mode & 0xf000;
        // Record links themselves. Never recurse into or dereference their targets.
        if (kind is not (0x8000 or 0x4000 or 0xa000)) throw new InvalidDataException();
        if (kind == 0x8000 && s.Nlink != 1) throw new InvalidDataException();
    }
    internal static SortedDictionary<string, string> Snapshot(string root)
    {
        var result = new SortedDictionary<string, string>(StringComparer.Ordinal);
        var pending = new Stack<string>(); pending.Push(root);
        Stat top = Observe(root);
        if ((top.Mode & 0xf000) != 0x4000) throw new InvalidDataException();
        while (pending.Count != 0)
        {
            string path = pending.Pop(); Stat s = Observe(path);
            if (s.DevMajor != top.DevMajor || s.DevMinor != top.DevMinor || result.Count >= 15000 || path.Length > 4096)
                throw new InvalidDataException();
            string target = (s.Mode & 0xf000) == 0xa000 ? new FileInfo(path).LinkTarget ?? throw new IOException() : "";
            if (target.Length > 4096) throw new InvalidDataException();
            result.Add(path, $"{s.DevMajor}:{s.DevMinor}:{s.Inode}:{s.Mode}:{s.Nlink}:{s.Uid}:{s.Gid}:{s.Size}:{s.CTime}:{s.CTimeNs}:{s.MTime}:{s.MTimeNs}:{target}");
            if ((s.Mode & 0xf000) == 0x4000)
                foreach (string child in Directory.EnumerateFileSystemEntries(path))
                {
                    if (pending.Count + result.Count >= 15000) throw new InvalidDataException();
                    pending.Push(child);
                }
        }
        return result;
    }
    internal static void WriteRecord(Stream stdout, string record)
    {
        byte[] bytes = Encoding.ASCII.GetBytes(Tag + record + "\n");
        stdout.Write(bytes, 0, bytes.Length); stdout.Flush();
    }
    private static string[] TarArguments(Request r) => new[] {
        "--create", "--file=-", "--format=gnu", "--numeric-owner", "--one-file-system",
        "--atime-preserve=system", "--directory=" + r.Parent + "/" + r.Id, "--", "." };
    internal static async Task Capture(Request r)
    {
        string root = r.Parent + "/" + r.Id;
        phase = "ancestors";
        // Neither package root nor its ancestors may be links.
        string prefix = "";
        foreach (string component in root.Split('/', StringSplitOptions.RemoveEmptyEntries))
        {
            prefix += "/" + component;
            if ((Observe(prefix).Mode & 0xf000) != 0x4000) throw new InvalidDataException();
        }
        phase = "snapshot-before";
        var before = Snapshot(root);
        phase = "tar-start";
        string tar = File.Exists("/usr/bin/tar") ? "/usr/bin/tar" : "/bin/tar";
        var info = new ProcessStartInfo(tar) { UseShellExecute = false, RedirectStandardOutput = true, RedirectStandardError = true };
        info.Environment.Clear(); info.Environment["PATH"] = "/usr/bin:/bin";
        info.Environment["LANG"] = "C"; info.Environment["LC_ALL"] = "C";
        foreach (string arg in TarArguments(r))
            info.ArgumentList.Add(arg);
        using var child = new Process { StartInfo = info };
        using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(90));
        using var hash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
        Stream stdout = Console.OpenStandardOutput(); long total = 0, count = 0;
        if (!child.Start()) throw new IOException();
        try
        {
            WriteRecord(stdout, "BEGIN package " + r.Id);
            phase = "tar-stream";
            Task stderr = DrainError(child.StandardError.BaseStream, deadline.Token);
            byte[] buffer = new byte[Chunk];
            while (true)
            {
                int n = await child.StandardOutput.BaseStream.ReadAsync(buffer.AsMemory(), deadline.Token);
                if (n == 0) break;
                total += n;
                if (total > r.Limit || ++count > r.Limit) throw new InvalidDataException();
                hash.AppendData(buffer, 0, n);
                string chunkHash = Convert.ToHexString(SHA256.HashData(buffer.AsSpan(0, n))).ToLowerInvariant();
                WriteRecord(stdout, $"DATA {count - 1} {Convert.ToBase64String(buffer, 0, n)} {chunkHash}");
            }
            phase = "tar-wait";
            await child.WaitForExitAsync(deadline.Token); await stderr;
            phase = "tar-exit-" + child.ExitCode.ToString(CultureInfo.InvariantCulture);
            if (child.ExitCode != 0 || total == 0) throw new IOException();
            phase = "snapshot-after";
            if (!before.SequenceEqual(Snapshot(root))) throw new IOException();
            WriteRecord(stdout, $"END {count} {total} {Convert.ToHexString(hash.GetHashAndReset()).ToLowerInvariant()}");
        }
        finally
        {
            if (!child.HasExited) { child.Kill(); child.WaitForExit(5000); }
        }
    }
    internal static bool PrivilegeSatisfied(LinuxPrivilegeStatus? status) =>
        status is not null && status.IsSatisfied;
    internal static void CaptureXattrs(Request r)
    {
        string root = r.Parent + "/" + r.Id;
        phase = "privilege";
        if (!PrivilegeSatisfied(LinuxPrivilegeStatus.ReadCurrent())) throw new UnauthorizedAccessException();
        phase = "ancestors";
        // Neither the xattr root nor its ancestors may be links.
        string prefix = "";
        foreach (string component in root.Split('/', StringSplitOptions.RemoveEmptyEntries))
        {
            prefix += "/" + component;
            if ((Observe(prefix).Mode & 0xf000) != 0x4000) throw new InvalidDataException();
        }
        phase = "snapshot-before";
        var before = Snapshot(root);
        Stream stdout = Console.OpenStandardOutput();
        phase = "xattr-start";
        WriteRecord(stdout, "BEGIN xattrs " + r.Id);
        phase = "xattr-stream";
        using var framing = new FramingStream(stdout, r.Limit);
        DumpResult result = Dumper.DumpToStream(root, framing, LinuxXattrStore.Instance);
        phase = "xattr-verify";
        if (result.ManifestBytes != framing.Total) throw new IOException();
        phase = "snapshot-after";
        if (!before.SequenceEqual(Snapshot(root))) throw new IOException();
        WriteRecord(stdout, $"END {framing.Count} {framing.Total} {framing.FinishHash()}");
    }
    private static async Task DrainError(Stream error, CancellationToken token)
    {
        byte[] buffer = new byte[1024]; int total = 0, n;
        while ((n = await error.ReadAsync(buffer.AsMemory(), token)) != 0)
            if ((total += n) > 65536) throw new IOException();
    }
    private static int SelfTest()
    {
        int count = 0;
        foreach (string[] bad in new[] {
            Array.Empty<string>(), new[] { "--package", "../escape", "opt", "99" },
            new[] { "--package", "com.tv.store", "opt", "99" }, new[] { "--package", "safe", "/tmp", "99" },
            new[] { "--package", "safe", "opt", "0" }, new[] { "--package", "safe", "opt", "4294967297" },
            new[] { "--package", "safe", "opt", "01" }, new[] { "--package", "safe\n", "opt", "99" },
            new[] { "--xattrs", "../escape", "opt", "99" }, new[] { "--xattrs", "com.tv.store", "opt", "99" },
            new[] { "--xattrs", "safe", "/tmp", "99" }, new[] { "--xattrs", "safe", "opt", "0" },
            new[] { "--xattrs", "safe", "opt", "33554433" }, new[] { "--xattrs", "safe", "opt", "01" },
            new[] { "--xattrs", "safe\n", "opt", "99" }, new[] { "--xattrs", "safe", "opt", "99", "extra" },
            new[] { "--unknown", "safe", "opt", "99" } })
        {
            try { Parse(bad); throw new Exception("accepted bad request"); }
            catch (InvalidDataException) { count++; }
        }
        Request package = Parse(new[] { "--package", "Valid.app-1", "opt", "4294967296" });
        if (package.Kind != CaptureKind.Package || package.Limit != Maximum) throw new Exception();
        Request xattrs = Parse(new[] { "--xattrs", "Valid.app-1", "rom", "33554432" });
        if (xattrs.Kind != CaptureKind.Xattrs || xattrs.Limit != MaximumXattrs) throw new Exception();
        count += 2;
        using (var stream = new MemoryStream())
        {
            WriteRecord(stream, "BEGIN package test");
            if (Encoding.ASCII.GetString(stream.ToArray()) != Tag + "BEGIN package test\n") throw new Exception();
            count++;
        }
        foreach (Stat invalid in new[] { new Stat { Mode = 0x1000 }, new Stat { Mode = 0xc000 }, new Stat { Mode = 0x8000, Nlink = 2 } })
        {
            try { CheckStat(invalid); throw new Exception("accepted unsafe object"); }
            catch (InvalidDataException) { count++; }
        }
        CheckStat(new Stat { Mode = 0xa000, Nlink = 1 });
        CheckStat(new Stat { Mode = 0x4000, Nlink = 2 });
        count += 2;
        string[] args = TarArguments(new Request(CaptureKind.Package, "fixture", "/opt/usr/apps", 999));
        if (args.Contains("--hard-dereference") || args.Contains("--dereference") || args[^2] != "--"
            || args[^1] != "." || !args.Contains("--directory=/opt/usr/apps/fixture")) throw new Exception();
        count++;
        if (!PrivilegeSatisfied(LinuxPrivilegeStatus.Parse("Uid:\t0\t0\t0\t0\nCapEff:\t200000\n"))) throw new Exception();
        if (PrivilegeSatisfied(LinuxPrivilegeStatus.Parse("Uid:\t1000\t1000\t1000\t1000\nCapEff:\t200000\n"))) throw new Exception();
        if (PrivilegeSatisfied(LinuxPrivilegeStatus.Parse("Uid:\t0\t0\t0\t0\nCapEff:\t0\n"))) throw new Exception();
        count += 3;
        foreach (string badStatus in new[] { "CapEff:\t1\n", "Uid:\t0\t0\t0\t0\n", "Uid:\t0\t0\t0\t0\nCapEff:\tzz\n" })
        {
            try { LinuxPrivilegeStatus.Parse(badStatus); throw new Exception("accepted bad process status"); }
            catch (BackupException) { count++; }
        }
        byte[] payload = new byte[70000];
        for (int i = 0; i < payload.Length; i++) payload[i] = (byte)(i * 31 + 7);
        using (var sink = new MemoryStream())
        {
            var framing = new FramingStream(sink, 4L << 30);
            framing.Write(payload, 0, payload.Length);
            string expectedHash = Convert.ToHexString(SHA256.HashData(payload)).ToLowerInvariant();
            if (framing.Count != 3 || framing.Total != payload.Length || framing.FinishHash() != expectedHash) throw new Exception();
            string text = Encoding.ASCII.GetString(sink.ToArray());
            if (!text.EndsWith("\n", StringComparison.Ordinal)) throw new Exception();
            string[] lines = text.Split('\n', StringSplitOptions.RemoveEmptyEntries);
            if (lines.Length != 3) throw new Exception();
            using var rebuilt = new MemoryStream();
            for (int i = 0; i < lines.Length; i++)
            {
                string[] parts = lines[i].Split(' ');
                if (parts.Length != 5 || parts[0] != Tag.TrimEnd() || parts[1] != "DATA"
                    || parts[2] != i.ToString(CultureInfo.InvariantCulture)) throw new Exception();
                byte[] chunk = Convert.FromBase64String(parts[3]);
                if (chunk.Length == 0 || chunk.Length > Chunk) throw new Exception();
                if (parts[4] != Convert.ToHexString(SHA256.HashData(chunk)).ToLowerInvariant()) throw new Exception();
                rebuilt.Write(chunk, 0, chunk.Length);
            }
            if (!rebuilt.ToArray().SequenceEqual(payload)) throw new Exception();
            count++;
            framing.Dispose();
            sink.WriteByte(0x41);
            if (sink.Length != text.Length + 1) throw new Exception();
            count++;
        }
        using (var sink = new MemoryStream())
        {
            var framing = new FramingStream(sink, 100);
            bool rejected = false;
            try { framing.Write(new byte[101], 0, 101); } catch (InvalidDataException) { rejected = true; }
            string text = Encoding.ASCII.GetString(sink.ToArray());
            if (!rejected || text.Contains("END", StringComparison.Ordinal)) throw new Exception();
            sink.WriteByte(0x41);
            framing.Dispose();
            sink.WriteByte(0x42);
            if (sink.Length != 2) throw new Exception();
            count++;
        }
        using (var sink = new MemoryStream())
        {
            var framing = new FramingStream(sink, 40000);
            bool rejected = false;
            try { framing.Write(new byte[41000], 0, 41000); } catch (InvalidDataException) { rejected = true; }
            string text = Encoding.ASCII.GetString(sink.ToArray());
            long written = sink.Length;
            string[] lines = text.Split('\n', StringSplitOptions.RemoveEmptyEntries);
            if (!rejected || !text.StartsWith(Tag + "DATA ", StringComparison.Ordinal)
                || text.Contains("END", StringComparison.Ordinal) || lines.Length != 1) throw new Exception();
            if (framing.Total != Chunk || framing.Count != 1) throw new Exception();
            sink.WriteByte(0x41);
            if (sink.Length != written + 1) throw new Exception();
            count++;
        }
        Console.WriteLine($"CAPTURE SELF-TEST PASS {count}");
        return 0;
    }
}

internal sealed class FramingStream : Stream
{
    private readonly Stream output;
    private readonly long limit;
    private readonly IncrementalHash hash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
    internal FramingStream(Stream output, long limit)
    {
        if (output is null) throw new ArgumentNullException(nameof(output));
        if (!output.CanWrite) throw new ArgumentException("output stream is not writable", nameof(output));
        if (limit <= 0) throw new ArgumentOutOfRangeException(nameof(limit));
        this.output = output;
        this.limit = limit;
    }
    internal long Total { get; private set; }
    internal long Count { get; private set; }
    internal string FinishHash() => Convert.ToHexString(hash.GetHashAndReset()).ToLowerInvariant();
    public override bool CanRead => false;
    public override bool CanSeek => false;
    public override bool CanWrite => true;
    public override long Length => throw new NotSupportedException();
    public override long Position { get => throw new NotSupportedException(); set => throw new NotSupportedException(); }
    public override void Flush() => output.Flush();
    public override int Read(byte[] buffer, int offset, int count) => throw new NotSupportedException();
    public override long Seek(long offset, SeekOrigin origin) => throw new NotSupportedException();
    public override void SetLength(long value) => throw new NotSupportedException();
    public override void Write(byte[] buffer, int offset, int count)
    {
        if (buffer is null) throw new ArgumentNullException(nameof(buffer));
        if (offset < 0 || count < 0 || offset > buffer.Length - count) throw new ArgumentOutOfRangeException(nameof(offset));
        while (count > 0)
        {
            int size = Math.Min(count, Program.Chunk);
            if (size > limit - Total) throw new InvalidDataException();
            hash.AppendData(buffer, offset, size);
            string chunkHash = Convert.ToHexString(SHA256.HashData(buffer.AsSpan(offset, size))).ToLowerInvariant();
            Program.WriteRecord(output, $"DATA {Count} {Convert.ToBase64String(buffer, offset, size)} {chunkHash}");
            Total += size;
            Count++;
            offset += size;
            count -= size;
        }
    }
    protected override void Dispose(bool disposing)
    {
        if (disposing) hash.Dispose();
        base.Dispose(disposing);
    }
}

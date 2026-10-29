using System.Diagnostics;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;

internal static class Program
{
    internal const string Tag = "QUIETTV-CAPTURE/1 ";
    internal const long Maximum = 4L << 30;
    internal const int Chunk = 32768;
    private static string phase = "request";
    // Exact protected token list in the supported profile. Tests check equality.
    internal static readonly string[] Protected = { "setting", "hdmi", "audio", "sound", "volume", "network", "wifi", "wlan", "ethernet", "bluetooth", "security", "csfs", "firmware", "update", "store", "webengine", "chromium", "ewk", "drm", "tv-viewer", "source" };
    internal sealed record Request(string Id, string Parent, long Limit);
    internal static Request Parse(string[] a)
    {
        if (a.Length != 4 || a[0] != "--package"
            || !Regex.IsMatch(a[1], @"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\z")
            || a[1] is "." or ".." || Protected.Any(t => a[1].Contains(t, StringComparison.OrdinalIgnoreCase))
            || a[2] is not ("opt" or "rom") || !Regex.IsMatch(a[3], @"\A[1-9][0-9]{0,10}\z")
            || !long.TryParse(a[3], NumberStyles.None, CultureInfo.InvariantCulture, out long n) || n > Maximum)
            throw new InvalidDataException();
        return new Request(a[1], a[2] == "opt" ? "/opt/usr/apps" : "/usr/apps", n);
    }
    internal static async Task<int> Main(string[] a)
    {
        try
        {
            if (a.SequenceEqual(new[] { "--self-test" })) return SelfTest();
            Request r = Parse(a); // Reject every invalid request before OS inspection/child execution.
            if (!OperatingSystem.IsLinux()) throw new PlatformNotSupportedException();
            await Capture(r);
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
    private static void Write(Stream stdout, string record)
    {
        byte[] bytes = Encoding.ASCII.GetBytes(Tag + record + "\n");
        stdout.Write(bytes, 0, bytes.Length); stdout.Flush();
    }
    private static string[] TarArguments(Request r) => new[] {
        "--create", "--file=-", "--format=gnu", "--numeric-owner", "--one-file-system",
        "--atime-preserve=system", "--directory=" + r.Parent, "--", r.Id };
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
            Write(stdout, "BEGIN package " + r.Id);
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
                Write(stdout, $"DATA {count - 1} {Convert.ToBase64String(buffer, 0, n)} {chunkHash}");
            }
            phase = "tar-wait";
            await child.WaitForExitAsync(deadline.Token); await stderr;
            phase = "tar-exit-" + child.ExitCode.ToString(CultureInfo.InvariantCulture);
            if (child.ExitCode != 0 || total == 0) throw new IOException();
            phase = "snapshot-after";
            if (!before.SequenceEqual(Snapshot(root))) throw new IOException();
            Write(stdout, $"END {count} {total} {Convert.ToHexString(hash.GetHashAndReset()).ToLowerInvariant()}");
        }
        finally
        {
            if (!child.HasExited) { child.Kill(); child.WaitForExit(5000); }
        }
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
            new[] { "--package", "safe", "opt", "01" }, new[] { "--package", "safe\n", "opt", "99" } })
        {
            try { Parse(bad); throw new Exception("accepted bad request"); }
            catch (InvalidDataException) { count++; }
        }
        if (Parse(new[] { "--package", "Valid.app-1", "opt", "4294967296" }).Limit != Maximum) throw new Exception();
        using var stream = new MemoryStream(); Write(stream, "BEGIN package test");
        if (Encoding.ASCII.GetString(stream.ToArray()) != Tag + "BEGIN package test\n") throw new Exception();
        foreach (Stat invalid in new[] { new Stat { Mode = 0x1000 }, new Stat { Mode = 0xc000 }, new Stat { Mode = 0x8000, Nlink = 2 } })
        {
            try { CheckStat(invalid); throw new Exception("accepted unsafe object"); }
            catch (InvalidDataException) { count++; }
        }
        CheckStat(new Stat { Mode = 0xa000, Nlink = 1 });
        CheckStat(new Stat { Mode = 0x4000, Nlink = 2 });
        string[] args = TarArguments(new Request("fixture", "/opt/usr/apps", 999));
        if (args.Contains("--hard-dereference") || args.Contains("--dereference") || args[^2] != "--" || args[^1] != "fixture") throw new Exception();
        Console.WriteLine($"CAPTURE SELF-TEST PASS {count + 5}"); return 0;
    }
}

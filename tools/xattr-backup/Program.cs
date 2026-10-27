using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace XattrBackup;

internal static class Program
{
    internal static int Main(string[] args)
    {
        try
        {
            if (args.Length == 1 && args[0] == "--self-test")
            {
                return SelfTests.Run();
            }

            if (args.Length == 3 && (args[0] == "--dump" || args[0] == "--restore"))
            {
                if (!OperatingSystem.IsLinux())
                {
                    Console.Error.WriteLine(
                        "error: " + args[0] + " requires Linux. Native extended-attribute calls are not available on this platform. No files were touched.");
                    return 2;
                }

                if (!EnsureLinuxXattrPrivileges(args[0]))
                {
                    return 2;
                }

                if (args[0] == "--dump")
                {
                    DumpResult result = Dumper.Dump(args[1], args[2], LinuxXattrStore.Instance);
                    Console.WriteLine(
                        "dump: entries=" + result.Entries + " xattrs=" + result.Xattrs + " manifest_bytes=" + result.ManifestBytes);
                }
                else
                {
                    RestoreResult result = Restorer.Restore(args[1], args[2], LinuxXattrStore.Instance);
                    Console.WriteLine(
                        "restore: entries=" + result.Entries + " set=" + result.XattrsSet + " removed=" + result.XattrsRemoved + " unchanged=" + result.XattrsUnchanged);
                }

                return 0;
            }

            Console.Error.WriteLine("usage: XattrBackup --dump ROOT MANIFEST | --restore ROOT MANIFEST | --self-test");
            return 2;
        }
        catch (BackupException ex)
        {
            Console.Error.WriteLine("error: " + ex.Message);
            return 1;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("error: unexpected failure: " + ex.Message);
            return 1;
        }
    }

    private static bool EnsureLinuxXattrPrivileges(string command)
    {
        LinuxPrivilegeStatus status;
        try
        {
            status = LinuxPrivilegeStatus.ReadCurrent();
        }
        catch (BackupException ex)
        {
            Console.Error.WriteLine("error: cannot verify Linux xattr privileges: " + ex.Message + ". No files were touched.");
            return false;
        }

        if (!status.IsSatisfied)
        {
            Console.Error.WriteLine(
                "error: " + command + " requires effective UID 0 and CAP_SYS_ADMIN (capability bit 21) so trusted.* attributes are visible. " +
                "Detected euid=" + status.EffectiveUid + ", cap_eff=0x" + status.CapEff.ToString("x", CultureInfo.InvariantCulture) +
                ". No files were touched.");
            return false;
        }

        return true;
    }
}

internal sealed class LinuxPrivilegeStatus
{
    private const int CapSysAdminBit = 21;

    private LinuxPrivilegeStatus(long effectiveUid, ulong capEff)
    {
        EffectiveUid = effectiveUid;
        CapEff = capEff;
    }

    internal long EffectiveUid { get; }

    internal ulong CapEff { get; }

    internal bool IsEffectiveRoot => EffectiveUid == 0;

    internal bool HasCapSysAdmin => (CapEff & (1UL << CapSysAdminBit)) != 0;

    internal bool IsSatisfied => IsEffectiveRoot && HasCapSysAdmin;

    internal static LinuxPrivilegeStatus ReadCurrent()
    {
        if (!OperatingSystem.IsLinux())
        {
            throw new BackupException("Linux privilege check requires Linux");
        }

        string contents;
        try
        {
            contents = File.ReadAllText("/proc/self/status");
        }
        catch (Exception ex) when (ex is IOException || ex is UnauthorizedAccessException)
        {
            throw new BackupException("cannot read /proc/self/status: " + ex.Message);
        }

        return Parse(contents);
    }

    internal static LinuxPrivilegeStatus Parse(string statusText)
    {
        if (statusText is null)
        {
            throw new BackupException("process status is missing");
        }

        long? effectiveUid = null;
        ulong? capEff = null;

        foreach (string rawLine in statusText.Split('\n'))
        {
            string line = rawLine.TrimEnd('\r');
            if (line.StartsWith("Uid:", StringComparison.Ordinal))
            {
                if (effectiveUid is not null)
                {
                    throw new BackupException("process status contains duplicate Uid lines");
                }

                string[] fields = line.Substring(4).Split(
                    new[] { ' ', '\t' },
                    StringSplitOptions.RemoveEmptyEntries);
                if (fields.Length < 2 ||
                    !long.TryParse(fields[1], NumberStyles.Integer, CultureInfo.InvariantCulture, out long uid))
                {
                    throw new BackupException("process status has a malformed Uid line");
                }

                effectiveUid = uid;
            }
            else if (line.StartsWith("CapEff:", StringComparison.Ordinal))
            {
                if (capEff is not null)
                {
                    throw new BackupException("process status contains duplicate CapEff lines");
                }

                string[] fields = line.Substring(7).Split(
                    new[] { ' ', '\t' },
                    StringSplitOptions.RemoveEmptyEntries);
                if (fields.Length < 1 ||
                    !ulong.TryParse(fields[0], NumberStyles.AllowHexSpecifier, CultureInfo.InvariantCulture, out ulong capabilities))
                {
                    throw new BackupException("process status has a malformed CapEff line");
                }

                capEff = capabilities;
            }
        }

        if (effectiveUid is null)
        {
            throw new BackupException("process status has no Uid line");
        }

        if (capEff is null)
        {
            throw new BackupException("process status has no CapEff line");
        }

        return new LinuxPrivilegeStatus(effectiveUid.Value, capEff.Value);
    }
}

internal sealed class BackupException : Exception
{
    internal BackupException(string message) : base(message)
    {
    }
}

internal static class Limits
{
    internal const int MaxEntries = 15000;
    internal const long MaxEntryXattrBytes = 1L << 20;
    internal const long MaxManifestBytes = 32L << 20;
    internal const int NativeAttempts = 8;
    internal const int MaxReconcilePasses = 4;
}

internal enum EntryKind
{
    File,
    Directory,
    Symlink,
}

internal static class KindNames
{
    internal static string ToJson(EntryKind kind) => kind switch
    {
        EntryKind.File => "file",
        EntryKind.Directory => "directory",
        EntryKind.Symlink => "symlink",
        _ => throw new BackupException("internal error: unknown entry kind"),
    };

    internal static EntryKind Parse(string value) => value switch
    {
        "file" => EntryKind.File,
        "directory" => EntryKind.Directory,
        "symlink" => EntryKind.Symlink,
        _ => throw new BackupException("manifest contains an unknown entry kind '" + value + "'"),
    };
}

internal static class TextRules
{
    internal static readonly UTF8Encoding StrictUtf8 = new UTF8Encoding(encoderShouldEmitUTF8Identifier: false, throwOnInvalidBytes: true);

    internal static void EnsureValidUnicode(string value, string what)
    {
        try
        {
            StrictUtf8.GetByteCount(value);
        }
        catch (EncoderFallbackException)
        {
            throw new BackupException(what + " contains invalid Unicode");
        }
    }

    internal static bool HasControlCharacters(string value)
    {
        foreach (char ch in value)
        {
            if (ch < 0x20 || ch == 0x7f)
            {
                return true;
            }
        }

        return false;
    }
}

internal static class XattrRules
{
    internal static void ValidateName(string name)
    {
        if (name.Length == 0)
        {
            throw new BackupException("extended-attribute name is empty");
        }

        TextRules.EnsureValidUnicode(name, "extended-attribute name");

        if (name.IndexOf('\0') >= 0)
        {
            throw new BackupException("extended-attribute name contains NUL");
        }

        if (name.IndexOf('/') >= 0)
        {
            throw new BackupException("extended-attribute name contains '/'");
        }

        if (TextRules.HasControlCharacters(name))
        {
            throw new BackupException("extended-attribute name contains control characters");
        }

        if (TextRules.StrictUtf8.GetByteCount(name) > 255)
        {
            throw new BackupException("extended-attribute name exceeds 255 bytes");
        }
    }
}

internal static class XattrLimits
{
    internal static void Validate(IReadOnlyDictionary<string, byte[]> attrs, string relativePath)
    {
        long total = 0;
        foreach (KeyValuePair<string, byte[]> attr in attrs)
        {
            XattrRules.ValidateName(attr.Key);
            total += TextRules.StrictUtf8.GetByteCount(attr.Key) + attr.Value.LongLength;
            if (total > Limits.MaxEntryXattrBytes)
            {
                throw new BackupException(
                    "extended attributes for '" + relativePath + "' exceed the 1 MiB per-entry limit");
            }
        }
    }
}

internal static class PathRules
{
    internal static StringComparison PathComparison =>
        OperatingSystem.IsWindows() ? StringComparison.OrdinalIgnoreCase : StringComparison.Ordinal;

    internal static string TrimTrailingSeparators(string path)
    {
        string trimmed = path.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
        return trimmed;
    }

    internal static void ValidateManifestRelativePath(string path, bool allowRoot, string context)
    {
        if (path.Length == 0)
        {
            throw new BackupException(context + ": relative path is empty");
        }

        TextRules.EnsureValidUnicode(path, context + ": relative path");

        if (path.IndexOf('\0') >= 0)
        {
            throw new BackupException(context + ": relative path contains NUL");
        }

        if (path == ".")
        {
            if (!allowRoot)
            {
                throw new BackupException(context + ": '.' is only allowed for the root entry");
            }

            return;
        }

        if (path.IndexOf('\\') >= 0)
        {
            throw new BackupException(context + ": relative path contains backslash");
        }

        if (path[0] == '/')
        {
            throw new BackupException(context + ": relative path is absolute");
        }

        if (path.Length >= 2 && IsAsciiLetter(path[0]) && path[1] == ':')
        {
            throw new BackupException(context + ": relative path looks like a drive path");
        }

        string[] parts = path.Split('/');
        foreach (string part in parts)
        {
            if (part.Length == 0)
            {
                throw new BackupException(context + ": relative path contains an empty component");
            }

            if (part == ".")
            {
                throw new BackupException(context + ": relative path contains a '.' component");
            }

            if (part == "..")
            {
                throw new BackupException(context + ": relative path contains a '..' component");
            }

            if (TextRules.HasControlCharacters(part))
            {
                throw new BackupException(context + ": relative path contains control characters");
            }
        }
    }

    internal static RootInfo ValidateRoot(string rootArgument, bool rejectSymlink = true)
    {
        if (string.IsNullOrEmpty(rootArgument))
        {
            throw new BackupException("ROOT is empty");
        }

        if (rootArgument.IndexOf('\0') >= 0)
        {
            throw new BackupException("ROOT contains NUL");
        }

        string full;
        try
        {
            full = Path.GetFullPath(rootArgument);
        }
        catch (Exception ex) when (ex is ArgumentException || ex is NotSupportedException || ex is PathTooLongException)
        {
            throw new BackupException("ROOT is not a valid path");
        }

        string trimmed = TrimTrailingSeparators(full);
        string pathRoot = TrimTrailingSeparators(Path.GetPathRoot(full) ?? string.Empty);
        if (trimmed.Length == 0 || string.Equals(trimmed, pathRoot, PathComparison))
        {
            throw new BackupException("ROOT must not be a filesystem root");
        }

        DirectoryInfo info = new DirectoryInfo(trimmed);
        if (rejectSymlink && info.LinkTarget is not null)
        {
            throw new BackupException("ROOT must not be a symbolic link");
        }

        if (!info.Exists || !Directory.Exists(trimmed))
        {
            throw new BackupException("ROOT does not exist or is not a directory");
        }

        string basename = Path.GetFileName(trimmed);
        if (string.IsNullOrEmpty(basename))
        {
            throw new BackupException("ROOT has no usable basename");
        }

        return new RootInfo(trimmed, basename);
    }

    internal static string ValidateManifestOutput(RootInfo root, string manifestArgument)
    {
        if (string.IsNullOrEmpty(manifestArgument))
        {
            throw new BackupException("MANIFEST is empty");
        }

        if (manifestArgument.IndexOf('\0') >= 0)
        {
            throw new BackupException("MANIFEST contains NUL");
        }

        string full;
        try
        {
            full = Path.GetFullPath(manifestArgument);
        }
        catch (Exception ex) when (ex is ArgumentException || ex is NotSupportedException || ex is PathTooLongException)
        {
            throw new BackupException("MANIFEST is not a valid path");
        }

        string trimmed = TrimTrailingSeparators(full);
        if (trimmed.Length == 0)
        {
            throw new BackupException("MANIFEST must not be a filesystem root");
        }

        if (Directory.Exists(trimmed))
        {
            throw new BackupException("MANIFEST path is a directory");
        }

        string? parent = Path.GetDirectoryName(trimmed);
        if (string.IsNullOrEmpty(parent) || !Directory.Exists(parent))
        {
            throw new BackupException("MANIFEST directory does not exist");
        }

        string canonicalManifest = Canonicalize(trimmed);
        string canonicalRoot = Canonicalize(root.FullPath);
        if (IsSameOrWithin(canonicalManifest, canonicalRoot))
        {
            throw new BackupException("MANIFEST must be outside ROOT");
        }

        return trimmed;
    }

    internal static string ValidateManifestInput(string manifestArgument)
    {
        if (string.IsNullOrEmpty(manifestArgument))
        {
            throw new BackupException("MANIFEST is empty");
        }

        if (manifestArgument.IndexOf('\0') >= 0)
        {
            throw new BackupException("MANIFEST contains NUL");
        }

        string full;
        try
        {
            full = Path.GetFullPath(manifestArgument);
        }
        catch (Exception ex) when (ex is ArgumentException || ex is NotSupportedException || ex is PathTooLongException)
        {
            throw new BackupException("MANIFEST is not a valid path");
        }

        string trimmed = TrimTrailingSeparators(full);
        if (trimmed.Length == 0)
        {
            throw new BackupException("MANIFEST must not be a filesystem root");
        }

        return trimmed;
    }

    internal static string Canonicalize(string path)
    {
        string full;
        try
        {
            full = Path.GetFullPath(path);
        }
        catch (Exception ex) when (ex is ArgumentException || ex is NotSupportedException || ex is PathTooLongException)
        {
            throw new BackupException("path is not valid: " + path);
        }

        string root = Path.GetPathRoot(full) ?? string.Empty;
        string remainder = full.Substring(root.Length);
        string current = root;
        bool missing = false;
        string[] parts = remainder.Split(
            new[] { Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar },
            StringSplitOptions.RemoveEmptyEntries);

        foreach (string part in parts)
        {
            string next = Path.Combine(current, part);
            if (!missing)
            {
                try
                {
                    FileSystemInfo info = Directory.Exists(next) ? new DirectoryInfo(next) : new FileInfo(next);
                    if (info.LinkTarget is not null)
                    {
                        FileSystemInfo? resolved = info.ResolveLinkTarget(returnFinalTarget: true);
                        if (resolved is not null)
                        {
                            next = resolved.FullName;
                        }
                    }

                    if (!File.Exists(next) && !Directory.Exists(next))
                    {
                        missing = true;
                    }
                }
                catch (IOException)
                {
                    // Fall back to lexical containment for paths we cannot inspect.
                }
                catch (UnauthorizedAccessException)
                {
                    // Fall back to lexical containment for paths we cannot inspect.
                }
            }

            current = next;
        }

        string result = TrimTrailingSeparators(current);
        if (result.Length == 0)
        {
            return root;
        }

        return result;
    }

    internal static bool IsSameOrWithin(string candidateCanonical, string parentCanonical)
    {
        string candidate = TrimTrailingSeparators(candidateCanonical);
        string parent = TrimTrailingSeparators(parentCanonical);
        if (string.Equals(candidate, parent, PathComparison))
        {
            return true;
        }

        if (candidate.Length <= parent.Length)
        {
            return false;
        }

        if (!candidate.StartsWith(parent, PathComparison))
        {
            return false;
        }

        char boundary = candidate[parent.Length];
        return boundary == Path.DirectorySeparatorChar || boundary == Path.AltDirectorySeparatorChar;
    }

    private static bool IsAsciiLetter(char ch) =>
        (ch >= 'A' && ch <= 'Z') || (ch >= 'a' && ch <= 'z');
}

internal sealed class RootInfo
{
    internal RootInfo(string fullPath, string basename)
    {
        FullPath = fullPath;
        Basename = basename;
    }

    internal string FullPath { get; }

    internal string Basename { get; }
}

internal sealed class Manifest
{
    internal int Version { get; set; } = 1;

    internal string RootBasename { get; set; } = string.Empty;

    internal List<ManifestEntry> Entries { get; set; } = new List<ManifestEntry>();
}

internal sealed class ManifestEntry
{
    internal string Path { get; set; } = string.Empty;

    internal EntryKind Kind { get; set; }

    internal SortedDictionary<string, byte[]> Xattrs { get; set; } = new SortedDictionary<string, byte[]>(StringComparer.Ordinal);

    internal string? Sha256 { get; set; }

    internal string? LinkTarget { get; set; }
}

internal sealed class ManifestDto
{
    [JsonPropertyName("version")]
    public int Version { get; set; }

    [JsonPropertyName("rootBasename")]
    public string RootBasename { get; set; } = string.Empty;

    [JsonPropertyName("entries")]
    public List<EntryDto> Entries { get; set; } = new List<EntryDto>();
}

internal sealed class EntryDto
{
    [JsonPropertyName("path")]
    public string Path { get; set; } = string.Empty;

    [JsonPropertyName("kind")]
    public string Kind { get; set; } = string.Empty;

    [JsonPropertyName("xattrs")]
    public SortedDictionary<string, string> Xattrs { get; set; } = new SortedDictionary<string, string>(StringComparer.Ordinal);

    [JsonPropertyName("sha256")]
    public string? Sha256 { get; set; }

    [JsonPropertyName("linkTarget")]
    public string? LinkTarget { get; set; }
}

internal static class ManifestCodec
{
    private static readonly JsonSerializerOptions WriteOptions = new JsonSerializerOptions
    {
        WriteIndented = true,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    };

    internal static byte[] Serialize(Manifest manifest)
    {
        if (manifest.Version != 1)
        {
            throw new BackupException("internal error: only manifest version 1 can be written");
        }

        if (manifest.Entries.Count > Limits.MaxEntries)
        {
            throw new BackupException("manifest exceeds the " + Limits.MaxEntries + " entry limit");
        }

        long estimate = 128;
        ManifestDto dto = new ManifestDto
        {
            Version = manifest.Version,
            RootBasename = manifest.RootBasename,
            Entries = new List<EntryDto>(manifest.Entries.Count),
        };

        foreach (ManifestEntry entry in manifest.Entries)
        {
            validateEntryForWrite(entry);

            SortedDictionary<string, string> xattrs = new SortedDictionary<string, string>(StringComparer.Ordinal);
            long entryXattrBytes = 0;
            foreach (KeyValuePair<string, byte[]> attr in entry.Xattrs)
            {
                XattrRules.ValidateName(attr.Key);
                entryXattrBytes += TextRules.StrictUtf8.GetByteCount(attr.Key) + attr.Value.LongLength;
                if (entryXattrBytes > Limits.MaxEntryXattrBytes)
                {
                    throw new BackupException(
                        "extended attributes for '" + entry.Path + "' exceed the 1 MiB per-entry limit");
                }

                string base64 = Convert.ToBase64String(attr.Value);
                xattrs.Add(attr.Key, base64);
                estimate += (long)TextRules.StrictUtf8.GetByteCount(attr.Key) * 6L + base64.Length + 8L;
            }

            estimate += 96L + (long)TextRules.StrictUtf8.GetByteCount(entry.Path) * 6L;
            estimate += entry.Sha256?.Length ?? 0;
            if (entry.LinkTarget is not null)
            {
                estimate += (long)TextRules.StrictUtf8.GetByteCount(entry.LinkTarget) * 6L;
            }

            if (estimate > Limits.MaxManifestBytes)
            {
                throw new BackupException("manifest would exceed the 32 MiB limit");
            }

            dto.Entries.Add(new EntryDto
            {
                Path = entry.Path,
                Kind = KindNames.ToJson(entry.Kind),
                Xattrs = xattrs,
                Sha256 = entry.Sha256,
                LinkTarget = entry.LinkTarget,
            });
        }

        byte[] bytes = JsonSerializer.SerializeToUtf8Bytes(dto, WriteOptions);
        if (bytes.LongLength > Limits.MaxManifestBytes)
        {
            throw new BackupException("manifest exceeds the 32 MiB limit");
        }

        return bytes;
    }

    internal static Manifest ReadFile(string manifestFullPath)
    {
        FileInfo info = new FileInfo(manifestFullPath);
        if (info.LinkTarget is not null)
        {
            throw new BackupException("MANIFEST must not be a symbolic link");
        }

        if (!info.Exists || (info.Attributes & FileAttributes.Directory) != 0)
        {
            throw new BackupException("MANIFEST does not exist or is not a regular file");
        }

        using FileStream stream = new FileStream(manifestFullPath, FileMode.Open, FileAccess.Read, FileShare.Read);
        if (stream.Length > Limits.MaxManifestBytes)
        {
            throw new BackupException("manifest exceeds the 32 MiB limit");
        }

        if (stream.Length == 0)
        {
            throw new BackupException("manifest is empty");
        }

        if (stream.Length > int.MaxValue)
        {
            throw new BackupException("manifest is too large");
        }

        byte[] bytes = new byte[(int)stream.Length];
        int offset = 0;
        while (offset < bytes.Length)
        {
            int read = stream.Read(bytes, offset, bytes.Length - offset);
            if (read <= 0)
            {
                throw new BackupException("manifest was truncated while reading");
            }

            offset += read;
        }

        if (stream.ReadByte() != -1)
        {
            throw new BackupException("manifest changed while reading");
        }

        return Parse(bytes);
    }

    internal static Manifest Parse(byte[] bytes)
    {
        if (bytes.LongLength > Limits.MaxManifestBytes)
        {
            throw new BackupException("manifest exceeds the 32 MiB limit");
        }

        JsonDocument document;
        try
        {
            document = JsonDocument.Parse(bytes, new JsonDocumentOptions
            {
                AllowTrailingCommas = false,
                CommentHandling = JsonCommentHandling.Disallow,
                MaxDepth = 64,
            });
        }
        catch (JsonException ex)
        {
            throw new BackupException("manifest is not valid JSON: " + ex.Message);
        }

        using (document)
        {
            Dictionary<string, JsonElement> root = ReadObject(
                document.RootElement,
                "manifest",
                new[] { "version", "rootBasename", "entries" });

            if (!root.TryGetValue("version", out JsonElement versionElement) ||
                versionElement.ValueKind != JsonValueKind.Number ||
                !versionElement.TryGetInt32(out int version))
            {
                throw new BackupException("manifest version is missing or not an integer");
            }

            if (version != 1)
            {
                throw new BackupException("unsupported manifest version " + version);
            }

            if (!root.TryGetValue("rootBasename", out JsonElement basenameElement) ||
                basenameElement.ValueKind != JsonValueKind.String)
            {
                throw new BackupException("manifest rootBasename is missing or not a string");
            }

            string rootBasename = basenameElement.GetString() ?? string.Empty;
            validateRootBasename(rootBasename);

            if (!root.TryGetValue("entries", out JsonElement entriesElement) ||
                entriesElement.ValueKind != JsonValueKind.Array)
            {
                throw new BackupException("manifest entries is missing or not an array");
            }

            int entryCount = entriesElement.GetArrayLength();
            if (entryCount == 0)
            {
                throw new BackupException("manifest contains no entries");
            }

            if (entryCount > Limits.MaxEntries)
            {
                throw new BackupException("manifest exceeds the " + Limits.MaxEntries + " entry limit");
            }

            List<ManifestEntry> entries = new List<ManifestEntry>(entryCount);
            foreach (JsonElement entryElement in entriesElement.EnumerateArray())
            {
                entries.Add(parseEntry(entryElement, entries.Count));
            }

            if (entries[0].Path != ".")
            {
                throw new BackupException("manifest root entry '.' must be first");
            }

            if (entries[0].Kind != EntryKind.Directory)
            {
                throw new BackupException("manifest root entry '.' must be a directory");
            }

            validateMembership(entries);

            return new Manifest
            {
                Version = version,
                RootBasename = rootBasename,
                Entries = entries,
            };
        }
    }

    private static ManifestEntry parseEntry(JsonElement element, int index)
    {
        string context = "manifest entry " + index;
        Dictionary<string, JsonElement> properties = ReadObject(
            element,
            context,
            new[] { "path", "kind", "xattrs", "sha256", "linkTarget" });

        if (!properties.TryGetValue("path", out JsonElement pathElement) ||
            pathElement.ValueKind != JsonValueKind.String)
        {
            throw new BackupException(context + ": path is missing or not a string");
        }

        string path = pathElement.GetString() ?? string.Empty;
        PathRules.ValidateManifestRelativePath(path, allowRoot: true, context);

        if (!properties.TryGetValue("kind", out JsonElement kindElement) ||
            kindElement.ValueKind != JsonValueKind.String)
        {
            throw new BackupException(context + ": kind is missing or not a string");
        }

        EntryKind kind = KindNames.Parse(kindElement.GetString() ?? string.Empty);

        if (!properties.TryGetValue("xattrs", out JsonElement xattrsElement) ||
            xattrsElement.ValueKind != JsonValueKind.Object)
        {
            throw new BackupException(context + ": xattrs is missing or not an object");
        }

        SortedDictionary<string, byte[]> xattrs = new SortedDictionary<string, byte[]>(StringComparer.Ordinal);
        long xattrBytes = 0;
        Dictionary<string, JsonElement> xattrProperties = ReadObjectWithDynamicKeys(xattrsElement, context + " xattrs");
        foreach (KeyValuePair<string, JsonElement> property in xattrProperties)
        {
            XattrRules.ValidateName(property.Key);
            if (property.Value.ValueKind != JsonValueKind.String)
            {
                throw new BackupException(context + ": xattr value is not a string");
            }

            string base64 = property.Value.GetString() ?? string.Empty;
            long maxEncoded = ((Limits.MaxEntryXattrBytes + 2) / 3) * 4 + 4;
            if (base64.Length > maxEncoded)
            {
                throw new BackupException(context + ": xattr value exceeds the 1 MiB per-entry limit");
            }

            byte[] value;
            try
            {
                value = Convert.FromBase64String(base64);
            }
            catch (FormatException)
            {
                throw new BackupException(context + ": xattr value is not valid base64");
            }

            xattrBytes += TextRules.StrictUtf8.GetByteCount(property.Key) + value.LongLength;
            if (xattrBytes > Limits.MaxEntryXattrBytes)
            {
                throw new BackupException(context + ": xattrs exceed the 1 MiB per-entry limit");
            }

            xattrs.Add(property.Key, value);
        }

        string? sha256 = null;
        if (properties.TryGetValue("sha256", out JsonElement shaElement) && shaElement.ValueKind != JsonValueKind.Null)
        {
            if (shaElement.ValueKind != JsonValueKind.String)
            {
                throw new BackupException(context + ": sha256 is not a string");
            }

            sha256 = shaElement.GetString() ?? string.Empty;
            if (sha256.Length != 64)
            {
                throw new BackupException(context + ": sha256 must contain 64 hexadecimal characters");
            }

            foreach (char ch in sha256)
            {
                if (!((ch >= '0' && ch <= '9') || (ch >= 'a' && ch <= 'f') || (ch >= 'A' && ch <= 'F')))
                {
                    throw new BackupException(context + ": sha256 must contain 64 hexadecimal characters");
                }
            }

            sha256 = sha256.ToLowerInvariant();
        }

        string? linkTarget = null;
        if (properties.TryGetValue("linkTarget", out JsonElement linkElement) && linkElement.ValueKind != JsonValueKind.Null)
        {
            if (linkElement.ValueKind != JsonValueKind.String)
            {
                throw new BackupException(context + ": linkTarget is not a string");
            }

            linkTarget = linkElement.GetString() ?? string.Empty;
            if (linkTarget.Length == 0)
            {
                throw new BackupException(context + ": linkTarget is empty");
            }

            if (linkTarget.IndexOf('\0') >= 0)
            {
                throw new BackupException(context + ": linkTarget contains NUL");
            }

            TextRules.EnsureValidUnicode(linkTarget, context + ": linkTarget");
        }

        switch (kind)
        {
            case EntryKind.File:
                if (sha256 is null)
                {
                    throw new BackupException(context + ": file entry is missing sha256");
                }

                if (linkTarget is not null)
                {
                    throw new BackupException(context + ": file entry must not have a linkTarget");
                }

                break;
            case EntryKind.Directory:
                if (sha256 is not null || linkTarget is not null)
                {
                    throw new BackupException(context + ": directory entry must not have sha256 or linkTarget");
                }

                break;
            case EntryKind.Symlink:
                if (linkTarget is null)
                {
                    throw new BackupException(context + ": symlink entry is missing linkTarget");
                }

                if (sha256 is not null)
                {
                    throw new BackupException(context + ": symlink entry must not have sha256");
                }

                break;
            default:
                throw new BackupException(context + ": unsupported entry kind");
        }

        return new ManifestEntry
        {
            Path = path,
            Kind = kind,
            Xattrs = xattrs,
            Sha256 = sha256,
            LinkTarget = linkTarget,
        };
    }

    private static void validateMembership(List<ManifestEntry> entries)
    {
        Dictionary<string, ManifestEntry> byPath = new Dictionary<string, ManifestEntry>(StringComparer.Ordinal);
        foreach (ManifestEntry entry in entries)
        {
            if (!byPath.TryAdd(entry.Path, entry))
            {
                throw new BackupException("manifest contains duplicate path '" + entry.Path + "'");
            }
        }

        foreach (ManifestEntry entry in entries)
        {
            if (entry.Path == ".")
            {
                continue;
            }

            int slash = entry.Path.LastIndexOf('/');
            string parentPath = slash < 0 ? "." : entry.Path.Substring(0, slash);
            if (!byPath.TryGetValue(parentPath, out ManifestEntry? parent))
            {
                throw new BackupException("manifest entry '" + entry.Path + "' has no parent entry");
            }

            if (parent.Kind != EntryKind.Directory)
            {
                throw new BackupException("manifest entry '" + entry.Path + "' has a non-directory ancestor '" + parentPath + "'");
            }
        }
    }

    private static void validateRootBasename(string rootBasename)
    {
        if (rootBasename.Length == 0)
        {
            throw new BackupException("manifest rootBasename is empty");
        }

        TextRules.EnsureValidUnicode(rootBasename, "manifest rootBasename");
        if (rootBasename.IndexOf('\0') >= 0 ||
            rootBasename.IndexOf('/') >= 0 ||
            rootBasename == "." ||
            rootBasename == "..")
        {
            throw new BackupException("manifest rootBasename is not a plain basename");
        }
    }

    private static void validateEntryForWrite(ManifestEntry entry)
    {
        PathRules.ValidateManifestRelativePath(entry.Path, allowRoot: true, "manifest");
        switch (entry.Kind)
        {
            case EntryKind.File:
                if (entry.Sha256 is null || entry.Sha256.Length != 64 || entry.LinkTarget is not null)
                {
                    throw new BackupException("internal error: invalid file entry '" + entry.Path + "'");
                }

                break;
            case EntryKind.Directory:
                if (entry.Sha256 is not null || entry.LinkTarget is not null)
                {
                    throw new BackupException("internal error: invalid directory entry '" + entry.Path + "'");
                }

                break;
            case EntryKind.Symlink:
                if (string.IsNullOrEmpty(entry.LinkTarget) || entry.Sha256 is not null)
                {
                    throw new BackupException("internal error: invalid symlink entry '" + entry.Path + "'");
                }

                break;
            default:
                throw new BackupException("internal error: invalid entry kind for '" + entry.Path + "'");
        }
    }

    private static Dictionary<string, JsonElement> ReadObject(JsonElement element, string context, string[] allowed)
    {
        if (element.ValueKind != JsonValueKind.Object)
        {
            throw new BackupException(context + " is not a JSON object");
        }

        HashSet<string> allowedNames = new HashSet<string>(allowed, StringComparer.Ordinal);
        Dictionary<string, JsonElement> properties = new Dictionary<string, JsonElement>(StringComparer.Ordinal);
        foreach (JsonProperty property in element.EnumerateObject())
        {
            if (!allowedNames.Contains(property.Name))
            {
                throw new BackupException(context + " has unknown property '" + property.Name + "'");
            }

            if (!properties.TryAdd(property.Name, property.Value))
            {
                throw new BackupException(context + " has duplicate property '" + property.Name + "'");
            }
        }

        return properties;
    }

    private static Dictionary<string, JsonElement> ReadObjectWithDynamicKeys(JsonElement element, string context)
    {
        if (element.ValueKind != JsonValueKind.Object)
        {
            throw new BackupException(context + " is not a JSON object");
        }

        Dictionary<string, JsonElement> properties = new Dictionary<string, JsonElement>(StringComparer.Ordinal);
        foreach (JsonProperty property in element.EnumerateObject())
        {
            if (!properties.TryAdd(property.Name, property.Value))
            {
                throw new BackupException(context + " has duplicate property '" + property.Name + "'");
            }
        }

        return properties;
    }
}

internal sealed class TreeEntry
{
    internal TreeEntry(string relativePath, string absolutePath, EntryKind kind, string? linkTarget, string? sha256)
    {
        RelativePath = relativePath;
        AbsolutePath = absolutePath;
        Kind = kind;
        LinkTarget = linkTarget;
        Sha256 = sha256;
    }

    internal string RelativePath { get; }

    internal string AbsolutePath { get; }

    internal EntryKind Kind { get; }

    internal string? LinkTarget { get; }

    internal string? Sha256 { get; }
}

internal static class TreeScanner
{
    internal static List<TreeEntry> Scan(string rootFullPath)
    {
        List<TreeEntry> entries = new List<TreeEntry>();
        scanDirectory(rootFullPath, ".", entries);
        return entries;
    }

    internal static string AbsoluteFor(string rootFullPath, string relativePath)
    {
        if (relativePath == ".")
        {
            return rootFullPath;
        }

        return Path.Combine(rootFullPath, relativePath.Replace('/', Path.DirectorySeparatorChar));
    }

    private static void scanDirectory(string directoryFullPath, string relativePath, List<TreeEntry> entries)
    {
        (EntryKind kind, string? linkTarget, string? sha256) = describe(directoryFullPath, relativePath);
        add(entries, relativePath, directoryFullPath, kind, linkTarget, sha256);
        if (kind != EntryKind.Directory)
        {
            return;
        }

        string[] children;
        try
        {
            children = Directory.GetFileSystemEntries(directoryFullPath);
        }
        catch (Exception ex) when (ex is IOException || ex is UnauthorizedAccessException)
        {
            throw new BackupException("cannot enumerate '" + relativePath + "': " + ex.Message);
        }

        Array.Sort(children, StringComparer.Ordinal);
        foreach (string childFullPath in children)
        {
            string name = Path.GetFileName(childFullPath);
            if (name.Length == 0)
            {
                throw new BackupException("cannot determine a name for an entry under '" + relativePath + "'");
            }

            string childRelative = relativePath == "." ? name : relativePath + "/" + name;
            PathRules.ValidateManifestRelativePath(childRelative, allowRoot: false, "tree");
            scanDirectory(childFullPath, childRelative, entries);
        }
    }

    private static void add(
        List<TreeEntry> entries,
        string relativePath,
        string absolutePath,
        EntryKind kind,
        string? linkTarget,
        string? sha256)
    {
        if (entries.Count >= Limits.MaxEntries)
        {
            throw new BackupException("tree exceeds the " + Limits.MaxEntries + " entry limit");
        }

        entries.Add(new TreeEntry(relativePath, absolutePath, kind, linkTarget, sha256));
    }

    private static (EntryKind Kind, string? LinkTarget, string? Sha256) describe(string fullPath, string relativePath)
    {
        string? linkTarget = readLinkTarget(fullPath);
        if (linkTarget is not null)
        {
            return (EntryKind.Symlink, linkTarget, null);
        }

        if (Directory.Exists(fullPath))
        {
            return (EntryKind.Directory, null, null);
        }

        if (!File.Exists(fullPath))
        {
            throw new BackupException("unsupported or unreadable filesystem entry at '" + relativePath + "'");
        }

        return (EntryKind.File, null, hashFile(fullPath, relativePath));
    }

    private static string? readLinkTarget(string fullPath)
    {
        try
        {
            string? target = new FileInfo(fullPath).LinkTarget;
            if (target is not null)
            {
                return target;
            }

            return new DirectoryInfo(fullPath).LinkTarget;
        }
        catch (Exception ex) when (ex is IOException || ex is UnauthorizedAccessException)
        {
            throw new BackupException("cannot inspect '" + fullPath + "': " + ex.Message);
        }
    }

    private static string hashFile(string fullPath, string relativePath)
    {
        try
        {
            using FileStream stream = new FileStream(fullPath, FileMode.Open, FileAccess.Read, FileShare.Read);
            using SHA256 sha = SHA256.Create();
            return Convert.ToHexString(sha.ComputeHash(stream)).ToLowerInvariant();
        }
        catch (Exception ex) when (ex is IOException || ex is UnauthorizedAccessException)
        {
            throw new BackupException("cannot read file '" + relativePath + "': " + ex.Message);
        }
    }
}

internal interface IXattrStore
{
    Dictionary<string, byte[]> Read(string absolutePath, string relativePath);

    void Set(string absolutePath, string relativePath, string name, byte[] value);

    void Remove(string absolutePath, string relativePath, string name);
}

internal sealed class LinuxXattrStore : IXattrStore
{
    internal static readonly LinuxXattrStore Instance = new LinuxXattrStore();

    private const int ERANGE = 34;
    private const int ENOENT = 2;
    private const int ENODATA = 61;

    private LinuxXattrStore()
    {
    }

    public Dictionary<string, byte[]> Read(string absolutePath, string relativePath)
    {
        for (int attempt = 0; attempt < Limits.NativeAttempts; attempt++)
        {
            IntPtr neededPointer = NativeMethods.llistxattr(absolutePath, null, UIntPtr.Zero);
            if (neededPointer.ToInt64() < 0)
            {
                int errno = Marshal.GetLastWin32Error();
                if (errno == ERANGE)
                {
                    continue;
                }

                throw NativeError("list extended attributes for", relativePath, errno);
            }

            long needed = neededPointer.ToInt64();
            if (needed == 0)
            {
                return new Dictionary<string, byte[]>(StringComparer.Ordinal);
            }

            if (needed > Limits.MaxEntryXattrBytes)
            {
                throw new BackupException("extended-attribute name list for '" + relativePath + "' exceeds the 1 MiB limit");
            }

            byte[] buffer = new byte[(int)needed];
            IntPtr gotPointer = NativeMethods.llistxattr(absolutePath, buffer, ToSize(needed));
            if (gotPointer.ToInt64() < 0)
            {
                int errno = Marshal.GetLastWin32Error();
                if (errno == ERANGE)
                {
                    continue;
                }

                throw NativeError("list extended attributes for", relativePath, errno);
            }

            long got = gotPointer.ToInt64();
            if (got > buffer.LongLength)
            {
                continue;
            }

            if (got == 0)
            {
                return new Dictionary<string, byte[]>(StringComparer.Ordinal);
            }

            if (buffer[got - 1] != 0)
            {
                throw new BackupException("extended-attribute name list for '" + relativePath + "' is malformed");
            }

            Dictionary<string, byte[]> result = new Dictionary<string, byte[]>(StringComparer.Ordinal);
            int start = 0;
            for (int i = 0; i < got; i++)
            {
                if (buffer[i] != 0)
                {
                    continue;
                }

                int length = i - start;
                if (length == 0)
                {
                    throw new BackupException("extended-attribute name list for '" + relativePath + "' is malformed");
                }

                string name;
                try
                {
                    name = TextRules.StrictUtf8.GetString(buffer, start, length);
                }
                catch (DecoderFallbackException)
                {
                    throw new BackupException("extended-attribute name for '" + relativePath + "' is not valid UTF-8");
                }

                XattrRules.ValidateName(name);
                byte[] value = ReadValue(absolutePath, relativePath, name);
                if (!result.TryAdd(name, value))
                {
                    throw new BackupException("extended-attribute name list for '" + relativePath + "' contains duplicates");
                }

                start = i + 1;
            }

            if (start != got)
            {
                throw new BackupException("extended-attribute name list for '" + relativePath + "' is malformed");
            }

            XattrLimits.Validate(result, relativePath);
            return result;
        }

        throw new BackupException("cannot list extended attributes for '" + relativePath + "': attribute list kept changing");
    }

    public void Set(string absolutePath, string relativePath, string name, byte[] value)
    {
        XattrRules.ValidateName(name);
        byte[] payload = value.Length == 0 ? new byte[1] : value;
        int result = NativeMethods.lsetxattr(absolutePath, name, payload, ToSize(value.LongLength), 0);
        if (result != 0)
        {
            throw NativeError("set extended attribute on", relativePath, Marshal.GetLastWin32Error());
        }
    }

    public void Remove(string absolutePath, string relativePath, string name)
    {
        XattrRules.ValidateName(name);
        int result = NativeMethods.lremovexattr(absolutePath, name);
        if (result != 0)
        {
            int errno = Marshal.GetLastWin32Error();
            if (errno == ENODATA || errno == ENOENT)
            {
                return;
            }

            throw NativeError("remove extended attribute from", relativePath, errno);
        }
    }

    private static byte[] ReadValue(string absolutePath, string relativePath, string name)
    {
        for (int attempt = 0; attempt < Limits.NativeAttempts; attempt++)
        {
            IntPtr sizePointer = NativeMethods.lgetxattr(absolutePath, name, null, UIntPtr.Zero);
            if (sizePointer.ToInt64() < 0)
            {
                int errno = Marshal.GetLastWin32Error();
                if (errno == ERANGE)
                {
                    continue;
                }

                throw NativeError("read extended attribute '" + name + "' from", relativePath, errno);
            }

            long size = sizePointer.ToInt64();
            if (size > Limits.MaxEntryXattrBytes)
            {
                throw new BackupException("extended attribute '" + name + "' on '" + relativePath + "' exceeds the 1 MiB limit");
            }

            byte[] buffer = new byte[size == 0 ? 1 : (int)size];
            IntPtr gotPointer = NativeMethods.lgetxattr(absolutePath, name, buffer, ToSize(size));
            if (gotPointer.ToInt64() < 0)
            {
                int errno = Marshal.GetLastWin32Error();
                if (errno == ERANGE)
                {
                    continue;
                }

                throw NativeError("read extended attribute '" + name + "' from", relativePath, errno);
            }

            long got = gotPointer.ToInt64();
            if (got != size)
            {
                continue;
            }

            if (got == 0)
            {
                return Array.Empty<byte>();
            }

            byte[] value = new byte[got];
            Array.Copy(buffer, value, got);
            return value;
        }

        throw new BackupException("cannot read extended attribute '" + name + "' from '" + relativePath + "': value kept changing");
    }

    private static UIntPtr ToSize(long length) => new UIntPtr((ulong)length);

    private static BackupException NativeError(string operation, string relativePath, int errno)
    {
        string message = "errno " + errno;
        try
        {
            IntPtr text = NativeMethods.strerror(errno);
            if (text != IntPtr.Zero)
            {
                message = Marshal.PtrToStringUTF8(text) ?? message;
            }
        }
        catch (Exception)
        {
            // Keep the numeric errno if strerror is unavailable.
        }

        return new BackupException("cannot " + operation + " '" + relativePath + "': errno " + errno + " (" + message + ")");
    }
}

internal static class NativeMethods
{
    [DllImport("libc", EntryPoint = "llistxattr", SetLastError = true)]
    internal static extern IntPtr llistxattr(
        [MarshalAs(UnmanagedType.LPUTF8Str)] string path,
        [Out] byte[]? list,
        UIntPtr size);

    [DllImport("libc", EntryPoint = "lgetxattr", SetLastError = true)]
    internal static extern IntPtr lgetxattr(
        [MarshalAs(UnmanagedType.LPUTF8Str)] string path,
        [MarshalAs(UnmanagedType.LPUTF8Str)] string name,
        [Out] byte[]? value,
        UIntPtr size);

    [DllImport("libc", EntryPoint = "lsetxattr", SetLastError = true)]
    internal static extern int lsetxattr(
        [MarshalAs(UnmanagedType.LPUTF8Str)] string path,
        [MarshalAs(UnmanagedType.LPUTF8Str)] string name,
        byte[] value,
        UIntPtr size,
        int flags);

    [DllImport("libc", EntryPoint = "lremovexattr", SetLastError = true)]
    internal static extern int lremovexattr(
        [MarshalAs(UnmanagedType.LPUTF8Str)] string path,
        [MarshalAs(UnmanagedType.LPUTF8Str)] string name);

    [DllImport("libc", EntryPoint = "chmod", SetLastError = true)]
    internal static extern int chmod(
        [MarshalAs(UnmanagedType.LPUTF8Str)] string path,
        uint mode);

    [DllImport("libc", EntryPoint = "strerror", SetLastError = false)]
    internal static extern IntPtr strerror(int errnum);
}

internal sealed class DumpResult
{
    internal DumpResult(int entries, int xattrs, int manifestBytes)
    {
        Entries = entries;
        Xattrs = xattrs;
        ManifestBytes = manifestBytes;
    }

    internal int Entries { get; }

    internal int Xattrs { get; }

    internal int ManifestBytes { get; }
}

internal sealed class RestoreResult
{
    internal RestoreResult(int entries, int xattrsSet, int xattrsRemoved, int xattrsUnchanged)
    {
        Entries = entries;
        XattrsSet = xattrsSet;
        XattrsRemoved = xattrsRemoved;
        XattrsUnchanged = xattrsUnchanged;
    }

    internal int Entries { get; }

    internal int XattrsSet { get; }

    internal int XattrsRemoved { get; }

    internal int XattrsUnchanged { get; }
}

internal static class Dumper
{
    internal static DumpResult Dump(string rootArgument, string manifestArgument, IXattrStore store)
    {
        RootInfo root = PathRules.ValidateRoot(rootArgument);
        string manifestFullPath = PathRules.ValidateManifestOutput(root, manifestArgument);
        List<TreeEntry> tree = TreeScanner.Scan(root.FullPath);

        List<ManifestEntry> entries = new List<ManifestEntry>(tree.Count);
        long xattrCount = 0;
        foreach (TreeEntry item in tree)
        {
            Dictionary<string, byte[]> attrs = store.Read(item.AbsolutePath, item.RelativePath);
            XattrLimits.Validate(attrs, item.RelativePath);
            xattrCount += attrs.Count;
            entries.Add(new ManifestEntry
            {
                Path = item.RelativePath,
                Kind = item.Kind,
                Xattrs = new SortedDictionary<string, byte[]>(attrs, StringComparer.Ordinal),
                Sha256 = item.Sha256,
                LinkTarget = item.LinkTarget,
            });
        }

        Manifest manifest = new Manifest
        {
            Version = 1,
            RootBasename = root.Basename,
            Entries = entries,
        };

        byte[] bytes = ManifestCodec.Serialize(manifest);
        AtomicFile.Write(manifestFullPath, bytes, protectOnUnix: OperatingSystem.IsLinux());
        return new DumpResult(entries.Count, (int)xattrCount, bytes.Length);
    }
}

internal sealed class EntryPlan
{
    internal EntryPlan(string absolutePath, string relativePath)
    {
        AbsolutePath = absolutePath;
        RelativePath = relativePath;
    }

    internal string AbsolutePath { get; }

    internal string RelativePath { get; }
}

internal static class Restorer
{
    internal static RestoreResult Restore(string rootArgument, string manifestArgument, IXattrStore store)
    {
        RootInfo root = PathRules.ValidateRoot(rootArgument);
        string manifestFullPath = PathRules.ValidateManifestInput(manifestArgument);
        Manifest manifest = ManifestCodec.ReadFile(manifestFullPath);

        if (!string.Equals(manifest.RootBasename, root.Basename, StringComparison.Ordinal))
        {
            throw new BackupException(
                "manifest root basename '" + manifest.RootBasename + "' does not match ROOT basename '" + root.Basename + "'");
        }

        List<TreeEntry> tree = TreeScanner.Scan(root.FullPath);
        Dictionary<string, TreeEntry> actual = new Dictionary<string, TreeEntry>(StringComparer.Ordinal);
        foreach (TreeEntry item in tree)
        {
            actual.Add(item.RelativePath, item);
        }

        if (actual.Count != manifest.Entries.Count)
        {
            throw new BackupException(
                "manifest and ROOT entries differ: manifest=" + manifest.Entries.Count + " root=" + actual.Count);
        }

        foreach (ManifestEntry expected in manifest.Entries)
        {
            if (!actual.TryGetValue(expected.Path, out TreeEntry? found))
            {
                throw new BackupException("ROOT is missing manifest entry '" + expected.Path + "'");
            }

            if (found.Kind != expected.Kind)
            {
                throw new BackupException(
                    "entry '" + expected.Path + "' kind mismatch: manifest=" + KindNames.ToJson(expected.Kind) +
                    " root=" + KindNames.ToJson(found.Kind));
            }

            if (expected.Kind == EntryKind.Symlink &&
                !string.Equals(found.LinkTarget, expected.LinkTarget, StringComparison.Ordinal))
            {
                throw new BackupException("entry '" + expected.Path + "' symlink target mismatch");
            }

            if (expected.Kind == EntryKind.File &&
                !string.Equals(found.Sha256, expected.Sha256, StringComparison.Ordinal))
            {
                throw new BackupException("entry '" + expected.Path + "' content hash mismatch");
            }
        }

        // Every attribute is read and validated before the first write. Kernel side effects
        // such as security.SMACK64PROCNAME are reconciled later, never assumed away here.
        List<EntryPlan> plans = new List<EntryPlan>(manifest.Entries.Count);
        foreach (ManifestEntry expected in manifest.Entries)
        {
            string absolutePath = TreeScanner.AbsoluteFor(root.FullPath, expected.Path);
            Dictionary<string, byte[]> current = store.Read(absolutePath, expected.Path);
            XattrLimits.Validate(current, expected.Path);
            plans.Add(new EntryPlan(absolutePath, expected.Path));
        }

        int setCount = 0;
        int removedCount = 0;
        HashSet<string> writtenExpected = new HashSet<string>(StringComparer.Ordinal);

        for (int index = 0; index < plans.Count; index++)
        {
            ApplyEntry(store, plans[index], manifest.Entries[index], writtenExpected, ref setCount, ref removedCount);
        }

        int failingIndex = -1;
        string? failure = FindMismatch(store, plans, manifest, out failingIndex);
        int passes = 0;
        while (failure != null && passes < Limits.MaxReconcilePasses)
        {
            passes++;
            for (int index = 0; index < plans.Count; index++)
            {
                ApplyEntry(store, plans[index], manifest.Entries[index], writtenExpected, ref setCount, ref removedCount);
            }

            failure = FindMismatch(store, plans, manifest, out failingIndex);
        }

        if (failure != null)
        {
            string path = failingIndex >= 0 ? manifest.Entries[failingIndex].Path : "<unknown>";
            throw new BackupException(
                "reconciliation did not converge after " + Limits.MaxReconcilePasses +
                " passes for '" + path + "': " + failure);
        }

        int expectedAttrCount = 0;
        foreach (ManifestEntry entry in manifest.Entries)
        {
            expectedAttrCount += entry.Xattrs.Count;
        }

        return new RestoreResult(
            manifest.Entries.Count,
            setCount,
            removedCount,
            expectedAttrCount - writtenExpected.Count);
    }

    private static void ApplyEntry(
        IXattrStore store,
        EntryPlan plan,
        ManifestEntry expected,
        HashSet<string> writtenExpected,
        ref int setCount,
        ref int removedCount)
    {
        Dictionary<string, byte[]> before = store.Read(plan.AbsolutePath, plan.RelativePath);
        foreach (KeyValuePair<string, byte[]> attr in expected.Xattrs)
        {
            if (before.TryGetValue(attr.Key, out byte[]? value) && value.AsSpan().SequenceEqual(attr.Value))
            {
                continue;
            }

            store.Set(plan.AbsolutePath, plan.RelativePath, attr.Key, attr.Value);
            writtenExpected.Add(plan.AbsolutePath + "\0" + attr.Key);
            setCount++;
        }

        // Only remove unexpected attributes after the expected ones are set, because a set can
        // cause the kernel to create an attribute (for example SMACK64PROCNAME) that did not
        // exist when the preflight read the entry.
        Dictionary<string, byte[]> afterSet = store.Read(plan.AbsolutePath, plan.RelativePath);
        List<string> extra = new List<string>();
        foreach (string name in afterSet.Keys)
        {
            if (!expected.Xattrs.ContainsKey(name))
            {
                extra.Add(name);
            }
        }

        extra.Sort(StringComparer.Ordinal);
        foreach (string name in extra)
        {
            store.Remove(plan.AbsolutePath, plan.RelativePath, name);
            removedCount++;
        }
    }

    private static string? FindMismatch(
        IXattrStore store,
        List<EntryPlan> plans,
        Manifest manifest,
        out int failingIndex)
    {
        for (int index = 0; index < plans.Count; index++)
        {
            ManifestEntry expected = manifest.Entries[index];
            Dictionary<string, byte[]> actual = store.Read(plans[index].AbsolutePath, plans[index].RelativePath);
            if (actual.Count != expected.Xattrs.Count)
            {
                failingIndex = index;
                return "xattr count differs";
            }

            foreach (KeyValuePair<string, byte[]> attr in expected.Xattrs)
            {
                if (!actual.TryGetValue(attr.Key, out byte[]? value) || !value.AsSpan().SequenceEqual(attr.Value))
                {
                    failingIndex = index;
                    return "xattr value differs";
                }
            }
        }

        failingIndex = -1;
        return null;
    }
}

internal static class AtomicFile
{
    internal static void Write(string fullPath, byte[] bytes, bool protectOnUnix)
    {
        string directory = Path.GetDirectoryName(fullPath)
            ?? throw new BackupException("MANIFEST has no parent directory");
        string temporaryPath = Path.Combine(
            directory,
            "." + Path.GetFileName(fullPath) + ".tmp-" + Guid.NewGuid().ToString("N"));

        try
        {
            FileStreamOptions options = new FileStreamOptions
            {
                Mode = FileMode.CreateNew,
                Access = FileAccess.Write,
                Share = FileShare.None,
            };

            using (FileStream stream = new FileStream(temporaryPath, options))
            {
                // The empty temporary manifest must be private before any attribute bytes are written.
                // This protects only the manifest file; restore never calls chmod/chown on the tree.
                if (protectOnUnix && NativeMethods.chmod(temporaryPath, 0x180) != 0)
                {
                    int errno = Marshal.GetLastWin32Error();
                    throw new BackupException("cannot restrict permissions on the temporary manifest: errno " + errno);
                }

                stream.Write(bytes, 0, bytes.Length);
                stream.Flush(flushToDisk: true);
            }

            File.Move(temporaryPath, fullPath, overwrite: true);
        }
        catch (Exception)
        {
            try
            {
                if (File.Exists(temporaryPath))
                {
                    File.Delete(temporaryPath);
                }
            }
            catch (Exception)
            {
                // Best effort cleanup; the original failure is more useful.
            }

            throw;
        }
    }
}

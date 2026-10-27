using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace QuietTVInspect;

// Framed output for --inventory-framed. Samsung's .NET runtime writes GC diagnostics to stdout,
// so the document is wrapped where a decoder can find and verify it exactly:
//   QUIETTV-FRAME/1 <length> <sha256>\n<payload>\nQUIETTV-FRAME-END/1\n
// <length> is the payload byte count (decimal, no leading zeros), <sha256> lowercase hex of the
// payload, and the payload is the compact one-line JSON document. tools/tv-inspect/frame.py is
// the matching strict decoder.
internal static class Frame
{
    internal const string Tag = "QUIETTV-FRAME/1";
    internal const string EndTag = "QUIETTV-FRAME-END/1";
    internal const int MaxPayloadBytes = 8 << 20;

    internal static byte[] Encode(byte[] payload)
    {
        if (payload.Length == 0 || payload.Length > MaxPayloadBytes)
        {
            throw new InvalidOperationException("frame payload size out of range");
        }

        foreach (byte b in payload)
        {
            if (b == (byte)'\n' || b == (byte)'\r')
            {
                throw new InvalidOperationException("frame payload must be one line");
            }
        }

        string hash = Convert.ToHexString(SHA256.HashData(payload)).ToLowerInvariant();
        byte[] header = Encoding.ASCII.GetBytes(
            Tag + " " + payload.Length.ToString(CultureInfo.InvariantCulture) + " " + hash + "\n");
        byte[] trailer = Encoding.ASCII.GetBytes("\n" + EndTag + "\n");
        byte[] frame = new byte[header.Length + payload.Length + trailer.Length];
        Buffer.BlockCopy(header, 0, frame, 0, header.Length);
        Buffer.BlockCopy(payload, 0, frame, header.Length, payload.Length);
        Buffer.BlockCopy(trailer, 0, frame, header.Length + payload.Length, trailer.Length);
        return frame;
    }

    // The rendered document ends in exactly one newline, which is not part of the payload. A
    // document over the cap is replaced by a framed output-oversize failure (ok = false).
    internal static byte[] ForOutcome(InspectionOutcome outcome, out bool ok)
    {
        byte[] json = outcome.Json;
        ok = json.Length >= 2 && json[json.Length - 1] == (byte)'\n' && json.Length - 1 <= MaxPayloadBytes;
        if (!ok)
        {
            json = Inspector.OutputOversize().Json;
        }

        try
        {
            return Encode(WithoutFinalNewline(json));
        }
        catch (InvalidOperationException)
        {
            // Not encodable as one line: fail closed with a fixed, framed failure document.
            ok = false;
            return Encode(WithoutFinalNewline(Inspector.InternalError().Json));
        }
    }

    private static byte[] WithoutFinalNewline(byte[] json)
    {
        byte[] payload = new byte[json.Length - 1];
        Buffer.BlockCopy(json, 0, payload, 0, payload.Length);
        return payload;
    }
}

// The only commands the inspector can ask for. The real adapter maps each one to a fixed
// executable and fixed arguments; no executable, argument, SQL or path ever comes from input.
internal enum FixedCommand
{
    AppQuery,
    LabelQuery,
    UnameMachine,
}

internal enum CommandStatus
{
    Ok,
    NotFound,
    StartFailed,
    TimedOut,
    OutputTooLarge,
    NonZeroExit,
}

internal sealed class CommandResult
{
    internal CommandResult(CommandStatus status, byte[] stdout, string stderrClass)
    {
        Status = status;
        Stdout = stdout;
        StderrClass = stderrClass;
    }

    internal CommandStatus Status { get; }

    internal byte[] Stdout { get; }

    // A fixed code from StderrClassifier, never the child's own text.
    internal string StderrClass { get; }

    internal static CommandResult Ok(byte[] stdout) => new CommandResult(CommandStatus.Ok, stdout, "none");

    internal static CommandResult Failed(CommandStatus status, string stderrClass = "none") =>
        new CommandResult(status, Array.Empty<byte>(), stderrClass);
}

internal enum RootKind
{
    Missing,
    Directory,
    Symlink,
    NotDirectory,
    Unreadable,
}

internal enum ReadStatus
{
    Ok,
    Missing,
    TooLarge,
    Unreadable,
}

internal sealed class FileReadResult
{
    internal FileReadResult(ReadStatus status, byte[] bytes)
    {
        Status = status;
        Bytes = bytes;
    }

    internal ReadStatus Status { get; }

    internal byte[] Bytes { get; }
}

// Everything the inspector may observe. There is no write, copy, delete or enumerate member:
// the inspector cannot change the TV through this seam, and the self-test fake records every call.
internal interface IInspectSystem
{
    CommandResult Run(FixedCommand command);

    FileReadResult ReadTizenRelease();

    // lstat-style probe of one lexically validated install root; never follows or enumerates it.
    RootKind ProbeRoot(string absoluteRoot);
}

internal static class InspectContract
{
    internal const string Schema = "quiettv-inspection/1";
    internal const string ToolVersion = "1.0.0";
    internal const string DatabasePath = "/opt/dbspace/.pkgmgr_parser.db";
    internal const string TizenReleasePath = "/etc/tizen-release";

    internal const int MaxStdoutBytes = 8 << 20;
    internal const int MaxStderrBytes = 4096;
    internal const int MaxUnameBytes = 64;
    internal const int MaxTizenReleaseBytes = 4096;
    internal const int MaxTizenReleaseLines = 32;
    internal const int MaxApps = 4096;
    internal const int MaxLabels = 65536;
    internal const int MaxLabelChars = 256;
    internal const int QueryTimeoutMs = 20000;
    internal const int UnameTimeoutMs = 5000;

    internal const int AppColumns = 10;
    internal const int LabelColumns = 3;

    // Every column is encoded as N (SQL NULL) or X followed by uppercase hex of its bytes, so
    // NULL and empty never collide and no value can inject a separator or a newline.
    private static string Enc(string column) =>
        "CASE WHEN " + column + " IS NULL THEN 'N' ELSE 'X'||hex(" + column + ") END";

    internal static readonly string AppQuery =
        "SELECT " + string.Join(", ", new[]
        {
            Enc("a.app_id"),
            Enc("a.app_component"),
            Enc("a.app_exec"),
            Enc("a.app_nodisplay"),
            Enc("a.app_disable"),
            Enc("a.app_root_path"),
            Enc("a.package"),
            Enc("p.root_path"),
            Enc("p.package_disable"),
            Enc("p.package_version"),
        }) +
        " FROM package_app_info a JOIN package_info p ON a.package = p.package ORDER BY a.app_id;";

    internal static readonly string LabelQuery =
        "SELECT " + string.Join(", ", new[]
        {
            Enc("app_id"),
            Enc("app_locale"),
            Enc("app_label"),
        }) +
        " FROM package_app_localized_info ORDER BY app_id, app_locale;";
}

// Preferred label locales. ASSUMPTION, not yet confirmed against this TV's database: Tizen
// stores US English as "en-us" (some tools write "en_US") and the default label under the
// literal "No Locale". Both spellings of each are accepted; anything else ranks after them
// in ordinal order of the locale string.
internal static class LocalePreference
{
    internal static readonly string[] English = { "en-us", "en_us" };
    internal static readonly string[] NoLocale = { "No Locale", "NoLocale" };

    internal static int Rank(string locale)
    {
        foreach (string candidate in English)
        {
            if (string.Equals(locale, candidate, StringComparison.OrdinalIgnoreCase))
            {
                return 0;
            }
        }

        foreach (string candidate in NoLocale)
        {
            if (string.Equals(locale, candidate, StringComparison.Ordinal))
            {
                return 1;
            }
        }

        return 2;
    }
}

internal static class Rules
{
    // Ported from public-toolkit/tvkit/paths.py (_IDENT plus the ".." refusal). Every pattern ends
    // in \z, not $, because .NET $ also matches before a final newline.
    private static readonly Regex Ident = new Regex(@"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\z", RegexOptions.CultureInvariant);
    private static readonly Regex Component = new Regex(@"^[a-z][a-z0-9_-]{0,31}\z", RegexOptions.CultureInvariant);
    private static readonly Regex Version = new Regex(@"^[0-9A-Za-z][0-9A-Za-z.+_~-]{0,63}\z", RegexOptions.CultureInvariant);
    private static readonly Regex Locale = new Regex(@"^[A-Za-z0-9][A-Za-z0-9 _-]{0,31}\z", RegexOptions.CultureInvariant);
    private static readonly Regex Machine = new Regex(@"^[A-Za-z0-9_]{1,32}\z", RegexOptions.CultureInvariant);
    private static readonly Regex TizenVersion = new Regex(@"^VERSION[ \t]*=[ \t]*([0-9A-Za-z._-]{1,32})[ \t]*\z", RegexOptions.CultureInvariant);

    internal static readonly string[] InstallParents = { "/opt/usr/apps/", "/usr/apps/" };

    internal static bool IsIdentifier(string? value) =>
        value is not null && Ident.IsMatch(value) && value.IndexOf("..", StringComparison.Ordinal) < 0;

    internal static bool IsComponent(string value) => Component.IsMatch(value);

    internal static bool IsVersion(string value) => Version.IsMatch(value);

    internal static bool IsLocale(string value) => Locale.IsMatch(value);

    internal static bool IsMachine(string value) => Machine.IsMatch(value);

    internal static string? MatchTizenVersion(string line)
    {
        Match match = TizenVersion.Match(line);
        return match.Success ? match.Groups[1].Value : null;
    }

    // Mirrors paths.check_absolute_posix: absolute, "/" separated, no empty, "." or ".."
    // components, no backslash, NUL or control characters, not "/" itself.
    internal static bool IsSafeAbsolute(string? path)
    {
        if (path is null || path.Length < 2 || path[0] != '/' || path.Length > 4096)
        {
            return false;
        }

        foreach (char ch in path)
        {
            if (ch < 0x20 || ch == 0x7f || ch == '\\')
            {
                return false;
            }
        }

        foreach (string part in path.Substring(1).Split('/'))
        {
            if (part.Length == 0 || part == "." || part == "..")
            {
                return false;
            }
        }

        return true;
    }

    // An install root counts only when it is exactly <parent><package>, no trailing slash.
    internal static bool IsStrictInstallRoot(string root, string package)
    {
        foreach (string parent in InstallParents)
        {
            if (string.Equals(root, parent + package, StringComparison.Ordinal))
            {
                return true;
            }
        }

        return false;
    }

    internal static bool IsSafeLabel(string? label)
    {
        if (label is null || label.Length == 0 || label.Length > InspectContract.MaxLabelChars)
        {
            return false;
        }

        if (!string.Equals(label.Trim(), label, StringComparison.Ordinal))
        {
            return false;
        }

        foreach (Rune rune in label.EnumerateRunes())
        {
            switch (Rune.GetUnicodeCategory(rune))
            {
                case UnicodeCategory.Control:
                case UnicodeCategory.Format:
                case UnicodeCategory.PrivateUse:
                case UnicodeCategory.Surrogate:
                case UnicodeCategory.LineSeparator:
                case UnicodeCategory.ParagraphSeparator:
                    return false;
            }

            if (rune.Value == 0xFFFD)
            {
                return false;
            }
        }

        return true;
    }
}

// Maps child stderr to a fixed code. The text itself is never emitted: it can carry device
// paths or database contents.
internal static class StderrClassifier
{
    internal static string Classify(byte[] stderr)
    {
        string text = Encoding.ASCII.GetString(stderr).ToLowerInvariant();
        if (text.Contains("no such table"))
        {
            return "table-unsupported";
        }

        if (text.Contains("no such column"))
        {
            return "column-unsupported";
        }

        if (text.Contains("unknown option"))
        {
            return "sqlite3-option-unsupported";
        }

        if (text.Contains("unable to open") || text.Contains("not a database") ||
            text.Contains("readonly") || text.Contains("permission") || text.Contains("locked"))
        {
            return "database-open-failed";
        }

        return "query-failed";
    }
}

internal static class BoundedCapture
{
    // Reads at most cap bytes. Returns overflow=true as soon as one more byte is available;
    // the caller then kills the child rather than draining an unbounded stream.
    internal static (byte[] Bytes, bool Overflow) Read(Stream stream, int cap)
    {
        using MemoryStream buffer = new MemoryStream();
        byte[] chunk = new byte[16384];
        while (true)
        {
            int read = stream.Read(chunk, 0, chunk.Length);
            if (read <= 0)
            {
                return (buffer.ToArray(), false);
            }

            if (buffer.Length + read > cap)
            {
                return (Array.Empty<byte>(), true);
            }

            buffer.Write(chunk, 0, read);
        }
    }
}

internal sealed class InspectionFailure : Exception
{
    internal InspectionFailure(string code, string subject) : base(code)
    {
        Code = code;
        Subject = subject;
    }

    internal string Code { get; }

    internal string Subject { get; }
}

internal sealed class Problem
{
    internal Problem(string code, string subject, bool fatal)
    {
        Code = code;
        Subject = subject;
        Fatal = fatal;
    }

    internal string Code { get; }

    internal string Subject { get; }

    internal bool Fatal { get; }
}

internal sealed class AppRow
{
    internal string AppId = string.Empty;
    internal string Component = string.Empty;
    internal string? Exec;
    internal bool NoDisplay;
    internal bool AppDisabled;
    internal string? AppRootPath;
    internal string Package = string.Empty;
    internal string? RootPath;
    internal bool PackageDisabled;
    internal string? Version;
}

internal sealed class RegistryRecord
{
    internal string Id = string.Empty;
    internal string? Name;
    internal bool Installed;
    internal bool Disabled;
    internal bool Launchable;
    internal string PackageId = string.Empty;
    internal string? Version;
    internal string? InstallRoot;
    internal string Reason = string.Empty;
}

internal sealed class InstallVerdict
{
    internal InstallVerdict(bool installed, string? installRoot, string reason)
    {
        Installed = installed;
        InstallRoot = installRoot;
        Reason = reason;
    }

    internal bool Installed { get; }

    internal string? InstallRoot { get; }

    internal string Reason { get; }
}

internal sealed class RuntimeEvidence
{
    internal List<string>? TizenReleaseLines;
    internal string? TizenVersion;
    internal string? Machine;
}

internal sealed class InspectionOutcome
{
    internal InspectionOutcome(bool ok, byte[] json)
    {
        Ok = ok;
        Json = json;
    }

    internal bool Ok { get; }

    internal byte[] Json { get; }
}

internal static class Inspector
{
    internal static InspectionOutcome Inspect(IInspectSystem system)
    {
        List<Problem> problems = new List<Problem>();
        RuntimeEvidence runtime = ObserveRuntime(system, problems);
        List<RegistryRecord>? records;
        try
        {
            records = ReadRegistry(system, problems);
        }
        catch (InspectionFailure failure)
        {
            problems.Add(new Problem(failure.Code, failure.Subject, fatal: true));
            records = null;
        }

        return Render(records, runtime, problems);
    }

    internal static InspectionOutcome PlatformRefusal()
    {
        List<Problem> problems = new List<Problem> { new Problem("platform-unsupported", "inventory", fatal: true) };
        return Render(null, null, problems);
    }

    internal static InspectionOutcome InternalError()
    {
        List<Problem> problems = new List<Problem> { new Problem("internal-error", "inventory", fatal: true) };
        return Render(null, null, problems);
    }

    internal static InspectionOutcome OutputOversize()
    {
        List<Problem> problems = new List<Problem> { new Problem("output-oversize", "inventory", fatal: true) };
        return Render(null, null, problems);
    }

    private static InspectionFailure Fatal(string code, string subject) => new InspectionFailure(code, subject);

    private static List<RegistryRecord> ReadRegistry(IInspectSystem system, List<Problem> problems)
    {
        byte[] appBytes = RunQuery(system, FixedCommand.AppQuery, "app-query");
        byte[] labelBytes = RunQuery(system, FixedCommand.LabelQuery, "label-query");

        List<string?[]> appRows = ParseRows(appBytes, InspectContract.AppColumns, InspectContract.MaxApps, "app-query");
        List<string?[]> labelRows = ParseRows(labelBytes, InspectContract.LabelColumns, InspectContract.MaxLabels, "label-query");
        if (appRows.Count == 0)
        {
            throw Fatal("registry-empty", "app-query");
        }

        Dictionary<string, AppRow> byId = new Dictionary<string, AppRow>(StringComparer.Ordinal);
        Dictionary<string, string> idFold = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        Dictionary<string, string> packageFold = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        foreach (string?[] fields in appRows)
        {
            AppRow row = ToAppRow(fields);
            string appId = row.AppId;
            if (!byId.TryAdd(appId, row))
            {
                throw Fatal("duplicate-app", appId);
            }

            if (idFold.TryGetValue(appId, out string? otherId) && !string.Equals(otherId, appId, StringComparison.Ordinal))
            {
                throw Fatal("case-clash-app", appId);
            }

            idFold[appId] = appId;
            if (packageFold.TryGetValue(row.Package, out string? otherPackage) &&
                !string.Equals(otherPackage, row.Package, StringComparison.Ordinal))
            {
                throw Fatal("case-clash-package", row.Package);
            }

            packageFold[row.Package] = row.Package;
        }

        Dictionary<string, List<(string Locale, string? Label)>> labels = ReadLabels(labelRows);

        List<string> ids = new List<string>(byId.Keys);
        ids.Sort(StringComparer.Ordinal);
        Dictionary<string, RootKind> probed = new Dictionary<string, RootKind>(StringComparer.Ordinal);
        List<RegistryRecord> records = new List<RegistryRecord>(ids.Count);
        foreach (string id in ids)
        {
            AppRow row = byId[id];
            InstallVerdict verdict = DecideInstall(system, row, probed);

            bool execPresent = !string.IsNullOrEmpty(row.Exec);
            bool execSafe = execPresent && Rules.IsSafeAbsolute(row.Exec);
            if (execPresent && !execSafe)
            {
                problems.Add(new Problem("exec-unsafe", id, fatal: false));
            }

            string? version = row.Version;
            if (version is not null && !Rules.IsVersion(version))
            {
                problems.Add(new Problem("version-unsafe", id, fatal: false));
                version = null;
            }
            else if (version is null)
            {
                problems.Add(new Problem("version-missing", id, fatal: false));
            }

            records.Add(new RegistryRecord
            {
                Id = id,
                Name = ChooseLabel(id, labels, problems),
                Installed = verdict.Installed,
                Disabled = row.AppDisabled || row.PackageDisabled,
                // Exactly the contract: uiapp, nodisplay false, non-empty exec. The exec is never run
                // or printed, so an unsafe exec is reported as a problem, not folded in here.
                Launchable = string.Equals(row.Component, "uiapp", StringComparison.Ordinal) && !row.NoDisplay && execPresent,
                PackageId = row.Package,
                Version = version,
                InstallRoot = verdict.InstallRoot,
                Reason = verdict.Reason,
            });
        }

        return records;
    }

    private static byte[] RunQuery(IInspectSystem system, FixedCommand command, string subject)
    {
        CommandResult result = system.Run(command);
        switch (result.Status)
        {
            case CommandStatus.Ok:
                return result.Stdout;
            case CommandStatus.NotFound:
                throw Fatal("sqlite3-unavailable", subject);
            case CommandStatus.StartFailed:
                throw Fatal("query-start-failed", subject);
            case CommandStatus.TimedOut:
                throw Fatal("query-timeout", subject);
            case CommandStatus.OutputTooLarge:
                throw Fatal("query-output-oversize", subject);
            case CommandStatus.NonZeroExit:
                throw Fatal(result.StderrClass == "none" ? "query-failed" : result.StderrClass, subject);
            default:
                throw Fatal("query-failed", subject);
        }
    }

    // Output must be lines of exactly `columns` fields separated by '|', each "N" or "X" plus
    // uppercase hex, every line ending in '\n'. Anything else is malformed and fatal.
    internal static List<string?[]> ParseRows(byte[] stdout, int columns, int maxRows, string what)
    {
        List<string?[]> rows = new List<string?[]>();
        if (stdout.Length == 0)
        {
            return rows;
        }

        if (stdout[stdout.Length - 1] != (byte)'\n')
        {
            throw Fatal("malformed-output", what);
        }

        foreach (byte b in stdout)
        {
            bool allowed = (b >= (byte)'0' && b <= (byte)'9') || (b >= (byte)'A' && b <= (byte)'F') ||
                           b == (byte)'N' || b == (byte)'X' || b == (byte)'|' || b == (byte)'\n';
            if (!allowed)
            {
                throw Fatal("malformed-output", what);
            }
        }

        string text = Encoding.ASCII.GetString(stdout, 0, stdout.Length - 1);
        foreach (string line in text.Split('\n'))
        {
            if (rows.Count >= maxRows)
            {
                throw Fatal("row-count-oversize", what);
            }

            string[] parts = line.Split('|');
            if (parts.Length != columns)
            {
                throw Fatal("malformed-output", what);
            }

            string?[] fields = new string?[columns];
            for (int i = 0; i < columns; i++)
            {
                fields[i] = DecodeField(parts[i], what);
            }

            rows.Add(fields);
        }

        return rows;
    }

    private static string? DecodeField(string field, string what)
    {
        if (field == "N")
        {
            return null;
        }

        if (field.Length == 0 || field[0] != 'X' || (field.Length - 1) % 2 != 0)
        {
            throw Fatal("malformed-output", what);
        }

        byte[] bytes = new byte[(field.Length - 1) / 2];
        for (int i = 0; i < bytes.Length; i++)
        {
            int hi = HexValue(field[1 + 2 * i]);
            int lo = HexValue(field[2 + 2 * i]);
            if (hi < 0 || lo < 0)
            {
                throw Fatal("malformed-output", what);
            }

            bytes[i] = (byte)((hi << 4) | lo);
        }

        try
        {
            return new UTF8Encoding(false, throwOnInvalidBytes: true).GetString(bytes);
        }
        catch (DecoderFallbackException)
        {
            throw Fatal("malformed-output", what);
        }
    }

    private static int HexValue(char ch)
    {
        if (ch >= '0' && ch <= '9')
        {
            return ch - '0';
        }

        if (ch >= 'A' && ch <= 'F')
        {
            return ch - 'A' + 10;
        }

        return -1;
    }

    private static AppRow ToAppRow(string?[] f)
    {
        string? appId = f[0];
        string? package = f[6];
        if (!Rules.IsIdentifier(appId))
        {
            throw Fatal("unexpected-row-key", "app_id");
        }

        if (!Rules.IsIdentifier(package))
        {
            throw Fatal("unexpected-row-key", appId!);
        }

        string? component = f[1];
        if (component is null || !Rules.IsComponent(component))
        {
            throw Fatal("malformed-field", appId!);
        }

        return new AppRow
        {
            AppId = appId!,
            Component = component,
            Exec = f[2],
            NoDisplay = ParseFlag(f[3], appId!),
            AppDisabled = ParseFlag(f[4], appId!),
            AppRootPath = f[5],
            Package = package!,
            RootPath = f[7],
            PackageDisabled = ParseFlag(f[8], appId!),
            Version = f[9],
        };
    }

    // Flags are the exact strings "true" or "false". NULL, case variants or numbers are fatal.
    private static bool ParseFlag(string? value, string appId)
    {
        if (string.Equals(value, "true", StringComparison.Ordinal))
        {
            return true;
        }

        if (string.Equals(value, "false", StringComparison.Ordinal))
        {
            return false;
        }

        throw Fatal("malformed-flag", appId);
    }

    private static Dictionary<string, List<(string Locale, string? Label)>> ReadLabels(List<string?[]> rows)
    {
        Dictionary<string, List<(string Locale, string? Label)>> labels =
            new Dictionary<string, List<(string Locale, string? Label)>>(StringComparer.Ordinal);
        foreach (string?[] f in rows)
        {
            string? appId = f[0];
            string? locale = f[1];
            if (!Rules.IsIdentifier(appId))
            {
                throw Fatal("unexpected-row-key", "label app_id");
            }

            if (locale is null || !Rules.IsLocale(locale))
            {
                throw Fatal("malformed-label-row", appId!);
            }

            if (!labels.TryGetValue(appId!, out List<(string Locale, string? Label)>? list))
            {
                list = new List<(string Locale, string? Label)>();
                labels.Add(appId!, list);
            }

            foreach ((string existing, string? _) in list)
            {
                if (string.Equals(existing, locale, StringComparison.Ordinal))
                {
                    throw Fatal("duplicate-label", appId!);
                }

                if (string.Equals(existing, locale, StringComparison.OrdinalIgnoreCase))
                {
                    throw Fatal("case-clash-label", appId!);
                }
            }

            list.Add((locale, f[2]));
        }

        return labels;
    }

    // Best-ranked locale wins. If that label is missing or unsafe the name is held (null) with a
    // problem; there is no fall back to another locale's text.
    private static string? ChooseLabel(
        string id,
        Dictionary<string, List<(string Locale, string? Label)>> labels,
        List<Problem> problems)
    {
        if (!labels.TryGetValue(id, out List<(string Locale, string? Label)>? list) || list.Count == 0)
        {
            problems.Add(new Problem("label-missing", id, fatal: false));
            return null;
        }

        (string Locale, string? Label) best = list[0];
        foreach ((string Locale, string? Label) candidate in list)
        {
            int rank = LocalePreference.Rank(candidate.Locale);
            int bestRank = LocalePreference.Rank(best.Locale);
            if (rank < bestRank || (rank == bestRank && string.CompareOrdinal(candidate.Locale, best.Locale) < 0))
            {
                best = candidate;
            }
        }

        if (!Rules.IsSafeLabel(best.Label))
        {
            problems.Add(new Problem("label-unsafe", id, fatal: false));
            return null;
        }

        return best.Label;
    }

    private static InstallVerdict DecideInstall(IInspectSystem system, AppRow row, Dictionary<string, RootKind> probed)
    {
        string? root = row.RootPath;
        if (string.IsNullOrEmpty(root))
        {
            return new InstallVerdict(false, null, "registry-root-missing");
        }

        if (!Rules.IsSafeAbsolute(root))
        {
            return new InstallVerdict(false, null, "root-path-unsafe");
        }

        // External roots (for example /usr/bin/wrt or an aria-engine directory) are read-only
        // shared references, not this package's install: never probed, hashed or counted.
        if (!Rules.IsStrictInstallRoot(root, row.Package))
        {
            return new InstallVerdict(false, null, "external-root-refused");
        }

        if (row.AppRootPath is not null && !string.Equals(row.AppRootPath, root, StringComparison.Ordinal))
        {
            return new InstallVerdict(false, root, "app-root-mismatch");
        }

        if (!probed.TryGetValue(root, out RootKind kind))
        {
            kind = system.ProbeRoot(root);
            probed.Add(root, kind);
        }

        switch (kind)
        {
            case RootKind.Directory:
                return new InstallVerdict(true, root, "installed");
            case RootKind.Missing:
                return new InstallVerdict(false, root, "root-absent-tombstone");
            case RootKind.Symlink:
                return new InstallVerdict(false, root, "symlink-root-refused");
            case RootKind.NotDirectory:
                return new InstallVerdict(false, root, "root-not-directory");
            default:
                return new InstallVerdict(false, root, "root-unreadable");
        }
    }

    private static RuntimeEvidence ObserveRuntime(IInspectSystem system, List<Problem> problems)
    {
        RuntimeEvidence evidence = new RuntimeEvidence();
        FileReadResult release = system.ReadTizenRelease();
        switch (release.Status)
        {
            case ReadStatus.Ok:
                evidence.TizenReleaseLines = ParseTizenRelease(release.Bytes, problems);
                break;
            case ReadStatus.Missing:
                problems.Add(new Problem("tizen-release-missing", InspectContract.TizenReleasePath, fatal: false));
                break;
            case ReadStatus.TooLarge:
                problems.Add(new Problem("tizen-release-oversize", InspectContract.TizenReleasePath, fatal: false));
                break;
            default:
                problems.Add(new Problem("tizen-release-unreadable", InspectContract.TizenReleasePath, fatal: false));
                break;
        }

        if (evidence.TizenReleaseLines is not null)
        {
            string? version = null;
            int matches = 0;
            foreach (string line in evidence.TizenReleaseLines)
            {
                string? found = Rules.MatchTizenVersion(line);
                if (found is not null)
                {
                    version = found;
                    matches++;
                }
            }

            evidence.TizenVersion = matches == 1 ? version : null;
        }

        if (evidence.TizenVersion is null)
        {
            problems.Add(new Problem("tizen-version-not-observed", InspectContract.TizenReleasePath, fatal: false));
        }

        CommandResult uname = system.Run(FixedCommand.UnameMachine);
        if (uname.Status == CommandStatus.Ok)
        {
            string text = Encoding.ASCII.GetString(uname.Stdout);
            if (text.EndsWith("\n", StringComparison.Ordinal))
            {
                text = text.Substring(0, text.Length - 1);
            }

            bool ascii = true;
            foreach (byte b in uname.Stdout)
            {
                ascii &= b < 0x80;
            }

            if (ascii && Rules.IsMachine(text))
            {
                evidence.Machine = text;
            }
            else
            {
                problems.Add(new Problem("machine-unsafe", "uname -m", fatal: false));
            }
        }
        else if (uname.Status == CommandStatus.NotFound)
        {
            problems.Add(new Problem("uname-unavailable", "uname -m", fatal: false));
        }
        else
        {
            problems.Add(new Problem("machine-not-observed", "uname -m", fatal: false));
        }

        // This tool never derives model, firmware or region (for example from a SKU string).
        problems.Add(new Problem("model-not-observed", "observedRuntime", fatal: false));
        problems.Add(new Problem("firmware-not-observed", "observedRuntime", fatal: false));
        problems.Add(new Problem("region-not-observed", "observedRuntime", fatal: false));
        return evidence;
    }

    private static List<string>? ParseTizenRelease(byte[] bytes, List<Problem> problems)
    {
        List<string> lines = new List<string>();
        foreach (byte b in bytes)
        {
            if (b != (byte)'\n' && b != (byte)'\r' && b != (byte)'\t' && (b < 0x20 || b > 0x7e))
            {
                problems.Add(new Problem("tizen-release-unsafe", InspectContract.TizenReleasePath, fatal: false));
                return null;
            }
        }

        foreach (string raw in Encoding.ASCII.GetString(bytes).Split('\n'))
        {
            string line = raw.TrimEnd('\r').Replace('\t', ' ').Trim();
            if (line.Length == 0)
            {
                continue;
            }

            if (line.IndexOf('\r') >= 0 || line.Length > 200 || lines.Count >= InspectContract.MaxTizenReleaseLines)
            {
                problems.Add(new Problem("tizen-release-unsafe", InspectContract.TizenReleasePath, fatal: false));
                return null;
            }

            lines.Add(line);
        }

        return lines;
    }

    private static InspectionOutcome Render(List<RegistryRecord>? records, RuntimeEvidence? runtime, List<Problem> problems)
    {
        bool ok = records is not null;
        foreach (Problem problem in problems)
        {
            ok &= !problem.Fatal;
        }

        List<Problem> sorted = new List<Problem>(problems);
        sorted.Sort((a, b) =>
        {
            int c = b.Fatal.CompareTo(a.Fatal);
            if (c == 0)
            {
                c = string.CompareOrdinal(a.Code, b.Code);
            }

            return c != 0 ? c : string.CompareOrdinal(a.Subject, b.Subject);
        });

        using MemoryStream stream = new MemoryStream();
        // Compact output: an indented writer would use the platform newline and differ by OS.
        using (Utf8JsonWriter w = new Utf8JsonWriter(stream, new JsonWriterOptions { Indented = false }))
        {
            w.WriteStartObject();
            w.WriteString("schema", InspectContract.Schema);
            w.WriteString("toolVersion", InspectContract.ToolVersion);
            w.WriteString("status", ok ? "ok" : "failed");
            w.WriteString("registryDatabase", InspectContract.DatabasePath);
            if (ok && records is not null)
            {
                w.WriteStartArray("registryRecords");
                foreach (RegistryRecord r in records)
                {
                    w.WriteStartObject();
                    w.WriteString("Id", r.Id);
                    WriteNullable(w, "Name", r.Name);
                    w.WriteBoolean("Installed", r.Installed);
                    w.WriteBoolean("Disabled", r.Disabled);
                    w.WriteBoolean("Launchable", r.Launchable);
                    w.WriteString("PackageId", r.PackageId);
                    WriteNullable(w, "Version", r.Version);
                    WriteNullable(w, "InstallRoot", r.InstallRoot);
                    w.WriteString("Reason", r.Reason);
                    w.WriteEndObject();
                }

                w.WriteEndArray();
            }
            else
            {
                w.WriteNull("registryRecords");
            }

            if (runtime is null)
            {
                w.WriteNull("observedRuntime");
            }
            else
            {
                w.WriteStartObject("observedRuntime");
                if (runtime.TizenReleaseLines is null)
                {
                    w.WriteNull("tizenRelease");
                }
                else
                {
                    w.WriteStartObject("tizenRelease");
                    w.WriteString("path", InspectContract.TizenReleasePath);
                    w.WriteStartArray("lines");
                    foreach (string line in runtime.TizenReleaseLines)
                    {
                        w.WriteStringValue(line);
                    }

                    w.WriteEndArray();
                    w.WriteEndObject();
                }

                WriteNullable(w, "tizenVersion", runtime.TizenVersion);
                WriteNullable(w, "machine", runtime.Machine);
                w.WriteNull("model");
                w.WriteNull("firmware");
                w.WriteNull("region");
                w.WriteEndObject();
            }

            w.WriteStartArray("problems");
            foreach (Problem p in sorted)
            {
                w.WriteStartObject();
                w.WriteString("code", p.Code);
                w.WriteString("subject", p.Subject);
                w.WriteBoolean("fatal", p.Fatal);
                w.WriteEndObject();
            }

            w.WriteEndArray();
            w.WriteEndObject();
        }

        stream.WriteByte((byte)'\n');
        return new InspectionOutcome(ok, stream.ToArray());
    }

    private static void WriteNullable(Utf8JsonWriter w, string name, string? value)
    {
        if (value is null)
        {
            w.WriteNull(name);
        }
        else
        {
            w.WriteString(name, value);
        }
    }
}

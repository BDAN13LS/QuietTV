using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Threading.Tasks;

namespace QuietTVInspect;

internal static class Program
{
    internal static int Main(string[] args)
    {
        if (args.Length == 1 && args[0] == "--self-test")
        {
            return SelfTests.Run();
        }

        // Raw mode prints the bare document. On Samsung's runtime the GC logs share stdout, so raw
        // stdout is not reliably one JSON document there; the framed mode is the one to decode.
        bool raw = args.Length == 1 && args[0] == "--inventory";
        bool framed = args.Length == 1 && args[0] == "--inventory-framed";
        if (raw || framed)
        {
            InspectionOutcome outcome;
            int exitCode;
            try
            {
                if (!OperatingSystem.IsLinux())
                {
                    outcome = Inspector.PlatformRefusal();
                    exitCode = 2;
                }
                else
                {
                    outcome = Inspector.Inspect(new LinuxInspectSystem());
                    exitCode = outcome.Ok ? 0 : 1;
                }
            }
            catch (Exception)
            {
                // The exception text can carry device paths; only a fixed code is reported.
                outcome = Inspector.InternalError();
                exitCode = 1;
            }

            byte[] bytes = outcome.Json;
            if (framed)
            {
                // Built as one buffer and written once, to keep runtime logs out of the frame.
                bytes = Frame.ForOutcome(outcome, out bool frameOk);
                if (!frameOk)
                {
                    exitCode = 1;
                }
            }

            using Stream stdout = Console.OpenStandardOutput();
            stdout.Write(bytes, 0, bytes.Length);
            stdout.Flush();
            return exitCode;
        }

        Console.Error.WriteLine("usage: QuietTVInspect --inventory | --inventory-framed | --self-test");
        return 2;
    }
}

// The real, read-only device adapter. Executables and arguments are compile-time constants
// selected by FixedCommand; nothing here accepts a path, command or SQL from a caller.
internal sealed class LinuxInspectSystem : IInspectSystem
{
    private const string Sqlite3 = "/usr/bin/sqlite3";
    private static readonly string[] UnameCandidates = { "/usr/bin/uname", "/bin/uname" };

    public CommandResult Run(FixedCommand command)
    {
        switch (command)
        {
            case FixedCommand.AppQuery:
                return RunSqlite(InspectContract.AppQuery);
            case FixedCommand.LabelQuery:
                return RunSqlite(InspectContract.LabelQuery);
            case FixedCommand.UnameMachine:
                foreach (string candidate in UnameCandidates)
                {
                    if (File.Exists(candidate))
                    {
                        return RunFixed(candidate, new[] { "-m" }, InspectContract.UnameTimeoutMs, InspectContract.MaxUnameBytes);
                    }
                }

                return CommandResult.Failed(CommandStatus.NotFound);
            default:
                return CommandResult.Failed(CommandStatus.StartFailed);
        }
    }

    public FileReadResult ReadTizenRelease()
    {
        try
        {
            if (!File.Exists(InspectContract.TizenReleasePath))
            {
                return new FileReadResult(ReadStatus.Missing, Array.Empty<byte>());
            }

            using FileStream stream = new FileStream(
                InspectContract.TizenReleasePath, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
            (byte[] bytes, bool overflow) = BoundedCapture.Read(stream, InspectContract.MaxTizenReleaseBytes);
            return overflow
                ? new FileReadResult(ReadStatus.TooLarge, Array.Empty<byte>())
                : new FileReadResult(ReadStatus.Ok, bytes);
        }
        catch (Exception ex) when (ex is IOException || ex is UnauthorizedAccessException)
        {
            return new FileReadResult(ReadStatus.Unreadable, Array.Empty<byte>());
        }
    }

    public RootKind ProbeRoot(string absoluteRoot)
    {
        try
        {
            // LinkTarget is read with lstat/readlink: a link is refused before anything follows it.
            if (new FileInfo(absoluteRoot).LinkTarget is not null)
            {
                return RootKind.Symlink;
            }

            if (Directory.Exists(absoluteRoot))
            {
                return RootKind.Directory;
            }

            return File.Exists(absoluteRoot) ? RootKind.NotDirectory : RootKind.Missing;
        }
        catch (Exception ex) when (ex is IOException || ex is UnauthorizedAccessException)
        {
            return RootKind.Unreadable;
        }
    }

    private static CommandResult RunSqlite(string sql)
    {
        if (!File.Exists(Sqlite3))
        {
            return CommandResult.Failed(CommandStatus.NotFound);
        }

        // -init /dev/null stops ~/.sqliterc running; -readonly opens the database read-only.
        string[] args =
        {
            "-init", "/dev/null", "-readonly", "-batch", "-bail", "-noheader", "-list",
            "-separator", "|", InspectContract.DatabasePath, sql,
        };
        return RunFixed(Sqlite3, args, InspectContract.QueryTimeoutMs, InspectContract.MaxStdoutBytes);
    }

    private static CommandResult RunFixed(string executable, IReadOnlyList<string> args, int timeoutMs, int maxStdout)
    {
        ProcessStartInfo info = new ProcessStartInfo(executable)
        {
            UseShellExecute = false,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            CreateNoWindow = true,
            WorkingDirectory = "/",
        };
        foreach (string arg in args)
        {
            info.ArgumentList.Add(arg);
        }

        info.Environment.Clear();
        info.Environment["PATH"] = "/usr/bin:/bin";
        info.Environment["LANG"] = "C";
        info.Environment["LC_ALL"] = "C";

        Process? process;
        try
        {
            process = Process.Start(info);
        }
        catch (Exception ex) when (ex is Win32Exception || ex is InvalidOperationException || ex is IOException)
        {
            return CommandResult.Failed(CommandStatus.StartFailed);
        }

        if (process is null)
        {
            return CommandResult.Failed(CommandStatus.StartFailed);
        }

        using (process)
        {
            process.StandardInput.Close();
            Task<(byte[] Bytes, bool Overflow)> stdout =
                Task.Run(() => BoundedCapture.Read(process.StandardOutput.BaseStream, maxStdout));
            Task<(byte[] Bytes, bool Overflow)> stderr =
                Task.Run(() => BoundedCapture.Read(process.StandardError.BaseStream, InspectContract.MaxStderrBytes));

            Stopwatch clock = Stopwatch.StartNew();
            bool exited = false;
            while (clock.ElapsedMilliseconds < timeoutMs)
            {
                if (process.WaitForExit(50))
                {
                    exited = true;
                    break;
                }

                if (stdout.IsCompleted && stdout.Result.Overflow)
                {
                    KillOwnChild(process);
                    return CommandResult.Failed(CommandStatus.OutputTooLarge);
                }
            }

            if (!exited)
            {
                KillOwnChild(process);
                return CommandResult.Failed(CommandStatus.TimedOut);
            }

            if (!Task.WaitAll(new Task[] { stdout, stderr }, 2000))
            {
                // A grandchild kept the pipes open after the child exited.
                KillOwnChild(process);
                return CommandResult.Failed(CommandStatus.TimedOut);
            }

            if (stdout.Result.Overflow)
            {
                return CommandResult.Failed(CommandStatus.OutputTooLarge);
            }

            if (process.ExitCode != 0)
            {
                string code = stderr.Result.Overflow ? "query-failed" : StderrClassifier.Classify(stderr.Result.Bytes);
                return CommandResult.Failed(CommandStatus.NonZeroExit, code);
            }

            return CommandResult.Ok(stdout.Result.Bytes);
        }
    }

    // Kills only the process this adapter started, and its descendants.
    private static void KillOwnChild(Process process)
    {
        try
        {
            process.Kill(entireProcessTree: true);
        }
        catch (InvalidOperationException)
        {
            // Already exited.
        }
        catch (Win32Exception)
        {
            // Could not signal; the wait below still reaps what it can.
        }

        try
        {
            process.WaitForExit(2000);
        }
        catch (InvalidOperationException)
        {
            // Nothing left to reap.
        }
    }
}

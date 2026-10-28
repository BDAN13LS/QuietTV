# Ecore-runner probe (on-TV validation for async app stop/launch)

This probe is the **primary evidence** that the Ecore-native stop/launch runner
(`launcher/host/CommandLifecycle.cs` â†’ `EcoreCommandRunner`) actually fixes the
`SIGABRT` crash. That crash is a race between .NET's `ProcessWaitState` and
ecore's own `SIGCHLD` reaping under an active ecore main loop, and it reproduces
**only on the real ARMv7 Tizen 9 .NET 6 TV** â€” never on Windows. So this must be
built on the PC and run on the TV by the parent during an authorized device test.

It compiles the **shipped** runner source (not a copy) and drives it against
fixed, harmless children only. It opens no window, touches no root, never calls
`app_launcher`, launches no target apps, uses no network, and reads no owner
files. It takes no arbitrary command input.

## What it checks

- ecore_exe event exports (`ECORE_EXE_EVENT_DEL/DATA/ERROR`) are present.
- **Direct exec**: `/bin/sleep` spawned via the shipped command builder â€” the
  `ecore_exe` handle's pid *is* the sleep CLI (comm `(sleep)`), proving there is
  no `/bin/sh` wrapper, so a kill hits the CLI process and not a shell.
- **30 `/bin/echo` children** each return exit 0 with their output (no false
  timeout, no `FailFast`).
- **A hung child** (`/bin/sleep 60`) is killed at the ~2500 ms deadline, and a
  50 ms liveness timer keeps advancing throughout â€” the loop never froze.
- **A finite output flood**: the probe re-launches its OWN assembly in `flood-child`
  mode (device `dotnet` + the probe dll + the literal `flood-child`, all safe args).
  That child writes 128 KiB stdout + 32 KiB stderr then exits 0, so the check is that
  the stored output is capped at exactly 64 KiB / 16 KiB AND the child exited cleanly
  (exit 0, not a timeout) â€” caps proven on a child that ends on its own, not by a kill.
- **Coexist**: a live `.NET Process` (`/bin/sleep 120`) is started first to install
  .NET's `SIGCHLD` handler exactly as IptvSession/WebFrontend do in the real app;
  three more echo children must still receive their ecore DEL. (This does *not*
  test ecore stealing the .NET child's own exit â€” see the lane report.)
- **No per-command file-descriptor leak** (`FdSteady`). The earlier whole-run count
  check failed (+5: 44 → 49) only because it compared a count taken *before* the first
  runner use and the `.NET` coexist child against one taken *after* every case while
  that coexist child was still **alive** — so it charged one-time .NET/`Process` init
  and an active child's own handles as a "leak". It is replaced by a regression-sensitive,
  **warmup-based** steady-state verdict: from a warm baseline (after the 30 echoes, before
  the `.NET` child), run batches of **10 / 30 / 60** more echoes and snapshot `/proc/self/fd`
  between them. A real per-command leak grows with the batch size; a warm steady state does
  not. The tolerance stays **+2** (not loosened). The verdict **fails closed**: a missing baseline/batch
  sample, fewer than the four required ordered stages, or a negative (unreadable) count is `Unavailable`,
  which is a FAILED check here — never a silent pass. A separate check asserts the ecore handle-tracking
  maps (`byHandle`/`byId`/`orphans`) are `(0,0,0)` at every between-command snapshot, so a managed
  tracking-map leak that the native count could miss also fails. The coexist `.NET` child is cleaned up
  honestly only after the loop stops pumping (identity-guarded exact-pid kill → bounded `WaitForExit` →
  `Dispose`), and the `coexist-dead` snapshot is taken only after that, so it is a true reclaim point; a
  child that could not be reaped is recorded and fails, not assumed clean. Bounded per-stage evidence is printed —
  `FD-STAGE …` (own `/proc/self/fd` only, categorised: pipe/socket/anon/dev/file, plus the
  live ecore handle-map sizes), `FD-ADDED …` (the actual link targets added at the key
  transitions), and `FD-WHOLE-RUN …` (the raw before / coexist-alive / coexist-dead counts,
  so the +N and its reclaim on cleanup are visible). Evidence reads descriptors only — never
  env, cmdline or another process. If `/proc/self/fd` is unreadable the stage is unmeasurable
  and makes no leak claim either way.

It prints one line per check and ends with `ECORE-PROBE: PASS|FAIL (n/total)`,
exiting non-zero on any failure. A 60 s watchdog fails the probe if the loop or a
child ever hangs the probe itself.

## Long-lived helper mode (`helper`)

The same crash class also lived in the **long-lived** helper paths (the IPTV
player and the web dashboard viewer), which used to own a `System.Diagnostics.Process`.
They now use the shipped `EcoreHelperRunner` / `ILongLivedHelper` (same file). Run
this mode on the TV to validate that path:

```
dotnet EcoreRunnerProbe.dll helper   # ends with ECORE-HELPER-PROBE: PASS|FAIL (n/total)
```

Driven by a 50 ms liveness timer and a 45 s watchdog, under a real ecore loop,
against `/bin/sleep` and `/bin/echo` children only (our own; no window, root,
network or owner files), it proves:

- a long-lived helper (`/bin/sleep 8`) is still **Alive past 2.5 s** while the loop
  pumps — it did **not** inherit the ordinary 2.5 s command deadline;
- a forced close sends an **identity-guarded exact-pid `SIGKILL`** that actually
  lands (`EcoreHelper.Killed`), the child's ecore DEL ends `Alive`, and the adapter
  map drains to empty;
- **the crash case**: a helper that **exits on its own while the loop pumps**
  (`/bin/sleep 1`) reaches DEL with **no `FailFast`/`SIGABRT`**, no kill, and the
  loop still ticking — exactly the `ProcessWaitState`/ecore-`SIGCHLD` race the old
  `.NET`-owned child lost;
- a child's complete **stdout line surfaces through `DrainLines`** (the `fatal=` scan path);
- **three concurrent** helpers coexist (tracked at once, under `MaxHelpers`) and each
  gets its own DEL, draining the shared map to empty;
- the **repeat lifecycle** works after a full teardown, and `Dispose` is idempotent
  and non-blocking (never `WaitForExit`).

As with the command probe, **no final `ECORE-HELPER-PROBE:` line** means the process
died (the SIGABRT class this targets) — investigate, do not read as a pass.

**If there is NO final `ECORE-PROBE:` line** (the process died with a SIGABRT and
no output), read that as a FAILURE to investigate, not a pass â€” it is the same
`FailFast` class of crash this fix targets, surfacing in the probe itself. Re-run
once; if it recurs, capture the console and the exit code before judging the fix.

## Build and run

Build a framework-dependent managed DLL on the PC, then copy the DLL, deps file
and runtimeconfig file to the TV and run with its existing .NET 6 runtime.
`UseAppHost=false` means a Linux runtime identifier is unnecessary for this build:

```
dotnet build probe/ecore-runner/EcoreRunnerProbe.csproj -c Release -o build/ecore-runner-probe
# copy the three managed output files to the TV, then on the TV:
dotnet EcoreRunnerProbe.dll
```

`libecore.so.1` and `/bin/{echo,sleep}` must be on the device (they are on
Tizen). The probe needs no arguments.

The pure FD comparator can be exercised with **no TV** (it returns before any native
call), on Windows with the pinned SDK:

```
dotnet build probe/ecore-runner/EcoreRunnerProbe.csproj -c Release -o build/ecore-runner-probe
dotnet build/ecore-runner-probe/EcoreRunnerProbe.dll fd-selftest   # prints FD-SELFTEST: PASS and exits 0
```

`tests/test_ecore_probe_fd.py` mirrors that comparator in Python (no .NET needed),
tests it both ways on synthetic stable vs steadily-leaking sets, and statically guards
the probe source so the tolerance cannot be quietly raised from +2.

**Unverified until the parent runs it on the TV.** Tizen's EFL fork may differ
from the upstream ABI the bindings were read against; this probe is the final ABI
check. It cannot run on Windows (no `libecore`).

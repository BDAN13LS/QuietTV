# Public owner-configured LiveOps provider (2026-10-30)

## Design

Add one fixed provider name, `owner-sdk-root`, under `public-toolkit/tvkit/provider/`.
It implements the existing `tvkit.liveops.LiveOps` / `LiveSession` contract. The public
registry stays empty unless the CLI is given an explicit `--provider-config`; an absent
config keeps today's refusal, and any other adapter name is refused.

The provider config is an owner-private JSON file outside every Git checkout. It is strict
JSON with duplicate-key rejection, exact keys and bounds, and it contains only:

- one private-LAN IPv4 TV target and one private-LAN IPv4 callback host;
- the expected raw SDK DUID pin (never printed, never joined to ProductInfo);
- the absolute SDK `sdb` executable plus SHA-256;
- the two root-payload DLLs built from the checked C# sources plus SHA-256, and the
  absolute OpenSSL executable plus SHA-256 used only by the vendored root launcher.
  Only those two DLLs are externally pinned root-payload files; the upstream launcher
  writes its runtimeconfig files internally.
- bounded timeouts and report attempts.

No target, DUID, key, password, package id or artifact path is hard-coded. Every hash is
revalidated immediately before the file is used. The provider never reads
`private/`, never loads a user plugin, never adds a Helper HTTP route and enables no
consumer install/restore/remove action.

Before its first capture, probe or write, a channel issues one fixed
`sdb connect <target>:26101` for its own process; it never calls `kill-server` or
`disconnect`, and a failed connect refuses without falling back. Every finite method then
starts with the same guard: verify local artifacts, run the raw SDK shell verb
`0 getduid` through the pinned absolute `sdb`, and compare it to the private pin. The raw
verb is used precisely because SDK capture may stage a script, and the guard runs before
the first staging capture as well as before every write. A mismatch or
an unreadable DUID sets a sticky refusal on the session: every later operation, including
cleanup, raises before any probe or write, so a changed target is never mutated. The
opaque token/binding stay stable for the session's life, so the collector can still
distinguish "cleanup refused" from "target drifted mid-collection". DUID is a target
guard only; ProductInfo model and firmware are collected by the diagnostic widget in the
same run and are judged later by the facts-join policy.

Root work uses the vendored upstream `archive_root.probe(commands=..., payloads=...)` in
a fresh child process (`python -I`, minimal explicit environment, vendored package loaded
by absolute path). The child patches only its own module globals (`find_sdb`, OpenSSL
path); the parent environment is never modified. Each probe must report
`authenticated is True`, integer-zero uid/euid/gid/egid, every command `exit_code == 0`
and `timed_out is False`, `system_files_unchanged is True` and `staging_cleaned is True`.
The vendored probe cleans its own `archive-root-<hex>` staging below
`/home/owner/share/tmp/sdk_tools` and the on-demand archive/signature. The provider
additionally removes only its own run directory and the single native output file.

The SDK group is obtained by one finite isolated worker operation that calls the vendored
restricted-shell `SdbClient.capture("id -g")` with an explicit target and callback and the
pinned SDK. The command is fixed internally, the callback output is bounded, and the
terminal `[exit:0]` marker plus one integer line are required. It is not the root probe's
`gid`: that must separately be integer zero. This distinction matches the reference SDK,
which does not run Unix commands through its raw shell.

The worker also assesses the target runtime and rejects anything other than `6.0` before
the upstream payload is staged or executed. The consumer-side fallback likewise lists only
6.x, so a 3.1 target cannot be substituted. All existing authentication, system-file and
staging checks stay in force.

The run directory is `quiettv-collect-<run id>` below the existing
`/home/owner/share/tmp/sdk_tools` (never `/tmp`). It is created with a non-recursive
`mkdir`, owned by the stat-derived UID read from the existing SDK-tools directory, grouped
by the bounded SDK-captured group, mode `770`, and its returned UID/group/mode attributes
are checked before any upload. A pre-existing directory or the global SDK-tools root is
never chmodded or chowned.

Operations:

- `assert_absent`: fixed SQLite count against the package DB plus two fixed report paths
  checked for presence including symlinks. Nothing is pre-cleaned.
- `install_widget`: push the exact verified WGT bytes, read back the remote SHA-256, run
  the fixed single shell argument `0 vd_appinstall <package> <computed path>` (the
  reference verb; no alternate), then require the fixed package and `<package>.Facts` rows
  in the package DB. Exit code alone is never success. Uninstall uses the fixed
  `0 vd_appuninstall <package>`.
- `launch_facts`: fixed `/usr/bin/app_launcher -s <package>.Facts`.
- `read_report`: a real `head -c 256 KiB + 1` bound before base64 of the two fixed data
  roots, re-checking decoded length; symlinks, non-regular files and ambiguity refuse.
- `push_native`: requires this session's own verified fresh run directory (never merely a
  present one), pushes exactly `QuietTVInspect.dll`, `.runtimeconfig.json` and
  `.deps.json` with remote SHA-256 readback, records the actual uploaded paths, and keeps
  the three pushed hashes as this session's only trusted bundle. A partial push leaves no
  trusted bundle.
- `run_native`: requires the stored pushed bundle, then one fixed compound shell guard
  re-hashes all three remote bundle files against those stored pins immediately before the
  fixed `/usr/bin/dotnet <run dir>/QuietTVInspect.dll --inventory-framed`; any changed
  file refuses without executing dotnet. Output is written inside the run directory,
  bounded at 4 MiB (+1), then read in bounded base64 chunks so the upstream 1 MiB
  command-capture limit is never crossed. The output and every chunk read refuse a symlink
  or non-regular file. One transport `[exit:N]` trailer is stripped if present.
- `close_owned_run`: checks the DUID again and uses the session's own stored state: its
  absence result, its confirmed install postcondition and the paths it actually uploaded.
  It uninstalls only a package this session proved absent and then confirmed installed.
  A failed, errored or partial install that leaves a package present is ambiguous: no
  uninstall and no data deletion, with recovery needed. Own uploads may be removed when
  the package is absent; mismatched replacements stay in place. It removes the exact
  empty run directory with `rmdir` only when this session verified creating it.

All commands are fixed internally; only computed alphanumeric package ids, 32-hex run ids
and fixed path names are substituted. Generated command strings contain no backslash and
no double quote, avoiding the upstream SDB escaping caveat. The isolated worker sets the
hardware-measured restricted-shell timing locally before any SDK capture or root preflight:
`SHELL_INJECTION_DELAY_SECONDS = 6.0` and `SHELL_INJECTION_MINIMUM_DELTA = 4.0` (upstream
2.0/1.5 is flaky on the reference TV). The minimum-delta check is retained, not bypassed,
and no host environment or module global changes.

Subprocess stdout/stderr are drained through pipes into fixed buffers while the child
runs; crossing either cap, or a timeout, kills only that process's own tree (its own group
on POSIX, the absolute trusted `taskkill.exe` on Windows). A runaway producer is stopped
before the ordinary timeout and cannot fill disk. Windows children use
`CREATE_NO_WINDOW` and `CREATE_NEW_PROCESS_GROUP`, so the SDK worker, build helper and
kill helper never show a console. No persistent listener, boot/root service, global
`sdb kill-server` or `sdb disconnect` is used.

On Windows the owner configuration is validated from machine-readable SIDs, not localised
account names: the ACL query returns the owner SID, current-user SID and allow/deny ACE
SIDs. The owner must be the current user, LocalSystem or Administrators. Allow ACEs
may name only the current user SID, LocalSystem (`S-1-5-18`) or
built-in Administrators (`S-1-5-32-544`); the current user must own the file or be allowed
by one of those principals. Other allow principals, an unparseable ACL, a missing
current-user path or a failure to query refuse. The query uses absolute
`WindowsPowerShell` with no window and never echoes ACL output. POSIX mode `0600` remains.

A separate local build helper, `tools/tv-collect/provider_build.py`, builds the two
checked C# root payload projects and the inspector with an explicit .NET 6 SDK into a new
owner-private directory outside every repository, and hashes the five outputs into a
`quiettv-provider-artifacts/1` manifest. Its CLI is exact: `--dotnet <absolute dotnet>
--out-dir <new private directory>` builds; `--dry-run` prints the plan and writes nothing;
`--check <absolute manifest> [--provider-config <absolute config>]` re-hashes the
artifacts and, with a config, confirms that it pins those exact root payloads. `--check`
cannot be combined with the build flags, and `--provider-config` is accepted only with
`--check`. It does not invent owner values or write a provider config. No prebuilt binary
is shipped.

## Tests

A focused offline suite drives the real provider and collector with a fake SDK/root
channel: the accept path reaches `collect_reports` and the existing Helper preflight;
missing/invalid config and pins do no device work; target swap before install or cleanup
causes no mutation; unauthenticated/non-root/dirty-root evidence and poisoned bindings
fail; push error with SDK exit 0, package/path collisions, output oversize/timeouts,
ownership replacement, missing `kill-server`/`disconnect`, and secret/path leakage are
covered. Command-boundary tests assert the exact finite SDK arguments and root command
strings, including `connect`, `0 vd_appinstall`, `0 vd_appuninstall` and the mode `770`
directory check. Channel tests require a failed connect to refuse before DUID or write and
a single cached connect per session; worker tests require one fixed `SdbClient.capture`
group operation (no raw `id -g`), bounded parsing, and rejection of non-6 runtimes before
probe. Streaming process tests cross stdout/stderr caps in long-running producers, prove
the kill happens before the timeout, and prove a descendant does not survive. On POSIX,
real local shell fixtures check the bounded report read against oversize, growing, symlink,
FIFO and directory files; command and decoder tests run on every platform. Native tests
require a session-created directory, a complete pushed bundle, and a remote hash guard
that refuses a replaced DLL before dotnet; a failed install with a package present is
never uninstalled and its data is left for recovery. Windows tests cover the owner/SYSTEM/
Administrators SID ACL accept path, other named/world allow denial, unparseable ACLs and
hidden child flags; the worker test pins the measured 6/4 injection timing. Build-helper
tests cover dry-run and manifest checking.

## Reference hardware verification (2026-10-30)

The reviewed public `owner-sdk-root` provider was exercised once end-to-end on the
reference Samsung UN43U8000FBXZA / firmware T-X22UDFAKUC-0090-1300.8 / Tizen 9 /
ARMv7 / .NET 6.0.9, using an owner-private config, an official owner-signed Facts
widget and an inspector built from the checked sources.

- The SDK raw-identity pin matched. Temporary root was authenticated with uid/euid 0;
  root staging was cleaned; the original `/etc/passwd` and SDB key hashes were unchanged.
- The full CLI collected a fresh ProductInfo report and native frame, joined all seven
  facts, wrote the exact Helper config, and the SDK uninstalled only its own diagnostic
  package and removed its uploads and run directory.
- An independent physical root read confirmed the package, both payload/data roots and
  the run directory absent, four protected runtime hashes unchanged, and boot/control
  service/dashboard PIDs unchanged; Home was requested.
- The actual Helper CLI and authenticated HTTP loaded the exact new config and displayed
  the seven facts with sources, without identity, token or source paths. All change
  actions remained OFF; the Helper live guided flow was not wired.
- The complete collection took 570.579 s (~9 min 30 s) because of 17 isolated root probes.

This is a one-time ADVANCED diagnostic. It does not claim a cold-boot improvement, a
consumer installation, a fresh-stock end-to-end result, or consumer restore/cleanup capability.
Consumer install, cleanup and restore remain unavailable until independent hardware
Rescue and full restore are verified. No stock firmware, packages, keys or binaries are
distributed. After the hardware run, small changes (1 MB boundary, standard numeric
RFC 1918 range form, synthetic example addresses, source-guard passwd colon parsing)
were verified offline; the provider root core is the hardware-tested reference path.
No TV reboot or startup/launcher change was made for this feature.

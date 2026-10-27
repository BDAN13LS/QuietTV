# QuietTV public native host (source release candidate)

This folder is a portable source copy of the private .NET 6 native host. It keeps
the known Tizen EFL calls and launch behaviour. Every device-specific value now
comes from one validated configuration file. **It is not a consumer install.** Nothing
here deploys to a TV, signs a package, gains root or edits firmware.

## Where it came from

The eight dependency files were taken read-only from `main:app/` (git blob ids):

| file | blob |
|---|---|
| Canvas.cs | `fb856b435383f34a99a5ffe920ac33e1226bb25a` |
| IptvSession.cs | `5edd981cbfc8376412672efbb9d16925530f9e3b` |
| Native.cs | `b82f7b7ffba6d72e33b5baf6b776b286ea507f94` |
| Program.cs | `0f34df05ff5a94484c7b73c37f586a9b1698b57f` |
| RemoteGate.cs | `e3ae8eea8d2021dcd91341c23c841bb4cc3a9b49` |
| State.cs | `8334d48f85107534b225156544896d63a756ec67` |
| WebFrontend.cs | `5c874d0d5cbfe0a6e509dea4a300181127a1a0ec` |
| QuietTV.csproj | `e150ae8467d799f2624f8b543f6a4ad6e20e41ac` (→ `QuietTVHost.csproj`) |

These files are new: `HostConfig.cs` (the config seam and the owner-data loader),
`HostEffects.cs` (startup and launch gates, input-event layout) and `SelfTest.cs`.
`RemoteGate.cs` is adapted from upstream; see `NOTICE.md`.

## The config seam (`--config PATH`)

The host reads only the JSON file given with `--config` (schema
`quiettv-host-config/1`). It never falls back to environment variables. The parser
rejects unknown keys, duplicate keys, nulls, wrong types and placeholders
(`<…>`, `${…}`, `CHANGE_ME`, `example`, …).

| key | rule |
|---|---|
| `runtimeRoot` | a plain POSIX path below `/home/owner/share/` or `/opt/usr/home/owner/share/`, safe segments only, no `.`/`..`/`//`. The last folder must be `quiet-tv` or `quiet-tv-<suffix>`. |
| `ownerUid` | the Tizen owner uid, 5000–5999. Used for `app_launcher -u` and `XDG_RUNTIME_DIR`. |
| `ownAppId` | the host's own registered app id (`<10-char package>.<Name>`) |
| `homeKeyCode` | 1–767, the Linux key code that opens the dashboard |
| `observedInputPaths` | 1–8 distinct `/dev/input/eventN` paths, opened read-only and never grabbed |
| `remoteDeviceNames` | exact `/proc/bus/input/devices` names. Required only when grabbing. |
| `remoteGrabEnabled`, `iptvEnabled`, `webUiEnabled`, `snapshotEnabled` | optional booleans, **default false** |

`host-config.example.json` still contains placeholders, so the host rejects it
unchanged.

The owner must also generate two files inside `runtimeRoot`:

- **`apps.json`:** a list of `{"Id","Name","Icon"}`. Every id must be safe and appear once. An icon must be empty or a file inside the root.
- **`settings.json`:** the keys `Order`, `Favorites` and `Wallpaper` 0–2. A legacy `HomeKeyCode` key is ignored.

If either file is missing or malformed, the host exits with code 2 and prints
`config_rejected=…`. It does this before any native call or filesystem write, and it
never creates the root folder. Ids for apps that are no longer installed are dropped
from `settings.json` rather than rejected. No real catalogue, artwork, favourites or
token ships here.

## Fail-closed defaults

- **IPTV off:** `quiet.iptv` is hidden from the catalogue and rejected by `LaunchGate` from every launch source: remote, web action and `launch.request`. `IptvSession.Start` refuses before it spawns anything, so `QuietIPTV.dll` is never launched.
- **Web UI off:** `--web-ui` alone does nothing, and `WebFrontend.Start` refuses. The web viewer *is* `QuietIPTV.dll --dashboard`, so it also needs `iptvEnabled: true`: with `webUiEnabled: true` and `iptvEnabled: false`, startup and `WebFrontend.Start` both refuse, and `QuietIPTV.dll` is never launched.
- **Snapshots off:** no snapshot timer is scheduled, so `screen.png` is never written.
- **Remote grab off:** `RemoteGate` (EVIOCGRAB plus uinput) is never constructed. The configured inputs are observed read-only instead.
- **Home watcher:** the `input_event` size follows `IntPtr.Size` (24 or 16 bytes). On any other size the host refuses to start.
- **Inputs tile (unsupported in the native fallback):** the native canvas's Inputs tiles carry the synthetic id `quiet.inputs`, which is not a real Tizen app id. The original passed it to `app_launcher`; this host refuses it from every source, even if the catalogue lists it, and shows "Inputs are not available here. Use the Source button on the remote." No native input-picker app id is assumed. The registered widget's own input handling is outside this host and unaffected.
- **Other launch ids:** remote, web actions and `launch.request` accept only catalogue ids and the two Samsung system ids.
- **Unreadable owner files:** an `apps.json` or `settings.json` that cannot be read is rejected cleanly with `config_rejected=`.

Every native process uses `ProcessStartInfo.ArgumentList` with `UseShellExecute=false`
and goes through one `IProcessStarter`.

## Building (offline)

```
$env:QUIETTV_DOTNET = '<path to SDK 6.0.428 dotnet.exe>'
pwsh -NoProfile -File tools/build-public-host.ps1
python -m unittest tests.test_public_host_source -v
```

The SDK is pinned by `launcher/host/global.json`: version 6.0.428, with
`rollForward: disable` and `allowPrerelease: false`.

The build script:

1. Runs every `dotnet` command with this folder as its working directory, so the pin applies wherever the script is started from. It restores your previous working directory and environment afterwards, even when the build fails. It refuses to run if `global.json` is missing or the pin is loosened, and checks that `dotnet --version` reports 6.0.428.
2. Compiles once into `launcher/host/bin/release/QuietTV.dll`, with Deterministic, ContinuousIntegrationBuild, PathMap and no PDB. Restore uses a NuGet config with no sources, and all caches stay under `obj/`, which is deleted afterwards.
3. Runs `--self-test`. The self-test needs no Tizen libraries and performs no session-file disk operations: `IptvSession` and `WebFrontend` read, write and delete their session files (and the `/proc` identity check) only through `ISessionFiles`, and the self-test passes them an in-memory fake. The only disk implementation, `DiskSessionFiles`, is created in `Program.Main` after the config is validated, and never on the self-test path.
4. Fails if any absolute path from the build machine appears in the output.

The Python test also:

- builds twice and compares the bytes, including from a copy with Windows line endings in another folder;
- checks that every `dotnet` call runs in this folder (using a stand-in `dotnet`) and that the real `dotnet` honours the pin and `rollForward: disable`. Only one SDK is installed on the build machine, so a mix of SDK 6 and newer SDKs was not tested;
- runs the dll against missing and example configs;
- for each guard, removes it (or restores the older, weaker version) in a copy and checks that the self-test then fails. This covers: the web viewer starting with IPTV off; stale-session cleanup going to the real disk; and the Inputs tile being launched.

## Not provided here

### Parent verification on the test TV

The exact build with SHA-256
`c1a52aed2e00aa450789f65f0526507e94d16549eaea13944e09a696f062cf64`
passed `--self-test` in temporary SDK storage on the test Samsung's ARMv7
Tizen9/.NET6 runtime. This exercised the config/model/feature guards with
memory fakes. It did not render a new dashboard, read physical input events,
install a service or test the native source picker. Temporary files were
removed; the existing dashboard PID and boot were unchanged.

Runtime paths are validated lexically. Installer validation of directory
ownership, filesystem links and the privileged launch policy is still a
release gate; this candidate is not a production installation path.

- **Service:** no systemd unit or launch service.
- **Widget:** no registered widget package. The registered widget is the intended cinematic UI.
- **Config generator:** no tool writes the host config, `apps.json` or `settings.json`.
- **Root:** no root or foothold step.
- **Signing:** no packaging or signing.
- **Firmware gates:** firmware and model gates are left to the future Helper.
- **Excluded binary:** `QuietIPTV.dll` is not included. The IPTV and web viewer paths stay off unless an owner supplies it and enables them.

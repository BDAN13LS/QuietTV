# QuietTV setup and recovery toolkit (developer preview)

Project: **https://github.com/BDAN13LS/QuietTV** (releases:
https://github.com/BDAN13LS/QuietTV/releases).

The first public release is a **developer preview**: this host-side toolkit
plus the **launcher frontend source**. It is **not** ready-to-run firmware,
there is **no automatic TV installer**, and nothing here performs a live setup
or restore (see [Release blockers](#release-blockers)).

A small host-side toolkit for **one owned TV**: a Samsung **UN43U8000FBXZA** on
firmware **1300.8**, **armv7l**, **Tizen 9**. It helps the owner back up the
original state of their own TV before installing a custom launcher, keep that
backup **off the TV**, check it, and plan an exact restore.

It is **not** a root exploit, not a firmware flasher, and not permission or a
method to modify anyone else's TV. Other models and firmware are refused, not
guessed at.

## What works today (PC side, tested offline)

| Command | What it does |
|---|---|
| `doctor` | Checks Python, the built-in profile, your config, and lists the manual prerequisites. |
| `profiles` | Lists the one supported profile. |
| `capsule validate PATH` | Strictly validates a recovery capsule (folder or `.tar` bundle): device binding, every byte hash, UID/GID/modes, xattr manifest, symlinks, registry and startup records. Add `--device-facts` to bind it to the live device identity. |
| `capsule assemble SPEC` | Builds a capsule folder on your PC from payload tars and XattrBackup dumps your own transport captured. |
| `capsule bundle DIR` | Packs a capsule into one `.tar` and prints the SHA-256 to pin. |
| `fetch SOURCE` | Copies a local/NAS file or downloads an `https://` URL, with a **mandatory pinned SHA-256**, a size cap and timeouts. Never uploads. |
| `plan setup` / `plan restore` | Prints the ordered plan (dry run). |
| `apply ...` | Dry run unless `--apply`. With `--apply` it **refuses** unless an implemented transport adapter is named. **None ships here**, so it always refuses and changes nothing. |
| `export` | Lists or writes a clean public source export from a positive allowlist; `--with-frontend` adds the launcher frontend source through its transforms. |

## What is NOT included (you must supply it yourself)

- **Developer Mode** on the TV, with your PC as the trusted host.
- **Root access** on this firmware. Not included; this toolkit is not a root bypass.
- Your own **signing certificate** for installing the launcher package.
- A **privileged transport adapter**: the code that actually runs plan steps on the TV. The adapter registry (`tvkit/transport.py`) is empty on purpose.
- The **XattrBackup** helper (`tools/xattr-backup`) built locally, plus a .NET 6 runtime on the TV.
- **The TV-side launcher host and Home widget** (see [Release blockers](#release-blockers)).
  The frontend source ships; the parts that run it on the TV do not.

## Release blockers

Runtime files the launcher needs that this release does **not** contain. Each
is listed in `export-allowlist.json` → `notExported`:

| Missing | Why it is not here |
|---|---|
| `app/` (native .NET launcher host: `Program.cs`, `State.cs`, `RemoteGate.cs`, `WebFrontend.cs`, `Canvas.cs`, `Native.cs`, `IptvSession.cs`) | Device-side; not positively audited for a public build. Without it the frontend has no TV-side host. |
| `boot/widget/` (registered Home widget: `config.xml`, `icon.png`, `bridge.js`, `home.js`, `tv-inputs.js`) | Device-side; `config.xml` names the owner and declares the excluded on-boot service. `bridge.js`/`home.js` only work against that service. |
| `boot/widget/service/` and `boot/widget/payload/*.dll` | Privileged on-boot service and native payload. Never published. |
| `iptv/` (IPTV player host and web app) | Not positively audited; bundles third-party scripts whose licences are not yet recorded. |
| `frontend/public/native-icons/` and the four brand PNGs in `frontend/src/assets/` | Icons extracted from the owner's TV (Samsung and partner artwork). The launcher shows its own fallback art instead. |
| `frontend/dist/` | Build output; rebuild with `npm ci && npm run build`. |
| Signed launcher package, boot cache, compiled payloads, root state, vendor binaries | Never published. |

Excluded frontend tests (each with its reason in `frontend-export.json`):
`weather.test.mjs`, `appearance.test.mjs` and its `fixtures/`,
`home-polish.browser.mjs`, `launcher-appearance.browser.mjs` (all built on the
owner's home location), and `visual-sources.test.mjs` (reads `boot/`).

## Not supported: Security Center

`com.samsung.tv.SecurityCenter` (and anything else matching the profile's
protected list: Settings, HDMI, sources, audio, network, Store/update, web
engine, core security) is refused as a capsule package, a registry app id and
a setup install id. An informational Security Center screen from private work
is **not** supported here, and the protected rules are not relaxed to allow it.

## Using an AI assistant with this project

- The ordinary command line (`tvkit_cli.py`, `Setup.ps1`) needs **no AI model,
  no account and no network**, and always works offline.
- Which assistant, if any, you use is your decision. Assistants may refuse
  privileged or device-modifying requests (Claude, for one, may decline them);
  that is expected and this project does not try to work around it.
- Claude 4.6 is **untested** with this project. **No model is guaranteed** to
  give correct or safe instructions for your TV: check every plan with
  `plan ... --json` and the validator, and keep your capsule off the TV.

## Quick start (Windows)

1. Download the release archive from
   https://github.com/BDAN13LS/QuietTV/releases (`.\Setup.ps1 release-url`
   prints the address).
2. Check it: `Get-FileHash .\tvkit-release.zip -Algorithm SHA256` and compare
   with the published hash. Do not run anything if it differs.
3. Extract it and run, from the extracted folder:

```powershell
.\Setup.ps1 doctor
.\Setup.ps1 validate  D:\TV-Backups\my-capsule
.\Setup.ps1 restore-plan D:\TV-Backups\my-capsule
.\Setup.ps1 fetch D:\TV-Backups\my-capsule.tar -Sha256 <64 hex> -Out C:\Temp\capsule.tar
```

`Setup.ps1` only finds Python 3.9+ and runs `tvkit_cli.py` in isolated mode
(`python -I`). It never downloads or executes anything from the network.
Without Windows: `python -I tvkit_cli.py --help`.

Copy `config/tvkit.config.example.json` to your own file **outside this
toolkit folder** and fill in the `<CONFIGURE: ...>` fields (`releaseRepoUrl`
is already the project address; change it only for a fork). Commands that need
a field refuse until it is set. Keep your config, device-facts files and
capsules outside the toolkit folder too; the source export refuses a filled
config or any JSON carrying a `deviceId`.

## The recovery capsule

A capsule holds **your own** original files and before-states from **your
own** TV, never generic Samsung binaries. Keep it on your PC, NAS or a private
URL; never on the TV, never in a public repository.

```
capsule.json                  manifest (schema tvkit-capsule/1)
payload/<packageId>.tar       the package tree: bytes, UID/GID, modes
xattrs/<packageId>.json       XattrBackup manifest version 1 (unchanged contract)
```

`examples/three-app-capsule.example.json` is a synthetic three-package
example (fake package ids, fake device id), and
`examples/three-app-assemble-spec.example.json` is the spec that builds it
with `capsule assemble`. Existing private backups need no migration: keep
them as they are and run `capsule assemble` over their payload tar and
XattrBackup dump when you want a capsule.

Symlinks are resolved through the other links in the tree, not just by text,
so `a -> .` plus `b -> a/..` is refused. Symlinks pointing anywhere outside
their own package root (including absolute system paths) are refused too,
which a real stock package may need relaxed after review.

The validator refuses a capsule when any of these is true:

- model, firmware, arch or OS differs **exactly** from the profile, or the
  device id hash differs from the live device (the raw id is never stored);
- any payload or xattr byte, file hash, size, UID, GID, mode or link target
  differs from the manifest;
- a path is absolute, has `..`, a backslash, a drive letter, control
  characters, or collides with another path ignoring case;
- an archive member is a hard link, device, FIFO or sits below a symlink, or a
  symlink points outside its package root;
- a package, registry app id or startup key is duplicated or conflicts;
- an install root is outside `/opt/usr/apps/<id>` or `/usr/apps/<id>`;
- an id touches Settings, HDMI, sources, audio, network, Store/update, the web
  engine or core security (the profile's protected list);
- a registry field is outside `app_disable`, `app_onboot`, `app_autorestart`,
  or a startup key is outside `db/appfw/startup_appid`, `db/appfw/home_appid`;
- a startup/Home record's value does not hash to its recorded SHA-256;
- JSON has duplicate keys, `NaN`, unknown properties or booleans used as numbers.

## Plans

**Setup** always backs up and **rehearses a restore first**: the capsule is
restored into a scratch folder on the TV and verified, the registry change is
tried on a copy of the database and reversed, and the startup keys are only
read and compared. Installing and changing anything comes after the
`rehearsal-passed` gate.

**Restore** order: check target → stage files → extract bytes → UID/GID →
modes → xattrs → re-check modes → verify tree → move into place → verify →
only the changed registry fields (guarded SQL: every row checked before any
update) → startup/Home keys (written only when the current value hashes to
what the capsule expects; other keys untouched) → owner verifies Home, remote,
Settings, sources and Store → **our own launcher is removed last** → final check.

A plan never replaces a whole registry database, never restores a stale
global cache, and never flashes firmware.

## Clean source export

```powershell
python -I tvkit_cli.py export --list --with-frontend
python -I tvkit_cli.py export --with-frontend --deny-values-file D:\private\deny.txt --out D:\export-new
```

Only paths in `export-allowlist.json` are considered; `.gitignore` is not
trusted and no git history is copied. `includeIfPresent` adds
`docs/screenshots/` and the root `README.md` when they exist (absent ones are
listed). The root `README.md` ships only if it contains the line
`<!-- quiettv:public -->`; otherwise it is the private workspace README and is
listed as excluded.

The MCP package is integrated separately from an explicit tracked-file list,
using its own `public_source_guard.py` and actual-owner deny hashes. Its
documented synthetic LAN/MAC and planted-secret fixtures deliberately fail
this toolkit's generic scanner. Do not widen either scanner to make an
integrated export pass; review those fixtures and approve their exact hashes
with the MCP guard. The final combined tree still needs both privacy checks.

Builds, packages, binaries, certificates, logs, archives, dumps and databases
refuse the export, as do MAC addresses, private IP addresses, PEM keys, Windows
user paths, tokens and real email addresses found anywhere in a file's bytes
(UTF-8 or UTF-16, images too). JPEG, WebP, GIF, TIFF, HEIC and AVIF images are
refused (their metadata is not filtered; convert screenshots to PNG).
`--deny-values-file` holds the owner's exact private values, one per line; they
are matched but never printed. Exclusions must be declared with a reason and
are always listed. The output folder must be new or empty and outside the
source tree; `EXPORT-MANIFEST.json` lists every file's SHA-256, and for a
transformed file also its source SHA-256 and the transforms applied.

### Frontend export (`--with-frontend`)

The private `frontend/` folder is never edited. `frontend-export.json` lists
every file that ships; every other file in the folder must match an exclusion
with a reason, or the export refuses. On the way out (`tvkit/frontend_export.py`):

- `src/weather.mjs`: the owner's default ZIP code and place become
  unconfigured values (`zip:''`, `DEFAULT_PLACE.zip:null`), and weather starts
  **off** until a ZIP code is entered. Saved settings are kept as they are.
- `src/main.jsx`: the imports of the two brand PNGs extracted from the TV are
  removed, so those tiles use the launcher's fallback art.
- Every PNG passes `tvkit/pngmeta.py`: chunks are parsed and every CRC is
  checked; only `tEXt`, `zTXt`, `iTXt`, `eXIf` and `caBX` (C2PA) are dropped,
  a text chunk keyed `Copyright` is kept, and all other chunks, including
  `IDAT`, are copied byte for byte. Pixels are never decoded or re-encoded.
- Every relative file reference in the exported source must point at an
  exported file, or be a declared gap with a reason.

Each transform anchor must match exactly once, so an upstream edit stops the
export rather than shipping half-transformed. The three cinematic wallpapers
are AI-generated images; their C2PA manifests (which named the generator and
embedded a signer contact address) are removed, and any watermark in the
pixels is untouched. The brand SVG marks are simple-icons style CC0 artwork;
the brands themselves remain their owners' trademarks.

## Licence

No licence has been chosen for this project yet; the owner decides before any
public release. Third-party components keep their own licences
(`THIRD-PARTY-NOTICES.md`).

## Tests

```powershell
python -m unittest discover -s tests -p "test_public_toolkit_*.py"
```

All tests use synthetic fixtures in temporary folders; none read a real backup
or talk to a device. One test exports the real frontend into a temporary
folder and, when Node.js is installed, runs its unit tests and checks the
unconfigured weather defaults there. `python -I dev/guard_off_check.py <repo root>`
switches each safety guard off in a scratch copy and confirms its test then
fails (29 guards).

In the exported frontend: `npm ci`, then `npm test` (unit tests) and
`npm run build`. The browser tests need Playwright installed globally and a
build in `dist/`.

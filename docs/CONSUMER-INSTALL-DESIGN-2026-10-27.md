# QuietTV consumer install and restore: design (2026-10-27)

Design only; nothing built. Plan:
`docs/superpowers/plans/2026-10-27-consumer-install.md`.

## 1. Product

QuietTV is a **replacement TV launcher** for privacy and performance. The
product is three things: an owner-authorized **root** bootstrap, the **new
launcher**, and a **removal menu** that physically removes proven-optional
Samsung payloads (tracking, ads, Bixby, TV Plus, bloat), each restorable.
Kept, always: display, audio, input/sources, Settings, security core, DRM,
the app store and the update backend. Normal install, use and restore need
no AI, subscription, command line or coding. AI tooling is an optional
developer footnote.

Owner's goal: on **stock firmware**, enable Developer Mode, type a URL into
the TV browser, and have it actually install root, launcher and the chosen
cleanup. A Windows option, or both, is acceptable.

## 2. Verified starting point

- Public repo `BDAN13LS/QuietTV` at `f2497c3`: frontend source, offline
  toolkit (capsule, fetch, plans), MCP server and human CLI. `TRANSPORTS` is
  empty and `apply --apply` refuses. No public transport, TV host, widget or
  root installer.
- Privately, on **one** TV (UN43U8000FBXZA, fw 1300.8, ARMv7, Tizen 9):
  launcher 2.0.18 warm-installed and working; root recovers after a cold
  boot without the PC (an independent, pinned TV-side helper); original signature,
  cache and native hashes protected. Physically removed: Guide, the
  informational Security Center UI, TV Plus. Browser kept. Held because of
  update dependencies: the voice client/speech-to-text package and the AI
  discovery/download-content package.
- **Not proven:** all Samsung tracking gone; any RAM saving; Store update
  installs; DRM; HDMI video. IPTV picture is black, so consumer builds hide
  it or mark it unavailable until it is fixed. The black startup gap is
  unresolved and out of scope.
- The public repo has no `app/` host or `boot/widget/` service yet.
  Publishing them as sanitized source, with per-owner builds and signing, is
  plan phase D5b.

## 3. Evidence and prior art (qualified)

| Source | What it supports | What it does not |
|---|---|---|
| [Samsung: TV device setup](https://developer.samsung.com/smarttv/develop/getting-started/using-sdk/tv-device.html) | Developer Mode takes the developer PC's IP, then a TV restart; installing widgets from USB is unsupported | Any root grant |
| [Samsung: creating certificates](https://developer.samsung.com/smarttv/develop/getting-started/setting-up-sdk/creating-certificates.html) | Distributor certificates target a device DUID and need a Samsung account | Root, or per-user partner-level eligibility |
| [Samsung: device information and firmware FAQ](https://developer.samsung.com/smarttv/develop/faq/device-information-and-firmware.html) | Firmware downgrade unsupported; full factory reset changes the DUID; Reset Smart Hub does not | Restoring physically removed files (no such promise anywhere) |
| [chris-ritsen/samsung-tv-root](https://github.com/chris-ritsen/samsung-tv-root) | Publishes Windows archives and exact-model profiles; routes start from an SDB foothold. Our private pinned adapter follows this per-firmware pattern, so no universal custom firmware is needed | Browser-only operation |
| [SushyDev/tizen-homebrew](https://github.com/SushyDev/tizen-homebrew) | README: a PC bootstrap discovers the TV, mints the owner's own certificate and installs; afterwards a phone/URL catalogue works | Raw-URL root on stock (the helper is already installed). Mints a partner certificate by default; the public-certificate on-boot path is untested; per-user partner eligibility not assumed |
| [abhayap/apps2samsung](https://github.com/abhayap/apps2samsung) | Installer from a computer or Android phone, with automatic certificate handling "in most cases" | Root |
| [andrearicci93/vetro](https://github.com/andrearicci93/vetro) | Launcher replacement; README needs an initial self-built, signed package with root | A generic URL installer |
| [Gathin23/cleanyourtv](https://github.com/Gathin23/cleanyourtv) | Removal-menu UX to borrow | Tizen (it is Android/Google TV only; its ADB transport does not apply) |

Licence and provenance are checked before any code is borrowed. No legal
ruling on redistribution was found. Unrelated Samsung EULAs (Gear, the
Developer Portal) are not cited as the TV licence.

## 4. Install routes, kept distinct

| Route | What it is | Status |
|---|---|---|
| **A. Raw stock browser page** | A page loaded by the stock browser, with no helper | Install/root from page content alone is unproven, and not ruled out |
| **B. TV browser as screen for a LAN helper** | The page is served by the QuietTV Helper on the owner's PC; the Helper does the device work | Plausible; D0a checks reachability |
| **C. URL triggers an existing per-firmware acquisition route** | A browser URL reaches an existing, validated, firmware-specific root route on stock firmware | **Unproven either way.** Viable only if the route is genuinely reachable from the browser on that exact firmware |
| **D. Already-installed homebrew catalogue** | After a PC bootstrap, the TV/phone menu installs and removes (tizen-homebrew pattern) | Proven pattern elsewhere; first install still needs B or a PC |

**Developer Mode is not root.** Root comes from an owner-authorized,
per-firmware bootstrap. These planning docs give no exploit steps. That is a
documentation choice, not a ban on shipping reviewed upstream root code
under its licence after a secrets and provenance audit. No permanent
private-only root prerequisite is introduced.

**Recommendation:** ship **Windows first** (PC Helper, optionally viewed from
the TV browser as route B). Routes A and C stay research. Failing them means
they are **not advertised or shipped** and do not delay Windows V1; further
research on them needs an owner cost decision.

## 5. D0: TV-URL feasibility, in two parts

**D0a, non-mutating capability review.** Compare three things separately:
the raw stock browser, a LAN Helper page, and an already-installed homebrew
catalogue. Record which browser features exist (LAN `http`, `fetch` back to
the Helper, downloads, private-network blocks) and which published routes
start from what foothold. A page load or user agent proves reachability
only. **It can never prove install or root.**

**D0b, later and only with explicit owner authorization:** a bounded
hardware proof of bootstrap, signed install, uninstall and root, limited to
QuietTV's own namespace. No cleanup and no boot changes in this probe. Root
or install counts only if it actually worked from stock, from the browser,
with no PC or helper involved.

**Off-TV save:** if the browser cannot save off the TV, raw browser-only
deletion stays blocked **until** an independently validated off-TV backup
channel exists (for example USB or NAS via the root helper later).

## 6. One engine, several front ends

```
 Windows Helper UI --+                         +-- Rescue (PC, cached offline,
 TV browser (B) -----+--> QuietTV engine <-----+    restore-only)
 On-TV menu (D) -----+    profile, catalogue,
                          capsule, plan, journal,
                          write-audit, transports
                            | devmode   | root bootstrap (pinned, per firmware)
```

- The engine is today's `public-toolkit/tvkit`, extended. Device facts reuse
  `profile.parse_device_facts` and `match_device`. Plans stay data; only a
  registered adapter runs them, behind the existing `run_plan` gate.
- **Windows packaging:** the python.org embeddable runtime with a local
  `127.0.0.1` web UI. No Python, npm or Tizen Studio for the owner to install,
  and no Electron. Samsung tools (for example `sdb`) are bundled only if their
  licence allows it; otherwise the Helper downloads them once from the
  official source, with a pinned hash.
- **LAN companion:** opt-in bind, pairing code, Origin/Host checks, short
  session. Nothing is reachable from the internet, and no root daemon is
  exposed.
- **Rescue:** internet/WAN blocked, LAN to the TV available. It carries its
  own cached bootstrap/root method, independent of the launcher and any
  on-TV agent. If that method is unavailable (for example after a firmware
  change), Rescue says so plainly: **no hard-brick guarantee**.
- **On-TV menu (D):** the launcher works without the PC always on. A
  destructive change needs either a reachable backup channel or a verified,
  already-exported backup of **that exact component version**. Otherwise
  Remove is greyed out with the reason. It never silently disables something
  and reports it as removed.

## 7. Recovery first, cleanup last

Order: **backup → off-TV readback → rehearsal → launcher → Rescue proven →
cleanup.**

A temporary bootstrap may be needed to read protected files. Its side
effects must be inventoried and journalled, with any affected originals
captured before they are touched. Stock deletion and persistent Home/boot
changes wait for verified off-TV recovery; failed setup removes its own
staging. Do not promise zero staging writes before privileged backup.


- **Backup** per owner, exact device and firmware: bytes, UID/GID, modes,
  xattrs, symlinks, and only the changed registry, Home and boot fields, each
  hashed. Taken on stock, it is labelled "stock baseline". Taken on a modified
  TV, it is labelled "current-state" and cannot restore earlier removals.
- **Hard gate:** the backup is hashed on the TV, re-read from its off-TV
  storage, validated with device binding, and rehearsed in a scratch area
  before any delete. USB write from the TV is not assumed.
- **Schema limits.** `tvkit-capsule/1` cannot represent the full private
  boot changes. Its install roots are app directories only, it allows two
  Home/startup keys, and it refuses absolute or external symlinks and hard
  links. So:
  - the first slice is explicitly limited to capsule/1 scope;
  - **full "Restore Samsung" is not claimed** until a versioned recovery
    schema and profile extension exist.
- **That extension:**
  - per-record, per-field three-way compare-and-set undo of **our** boot
    cache and service changes, preserving unrelated changes. Never a full
    overwrite of a stale global cache or registry;
  - match on the full firmware build, region and arch, plus per-package
    version and hash;
  - an explicit policy for external symlinks: read-only references recorded
    and checked, versus safe-extraction rules. No blanket loosening;
  - no SDK certificate or owner state in any public catalogue.
- **Restore order:** our changes first, then remote and native Home verified,
  then launcher and root helper removed **last**. If model or firmware
  changed, stop. If an app is newer than the backup, do not overwrite it;
  report it.
- **Recoveries, named separately:**
  - **Factory reset:** changes the DUID, and is not relied on to bring back
    removed files.
  - **Reset Smart Hub.**
  - **Samsung USB firmware update:** no downgrade. An official firmware
    download is not a restore guarantee.
  - **QuietTV restore:** from our capsule.
  - **Hard brick:** out of scope.

## 8. Hosting

- **Now:** we host our tools, profiles, catalogues, signed manifests
  (minisign, pinned key) and checksums, with reproducible builds and
  rollback pins. The Helper keeps an offline copy, so a GitHub outage does
  not block restore.
- **Initially:** owner capsules stay off the TV in owner-controlled
  storage (PC, NAS or USB, encrypted).
- **Later, possibly:** rights-cleared, exact-version **generic** payloads,
  published only if permission is verified, a clean scan passes and
  compatibility is proven.
- **Never:** shared accounts, certificates, registry data or per-device
  payloads. Root and signing are per owner.

## 9. Removal menu

- A **Recommended privacy cleanup** preset, plus per-component choices
  explaining what each one does, what depends on it, what you lose, and
  restore support. Browser is optional.
- **States reported truthfully:** removed (physically), disabled, kept,
  held, failed.
- The owner approves the exact change list. Visiting a URL changes nothing
  automatically.
- **Restore Individual / Restore Samsung**, within the schema limits in §7.
- **Security Center:** substring protection stays. It gets an explicit
  reviewed per-component profile, and core security is not weakened.
- **Firmware auto-update hold** is a separate, explained opt-in. App updates
  stay on, and count as working only after an actual update install is
  accepted.

## 10. Claims discipline

- Storage and RAM are reported separately; RAM is PSS (fair share of shared
  memory) under a matched workload.
- The privacy audit covers known services and domains only: no proof of
  zero telemetry, nothing about third-party apps.
- Diagnostics are opt-in and exported locally only. The website has no
  unique-ID tracking.
- Known hardware is validated, not promised.

## 11. Critical feasibility blockers

1. **Browser reachability of a root route on stock firmware** (routes A
   and C) is unproven either way. D0a scopes it; only D0b can prove it.
2. **The recovery schema.** capsule/1 cannot hold the full boot changes, so
   full Restore Samsung waits for the versioned extension.
3. **Per-owner signing without Tizen Studio.** It needs the DUID and a
   Samsung account. Partner-level eligibility per user is not assumed.

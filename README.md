<!-- quiettv:public -->

# QuietTV

**Replace Samsung Home, remove unwanted Samsung software, and keep the TV features you use.**
QuietTV uses owner-authorized root and backend cleanup.

**Verified profile: Samsung UN43U8000FBXZA · firmware 1300.8 · Tizen 9 · ARMv7.**
This is the only verified profile; other models and firmware are not supported.

**Install status:** the launcher and cleanup run on the test TV. The public installer
and live restore tool are still being built. Source and developer tools are available;
there is no ready-to-install release yet.

## What it changes on the reference TV

| | Summary |
|---|---|
| **Adds** | QuietTV Home and startup, remote navigation, app cards, wallpapers, appearance options and optional weather. |
| **Removes** | 27 optional payload directories, including quarantine copies: Samsung analytics, targeted ads, Bixby/voice, AI Companion, content recognition, Guide, the standalone TV Plus app and the Security Center information screen. |
| **Blocks** | Selected remaining Samsung background services. Some read-only ROM components are blocked, not deleted. |
| **Keeps** | Tizen OS and drivers, picture/sound, inputs and HDMI switching, volume, network and core security, native apps, Browser, and Store/update dependencies. Two shared helper packages are held pending dependency proof. |

About **250 MiB** of storage was reclaimed, excluding the separate analytics removal.
**RAM savings were not measured.** Removed files leave disabled registry tombstones, and
this is not proof of zero tracking. Exact inventory:
[docs/CLEANUP-STATUS.md](docs/CLEANUP-STATUS.md).

![QuietTV home screen in day mode with scenic mountain artwork, app row and recent apps](docs/screenshots/home-day.png)

![QuietTV home screen in night mode with dark scenic artwork, app row and recent apps](docs/screenshots/home-night.png)

*Desktop renders of the current launcher at 1920×1080, using sample recent-app history.
They are not photos of a TV. App availability varies by TV.*

## Get started

### On a stock TV (not available yet)

The current [Helper preview](helper/README.md) can **Check TV**; its install, cleanup
and restore actions are not enabled yet. Windows Helper and TV-browser setup routes
are planned. See the
[install options](docs/INSTALL-OPTIONS.md) and
[consumer install design](docs/CONSUMER-INSTALL-DESIGN-2026-10-27.md).

On the reference TV, Home, the remote, native app launch, Settings, the input picker,
Store opening and unattended root recovery were tested.

### On a TV already running QuietTV (optional AI/MCP control)

The [public-llm MCP package](public-llm/README.md) works with an **already installed**
QuietTV control service and the owner's key. It covers a local venv install, guided owner
configuration, doctor, client configuration, finite remote/app controls and fresh
screenshots, plus optional Wake-on-LAN followed by identity/status confirmation. It is
**not** a stock-TV installer. Use the actual commands in that README.

## AI setup prompt (optional)

Use a local assistant with PC terminal or MCP access. A chat without those tools cannot
operate your TV. Claude Opus 4.8 may help, but its complete setup path is unverified and
it can refuse device/root work. Other models can refuse too. AI remains optional.

Copy this into a local assistant that can use your PC terminal and MCP client. The
expanded checklist is [docs/AI-SETUP-PROMPT.md](docs/AI-SETUP-PROMPT.md).

```text
Help me set up QuietTV on my own TV: https://github.com/BDAN13LS/QuietTV
Use local PC terminal/MCP tools. Clone the repo if needed, then read its README,
docs/AI-SETUP-PROMPT.md and local instructions before making changes.
First check whether the TV is stock or already running QuietTV. Confirm its actual
model and firmware; never guess compatibility or treat Unknown as a match.
Check which components are published. Explain missing steps; do not invent an
installer, download, root method or restore capability.
For an existing QuietTV service, follow public-llm/README.md: local environment,
guided setup, doctor, client configuration, then status and controls. If wake is
configured, send Wake-on-LAN and confirm identity/status afterward.
Keep keys, certificates, credentials, screenshots and backups private and outside
Git. Use local files/hidden prompts; never ask me to paste secrets into chat.
Before persistent root, startup or removal changes, require an off-TV backup with
verified readback and a tested recovery path. Stop dependent steps if either is
missing; continue safe PC preparation. Do not flash firmware or use raw reboots.
Verify results with status/captures. Distinguish observed from untested outcomes.
```

## Known limits

- Store app-update transactions, DRM playback and physical HDMI video are **not established**.
- IPTV picture is still black, and the cold-boot black gap is unresolved. Neither is claimed fixed.
- One package capture is not an end-to-end restorable backup, root transport or self-installer.

## Development and evidence

The [package reader](tools/tv-capture/README.md) captured a real package and its
metadata on the supported TV: 14 matching entries, 36 observed security attributes,
and matching TV/PC hashes. Restoration was not performed.

See [capture evidence](public-toolkit/docs/D2-CAPTURE.md),
[Rescue](rescue/README.md), the [launcher host](launcher/host/README.md) and the
[installation plan](docs/CONSUMER-INSTALL-DESIGN-2026-10-27.md) for technical progress.

## Contributing

Fork, open a pull request, let CI run, and wait for review. Hardware claims need evidence
at the exact commit. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Licence

The original QuietTV code is released under the [MIT licence](LICENSE).
Third-party components keep their own licences, listed in
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md). The grant does not cover Samsung
firmware or apps, third-party brand artwork, or unpublished source such as the
IPTV player. See [public-llm/docs/LICENSING.md](public-llm/docs/LICENSING.md).

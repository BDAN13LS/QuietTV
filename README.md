<!-- quiettv:public -->

# QuietTV

**A replacement launcher for Samsung TVs, built for privacy and speed.**
QuietTV replaces your Samsung TV's Home screen with a fast, remote-first
launcher. It also lets you choose optional Samsung tracking, ads, TV Plus,
Bixby and bloat to remove. The planned installer needs no AI, subscription or coding. It uses
owner-authorized root access for your exact model and firmware. Picture,
sound, inputs, Settings, security, DRM, the app store and updates stay in
place. Restoring what QuietTV changes is planned and is a required release
gate. There is no self-installer for consumers today.

![QuietTV home screen in day mode with scenic mountain artwork, app row and recent apps](docs/screenshots/home-day.png)

![QuietTV home screen in night mode with dark scenic artwork, app row and recent apps](docs/screenshots/home-night.png)

*Desktop renders of the current launcher at 1920×1080, using sample
recent-app history. They are not photos of a TV. App availability varies by
TV.*

## Compatibility

| TV model | Firmware | Status |
|---|---|---|
| Samsung UN43U8000FBXZA (ARMv7, Tizen 9) | 1300.8 | Developer preview, one TV tested |
| Anything else | — | **Not supported.** QuietTV refuses other models and firmware. |

## Current status: developer preview

- **Not ready for consumers yet.** There is no installer you can download
  today.
- On the one test TV: the launcher (2.0.18) is installed and
  opens native apps, the input picker, picture and sound settings, and the
  Store. Three optional payloads have been removed there: Guide, the
  informational Security Center screen, and TV Plus.
- **Not verified yet:** Store app updates actually installing, DRM playback
  and HDMI video. IPTV picture is currently black, so IPTV will be
  hidden or marked unavailable until fixed. A black gap at startup is
  still unsolved. Memory savings have not been measured, and removing all
  Samsung tracking has not been proven.

## How installing will work (planned, not available yet)

1. Download **QuietTV Helper for Windows**. It checks its own signature, and
   you install no Python, coding tools or AI.
2. On the TV, use the remote to turn on **Developer Mode** and enter your
   PC's address. Developer Mode lets your PC talk to the TV. It does not
   change the TV by itself.
3. In the Helper, **back up** your TV to your PC, NAS or USB stick. Stock
   files are not removed and Home/startup is not changed until the backup
   has been read back and checked.
4. Choose root + launcher, then **Recommended cleanup** or items one by one.
   You see exactly what will change, then approve it.
5. Check Home, remote, Settings and inputs with your remote.

You will also need to sign in to your own Samsung account once, for a
certificate tied to your TV. Developer Mode and that certificate do not give
root access; root comes from a separate step built for your exact model and
firmware. Each owner gets their own keys, and nothing is shared.

**Type a URL into the TV browser?** That is the goal. The first planned
version uses the TV browser as a *screen* for the Helper on your PC, on the
same network. Whether a browser page can install root and QuietTV on stock
firmware by itself is **not yet proven or ruled out**, and we won't claim it
until it is shown working on a real TV. Opening a QuietTV page never changes
your TV by itself.

## Cleanup and restore (planned)

- Every item shows what it does, what depends on it, and whether it can be
  restored.
- Results are reported plainly: **removed**, **disabled**, **kept**,
  **held** (needed by something else, such as updates) or **failed**.
- **Restore individual** puts back one item. **Restore Samsung** is meant
  to undo QuietTV's changes, removing QuietTV itself last. It will only be
  offered once it has been proven on hardware.
- **Rescue** is a separate restore tool kept on your PC. It needs your home
  network to reach the TV, but not the internet, the QuietTV launcher or its installed control service. If the TV's firmware changes and Rescue's access method
  no longer works, Rescue tells you; it cannot promise to fix every failure.
- Removing something from the TV's own menu later needs a verified backup of
  that exact item, either already on your PC or reachable at that moment.
  Otherwise **Remove** is greyed out and tells you why. The launcher itself
  works without your PC switched on.
- Holding firmware auto-updates is a separate choice you opt into, with the
  trade-off explained. App updates stay on.

## Backups and privacy

Your backup stays **off the TV**, on storage you control (planned: encrypted
with your own passphrase). QuietTV never uploads your backup, account
details, certificates or serial numbers. We publish our own source, builds,
model profiles and checksums. We do not host Samsung firmware or Samsung
apps. Generic, exact-version components might be published later, but only
with verified permission, a clean scan and proven compatibility. A link to
Samsung's official firmware is not a restore guarantee.

## FAQ

**Is this a factory reset?** No. Factory reset, Reset Smart Hub, Samsung's
USB firmware update and QuietTV restore are different things. Samsung notes
that a full factory reset changes the TV's device ID, and nothing promises it
brings back removed files. QuietTV restore puts back what QuietTV changed,
from your own backup.

**Can I go back to older Samsung firmware?** [Samsung says firmware downgrade is not supported](https://developer.samsung.com/smarttv/develop/faq/device-information-and-firmware.html). QuietTV does not flash firmware.

**Does it stop all tracking?** We report which known Samsung services we
removed or turned off. That does not prove zero telemetry, and it does not
cover streaming apps you install.

**Will it make my TV faster?** We will publish measured storage and memory
numbers, with the method used. Until then we make no claim.

**My TV is a different model.** Not supported. QuietTV refuses rather than
guesses.

**What if my backup was made after other changes?** It is labelled a
"current-state" backup. It cannot restore anything removed before it was
taken.

## Installation roadmap

Read the [consumer install design](docs/CONSUMER-INSTALL-DESIGN-2026-10-27.md)
and the [phased implementation plan](docs/superpowers/plans/2026-10-27-consumer-install.md).
These describe planned work, not an installer available today.

Implementation is underway. A [TV browser setup-page test](public-toolkit/docs/D0-BROWSER-PROBE.md)
passed on the test TV. The [offline engine](public-toolkit/docs/D1-ENGINE.md)
and [backup readback gate](public-toolkit/docs/D2-READBACK.md) have automated
tests. A separate [Rescue engine](rescue/README.md) is also tested offline.
These foundations do not yet install or restore a consumer TV.

## Contributing

Fork, open a pull request, let the offline checks run, and wait for review.
Hardware claims need evidence at the exact commit. See
[public-llm/community/CONTRIBUTING.md](public-llm/community/CONTRIBUTING.md).

## Licence

No repository-wide licence has been chosen yet. Component licences are in
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).

## For developers using AI assistants (optional)

You never need AI for install, use or restore. If you want to drive a test
TV from an AI client, see the optional MCP package in
[public-llm/README.md](public-llm/README.md). Models may refuse device
work; the plain command line is always available.

<!-- quiettv:public -->

# QuietTV

A remote-first alternative launcher for Samsung Tizen TVs. QuietTV keeps the TV feeling like a TV: big artwork, clear focus, fast remote navigation, and the native apps you already use.

[github.com/BDAN13LS/QuietTV](https://github.com/BDAN13LS/QuietTV)

![QuietTV home screen in day mode with scenic mountain artwork, app row and recent apps](docs/screenshots/home-day.png)

![QuietTV home screen in night mode with dark scenic artwork, app row and recent apps](docs/screenshots/home-night.png)

*Desktop renders of the current launcher at 1920×1080, using sample recent-app history. App availability varies by TV.*

These are renders of the actual app, not hardware captures. They show layout and design; they do not prove that every feature works on a TV.

## Developer preview

This is an early developer preview, not a universal root or firmware installer, and not a ready-to-use package for arbitrary TVs. The current private build is **2.0.18**, warm-installed on one TV.

Exact test hardware:

- Samsung **UN43U8000FBXZA**
- firmware **1300.8**
- **ARMv7**, **Tizen 9**

No broader compatibility is claimed.

## What it does

- **Scenic or simple home screen** with day, dusk and night art, or plain colours.
- **Optional weather**, configured by the device owner.
- **Recent apps** from local launch history, not a provider's continue-watching feed.
- **Native app launches** with Home, input picker, picture/sound settings and the Samsung Store retained.
- **Preserved native core** so settings, input, audio, network, update and security dependencies still work. Cleanup is limited to verified optional payloads, not a blanket removal of Samsung software.

## Status and known limits

Verified on the tested TV: Store catalogue/open and Home. Actual Store app update installs, DRM playback and HDMI video are **not verified**.

- **IPTV picture is currently black on the tested TV** even though decoding metadata is present. This is an open bug; playback is not claimed to work.
- **Startup black gap:** not solved. No boot-speed claims are made.
- App availability varies by TV.

## Install packages

The public LLM install package lives in **[public-llm/README.md](public-llm/README.md)**. It exposes read-only status plus allowlisted controls and fresh screenshots, with no arbitrary shell access. Private owner configuration and signing credentials are not shipped.

The toolkit in **[public-toolkit/README.md](public-toolkit/README.md)** validates fetched backup capsules with a pinned hash and device binding, and builds offline restore plans. Its live transport adapter is **not implemented**, so automatic setup and restore **APPLY refuses and changes nothing**.

This preview is not a ready-to-run universal root or firmware installer. Read the package documentation and understand the risks before touching a TV you own.

## Backups and privacy

Keep backups of your own device **off the TV**, on your own PC, NAS or private storage. Do not fetch someone else's Samsung payloads. This repository does not ship stock images, keys or tokens.

## LLM-assisted work

Claude may refuse privileged device work, and provider policies vary. The older 4.6 model was not tested, and no model is guaranteed to help. Nothing here is intended to circumvent model or provider safeguards. Human CLI and offline tools are available without an LLM.

## Contributing

Fork the repository, open a pull request, let the GitHub-hosted offline checks run, then wait for maintainer review. Hardware changes need tested evidence at the exact commit SHA. Do not run untrusted pull-request code on your personal TV or PC.

Start with **[public-llm/community/CONTRIBUTING.md](public-llm/community/CONTRIBUTING.md)**. Main requires passing offline/privacy checks and code-owner review before contributor changes merge. The sole maintainer retains an administrator override.

## Licence

There is no repository-wide open-source licence yet. Licence selection and third-party provenance are pending, so do not assume the whole repository is under one licence. Component licences are recorded in **[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md)**.

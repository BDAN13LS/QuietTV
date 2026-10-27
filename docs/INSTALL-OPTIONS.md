# Install options and upstream ideas

**Research and design note, not a completed integration.** Nothing here
installs anything today. QuietTV has no consumer installer, and this page
does not choose a repository licence (still pending). It covers public
code and documents only; no TV was involved. No issues or pull requests have
been opened with any project named here, and no contact has been made.

## What Samsung requires

Per [Samsung's certificate guide](https://developer.samsung.com/smarttv/develop/tools/additional-tools/vscode/creating-certificates.html):

- the app must be a **signed, normal packaged app**;
- a signing profile contains an **author certificate and distributor certificate**;
- you must log in with a **Samsung developer account**;
- the profile is tied to the **target TV's DUID** (device ID).

This covers a normal WGT app, not arbitrary native root files. A certificate
**does not grant root**, and there are **no shared owner keys**: each owner
gets their own. QuietTV's current WGT route uses this mechanism, so it cannot
promise an account-free consumer install.

## Easiest exact-TV plan

1. **Windows Helper** with Developer Mode on the TV, plus **one browser
   Samsung login**.
2. Automated **per-owner TV certificate**, or reuse of the owner's existing one.
3. A **verified off-TV backup**.
4. The **exact-profile installer** for that model and firmware.

This is the simplest near-term route. Installing from a stock TV with only a
URL is **unproven**; see the [README](../README.md) for why we make no claim.

## Projects worth borrowing from

Nothing below is assumed to interoperate with QuietTV or with our firmware.

| Project | What it is | What to take |
|---|---|---|
| [Apps2Samsung](https://github.com/Apps2Samsung/Apps2Samsung) (MIT; the older [Samsung-Jellyfin-Installer](https://github.com/Jellyfin2Samsung/Samsung-Jellyfin-Installer) address redirects to it) | Its README offers a cross-platform GUI and a self-contained CLI, automated signing and installs, reuse of the owner's TV certificate, a first-install Samsung login, your own WGTs/TPKs and a catalogue | **Strongest candidate to reuse or integrate** for generic device, sign and install plumbing, after code, licence and compatibility review |
| [quiet-tizen](https://github.com/jss8649/quiet-tizen) | Closest Tizen/native cleanup. Tested on UN75U8000FFXZA, firmware 1300.9, **not** our UN43U8000FBXZA on 1300.8. Native Home/startup and selected service flags; its computer re-acquires root for maintenance | Field-specific restores and compatibility evidence; QuietTV keeps its own interface |
| [cleanyourtv](https://github.com/Gathin23/cleanyourtv) | Explicitly does **not** support Samsung Tizen; Android/Google TV over ADB | Model profiles, undo and preflight UX. Not its Android commands |
| [dumbtv](https://github.com/humblemuzzu/dumbtv) | Fire TV / Android ADB ad and clutter cleanup | Ideas only, no Samsung implementation |
| [TizenBrew](https://github.com/reisxd/TizenBrew) | Module and app host | Its capabilities are **not verified** against QuietTV's installer or our firmware; we do not assume the same ones |

## Keep as ours

- QuietTV's own **launcher design** and **device cleanup**.
- The exact-model profile, verified-backup and removal gates stay QuietTV's,
  even if generic plumbing is shared.
- **No automatic root flag**: root is never switched on as a side effect.

## Contribution recommendation

- Explore general device, certificate and CLI fixes **upstream in Apps2Samsung**.
- Offer sanitized exact-model and recoverability findings to **quiet-tizen**
  (or the root repository it builds on).

Both are suggestions only; nothing has been sent.

An eventual [Apps2Samsung catalogue entry](https://github.com/Apps2Samsung/tizen-community-packages/blob/main/packages/README.md)
could make a released launcher easier to find and install. Listing a WGT does
not install QuietTV's separate root, cleanup or recovery components. Those
still need their own verified setup and restore path.

Its separate [TV-Store](https://github.com/Apps2Samsung/TV-Store) already needs
a phone or PC to install and arm it. It is not evidence of installation from
a stock TV's browser URL alone. Its documented setup changes Developer Mode's
host setting; QuietTV must not copy that step without testing recovery.

## AI and Wake-on-LAN

AI is optional. A plain GUI/CLI comes first. The public MCP package is already
a controller for an installed TV; it is not a stock-root bootstrap. A
Wake-on-LAN feature is **planned, pending merge from another lane, and not
working yet**.

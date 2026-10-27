# QuietTV preview widget (D5b slice, offline only)

An **unsigned, preview-only** Tizen web app that wraps a built QuietTV frontend. It is a normal app:
not registered as Home, no on-boot start, no service, no root, no signing. Nothing here has been
installed on a TV.

## Files

| file | what it is |
|---|---|
| `quiettv-bridge.js` | Frontend globals → documented Tizen web APIs. Written fresh for this contract. |
| `tv-inputs.js` | Byte-identical copy of `main:boot/widget/tv-inputs.js` (SHA-256 `261f8e2b824e3eec5adda91bac2d5aa3a88ea0d83cfaf124042464832e207fca`, LF). It was parent-scanned as safe. `tests/test_widget_bridge.py` re-checks the hash. |
| `config.xml.template` | Owner app and package ids are filled in and XML-escaped. There is no author, address or secret. The only privileges are `application.launch`, `application.info` and `tv.window`. `internet`, plus an `<access>` entry for `127.0.0.1:<port>`, is added only when the backend is enabled. |

## Flow

1. Owner storage: `python helper/owner_init.py` (see `--help`) creates a new folder.
   - Inputs: an exact built-in profile, device facts and an installed-app inventory
     (`[{Id,Name,Installed,Disabled,Launchable}]`).
   - Output: `host-config.json`, `apps.json`, `settings.json`, `widget-config.json` and
     `owner-state.json`, plus `backend-token` with `--backend-port` only.
   - The folder must be outside every git checkout or worktree, links included. It must not already
     exist. It is owner-only: POSIX 0700, or a Windows ACL with inheritance removed.
   - Only the device-id **hash** is stored. The CLI prints paths and that hash only.
   - `consumerApplyEligible` is always `false`.
2. Package: `python helper/build_widget.py --dist <frontend dist> --owner-dir <dir> --out <new>.wgt [--stage <new dir>]`.
   - It builds an exact positive inventory: html/js/css/png/svg/jpg/webp/woff only, safe names,
     and every html/css reference resolved inside the dist.
   - It refuses links, source maps, keys, certificates, signatures, binaries and project roots.
   - It injects `tv-inputs.js`, then `quiettv-widget-config.js`, then `quiettv-bridge.js` before
     the frontend bundle, and writes `quiettv-manifest.json` with SHA-256 for every file.
   - The ZIP is deterministic: sorted entries, fixed time and attributes, stored (uncompressed, so
     no zlib-version drift) and LF widget sources. Same inputs give the same bytes.
   - With the backend enabled, the WGT holds the token and is made owner-only (ACL or 0600).

## Bridge contract

- Globals the generated `quiettv-widget-config.js` **sets**, before the frontend bundle, from one
  validated owner list:
  - `QuietTVApps = [{Id, Name, Icon:''}]`: frozen; replaces the compiled catalogue in the
    frontend's `runtime-catalog.mjs`.
  - `QuietTVConfig = {schema:'quiettv-widget/1', preview:true, iptvEnabled:false}`: fail-closed
    owner mode. A missing or malformed list gives no apps plus a setup error, never the compiled list.
  - `QuietTVWidgetConfig`: the bridge's own config. It holds the token only when the backend is enabled.
  - The bridge refuses to start unless `QuietTVApps` lists exactly its catalogue ids, in the same
    order, and `QuietTVConfig` is preview with IPTV off.
  - Owner ids must also pass the runtime rule: an alphanumeric first character and at most 128
    characters.
- Globals the bridge **sets**:
  - `QuietTVGetInputs()`: a promise of `[{type, number, connected}]`.
  - `QuietTVSelectInput({type, number})`.
  - `QuietTVBridge`: `{drain, launch, home, close, status, reportUiState, stop}`.
- Globals it **calls**: `QuietTVResetHome`, `QuietTVSetMessage`, `QuietTVState`, and drains the
  *current* `QuietTVOutbox` every 100 ms.
- Outbox actions:
  - `{type:'launch',id}`: allowed only for owner catalogue ids and Settings/Store. `quiet.iptv` is
    refused with a message; there is no IPTV support.
  - `{type:'home'}`: resets our own UI.
  - `{type:'close'}`: calls `getCurrentApplication().exit()`.
  - Any other action is ignored.
- Back (461/10009/27/8) is intercepted **only** while a TV input is showing. It hides the input and
  resets our Home. Otherwise the frontend's own Back handling runs. No keys are registered.
- Becoming visible again resets our Home.
- **Backend disabled (default):** no request of any kind; launches go through
  `tizen.application.launch`.
- **Backend enabled (future service; no service code exists yet):**
  - The only endpoint is `http://127.0.0.1:<port>`. Ports 4190 and 4196 are refused.
  - Every request carries an `X-QuietTV-Token` header.
  - The only routes are `GET /status`, `POST /home`, `POST /launch/<allowlisted id>` and
    `POST /ui-state`.

## Limits

- Tiles come from `QuietTVApps` only with a frontend that includes `runtime-catalog.mjs`. An older
  dist, such as public b039885, ignores these globals and draws its compiled catalogue and IPTV tile;
  the bridge still refuses to launch anything outside the owner list, and IPTV.
- The bridge proves only that **it** makes no requests. The frontend has its own:
  - The native splash hand-off (`127.0.0.1:4196`): a current frontend skips it entirely in owner
    preview. It passes `nativeEnabled:false` when `QuietTVConfig.preview` is true, so there is no
    fetch and no retry. The legacy registered launcher (no `QuietTVConfig`) keeps
    `nativeEnabled:true` and still signals 4196. An older dist without this change still probes it.
  - Weather from open-meteo, only when the owner turns it on. Without the `internet` privilege it
    is expected to fail on the TV; that is unverified.
- Warm hardware check on the supported model/firmware: the sanitized frontend was built, packaged,
  privately signed and installed under a separate preview identity. It rendered 14 selected installed
  apps; Right moved focus and Enter on YouTube opened YouTube. Physical Home returned to the existing
  launcher. The preview was uninstalled and its temporary upload removed; the original launcher,
  recovery and startup-file hashes were unchanged. No reboot occurred. Owner keys and signed packages
  are not distributed.
- Still unverified: this privilege set for `tv.window` input switching, Back and return visibility,
  stock first installation and startup. There is no consumer install and no root proof.
- Windows ACL hardening uses `icacls`. Elsewhere, POSIX mode bits are used.

Tests: `python -B -m unittest tests.test_owner_init tests.test_widget_build tests.test_widget_bridge`.
- The bridge tests need `node`.
- The resolver integration test reads `frontend/src/runtime-catalog.mjs`. It is skipped, and says
  so, when that file is absent (a toolkit export without `--with-frontend`).
  `QUIETTV_RUNTIME_CATALOG` may point at another checkout for private checks.
- The real-dist smoke test uses `frontend/dist`, or `QUIETTV_FRONTEND_DIST`, and is skipped when
  neither has been built.
- `ExportSmokeTests` exports these 9 files to a fresh temp folder and runs the three suites there.

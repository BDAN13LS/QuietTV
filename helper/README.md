# QuietTV Helper (developer preview)

A small local web page for the QuietTV owner flow, served by Python's standard library on
`127.0.0.1` only. Plan: D6 in `docs/superpowers/plans/2026-10-27-consumer-install.md`.

**What works now:** *Check TV*. You type your TV's home-network address and QuietTV reads
the TV's public information page (`http://<address>:8001/api/v2/`). It shows, separately:

- whether a Samsung TV answered;
- its model, and whether that model is on the supported list;
- whether Developer Mode is reported on;
- a fingerprint (a hash) of the TV's id. The raw id is never shown, stored or logged;
- which exact facts are **not** confirmed. The information page does not give the firmware
  version, build, region, processor type or Tizen version, so an exact match is never
  claimed and the TV is always shown as **not ready to install**.

**What does not work yet, and says so:** *Back up*, *Launcher*, *Cleanup* and *Restore*
show the steps they will take and each missing requirement, with the button disabled. The
server refuses these actions even if a request is forged, and makes no TV call. *Report*
gives a plain summary you can copy. Nothing in this preview changes a TV.

## Run it

```
python -B helper/server.py
```

This opens your default browser at a private link on `127.0.0.1`. To start without a
browser window:

```
python -B helper/server.py --no-browser --launch-file <OWNER_STORAGE>\quiettv-link.txt
```

The private link is written to that new file, which only you can read. The file must not
already exist and must be outside every git checkout. It is deleted when the Helper stops
normally (Ctrl+C); a forced kill leaves it behind, so delete it yourself in that case.
Without `--launch-file`, `--no-browser` gives no way in, because the link is never printed.

Packaging target: the python.org embeddable runtime. No npm, Electron or global tools.

## Security contract

| Rule | Where |
|---|---|
| Binds `127.0.0.1`, a random free port, exclusive on Windows | `server.py` `_LoopbackServer` |
| Exactly one `Host`, equal to `127.0.0.1:<port>`, on every request | `_check_host_and_path` |
| API `Origin` exactly `http://127.0.0.1:<port>`: required on POST, checked on GET if sent | `_check_api` |
| Fresh 256-bit token per process, only in the `X-QuietTV-Session` header, `compare_digest` | `_check_api` |
| The token reaches the page in the URL fragment, which the page removes at once | `ui/app.js` `takeToken` |
| Any query string refused; fixed route table; no file, shell, proxy or upload route | `do_GET`, `do_POST` |
| POST body: at most 4 KiB, `application/json`, strict JSON (no duplicate keys or NaN), exact keys | `_read_body` |
| No CORS headers; `no-store`, `nosniff`, `no-referrer`, CSP without inline script or style | `SECURITY_HEADERS` |
| No request logging; the token is never printed | `log_message`, `main` |
| TV address: an explicit `10/8`, `172.16/12` or `192.168/16` address only; no names or scans | `session.validate_target` |
| TV read: one plain socket, fixed port 8001 and path `/api/v2/`; no proxy, no redirect, at most 16 KiB of headers and 64 KiB of body; one hard 6 s deadline for connect, request, headers and body together | `session.HttpInfoReader` |
| One check at a time, locked to one address; no address change while a check runs | `session.Session` |
| Cancel stops the read itself within 0.2 s and closes its socket; stopping the Helper joins the reader thread | `Session.cancel`, `Session.shutdown` |
| A check whose worker thread cannot start is marked failed at once, with no TV call, and the next check works | `Session.start_check` |
| Nothing contacts the TV until *Check TV* is pressed | `ui/app.js`, tests |

## The engine seam

`Session(engine=...)` takes the future install/backup engine. The preview ships
`session.Engine`, which has no capabilities. Even an engine that claims every capability
is still refused here: the exact-facts and preview gates stay closed, and the engine is
never called. `tvkit.transport.TRANSPORTS` stays empty.

## Not included, on purpose

- **Offline backup check (capsule readback).** `tvkit.backup.readback_gate` needs the
  exact device facts this network check cannot read, and a way to choose a backup folder.
  That would mean a file-reading route from the browser, which this slice must not add.
- **Finding this PC's address** for the Developer Mode step. The usual trick contacts an
  internet address; the page asks you to enter it instead.

## Tests

```
python -B -m unittest tests.test_helper_session tests.test_helper_server
node --test tests/test_helper_ui.mjs
```

The automated TV answers are synthetic and served from `127.0.0.1`. No automated test
contacts a real TV. A separate maintainer browser check on the supported Samsung model
also passed: the real information page parsed, the model and Developer Mode were
recognised, only the device-id hash was returned, and all change actions stayed unavailable.
The browser removed the session token from its address bar and showed "TV check complete."
This proves the read-only setup check, not an installation or recovery.

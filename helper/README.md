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

**Saved-report preflight (advanced, optional, owner-configured at startup).** If, and only
if, you start the Helper with `--preflight-config <FILE>`, a *Saved report preflight* card
appears on the Check screen. It checks four **saved local files you name at startup** — a
ProductInfo report, a native inspection frame, a trusted join policy and a profile — through
the existing offline `tvkit.factsjoin` library, and shows the seven exact device facts it
derived, each with its source label and clear SAVED REPORTS provenance. See *Saved-report
preflight* below. This is **not** a live connection, **not** verified support and **not**
installation eligibility; it enables nothing and every change action still refuses.

**What does not work yet, and says so:** *Back up*, *Launcher*, *Cleanup* and *Restore*
show the steps they will take and each missing requirement, with the button disabled. The
server refuses these actions even if a request is forged, and makes no TV call. A positive
saved-report preflight does not change that. *Report* gives a plain summary you can copy.
Nothing in this preview changes a TV.

## Run it

```
python -B helper/server.py
```

This opens your default browser at a private link on `127.0.0.1`. To start without a
browser window:

```
python -B helper/server.py --no-browser --launch-file <OWNER_STORAGE>\quiettv-link.txt
```

To offer the advanced saved-report preflight as well:

```
python -B helper/server.py --preflight-config <OWNER_STORAGE>\quiettv-preflight.json
```

## Saved-report preflight (advanced)

**Interface, and why it is shaped this way.** The preflight sources are named **once, at
startup, in a single strict-JSON config file** given with `--preflight-config`. No HTTP route
ever accepts a file path, a URL, a policy or a profile, or a device identity: the browser can
only press a body-less *Check saved reports* button (`POST /api/preflight` with `{}`), which
runs the check against the already-configured sources. This keeps the browser surface exactly
as small as the live check's — nothing the page sends chooses what is read. A new owner
config file (not an HTTP field) is the only way to point the preflight at different files, by
design: it is the same "owner-configured, never web-configured" rule the rest of the Helper
follows.

The config file (`quiettv-helper-preflight/1`) names four local files and records the
operator's explicit trust and pairing assertions:

```json
{
  "schema": "quiettv-helper-preflight/1",
  "productInfoReport": "<OWNER_STORAGE>\\productinfo.json",
  "nativeInspectionFrame": "<OWNER_STORAGE>\\inspection.frame",
  "joinPolicy": "<OWNER_STORAGE>\\policy.json",
  "profile": "<OWNER_STORAGE>\\profile2.json",
  "operatorAssertions": {
    "trustPolicyAndProfile": true,
    "pairedFromSameTv": true
  }
}
```

Both `operatorAssertions` must be `true` or the Helper refuses to start: `factsjoin` checks
that the saved values agree with the trusted policy, but it cannot authenticate that you
trust that policy, or that the two reports came from one TV. Those are **your** assertions,
shown on screen as assertions, never as proof.

Each of the four paths must be an **absolute local regular file** — no `..`, no symlink,
junction or other reparse point (in the file **or any parent folder**), no UNC path, no
mapped network drive and no URL. OneDrive and other placeholder folders are reparse points
and are refused. The four roles must name four distinct files. Reads are bounded (report,
policy and profile at 256 KiB each; the inspection frame at 4 MiB, then the inspection
decoder's own limits); anything larger, missing, malformed, mismatched or drifted is reported
with a short fixed reason. The Helper never stores or logs the report bytes, the device
identity, the receipt or any file path, and a refusal never echoes a source value.

Pressing *Check saved reports* shows only the display-safe projection from
`factsjoin.preflight_display`: the seven `tvkit-profile/2` fields (`model`, `firmware`,
`arch`, `osName`, `osVersion`, `firmwareBuild`, `region`) with their fixed source labels, the
profile id, the identity scheme name (never the digest or the opaque device-id token), and a
fixed note. It carries no capability, no enabled action and no device-id token. Changing the
TV address or starting a live TV check clears any shown preflight result, so a saved-report
match is never mistaken for live reachability.

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
| Preflight sources named only at startup in one strict-JSON config; no HTTP route takes a path, URL, policy, profile or identity; the button posts `{}` only | `--preflight-config`, `/api/preflight` |
| Preflight files: absolute local regular files only; no symlink/junction/reparse in the file or any parent; no UNC, mapped drive or URL; distinct files; bounded reads | `session.load_preflight_config`, `session._read_local_file` |
| Preflight shows only the `factsjoin.preflight_display` safe result; no report bytes, identity, receipt, token, digest or path in state or logs; refusals use fixed reasons | `session.Session.run_preflight` |
| A positive preflight enables nothing: every change action still refuses, and it never marks the live target verified or overrides network facts | `session.Session`, tests |
| Changing the TV address or starting a live check clears a stale preflight result | `Session.set_target`, `Session.start_check` |
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
python -B -m unittest tests.test_helper_session tests.test_helper_server tests.test_helper_preflight
node --test tests/test_helper_ui.mjs
```

`tests/test_helper_preflight.py` exercises the saved-report preflight end to end through the
real loopback server with synthetic report, frame, policy and profile files (no core loader,
checker or `factsjoin`/`preflight_display` is patched to succeed), and covers the refusals:
malformed or ambiguous config, non-local URL/UNC paths, symlink/reparse sources, oversize
files, firmware/model drift, a failed native run, that no source value, path or identity
leaks into HTTP or stdio even on error, that a positive preflight enables no action, that the
CSRF protections hold on `/api/preflight`, and that a target change or a live check
invalidates a shown preflight result.

The automated TV answers are synthetic and served from `127.0.0.1`. No automated test
contacts a real TV. A separate maintainer browser check on the supported Samsung model
also passed: the real information page parsed, the model and Developer Mode were
recognised, only the device-id hash was returned, and all change actions stayed unavailable.
The browser removed the session token from its address bar and showed "TV check complete."
This proves the read-only setup check, not an installation or recovery.

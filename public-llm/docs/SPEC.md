# Interface specification

What `quiettv-mcp` sends, what it expects back, and what it refuses. The TV-side service is the existing
root-backed HTTP service on port 4190; nothing here adds a route to it except `POST /capture`, which the
service must implement for the capture tool to work.

## Order of every control call

1. **Read the key file** named by `control_key_file` (local). Missing, symlinked, non-ASCII, or not exactly one
   16–256 character printable token: refuse. **Nothing is sent.**
2. **Same-LAN check** (local). A UDP `connect()` (sends no packet) finds this machine's source address; the TV IP
   must be inside `<source>/<lan_prefix>` (default /24). Refuse otherwise.
3. **Identity check**: `GET http://<tv_ip>:8001/api/v2/` with **no key**. `device.modelName` must equal the
   profile model; `device.wifiMac` must equal the configured MAC exactly (case-insensitive, `-` or `:`);
   `device.firmwareVersion`, when reported and not `Unknown`, must match the profile, else refuse (when absent or
   `Unknown`, results carry `firmware: "unverified"`); `device.OS`, when reported, must be `Tizen`; `device.ip`,
   when reported, must equal the configured IP. Any mismatch: refuse, **no request to 4190**.
4. **One request** to `http://<tv_ip>:4190/<route>` with header `X-QuietTV-Key: <key>`.

`tv_wake` is the one exception (below): it uses no key and makes no HTTP request, because a TV in standby does
not answer step 3.

The target is always the literal IPv4 from the owner config (private range; no host names, so no DNS). Requests
use `http.client` directly: proxies from the environment are ignored and redirects are never followed. Response
bodies are read with a hard cap (JSON 256 KiB, capture 8 MiB).

## Routes used

| Tool | Method and route | Expected response | Returned to the model |
|---|---|---|---|
| `tv_status` | `GET /status` | 200 JSON | whitelisted fields only (below) |
| `tv_home` | `POST /key/63` (the profile's `home` key) | 200 `{exit_code, stdout}` | `accepted` only on exit 0 (otherwise an error), `foreground_verified: false`; never stdout |
| `tv_remote_key` | `POST /key/<code>` | 200 `{exit_code, stdout}` | key name, code, `injected` (exit 0), exit code; never stdout |
| `tv_launch_app` | `POST /launch/<appId>` | 200 `{accepted: true}` | app id, accepted |
| `tv_diagnostics` | `POST /diagnostics` | 200 `{exit_code, stdout}` | redacted summary (below) |
| `tv_capture` | `POST /capture` | 200 PNG (contract below) | `ImageContent` PNG + `TextContent` metadata |
| `tv_capabilities` | none by default; `probe_device: true` runs the `tv_status` path | | profile, tools, keys, apps, capture, wake and maintenance availability |
| `tv_wake` | none: one or two UDP magic packets (see Wake-on-LAN) | nothing is read back | `requested`, `packets_sent`, `port`, `awake_verified: false`, `identity_checked: false`; never the MAC or address |

Key codes (Linux input codes accepted by the TV's RemoteProbe): `back` 1, `ok` 28, `home` 63, `volume_down` 67,
`volume_up` 68, `up` 103, `left` 105, `right` 106, `down` 108.

App IDs: the 17 IDs in `profiles.APP_IDS`, intersected with the owner's optional `allowed_apps`.

**Never used:** `maintenance`, `local-developer`, `reboot`, `set-home`, `restore-home`, `restart-dashboard`,
`snapshot`, `player-key/*`, `ui-state`, `GET /screenshot` (it serves the last file written, possibly stale), and
`POST /home` (on the real TV it was accepted but left another app, HBO Max, in the foreground; key 63 brought the
registered Home forward, 2026-10-27). The server has no code path to them; tests assert none is requested while the
other tools run.

`tv_home` reports that the key was injected, not that Home is showing. This server does not read or report the
foreground app (the status whitelist omits it); only a fresh `tv_capture` confirms Home.

HTTP 403 is reported as "wrong control key, or this PC's address is not on the TV service's controller
allowlist". The service only accepts loopback and its configured controller address.

## Wake-on-LAN (`tv_wake`, owner opt-in)

Registered only when the owner config has `"wake": {"enabled": true, ...}`; otherwise the tool does not exist and
`run wake` refuses with "Wake-on-LAN is off". Order of a wake call:

1. **Cooldown** (local): a wake less than 5 s after the previous one in this process is refused. Nothing is sent.
2. **Same-LAN check** (local, sends nothing), as for control calls.
3. **Send** `wake.packets` (1 or 2) UDP datagrams with `SO_BROADCAST` to `(<broadcast>, wake.port)`, where the
   payload is the standard 102-byte magic packet: `FF` x 6, then the 6 bytes of `tv.mac` x 16 (no SecureOn
   password).

`<broadcast>` is derived, never configured: the broadcast address of `tv.ip`/`lan_prefix`. Config load rejects
wake unless that network lies inside 10/8, 172.16/12 or 192.168/16, and rejects a `tv.ip` that is itself the
network or broadcast address. `wake.port` must be 7 or 9 (default 9); `wake.packets` 1 or 2 (default 1); any other
key in `wake` (a target host, MAC or URL) is rejected. The MAC is `tv.mac`, already validated as unicast, not
all-zero and not broadcast.

No key is read, no request goes to 8001 or 4190, and nothing is read back, so the result can only say the packet
was requested. Whether the TV wakes depends on its network power-on setting (for example Network Standby / Power
On with Mobile), on Wi-Fi versus Ethernet support, and on it being plugged in. Confirmation is a later
`tv_status`, which runs the full identity check. Not yet verified on hardware.

## Status whitelist

`ready`, `root.connected`, `platform.prepared`, `health.ready`, `health.exit`, `health.at`, `started`,
presence (not value) of `dashboardPid` / `viewerPid`, `ui.visible`, whether `player` is present, `error` (redacted,
120 characters), and `capabilities` (array of short strings, if the service adds one). Everything else, boot IDs
included, is withheld and counted in `fields_withheld`.

## Diagnostics summary

Plain lines are redacted (control-key headers, `Authorization`/`Cookie`, URL paths/queries/userinfo, `token=`-style
assignments, long hex/base64 blobs, IPv4 except loopback, MAC addresses, `C:\Users\<name>`, `/home/<name>`,
`/opt/usr/home/<name>`, `/Users/<name>`), capped at 40 lines of 160 characters. JSON documents in the output are
reduced to keys with boolean, number or null values; every string value becomes `[text withheld]`. The literal
key is also replaced wherever it appears.

## `POST /capture` contract (to be implemented by the TV service)

Request: `POST /capture`, headers `X-QuietTV-Key` and `X-QuietTV-Capture-Nonce: <32 hex chars>`.

Success response, all required:

| Item | Requirement |
|---|---|
| Status | `200` |
| `Content-Type` | `image/png` |
| `Cache-Control` | contains `no-store` |
| `X-QuietTV-Capture-Id` | `[A-Za-z0-9._-]{8,128}`, unique per capture |
| `X-QuietTV-Captured-At` | capture time, Unix epoch **milliseconds** (TV clock), integer |
| `X-QuietTV-Capture-Nonce` | *recommended*: echo the request nonce; if present it must match |
| Body | a complete PNG: signature, valid IHDR (CRC checked, 1–8192 px each side), ends with IEND; at most 8 MiB |

The capture must be taken **for this request**, never served from a previously written file.

Client-side freshness: the id must not have been seen before in this server process; the timestamp must be no more
than `capture_max_age_s` (default 120) old and no more than 5 s in the future by this machine's clock; a mismatched
echoed nonce is rejected. With the nonce echoed, results report `freshness: "nonce-bound"`, otherwise
`"clock-bound"`.

Unsupported: `404 {"error":"unknown action"}` (what the service returns today for unknown routes). The tool then
reports that capture is not available on this device. **There is no fallback to `GET /screenshot`.**
Not ready: `503`. Any other status, content type, missing header, stale timestamp, reused id, or malformed PNG is
an error; no image is returned.

**Live service as observed (2026-10-26, UI 2.0.18):** valid key gives 200 PNG with a 16-hex capture id, epoch-ms
`X-QuietTV-Captured-At`, compositor source and `no-store`; an invalid key gives 403. It does not echo the nonce
yet, so results are `clock-bound`. The service allows up to 20 MiB; a 1920x1080 capture is about 2 MiB, well
inside this client's 8 MiB cap.

Optional, recommended: include `"capabilities": ["status","home","key","launch","diagnostics","capture"]` in
`GET /status` so `tv_capabilities(probe_device=true)` and `doctor --online` can report capture support without
taking a picture.

**Limits.** The TV composites hardware video on a separate plane, so captures show the UI layer and a playing video
reads as black. Sound is not captured. A capture never proves video or audio works; check them on the TV.

Captures are written to disk only if the owner sets `capture_dir`, as new files with owner-only permissions. Nothing
is uploaded anywhere by this server.

## Owner acceptance run on the real TV

After the service implements `POST /capture`, on the controller PC (the address the service allowlists):

1. `quiettv-mcp setup`: answer the model, software version, IP and MAC from the TV's own menus, and give the path
   of the **existing private control key file** (do not retype or copy the key).
2. `quiettv-mcp doctor --online`. Pass: every line PASS (or WARN only for "firmware unverified" / capture not
   advertised), `identity: model and MAC match`.
3. Status and two captures over real MCP stdio, using the project venv's Python:

```python
import anyio, base64, json, sys
from mcp.client.client import Client
from mcp.client.stdio import StdioServerParameters

async def main():
    params = StdioServerParameters(command=sys.executable, args=["-m", "quiettv_mcp", "serve", "--config", sys.argv[1]])
    async with Client(params) as client:
        print((await client.call_tool("tv_status", {})).structured_content)
        for n in (1, 2):
            shot = await client.call_tool("tv_capture", {})
            assert not shot.is_error, shot.content[0].text
            png = next(c for c in shot.content if c.type == "image")
            meta = json.loads(next(c for c in shot.content if c.type == "text").text)
            open(f"acceptance-{n}.png", "wb").write(base64.b64decode(png.data))   # private output; do not commit
            print(meta["capture"]["capture_id"], meta["capture"]["freshness"], meta["identity"])

anyio.run(main)
```

Pass criteria: `identity.mac_verified` is `true`; the two capture ids differ; `freshness` is `nonce-bound` if the
service echoes the nonce (otherwise `clock-bound`); the PNGs show the current UI. A playing video area reads as
black: that is expected and says nothing about whether video or sound work, which must be checked on the TV.
A wrong configured MAC (edit the config temporarily) must make every tool fail with "no control request sent".

## Owner config (`quiettv-mcp-config/1`)

```json
{
  "schema": "quiettv-mcp-config/1",
  "profile": "UN43U8000FBXZA-1300.8",
  "tv": {"ip": "10.20.30.40", "mac": "02:00:00:00:00:01", "model": "UN43U8000FBXZA", "firmware": "1300.8"},
  "control_key_file": "/absolute/path/to/private/control.key",
  "lan_prefix": 24,
  "capture_dir": "/absolute/path/to/private/captures",
  "allowed_apps": ["quiet.iptv", "org.tizen.netflix-app"],
  "capture_max_age_s": 120,
  "wake": {"enabled": true, "port": 9, "packets": 1}
}
```

All of `schema`, `profile`, `tv.*` and `control_key_file` are mandatory and have no defaults; unknown keys are
rejected. `wake` is optional; absent or `"enabled": false` means Wake-on-LAN is off, and configs written before
it existed load unchanged. Location: `--config`, else `QUIETTV_MCP_CONFIG`, else `%APPDATA%\QuietTV-MCP\config.json` (Windows) or
`$XDG_CONFIG_HOME/quiettv-mcp/config.json`. The key itself is never in the config, the command line or the
environment.

## Supported profile

| Field | Value |
|---|---|
| Profile id | `UN43U8000FBXZA-1300.8` |
| Model | UN43U8000FBXZA |
| Software | 1300.8 (build prefix T-X22UDFAKUC; recorded full build `T-X22UDFAKUC-0090-1300.8`, matched whole string, case-insensitive) |
| OS / CPU | Tizen 9 / ARMv7 |

The CPU architecture cannot be read from the identity endpoint; it is owner-declared through the profile. Any other
model or firmware is rejected at config load and again at every call.

## Tools and annotations

| Tool | readOnly | destructive | idempotent | Inputs |
|---|---|---|---|---|
| `tv_capabilities` | yes | no | yes | `probe_device: bool = false` |
| `tv_status` | yes | no | yes | none |
| `tv_diagnostics` | yes | no | yes | none |
| `tv_capture` | yes | no | yes | none |
| `tv_home` | no | no | no | none |
| `tv_remote_key` | no | no | no | `key`: enum of the 9 names |
| `tv_launch_app` | no | no | no | `app_id`: enum of the 17 IDs |
| `tv_wake` † | no | no | yes | none |
| `maintenance_plan` * | yes | no | yes | `operation`: backup/cleanup/restore, `capsule_ref`: identifier |
| `maintenance_apply` * | no | **yes** | no | `operation`, `plan_id`, `capsule_ref` |

\* Registered only when the owner starts `quiettv-mcp serve --enable-maintenance`; they work only through a
validated toolkit adapter (see `TOOLKIT-ADAPTER.md`). † Registered only when the owner config enables `wake`.
Annotations are hints to clients, not enforcement; the
enforcement is the allowlists and checks above.

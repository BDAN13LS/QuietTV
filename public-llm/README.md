# quiettv-mcp

For a copyable owner prompt and setup checklist, see
[AI setup](../docs/AI-SETUP-PROMPT.md).

A local [Model Context Protocol](https://modelcontextprotocol.io) server that lets an AI client (Claude Desktop,
Claude Code, Codex, OpenCode) check and drive **one** owner-configured Quiet TV on your home network: read status,
show Home, press remote keys, launch allowlisted apps, run redacted diagnostics, take a fresh screen capture it
can look at and, if you opt in, send a Wake-on-LAN packet. Built on the official MCP Python SDK (`mcp` 2.3),
stdio transport.

It is deliberately narrow: one verified TV profile, fixed allowlists, identity checked on every call to the TV
service, no shell, no file access, no cloud.

**This is optional, and it is not an installer.** You never need AI to use a Quiet TV. This package only talks
to a TV that **already** runs the Quiet TV control service and for which you **already** hold the control key.
It cannot root, install, set up or restore a stock TV, and no consumer self-installer exists today; that is
separate, planned work. Setting up this AI controller and installing Quiet TV on a TV are different things.

## What you need first

- A TV that already runs the Quiet TV control service (HTTP port 4190) and is the supported profile:
  **UN43U8000FBXZA, software 1300.8, Tizen 9, ARMv7**. Other models and firmware are rejected.
- The service's controller allowlist includes this computer's LAN address (otherwise every call gets HTTP 403).
- Your control key, ideally already in a private file outside any git folder. Setup can also take it once at a
  hidden prompt and write that file for you.
- This computer on the same LAN as the TV, and Python 3.11 or newer.
- An MCP client if you want AI control (Claude Desktop, Claude Code, Codex or OpenCode). The human CLI below
  needs none.

## 1. Install into a virtual environment

Keep it in its own venv, not your global Python.

Windows (PowerShell):

```
git clone https://github.com/BDAN13LS/QuietTV.git
cd QuietTV\public-llm
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e .
.venv\Scripts\quiettv-mcp --help
```

macOS / Linux:

```
git clone https://github.com/BDAN13LS/QuietTV.git
cd QuietTV/public-llm
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/quiettv-mcp --help
```

The examples below use the Windows form `.venv\Scripts\quiettv-mcp`; on macOS/Linux use `.venv/bin/quiettv-mcp`.

## 2. Run guided setup

```
.venv\Scripts\quiettv-mcp setup
```

The wizard asks, in order:

1. the TV model code and software version (TV: Settings > Support > About This TV);
2. the TV's IP address and MAC address (Settings > Network > Network Status). The MAC must equal the address the
   TV's identity endpoint reports as `wifiMac`: every call compares the two and refuses on a mismatch, and
   `doctor --online` confirms it;
3. **the control key**: give the absolute path of your existing key file, or leave it empty and type the key at a
   hidden prompt; setup then writes `control.key` next to the config with owner-only permissions. The key is
   never accepted as a command-line argument or environment variable and never stored in the config;
4. an optional folder for private screenshots (empty keeps captures in memory only);
5. whether to enable optional Wake-on-LAN (default no; see [Wake-on-LAN](#wake-on-lan-optional)).

It writes the private config to `%APPDATA%\QuietTV-MCP\config.json` on Windows or
`~/.config/quiettv-mcp/config.json` elsewhere (`$XDG_CONFIG_HOME` is respected). To use another location, pass
`--config <path>` to every command or set `QUIETTV_MCP_CONFIG`. An existing config is replaced only after you
confirm. The full format is in [docs/SPEC.md](docs/SPEC.md#owner-config-quiettv-mcp-config1); you can also edit the
file by hand, and every command re-validates it.

Keep the key file and the capture folder out of any git working tree; `doctor` warns if they are inside one.

## 3. Check it with doctor

```
.venv\Scripts\quiettv-mcp doctor            # offline: config, key file, same LAN, wake settings
.venv\Scripts\quiettv-mcp doctor --online   # adds read-only identity (8001) and status (4190) checks
```

Every line should be `PASS`; `WARN` lines explain themselves (for example wake off, or capture not advertised).
`doctor` never sends a wake packet. Fix any `FAIL` before connecting an AI client.

## 4. Connect an MCP client

```
.venv\Scripts\quiettv-mcp client-config --client all
.venv\Scripts\quiettv-mcp client-config --client codex
.venv\Scripts\quiettv-mcp client-config --client all --output-dir C:\some\private\folder
```

`--client` is one of `claude-desktop`, `claude-code`, `codex`, `opencode` or `all`. The command prints each
fragment, or with `--output-dir` writes new files (it never overwrites, and refuses to write into a client's own
settings folder). **It never edits any client's settings: you merge the fragment or run the command yourself.**
Every fragment launches the venv's Python as `python -m quiettv_mcp serve --config <your config>`; it carries the
config path, never the key.

| Client | What `client-config` gives you | What you do with it |
|---|---|---|
| Claude Desktop | `{"mcpServers": {"quiettv": {"command": ..., "args": [...]}}}` | merge into `claude_desktop_config.json` (Windows `%APPDATA%\Claude\`, macOS `~/Library/Application Support/Claude/`), then restart Claude Desktop |
| Claude Code | a `claude mcp add --transport stdio --scope user quiettv -- <python> -m quiettv_mcp serve --config <path>` command | run it once; `claude mcp list` shows the server. Use `--scope local` or `project` instead of `user` if you prefer |
| Codex | `[mcp_servers.quiettv]` with `command` and `args` | merge into `~/.codex/config.toml`, or run the equivalent `codex mcp add quiettv -- <python> -m quiettv_mcp serve --config <path>` |
| OpenCode | `{"mcp": {"quiettv": {"type": "local", "command": [...], "enabled": true}}}` | merge into `opencode.json` |

Sources: Claude Desktop from modelcontextprotocol.io "Connect to local MCP servers" (checked 2026-10-26); Codex
`[mcp_servers.<name>]` keys `command`/`args`/`env` (2026-10-26); OpenCode `mcp.<name>.type = "local"` with a
`command` array, matching a working local OpenCode config (2026-10-26). The `claude mcp add` and `codex mcp add`
forms were checked against the local `--help` of Claude Code 2.1.310 and codex-cli 0.162.1 on 2026-10-27. Other
clients and other versions are untested; if your client takes stdio MCP servers, the same command and arguments
should apply, but nothing here confirms it.

In PowerShell, a client installed through npm may resolve to a `.ps1` shim (`Get-Command codex` shows which), and
PowerShell can drop the bare `--` when calling one. Use the `.cmd` shim instead (`codex.cmd mcp add ...`), run the
command from cmd or Git Bash, or merge the TOML fragment.

Then ask your client to call `tv_capabilities` (no device traffic) and `tv_status` (identity-checked) to confirm
the connection.

## Tools

| Tool | What it does |
|---|---|
| `tv_capabilities` | profile, tools, keys, apps; capture, wake and maintenance availability (offline unless `probe_device`) |
| `tv_status` | identity-checked service status, whitelisted fields |
| `tv_home` | press the remote Home key (63); confirm with `tv_capture` |
| `tv_remote_key` | `up` `down` `left` `right` `ok` `back` `home` `volume_up` `volume_down` |
| `tv_launch_app` | one of the profile's 17 app IDs (optionally narrowed by `allowed_apps`) |
| `tv_diagnostics` | the service's fixed diagnostics, summarised and redacted |
| `tv_capture` | a **fresh** PNG of the UI layer, plus metadata (id, time, size, SHA-256) |
| `tv_wake` | **only when the owner config enables wake**: send a Wake-on-LAN packet; reports "requested", never "awake" |

A control call being accepted is not proof it worked; confirm with `tv_capture`. **Captures show the UI layer
only: the TV's hardware video plane reads as black and sound is not captured, so check video and audio on the
TV itself.** If the TV service does not offer `POST /capture`, the tool says so; it never falls back to an older
saved screenshot.

## Wake-on-LAN (optional)

Off by default; existing configs are unchanged. To turn it on, answer `y` in setup or add this to the config:

```json
"wake": {"enabled": true, "port": 9, "packets": 1}
```

- `port` is 7 or 9; `packets` is 1 or 2 per call. Nothing else is accepted: there is no target field.
- The packet is the standard magic packet (six `FF` bytes, then the TV's MAC sixteen times) sent by UDP to the
  broadcast address of `tv.ip`/`lan_prefix` (for example `10.20.30.255` for `10.20.30.40` on a /24). The MAC is
  always `tv.mac`, the same address every identity check compares. `lan_prefix` must match your LAN's real
  subnet, and the config is rejected unless that is an RFC 1918 home network. No host names, URLs, scanning or
  shell.
- Repeat calls within 5 seconds are refused, so a model cannot loop broadcasts.
- A TV in standby does not answer the identity endpoint, so wake does **not** check identity first and does not
  use the control key. It only runs the local same-LAN check (which sends nothing). After waking, `tv_status`
  checks the identity as usual.

**A sent packet is not a woken TV.** The result says `requested: true, awake_verified: false`. It may not wake
the TV at all: the TV must allow network power-on (on Samsung TVs settings such as Network Standby or Power On
with Mobile, whose names vary by model and firmware), and an unplugged TV cannot wake. Support differs between
Wi-Fi and Ethernet: the packet carries `tv.mac`, the identity-checked `wifiMac`, so a TV on Ethernet whose wired
adapter has a different MAC may ignore it. Wait about 15 seconds, then run `tv_status` (or `run status`) to confirm. Wake has not been
verified on hardware by this release.

```
.venv\Scripts\quiettv-mcp run wake
.venv\Scripts\quiettv-mcp run status
```

### Maintenance is not available yet

Backup, cleanup and restore are **unavailable in this release**, and `tv_capabilities` says so. They are off
unless you start the server with `serve --enable-maintenance`, and even then they run only through an installed
toolkit adapter that reports a validated live transport (`docs/TOOLKIT-ADAPTER.md`). The companion
`public-toolkit` currently validates fetched backup capsules and builds offline restore plans, but it has **no
live adapter**: its apply step refuses and changes nothing. So with or without the flag, no maintenance tool can
change your TV today.

## Without an AI model: the human CLI

Every tool is also a plain command, with the same identity checks and allowlists:

```
.venv\Scripts\quiettv-mcp run status
.venv\Scripts\quiettv-mcp run home
.venv\Scripts\quiettv-mcp run key right            # up down left right ok back home volume_up volume_down
.venv\Scripts\quiettv-mcp run launch quiet.iptv
.venv\Scripts\quiettv-mcp run diagnostics
.venv\Scripts\quiettv-mcp run capture --out C:\private\shot.png   # writes a new file, never overwrites
.venv\Scripts\quiettv-mcp run wake                 # only if wake is enabled in your config
```

## What to expect from AI models

- **The model may refuse.** Claude (or any model) may decline to drive a device it cannot see, to work with a
  rooted TV, or to do anything it judges privileged. That is its decision. This project has **no prompt or
  setting to bypass a refusal** and accepts no contributions that try to; use the human CLI instead.
- **Provider policies vary** and change over time. What one client or model allows, another may not.
- **No model guarantees.** Development was assisted by Claude Opus 5.5. The stdio protocol tests exercise an SDK client, not an LLM model.
  **Claude Opus 4.8 may be able to help with setup, but the full setup process has not been verified with it.**
  Other older models, Claude 4.6 included, and other providers are **untested**. A model can misread a capture
  or press the wrong key; nothing here makes its judgement reliable. Confirm results yourself.
- The server's own limits hold whatever the model asks: fixed keys and apps, identity checks, no shell.

## Frontend developer preview

To try the Home interface source in a desktop browser, see [docs/FRONTEND-PREVIEW.md](docs/FRONTEND-PREVIEW.md).
That is a developer preview only, not an installer for any TV.

## Privacy

Tool results, captures included, go to whichever model your MCP client uses. Captures are saved to disk only if
you set `capture_dir`. See `SECURITY.md`.

## Development

`pip install -e ".[test]"`, then `python -m pytest`. Tests use a loopback fake TV, a recording UDP sender for wake
(no packet reaches any network) and real MCP over stdio; no TV or network is needed. Contributor guide and GitHub
templates are in `community/`; specification in `docs/SPEC.md`.

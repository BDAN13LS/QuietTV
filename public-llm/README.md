# quiettv-mcp

A local [Model Context Protocol](https://modelcontextprotocol.io) server that lets an AI client (Claude Desktop,
Codex, OpenCode) check and drive **one** owner-configured Quiet TV on your home network: read status, show
Home, press remote keys, launch allowlisted apps, run redacted diagnostics, and take a fresh screen capture it
can look at. Built on the official MCP Python SDK (`mcp` 2.3), stdio transport.

It is deliberately narrow: one verified TV profile, fixed allowlists, identity checked on every call, no shell,
no file access, no cloud.

## Requirements

- The TV runs the Quiet TV root service (HTTP port 4190) and is the supported profile:
  **UN43U8000FBXZA, software 1300.8, Tizen 9, ARMv7**. Other models and firmware are rejected.
- The TV service's controller allowlist includes this computer's LAN address.
- Your control key in a private file (or typed once into setup, which writes that file for you).
- Python 3.11+.

## Install (project venv, not global)

```
git clone https://github.com/BDAN13LS/QuietTV.git
cd QuietTV/public-llm
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e .          # macOS/Linux: .venv/bin/python -m pip install -e .
```

## Set up

```
.venv\Scripts\quiettv-mcp setup
```

The wizard asks for the model code and software version (TV: Settings > Support > About This TV), the TV's IP
and MAC address (Settings > Network > Network Status), and your key file path, or lets you type the key at a
hidden prompt. It writes a private config (`%APPDATA%\QuietTV-MCP\config.json` on Windows,
`~/.config/quiettv-mcp/config.json` elsewhere) with owner-only permissions. Secrets are never accepted as
command-line arguments.

Then check everything:

```
.venv\Scripts\quiettv-mcp doctor            # offline: config, key file, same LAN
.venv\Scripts\quiettv-mcp doctor --online   # adds read-only identity and status checks against the TV
```

## Connect an MCP client

```
.venv\Scripts\quiettv-mcp client-config --client all                  # print fragments
.venv\Scripts\quiettv-mcp client-config --client all --output-dir C:\some\private\folder
```

This prints (or writes new files with) a fragment for each client. **It never edits a client's own settings**:
merge the fragment yourself. Formats:

| Client | Where you merge it | Shape |
|---|---|---|
| Claude Desktop | `claude_desktop_config.json` | `{"mcpServers": {"quiettv": {"command": ..., "args": [...]}}}` |
| Codex | `~/.codex/config.toml` | `[mcp_servers.quiettv]` with `command` and `args` |
| OpenCode | `opencode.json` | `{"mcp": {"quiettv": {"type": "local", "command": [...], "enabled": true}}}` |

Sources checked on 2026-10-26: Claude Desktop from modelcontextprotocol.io "Connect to local MCP servers";
Codex `[mcp_servers.<name>]` keys `command`/`args`/`env`; OpenCode `mcp.<name>.type = "local"` with a `command`
array, matching a working local OpenCode config.

The fragment launches `python -m quiettv_mcp serve --config <your config>` with the venv's Python.

## Tools

| Tool | What it does |
|---|---|
| `tv_capabilities` | profile, tools, keys, apps; capture and maintenance availability (offline unless `probe_device`) |
| `tv_status` | identity-checked service status, whitelisted fields |
| `tv_home` | press the remote Home key (63); confirm with `tv_capture` |
| `tv_remote_key` | `up` `down` `left` `right` `ok` `back` `home` `volume_up` `volume_down` |
| `tv_launch_app` | one of the profile's 17 app IDs (optionally narrowed by `allowed_apps`) |
| `tv_diagnostics` | the service's fixed diagnostics, summarised and redacted |
| `tv_capture` | a **fresh** PNG of the UI layer, plus metadata (id, time, size, SHA-256) |

A control call being accepted is not proof it worked; confirm with `tv_capture`. **Captures show the UI layer
only: the TV's hardware video plane reads as black and sound is not captured, so check video and audio on the
TV itself.** If the TV service does not offer `POST /capture`, the tool says so; it never falls back to an older
saved screenshot.

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
.venv\Scripts\quiettv-mcp run key right            # up down left right ok back home volume_up volume_down
.venv\Scripts\quiettv-mcp run launch quiet.iptv
.venv\Scripts\quiettv-mcp run capture --out C:\private\shot.png   # writes a new file, never overwrites
```

## What to expect from AI models

- **The model may refuse.** Claude (or any model) may decline to drive a device it cannot see, to work with a
  rooted TV, or to do anything it judges privileged. That is its decision. This project has **no prompt or
  setting to bypass a refusal** and accepts no contributions that try to; use the human CLI instead.
- **Provider policies vary** and change over time. What one client or model allows, another may not.
- **No model guarantees.** Development was assisted by Claude Opus 5.5. The stdio protocol tests exercise an SDK client, not an LLM model.
  Older models, Claude 4.6 included, and other providers are **untested**. A model can misread a capture or
  press the wrong key; nothing here makes its judgement reliable. Confirm results yourself.
- The server's own limits hold whatever the model asks: fixed keys and apps, identity checks, no shell.

## Frontend developer preview

To try the Home interface source in a desktop browser, see [docs/FRONTEND-PREVIEW.md](docs/FRONTEND-PREVIEW.md).
That is a developer preview only, not an installer for any TV.

## Privacy

Tool results, captures included, go to whichever model your MCP client uses. Captures are saved to disk only if
you set `capture_dir`. See `SECURITY.md`.

## Development

`pip install -e ".[test]"`, then `python -m pytest`. Tests use a loopback fake TV and real MCP over stdio; no TV
or network is needed. Contributor guide and GitHub templates are in `community/`; specification in
`docs/SPEC.md`.

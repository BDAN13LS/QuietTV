# Security model

**What it can do.** Read the TV service status, show Home, press nine remote keys, launch allowlisted apps, run
the service's fixed diagnostics, and take a fresh UI capture. With an owner launch flag and a validated toolkit
adapter, plan and apply backup, cleanup or restore.

**What it cannot do.** Run arbitrary commands, read or write arbitrary files, fetch arbitrary URLs, use the TV
service's maintenance / developer / reboot / home-assignment routes, or talk to anything but the one configured
LAN address. There are no default credentials or device values; the key lives in an owner-only file.

**Fail-closed checks on every call.** Key file well-formed, TV on this machine's LAN, model and MAC match the
owner config, firmware matches the profile when the TV reports it. Any failure stops the call before the next
request; tests prove a mismatch sends nothing to the control port.

**Data leaving the server.** Status is whitelisted; diagnostics are summarised and redacted; the key is never
echoed, logged or placed in errors. Captures are returned to the MCP client and, only if configured, saved to the
owner's private folder. Nothing is uploaded by this server, but **your MCP client sends tool results, captures
included, to its model provider**. Do not capture screens showing private content you would not send there.

**Trust boundaries.** The service on port 4190 and the identity endpoint on 8001 are plain HTTP on the LAN.
Anyone on the LAN who can read your traffic can see the key; keep the TV on a trusted network. MCP tool
annotations are hints, not enforcement.

Report vulnerabilities privately through GitHub's private vulnerability reporting.

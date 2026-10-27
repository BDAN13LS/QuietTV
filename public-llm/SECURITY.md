# Security model

**What it can do.** Read the TV service status, show Home, press nine remote keys, launch allowlisted apps, run
the service's fixed diagnostics, and take a fresh UI capture. If the owner config enables it, send a Wake-on-LAN
magic packet to the configured TV MAC on the LAN broadcast derived from the TV's IP. With an owner launch flag and a validated toolkit
adapter, plan and apply backup, cleanup or restore.

**What it cannot do.** Run arbitrary commands, read or write arbitrary files, fetch arbitrary URLs, use the TV
service's maintenance / developer / reboot / home-assignment routes, or talk to anything but the one configured
LAN address (plus, with wake enabled, that address's own RFC 1918 LAN broadcast on UDP port 7 or 9, at most two
packets per call). There are no default credentials or device values; the key lives in an owner-only file.

**Wake is unauthenticated by nature.** A magic packet carries no key and anyone on the LAN can send one; this
server adds nothing an attacker on your LAN could not already do. It sends no key with it, cannot be aimed at any
other host or MAC, and never reports the TV as awake.

**Fail-closed checks on every call to the TV service.** Key file well-formed, TV on this machine's LAN, model and MAC match the
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

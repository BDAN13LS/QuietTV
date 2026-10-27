"""MCP server definition (official Python SDK, MCPServer, stdio transport)."""
from __future__ import annotations

import base64
import json
import logging
import re
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CallToolResult, ImageContent, TextContent, ToolAnnotations
from pydantic import Field

from . import __version__
from .config import OwnerConfig
from .device import DeviceError, QuietTV
from .profiles import AppId, KeyName
from .redact import redact_value
from .toolkit import MaintenanceError, MaintenanceGate, Operation

log = logging.getLogger("quiettv_mcp")

INSTRUCTIONS = (
    "Controls one owner-configured TV on the local network. Every call re-checks the TV's identity "
    "(model and MAC) and refuses on mismatch. Use tv_capture to see the screen after any control action: "
    "a control call being accepted is not proof that it worked. Captures show the UI layer only; the "
    "hardware video layer reads as black, so video and sound must be checked directly by a person."
)

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
CONTROL = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
MUTATION = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False)

TOOL_NAMES = ("tv_capabilities", "tv_status", "tv_home", "tv_remote_key", "tv_launch_app", "tv_diagnostics",
              "tv_capture")
MAINTENANCE_TOOL_NAMES = ("maintenance_plan", "maintenance_apply")

CapsuleRef = Annotated[str | None, Field(default=None, pattern=r"^[A-Za-z0-9._-]{1,128}$",
                                         description="Capsule identifier known to the toolkit (not a path).")]
PlanId = Annotated[str, Field(pattern=r"^[A-Za-z0-9._-]{1,128}$", description="plan_id returned by maintenance_plan.")]


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_STRUCTURAL = {"operation", "plan_id", "result", "applied", "mutates", "capsule_sha256"}


def _keep_digests(value: Any) -> Any:
    """Redact free text but keep SHA-256 hex digests, which are evidence rather than secrets."""
    if isinstance(value, str) and _SHA256.match(value):
        return value
    if isinstance(value, dict):
        return {redact_value(str(k))[:80]: _keep_digests(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_keep_digests(v) for v in value]
    return redact_value(value)


def _safe_maintenance(result: dict[str, Any]) -> dict[str, Any]:
    # Structural fields were format-checked by the gate (identifier / 64-hex / bool) and must stay usable.
    return {k: (v if k in _STRUCTURAL else _keep_digests(v)) for k, v in result.items()}


def _device_call(fn, *args):
    try:
        return fn(*args)
    except DeviceError as exc:
        raise ToolError(str(exc)) from None


def build_server(config: OwnerConfig, *, device: QuietTV | None = None, maintenance: MaintenanceGate | None = None) -> MCPServer:
    tv = device or QuietTV(config)
    gate = maintenance or MaintenanceGate(enabled=False, adapter=None)
    server = MCPServer(name="quiettv", title="Quiet TV", version=__version__, instructions=INSTRUCTIONS)

    @server.tool(title="Capabilities", annotations=READ_ONLY)
    def tv_capabilities(probe_device: bool = False) -> dict[str, Any]:
        """List what this server can do for the configured TV profile. With probe_device=true it also
        runs the identity check and reads the TV service status (read-only)."""
        result: dict[str, Any] = {
            "server_version": __version__,
            "profile": {
                "id": config.profile.profile_id,
                "model": config.profile.model,
                "firmware": config.profile.firmware,
                "os": f"{config.profile.os} {config.profile.os_version}",
                "arch": config.profile.arch,
            },
            "tools": list(TOOL_NAMES) + (list(MAINTENANCE_TOOL_NAMES) if gate.enabled else []),
            "remote_keys": list(config.profile.keys),
            "apps": list(config.allowed_apps),
            "capture": {
                "route": "POST /capture",
                "support": "unknown until probed or called; reported as not available when the TV service lacks it",
                "stale_fallback": "never (GET /screenshot is not used)",
                "limits": "UI layer only; hardware video plane captures black; sound is not captured",
            },
            "maintenance": gate.capabilities(),
        }
        if config.fixture_mode:
            result["mode"] = "FIXTURE MODE (loopback test target, not a TV)"
        if probe_device:
            probe = _device_call(tv.status)
            result["device"] = {"identity": probe["identity"],
                                "service_capabilities": probe["status"].get("service_capabilities", "not reported")}
        return result

    @server.tool(title="TV status", annotations=READ_ONLY)
    def tv_status() -> dict[str, Any]:
        """Read the TV service status (identity-checked, whitelisted fields only)."""
        return _device_call(tv.status)

    @server.tool(title="Show Home", annotations=CONTROL)
    def tv_home() -> dict[str, Any]:
        """Press the remote Home key so the TV shows its registered Home. The foreground is not verified; check with tv_capture."""
        return _device_call(tv.home)

    @server.tool(title="Press remote key", annotations=CONTROL)
    def tv_remote_key(key: KeyName) -> dict[str, Any]:
        """Press one remote key. Only arrows, ok, back, home and volume are accepted."""
        return _device_call(tv.key, key)

    @server.tool(title="Launch app", annotations=CONTROL)
    def tv_launch_app(app_id: AppId) -> dict[str, Any]:
        """Launch one app from the profile's allowlist (and the owner's allowed_apps)."""
        return _device_call(tv.launch, app_id)

    @server.tool(title="Diagnostics", annotations=READ_ONLY)
    def tv_diagnostics() -> dict[str, Any]:
        """Run the TV service's fixed diagnostics and return a redacted summary (never raw settings)."""
        return _device_call(tv.diagnostics)

    @server.tool(title="Capture screen", annotations=READ_ONLY, structured_output=False)
    def tv_capture() -> CallToolResult:
        """Capture a FRESH screenshot of the TV's UI layer as a PNG. Video playback reads as black."""
        shot, meta = _device_call(tv.capture)
        return CallToolResult(content=[
            ImageContent(type="image", data=base64.b64encode(shot.png).decode("ascii"), mime_type="image/png"),
            TextContent(type="text", text=json.dumps(meta, sort_keys=True)),
        ])

    if gate.enabled:
        @server.tool(title="Plan maintenance (dry run)", annotations=READ_ONLY)
        def maintenance_plan(operation: Operation, capsule_ref: CapsuleRef = None) -> dict[str, Any]:
            """Ask the validated toolkit for a dry-run plan of backup, cleanup or restore. Changes nothing."""
            try:
                return _safe_maintenance(gate.plan(operation, capsule_ref))
            except MaintenanceError as exc:
                raise ToolError(str(exc)) from None

        @server.tool(title="Apply maintenance plan", annotations=MUTATION)
        def maintenance_apply(operation: Operation, plan_id: PlanId, capsule_ref: CapsuleRef = None) -> dict[str, Any]:
            """Apply a plan from maintenance_plan through the validated toolkit. Destructive. Reports
            'applied' only with toolkit evidence."""
            try:
                return _safe_maintenance(gate.apply(operation, plan_id, capsule_ref))
            except MaintenanceError as exc:
                raise ToolError(str(exc)) from None

    return server

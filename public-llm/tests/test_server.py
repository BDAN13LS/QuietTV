"""MCP-level behaviour through the SDK's in-process client."""
from __future__ import annotations

import base64
import json

import anyio
from mcp.client.client import Client

from quiettv_mcp.capture import PNG_SIGNATURE
from quiettv_mcp.profiles import APP_IDS, KEY_CODES
from quiettv_mcp.server import TOOL_NAMES, build_server
from quiettv_mcp.toolkit import MaintenanceGate


def _run(server, fn):
    async def go():
        async with Client(server) as client:
            return await fn(client)
    return anyio.run(go)


def test_tools_schemas_and_annotations(config, device):
    tools = _run(build_server(config, device=device), lambda c: c.list_tools()).tools
    by_name = {t.name: t for t in tools}
    assert set(by_name) == set(TOOL_NAMES)
    assert set(by_name["tv_remote_key"].input_schema["properties"]["key"]["enum"]) == set(KEY_CODES)
    assert set(by_name["tv_launch_app"].input_schema["properties"]["app_id"]["enum"]) == set(APP_IDS)
    for name in ("tv_capabilities", "tv_status", "tv_diagnostics", "tv_capture"):
        assert by_name[name].annotations.read_only_hint is True, name
    for name in ("tv_home", "tv_remote_key", "tv_launch_app"):
        ann = by_name[name].annotations
        assert ann.read_only_hint is False and ann.destructive_hint is False, name
        assert ann.idempotent_hint is False, name  # a second Home press is not known to be a no-op
    assert not any("maintenance" in n for n in by_name)


def test_invalid_arguments_rejected_before_device(config, device, fake):
    async def calls(client):
        return [await client.call_tool("tv_remote_key", {"key": "power"}),
                await client.call_tool("tv_remote_key", {"key": "ok; reboot"}),
                await client.call_tool("tv_remote_key", {"key": 116}),
                await client.call_tool("tv_launch_app", {"app_id": "com.example.evil"}),
                await client.call_tool("tv_launch_app", {"app_id": "quiet.iptv", "extra": "x"})]
    results = _run(build_server(config, device=device), calls)
    assert all(r.is_error for r in results[:4])
    assert fake.hits["POST /key/116"] == 0
    assert not any("example" in route for route in fake.hits)


def test_status_and_capture_over_mcp(config, device, fake, key):
    async def calls(client):
        return (await client.call_tool("tv_status", {}), await client.call_tool("tv_capture", {}))
    status, capture = _run(build_server(config, device=device), calls)
    assert not status.is_error and status.structured_content["status"]["ready"] is True
    assert not capture.is_error
    image = [c for c in capture.content if c.type == "image"][0]
    assert image.mime_type == "image/png"
    assert base64.b64decode(image.data).startswith(PNG_SIGNATURE)
    meta = json.loads([c for c in capture.content if c.type == "text"][0].text)
    assert meta["capture"]["capture_id"].startswith("cap-")
    for result in (status, capture):
        assert key not in result.model_dump_json()


def test_capture_unavailable_is_an_honest_error(config, device, fake):
    fake.capture_mode = "absent"
    result = _run(build_server(config, device=device), lambda c: c.call_tool("tv_capture", {}))
    assert result.is_error
    assert "does not offer POST /capture" in result.content[0].text
    assert fake.hits["GET /screenshot"] == 0


def test_tv_home_injects_home_key_over_mcp(config, device, fake):
    """Regression (live defect): tv_home must press key 63, not use the ineffective POST /home."""
    result = _run(build_server(config, device=device), lambda c: c.call_tool("tv_home", {}))
    assert not result.is_error
    assert result.structured_content["code"] == 63 and result.structured_content["foreground_verified"] is False
    assert fake.hits["POST /key/63"] == 1 and fake.hits["POST /home"] == 0
    assert fake.foreground == "home"


def test_tv_home_failed_injection_is_tool_error(config, device, fake):
    fake.key_exit_code = 2
    result = _run(build_server(config, device=device), lambda c: c.call_tool("tv_home", {}))
    assert result.is_error and "exit code 2" in result.content[0].text


def test_identity_mismatch_is_tool_error_with_no_control(config, device, fake):
    fake.device["wifiMac"] = "02:00:00:00:00:0A"
    result = _run(build_server(config, device=device), lambda c: c.call_tool("tv_home", {}))
    assert result.is_error and "MAC" in result.content[0].text
    assert fake.control_hits() == 0


def test_capabilities_offline_by_default(config, device, fake):
    result = _run(build_server(config, device=device), lambda c: c.call_tool("tv_capabilities", {}))
    data = result.structured_content
    assert data["maintenance"]["backup"]["apply"] is False
    assert "launch the server with --enable-maintenance" in data["maintenance"]["backup"]["reason"]
    assert sum(fake.hits.values()) == 0
    probed = _run(build_server(config, device=device), lambda c: c.call_tool("tv_capabilities", {"probe_device": True}))
    assert "capture" in probed.structured_content["device"]["service_capabilities"]


def test_maintenance_round_trip_over_mcp_keeps_ids_and_digests(config, device):
    """Realistic adapter values (UUID plan ids, SHA-256 digests) must survive redaction so apply can work."""
    import hashlib
    import uuid

    from test_maintenance import RecordingAdapter
    digest = hashlib.sha256(b"capsule").hexdigest()

    class UuidAdapter(RecordingAdapter):
        def plan(self, operation, capsule_ref):
            return {"plan_id": str(uuid.uuid4()), "summary": "dry run token=hunter2secret9", "steps": ["s"], "mutates": True}

        def verify_capsule(self, capsule_ref):
            return {"verified": True, "sha256": digest, "reason": "ok"}

    adapter = UuidAdapter(evidence={"post_restore_sha256": digest, "note": "see http://user:pw1@host.example/x?t=abc123"})
    gate = MaintenanceGate(enabled=True, adapter=adapter)

    async def calls(client):
        plan = await client.call_tool("maintenance_plan", {"operation": "restore", "capsule_ref": "capsule-1"})
        applied = await client.call_tool("maintenance_apply", {"operation": "restore", "capsule_ref": "capsule-1",
                                                               "plan_id": plan.structured_content["plan_id"]})
        return plan, applied
    plan, applied = _run(build_server(config, device=device, maintenance=gate), calls)
    assert not plan.is_error and len(plan.structured_content["plan_id"]) == 36
    assert "hunter2secret9" not in plan.model_dump_json()
    assert not applied.is_error, applied.content
    data = applied.structured_content
    assert data["result"] == "applied" and data["capsule_sha256"] == digest
    assert data["evidence"]["post_restore_sha256"] == digest
    assert "pw1" not in applied.model_dump_json() and "abc123" not in applied.model_dump_json()


def test_maintenance_flag_without_adapter_registers_nothing_that_lies(config, device):
    gate = MaintenanceGate(enabled=True, adapter=None, reason="no toolkit adapter installed")

    async def calls(client):
        tools = await client.list_tools()
        plan = await client.call_tool("maintenance_plan", {"operation": "backup"})
        apply = await client.call_tool("maintenance_apply", {"operation": "restore", "plan_id": "p1", "capsule_ref": "c1"})
        bad = await client.call_tool("maintenance_plan", {"operation": "backup", "capsule_ref": "../../etc/passwd"})
        return tools, plan, apply, bad
    tools, plan, apply, bad = _run(build_server(config, device=device, maintenance=gate), calls)
    names = {t.name: t for t in tools.tools}
    assert names["maintenance_apply"].annotations.destructive_hint is True
    assert plan.is_error and "no toolkit adapter" in plan.content[0].text
    assert apply.is_error
    assert bad.is_error

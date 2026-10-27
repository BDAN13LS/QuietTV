"""Real MCP protocol over a real stdio subprocess (initialize, tools/list, tools/call)."""
from __future__ import annotations

import base64
import json
import shutil
import sys
from pathlib import Path

import anyio
import pytest
from conftest import EXAMPLE_IP, config_dict
from mcp.client.client import Client
from mcp.client.stdio import StdioServerParameters

from quiettv_mcp.capture import PNG_SIGNATURE
from quiettv_mcp.server import TOOL_NAMES, WAKE_TOOL_NAMES

HERE = Path(__file__).resolve().parent


def _session(params: StdioServerParameters, fn):
    async def go():
        with anyio.fail_after(60):
            async with Client(params) as client:
                return client.server_info, await fn(client)
    return anyio.run(go)


def _console_entry() -> str:
    exe = shutil.which("quiettv-mcp", path=str(Path(sys.executable).parent))
    if exe is None:
        pytest.fail("quiettv-mcp console entry is not installed; run `pip install -e .` in this venv first")
    return exe


def test_installed_console_entry_initializes_and_fails_closed(config_file, key_file):
    key_file.unlink()  # no key: status must fail locally, before any packet leaves
    params = StdioServerParameters(command=_console_entry(), args=["serve", "--config", str(config_file)])

    async def calls(client):
        tools = await client.list_tools()
        caps = await client.call_tool("tv_capabilities", {})
        status = await client.call_tool("tv_status", {})
        return tools, caps, status
    info, (tools, caps, status) = _session(params, calls)
    assert info.name == "quiettv"
    assert {t.name for t in tools.tools} == set(TOOL_NAMES)
    assert caps.structured_content["profile"]["model"] == "UN43U8000FBXZA"
    assert "mode" not in caps.structured_content  # the console entry can never be in fixture mode
    assert status.is_error and "no request sent" in status.content[0].text


def test_maintenance_tools_need_owner_flag(config_file):
    exe = _console_entry()

    async def names(client):
        return {t.name for t in (await client.list_tools()).tools}
    _, off = _session(StdioServerParameters(command=exe, args=["serve", "--config", str(config_file)]), names)
    _, on = _session(StdioServerParameters(command=exe, args=["serve", "--config", str(config_file), "--enable-maintenance"]), names)
    assert "maintenance_apply" not in off
    assert {"maintenance_plan", "maintenance_apply"} <= on


def test_console_entry_declares_tv_wake_only_when_owner_enables_it(tmp_path, key_file):
    """List only: calling tv_wake through the console entry would put a real broadcast on this machine's LAN."""
    exe = _console_entry()
    cfg = tmp_path / "wake-config.json"
    cfg.write_text(json.dumps(config_dict(key_file, ip=EXAMPLE_IP, wake={"enabled": True})), encoding="utf-8")

    async def names(client):
        return {t.name for t in (await client.list_tools()).tools}
    _, on = _session(StdioServerParameters(command=exe, args=["serve", "--config", str(cfg)]), names)
    assert on == set(TOOL_NAMES) | set(WAKE_TOOL_NAMES)


def test_stdio_tv_wake_sends_one_recorded_packet(tmp_path, key_file):
    cfg = tmp_path / "wake-config.json"
    cfg.write_text(json.dumps(config_dict(key_file, ip=EXAMPLE_IP, wake={"enabled": True, "port": 7})), encoding="utf-8")
    record = tmp_path / "packets.jsonl"
    params = StdioServerParameters(command=sys.executable,
                                   args=[str(HERE / "stdio_wake_fixture_server.py"), str(cfg), str(record)])
    _, result = _session(params, lambda client: client.call_tool("tv_wake", {}))
    assert not result.is_error, result.content
    assert result.structured_content["requested"] is True and result.structured_content["awake_verified"] is False
    lines = [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]
    assert lines == [["ff" * 6 + "020000000001" * 16, "10.20.30.255", 7]]


def test_stdio_status_and_capture_against_fake_tv(tmp_path, key_file, fake, key):
    cfg = tmp_path / "fixture-config.json"
    cfg.write_text(json.dumps(config_dict(key_file)), encoding="utf-8")
    params = StdioServerParameters(command=sys.executable,
                                   args=[str(HERE / "stdio_fixture_server.py"), str(cfg), str(fake.identity_port), str(fake.control_port)])

    async def calls(client):
        return (await client.list_tools(), await client.call_tool("tv_status", {}),
                await client.call_tool("tv_remote_key", {"key": "down"}), await client.call_tool("tv_capture", {}))
    _, (tools, status, press, shot) = _session(params, calls)
    assert len(tools.tools) == len(TOOL_NAMES)
    assert status.structured_content["status"]["ready"] is True
    assert "FIXTURE MODE" in status.structured_content["mode"]
    assert press.structured_content["code"] == 108
    image = next(c for c in shot.content if c.type == "image")
    assert base64.b64decode(image.data).startswith(PNG_SIGNATURE)
    for result in (status, press, shot):
        assert key not in result.model_dump_json()
    assert fake.hits["POST /capture"] == 1 and fake.hits["POST /key/108"] == 1

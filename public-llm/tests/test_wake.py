"""Optional owner-configured Wake-on-LAN: config gate, packet bytes, limits, no online precheck, MCP and CLI.

Every packet here goes to a recording sender. The real sender is replaced by a failing stub in conftest, and its
own test swaps in a fake socket, so nothing reaches a network.
"""
from __future__ import annotations

import io
import json
import socket

import anyio
import pytest
from conftest import EXAMPLE_IP, config_dict
from mcp.client.client import Client

import quiettv_mcp.cli as cli
import quiettv_mcp.device as device_module
from quiettv_mcp.config import ConfigError, config_to_dict, load_config, validate_config
from quiettv_mcp.device import WAKE_COOLDOWN_S, DeviceError, QuietTV, magic_packet
from quiettv_mcp.device import send_udp_broadcast as REAL_SENDER
from quiettv_mcp.server import TOOL_NAMES, WAKE_TOOL_NAMES, build_server
from quiettv_mcp.setup_cli import run_doctor, run_setup

MAC = "02:00:00:00:00:01"  # conftest FIXTURE_MAC
BROADCAST = "10.20.30.255"
ON = {"enabled": True}


def wake_config(key_file, wake=ON, ip=EXAMPLE_IP, **extra):
    return validate_config(config_dict(key_file, ip=ip, wake=wake, **extra))


class Recorder:
    def __init__(self, fail_after: int | None = None):
        self.sent: list[tuple[bytes, tuple[str, int]]] = []
        self.fail_after = fail_after

    def __call__(self, packet, address):
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            raise OSError("simulated send failure")
        self.sent.append((packet, address))


class OfflineTV(QuietTV):
    """The same-LAN check is local, but its answer depends on this machine's routes; pin it for the tests."""
    lan_ok = True

    def check_lan(self):
        if not self.lan_ok:
            raise DeviceError("the TV is not on this machine's LAN segment (lan_prefix); no request sent")
        return {"same_lan": True, "lan_prefix": self.config.lan_prefix}


@pytest.fixture
def no_tcp(monkeypatch):
    """Any HTTP request (identity on 8001 or control on 4190) fails the test."""
    calls = []

    def refuse(*args, **kwargs):
        calls.append(args)
        raise AssertionError("wake must not contact the TV service or identity endpoint")
    monkeypatch.setattr(device_module, "request", refuse)
    return calls


# -- packet ------------------------------------------------------------------

def test_magic_packet_bytes_are_standard():
    packet = magic_packet(MAC)
    assert len(packet) == 102
    assert packet == bytes.fromhex("ff" * 6 + "020000000001" * 16)


def test_real_sender_sets_broadcast_and_sends_once(monkeypatch):
    events = []

    class FakeSocket:
        def __init__(self, family, kind):
            events.append(("socket", family, kind))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            events.append(("closed",))

        def setsockopt(self, level, option, value):
            events.append(("setsockopt", level, option, value))

        def sendto(self, data, address):
            events.append(("sendto", data, address))

        def connect(self, *args):  # pragma: no cover - would mean a non-broadcast path
            raise AssertionError("the sender must not connect")

    monkeypatch.setattr(device_module.socket, "socket", FakeSocket)
    REAL_SENDER(b"payload", (BROADCAST, 9))
    assert events == [("socket", socket.AF_INET, socket.SOCK_DGRAM),
                      ("setsockopt", socket.SOL_SOCKET, socket.SO_BROADCAST, 1),
                      ("sendto", b"payload", (BROADCAST, 9)), ("closed",)]


# -- config gate -------------------------------------------------------------

def test_existing_config_without_wake_is_unchanged(config_file):
    config = load_config(config_file)
    assert config.wake is None
    assert "wake" not in config_to_dict(config)


def test_wake_derives_lan_broadcast_and_defaults(key_file):
    wake = wake_config(key_file).wake
    assert (str(wake.broadcast), wake.port, wake.packets) == (BROADCAST, 9, 1)
    assert str(wake_config(key_file, lan_prefix=16).wake.broadcast) == "10.20.255.255"
    assert wake_config(key_file, wake={"enabled": True, "port": 7, "packets": 2}).wake.port == 7


def test_wake_round_trips_through_config_to_dict(key_file):
    config = wake_config(key_file, wake={"enabled": True, "port": 7, "packets": 2})
    data = config_to_dict(config)
    assert data["wake"] == {"enabled": True, "port": 7, "packets": 2}
    assert validate_config(data).wake == config.wake


def test_wake_enabled_false_is_off(key_file):
    assert wake_config(key_file, wake={"enabled": False, "port": 9}).wake is None


@pytest.mark.parametrize("wake", [
    {"enabled": True, "port": 8},
    {"enabled": True, "port": 10},
    {"enabled": True, "port": 0},
    {"enabled": True, "port": "9"},
    {"enabled": True, "port": True},
    {"enabled": True, "packets": 0},
    {"enabled": True, "packets": 3},
    {"enabled": True, "packets": 100},
    {"enabled": True, "packets": True},
    {"enabled": True, "packets": "1"},
    {"enabled": "true"},
    {"enabled": 1},
    {"port": 9},
    {"enabled": True, "broadcast": "203.0.113.255"},
    {"enabled": True, "mac": "02:00:00:00:00:02"},
    {"enabled": True, "host": "example.com"},
    {"enabled": True, "url": "http://example.com/"},
    [True],
    True,
    "on",
])
def test_bad_wake_sections_fail_closed(key_file, wake):
    with pytest.raises(ConfigError):
        wake_config(key_file, wake=wake)


@pytest.mark.parametrize("ip,prefix", [("10.20.30.0", 24), ("10.20.30.255", 24), ("10.20.0.0", 16)])
def test_wake_rejects_tv_ip_that_is_network_or_broadcast(key_file, ip, prefix):
    with pytest.raises(ConfigError, match="network or broadcast"):
        wake_config(key_file, ip=ip, lan_prefix=prefix)
    assert validate_config(config_dict(key_file, ip=ip, lan_prefix=prefix)).wake is None  # wake off: unchanged


def test_wake_needs_an_rfc1918_lan(key_file):
    with pytest.raises(ConfigError, match="RFC 1918"):
        wake_config(key_file, ip="198.18.0.5")  # "private" to Python, but not an RFC 1918 home LAN
    with pytest.raises(ConfigError, match="RFC 1918"):
        validate_config(config_dict(key_file, wake=ON), allow_loopback_fixture=True)  # loopback fixture


@pytest.mark.parametrize("mac", ["FF:FF:FF:FF:FF:FF", "00:00:00:00:00:00", "01:00:5E:00:00:01", "03:00:00:00:00:01"])
def test_wake_uses_the_validated_unicast_tv_mac(key_file, mac):
    data = config_dict(key_file, ip=EXAMPLE_IP, wake=ON)
    data["tv"]["mac"] = mac
    with pytest.raises(ConfigError, match="unicast"):
        validate_config(data)


# -- device ------------------------------------------------------------------

@pytest.mark.parametrize("packets", [1, 2])
def test_wake_sends_exact_packets_to_derived_broadcast(key_file, no_tcp, packets):
    key_file.unlink()  # the key is not needed to wake, and its absence must not matter
    sender = Recorder()
    tv = OfflineTV(wake_config(key_file, wake={"enabled": True, "port": 9, "packets": packets}), udp_sender=sender)
    result = tv.wake()
    assert sender.sent == [(magic_packet(MAC), (BROADCAST, 9))] * packets
    assert result["requested"] is True and result["packets_sent"] == packets
    assert result["awake_verified"] is False and result["identity_checked"] is False
    assert "tv_status" in result["note"] and "does not confirm" in result["note"]
    assert no_tcp == []
    text = json.dumps(result)
    assert MAC not in text and BROADCAST not in text and EXAMPLE_IP not in text


def test_wake_port_7(key_file, no_tcp):
    sender = Recorder()
    OfflineTV(wake_config(key_file, wake={"enabled": True, "port": 7}), udp_sender=sender).wake()
    assert sender.sent == [(magic_packet(MAC), (BROADCAST, 7))]


def test_wake_disabled_sends_nothing(config, key_file, no_tcp):
    sender = Recorder()
    with pytest.raises(DeviceError, match="Wake-on-LAN is off"):
        OfflineTV(config, udp_sender=sender).wake()
    with pytest.raises(DeviceError, match="Wake-on-LAN is off"):
        OfflineTV(validate_config(config_dict(key_file, ip=EXAMPLE_IP, wake={"enabled": False})),
                  udp_sender=sender).wake()
    assert sender.sent == []


def test_wake_refused_off_lan_sends_nothing(key_file, no_tcp):
    sender = Recorder()
    tv = OfflineTV(wake_config(key_file), udp_sender=sender)
    tv.lan_ok = False
    with pytest.raises(DeviceError, match="LAN segment"):
        tv.wake()
    assert sender.sent == []


def test_wake_cooldown_bounds_repeat_calls(key_file, no_tcp):
    now = [1000.0]
    sender = Recorder()
    tv = OfflineTV(wake_config(key_file, wake={"enabled": True, "packets": 2}), udp_sender=sender,
                   clock=lambda: now[0])
    tv.wake()
    with pytest.raises(DeviceError, match="less than"):
        tv.wake()
    assert len(sender.sent) == 2
    now[0] += WAKE_COOLDOWN_S
    tv.wake()
    assert len(sender.sent) == 4


def test_wake_send_failure_is_reported_honestly(key_file, no_tcp):
    tv = OfflineTV(wake_config(key_file, wake={"enabled": True, "packets": 2}), udp_sender=Recorder(fail_after=1))
    with pytest.raises(DeviceError, match="after 1 of 2 sent"):
        tv.wake()


def test_default_sender_is_the_module_sender(key_file, no_tcp):
    """Without an injected sender, wake uses send_udp_broadcast (stubbed to fail by conftest)."""
    with pytest.raises(AssertionError, match="real UDP broadcast sender"):
        OfflineTV(wake_config(key_file)).wake()


# -- MCP ---------------------------------------------------------------------

def _run(server, fn):
    async def go():
        async with Client(server) as client:
            return await fn(client)
    return anyio.run(go)


def test_tv_wake_not_declared_unless_enabled(config, device, fake):
    async def calls(client):
        return (await client.list_tools(), await client.call_tool("tv_capabilities", {}),
                await client.call_tool("tv_wake", {}))
    tools, caps, wake = _run(build_server(config, device=device), calls)
    assert {t.name for t in tools.tools} == set(TOOL_NAMES)
    assert caps.structured_content["wake"]["enabled"] is False
    assert "tv_wake" not in caps.structured_content["tools"]
    assert wake.is_error
    assert sum(fake.hits.values()) == 0


def test_tv_wake_declaration_and_result_over_mcp(key_file, no_tcp):
    config = wake_config(key_file, wake={"enabled": True, "packets": 2})
    sender = Recorder()
    server = build_server(config, device=OfflineTV(config, udp_sender=sender))

    async def calls(client):
        return (client.server_info, await client.list_tools(), await client.call_tool("tv_capabilities", {}),
                await client.call_tool("tv_wake", {}),
                await client.call_tool("tv_wake", {"host": "203.0.113.9", "port": 22}))
    info, tools, caps, result, again = _run(server, calls)
    by_name = {t.name: t for t in tools.tools}
    assert set(by_name) == set(TOOL_NAMES) | set(WAKE_TOOL_NAMES)
    wake_tool = by_name["tv_wake"]
    assert wake_tool.input_schema.get("properties", {}) == {}
    ann = wake_tool.annotations
    assert (ann.read_only_hint, ann.destructive_hint, ann.idempotent_hint, ann.open_world_hint) == (False, False, True, False)
    assert caps.structured_content["wake"] == {"enabled": True, "tool": "tv_wake", "port": 9, "packets": 2,
                                               "confirms_awake": False, "confirm_with": "tv_status"}
    assert "tv_wake" in caps.structured_content["tools"]
    assert not result.is_error
    data = result.structured_content
    assert data["requested"] is True and data["packets_sent"] == 2 and data["awake_verified"] is False
    # The second call is inside the cooldown; whatever arguments a client adds, the target never changes.
    assert again.is_error and "less than" in again.content[0].text
    assert sender.sent == [(magic_packet(MAC), (BROADCAST, 9))] * 2
    assert no_tcp == []


def test_wake_server_instructions_say_it_does_not_confirm(key_file):
    from quiettv_mcp.server import WAKE_INSTRUCTIONS
    config = wake_config(key_file)
    assert WAKE_INSTRUCTIONS in build_server(config, device=OfflineTV(config, udp_sender=Recorder())).instructions


# -- CLI, doctor and setup ---------------------------------------------------

def _cli(tmp_path, data, factory, *argv):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps(data), encoding="utf-8")
    args = cli._parser().parse_args(["run", "--config", str(cfg), *argv])
    return cli._run(cfg, args, device_factory=factory)


def test_run_wake_sends_and_reports_requested_only(tmp_path, key_file, capsys, no_tcp):
    sender = Recorder()
    code = _cli(tmp_path, config_dict(key_file, ip=EXAMPLE_IP, wake=ON),
                lambda c: OfflineTV(c, udp_sender=sender), "wake")
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["requested"] is True and out["awake_verified"] is False
    assert sender.sent == [(magic_packet(MAC), (BROADCAST, 9))]


def test_run_wake_refuses_when_not_enabled(tmp_path, key_file, capsys, no_tcp):
    sender = Recorder()
    code = _cli(tmp_path, config_dict(key_file, ip=EXAMPLE_IP), lambda c: OfflineTV(c, udp_sender=sender), "wake")
    assert code == 1
    assert "refused: Wake-on-LAN is off" in capsys.readouterr().err
    assert sender.sent == []


def test_run_wake_takes_no_target_arguments():
    for extra in (["10.20.30.255"], ["--mac", MAC], ["--port", "9"]):
        with pytest.raises(SystemExit):
            cli._parser().parse_args(["run", "wake", *extra])


def test_doctor_reports_wake_and_sends_nothing(tmp_path, key_file, monkeypatch, no_tcp):
    import quiettv_mcp.setup_cli as sc
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps(config_dict(key_file, ip=EXAMPLE_IP, wake={"enabled": True, "packets": 2})),
                   encoding="utf-8")
    sender = Recorder()
    out = io.StringIO()
    run_doctor(cfg, out=out, device_factory=lambda c: OfflineTV(c, udp_sender=sender))
    assert f"[PASS] wake: enabled: 2 packet(s) to {BROADCAST} port 9" in out.getvalue()
    assert sender.sent == []
    off = io.StringIO()
    cfg.write_text(json.dumps(config_dict(key_file, ip=EXAMPLE_IP)), encoding="utf-8")
    run_doctor(cfg, out=off, device_factory=lambda c: OfflineTV(c, udp_sender=sender))
    assert "[WARN] wake: off (optional" in off.getvalue()


def test_setup_can_opt_in_to_wake(tmp_path):
    cfg = tmp_path / "cfg" / "config.json"
    answers = iter(["UN43U8000FBXZA", "1300.8", EXAMPLE_IP, MAC, "", "", "y"])
    out = io.StringIO()
    run_setup(cfg, input_fn=lambda prompt: next(answers), secret_fn=lambda prompt: "typed-secret-0123456789abcdef",
              out=out)
    config = load_config(cfg)
    assert config.wake is not None and str(config.wake.broadcast) == BROADCAST
    assert json.loads(cfg.read_text(encoding="utf-8"))["wake"] == {"enabled": True, "port": 9, "packets": 1}
    assert "Network Standby" in out.getvalue()

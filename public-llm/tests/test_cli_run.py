"""The human CLI (`quiettv-mcp run ...`) uses the same checked device path as the MCP tools."""
from __future__ import annotations

import json

import pytest

import quiettv_mcp.cli as cli
from quiettv_mcp.capture import PNG_SIGNATURE
from quiettv_mcp.config import load_config
from quiettv_mcp.device import QuietTV


@pytest.fixture
def run(tmp_path, key_file, fake, monkeypatch):
    from conftest import config_dict
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps(config_dict(key_file)), encoding="utf-8")
    monkeypatch.setattr(cli, "load_config", lambda p: load_config(p, allow_loopback_fixture=True))
    factory = lambda c: QuietTV(c, identity_port=fake.identity_port, control_port=fake.control_port)  # noqa: E731

    def go(*argv):
        args = cli._parser().parse_args(["run", "--config", str(cfg), *argv])
        return cli._run(cfg, args, device_factory=factory)
    return go


def test_run_status_key_and_capture(run, fake, tmp_path, capsys, key):
    assert run("status") == 0
    assert run("key", "right") == 0
    out = tmp_path / "shot.png"
    assert run("capture", "--out", str(out)) == 0
    assert out.read_bytes().startswith(PNG_SIGNATURE)
    assert run("capture", "--out", str(out)) == 1  # never overwrites
    text = capsys.readouterr()
    assert key not in text.out + text.err
    assert fake.hits["POST /key/106"] == 1


def test_run_home_injects_home_key(run, fake):
    """Regression (live defect): `run home` must press key 63, not use the ineffective POST /home."""
    assert run("home") == 0
    assert fake.hits["POST /key/63"] == 1 and fake.hits["POST /home"] == 0
    assert fake.foreground == "home"
    fake.key_exit_code = 1
    assert run("home") == 1


def test_run_refuses_on_identity_mismatch(run, fake, capsys):
    fake.device["wifiMac"] = "02:00:00:00:00:0B"
    assert run("home") == 1
    assert "refused" in capsys.readouterr().err
    assert fake.control_hits() == 0


@pytest.mark.parametrize("argv", [["key", "power"], ["launch", "com.example.evil"]])
def test_run_rejects_unlisted_values(argv):
    with pytest.raises(SystemExit):
        cli._parser().parse_args(["run", *argv])

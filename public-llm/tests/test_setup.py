from __future__ import annotations

import io
import json
import tomllib
from pathlib import Path

import pytest
from conftest import EXAMPLE_IP

from quiettv_mcp.config import ConfigError, load_config
from quiettv_mcp.setup_cli import client_fragments, run_doctor, run_setup, write_client_fragments


def _answers(*values):
    it = iter(values)
    return lambda prompt: next(it)


def test_setup_with_hidden_key_writes_private_files(tmp_path):
    cfg = tmp_path / "cfg" / "config.json"
    secret = "typed-secret-0123456789abcdef"
    out = io.StringIO()
    run_setup(cfg, input_fn=_answers("UN43U8000FBXZA", "1300.8", EXAMPLE_IP, "02-00-00-00-00-01", "", "", ""),
              secret_fn=lambda prompt: secret, out=out)
    config = load_config(cfg)
    assert config.tv_mac == "02:00:00:00:00:01"
    assert config.wake is None and "wake" not in json.loads(cfg.read_text(encoding="utf-8"))
    assert config.control_key_file.read_text(encoding="ascii").strip() == secret
    assert secret not in cfg.read_text(encoding="utf-8")
    assert secret not in out.getvalue()


def test_setup_rejects_unsupported_model_and_writes_nothing(tmp_path):
    cfg = tmp_path / "config.json"
    with pytest.raises(ConfigError):
        run_setup(cfg, input_fn=_answers("QN90BAFXZA", "QN65", "X"), out=io.StringIO())
    assert not cfg.exists()


def test_setup_keeps_existing_config_unless_confirmed(config_file):
    before = config_file.read_text(encoding="utf-8")
    with pytest.raises(ConfigError, match="kept"):
        run_setup(config_file, input_fn=_answers("n"), out=io.StringIO())
    assert config_file.read_text(encoding="utf-8") == before


def test_client_fragments_parse_in_each_official_shape(config_file):
    frags = client_fragments(config_file, command=("/opt/venv/bin/python", ["-m", "quiettv_mcp", "serve", "--config", str(config_file)]))
    claude = json.loads(frags["claude-desktop"])["mcpServers"]["quiettv"]
    assert claude["command"] == "/opt/venv/bin/python" and claude["args"][-1] == str(config_file)
    codex = tomllib.loads(frags["codex"])["mcp_servers"]["quiettv"]
    assert codex["args"] == claude["args"]
    opencode = json.loads(frags["opencode"])["mcp"]["quiettv"]
    assert opencode["type"] == "local" and opencode["command"][0] == "/opt/venv/bin/python"
    assert all("key" not in text.lower().replace("control_key_file", "") for text in frags.values())


def test_claude_code_fragment_is_its_mcp_add_command(config_file, monkeypatch):
    import shlex
    import subprocess

    import quiettv_mcp.setup_cli as sc
    args = ["-m", "quiettv_mcp", "serve", "--config", "/path with space/config.json"]
    expected = ["claude", "mcp", "add", "--transport", "stdio", "--scope", "user", "quiettv", "--",
                "/opt/venv/bin/python", *args]
    monkeypatch.setattr(sc.sys, "platform", "linux")
    line = client_fragments(config_file, command=("/opt/venv/bin/python", args))["claude-code"].strip()
    assert shlex.split(line) == expected
    monkeypatch.setattr(sc.sys, "platform", "win32")
    line = client_fragments(config_file, command=("/opt/venv/bin/python", args))["claude-code"].strip()
    assert line == subprocess.list2cmdline(expected) and '"/path with space/config.json"' in line


def test_fragments_never_overwrite_or_touch_global_settings(config_file, tmp_path, monkeypatch):
    out_dir = tmp_path / "fragments"
    written = write_client_fragments(config_file, ["codex"], out_dir, io.StringIO())
    assert written[0].exists()
    with pytest.raises(ConfigError, match="already exists"):
        write_client_fragments(config_file, ["codex"], out_dir, io.StringIO())
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    with pytest.raises(ConfigError, match="global settings"):
        write_client_fragments(config_file, ["codex"], tmp_path / ".codex", io.StringIO())
    assert not (tmp_path / ".codex").exists()


def test_doctor_offline_reports_without_key_value(config_file, key):
    out = io.StringIO()
    code = run_doctor(config_file, out=out)
    text = out.getvalue()
    assert key not in text
    assert "[PASS] control key file" in text
    assert "skipped (run with --online" in text
    assert code in (0, 1)  # same-LAN depends on the test machine's routes to the example address


def test_doctor_online_against_fake(tmp_path, key_file, fake, key, monkeypatch):
    from conftest import config_dict

    import quiettv_mcp.setup_cli as sc
    from quiettv_mcp.device import QuietTV
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps(config_dict(key_file)), encoding="utf-8")
    original = sc.load_config
    monkeypatch.setattr(sc, "load_config", lambda p: original(p, allow_loopback_fixture=True))
    out = io.StringIO()
    code = run_doctor(cfg, online=True, out=out,
                      device_factory=lambda c: QuietTV(c, identity_port=fake.identity_port, control_port=fake.control_port))
    text = out.getvalue()
    assert code == 0, text
    assert "[PASS] identity" in text and "[PASS] capture capability: advertised" in text
    assert key not in text


def test_cli_has_no_secret_arguments():
    from quiettv_mcp.cli import _parser
    help_text = " ".join(action.dest for sub in _parser()._subparsers._group_actions[0].choices.values() for action in sub._actions)
    for word in ("key", "token", "secret", "password"):
        assert word not in help_text.replace("control_key_file", "")


def test_config_path_resolution_order(monkeypatch, tmp_path):
    from quiettv_mcp.config import resolve_config_path
    monkeypatch.setenv("QUIETTV_MCP_CONFIG", str(tmp_path / "env.json"))
    assert resolve_config_path(None) == tmp_path / "env.json"
    assert resolve_config_path(str(tmp_path / "cli.json")) == Path(tmp_path / "cli.json")

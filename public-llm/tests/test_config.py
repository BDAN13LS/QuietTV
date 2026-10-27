from __future__ import annotations

import json

import pytest
from conftest import EXAMPLE_IP, config_dict

from quiettv_mcp.config import ConfigError, load_config, validate_config
from quiettv_mcp.profiles import APP_IDS, KEY_CODES, PROFILES, AppId, KeyName


def test_valid_config_loads(config_file):
    config = load_config(config_file)
    assert str(config.tv_ip) == EXAMPLE_IP
    assert config.fixture_mode is False
    assert config.allowed_apps == APP_IDS


@pytest.mark.parametrize("ip", ["tv.local", "8.8.8.8", "127.0.0.1", "169.254.1.2", "224.0.0.1", "0.0.0.0", "", "fe80::1"])
def test_rejects_non_lan_targets(key_file, ip):
    with pytest.raises(ConfigError):
        validate_config(config_dict(key_file, ip=ip))


def test_loopback_only_via_in_process_fixture_flag(key_file):
    config = validate_config(config_dict(key_file), allow_loopback_fixture=True)
    assert config.fixture_mode is True


@pytest.mark.parametrize("change", [
    {"profile": "QN90B-generic"},
    {"profile": None},
    {"schema": "other/1"},
    {"control_key_file": "relative/control.key"},
    {"lan_prefix": 8},
    {"capture_max_age_s": 0},
    {"allowed_apps": ["com.example.not-allowlisted"]},
    {"allowed_apps": []},
    {"unexpected": True},
])
def test_rejects_bad_top_level(key_file, change):
    data = config_dict(key_file, ip=EXAMPLE_IP)
    data.update(change)
    with pytest.raises(ConfigError):
        validate_config(data)


@pytest.mark.parametrize("field,value", [
    ("model", "QN90BAFXZA"),
    ("model", "un43u8000fbxza "),
    ("firmware", "1300.9"),
    ("firmware", "Unknown"),
    ("mac", "FF:FF:FF:FF:FF:FF"),
    ("mac", "01:00:00:00:00:01"),
    ("mac", "02:00:00:00:00"),
    ("mac", "02:00:00:00:00:01;rm"),
])
def test_rejects_bad_tv_fields(key_file, field, value):
    data = config_dict(key_file, ip=EXAMPLE_IP)
    data["tv"][field] = value
    with pytest.raises(ConfigError):
        validate_config(data)


def test_firmware_accepts_build_prefixed_form(key_file):
    data = config_dict(key_file, ip=EXAMPLE_IP)
    data["tv"]["firmware"] = "T-X22UDFAKUC-1300.8"
    assert validate_config(data).profile.firmware == "1300.8"


def test_missing_config_message_points_to_setup(tmp_path):
    with pytest.raises(ConfigError, match="quiettv-mcp setup"):
        load_config(tmp_path / "missing.json")


def test_oversize_and_invalid_json_rejected(tmp_path):
    big = tmp_path / "big.json"
    big.write_bytes(b" " * (70 * 1024))
    with pytest.raises(ConfigError):
        load_config(big)
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(bad)


def test_schema_literals_match_profile():
    # The Literal types feed the MCP input schema; they must equal the profile allowlists exactly.
    assert set(KeyName.__args__) == set(KEY_CODES)
    assert set(AppId.__args__) == set(APP_IDS)
    assert list(PROFILES) == ["UN43U8000FBXZA-1300.8"]


def test_config_never_contains_key_material(config_file, key):
    assert key not in config_file.read_text(encoding="utf-8")
    assert "control_key_file" in json.loads(config_file.read_text(encoding="utf-8"))

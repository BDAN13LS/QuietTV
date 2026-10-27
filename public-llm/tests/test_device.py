from __future__ import annotations

import json
import logging

import pytest

from quiettv_mcp.device import DeviceError, IdentityMismatch, QuietTV


def test_accept_path_status(device, fake, key):
    result = device.status()
    assert result["identity"]["mac_verified"] is True
    assert result["identity"]["firmware"] == "unverified"  # the fake reports "Unknown", like real TVs often do
    assert result["status"]["ready"] is True
    assert "capture" in result["status"]["service_capabilities"]
    assert "FIXTURE MODE" in result["mode"]
    assert fake.hits["GET /status"] == 1
    # The key went only to the control service, never to the identity endpoint.
    identity_headers = [h for h in fake.seen_headers if "x-quiettv-key" not in h]
    assert identity_headers, "identity request should carry no key"
    assert key not in json.dumps(result)


def test_status_withholds_unlisted_fields(device, fake):
    fake.status_doc["secretConfig"] = {"token": "abc"}
    result = device.status()
    assert "secretConfig" not in json.dumps(result)
    assert "0f0e0d0c" not in json.dumps(result)  # boot id is not whitelisted
    assert result["status"]["fields_withheld"] >= 2


@pytest.mark.parametrize("field,value", [
    ("wifiMac", "02:00:00:00:00:02"),
    ("wifiMac", None),
    ("modelName", "QN90BAFXZA"),
    ("firmwareVersion", "T-OTHER-9999.1"),
    ("OS", "webOS"),
    ("ip", "127.0.0.2"),
])
def test_identity_mismatch_sends_no_control_request(device, fake, field, value):
    if value is None:
        fake.device.pop(field)
    else:
        fake.device[field] = value
    for call in (device.status, device.home, lambda: device.key("ok"), lambda: device.launch("quiet.iptv"),
                 device.diagnostics, device.capture):
        with pytest.raises(IdentityMismatch):
            call()
    assert fake.control_hits() == 0


def test_negative_control_identity_check_is_what_blocks(device, fake, monkeypatch):
    """Positive control for the test above: with the identity check bypassed the mismatch DOES reach 4190."""
    fake.device["wifiMac"] = "02:00:00:00:00:02"
    monkeypatch.setattr(QuietTV, "verify_identity", lambda self: type("I", (), {"as_dict": lambda s: {}})())
    device.status()
    assert fake.control_hits() == 1


def test_firmware_match_reported_as_match(device, fake):
    fake.device["firmwareVersion"] = "T-X22UDFAKUC-1300.8"
    assert device.status()["identity"]["firmware"] == "match"


def test_recorded_full_build_matches_exactly_only():
    from quiettv_mcp.profiles import UN43U8000FBXZA_1300_8 as p
    assert p.firmware_status("T-X22UDFAKUC-0090-1300.8") == "match"
    assert p.firmware_status(" t-x22udfakuc-0090-1300.8 ") == "match"
    for near in ("T-X22UDFAKUC-0091-1300.8", "T-X22UDFAKUC-0090-1300.9", "T-X22UDFAKUC-0090-1300.8.1",
                 "X-T-X22UDFAKUC-0090-1300.8", "T-X22UDFAKUC-0090", "T-OTHER-0090-1300.8"):
        assert p.firmware_status(near) == "mismatch", near
    assert p.firmware_status("Unknown") == "unreported"


def test_missing_key_file_sends_nothing(device, fake, key_file):
    key_file.unlink()
    with pytest.raises(DeviceError, match="no request sent"):
        device.status()
    assert sum(fake.hits.values()) == 0


@pytest.mark.parametrize("content", [b"short", b"has space in the middle of key", b"line1-0123456789\nline2-0123456789",
                                     b"\xff\xfe-not-ascii-0123456789", b"x" * 300])
def test_malformed_key_file_sends_nothing(device, fake, key_file, content):
    key_file.write_bytes(content)
    with pytest.raises(DeviceError):
        device.status()
    assert sum(fake.hits.values()) == 0


def test_wrong_key_403_message_and_no_leak(device, fake, key_file, caplog):
    key_file.write_text("wrong-key-0123456789abcdef\n", encoding="ascii")
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(DeviceError, match="403") as info:
            device.status()
    assert "wrong-key" not in str(info.value)
    assert "allowlist" in str(info.value)
    assert "wrong-key" not in caplog.text


def test_invalid_key_and_app_rejected_locally(device, fake):
    with pytest.raises(DeviceError):
        device.key("power")
    with pytest.raises(DeviceError):
        device.key("ok; reboot")
    with pytest.raises(DeviceError):
        device.launch("com.example.evil")
    with pytest.raises(DeviceError):
        device.launch("../reboot")
    assert sum(fake.hits.values()) == 0


def test_owner_allowed_apps_subset_enforced(fake, key_file, tmp_path):
    from conftest import config_dict

    from quiettv_mcp.config import validate_config
    config = validate_config(config_dict(key_file, allowed_apps=["quiet.iptv"]), allow_loopback_fixture=True)
    tv = QuietTV(config, identity_port=fake.identity_port, control_port=fake.control_port)
    assert tv.launch("quiet.iptv")["accepted"] is True
    with pytest.raises(DeviceError):
        tv.launch("org.tizen.browser")


def test_home_injects_remote_home_key_not_ineffective_post_home(device, fake):
    """Regression (live defect): POST /home was accepted but left another app in the foreground."""
    result = device.home()
    assert fake.hits["POST /key/63"] == 1
    assert fake.hits["POST /home"] == 0
    assert fake.foreground == "home"
    assert result["accepted"] is True and result["code"] == 63
    assert result["foreground_verified"] is False


def test_home_failed_injection_is_an_error(device, fake):
    fake.key_exit_code = 1
    with pytest.raises(DeviceError, match="exit code 1"):
        device.home()
    assert fake.foreground == "other.app"


def test_controls_use_exact_routes(device, fake):
    device.home()
    assert device.key("volume_up")["code"] == 68
    assert device.key("back")["injected"] is True
    device.launch("org.tizen.netflix-app")
    assert fake.hits["POST /key/63"] == 1
    assert fake.hits["POST /key/68"] == 1
    assert fake.hits["POST /key/1"] == 1
    assert fake.hits["POST /launch/org.tizen.netflix-app"] == 1
    forbidden = ("maintenance", "local-developer", "reboot", "set-home", "restore-home", "restart-dashboard",
                 "snapshot", "player-key", "screenshot", "ui-state")
    assert not any(any(f in route for f in forbidden) for route in fake.hits)


def test_proxy_environment_is_ignored(device, fake, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    assert device.status()["status"]["ready"] is True


def test_lan_check_rejects_target_outside_prefix(device, monkeypatch):
    import dataclasses
    import ipaddress
    import socket as _socket
    monkeypatch.setattr(device, "config", dataclasses.replace(device.config, tv_ip=ipaddress.IPv4Address("10.20.99.1")))

    class FakeSock:
        def __init__(self, *a):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def connect(self, addr):
            pass

        def getsockname(self):
            return ("10.20.30.5", 5000)
    monkeypatch.setattr(_socket, "socket", FakeSock)
    with pytest.raises(DeviceError, match="LAN"):
        device.check_lan()

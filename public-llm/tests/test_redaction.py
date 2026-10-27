from __future__ import annotations

import json

from quiettv_mcp.redact import redact_text

PLANTED = [
    "X-QuietTV-Key: planted-header-secret-1234",
    "http://user:hunter2pass@cdn.example.net/live/abc/stream.m3u8?token=querysecret99",
    "token=assignsecret42",
    '"api_key": "jsonsecret77"',
    "C:\\Users\\alice\\Documents\\private.txt",
    "/home/owner/share/tmp/sdk_tools/quiet-tv",
    "/Users/bob/Library/thing",
    "10.20.30.77",
    "02:00:00:00:00:09",
    "Authorization: Bearer bearersecret55",
    "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",
]
FORBIDDEN = ["planted-header-secret", "hunter2pass", "querysecret99", "assignsecret42", "jsonsecret77", "alice", "bob",
             "10.20.30.77", "02:00:00:00:00:09", "bearersecret55", "a1b2c3d4e5f6a7b8c9d0", "/live/abc", "owner/share"]


def test_redact_text_removes_every_planted_secret():
    out = redact_text("\n".join(PLANTED), secrets=("literal-owner-key-000111",))
    for bad in FORBIDDEN:
        assert bad not in out, bad
    assert "cdn.example.net" in out  # host kept for diagnosis
    assert "127.0.0.1" in redact_text("bound to 127.0.0.1")


def test_literal_secret_removed():
    assert "literal-owner-key-000111" not in redact_text("x literal-owner-key-000111 y", secrets=("literal-owner-key-000111",))


def test_diagnostics_never_return_raw_settings(device, fake, key):
    config_json = json.dumps({
        "developerIp": "10.20.30.77",
        "streamUrl": "http://user:hunter2pass@cdn.example.net/live/abc/stream.m3u8?token=querysecret99",
        "api_key": "jsonsecret77",
        "visible": True,
        "volume": 9,
    }, indent=2)
    fake.diag_stdout = "\n".join([
        "home_owner=quiet",
        "10.20.30.77",
        f"X-QuietTV-Key: {key}",
        config_json,
        '{"playing": false, "url": "http://cdn.example.net/x?sig=querysecret99"}',
        "C:\\Users\\alice\\Desktop",
    ])
    result = device.diagnostics()
    text = json.dumps(result)
    for bad in FORBIDDEN + [key]:
        assert bad not in text, bad
    fields = result["diagnostics"]["json_sections"][0]["json_fields"]
    assert fields["visible"] is True and fields["volume"] == 9
    assert fields["streamUrl"] == "[text withheld]"


def test_negative_control_raw_passthrough_would_leak(device, fake, key, monkeypatch):
    """Proves the diagnostics test can fail: without the summarizer the planted values reach the output."""
    import quiettv_mcp.device as dev
    monkeypatch.setattr(dev, "summarize_diagnostics", lambda doc, secrets: doc)
    fake.diag_stdout = f"X-QuietTV-Key: {key}\n10.20.30.77"
    text = json.dumps(device.diagnostics())
    assert key in text and "10.20.30.77" in text

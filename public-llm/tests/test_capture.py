from __future__ import annotations

import pytest
from fake_tv import make_png

from quiettv_mcp import capture as cap
from quiettv_mcp.device import DeviceError


def test_fresh_capture_accepted_and_saved_privately(device, fake, config):
    shot, meta = device.capture()
    assert shot.png.startswith(cap.PNG_SIGNATURE)
    assert (shot.width, shot.height) == (4, 3)
    assert meta["capture"]["freshness"] == "nonce-bound"
    assert "black" in meta["capture"]["layer_note"]
    assert meta["saved_to_private_dir"] is True
    assert len(list(config.capture_dir.glob("capture-*.png"))) == 1
    assert fake.hits["POST /capture"] == 1
    assert fake.hits["GET /screenshot"] == 0


def test_second_capture_gets_new_id(device, fake):
    a, _ = device.capture()
    b, _ = device.capture()
    assert a.capture_id != b.capture_id


@pytest.mark.parametrize("mode,match", [
    ("html", "not image/png"),
    ("html-as-png", "not a PNG"),
    ("oversize", "larger than"),
    ("oversize-undeclared", "larger than"),
    ("cacheable", "no-store"),
    ("stale-time", "freshness window"),
    ("no-id", "Capture-Id"),
    ("wrong-nonce", "nonce mismatch"),
    ("bad-crc", "checksum"),
])
def test_rejected_capture_responses(device, fake, mode, match):
    fake.capture_mode = mode
    with pytest.raises(DeviceError, match=match):
        device.capture()
    assert fake.hits["GET /screenshot"] == 0


def test_repeated_capture_id_rejected_as_stale(device, fake):
    fake.capture_mode = "repeat-id"
    device.capture()
    with pytest.raises(DeviceError, match="already returned"):
        device.capture()


def test_missing_capture_route_reports_not_available_without_fallback(device, fake):
    fake.capture_mode = "absent"
    with pytest.raises(DeviceError, match="does not offer POST /capture"):
        device.capture()
    assert fake.hits["GET /screenshot"] == 0


def test_clock_bound_when_nonce_not_echoed(device, fake):
    fake.capture_mode = "no-nonce"
    _, meta = device.capture()
    assert meta["capture"]["freshness"] == "clock-bound"


def test_png_dimension_guard():
    import struct
    import zlib
    png = make_png()
    ihdr = struct.pack(">IIBBBBB", 100000, 1, 8, 2, 0, 0, 0)
    huge = png[:16] + ihdr + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr) & 0xFFFFFFFF) + png[33:]
    with pytest.raises(cap.CaptureError, match="dimensions"):
        cap.png_dimensions(huge)
    with pytest.raises(cap.CaptureError, match="truncated"):
        cap.png_dimensions(png[:-12])

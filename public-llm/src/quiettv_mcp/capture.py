"""Validation for the TV service's POST /capture response.

Only a fresh PNG is accepted. The legacy GET /screenshot route serves whatever
file was last written and is never used as a fallback.
"""
from __future__ import annotations

import hashlib
import re
import struct
import zlib
from dataclasses import dataclass

from .transport import Response

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_CAPTURE_BYTES = 8 * 1024 * 1024
MAX_DIMENSION = 8192
CLOCK_SKEW_S = 5
ID_HEADER = "X-QuietTV-Capture-Id"
TIME_HEADER = "X-QuietTV-Captured-At"
NONCE_HEADER = "X-QuietTV-Capture-Nonce"
_ID = re.compile(r"^[A-Za-z0-9._-]{8,128}$")


class CaptureError(Exception):
    """The response is not a fresh, well-formed capture."""


class CaptureUnavailable(CaptureError):
    """The TV service does not offer POST /capture."""


@dataclass(frozen=True)
class Capture:
    png: bytes
    capture_id: str
    captured_at_ms: int
    width: int
    height: int
    sha256: str
    freshness: str

    def metadata(self) -> dict:
        return {
            "capture_id": self.capture_id,
            "captured_at_ms": self.captured_at_ms,
            "width": self.width,
            "height": self.height,
            "bytes": len(self.png),
            "sha256": self.sha256,
            "freshness": self.freshness,
            "layer_note": ("Captures the UI/graphics plane only. The TV's hardware video plane reads as black, "
                           "so a black or empty video area is expected and does not prove video or sound work; "
                           "verify playback and audio directly on the TV."),
        }


def png_dimensions(data: bytes) -> tuple[int, int]:
    if len(data) < 8 + 25 + 12 or not data.startswith(PNG_SIGNATURE):
        raise CaptureError("body is not a PNG (signature mismatch)")
    length, ctype = struct.unpack(">I4s", data[8:16])
    if ctype != b"IHDR" or length != 13:
        raise CaptureError("PNG does not start with a valid IHDR chunk")
    ihdr = data[16:29]
    (crc,) = struct.unpack(">I", data[29:33])
    if zlib.crc32(b"IHDR" + ihdr) & 0xFFFFFFFF != crc:
        raise CaptureError("PNG IHDR checksum mismatch")
    width, height = struct.unpack(">II", ihdr[:8])
    if not (0 < width <= MAX_DIMENSION and 0 < height <= MAX_DIMENSION):
        raise CaptureError("PNG dimensions out of range")
    if data[-12:] != b"\x00\x00\x00\x00IEND\xaeB`\x82":
        raise CaptureError("PNG is truncated (no IEND chunk)")
    return width, height


def validate_capture(resp: Response, *, nonce: str, now_ms: int, max_age_s: int, seen_ids: set[str]) -> Capture:
    if resp.status == 404:
        raise CaptureUnavailable("the TV service does not offer POST /capture on this device")
    if resp.status != 200:
        raise CaptureError(f"capture failed with HTTP {resp.status}")
    ctype = (resp.header("Content-Type") or "").split(";")[0].strip().lower()
    if ctype != "image/png":
        raise CaptureError("capture response is not image/png")
    cache = (resp.header("Cache-Control") or "").lower()
    if "no-store" not in [part.strip() for part in cache.split(",")]:
        raise CaptureError("capture response is cacheable (Cache-Control: no-store missing)")
    capture_id = resp.header(ID_HEADER) or ""
    if not _ID.match(capture_id):
        raise CaptureError(f"capture response lacks a valid {ID_HEADER} header")
    if capture_id in seen_ids:
        raise CaptureError("capture id was already returned once; the image may be stale")
    stamp = resp.header(TIME_HEADER) or ""
    if not stamp.isdigit() or len(stamp) > 15:
        raise CaptureError(f"capture response lacks a valid {TIME_HEADER} header (epoch milliseconds)")
    captured_at = int(stamp)
    age_ms = now_ms - captured_at
    if age_ms < -CLOCK_SKEW_S * 1000 or age_ms > max_age_s * 1000:
        raise CaptureError(f"capture timestamp is outside the {max_age_s}s freshness window")
    echoed = resp.header(NONCE_HEADER)
    if echoed is not None and echoed != nonce:
        raise CaptureError("capture nonce mismatch; the image belongs to another request")
    width, height = png_dimensions(resp.body)
    seen_ids.add(capture_id)
    return Capture(
        png=resp.body,
        capture_id=capture_id,
        captured_at_ms=captured_at,
        width=width,
        height=height,
        sha256=hashlib.sha256(resp.body).hexdigest(),
        freshness="nonce-bound" if echoed is not None else "clock-bound",
    )

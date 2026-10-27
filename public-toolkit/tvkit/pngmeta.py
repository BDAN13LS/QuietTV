"""Remove textual and provenance metadata from a PNG without touching the image.

Stdlib only. The file is split into chunks and every chunk's CRC is checked;
any damage, truncation, bytes after IEND, or IHDR/IEND out of place refuses
the file. Only these chunk types are dropped:

- ``tEXt``, ``zTXt``, ``iTXt``: free text (author, comments, software, paths);
- ``eXIf``: EXIF, which can carry camera, time and location;
- ``caBX``: a C2PA provenance manifest (signer, certificates, addresses).

A text chunk whose keyword is exactly ``Copyright`` is kept: copyright notices
are never removed. Every other chunk, IDAT included, is copied byte for byte
with its original CRC. Pixels are never decoded or re-encoded.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

SIGNATURE = b"\x89PNG\r\n\x1a\n"
DROPPED_TYPES = frozenset({b"tEXt", b"zTXt", b"iTXt", b"eXIf", b"caBX"})
TEXT_TYPES = frozenset({b"tEXt", b"zTXt", b"iTXt"})
KEPT_TEXT_KEYWORDS = frozenset({b"Copyright"})
MAX_CHUNK = 0x7FFFFFFF


class PngError(ValueError):
    pass


@dataclass(frozen=True)
class Chunk:
    type: bytes
    data: bytes
    raw: bytes  # length + type + data + crc, exactly as read


def parse_chunks(data: bytes) -> list[Chunk]:
    if not data.startswith(SIGNATURE):
        raise PngError("not a PNG file (bad signature)")
    chunks: list[Chunk] = []
    pos = len(SIGNATURE)
    while pos < len(data):
        if len(data) - pos < 12:
            raise PngError("truncated chunk header")
        length, ctype = struct.unpack(">I4s", data[pos:pos + 8])
        if length > MAX_CHUNK:
            raise PngError("chunk length out of range")
        if not all(65 <= b <= 90 or 97 <= b <= 122 for b in ctype):
            raise PngError("invalid chunk type")
        end = pos + 12 + length
        if end > len(data):
            raise PngError("truncated chunk")
        body = data[pos + 8:pos + 8 + length]
        (crc,) = struct.unpack(">I", data[end - 4:end])
        if zlib.crc32(ctype + body) & 0xFFFFFFFF != crc:
            raise PngError(f"CRC mismatch in a {ctype.decode('ascii')} chunk")
        chunks.append(Chunk(ctype, body, data[pos:end]))
        pos = end
        if ctype == b"IEND":
            break
    if pos != len(data):
        raise PngError("bytes after IEND")
    if not chunks or chunks[0].type != b"IHDR":
        raise PngError("IHDR is not the first chunk")
    if chunks[-1].type != b"IEND" or sum(c.type == b"IEND" for c in chunks) != 1:
        raise PngError("IEND is missing or not last")
    if not any(c.type == b"IDAT" for c in chunks):
        raise PngError("no image data")
    return chunks


def _drop(chunk: Chunk) -> bool:
    if chunk.type not in DROPPED_TYPES:
        return False
    if chunk.type in TEXT_TYPES and chunk.data.split(b"\x00", 1)[0] in KEPT_TEXT_KEYWORDS:
        return False
    return True


def strip_metadata(data: bytes) -> tuple[bytes, list[str]]:
    """Return the filtered PNG and the chunk types removed (in file order)."""
    chunks = parse_chunks(data)
    kept = [c for c in chunks if not _drop(c)]
    removed = [c.type.decode("ascii") for c in chunks if _drop(c)]
    out = SIGNATURE + b"".join(c.raw for c in kept)
    # The result must itself be a valid PNG with identical image chunks.
    again = parse_chunks(out)
    if [c.raw for c in again if c.type == b"IDAT"] != [c.raw for c in chunks if c.type == b"IDAT"]:
        raise PngError("image data changed while filtering")
    return out, removed

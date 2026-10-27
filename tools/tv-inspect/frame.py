"""Strict decoder for QuietTVInspect --inventory-framed output (quiettv-inspection/1).

Samsung's .NET runtime appends GC diagnostics to stdout, even with stderr discarded, so raw
stdout is not one JSON document. The framed mode wraps the document as

    QUIETTV-FRAME/1 <length> <sha256>\\n<payload>\\nQUIETTV-FRAME-END/1\\n

where <length> is the exact payload byte count (decimal, no leading zeros), <sha256> the
lowercase hex SHA-256 of the payload, and the payload one line of compact UTF-8 JSON.

decode_frame() accepts exactly one frame. Outside it, only whole lines that match the known,
bounded GC diagnostic shapes are allowed, before and after; any other text, a second frame, a
truncated or corrupt frame, or a payload that is not strict JSON in the expected schema is
refused with FrameError. The SDK's own "[exit:N]" trailer is transport, not part of this
format: the caller strips it before decoding. Standard library only.
"""

from __future__ import annotations

import hashlib
import json
import re

SCHEMA = "quiettv-inspection/1"
FRAME_TAG = b"QUIETTV-FRAME/1"
END_TAG = b"QUIETTV-FRAME-END/1"
MAX_PAYLOAD_BYTES = 8 << 20  # matches Frame.MaxPayloadBytes in Inspector.cs
MAX_NOISE_LINES = 4096
MAX_NOISE_LINE_BYTES = 160
MAX_INPUT_BYTES = MAX_PAYLOAD_BYTES + 256 + MAX_NOISE_LINES * (MAX_NOISE_LINE_BYTES + 1)
MAX_RECORDS = 4096
MAX_PROBLEMS = 4 * MAX_RECORDS + 64
REGISTRY_DATABASE = "/opt/dbspace/.pkgmgr_parser.db"  # matches Inspector.DatabasePath in Inspector.cs

_TRAILER = b"\n" + END_TAG + b"\n"
_HEADER = re.compile(rb"QUIETTV-FRAME/1 (0|[1-9][0-9]{0,8}) ([0-9a-f]{64})\Z")
# The only text allowed outside the frame. These shapes are from the synthetic examples in the
# brief; widen them only against real captured lines, never to "ignore anything".
_GC_LINE = re.compile(
    rb"\[GC_INFO\] GC_Start #[0-9]{1,10}: Gen\([0-9]\), Time\([0-9]{1,10} ms\), Reason\([0-9]{1,3}\)\Z"
    rb"|\[GC_INFO\] GC_End #[0-9]{1,10}: Gen\([0-9]\), Time\([0-9]{1,10} ms\)\Z")

_TOP_KEYS = {"schema", "toolVersion", "status", "registryDatabase", "registryRecords", "observedRuntime", "problems"}
_RECORD_KEYS = {"Id", "Name", "Installed", "Disabled", "Launchable", "PackageId", "Version", "InstallRoot", "Reason"}
_RUNTIME_KEYS = {"tizenRelease", "tizenVersion", "machine", "model", "firmware", "region"}
_PROBLEM_KEYS = {"code", "subject", "fatal"}
_REASONS = {"installed", "root-absent-tombstone", "symlink-root-refused", "external-root-refused", "root-path-unsafe",
            "app-root-mismatch", "root-not-directory", "root-unreadable", "registry-root-missing"}
_IDENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_CODE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
_PRINTABLE = re.compile(r"[\x20-\x7e]{1,200}\Z")


class FrameError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def encode_frame(payload: bytes) -> bytes:
    """The same bytes Frame.Encode writes; for tests and round trips."""
    if type(payload) is not bytes or not 0 < len(payload) <= MAX_PAYLOAD_BYTES:
        raise FrameError("payload-size")
    if b"\n" in payload or b"\r" in payload:
        raise FrameError("payload-not-one-line")
    digest = hashlib.sha256(payload).hexdigest().encode("ascii")
    return FRAME_TAG + b" " + str(len(payload)).encode("ascii") + b" " + digest + b"\n" + payload + _TRAILER


def decode_frame(data: bytes) -> dict:
    """Return the verified document from one framed stdout capture, or raise FrameError."""
    if type(data) is not bytes:
        raise FrameError("input-not-bytes")
    if len(data) > MAX_INPUT_BYTES:
        raise FrameError("input-oversize")

    noise = 0
    pos = 0
    header = None
    while header is None:
        nl = data.find(b"\n", pos)
        if nl < 0:
            raise FrameError("no-frame")
        line = data[pos:nl]
        pos = nl + 1
        header = _HEADER.match(line)
        if header is None:
            noise = _check_noise(line, noise)

    length = int(header.group(1))
    digest = header.group(2).decode("ascii")
    if length > MAX_PAYLOAD_BYTES:
        raise FrameError("payload-oversize")
    payload = data[pos:pos + length]
    if len(payload) != length:
        raise FrameError("truncated")
    pos += length
    if data[pos:pos + len(_TRAILER)] != _TRAILER:
        raise FrameError("end-tag-mismatch")
    pos += len(_TRAILER)
    if hashlib.sha256(payload).hexdigest() != digest:
        raise FrameError("hash-mismatch")

    while pos < len(data):
        nl = data.find(b"\n", pos)
        if nl < 0:
            raise FrameError("unterminated-noise")
        noise = _check_noise(data[pos:nl], noise)
        pos = nl + 1

    return validate_document(_strict_json(payload))


def _check_noise(line: bytes, count: int) -> int:
    if len(line) > MAX_NOISE_LINE_BYTES or not _GC_LINE.match(line):
        raise FrameError("unknown-noise")
    if count + 1 > MAX_NOISE_LINES:
        raise FrameError("noise-oversize")
    return count + 1


def _strict_json(payload: bytes):
    if b"\n" in payload or b"\r" in payload:
        raise FrameError("payload-not-one-line")
    if payload[:1] != b"{" or payload[-1:] != b"}":
        raise FrameError("payload-not-object")
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise FrameError("payload-not-utf8") from None

    def pairs(items):
        keys = [k for k, _ in items]
        if len(keys) != len(set(keys)):
            raise FrameError("json-duplicate-key")
        return dict(items)

    def constant(_value):
        raise FrameError("json-non-finite")

    def no_float(_value):
        raise FrameError("json-float")

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=constant, parse_float=no_float)
    except FrameError:
        raise
    except (ValueError, RecursionError):
        raise FrameError("payload-not-json") from None


def _str(value, allow_none: bool = False) -> bool:
    return (allow_none and value is None) or (type(value) is str and 0 < len(value) <= 4096)


def validate_document(doc) -> dict:
    """Exact quiettv-inspection/1 shape; anything else raises FrameError."""
    if type(doc) is not dict or set(doc) != _TOP_KEYS:
        raise FrameError("schema-top-level")
    if doc["schema"] != SCHEMA or not _str(doc["toolVersion"]) or doc["registryDatabase"] != REGISTRY_DATABASE:
        raise FrameError("schema-identity")

    problems = doc["problems"]
    if type(problems) is not list or len(problems) > MAX_PROBLEMS:
        raise FrameError("schema-problems")
    fatal = False
    for p in problems:
        if (type(p) is not dict or set(p) != _PROBLEM_KEYS or type(p["code"]) is not str
                or not _CODE.match(p["code"]) or not _str(p["subject"]) or type(p["fatal"]) is not bool):
            raise FrameError("schema-problem")
        fatal |= p["fatal"]

    records = doc["registryRecords"]
    status = doc["status"]
    ok = status == "ok" and type(records) is list and not fatal
    failed = status == "failed" and records is None and fatal
    if not (ok or failed):
        raise FrameError("schema-status-inconsistent")
    if ok:
        _validate_records(records)

    _validate_runtime(doc["observedRuntime"])
    return doc


def _validate_records(records: list) -> None:
    if not records:
        # The collector refuses an empty registry (registry-empty), so an "ok" inventory is never empty.
        raise FrameError("schema-records-empty")
    if len(records) > MAX_RECORDS:
        raise FrameError("schema-records-oversize")
    previous = None
    folded = set()
    for r in records:
        if type(r) is not dict or set(r) != _RECORD_KEYS:
            raise FrameError("schema-record-keys")
        if (type(r["Id"]) is not str or not _IDENT.match(r["Id"]) or ".." in r["Id"]
                or type(r["PackageId"]) is not str or not _IDENT.match(r["PackageId"]) or ".." in r["PackageId"]):
            raise FrameError("schema-record-id")
        if not all(type(r[k]) is bool for k in ("Installed", "Disabled", "Launchable")):
            raise FrameError("schema-record-flags")
        if not (_str(r["Name"], True) and _str(r["Version"], True) and _str(r["InstallRoot"], True)):
            raise FrameError("schema-record-text")
        if r["Reason"] not in _REASONS or (r["Reason"] == "installed") != r["Installed"]:
            raise FrameError("schema-record-reason")
        if r["Installed"] and r["InstallRoot"] not in ("/opt/usr/apps/" + r["PackageId"], "/usr/apps/" + r["PackageId"]):
            raise FrameError("schema-record-root")
        if previous is not None and not previous < r["Id"]:
            raise FrameError("schema-record-order")
        if r["Id"].casefold() in folded:
            raise FrameError("schema-record-duplicate")
        folded.add(r["Id"].casefold())
        previous = r["Id"]


def _validate_runtime(runtime) -> None:
    if runtime is None:
        return
    if type(runtime) is not dict or set(runtime) != _RUNTIME_KEYS:
        raise FrameError("schema-runtime")
    if runtime["model"] is not None or runtime["firmware"] is not None or runtime["region"] is not None:
        raise FrameError("schema-runtime-guessed")
    if not (_str(runtime["tizenVersion"], True) and _str(runtime["machine"], True)):
        raise FrameError("schema-runtime")
    release = runtime["tizenRelease"]
    if release is not None:
        if (type(release) is not dict or set(release) != {"path", "lines"} or release["path"] != "/etc/tizen-release"
                or type(release["lines"]) is not list or len(release["lines"]) > 32
                or not all(type(x) is str and _PRINTABLE.match(x) for x in release["lines"])):
            raise FrameError("schema-runtime-release")

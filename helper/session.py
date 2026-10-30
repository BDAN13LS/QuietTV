"""QuietTV Helper session (D6, developer preview): one TV target, one bounded read-only check.

Everything the local Helper UI can do lives here; helper/server.py only adds HTTP.

What it does:
    set_target(ip)   explicit owner-typed private IPv4 (10/8, 172.16/12, 192.168/16) only
    start_check()    one background read of http://<ip>:8001/api/v2/ (the TV's public info page)
    cancel(job_id)   stop a running check: the reader sees it within 0.2 s and closes its socket
    shutdown()       cancel and join the reader thread (the server calls this when it stops)
    state()          what the UI shows: reachability, model support, exact-match gaps, gates
    refuse_action()  every change action (backup, launcher, cleanup, restore) is refused here
    run_preflight()  OPTIONAL, only when the Helper was started with --preflight-config: an
                     offline check of four owner-named saved files (ProductInfo report, native
                     inspection frame, trusted join policy, profile) through tvkit.factsjoin,
                     showing the seven profile/2 facts it derives. It is display-only: it reads
                     no TV, enables nothing, never marks the live target verified, and every
                     change action still refuses. Changing the target or starting a live check
                     clears a shown preflight result.

Every read has one hard 6-second deadline covering connect, request, headers and body.

What it never does: scan, guess an address, follow a redirect, use a proxy, store the raw
response, return the raw device id, or call the engine. The capability manifest starts
empty; an engine can be injected later (``engine=``) but this preview never runs one.

Facts honesty: the TV's info page reports a model name and an OS name. It does NOT give the
firmware version, build, region, processor type or Tizen version a profile needs, so an
exact match is never claimed and installation is always "not eligible" from this check.
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True  # importing tvkit must never create files under public-toolkit/

import errno
import ipaddress
import os
import re
import secrets
import select
import socket
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "public-toolkit") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "public-toolkit"))
from tvkit import profile as tvprofile  # noqa: E402
from tvkit import strictjson as sj  # noqa: E402

TV_PORT = 8001  # fixed in production; only tests construct a reader on another port
TV_PATH = "/api/v2/"
MAX_RESPONSE_BYTES = 64 * 1024
MAX_HEADER_BYTES = 16 * 1024
MAX_RAW_BODY_BYTES = 2 * MAX_RESPONSE_BYTES  # chunked framing overhead included
TOTAL_DEADLINE_S = 6.0  # hard limit for the whole read: connect, request, headers and body
WAIT_SLICE_S = 0.2  # longest single wait, so cancel and the deadline are noticed promptly
_IN_PROGRESS = {errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY,
                getattr(errno, "WSAEWOULDBLOCK", errno.EWOULDBLOCK)}
# 10/8, 172.16/12 and 192.168/16, given as (network, prefix) integers
PRIVATE_NETS = tuple(ipaddress.IPv4Network(n) for n in ((0x0A000000, 8), (0xAC100000, 12), (0xC0A80000, 16)))
SAMSUNG_TYPE = "Samsung SmartTV"
MODEL_RE = r"[A-Za-z0-9_-]{1,64}"
OS_RE = r"[A-Za-z][A-Za-z0-9 ._-]{0,31}"
DEVICE_ID_RE = r"[\x21-\x7e]{1,128}"
JOB_ID_RE = r"[0-9a-f]{16}"
DEVICE_ID_FIELDS = ("duid", "id")  # hashed only; the first one present is used

CAPABILITIES = ("capture", "readback", "install", "remove", "restore")
CAPABILITY_TEXT = {
    "capture": "QuietTV cannot copy files off your TV yet.",
    "readback": "QuietTV cannot yet check a saved backup against your TV.",
    "install": "QuietTV cannot install the launcher or root helper yet.",
    "remove": "QuietTV cannot remove Samsung apps yet.",
    "restore": "QuietTV cannot put removed apps back yet.",
}
FIELD_LABELS = {
    "model": "model number",
    "firmware": "software (firmware) version",
    "arch": "processor type",
    "osName": "operating system",
    "osVersion": "Tizen version",
    "firmwareBuild": "full firmware build",
    "region": "sales region",
}

ACTIONS = {
    "backup": {
        "title": "Back up",
        "needs": ("capture", "readback"),
        "needsBackup": False,
        "steps": (
            "Read the exact model, firmware build, region and processor type from the TV.",
            "Copy every file QuietTV could ever change to this PC, and hash it on the TV first.",
            "Read the copy back from this PC and check every hash matches the TV's.",
            "Practise a restore in a scratch folder before anything is removed.",
        ),
    },
    "launcher": {
        "title": "Install launcher",
        "needs": ("install",),
        "needsBackup": True,
        "steps": (
            "Turn on Developer Mode on the TV with this PC's address, then restart the TV.",
            "Sign in to your own Samsung account so a certificate is made for your TV only.",
            "Install the root helper made for your exact firmware.",
            "Install the QuietTV launcher, then press Home on the remote to check it opens.",
        ),
    },
    "cleanup": {
        "title": "Cleanup",
        "needs": ("remove",),
        "needsBackup": True,
        "steps": (
            "Choose what to remove, or take the recommended privacy cleanup.",
            "Review the exact list of apps that will be removed and what you lose.",
            "Approve the list. Nothing is removed before you approve it.",
            "Check each app is really gone, and report anything that is not.",
        ),
    },
    "restore": {
        "title": "Restore",
        "needs": ("restore",),
        "needsBackup": True,
        "steps": (
            "Pick one app to put back, or everything QuietTV changed.",
            "QuietTV checks the backup belongs to this exact TV and firmware.",
            "Apps newer than the backup are left alone and reported.",
            "QuietTV's own changes are undone first; the launcher and root helper go last.",
        ),
    },
}

GENERAL_PREREQUISITES = (
    "The TV is switched on and on the same home network as this PC.",
    "You know the TV's address (on the TV: Settings, General, Network, Network Status, IP Settings).",
    "Developer Mode turned on, with this PC's address entered, then the TV restarted.",
    "Your own Samsung account, to make a signing certificate for your TV only.",
    "A remote control within reach for every check that needs you to press a button.",
)


class SessionError(ValueError):
    """A request the session refuses. ``status`` is the HTTP status the server should send."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class ReaderError(RuntimeError):
    """The TV read failed. ``code`` is one of the fixed codes in ERROR_TEXT."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


ERROR_TEXT = {
    "unreachable": "The TV did not answer. Check it is switched on, on the same home network, "
                   "and that the address is right.",
    "redirect": "That address tried to send QuietTV somewhere else, so the check stopped.",
    "http-status": "Something answered at that address, but not with the TV's information page.",
    "too-large": "The answer from that address was too big to be a TV's information page.",
    "bad-response": "Something answered at that address, but it did not look like a Samsung TV.",
    "cancelled": "The check was cancelled.",
    "too-slow": "The TV took too long to answer, so the check stopped.",
    "start-failed": "QuietTV could not start the check. Try again.",
}


# ---- the read-only TV reader (one socket, fixed path, hard deadline) ----

_STATUS_RE = re.compile(rb"HTTP/1\.[01] ([0-9]{3})(?: [\x20-\x7e]*)?")
_TOKEN_RE = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+")


def _parse_head(head: bytes) -> tuple:
    """Status line and headers -> (content length or None, chunked). Refuses anything else."""
    lines = head.split(b"\r\n")
    match = _STATUS_RE.fullmatch(lines[0])
    if not match:
        raise ReaderError("bad-response")
    status = int(match.group(1))
    fields: dict = {}
    for line in lines[1:]:
        name, sep, value = line.partition(b":")
        if not sep or not _TOKEN_RE.fullmatch(name):
            raise ReaderError("bad-response")
        key = name.lower()
        if key in (b"content-length", b"transfer-encoding") and key in fields:
            raise ReaderError("bad-response")
        fields[key] = value.strip()
    if 300 <= status < 400:
        raise ReaderError("redirect")  # never followed
    if status != 200:
        raise ReaderError("http-status")
    encoding = fields.get(b"transfer-encoding")
    if encoding is not None and (encoding.lower() != b"chunked" or b"content-length" in fields):
        raise ReaderError("bad-response")
    length = None
    if b"content-length" in fields:
        if not re.fullmatch(rb"[0-9]{1,9}", fields[b"content-length"]):
            raise ReaderError("bad-response")
        length = int(fields[b"content-length"])
        if length > MAX_RESPONSE_BYTES:
            raise ReaderError("too-large")
    return length, encoding is not None


def _dechunk(raw: bytes) -> bytes | None:
    """Decode a chunked body; None while incomplete. Bounded by MAX_RESPONSE_BYTES."""
    out, i = bytearray(), 0
    while True:
        j = raw.find(b"\r\n", i)
        if j < 0:
            if len(raw) - i > 64:
                raise ReaderError("bad-response")
            return None
        size_text = raw[i:j].split(b";", 1)[0].strip()
        if not re.fullmatch(rb"[0-9A-Fa-f]{1,8}", size_text):
            raise ReaderError("bad-response")
        size, i = int(size_text, 16), j + 2
        if size == 0:
            if raw[i:i + 2] == b"\r\n" or raw.find(b"\r\n\r\n", i) >= 0:
                return bytes(out)
            return None
        if len(out) + size > MAX_RESPONSE_BYTES:
            raise ReaderError("too-large")
        if len(raw) < i + size + 2:
            return None
        if raw[i + size:i + size + 2] != b"\r\n":
            raise ReaderError("bad-response")
        out += raw[i:i + size]
        i += size + 2


class HttpInfoReader:
    """GET http://<ip>:8001/api/v2/ over one plain TCP socket; returns at most 64 KiB of body.

    One hard deadline covers everything (connect, request, headers and body), so a TV or an
    impostor that drips bytes cannot hold the check open. Every wait is at most WAIT_SLICE_S,
    so a cancel stops the read itself, not just the job's label. The socket is always closed
    before ``read`` returns or raises. There is no proxy support (no environment variable is
    read), no redirect following, no TLS and no other path.
    ``port`` and ``deadline`` exist for tests only; the Helper always uses the defaults.
    """

    def __init__(self, *, port: int = TV_PORT, deadline: float = TOTAL_DEADLINE_S):
        self.port = port
        self.deadline = deadline

    def read(self, ip: str, cancel: threading.Event | None = None) -> bytes:
        address = str(ipaddress.IPv4Address(ip))  # the session validated it; this only rebuilds it
        cancel = cancel if cancel is not None else threading.Event()
        stop_at = time.monotonic() + self.deadline
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setblocking(False)
            self._connect(sock, (address, self.port), stop_at, cancel)
            request = (f"GET {TV_PATH} HTTP/1.1\r\nHost: {address}:{self.port}\r\n"
                       "Accept: application/json\r\nConnection: close\r\n\r\n").encode("ascii")
            self._send(sock, request, stop_at, cancel)
            return self._receive(sock, stop_at, cancel)
        finally:
            sock.close()

    @staticmethod
    def _wait(sock: socket.socket, write: bool, stop_at: float, cancel: threading.Event) -> bool:
        if cancel.is_set():
            raise ReaderError("cancelled")
        remaining = stop_at - time.monotonic()
        if remaining <= 0:
            raise ReaderError("too-slow")
        try:
            ready = select.select([] if write else [sock], [sock] if write else [], [sock],
                                  min(WAIT_SLICE_S, remaining))
        except (OSError, ValueError):
            raise ReaderError("unreachable") from None
        return any(ready)

    def _connect(self, sock: socket.socket, addr: tuple, stop_at: float, cancel: threading.Event) -> None:
        err = sock.connect_ex(addr)
        if err == 0:
            return
        if err not in _IN_PROGRESS:
            raise ReaderError("unreachable")
        try:
            while not self._wait(sock, True, stop_at, cancel):
                pass
        except ReaderError as exc:
            raise ReaderError("unreachable" if exc.code == "too-slow" else exc.code) from None  # no answer at all
        if sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) != 0:
            raise ReaderError("unreachable")

    def _send(self, sock: socket.socket, data: bytes, stop_at: float, cancel: threading.Event) -> None:
        view = memoryview(data)
        while view:
            if not self._wait(sock, True, stop_at, cancel):
                continue
            try:
                view = view[sock.send(view):]
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                raise ReaderError("unreachable") from None

    def _receive(self, sock: socket.socket, stop_at: float, cancel: threading.Event) -> bytes:
        buf, head = bytearray(), None
        while True:
            if not self._wait(sock, False, stop_at, cancel):
                continue
            try:
                chunk = sock.recv(8192)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                raise ReaderError("unreachable") from None
            buf += chunk
            if head is None:
                end = buf.find(b"\r\n\r\n")
                if end < 0:
                    if len(buf) > MAX_HEADER_BYTES or not chunk:
                        raise ReaderError("bad-response")
                    continue
                head = _parse_head(bytes(buf[:end]))
                del buf[:end + 4]
            length, chunked = head
            if chunked:
                body = _dechunk(bytes(buf))
            elif length is not None:
                body = bytes(buf[:length]) if len(buf) >= length else None
            else:
                body = bytes(buf) if not chunk else None
            if body is not None:
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ReaderError("too-large")
                return body
            if len(buf) > MAX_RAW_BODY_BYTES or (length is None and not chunked and len(buf) > MAX_RESPONSE_BYTES):
                raise ReaderError("too-large")
            if not chunk:
                raise ReaderError("bad-response")  # closed before the answer was complete


# ---- validation ----

def validate_target(value: Any) -> str:
    """An explicit dotted-quad home-network address. No names, no ranges, no ports."""
    if type(value) is not str or not re.fullmatch(r"[0-9]{1,3}(\.[0-9]{1,3}){3}", value):
        raise SessionError("Type the TV's address as four numbers separated by dots, as shown in the TV's network settings.")
    try:
        address = ipaddress.IPv4Address(value)
    except ValueError:
        raise SessionError("That is not a valid address.") from None
    if str(address) != value:
        raise SessionError("Type the address without leading zeros.")
    if not any(address in net for net in PRIVATE_NETS) or address.packed[3] in (0, 255):
        raise SessionError("Only a home-network address (10.x.x.x, 172.16-31.x.x or 192.168.x.x) "
                           "can be checked.")
    return value


def _profiles() -> dict:
    return tvprofile.builtin_profiles()


def interpret_response(raw: bytes) -> dict:
    """Project the TV's info page onto the few fields the Helper trusts. Raises ReaderError."""
    if type(raw) is not bytes or len(raw) > MAX_RESPONSE_BYTES:
        raise ReaderError("too-large")
    try:
        top = sj.loads(raw)
    except (sj.StrictJsonError, RecursionError, ValueError):
        raise ReaderError("bad-response") from None
    if not isinstance(top, dict) or not isinstance(top.get("device"), dict):
        raise ReaderError("bad-response")
    device = top["device"]
    if SAMSUNG_TYPE not in (device.get("type"), top.get("type")):
        raise ReaderError("bad-response")
    model = device.get("modelName")
    if type(model) is not str or not re.fullmatch(MODEL_RE, model):
        raise ReaderError("bad-response")
    os_name = device.get("OS")
    os_name = os_name if type(os_name) is str and re.fullmatch(OS_RE, os_name) else None
    dev_mode = {"1": True, "0": False}.get(device.get("developerMode")) \
        if type(device.get("developerMode")) is str else None
    id_hash, id_source = None, None
    for key in DEVICE_ID_FIELDS:
        value = device.get(key)
        if type(value) is str and re.fullmatch(DEVICE_ID_RE, value):
            try:
                id_hash, id_source = tvprofile.device_id_sha256(value), key
            except tvprofile.ProfileError:
                pass
            break
    return {"model": model, "os": os_name, "developerMode": dev_mode,
            "deviceIdSha256": id_hash, "deviceIdSource": id_source}


def compatibility(facts: dict) -> dict:
    """Model support is separate from exact compatibility, which a network read can never prove."""
    matching = [p for p in _profiles().values() if p.match.get("model") == facts["model"]]
    confirmed = {"model"}
    if facts.get("os") is not None and all(p.match.get("osName") == facts["os"] for p in matching):
        confirmed.add("osName")
    missing: list = []
    for prof in matching:
        for field in prof.match_fields:
            if field not in confirmed and field not in missing:
                missing.append(field)
    if not matching:
        missing = [f for f in tvprofile.MATCH_FIELDS if f not in confirmed]
    return {
        "modelSupported": bool(matching),
        "profileIds": sorted(p.profile_id for p in matching),
        "exactMatchProven": False,
        "missingFacts": [{"field": f, "label": FIELD_LABELS.get(f, f)} for f in missing],
        "installEligible": False,
    }


# ---- optional saved-report preflight (display only; owner-configured at startup) ----
#
# The four sources are named ONCE, in a strict-JSON config file given on the command line with
# --preflight-config. No HTTP route ever takes a path, URL, policy, profile or identity: the
# browser can only press a body-less button that runs the check against the configured sources.
# The check is the existing offline tvkit.factsjoin library; nothing here reads a TV, a network
# or a receipt supplied from outside, and the result enables nothing.

PREFLIGHT_CONFIG_SCHEMA = "quiettv-helper-preflight/1"
PREFLIGHT_DISPLAY_SCHEMA = "tvkit-facts-join-preflight/1"  # factsjoin.PREFLIGHT_SCHEMA; checked, never trusted blindly
# role -> (config key, read limit in bytes). report/policy/profile are small; the framed native
# inventory is larger, then the inspection decoder enforces its own limits.
PREFLIGHT_SOURCES = (
    ("productInfoReport", 256 * 1024),
    ("nativeInspectionFrame", 4 * 1024 * 1024),
    ("joinPolicy", 256 * 1024),
    ("profile", 256 * 1024),
)
MAX_PREFLIGHT_CONFIG_BYTES = 64 * 1024
_REPARSE_POINT = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT (symlinks, junctions, OneDrive placeholders)


def _is_link_or_reparse(info: os.stat_result) -> bool:
    """True for a symlink, junction or any other reparse point. Single gate so tests can both-way it."""
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)

# Fixed, non-sensitive reasons. A refusal never echoes a source value, a path, a digest or a token.
PREFLIGHT_ERRORS = {
    "not-configured": "Saved-report preflight is not configured. Start QuietTV Helper with "
                      "--preflight-config to use it.",
    "source-unreadable": "QuietTV could not read one of the configured saved report files. Check "
                         "each one is still a plain local file.",
    "source-too-large": "One of the configured saved report files is too large to be a saved report.",
    "verify-failed": "QuietTV could not verify the saved reports against the trusted policy and "
                     "profile. Check they are the report, frame, policy and profile for this exact TV.",
    "in-progress": "A saved-report preflight is already running. Wait for it to finish.",
    "internal": "The saved-report preflight could not be completed.",
}


class PreflightConfigError(ValueError):
    """A startup --preflight-config problem. Names the field/key, never a path or file content."""


class _PreflightSourceError(Exception):
    """A per-run source read problem, carrying one PREFLIGHT_ERRORS code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class PreflightConfig:
    """Four validated absolute local file paths plus the operator's explicit assertions."""

    product_info_report: str
    native_inspection_frame: str
    join_policy: str
    profile: str
    trust_policy_and_profile: bool
    paired_from_same_tv: bool

    def paths(self) -> dict:
        return {"productInfoReport": self.product_info_report,
                "nativeInspectionFrame": self.native_inspection_frame,
                "joinPolicy": self.join_policy, "profile": self.profile}


def _path_is_local_and_plain(value: str, key: str) -> str:
    """Refuse a path that is not an absolute local path without links or reparse points.

    Walks every component (not just the leaf) so a junction in a PARENT folder is caught, and
    refuses UNC paths, mapped network drives and URLs. Raises PreflightConfigError naming the key.
    """
    if type(value) is not str or not value:
        raise PreflightConfigError(f"preflight config {key} must be a non-empty path string")
    if "\x00" in value:  # an embedded NUL makes every os path call raise ValueError, not OSError
        raise PreflightConfigError(f"preflight config {key} is not a valid path")
    if "://" in value or value.startswith(("\\\\", "//")):
        raise PreflightConfigError(f"preflight config {key} must be a local file, not a URL or network share")
    try:
        path = Path(value)
    except (ValueError, OSError):
        raise PreflightConfigError(f"preflight config {key} is not a valid path") from None
    if not path.is_absolute():
        raise PreflightConfigError(f"preflight config {key} must be an absolute path")
    if ".." in path.parts:
        raise PreflightConfigError(f"preflight config {key} must not contain '..'")
    try:
        real = os.path.realpath(value)
    except (ValueError, OSError):
        raise PreflightConfigError(f"preflight config {key} could not be resolved") from None
    if real.startswith(("\\\\", "//")):
        raise PreflightConfigError(f"preflight config {key} resolves to a network share, which is not allowed")
    # Every ancestor and the leaf itself must be a plain, non-reparse entry.
    for component in (path, *path.parents):
        try:
            info = os.lstat(component)
        except FileNotFoundError:
            continue  # a not-yet-existing parent cannot be a link; the leaf's own existence is checked on read
        except (ValueError, OSError):
            raise PreflightConfigError(f"preflight config {key} could not be inspected") from None
        if _is_link_or_reparse(info):
            raise PreflightConfigError(f"preflight config {key} crosses a symlink, junction or reparse point")
    return os.path.abspath(value)


def _read_local_file(path: str, max_bytes: int) -> bytes:
    """Read a plain local file, bounded, refusing links/reparse (leaf and ancestors) and growth.

    Used at run time (not startup): a link swapped in after startup is caught here too. Raises
    _PreflightSourceError with a fixed code; never includes the path or content in the error.
    """
    try:
        base = os.lstat(path)
    except (ValueError, OSError):
        raise _PreflightSourceError("source-unreadable") from None
    if _is_link_or_reparse(base) or not stat.S_ISREG(base.st_mode):
        raise _PreflightSourceError("source-unreadable")
    # Re-check every ancestor for a reparse point swapped in since startup.
    for component in Path(path).parents:
        try:
            info = os.lstat(component)
        except (ValueError, OSError):
            raise _PreflightSourceError("source-unreadable") from None
        if _is_link_or_reparse(info):
            raise _PreflightSourceError("source-unreadable")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except (ValueError, OSError):
        raise _PreflightSourceError("source-unreadable") from None
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise _PreflightSourceError("source-unreadable")
        # The opened fd must be the file lstat saw: no swap between lstat and open.
        if (opened.st_dev, opened.st_ino) != (base.st_dev, base.st_ino):
            raise _PreflightSourceError("source-unreadable")
        if opened.st_size > max_bytes:
            raise _PreflightSourceError("source-too-large")
        chunks, total = [], 0
        while total <= max_bytes:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise _PreflightSourceError("source-too-large")
            chunks.append(chunk)
    except OSError:
        # A read/stat failure after opening (patched os.read, a device error) is a fixed-reason
        # refusal, not an escaped OSError whose message could carry a path. The fd still closes below.
        raise _PreflightSourceError("source-unreadable") from None
    finally:
        os.close(fd)
    return b"".join(chunks)


def load_preflight_config(config_path: str) -> PreflightConfig:
    """Validate a --preflight-config file. Raises PreflightConfigError (never echoes a path)."""
    plain = _path_is_local_and_plain(config_path, "config")
    try:
        raw = _read_local_file(plain, MAX_PREFLIGHT_CONFIG_BYTES)
    except _PreflightSourceError as exc:
        raise PreflightConfigError("preflight config " +
                                   ("is too large" if exc.code == "source-too-large" else "could not be read")) from None
    try:
        top = sj.require_object(sj.loads(raw), "preflight config",
                                ("schema", "productInfoReport", "nativeInspectionFrame",
                                 "joinPolicy", "profile", "operatorAssertions"))
    except sj.StrictJsonError:
        raise PreflightConfigError("preflight config is not well-formed strict JSON") from None
    if top["schema"] != PREFLIGHT_CONFIG_SCHEMA:
        raise PreflightConfigError(f"preflight config schema must be {PREFLIGHT_CONFIG_SCHEMA}")
    resolved = {}
    for key, _ in PREFLIGHT_SOURCES:
        if type(top[key]) is not str:
            raise PreflightConfigError(f"preflight config {key} must be a path string")
        resolved[key] = _path_is_local_and_plain(top[key], key)
    # Four distinct files: refuse an ambiguous config that names the same file for two roles.
    for i, (a, _) in enumerate(PREFLIGHT_SOURCES):
        for b, _ in PREFLIGHT_SOURCES[i + 1:]:
            same = resolved[a] == resolved[b]
            if not same:
                try:
                    same = os.path.samefile(resolved[a], resolved[b])
                except OSError:
                    same = False
            if same:
                raise PreflightConfigError("preflight config names the same file for two sources")
    try:
        assertions = sj.require_object(top["operatorAssertions"], "preflight config operatorAssertions",
                                       ("trustPolicyAndProfile", "pairedFromSameTv"))
    except sj.StrictJsonError:
        raise PreflightConfigError("preflight config operatorAssertions is malformed") from None
    if assertions["trustPolicyAndProfile"] is not True:
        raise PreflightConfigError("preflight config requires operatorAssertions.trustPolicyAndProfile = true")
    if assertions["pairedFromSameTv"] is not True:
        raise PreflightConfigError("preflight config requires operatorAssertions.pairedFromSameTv = true")
    return PreflightConfig(resolved["productInfoReport"], resolved["nativeInspectionFrame"],
                           resolved["joinPolicy"], resolved["profile"], True, True)


# ---- engine seam ----

class Engine:
    """The future install/backup engine. The preview ships this empty one only."""

    name = "none"
    capabilities: frozenset = frozenset()


class _Job:
    def __init__(self, target: str):
        self.id = secrets.token_hex(8)
        self.target = target
        self.status = "running"  # running | cancelling | done | failed | cancelled
        self.started = time.time()
        self.cancel = threading.Event()  # the reader polls this; cancelling stops the read itself
        self.thread: threading.Thread | None = None


class Session:
    """All Helper state, behind one lock. Safe to call from many server threads."""

    def __init__(self, *, reader: Any = None, engine: Engine | None = None,
                 thread_factory: Callable[..., threading.Thread] = threading.Thread,
                 preflight_config: PreflightConfig | None = None):
        self._reader = reader if reader is not None else HttpInfoReader()
        self._engine = engine if engine is not None else Engine()
        self._thread_factory = thread_factory
        self._preflight_config = preflight_config
        self._lock = threading.Lock()
        self._target: str | None = None
        self._job: _Job | None = None
        self._last_job: dict | None = None
        self._check: dict | None = None
        self._preflight: dict | None = None   # last run result/error, or None (idle)
        self._preflight_gen = 0               # bumped when a target change or live check invalidates it
        self._preflight_busy = False          # a preflight compute is in flight (one at a time)

    # -- target and jobs --

    def _busy(self) -> bool:
        return self._job is not None and self._job.status in ("running", "cancelling")

    def set_target(self, value: Any) -> dict:
        ip = validate_target(value)
        with self._lock:
            if self._busy():
                raise SessionError("A check is still running for another address. Cancel it first.", 409)
            if ip != self._target:
                self._target, self._check, self._last_job = ip, None, None
                self._invalidate_preflight_locked()  # a saved-report result is for no particular target
            return self._state_locked()

    def start_check(self, target: Any) -> dict:
        with self._lock:
            if self._target is None:
                raise SessionError("Enter the TV's address first.", 409)
            if target != self._target:
                raise SessionError("This check is locked to the address already entered.", 409)
            if self._busy():
                raise SessionError("A check is already running.", 409)
            self._invalidate_preflight_locked()  # a live check supersedes any shown saved-report result
            job = self._job = _Job(self._target)
            self._check = None
            # Started under the lock: no other request can see a "running" job without a worker.
            try:
                worker = self._thread_factory(target=self._run, args=(job,), daemon=True, name="quiettv-check")
                worker.start()
                job.thread = worker
            except Exception:
                job.cancel.set()  # if a worker did start anyway, it stops and its result is discarded
                job.status = "failed"
                self._job = None
                self._last_job = {"id": job.id, "status": "failed"}
                self._check = {"reachable": False,
                               "error": {"code": "start-failed", "message": ERROR_TEXT["start-failed"]}}
                raise SessionError(ERROR_TEXT["start-failed"], 500) from None
            return self._state_locked()

    def cancel(self, job_id: Any) -> dict:
        if type(job_id) is not str or not re.fullmatch(JOB_ID_RE, job_id):
            raise SessionError("Unknown check.")
        with self._lock:
            if self._job is None or self._job.id != job_id or self._job.status != "running":
                raise SessionError("That check is not running.", 409)
            self._job.status = "cancelling"
            self._job.cancel.set()
            return self._state_locked()

    def shutdown(self, timeout: float = TOTAL_DEADLINE_S + 1) -> bool:
        """Cancel any check and wait for its read to stop. True when no worker is left."""
        with self._lock:
            job = self._job
            if job is not None:
                job.cancel.set()
                if job.status == "running":
                    job.status = "cancelling"
        worker = job.thread if job is not None else None
        if worker is not None:
            worker.join(timeout)
            return not worker.is_alive()
        return True

    def _run(self, job: _Job) -> None:
        result: dict
        try:
            raw = self._reader.read(job.target, job.cancel)
            facts = interpret_response(raw)
            result = {"reachable": True, "error": None, **facts, **compatibility(facts)}
        except ReaderError as exc:
            code = exc.code if exc.code in ERROR_TEXT else "unreachable"
            result = {"reachable": code != "unreachable",
                      "error": {"code": code, "message": ERROR_TEXT[code]}}
        except Exception:  # a broken reader must not leave the session stuck "running"
            result = {"reachable": False, "error": {"code": "unreachable", "message": ERROR_TEXT["unreachable"]}}
        with self._lock:
            if self._job is not job:
                return
            if job.status == "cancelling" or job.cancel.is_set():
                job.status = "cancelled"
            else:
                job.status = "failed" if result["error"] else "done"
                result["checkedAt"] = int(time.time())
                self._check = result
            self._last_job = {"id": job.id, "status": job.status}
            self._job = None

    # -- actions: refused, with the concrete missing gates --

    def _gates_locked(self, action: str) -> list:
        spec = ACTIONS[action]
        gates = []
        check = self._check
        if check is None or check.get("error"):
            gates.append({"id": "check", "text": "Check your TV first."})
        elif not check["modelSupported"]:
            gates.append({"id": "model", "text": f"Model {check['model']} is not on the supported list yet."})
        if check is None or check.get("error") or check["modelSupported"]:
            labels = ", ".join(m["label"] for m in (check or {}).get("missingFacts", [])) or \
                "software version, processor type and Tizen version"
            gates.append({"id": "exact", "text": "QuietTV could not confirm this TV's exact " + labels +
                          ". A network check cannot read these; it needs the setup connection, "
                          "which this preview does not have."})
        for cap in spec["needs"]:
            if cap not in self._engine.capabilities:
                gates.append({"id": "capability-" + cap, "text": CAPABILITY_TEXT[cap]})
        if spec["needsBackup"]:
            gates.append({"id": "backup", "text": "There is no verified backup of this exact TV yet."})
        gates.append({"id": "preview", "text": "This developer preview never changes your TV."})
        return gates

    def refuse_action(self, action: str, body: Any) -> dict:
        """Validate the request shape, then refuse with the gates. Never touches the TV or engine."""
        if action not in ACTIONS:
            raise SessionError("Unknown action.", 404)
        if not isinstance(body, dict) or set(body) - {"componentIds"}:
            raise SessionError("Unexpected request.")
        ids = body.get("componentIds", [])
        if not isinstance(ids, list) or len(ids) > 64 or \
                not all(type(i) is str and re.fullmatch(r"[A-Za-z0-9._-]{1,128}", i) for i in ids):
            raise SessionError("Unexpected request.")
        with self._lock:
            return {"refused": True, "action": action, "changed": False, "gates": self._gates_locked(action)}

    # -- saved-report preflight (display only; enables nothing) --

    def _invalidate_preflight_locked(self) -> None:
        """Drop any shown saved-report result and bump the generation (caller holds the lock)."""
        self._preflight = None
        self._preflight_gen += 1

    def run_preflight(self) -> dict:
        """Run the offline saved-report preflight against the configured sources. Never reads a TV.

        The file reads and the factsjoin call run OUTSIDE the lock; the result is stored only if
        no target change or live check invalidated it meanwhile (generation guard). Only one
        preflight runs at a time: a second concurrent request is refused with a 409. While a run
        is in flight the state reports status "running"; the finally clause always clears the
        busy flag, so an invalidated or failed run never leaves the preflight stuck "running".
        """
        with self._lock:
            config = self._preflight_config
            if config is None:
                raise SessionError(PREFLIGHT_ERRORS["not-configured"], 409)
            if self._preflight_busy:
                raise SessionError(PREFLIGHT_ERRORS["in-progress"], 409)
            self._preflight_busy = True
            gen = self._preflight_gen
        result = None
        try:
            result = self._compute_preflight(config)
        finally:
            with self._lock:
                self._preflight_busy = False
                # Store only a run that completed and was not invalidated meanwhile: an invalidated
                # run must not republish over the None that _invalidate_preflight_locked left behind.
                if result is not None and gen == self._preflight_gen:
                    self._preflight = result
                state = self._state_locked()
        return state

    def _compute_preflight(self, config: PreflightConfig) -> dict:
        """Read the four saved files and derive the display-safe result or a fixed-reason error.

        Reads no TV and no network. Catches every failure and maps it to a fixed PREFLIGHT_ERRORS
        code; it never stores or returns the report bytes, the identity, the receipt or a path.
        """
        try:
            limits = dict(PREFLIGHT_SOURCES)
            report = _read_local_file(config.product_info_report, limits["productInfoReport"])
            frame = _read_local_file(config.native_inspection_frame, limits["nativeInspectionFrame"])
            policy_raw = _read_local_file(config.join_policy, limits["joinPolicy"])
            profile_raw = _read_local_file(config.profile, limits["profile"])
        except _PreflightSourceError as exc:
            return self._preflight_error(exc.code)
        try:
            from tvkit import factsjoin as fj  # lazy: only pulled in when preflight is actually used
        except Exception:
            return self._preflight_error("internal")
        try:
            profile = tvprofile.parse_profile(sj.loads(profile_raw))
            policy = fj.join_policy_from_json(policy_raw)
            receipt = fj.join(report, frame, policy=policy, profile=profile)
            shown = fj.preflight_display(receipt, report, frame, policy=policy, profile=profile)
            # The projection is inside the try too: a KeyError here must not escape to the server.
            return self._preflight_success(config, shown)
        except (fj.JoinError, tvprofile.ProfileError, sj.StrictJsonError):
            return self._preflight_error("verify-failed")
        except Exception:
            # Any other failure (a hostile or truncated frame, RecursionError, a bad profile object)
            # is still a refusal, never an escaped exception: do_POST would otherwise send no response.
            return self._preflight_error("verify-failed")

    @staticmethod
    def _preflight_error(code: str) -> dict:
        code = code if code in PREFLIGHT_ERRORS else "internal"
        return {"status": "error", "error": {"code": code, "message": PREFLIGHT_ERRORS[code]}, "result": None}

    @staticmethod
    def _preflight_success(config: PreflightConfig, shown: dict) -> dict:
        """Project factsjoin.preflight_display onto the safe view the UI shows. No token/digest/path."""
        if shown.get("schema") != PREFLIGHT_DISPLAY_SCHEMA:
            return Session._preflight_error("internal")
        fields = []
        for field in tvprofile.MATCH_FIELDS_V2:
            fields.append({"field": field, "label": FIELD_LABELS.get(field, field),
                           "value": shown["fields"][field], "source": shown["fieldSources"][field]})
        return {"status": "success", "error": None, "result": {
            "profileId": shown["profileId"],
            "identityScheme": shown["identityScheme"],
            "fields": fields,
            "note": shown["note"],
            "operatorAssertions": {"trustPolicyAndProfile": config.trust_policy_and_profile,
                                   "pairedFromSameTv": config.paired_from_same_tv},
        }}

    def _preflight_view_locked(self) -> dict:
        if self._preflight_config is None:
            return {"configured": False, "status": "idle", "error": None, "result": None}
        if self._preflight_busy:  # a compute is in flight: pending, distinct from idle/success/error
            return {"configured": True, "status": "running", "error": None, "result": None}
        current = self._preflight
        if current is None:
            return {"configured": True, "status": "idle", "error": None, "result": None}
        return {"configured": True, **current}

    # -- views --

    def _capabilities_locked(self) -> dict:
        return {cap: {"available": cap in self._engine.capabilities,
                      "reason": "" if cap in self._engine.capabilities else CAPABILITY_TEXT[cap]}
                for cap in CAPABILITIES}

    def _prerequisites_locked(self) -> list:
        check = self._check
        if check and not check.get("error") and check["modelSupported"]:
            profiles = _profiles()
            items = []
            for pid in check["profileIds"]:
                for item in profiles[pid].manual_prerequisites:
                    if item not in items:
                        items.append(item)
            return items
        return list(GENERAL_PREREQUISITES)

    def _state_locked(self) -> dict:
        job = self._job
        return {
            "preview": True,
            "target": self._target,
            "job": {"id": job.id, "status": job.status} if job else None,
            "lastJob": self._last_job,
            "check": dict(self._check) if self._check else None,
            "engine": self._engine.name,
            "capabilities": self._capabilities_locked(),
            "actions": {name: {"title": spec["title"], "enabled": False, "steps": list(spec["steps"]),
                               "gates": self._gates_locked(name)} for name, spec in ACTIONS.items()},
            "prerequisites": self._prerequisites_locked(),
            "preflight": self._preflight_view_locked(),
        }

    def state(self) -> dict:
        with self._lock:
            return self._state_locked()

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """Tests and shutdown: wait until no check is in flight."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if not self._busy():
                    return True
            time.sleep(0.01)
        return False

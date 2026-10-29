"""Decode QUIETTV-CAPTURE/1 package and xattr-manifest frames.

No device connection or filesystem mutation on the TV. The caller owns transport
timeout/line reading (readline(MAX_LINE + 1)) and supplies the exit status after
EOF. This module refuses unknown diagnostics, not just bad frames.

Package frames start with ``BEGIN package <id>`` and are capped at 4 GiB. Xattr
frames start with ``BEGIN xattrs <id>`` and are capped at 32 MiB. Each public
wrapper fixes its kind; input of the other kind is refused, never auto-detected.
There is no caller-selected arbitrary kind, root, path or command. Xattr payloads
remain opaque bytes: this module does not parse a manifest, register a transport,
or claim a full backup.

Input ownership starts when frame iteration starts, after argument and spool
storage validation. From that point the iterator is closed on every path. The
returned chunks own the private PC spool until the consumer closes them; cleanup
removes only the spool file this call created.
"""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
import re
import tempfile

from .capture import CapturedStream

TAG = b"QUIETTV-CAPTURE/1 "
CHUNK = 32768
MAX_LINE = 43900
MAX_BYTES = 4 << 30
MAX_XATTR_BYTES = 32 << 20
_KIND_LIMITS = {"package": (b"package", MAX_BYTES), "xattrs": (b"xattrs", MAX_XATTR_BYTES)}
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_NUM = re.compile(rb"0|[1-9][0-9]{0,10}\Z")
_HASH = re.compile(rb"[0-9a-f]{64}\Z")
_GC = re.compile(
    rb"\[GC_INFO\] GC_Start #[0-9]{1,10}: Gen\([0-9]\), Time\([0-9]{1,10} ms\), Reason\([0-9]{1,3}\)\Z"
    rb"|\[GC_INFO\] GC_End #[0-9]{1,10}: Gen\([0-9]\), Time\([0-9]{1,10} ms\)\Z")


class StreamRefused(ValueError):
    pass


def _storage_parent(path):
    # Check lexical AND resolved ancestry: a link cannot bypass an ancestor .git.
    lexical = Path(os.path.abspath(path))
    real = lexical.resolve(strict=True)
    for start in (lexical, real):
        for parent in (start, *start.parents):
            if os.path.lexists(parent / ".git"):
                raise StreamRefused("spool storage is inside Git")
    if not real.is_dir():
        raise StreamRefused("spool parent is not a directory")
    return real


class _Spool:
    def __init__(self, handle, path):
        self.handle, self.path = handle, path
        self.identity = os.fstat(handle.fileno())
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        if self.closed:
            raise StopIteration
        try:
            data = self.handle.read(CHUNK)
            if data:
                return data
            self.close()
            raise StopIteration
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.handle.close()
        try:
            now = self.path.lstat()
            if (now.st_dev, now.st_ino) == (self.identity.st_dev, self.identity.st_ino):
                self.path.unlink()
        except FileNotFoundError:
            pass


def _number(value):
    if not _NUM.fullmatch(value):
        raise StreamRefused("invalid count")
    return int(value)


def decode_package_stream(lines, *, expected_package_id, max_bytes, spool_parent, exit_code):
    """Decode a ``BEGIN package`` stream, capped at 4 GiB.

    Return CapturedStream only after full transfer and successful remote exit.
    exit_code is a zero-argument callable, invoked exactly once AFTER EOF; a
    pre-recorded exit value is not accepted. Input ownership starts when frame
    iteration starts, after argument and spool storage validation; from that
    point the iterator is closed on every path. Consumer MUST close the returned
    chunks if it stops early (as capture.capture_backup already does).
    """
    return _decode_stream(lines, kind="package", expected_package_id=expected_package_id,
                          max_bytes=max_bytes, spool_parent=spool_parent,
                          exit_code=exit_code)


def decode_xattr_stream(lines, *, expected_package_id, max_bytes, spool_parent, exit_code):
    """Decode a ``BEGIN xattrs`` stream, capped at 32 MiB, as opaque bytes.

    The payload is not parsed as a manifest. Return CapturedStream only after
    full transfer and successful remote exit. exit_code is a zero-argument
    callable, invoked exactly once AFTER EOF; a pre-recorded exit value is not
    accepted. Input ownership starts when frame iteration starts, after argument
    and spool storage validation; from that point the iterator is closed on every
    path. Consumer MUST close the returned chunks if it stops early.
    """
    return _decode_stream(lines, kind="xattrs", expected_package_id=expected_package_id,
                          max_bytes=max_bytes, spool_parent=spool_parent,
                          exit_code=exit_code)


def _decode_stream(lines, *, kind, expected_package_id, max_bytes, spool_parent, exit_code):
    """Shared implementation; the public wrappers fix kind and its hard cap."""
    try:
        kind_tag, ceiling = _KIND_LIMITS[kind]
    except (KeyError, TypeError) as exc:
        raise StreamRefused("invalid stream kind") from exc
    if type(expected_package_id) is not str or not _ID.fullmatch(expected_package_id):
        raise StreamRefused("invalid package id")
    if type(max_bytes) is not int or not 0 < max_bytes <= ceiling or not callable(exit_code):
        raise StreamRefused("invalid transfer limit or exit provider")
    parent = _storage_parent(spool_parent)
    iterator = iter(lines)
    spool = None
    try:
        fd, path = tempfile.mkstemp(prefix=f".quiettv-{kind}-", dir=parent)
        spool = _Spool(os.fdopen(fd, "w+b"), Path(path))
        state, count, total, noise = "begin", 0, 0, 0
        digest = hashlib.sha256()
        final_digest = None
        for raw in iterator:
            if type(raw) is not bytes or not raw.endswith(b"\n") or len(raw) > MAX_LINE:
                raise StreamRefused("invalid or oversized line")
            line = raw[:-1]
            if _GC.fullmatch(line):
                noise += 1
                if noise > 4096:
                    raise StreamRefused("too much noise")
                continue
            if not line.startswith(TAG):
                raise StreamRefused("unknown diagnostic")
            parts = line[len(TAG):].split(b" ")
            if state == "begin":
                if parts != [b"BEGIN", kind_tag, expected_package_id.encode("ascii")]:
                    raise StreamRefused(f"wrong begin or {kind}")
                state = "data"
            elif state == "data" and len(parts) == 4 and parts[0] == b"DATA":
                if _number(parts[1]) != count or not _HASH.fullmatch(parts[3]):
                    raise StreamRefused("chunk sequence or hash syntax")
                try:
                    data = base64.b64decode(parts[2], validate=True)
                except ValueError as exc:
                    raise StreamRefused("invalid base64") from exc
                if not 0 < len(data) <= CHUNK or base64.b64encode(data) != parts[2]:
                    raise StreamRefused("invalid chunk size or encoding")
                if hashlib.sha256(data).hexdigest().encode() != parts[3]:
                    raise StreamRefused("chunk hash mismatch")
                total += len(data)
                count += 1
                if total > max_bytes or count > max_bytes:
                    raise StreamRefused("transfer overflow")
                spool.handle.write(data)
                digest.update(data)
            elif state == "data" and len(parts) == 4 and parts[0] == b"END":
                if (_number(parts[1]) != count or _number(parts[2]) != total
                        or not _HASH.fullmatch(parts[3]) or total == 0
                        or digest.hexdigest().encode() != parts[3]):
                    raise StreamRefused("final digest or size mismatch")
                final_digest = parts[3].decode("ascii")  # Device value, NOT PC rehash.
                state = "ended"
            else:
                raise StreamRefused("unexpected frame")
        if state != "ended":
            raise StreamRefused("truncated stream")
        code = exit_code()
        if type(code) is not int or code != 0:
            raise StreamRefused("device command failed")
        spool.handle.flush()
        spool.handle.seek(0)
        result = CapturedStream(final_digest, total, spool)
        spool = None  # ownership passes to result.chunks
        return result
    finally:
        if spool is not None:
            spool.close()
        close = getattr(iterator, "close", None)
        if close is not None:
            close()

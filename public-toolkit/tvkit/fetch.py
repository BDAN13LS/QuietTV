"""Fetch a capsule or release artifact and verify it against a pinned SHA-256.

Sources: a local file (PC or NAS share) or an ``https://`` URL. Plain HTTP,
redirects to anything but HTTPS, and URLs carrying ``user:password@`` are
refused. The pinned hash and a size cap are mandatory. Bytes stream into an
exclusive ``.partial`` file next to the destination, are hashed while
streaming, and are renamed into place only after the hash matches (the same
pattern as receive_backup.py). Nothing is ever uploaded.

Every message shows the URL redacted to scheme, host and path: userinfo,
query strings and fragments can carry tokens and are never printed.
"""

from __future__ import annotations

import hashlib
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .paths import is_sha256

DEFAULT_MAX_BYTES = 8 * 1024 * 1024 * 1024
DEFAULT_READ_TIMEOUT = 30.0
DEFAULT_TOTAL_TIMEOUT = 30 * 60.0
CHUNK = 1024 * 1024


class FetchError(RuntimeError):
    pass


@dataclass(frozen=True)
class FetchResult:
    destination: Path
    sha256: str
    size: int
    source: str  # redacted


def redact_url(url: str) -> str:
    """scheme://host[:port]/path, nothing else. Never raises."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
        if not parts.scheme or not host:
            return "<unparseable URL>"
        return f"{parts.scheme}://{host}{port}{parts.path}"
    except ValueError:
        return "<unparseable URL>"


class _HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        target = urllib.parse.urlsplit(newurl)
        if target.scheme != "https" or target.username or target.password:
            raise FetchError(f"refusing redirect to non-HTTPS or credentialed URL {redact_url(newurl)}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def default_opener() -> Callable[..., object]:
    opener = urllib.request.build_opener(_HttpsOnlyRedirects())
    return opener.open


def _check_pin(expected_sha256: str, max_bytes: int) -> str:
    expected = expected_sha256.strip().lower()
    if not is_sha256(expected):
        raise FetchError("a pinned SHA-256 (64 hex characters) is required")
    if type(max_bytes) is not int or max_bytes <= 0:
        raise FetchError("a positive size cap is required")
    return expected


def _prepare_destination(destination: Path) -> Path:
    if destination.exists() or destination.is_symlink():
        raise FetchError(f"refusing to overwrite existing {destination.name}")
    if not destination.parent.is_dir():
        raise FetchError("destination folder does not exist")
    partial = destination.with_name(destination.name + ".partial")
    if partial.exists() or partial.is_symlink():
        raise FetchError(f"a stale {partial.name} exists; inspect and remove it first")
    return partial


def _stream(read: Callable[[int], bytes], partial: Path, expected: str, max_bytes: int,
            deadline: float, label: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(partial, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                if time.monotonic() > deadline:
                    raise FetchError(f"timed out reading {label}")
                chunk = read(CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise FetchError(f"{label} is larger than the {max_bytes}-byte cap")
                digest.update(chunk)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    actual = digest.hexdigest()
    if actual != expected:
        partial.unlink(missing_ok=True)
        raise FetchError(f"SHA-256 mismatch for {label}; nothing was kept")
    return actual, size


def fetch(
    source: str,
    destination: str | Path,
    expected_sha256: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    read_timeout: float = DEFAULT_READ_TIMEOUT,
    total_timeout: float = DEFAULT_TOTAL_TIMEOUT,
    opener: Optional[Callable[..., object]] = None,
) -> FetchResult:
    expected = _check_pin(expected_sha256, max_bytes)
    dest = Path(destination)
    deadline = time.monotonic() + total_timeout

    if "://" not in source:
        local = Path(source)
        label = local.name
        if local.is_symlink() or not local.is_file():
            raise FetchError(f"local source {label} is not a regular file")
        if local.stat().st_size > max_bytes:
            raise FetchError(f"{label} is larger than the {max_bytes}-byte cap")
        if dest.exists() and local.resolve() == dest.resolve():
            raise FetchError("source and destination are the same file")
        partial = _prepare_destination(dest)
        with open(local, "rb") as handle:
            digest, size = _stream(handle.read, partial, expected, max_bytes, deadline, label)
        os.replace(partial, dest)
        return FetchResult(dest, digest, size, label)

    try:
        parts = urllib.parse.urlsplit(source)
    except ValueError:
        raise FetchError("source URL cannot be parsed") from None
    label = redact_url(source)
    # http.client puts the whole URL, query included, into InvalidURL messages.
    if any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in source):
        raise FetchError(f"refusing a URL with spaces or control characters ({label})")
    if parts.scheme != "https":
        raise FetchError(f"only https:// sources are allowed, not {parts.scheme or 'none'}")
    if parts.username or parts.password or "@" in parts.netloc:
        raise FetchError(f"refusing a URL with embedded credentials ({label})")
    partial = _prepare_destination(dest)
    open_url = opener or default_opener()
    request = urllib.request.Request(source, method="GET", headers={"User-Agent": "tvkit/0.1"})
    try:
        response = open_url(request, timeout=read_timeout)
    except FetchError:
        raise
    except urllib.error.HTTPError as exc:
        raise FetchError(f"server returned HTTP {exc.code} for {label}") from None
    except Exception as exc:  # noqa: BLE001 - any message may echo the URL
        reason = getattr(exc, "reason", exc)
        kind = type(reason).__name__
        raise FetchError(f"could not reach {label} ({kind})") from None
    try:
        final_url = getattr(response, "geturl", lambda: source)()
        if urllib.parse.urlsplit(final_url).scheme != "https":
            raise FetchError(f"final URL is not HTTPS ({redact_url(final_url)})")
        headers = getattr(response, "headers", None)
        length = headers.get("Content-Length") if headers is not None else None
        if length is not None and length.isdigit() and int(length) > max_bytes:
            raise FetchError(f"{label} declares more than the {max_bytes}-byte cap")
        try:
            digest, size = _stream(response.read, partial, expected, max_bytes, deadline, label)
        except FetchError:
            raise
        except Exception as exc:  # noqa: BLE001 - e.g. IncompleteRead; never echo it
            raise FetchError(f"transfer from {label} failed ({type(exc).__name__})") from None
    finally:
        close = getattr(response, "close", None)
        if close:
            close()
    os.replace(partial, dest)
    return FetchResult(dest, digest, size, label)


def sha256_file(path: str | Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


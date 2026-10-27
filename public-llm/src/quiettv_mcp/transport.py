"""Bounded plain-HTTP requests to a literal LAN address.

http.client is used directly: it never consults HTTP(S)_PROXY, never follows
redirects and never resolves anything but the literal IP we hand it.
"""
from __future__ import annotations

import http.client
from dataclasses import dataclass
from typing import Mapping


class TransportError(Exception):
    """Network-level failure. The message is safe to show."""


class OversizeError(TransportError):
    pass


@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping[str, str]
    body: bytes

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())


def request(host: str, port: int, method: str, path: str, *, headers: Mapping[str, str] | None = None,
            timeout: float = 10.0, max_bytes: int = 256 * 1024) -> Response:
    if method not in ("GET", "POST"):
        raise TransportError("unsupported method")
    if not path.startswith("/") or any(c in path for c in "\r\n \t?#"):
        raise TransportError("unsupported path")
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        send_headers = {"Accept": "application/json, image/png", "Connection": "close"}
        send_headers.update(headers or {})
        conn.request(method, path, body=b"" if method == "POST" else None, headers=send_headers)
        resp = conn.getresponse()
        declared = resp.getheader("Content-Length")
        if declared is not None and declared.isdigit() and int(declared) > max_bytes:
            raise OversizeError(f"response larger than {max_bytes} bytes")
        body = resp.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise OversizeError(f"response larger than {max_bytes} bytes")
        return Response(resp.status, {k.lower(): v for k, v in resp.getheaders()}, body)
    except OversizeError:
        raise
    except (OSError, http.client.HTTPException) as exc:
        raise TransportError(f"{method} {path} on port {port} failed: {type(exc).__name__}") from None
    finally:
        conn.close()

"""Loopback stand-in for the TV: an identity server (like :8001) and a control service (like :4190).

Fixture values only: the MAC is locally administered and the key is random per run.
"""
from __future__ import annotations

import json
import struct
import threading
import time
import zlib
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

FIXTURE_MAC = "02:00:00:00:00:01"
FIXTURE_MODEL = "UN43U8000FBXZA"


def make_png(width: int = 4, height: int = 3) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\x10\x20\x30" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class FakeTV:
    def __init__(self, key: str):
        self.key = key
        self.hits: Counter[str] = Counter()
        self.seen_headers: list[dict[str, str]] = []
        self.device: dict[str, Any] = {"modelName": FIXTURE_MODEL, "wifiMac": FIXTURE_MAC, "OS": "Tizen",
                                       "firmwareVersion": "Unknown", "ip": "127.0.0.1"}
        self.status_doc: dict[str, Any] = {
            "ready": True, "root": {"connected": True}, "platform": {"prepared": True},
            "health": {"at": 1, "exit": 0, "ready": True}, "dashboardPid": "123", "viewerPid": "",
            "bootId": "0f0e0d0c-0b0a-0908-0706-050403020100", "ui": {"visible": True, "source": "registered-home"},
            "capabilities": ["status", "home", "key", "launch", "diagnostics", "capture"],
        }
        self.diag_stdout = "home=quiet\n"
        # Mirrors the live defect: POST /home is accepted but leaves another app in the foreground;
        # only the remote Home key (63) brings the registered Home forward.
        self.foreground = "other.app"
        self.key_exit_code = 0
        self.capture_mode = "fresh"
        self.capture_counter = 0
        self.identity_server = self._serve(self._identity_handler())
        self.control_server = self._serve(self._control_handler())

    # -- servers ----------------------------------------------------------
    def _serve(self, handler) -> ThreadingHTTPServer:
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    @property
    def identity_port(self) -> int:
        return self.identity_server.server_address[1]

    @property
    def control_port(self) -> int:
        return self.control_server.server_address[1]

    def close(self) -> None:
        for server in (self.identity_server, self.control_server):
            server.shutdown()
            server.server_close()

    def control_hits(self) -> int:
        return sum(v for k, v in self.hits.items() if not k.startswith("identity "))

    def _identity_handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                fake.hits["identity " + self.path] += 1
                fake.seen_headers.append({k.lower(): v for k, v in self.headers.items()})
                body = json.dumps({"device": fake.device, "type": "Samsung SmartTV"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        return Handler

    def _control_handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, code: int, doc: Any) -> None:
                body = json.dumps(doc).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _auth(self) -> bool:
                fake.hits[f"{self.command} {self.path}"] += 1
                fake.seen_headers.append({k.lower(): v for k, v in self.headers.items()})
                if self.headers.get("X-QuietTV-Key") != fake.key:
                    self._json(403, {"error": "unauthorized"})
                    return False
                return True

            def do_GET(self):
                if not self._auth():
                    return
                if self.path == "/status":
                    self._json(200, fake.status_doc)
                elif self.path == "/screenshot":
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.end_headers()
                    self.wfile.write(make_png())
                else:
                    self._json(405, {"error": "POST required"})

            def do_POST(self):
                if not self._auth():
                    return
                route = self.path[1:]
                if route == "home":
                    self._json(200, fake.status_doc)
                elif route.startswith("key/"):
                    if route == "key/63" and fake.key_exit_code == 0:
                        fake.foreground = "home"
                    self._json(200, {"exit_code": fake.key_exit_code,
                                     "stdout": "remote_injected=" + route[4:] + " receiver=/dev/input/event2"})
                elif route.startswith("launch/"):
                    self._json(200, {"accepted": True})
                elif route == "diagnostics":
                    self._json(200, {"exit_code": 0, "stdout": fake.diag_stdout})
                elif route == "capture":
                    self._capture()
                else:
                    self._json(404, {"error": "unknown action"})

            def _capture(self):
                mode = fake.capture_mode
                if mode == "absent":
                    self._json(404, {"error": "unknown action"})
                    return
                fake.capture_counter += 1
                body = make_png()
                headers = {
                    "Content-Type": "image/png",
                    "Cache-Control": "no-store",
                    "X-QuietTV-Capture-Id": f"cap-{fake.capture_counter:06d}",
                    "X-QuietTV-Captured-At": str(int(time.time() * 1000)),
                    "X-QuietTV-Capture-Nonce": self.headers.get("X-QuietTV-Capture-Nonce", ""),
                }
                if mode == "html":
                    body = b"<html><body>cached screenshot page</body></html>"
                    headers["Content-Type"] = "text/html"
                elif mode == "html-as-png":
                    body = b"<html><body>not a png</body></html>"
                elif mode == "oversize":
                    body = b"\x89PNG\r\n\x1a\n" + b"\x00" * (8 * 1024 * 1024 + 10)
                elif mode == "cacheable":
                    headers["Cache-Control"] = "max-age=60"
                elif mode == "stale-time":
                    headers["X-QuietTV-Captured-At"] = str(int(time.time() * 1000) - 3_600_000)
                elif mode == "repeat-id":
                    headers["X-QuietTV-Capture-Id"] = "cap-repeat01"
                elif mode == "no-id":
                    del headers["X-QuietTV-Capture-Id"]
                elif mode == "wrong-nonce":
                    headers["X-QuietTV-Capture-Nonce"] = "someone-else"
                elif mode == "no-nonce":
                    del headers["X-QuietTV-Capture-Nonce"]
                elif mode == "bad-crc":
                    body = body[:29] + b"\x00\x00\x00\x00" + body[33:]
                self.send_response(200)
                for k, v in headers.items():
                    self.send_header(k, v)
                if mode != "oversize-undeclared":  # HTTP/1.0 close-delimited body when undeclared
                    self.send_header("Content-Length", str(len(body)))
                else:
                    body = b"\x89PNG\r\n\x1a\n" + b"\x00" * (8 * 1024 * 1024 + 10)
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass
        return Handler

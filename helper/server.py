"""QuietTV Helper local web UI (D6, developer preview).

Serves helper/ui/ and a small JSON API to the owner's own browser, on 127.0.0.1 only.

Security contract (tests/test_helper_server.py checks each line):
    bind        127.0.0.1, a random free port, exclusive (SO_EXCLUSIVEADDRUSE on Windows)
    Host        exactly one header, exactly "127.0.0.1:<port>", on every request
    Origin      API: exactly "http://127.0.0.1:<port>"; required on POST, checked if sent on GET
    session     a fresh 256-bit token per process, sent only in the X-QuietTV-Session header and
                compared with hmac.compare_digest; it reaches the page in the URL fragment
                (never sent to a server) and the page removes it at once
    body        POST only, Content-Length <= 4 KiB, Content-Type application/json, strict JSON
                (duplicate keys and NaN refused), an object with exactly the route's keys
    routes      a fixed table; any query string is refused; no file, shell, proxy or upload route.
                /api/preflight takes an empty object only; the saved-report sources are named once
                at startup by --preflight-config, never over HTTP (no path, policy, profile or id)
    responses   no CORS headers, no-store, nosniff, no-referrer, CSP without inline script/style
    logging     none: no request line, path, header or token is ever written

The token is never printed. With --no-browser, --launch-file writes the private sign-in link to
a NEW owner-only file outside every repository and deletes it when the Helper stops.

Run:  python -B helper/server.py               (opens your default browser on 127.0.0.1)
      python -B helper/server.py --no-browser --launch-file <OWNER_STORAGE>\\quiettv-link.txt
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True  # importing tvkit must never create files under public-toolkit/

import argparse
import hmac
import json
import os
import re
import secrets
import socket
import socketserver
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

HELPER_DIR = Path(__file__).resolve().parent
if str(HELPER_DIR) not in sys.path:
    sys.path.insert(0, str(HELPER_DIR))
import owner_init as oi  # noqa: E402
import session as hs  # noqa: E402
from tvkit import strictjson as sj  # noqa: E402  (path set up by session)

BIND_ADDRESS = "127.0.0.1"
TOKEN_HEADER = "X-QuietTV-Session"
MAX_BODY = 4096
DRAIN_LIMIT = 64 * 1024  # a refused request's unread body is discarded up to this size
MAX_CONCURRENT = 16
SOCKET_TIMEOUT_S = 10
JSON_TYPES = ("application/json", "application/json;charset=utf-8")
UI_DIR = HELPER_DIR / "ui"
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
ACTION_ROUTES = {"/api/action/" + name: name for name in hs.ACTIONS}
# /api/preflight takes an empty object only: it never carries a path, policy, profile or identity;
# the sources are fixed at startup by --preflight-config.
POST_KEYS = {"/api/target": ("ip",), "/api/check": ("target",), "/api/cancel": ("jobId",),
             "/api/preflight": ()}
SECURITY_HEADERS = (
    ("Cache-Control", "no-store"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Frame-Options", "DENY"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; "
                                "connect-src 'self'; img-src 'self'; base-uri 'none'; "
                                "form-action 'none'; frame-ancestors 'none'"),
)


class _Refused(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class _LoopbackServer(ThreadingHTTPServer):
    allow_reuse_address = False  # SO_REUSEADDR on Windows would let another process share the port
    daemon_threads = True

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        socketserver.TCPServer.server_bind(self)  # skip HTTPServer's getfqdn() name lookup
        host, port = self.server_address[:2]
        self.server_name, self.server_port = host, port

    def handle_error(self, request: Any, client_address: Any) -> None:
        print("QuietTV Helper: a request failed (" + type(sys.exc_info()[1]).__name__ + ")", file=sys.stderr)


def load_static() -> dict:
    return {route: (UI_DIR.joinpath(name).read_bytes(), ctype) for route, (name, ctype) in STATIC.items()}


def _make_handler(app: "HelperApp") -> type:
    class Handler(BaseHTTPRequestHandler):
        server_version = "QuietTVHelper"
        sys_version = ""
        timeout = SOCKET_TIMEOUT_S

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return  # never log request lines, paths or headers

        def handle(self) -> None:
            if not app.slots.acquire(blocking=False):
                self.close_connection = True
                return
            try:
                super().handle()
            finally:
                app.slots.release()

        # -- responses --

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for name, value in SECURITY_HEADERS:
                self.send_header(name, value)
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

        def _json(self, status: int, value: Any) -> None:
            self._send(status, json.dumps(value, ensure_ascii=True).encode("ascii"),
                       "application/json; charset=utf-8")

        # -- checks --

        def _one(self, name: str) -> str | None:
            values = self.headers.get_all(name) or []
            if len(values) > 1:
                raise _Refused(400, "Duplicate " + name + " header.")
            return values[0] if values else None

        def _check_host_and_path(self) -> None:
            if self._one("Host") != app.host:
                raise _Refused(421, "Wrong host.")
            if "?" in self.path or "#" in self.path:
                raise _Refused(400, "Query strings are not accepted.")

        def _check_api(self, mutation: bool) -> None:
            origin = self._one("Origin")
            if (origin is None and mutation) or (origin is not None and origin != app.origin):
                raise _Refused(403, "Wrong or missing origin.")
            token = self._one(TOKEN_HEADER)
            if token is None or not hmac.compare_digest(token.encode("utf-8", "replace"), app.token_bytes):
                raise _Refused(403, "Missing or wrong session. Reopen QuietTV Helper from its shortcut.")

        def _read_body(self, keys: tuple | None) -> Any:
            if self.headers.get_all("Transfer-Encoding"):
                raise _Refused(400, "Chunked bodies are not accepted.")
            ctype = self._one("Content-Type")
            if ctype is None or ctype.replace(" ", "").lower() not in JSON_TYPES:
                raise _Refused(415, "Send JSON.")
            length = self._one("Content-Length")
            if length is None or not re.fullmatch(r"[0-9]{1,6}", length):
                raise _Refused(411, "Content-Length is required.")
            if int(length) > MAX_BODY:
                raise _Refused(413, "Request too large.")
            self._body_read = True
            raw = self.rfile.read(int(length))
            if len(raw) != int(length):
                raise _Refused(400, "Incomplete request.")
            try:
                body = sj.loads(raw)
            except (sj.StrictJsonError, RecursionError, ValueError):
                raise _Refused(400, "Request is not valid JSON.") from None
            if not isinstance(body, dict) or (keys is not None and set(body) != set(keys)):
                raise _Refused(400, "Unexpected request.")
            return body

        # -- methods --

        def do_GET(self) -> None:  # noqa: N802
            try:
                self._check_host_and_path()
                if self.path in app.static:
                    body, ctype = app.static[self.path]
                    self._send(200, body, ctype)
                    return
                if self.path == "/favicon.ico":  # browsers ask on their own; answer quietly
                    self._send(204, b"", "text/plain")
                    return
                if self.path == "/api/state":
                    self._check_api(mutation=False)
                    self._json(200, app.session.state())
                    return
                raise _Refused(404, "Not found.")
            except _Refused as exc:
                self._json(exc.status, {"error": exc.message})

        def _drain(self) -> None:
            """Read (and discard) a small unread body so Windows does not reset the refusal."""
            if getattr(self, "_body_read", False) or self.headers.get_all("Transfer-Encoding"):
                return
            values = self.headers.get_all("Content-Length") or []
            if len(values) == 1 and re.fullmatch(r"[0-9]{1,6}", values[0]) and int(values[0]) <= DRAIN_LIMIT:
                try:
                    self.rfile.read(int(values[0]))
                except OSError:
                    pass

        def do_POST(self) -> None:  # noqa: N802
            try:
                self._check_host_and_path()
                if self.path not in POST_KEYS and self.path not in ACTION_ROUTES:
                    raise _Refused(404, "Not found.")
                self._check_api(mutation=True)
                body = self._read_body(POST_KEYS.get(self.path))
                if self.path == "/api/target":
                    self._json(200, app.session.set_target(body["ip"]))
                elif self.path == "/api/check":
                    self._json(202, app.session.start_check(body["target"]))
                elif self.path == "/api/cancel":
                    self._json(200, app.session.cancel(body["jobId"]))
                elif self.path == "/api/preflight":
                    self._json(200, app.session.run_preflight())
                else:
                    self._json(409, app.session.refuse_action(ACTION_ROUTES[self.path], body))
            except _Refused as exc:
                self._drain()
                self._json(exc.status, {"error": exc.message})
            except hs.SessionError as exc:
                self._json(exc.status, {"error": str(exc)})

        def _not_allowed(self) -> None:
            try:
                self._check_host_and_path()
                raise _Refused(405, "Method not allowed.")
            except _Refused as exc:
                self._json(exc.status, {"error": exc.message})

        do_HEAD = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_TRACE = do_CONNECT = _not_allowed

    return Handler


class HelperApp:
    """One Helper process: one session, one token, one loopback listener."""

    def __init__(self, session: hs.Session | None = None, *, token: str | None = None):
        self.session = session if session is not None else hs.Session()
        self.token = token if token is not None else secrets.token_urlsafe(32)  # 256 bits
        if len(self.token) < 43:
            raise ValueError("session token must carry at least 256 bits")
        self.token_bytes = self.token.encode("ascii")
        self.static = load_static()
        self.slots = threading.BoundedSemaphore(MAX_CONCURRENT)
        self.httpd = _LoopbackServer((BIND_ADDRESS, 0), _make_handler(self))
        self.port = self.httpd.server_address[1]
        self.host = f"{BIND_ADDRESS}:{self.port}"
        self.origin = f"http://{self.host}"
        self._thread: threading.Thread | None = None

    def launch_url(self) -> str:
        return f"{self.origin}/#t={self.token}"

    def start(self) -> None:
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True, name="quiettv-helper")
        self._thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.session.shutdown()  # cancels any check and joins its reader thread (bounded)


def write_launch_file(path: str, url: str) -> Path:
    """Owner-only, new file outside every repository; never overwrites."""
    target = oi.check_outside_repos(path)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    os.close(fd)
    try:
        oi.make_private_file(target)  # restrict access before the secret is written
        oi.check_outside_repos(target, must_not_exist=False)
        with open(target, "r+b") as handle:
            handle.write(url.encode("ascii") + b"\n")
    except BaseException:
        os.unlink(target)
        raise
    return target


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="QuietTV Helper (developer preview): local page on 127.0.0.1")
    ap.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    ap.add_argument("--launch-file", default=None,
                    help="with --no-browser: write the private sign-in link to this NEW file "
                         "(outside every repository); it is deleted when the Helper stops")
    ap.add_argument("--preflight-config", default=None,
                    help="enable the advanced saved-report preflight: a strict-JSON config naming "
                         "four LOCAL files (ProductInfo report, native inspection frame, trusted "
                         "join policy, profile) and the operator's trust/pairing assertions. No "
                         "HTTP route ever accepts a path, policy, profile or identity")
    return ap


def main(argv: list | None = None, *, browser_open: Callable[[str], Any] = webbrowser.open,
         serve: Callable[[HelperApp], None] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.launch_file and not args.no_browser:
        print("QuietTV Helper: --launch-file needs --no-browser", file=sys.stderr)
        return 2
    preflight_config = None
    if args.preflight_config is not None:
        # Load and validate the config BEFORE binding a port, so a bad config never starts a server.
        try:
            preflight_config = hs.load_preflight_config(args.preflight_config)
        except (hs.PreflightConfigError, OSError) as exc:
            message = str(exc) if isinstance(exc, hs.PreflightConfigError) else type(exc).__name__
            print("QuietTV Helper: preflight config refused: " + message, file=sys.stderr)
            return 2
    app = HelperApp(hs.Session(preflight_config=preflight_config))
    launch_file = None
    try:
        if args.launch_file:
            try:
                launch_file = write_launch_file(args.launch_file, app.launch_url())
            except (oi.OwnerInitError, OSError) as exc:
                message = str(exc) if isinstance(exc, oi.OwnerInitError) else type(exc).__name__
                print("QuietTV Helper: launch file refused: " + message, file=sys.stderr)
                return 2
        print(f"QuietTV Helper is running at {app.origin}/ - press Ctrl+C to stop.", flush=True)
        if not args.no_browser:
            browser_open(app.launch_url())
        elif launch_file is None:
            print("No browser opened. Use --launch-file to get a private sign-in link.", flush=True)
        else:
            print("Your private sign-in link is in the launch file you named.", flush=True)
        if serve is not None:
            serve(app)
        else:
            try:
                app.httpd.serve_forever()
            except KeyboardInterrupt:
                pass
    finally:
        app.httpd.server_close()
        if launch_file is not None and os.path.lexists(launch_file):
            os.unlink(launch_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

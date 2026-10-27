"""helper/server.py over a real 127.0.0.1 socket: Host, Origin, token, body, routes, leaks, actions.

Every TV answer is SYNTHETIC. The probe test runs the production socket reader against a local
127.0.0.1 fixture (only its port is injected); no real TV, network or browser is used.
Run: python -B -m unittest tests.test_helper_server
"""
import contextlib
import http.client
import io
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "helper"))
sys.path.insert(0, str(REPO / "tests"))
import server as srv  # noqa: E402
import session as hs  # noqa: E402
from test_helper_session import (  # noqa: E402
    OTHER, SECRET_VALUES, TARGET, TV_BYTES, CountingEngine, FakeReader, Fixture,
)

UNSET = object()


class LoopbackReader:
    """The real reader; the session's validated target is checked, then the fixture is read."""

    def __init__(self, port):
        self.inner, self.calls = hs.HttpInfoReader(port=port, deadline=4), []

    def read(self, ip, cancel=None):
        self.calls.append(ip)
        assert ip == TARGET
        return self.inner.read("127.0.0.1", cancel)


class ServerCase(unittest.TestCase):
    reader_factory = FakeReader

    def setUp(self):
        self.reader = self.make_reader()
        self.engine = CountingEngine()
        self.engine.capabilities = frozenset()
        self.app = srv.HelperApp(hs.Session(reader=self.reader, engine=self.engine))
        self.app.start()
        self.bodies = []
        self.stderr = io.StringIO()
        self._redirect = contextlib.redirect_stderr(self.stderr)
        self._redirect.__enter__()

    def make_reader(self):
        return FakeReader()

    def tearDown(self):
        self.app.close()
        self._redirect.__exit__(None, None, None)
        everything = "\n".join(self.bodies) + self.stderr.getvalue()
        self.assertNotIn(self.app.token, everything)
        for value in SECRET_VALUES:
            self.assertNotIn(value, everything)

    def call(self, method, path, body=None, *, host=UNSET, origin=UNSET, token=UNSET, ctype=UNSET,
             raw=None, extra=()):
        conn = http.client.HTTPConnection("127.0.0.1", self.app.port, timeout=10)
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", self.app.host if host is UNSET else host) if host is not None else None
        if origin is UNSET:
            origin = self.app.origin if method == "POST" else None
        if origin is not None:
            conn.putheader("Origin", origin)
        token = self.app.token if token is UNSET else token
        if token is not None:
            conn.putheader(srv.TOKEN_HEADER, token)
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if data is not None:
            conn.putheader("Content-Type", "application/json" if ctype is UNSET else ctype)
            conn.putheader("Content-Length", str(len(data)))
        for k, v in extra:
            conn.putheader(k, v)
        conn.endheaders()
        if data is not None:
            conn.send(data)
        res = conn.getresponse()
        text = res.read().decode("utf-8", "replace")
        headers = dict(res.getheaders())
        conn.close()
        self.bodies.append(text)
        self.last_headers = headers
        try:
            payload = json.loads(text)
        except ValueError:
            payload = text
        return res.status, payload

    def run_check(self):
        self.assertEqual(self.call("POST", "/api/target", {"ip": TARGET})[0], 200)
        status, st = self.call("POST", "/api/check", {"target": TARGET})
        self.assertEqual(status, 202)
        self.assertTrue(self.app.session.wait_idle())
        return self.call("GET", "/api/state")[1]


class BindTests(ServerCase):
    def test_loopback_only_random_port(self):
        self.assertEqual(self.app.httpd.server_address[0], "127.0.0.1")
        self.assertNotEqual(self.app.port, 0)
        self.assertFalse(srv._LoopbackServer.allow_reuse_address)
        other = srv.HelperApp(hs.Session(reader=FakeReader()))
        try:
            self.assertNotEqual(other.port, self.app.port)
            self.assertNotEqual(other.token, self.app.token)
        finally:
            other.httpd.server_close()

    @unittest.skipUnless(hasattr(socket, "SO_EXCLUSIVEADDRUSE"), "Windows only")
    def test_port_is_exclusive(self):
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        with self.assertRaises(OSError):
            s.bind(("127.0.0.1", self.app.port))
        s.close()

    def test_token_has_256_bits(self):
        self.assertGreaterEqual(len(self.app.token), 43)
        with self.assertRaises(ValueError):
            srv.HelperApp(hs.Session(reader=FakeReader()), token="short")

    def test_cli_has_no_port_host_or_target_option(self):
        opts = {a for action in srv.build_parser()._actions for a in action.option_strings}
        self.assertEqual(opts, {"-h", "--help", "--no-browser", "--launch-file"})


class StaticTests(ServerCase):
    def test_page_and_assets_served_with_headers(self):
        for path, kind in (("/", "text/html"), ("/app.js", "text/javascript"), ("/style.css", "text/css")):
            status, _ = self.call("GET", path, token=None)
            self.assertEqual(status, 200)
            self.assertTrue(self.last_headers["Content-Type"].startswith(kind))
            self.assertEqual(self.last_headers["Cache-Control"], "no-store")
            self.assertEqual(self.last_headers["X-Content-Type-Options"], "nosniff")
            self.assertIn("frame-ancestors 'none'", self.last_headers["Content-Security-Policy"])
            self.assertNotIn("unsafe-inline", self.last_headers["Content-Security-Policy"])
            self.assertFalse(any(h.lower().startswith("access-control") for h in self.last_headers))
        self.assertEqual(self.call("GET", "/favicon.ico", token=None)[0], 204)

    def test_no_file_or_traversal_routes(self):
        for path in ("/index.html", "/ui/app.js", "/../server.py", "/%2e%2e/server.py", "/helper/session.py",
                     "/api/file", "/api/shell", "/api/exec", "/api/proxy", "/api/upload", "//etc/passwd",
                     "http://127.0.0.1/api/state"):
            self.assertEqual(self.call("GET", path)[0], 404, path)

    def test_query_strings_refused(self):
        self.assertEqual(self.call("GET", "/?t=" + "a" * 43, token=None)[0], 400)
        self.assertEqual(self.call("GET", "/api/state?token=" + self.app.token, token=None)[0], 400)
        self.assertEqual(self.call("GET", "/api/state?x=1")[0], 400)


class HostOriginTokenTests(ServerCase):
    def test_accept_path(self):
        status, st = self.call("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertTrue(st["preview"])
        self.assertEqual(self.call("GET", "/api/state", origin=self.app.origin)[0], 200)

    def test_foreign_host(self):
        for host in ("localhost:%d" % self.app.port, "127.0.0.1", "127.0.0.1:1", "evil.example:%d" % self.app.port,
                     "[::1]:%d" % self.app.port, "", "127.0.0.1:%d.evil" % self.app.port):
            self.assertEqual(self.call("GET", "/api/state", host=host)[0], 421, host)
            self.assertEqual(self.call("GET", "/", host=host, token=None)[0], 421, host)
        self.assertEqual(self.call("GET", "/api/state", host=None)[0], 421)
        self.assertEqual(self.call("GET", "/api/state", extra=[("Host", self.app.host)])[0], 400)

    def test_origin_rules(self):
        body = {"ip": TARGET}
        for origin in ("http://localhost:%d" % self.app.port, "http://127.0.0.1", "null", "https://127.0.0.1:%d"
                       % self.app.port, "http://evil.example", self.app.origin + "/"):
            self.assertEqual(self.call("POST", "/api/target", body, origin=origin)[0], 403, origin)
            self.assertEqual(self.call("GET", "/api/state", origin=origin)[0], 403, origin)
        self.assertEqual(self.call("POST", "/api/target", body, origin=None)[0], 403)  # missing on mutation
        self.assertEqual(self.call("POST", "/api/target", body, extra=[("Origin", self.app.origin)])[0], 400)
        self.assertIsNone(self.app.session.state()["target"])

    def test_token_rules(self):
        wrong = self.app.token[:-1] + ("A" if self.app.token[-1] != "A" else "B")
        for token in (None, "", wrong, self.app.token + "x", self.app.token.lower() if self.app.token.lower()
                      != self.app.token else wrong):
            self.assertEqual(self.call("GET", "/api/state", token=token)[0], 403)
            self.assertEqual(self.call("POST", "/api/target", {"ip": TARGET}, token=token)[0], 403)
        self.assertEqual(self.call("GET", "/api/state", extra=[(srv.TOKEN_HEADER, self.app.token)])[0], 400)
        self.assertEqual(self.call("GET", "/api/state", token=None,
                                   extra=[("Authorization", "Bearer " + self.app.token),
                                          ("Cookie", "t=" + self.app.token)])[0], 403)
        self.assertIsNone(self.app.session.state()["target"])

    def test_no_cors(self):
        status, _ = self.call("OPTIONS", "/api/target", token=None,
                              extra=[("Access-Control-Request-Method", "POST")])
        self.assertEqual(status, 405)
        self.assertFalse(any(h.lower().startswith("access-control") for h in self.last_headers))
        for method in ("PUT", "DELETE", "PATCH", "HEAD"):
            self.assertEqual(self.call(method, "/api/state", token=None)[0], 405 if method != "HEAD" else 405)


class BodyTests(ServerCase):
    def test_body_rules(self):
        cases = [
            dict(ctype="text/plain"), dict(ctype="application/x-www-form-urlencoded"),
            dict(ctype="multipart/form-data"), dict(ctype="application/json, text/plain"),
            dict(raw=('{"ip": "%s", "ip": "%s"}' % (TARGET, OTHER)).encode()),
            dict(raw=b'{"ip": NaN}'), dict(raw=b"not json"), dict(raw=b"[" * 3000 + b"]" * 1000),
            dict(raw=('["%s"]' % TARGET).encode()), dict(raw=('{"ip": "%s", "port": 8001}' % TARGET).encode()),
            dict(raw=('{"ip": "%s", "url": "http://x"}' % TARGET).encode()), dict(raw=b"{}"),
            dict(raw=b"\xef\xbb\xbf" + json.dumps({"ip": TARGET}).encode()),
        ]
        for kw in cases:
            status, _ = self.call("POST", "/api/target", {"ip": TARGET}, **kw)
            self.assertIn(status, (400, 415), kw)
        self.assertEqual(self.call("POST", "/api/target", raw=b"{" + b" " * srv.MAX_BODY + b"}")[0], 413)
        self.assertEqual(self.call("POST", "/api/target", {"ip": TARGET},
                                   extra=[("Transfer-Encoding", "chunked")])[0], 400)
        self.assertEqual(self.call("POST", "/api/target", {"ip": TARGET},
                                   extra=[("Content-Type", "application/json")])[0], 400)
        for bad_length in ("²", "¹", "+5", "0x10", "-1", "1e3", "1234567"):
            conn = http.client.HTTPConnection("127.0.0.1", self.app.port, timeout=10)
            conn.putrequest("POST", "/api/target", skip_host=True, skip_accept_encoding=True)
            for k, v in (("Host", self.app.host), ("Origin", self.app.origin), (srv.TOKEN_HEADER, self.app.token),
                         ("Content-Type", "application/json")):
                conn.putheader(k, v)
            conn._output(b"Content-Length: " + bad_length.encode("latin-1"))
            conn.endheaders()
            res = conn.getresponse()
            self.bodies.append(res.read().decode())
            conn.close()
            self.assertIn(res.status, (400, 411), repr(bad_length))
        self.assertIsNone(self.app.session.state()["target"])
        self.assertEqual(self.call("POST", "/api/target", {"ip": TARGET},
                                   ctype="application/json; charset=utf-8")[0], 200)

    def test_bad_targets_never_read(self):
        for ip in ("127.0.0.1", "8.8.8.8", "tv.local", TARGET + ":8001", "100.64.0.1"):
            self.assertEqual(self.call("POST", "/api/target", {"ip": ip})[0], 400, ip)
        self.assertEqual(self.reader.calls, [])


class FlowTests(ServerCase):
    def test_no_passive_scan(self):
        for _ in range(3):
            self.call("GET", "/api/state")
            self.call("GET", "/", token=None)
        self.call("POST", "/api/target", {"ip": TARGET})
        self.assertEqual(self.reader.calls, [])

    def test_check_flow_and_lock(self):
        st = self.run_check()
        self.assertEqual(self.reader.calls, [TARGET])
        self.assertTrue(st["check"]["modelSupported"])
        self.assertFalse(st["check"]["installEligible"])
        self.assertEqual(self.call("POST", "/api/check", {"target": OTHER})[0], 409)
        self.assertEqual(self.call("POST", "/api/cancel", {"jobId": "0" * 16})[0], 409)
        self.assertEqual(self.call("POST", "/api/cancel", {"jobId": "../x"})[0], 400)

    def test_forged_actions_refused_with_zero_device_calls(self):
        st = self.run_check()
        calls_before = list(self.reader.calls)
        for name in hs.ACTIONS:
            for body in ({}, {"componentIds": ["com.samsung.tv.synthetic"]}):
                status, out = self.call("POST", "/api/action/" + name, body)
                self.assertEqual(status, 409, name)
                self.assertTrue(out["refused"])
                self.assertFalse(out["changed"])
                ids = [g["id"] for g in out["gates"]]
                self.assertIn("exact", ids)
                self.assertIn("preview", ids)
        for path in ("/api/action/apply", "/api/action/remove", "/api/action/install", "/api/apply",
                     "/api/remove", "/api/restore", "/api/action/backup/../apply"):
            self.assertEqual(self.call("POST", path, {})[0], 404, path)
        self.assertEqual(self.call("POST", "/api/action/cleanup", raw=b'{"apply": true}')[0], 400)
        self.assertEqual(self.reader.calls, calls_before)
        self.assertEqual(self.engine.calls, 0)
        self.assertTrue(all(a["enabled"] is False for a in st["actions"].values()))
        self.assertTrue(all(not c["available"] for c in st["capabilities"].values()))


class EngineInjectionTests(ServerCase):
    def test_full_capability_engine_still_refused(self):
        self.engine.capabilities = frozenset(hs.CAPABILITIES)
        self.run_check()
        for name in hs.ACTIONS:
            status, out = self.call("POST", "/api/action/" + name, {})
            self.assertEqual(status, 409)
            self.assertFalse(any(g["id"].startswith("capability-") for g in out["gates"]))
            self.assertIn("preview", [g["id"] for g in out["gates"]])
            self.assertFalse(out["changed"])
        self.assertEqual(self.engine.calls, 0)


class RealProbeTests(ServerCase):
    """Valid probe through the production socket reader and a 127.0.0.1 fixture."""

    def make_reader(self):
        self.fixture = Fixture()
        return LoopbackReader(self.fixture.port)

    def tearDown(self):
        super().tearDown()
        self.fixture.close()

    def test_valid_probe_accept_path(self):
        st = self.run_check()
        self.assertEqual(self.fixture.requests, [("/api/v2/", f"127.0.0.1:{self.fixture.port}")])
        check = st["check"]
        self.assertTrue(check["reachable"])
        self.assertEqual(check["model"], "UN43U8000FBXZA")
        self.assertTrue(check["modelSupported"])
        self.assertFalse(check["exactMatchProven"])
        self.assertFalse(check["installEligible"])
        self.assertRegex(check["deviceIdSha256"], r"^[0-9a-f]{64}$")
        self.assertIn("firmware", [m["field"] for m in check["missingFacts"]])


class NetworkErrorTests(ServerCase):
    def make_reader(self):
        fx = Fixture()
        port = fx.port
        fx.close()
        return LoopbackReader(port)

    def test_unreachable(self):
        st = self.run_check()
        self.assertFalse(st["check"]["reachable"])
        self.assertEqual(st["check"]["error"]["code"], "unreachable")


class WrongModelTests(ServerCase):
    def make_reader(self):
        return FakeReader(TV_BYTES.replace(b"UN43U8000FBXZA", b"QN65Q80CAFXZA"))

    def test_wrong_model(self):
        st = self.run_check()
        self.assertFalse(st["check"]["modelSupported"])
        self.assertIn("model", [g["id"] for g in st["actions"]["launcher"]["gates"]])


class MainTests(unittest.TestCase):
    def test_no_browser_never_opens_and_never_prints_token(self):
        opened, seen = [], {}
        out, err = io.StringIO(), io.StringIO()

        def serve(app):
            seen["token"] = app.token

        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(srv.main(["--no-browser"], browser_open=opened.append, serve=serve), 0)
        self.assertEqual(opened, [])
        self.assertNotIn(seen["token"], out.getvalue() + err.getvalue())

    def test_normal_launch_opens_own_loopback_with_fragment_only(self):
        opened, seen = [], {}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            srv.main([], browser_open=opened.append, serve=lambda app: seen.update(origin=app.origin, t=app.token))
        self.assertEqual(opened, [seen["origin"] + "/#t=" + seen["t"]])
        self.assertTrue(opened[0].startswith("http://127.0.0.1:"))
        self.assertNotIn(seen["t"], out.getvalue())

    def test_launch_file_is_new_private_and_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "quiettv-link.txt")
            seen = {}

            def serve(app):
                seen["content"] = Path(path).read_text("ascii").strip()
                seen["url"] = app.launch_url()

            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(srv.main(["--no-browser", "--launch-file", path], browser_open=None, serve=serve), 0)
            self.assertEqual(seen["content"], seen["url"])
            self.assertFalse(os.path.exists(path))
            self.assertNotIn(seen["url"], out.getvalue())
            Path(path).write_text("existing owner state")
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                self.assertEqual(srv.main(["--no-browser", "--launch-file", path], serve=lambda a: None), 2)
            self.assertEqual(Path(path).read_text(), "existing owner state")

    def test_launch_file_refused_inside_repo(self):
        path = str(REPO / "helper" / "quiettv-link.txt")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(srv.main(["--no-browser", "--launch-file", path], serve=lambda a: None), 2)
            self.assertEqual(srv.main(["--launch-file", "C:\\x.txt"], serve=lambda a: None), 2)
        self.assertFalse(os.path.exists(path))


if __name__ == "__main__":
    unittest.main()

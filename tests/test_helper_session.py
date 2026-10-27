"""helper/session.py: target rules, honest facts, one bounded job, refused actions, the real reader.

Every TV answer here is SYNTHETIC and served from 127.0.0.1 by a local fixture; no real TV,
network or internet address is contacted.
Run: python -B -m unittest tests.test_helper_session
"""
import json
import os
import socket
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "helper"))
import session as hs  # noqa: E402

MODEL = "UN43U8000FBXZA"


def ip4(*octets):
    """Synthetic LAN addresses are built at run time, so no literal one sits in public source."""
    return ".".join(str(o) for o in octets)


HOME_NET = (192, 168, 1)
TARGET, OTHER = ip4(*HOME_NET, 20), ip4(*HOME_NET, 21)
TEN_NET_TV = ip4(10, 0, 0, 5)
TV_SELF_IP, DEV_PC_IP = ip4(*HOME_NET, 77), ip4(*HOME_NET, 50)
SYN_MAC = ":".join(f"{b:02x}" for b in (0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0x01))
RAW_DUID = "uuid:SYNTHETIC-DUID-0001"
TV_INFO = {
    "device": {
        "FrameTVSupport": "false", "OS": "Tizen", "developerMode": "1", "developerIP": DEV_PC_IP,
        "duid": RAW_DUID, "id": RAW_DUID, "ip": TV_SELF_IP, "modelName": MODEL,
        "name": "[TV] Synthetic Living Room", "type": "Samsung SmartTV", "wifiMac": SYN_MAC,
        "firmwareVersion": "1300.8", "countryCode": "US", "model": "22_SYNTH_UHD",
    },
    "id": RAW_DUID, "isSupport": "{\"remote\":\"true\"}", "name": "[TV] Synthetic Living Room",
    "remote": "1.0", "type": "Samsung SmartTV", "uri": f"http://{TV_SELF_IP}:8001/api/v2/", "version": "2.0.25",
}
TV_BYTES = json.dumps(TV_INFO).encode()
SECRET_VALUES = (RAW_DUID, SYN_MAC, TV_SELF_IP, DEV_PC_IP, "Synthetic Living Room")


def info(**device_over):
    doc = json.loads(TV_BYTES)
    doc["device"].update(device_over)
    return json.dumps(doc).encode()


class FakeReader:
    def __init__(self, result=TV_BYTES, gate=None):
        self.result, self.gate, self.calls = result, gate, []

    def read(self, ip, cancel=None):
        self.calls.append(ip)
        if self.gate is not None:
            self.gate.wait(5)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


# ---- a local fixture standing in for the TV's port; only ever bound to 127.0.0.1 ----

class Fixture:
    def __init__(self, status=200, body=TV_BYTES, headers=()):
        self.requests = []
        fixture = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                fixture.requests.append((self.path, self.headers.get("Host")))
                self.send_response(status)
                for k, v in headers:
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class DripFixture:
    """A raw-socket answer. With drip_head/drip_body each byte is sent alone, DRIP_S apart:
    no single gap is long (far below any inactivity timeout), but the whole answer is."""

    DRIP_S = 0.08

    def __init__(self, head, body=b"", *, drip_head=False, drip_body=False):
        self.stop, self.client_closed = threading.Event(), threading.Event()
        self.connections = 0
        self.pieces = ([bytes([b]) for b in head] if drip_head else [head]) + \
            ([bytes([b]) for b in body] if drip_body else ([body] if body else []))
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.listener.settimeout(0.1)
        self.port = self.listener.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self.stop.is_set():
            try:
                conn, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        with conn:
            conn.settimeout(2)
            try:
                conn.recv(4096)
            except OSError:
                return
            drip = len(self.pieces) > 2
            for piece in self.pieces:
                try:
                    conn.sendall(piece)
                except OSError:
                    self.client_closed.set()
                    return
                if drip and self.stop.wait(self.DRIP_S):
                    return
            if drip:  # keep probing so a reader that hung up is noticed
                while not self.stop.wait(self.DRIP_S):
                    try:
                        conn.sendall(b" ")
                    except OSError:
                        self.client_closed.set()
                        return

    def close(self):
        self.stop.set()
        self.listener.close()


SLOW_HEAD = b"HTTP/1.1 200 OK\r\nX-Pad: " + b"a" * 2000 + b"\r\n\r\n"  # 2000 x 0.08 s = 160 s if read whole
SLOW_BODY_HEAD = b"HTTP/1.1 200 OK\r\nContent-Length: 2000\r\n\r\n"


def slow_headers():
    return DripFixture(SLOW_HEAD, drip_head=True)


def slow_body():
    return DripFixture(SLOW_BODY_HEAD, b" " * 2000, drip_body=True)


def good_raw(head_extra=b"", body=TV_BYTES):
    return DripFixture(b"HTTP/1.1 200 OK\r\n" + head_extra + b"Content-Length: " + str(len(body)).encode()
                       + b"\r\n\r\n", body)


class Mapped:
    """Real reader; each call reads the next fixture port on 127.0.0.1 (the session validated its target)."""

    def __init__(self, *ports, deadline=1.0):
        self.ports, self.deadline, self.calls = list(ports), deadline, []

    def read(self, ip, cancel=None):
        self.calls.append(ip)
        return hs.HttpInfoReader(port=self.ports.pop(0), deadline=self.deadline).read("127.0.0.1", cancel)


def check_threads_alive():
    return [t for t in threading.enumerate() if t.name == "quiettv-check" and t.is_alive()]


class TargetTests(unittest.TestCase):
    def test_accepts_home_network_addresses(self):
        for ip in (TARGET, TEN_NET_TV, ip4(172, 16, 0, 9), ip4(172, 31, 255, 254)):
            self.assertEqual(hs.validate_target(ip), ip)

    def test_refuses_everything_else(self):
        for bad in ("127.0.0.1", "8.8.8.8", "172.32.0.1", "100.64.0.1", "169.254.1.1", "198.18.0.1",
                    ip4(*HOME_NET, 0), ip4(*HOME_NET, 255), "224.0.0.1", "0.0.0.0", ip4("192", "168", "001", "020"),
                    TARGET + ":8001", "tv.local", ip4(*HOME_NET, 0) + "/24", "http://" + TARGET,
                    " " + TARGET, TARGET + " ", "", None, 3232235796, [TARGET], "::1"):
            with self.assertRaises(hs.SessionError, msg=repr(bad)):
                hs.validate_target(bad)


class InterpretTests(unittest.TestCase):
    def test_projects_only_allowed_fields(self):
        facts = hs.interpret_response(TV_BYTES)
        self.assertEqual(set(facts), {"model", "os", "developerMode", "deviceIdSha256", "deviceIdSource"})
        self.assertEqual(facts["model"], MODEL)
        self.assertEqual(facts["os"], "Tizen")
        self.assertIs(facts["developerMode"], True)
        self.assertEqual(facts["deviceIdSource"], "duid")
        self.assertEqual(facts["deviceIdSha256"], hs.tvprofile.device_id_sha256(RAW_DUID))
        flat = json.dumps(facts)
        for value in SECRET_VALUES:
            self.assertNotIn(value, flat)

    def test_never_fabricates_firmware_region_arch(self):
        facts = hs.interpret_response(TV_BYTES)
        comp = hs.compatibility(facts)
        self.assertTrue(comp["modelSupported"])
        self.assertFalse(comp["exactMatchProven"])
        self.assertFalse(comp["installEligible"])
        missing = [m["field"] for m in comp["missingFacts"]]
        for field in ("firmware", "arch", "osVersion"):
            self.assertIn(field, missing)
        self.assertNotIn("model", missing)
        for key in ("firmware", "region", "arch", "firmwareBuild"):
            self.assertNotIn(key, facts)

    def test_wrong_model_is_not_supported(self):
        comp = hs.compatibility(hs.interpret_response(info(modelName="QN65Q80CAFXZA")))
        self.assertFalse(comp["modelSupported"])
        self.assertEqual(comp["profileIds"], [])
        self.assertFalse(comp["installEligible"])

    def test_model_match_is_exact(self):
        for near in (MODEL.lower(), MODEL + "X", MODEL[:-1]):
            self.assertFalse(hs.compatibility(hs.interpret_response(info(modelName=near)))["modelSupported"])

    def test_missing_device_id_is_reported_as_missing(self):
        doc = json.loads(TV_BYTES)
        del doc["device"]["duid"], doc["device"]["id"]
        facts = hs.interpret_response(json.dumps(doc).encode())
        self.assertIsNone(facts["deviceIdSha256"])

    def test_refuses_bad_shapes(self):
        cases = [
            b"", b"not json", b"[]", b"null", b'{"device": []}', json.dumps({"device": {"modelName": MODEL}}).encode(),
            info(modelName=None), info(modelName="UN43 U8000"), info(modelName="x" * 65),
            json.dumps({**TV_INFO, "type": "Other", "device": {**TV_INFO["device"], "type": "Other"}}).encode(),
            b'{"device": {"modelName": "A", "modelName": "B", "type": "Samsung SmartTV"}}',
            b'{"device": {"modelName": "A", "type": "Samsung SmartTV", "x": NaN}}',
            b"\xef\xbb\xbf" + TV_BYTES, b"\xff\xfe",
            b"[" * 4000 + b"]" * 4000,  # RecursionError inside json must not escape
            b" " * (hs.MAX_RESPONSE_BYTES + 1),
        ]
        for raw in cases:
            with self.assertRaises(hs.ReaderError, msg=raw[:40]):
                hs.interpret_response(raw)

    def test_ignores_junk_in_optional_fields(self):
        facts = hs.interpret_response(info(OS="<script>", developerMode="yes", duid="bad id\n", id=None))
        self.assertIsNone(facts["os"])
        self.assertIsNone(facts["developerMode"])
        self.assertIsNone(facts["deviceIdSha256"])


class SessionTests(unittest.TestCase):
    def test_valid_probe_accept_path(self):
        reader = FakeReader()
        s = hs.Session(reader=reader)
        s.set_target(TARGET)
        s.start_check(TARGET)
        self.assertTrue(s.wait_idle())
        st = s.state()
        self.assertEqual(reader.calls, [TARGET])
        check = st["check"]
        self.assertTrue(check["reachable"])
        self.assertIsNone(check["error"])
        self.assertTrue(check["modelSupported"])
        self.assertFalse(check["exactMatchProven"])
        self.assertFalse(check["installEligible"])
        self.assertEqual(st["lastJob"]["status"], "done")
        self.assertIn("A remote control within reach", " ".join(st["prerequisites"]))
        for value in SECRET_VALUES:
            self.assertNotIn(value, json.dumps(st))

    def test_network_error_is_honest(self):
        s = hs.Session(reader=FakeReader(hs.ReaderError("unreachable")))
        s.set_target(TEN_NET_TV)
        s.start_check(TEN_NET_TV)
        s.wait_idle()
        check = s.state()["check"]
        self.assertFalse(check["reachable"])
        self.assertEqual(check["error"]["code"], "unreachable")
        self.assertNotIn("modelSupported", check)

    def test_broken_reader_does_not_stick(self):
        s = hs.Session(reader=FakeReader(RuntimeError("boom")))
        s.set_target(TEN_NET_TV)
        s.start_check(TEN_NET_TV)
        self.assertTrue(s.wait_idle())
        self.assertEqual(s.state()["check"]["error"]["code"], "unreachable")

    def test_check_needs_target_and_is_locked_to_it(self):
        reader = FakeReader()
        s = hs.Session(reader=reader)
        with self.assertRaises(hs.SessionError):
            s.start_check(TARGET)
        s.set_target(TARGET)
        with self.assertRaises(hs.SessionError) as ctx:
            s.start_check(OTHER)
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(reader.calls, [])

    def test_one_job_cancel_and_no_target_switch_in_flight(self):
        gate = threading.Event()
        reader = FakeReader(gate=gate)
        s = hs.Session(reader=reader)
        s.set_target(TARGET)
        job = s.start_check(TARGET)["job"]
        self.assertEqual(job["status"], "running")
        for call in (lambda: s.start_check(TARGET), lambda: s.set_target(OTHER)):
            with self.assertRaises(hs.SessionError) as ctx:
                call()
            self.assertEqual(ctx.exception.status, 409)
        with self.assertRaises(hs.SessionError):
            s.cancel("0" * 16)
        self.assertEqual(s.cancel(job["id"])["job"]["status"], "cancelling")
        with self.assertRaises(hs.SessionError):  # still in flight until the read returns
            s.set_target(OTHER)
        gate.set()
        self.assertTrue(s.wait_idle())
        st = s.state()
        self.assertIsNone(st["check"])  # a cancelled result is discarded
        self.assertEqual(st["lastJob"]["status"], "cancelled")
        self.assertEqual(reader.calls, [TARGET])
        s.set_target(OTHER)  # allowed once idle

    def test_concurrent_starts_run_one_read(self):
        gate = threading.Event()
        reader = FakeReader(gate=gate)
        s = hs.Session(reader=reader)
        s.set_target(TARGET)
        wins, barrier = [], threading.Barrier(20)

        def go():
            barrier.wait()
            try:
                s.start_check(TARGET)
                wins.append(1)
            except hs.SessionError:
                pass
        threads = [threading.Thread(target=go) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        gate.set()
        s.wait_idle()
        self.assertEqual(len(wins), 1)
        self.assertEqual(len(reader.calls), 1)


class CountingEngine(hs.Engine):
    name = "test-engine"
    capabilities = frozenset(hs.CAPABILITIES)

    def __init__(self):
        self.calls = 0

    def run(self, *a, **k):
        self.calls += 1


class ActionTests(unittest.TestCase):
    def test_every_action_refused_with_concrete_gates(self):
        reader = FakeReader()
        s = hs.Session(reader=reader)
        for name in hs.ACTIONS:
            out = s.refuse_action(name, {})
            self.assertTrue(out["refused"])
            self.assertFalse(out["changed"])
            ids = [g["id"] for g in out["gates"]]
            self.assertIn("check", ids)
            self.assertIn("preview", ids)
            self.assertTrue(any(i.startswith("capability-") for i in ids))
            self.assertTrue(all(g["text"] for g in out["gates"]))
        self.assertEqual(reader.calls, [])
        st = s.state()
        self.assertTrue(all(a["enabled"] is False for a in st["actions"].values()))
        self.assertTrue(all(c["available"] is False and c["reason"] for c in st["capabilities"].values()))
        self.assertEqual(st["engine"], "none")

    def test_engine_with_capabilities_still_refused_and_never_called(self):
        engine, reader = CountingEngine(), FakeReader()
        s = hs.Session(reader=reader, engine=engine)
        s.set_target(TARGET)
        s.start_check(TARGET)
        s.wait_idle()
        for name in hs.ACTIONS:
            out = s.refuse_action(name, {"componentIds": ["com.samsung.tv.synthetic"]})
            ids = [g["id"] for g in out["gates"]]
            self.assertIn("exact", ids)
            self.assertIn("preview", ids)
            self.assertFalse(any(i.startswith("capability-") for i in ids))
        self.assertEqual(engine.calls, 0)
        self.assertEqual(len(reader.calls), 1)

    def test_wrong_model_gate(self):
        s = hs.Session(reader=FakeReader(info(modelName="QN65Q80CAFXZA")))
        s.set_target(TARGET)
        s.start_check(TARGET)
        s.wait_idle()
        ids = [g["id"] for g in s.refuse_action("launcher", {})["gates"]]
        self.assertIn("model", ids)

    def test_bad_action_bodies(self):
        s = hs.Session(reader=FakeReader())
        for body in ([], {"apply": True}, {"componentIds": "x"}, {"componentIds": ["../x"]},
                     {"componentIds": ["a"] * 65}, {"componentIds": [1]}):
            with self.assertRaises(hs.SessionError):
                s.refuse_action("cleanup", body)
        with self.assertRaises(hs.SessionError) as ctx:
            s.refuse_action("shell", {})
        self.assertEqual(ctx.exception.status, 404)


class RealReaderTests(unittest.TestCase):
    """The production socket reader against 127.0.0.1 fixtures; only port and deadline are injected."""

    def read(self, port, deadline=2.0, cancel=None):
        return hs.HttpInfoReader(port=port, deadline=deadline).read("127.0.0.1", cancel)

    def refused(self, port, deadline=2.0):
        with self.assertRaises(hs.ReaderError) as ctx:
            self.read(port, deadline)
        return ctx.exception.code

    def test_production_defaults_are_fixed(self):
        reader = hs.HttpInfoReader()
        self.assertEqual((reader.port, reader.deadline), (8001, 6.0))
        self.assertEqual(hs.TV_PATH, "/api/v2/")

    def test_reads_fixed_path_and_parses(self):
        fx = Fixture()
        try:
            raw = self.read(fx.port)
        finally:
            fx.close()
        self.assertEqual(fx.requests, [("/api/v2/", f"127.0.0.1:{fx.port}")])
        self.assertEqual(hs.interpret_response(raw)["model"], MODEL)

    def test_chunked_and_close_delimited_answers(self):
        chunked = b"".join(b"%x\r\n" % len(TV_BYTES[i:i + 500]) + TV_BYTES[i:i + 500] + b"\r\n"
                           for i in range(0, len(TV_BYTES), 500)) + b"0\r\n\r\n"
        fx1 = DripFixture(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + chunked)
        fx2 = DripFixture(b"HTTP/1.0 200 OK\r\n\r\n" + TV_BYTES)
        try:
            self.assertEqual(self.read(fx1.port), TV_BYTES)
            self.assertEqual(self.read(fx2.port), TV_BYTES)
        finally:
            fx1.close()
            fx2.close()

    def test_refuses_malformed_heads(self):
        cases = {
            b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nContent-Length: 5\r\n\r\nhello": "bad-response",
            b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n": "bad-response",
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: gzip\r\n\r\nx": "bad-response",
            b"HTTP/1.1 200 OK\r\nContent-Length: \xc2\xb2\r\n\r\nx": "bad-response",
            b"HTTP/1.1 200 OK\r\nBad Header: x\r\n\r\n": "bad-response",
            b"SSH-2.0-OpenSSH\r\n\r\n": "bad-response",
            b"HTTP/1.1 200 OK\r\nContent-Length: 999999\r\n\r\n": "too-large",
            b"HTTP/1.1 200 OK\r\nContent-Length: 50\r\n\r\nshort": "bad-response",  # closed early
            b"HTTP/1.1 200 OK\r\nX: " + b"a" * (hs.MAX_HEADER_BYTES + 10): "bad-response",
            b"HTTP/1.1 301 Moved\r\nLocation: http://127.0.0.1:1/\r\n\r\n": "redirect",
            b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n": "http-status",
        }
        for answer, code in cases.items():
            fx = DripFixture(answer)
            try:
                self.assertEqual(self.refused(fx.port), code, answer[:50])
            finally:
                fx.close()

    def test_ignores_environment_proxy(self):
        fx, proxy = Fixture(), Fixture(body=b"{}")
        env = {"http_proxy": f"http://127.0.0.1:{proxy.port}", "HTTP_PROXY": f"http://127.0.0.1:{proxy.port}",
               "no_proxy": "", "NO_PROXY": ""}
        try:
            with mock.patch.dict(os.environ, env):
                self.read(fx.port)
        finally:
            fx.close()
            proxy.close()
        self.assertEqual(len(fx.requests), 1)
        self.assertEqual(proxy.requests, [])

    def test_refuses_redirect_after_one_request(self):
        target = Fixture()
        fx = Fixture(status=302, body=b"", headers=[("Location", f"http://127.0.0.1:{target.port}/api/v2/")])
        try:
            code = self.refused(fx.port)
        finally:
            fx.close()
            target.close()
        self.assertEqual(code, "redirect")
        self.assertEqual(len(fx.requests), 1)
        self.assertEqual(target.requests, [])

    def test_http_error_and_oversize(self):
        fx = Fixture(status=404, body=b"no")
        big = Fixture(body=b"x" * (hs.MAX_RESPONSE_BYTES + 10))
        try:
            self.assertEqual(self.refused(fx.port), "http-status")
            self.assertEqual(self.refused(big.port), "too-large")
        finally:
            fx.close()
            big.close()

    def test_connection_refused_is_unreachable(self):
        fx = Fixture()
        port = fx.port
        fx.close()
        self.assertEqual(self.refused(port, deadline=4.0), "unreachable")

    def assert_cut_off(self, fixture_factory):
        """Every byte arrives 0.08 s after the last, yet the read must stop at its 1 s total deadline."""
        fx, good = fixture_factory(), Fixture()
        try:
            started = time.monotonic()
            self.assertEqual(self.refused(fx.port, deadline=1.0), "too-slow")
            self.assertLess(time.monotonic() - started, 1.0 + 0.5)
            self.assertTrue(fx.client_closed.wait(2), "the reader left its socket open")
            self.assertEqual(hs.interpret_response(self.read(good.port))["model"], MODEL)  # next read works
        finally:
            fx.close()
            good.close()

    def test_slow_headers_hit_the_hard_deadline(self):
        self.assert_cut_off(slow_headers)

    def test_slow_body_hits_the_hard_deadline(self):
        self.assert_cut_off(slow_body)

    def test_cancel_stops_the_read_itself(self):
        for factory in (slow_headers, slow_body):
            fx, cancel, out = factory(), threading.Event(), {}

            def go():
                try:
                    self.read(fx.port, deadline=30.0, cancel=cancel)
                except hs.ReaderError as exc:
                    out["code"] = exc.code
            worker = threading.Thread(target=go)
            try:
                worker.start()
                time.sleep(0.3)
                started = time.monotonic()
                cancel.set()
                worker.join(2)
                self.assertFalse(worker.is_alive())
                self.assertLess(time.monotonic() - started, hs.WAIT_SLICE_S + 0.3)
                self.assertEqual(out["code"], "cancelled")
                self.assertTrue(fx.client_closed.wait(2))
            finally:
                fx.close()


class SessionDeadlineTests(unittest.TestCase):
    def test_slow_answers_fail_bounded_then_next_check_works(self):
        for factory in (slow_headers, slow_body):
            fx, good = factory(), good_raw()
            reader = Mapped(fx.port, good.port, deadline=1.0)
            s = hs.Session(reader=reader)
            try:
                s.set_target(TARGET)
                started = time.monotonic()
                s.start_check(TARGET)
                self.assertTrue(s.wait_idle(timeout=3))
                self.assertLess(time.monotonic() - started, 1.0 + 0.6)
                st = s.state()
                self.assertEqual(st["lastJob"]["status"], "failed")
                self.assertEqual(st["check"]["error"]["code"], "too-slow")
                self.assertTrue(st["check"]["reachable"])
                self.assertEqual(check_threads_alive(), [])
                s.start_check(TARGET)
                self.assertTrue(s.wait_idle(timeout=3))
                self.assertTrue(s.state()["check"]["modelSupported"])
                self.assertEqual(len(reader.calls), 2)
            finally:
                fx.close()
                good.close()

    def test_session_cancel_is_real_and_bounded(self):
        fx = slow_body()
        s = hs.Session(reader=Mapped(fx.port, deadline=30.0))
        try:
            s.set_target(TARGET)
            job = s.start_check(TARGET)["job"]
            time.sleep(0.3)
            started = time.monotonic()
            s.cancel(job["id"])
            self.assertTrue(s.wait_idle(timeout=1.0))
            self.assertLess(time.monotonic() - started, hs.WAIT_SLICE_S + 0.4)
            self.assertEqual(s.state()["lastJob"]["status"], "cancelled")
            self.assertIsNone(s.state()["check"])
            self.assertEqual(check_threads_alive(), [])
            self.assertTrue(fx.client_closed.wait(2))
        finally:
            fx.close()

    def test_shutdown_joins_the_reader(self):
        fx = slow_headers()
        s = hs.Session(reader=Mapped(fx.port, deadline=30.0))
        try:
            s.set_target(TARGET)
            s.start_check(TARGET)
            time.sleep(0.2)
            started = time.monotonic()
            self.assertTrue(s.shutdown(timeout=2))
            self.assertLess(time.monotonic() - started, hs.WAIT_SLICE_S + 0.4)
            self.assertEqual(check_threads_alive(), [])
        finally:
            fx.close()


class BrokenThread:
    def __init__(self, **kw):
        pass

    def start(self):
        raise RuntimeError("can't start new thread")


class ExplodingFactory:
    def __call__(self, **kw):
        raise RuntimeError("no threads")


class StartFailureTests(unittest.TestCase):
    def test_failed_start_does_not_stick_and_next_check_works(self):
        for broken in (BrokenThread, ExplodingFactory()):
            reader, calls = FakeReader(), []

            def factory(**kw):
                calls.append(1)
                return broken(**kw) if len(calls) == 1 else threading.Thread(**kw)
            s = hs.Session(reader=reader, thread_factory=factory)
            s.set_target(TARGET)
            with self.assertRaises(hs.SessionError) as ctx:
                s.start_check(TARGET)
            self.assertEqual(ctx.exception.status, 500)
            st = s.state()
            self.assertIsNone(st["job"])
            self.assertEqual(st["lastJob"]["status"], "failed")
            self.assertEqual(st["check"]["error"]["code"], "start-failed")
            self.assertEqual(reader.calls, [])  # zero device calls
            self.assertTrue(s.wait_idle(timeout=0.5))
            s.set_target(OTHER)  # not locked
            s.start_check(OTHER)
            self.assertTrue(s.wait_idle())
            self.assertEqual(reader.calls, [OTHER])
            self.assertEqual(s.state()["lastJob"]["status"], "done")

    def test_worker_that_runs_despite_failed_start_cannot_publish(self):
        gate, reader = threading.Event(), None
        reader = FakeReader(gate=gate)
        started = []

        class StartsThenRaises(threading.Thread):
            def start(self):
                super().start()
                started.append(self)
                raise RuntimeError("late failure")
        s = hs.Session(reader=reader, thread_factory=StartsThenRaises)
        s.set_target(TARGET)
        with self.assertRaises(hs.SessionError):
            s.start_check(TARGET)
        gate.set()
        started[0].join(2)
        st = s.state()
        self.assertEqual(st["check"]["error"]["code"], "start-failed")  # the stray result was discarded
        self.assertEqual(st["lastJob"]["status"], "failed")


if __name__ == "__main__":
    unittest.main()

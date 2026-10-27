from __future__ import annotations

import hashlib
import io
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_public_toolkit_fixtures  # noqa: E402,F401  (puts public-toolkit on sys.path)
from tvkit import fetch as f  # noqa: E402

PAYLOAD = b"synthetic release artifact\n" * 1000
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()
# Built at runtime so no secret-looking literal sits in this file.
TOKEN = "tok" + "en-" + "S3CR3T" + "VALUE"
PASSWORD = "pass" + "W0RD" + "xyz"


class FakeResponse:
    def __init__(self, data=PAYLOAD, url="https://example.com/a.tar", length=None, fail=None):
        self._data = io.BytesIO(data)
        self._url = url
        self.headers = {"Content-Length": str(length if length is not None else len(data))}
        self.fail = fail
        self.closed = False

    def geturl(self):
        return self._url

    def read(self, n):
        if self.fail:
            raise self.fail
        return self._data.read(n)

    def close(self):
        self.closed = True


class FetchCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.src = self.tmp / "artifact.tar"
        self.src.write_bytes(PAYLOAD)
        self.dest = self.tmp / "out" / "artifact.tar"
        self.dest.parent.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def assertNothingKept(self):
        self.assertFalse(self.dest.exists())
        self.assertEqual(list(self.dest.parent.iterdir()), [])

    def test_local_copy_verified(self):
        result = f.fetch(str(self.src), self.dest, DIGEST)
        self.assertEqual((result.sha256, result.size), (DIGEST, len(PAYLOAD)))
        self.assertEqual(self.dest.read_bytes(), PAYLOAD)
        self.assertEqual(sorted(p.name for p in self.dest.parent.iterdir()), ["artifact.tar"])

    def test_wrong_hash_keeps_nothing(self):
        with self.assertRaises(f.FetchError):
            f.fetch(str(self.src), self.dest, "0" * 64)
        self.assertNothingKept()

    def test_pin_and_cap_are_mandatory(self):
        for pin in ("", "abc", "g" * 64):
            with self.subTest(pin=pin), self.assertRaises(f.FetchError):
                f.fetch(str(self.src), self.dest, pin)
        with self.assertRaises(f.FetchError):
            f.fetch(str(self.src), self.dest, DIGEST, max_bytes=0)

    def test_size_cap_enforced_while_streaming(self):
        with self.assertRaises(f.FetchError):
            f.fetch(str(self.src), self.dest, DIGEST, max_bytes=100)
        self.assertNothingKept()
        lying = FakeResponse(length=10)
        with self.assertRaises(f.FetchError):
            f.fetch("https://example.com/a.tar", self.dest, DIGEST, max_bytes=1000,
                    opener=lambda req, timeout: lying)
        self.assertNothingKept()

    def test_never_overwrites(self):
        self.dest.write_bytes(b"existing")
        with self.assertRaises(f.FetchError):
            f.fetch(str(self.src), self.dest, DIGEST)
        self.assertEqual(self.dest.read_bytes(), b"existing")

    def test_http_and_other_schemes_refused(self):
        for url in ("http://example.com/a.tar", "ftp://example.com/a.tar", "file:///etc/passwd"):
            with self.subTest(url=url), self.assertRaises(f.FetchError) as ctx:
                f.fetch(url, self.dest, DIGEST, opener=self.fail_if_called)
            self.assertIn("only https://", str(ctx.exception))
        self.assertEqual(self.opener_calls, 0)
        self.assertNothingKept()

    opener_calls = 0

    def fail_if_called(self, *a, **k):
        # fetch wraps every opener exception, so count calls instead of trusting a raise.
        self.opener_calls += 1
        raise AssertionError("opener must not be called")

    def test_userinfo_refused_and_never_printed(self):
        url = f"https://user:{PASSWORD}@example.com/a.tar?{TOKEN}#{TOKEN}"
        with self.assertRaises(f.FetchError) as ctx:
            f.fetch(url, self.dest, DIGEST, opener=self.fail_if_called)
        self.assertNotIn(PASSWORD, str(ctx.exception))
        self.assertNotIn(TOKEN, str(ctx.exception))

    def test_https_success_with_redacted_source(self):
        response = FakeResponse()
        result = f.fetch(f"https://example.com/a.tar?sig={TOKEN}#{TOKEN}", self.dest, DIGEST,
                         opener=lambda req, timeout: response)
        self.assertEqual(result.source, "https://example.com/a.tar")
        self.assertTrue(response.closed)
        self.assertEqual(self.dest.read_bytes(), PAYLOAD)

    def test_errors_never_include_query_or_fragment(self):
        def http_error(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden " + TOKEN, {}, None)

        def url_error(req, timeout):
            raise urllib.error.URLError(OSError("connection refused " + TOKEN))

        for opener in (http_error, url_error):
            with self.subTest(opener=opener.__name__), self.assertRaises(f.FetchError) as ctx:
                f.fetch(f"https://example.com/a.tar?key={TOKEN}", self.dest, DIGEST, opener=opener)
            self.assertNotIn(TOKEN, str(ctx.exception))
            self.assertIsNone(ctx.exception.__cause__)

    def test_downgraded_final_url_refused(self):
        response = FakeResponse(url=f"http://example.com/a.tar?{TOKEN}")
        with self.assertRaises(f.FetchError) as ctx:
            f.fetch("https://example.com/a.tar", self.dest, DIGEST, opener=lambda req, timeout: response)
        self.assertNotIn(TOKEN, str(ctx.exception))
        self.assertNothingKept()

    def test_redirect_handler_refuses_downgrade_and_credentials(self):
        handler = f._HttpsOnlyRedirects()
        request = urllib.request.Request("https://example.com/a")
        for target in ("http://example.com/b", f"https://u:{PASSWORD}@example.com/b"):
            with self.subTest(target=target), self.assertRaises(f.FetchError) as ctx:
                handler.redirect_request(request, None, 302, "Found", {}, target)
            self.assertNotIn(PASSWORD, str(ctx.exception))
        self.assertIsNotNone(handler.redirect_request(request, None, 302, "Found", {}, "https://example.org/b"))

    def test_read_timeout_cleans_partial(self):
        response = FakeResponse(fail=TimeoutError("timed out"))
        with self.assertRaises(f.FetchError):
            f.fetch("https://example.com/a.tar", self.dest, DIGEST, opener=lambda req, timeout: response)
        self.assertNothingKept()

    def test_timeout_is_passed_to_opener(self):
        seen = {}

        def opener(req, timeout):
            seen["timeout"] = timeout
            return FakeResponse()

        f.fetch("https://example.com/a.tar", self.dest, DIGEST, read_timeout=7.5, opener=opener)
        self.assertEqual(seen["timeout"], 7.5)

    def test_url_with_space_never_leaks_through_cli(self):
        import contextlib
        from tvkit import cli
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["fetch", f"https://example.com/a.tar?k={TOKEN} x", "--sha256", DIGEST,
                             "--out", str(self.dest)])
        self.assertEqual(code, 1)
        self.assertNotIn(TOKEN, out.getvalue() + err.getvalue())
        self.assertIn("spaces or control characters", err.getvalue())
        self.assertNothingKept()

    def test_unexpected_library_errors_are_wrapped(self):
        import http.client

        def invalid(req, timeout):
            raise http.client.InvalidURL("URL can't contain control characters. " + TOKEN)

        incomplete = FakeResponse(fail=http.client.IncompleteRead(TOKEN.encode()))
        for opener in (invalid, lambda req, timeout: incomplete):
            with self.subTest(opener=opener), self.assertRaises(f.FetchError) as ctx:
                f.fetch(f"https://example.com/a.tar?k={TOKEN}", self.dest, DIGEST, opener=opener)
            self.assertNotIn(TOKEN, str(ctx.exception))
            self.assertIsNone(ctx.exception.__cause__)
            self.assertNothingKept()

    def test_redact_url(self):
        self.assertEqual(f.redact_url(f"https://u:{PASSWORD}@host.example:8443/p/q?{TOKEN}#{TOKEN}"),
                         "https://host.example:8443/p/q")
        self.assertEqual(f.redact_url("not a url"), "<unparseable URL>")


if __name__ == "__main__":
    unittest.main()

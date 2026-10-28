import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "public-toolkit"))
from tvkit.package_stream import decode_package_stream, StreamRefused, TAG, CHUNK


def frames(data=b"package contents", package="fixture.app"):
    chunks = [data[n:n+CHUNK] for n in range(0, len(data), CHUNK)]
    out = [TAG + b"BEGIN package " + package.encode() + b"\n"]
    for seq, chunk in enumerate(chunks):
        out.append(TAG + b"DATA " + str(seq).encode() + b" " + base64.b64encode(chunk)
                   + b" " + hashlib.sha256(chunk).hexdigest().encode() + b"\n")
    out.append(TAG + b"END " + str(len(chunks)).encode() + b" " + str(len(data)).encode()
               + b" " + hashlib.sha256(data).hexdigest().encode() + b"\n")
    return out


class Closeable:
    def __init__(self, rows): self.rows, self.closed = iter(rows), False
    def __iter__(self): return self
    def __next__(self): return next(self.rows)
    def close(self): self.closed = True


class PackageStreamTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.parent = Path(self.temp.name)

    def decode(self, rows=None, **kw):
        opts = dict(expected_package_id="fixture.app", max_bytes=1 << 20,
                    spool_parent=self.parent, exit_code=lambda: 0)
        opts.update(kw)
        return decode_package_stream(frames() if rows is None else rows, **opts)

    def assert_refused(self, rows, **kw):
        source = Closeable(rows)
        with self.assertRaises(StreamRefused): self.decode(source, **kw)
        self.assertTrue(source.closed)
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_stream_matches_device_digest_and_closes_on_consumption(self):
        data = b"x" * (CHUNK * 3 + 31)
        src = Closeable(frames(data))
        observed_eof = []
        result = self.decode(src, exit_code=lambda: observed_eof.append(True) or 0)
        self.assertTrue(src.closed)
        self.assertEqual(observed_eof, [True])
        self.assertEqual(result.device_sha256, hashlib.sha256(data).hexdigest())
        self.assertEqual(result.device_size, len(data))
        self.assertEqual(b"".join(result.chunks), data)
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_early_close_removes_spool(self):
        result = self.decode(frames(b"z" * (CHUNK + 3)))
        self.assertEqual(len(next(result.chunks)), CHUNK)
        result.chunks.close(); result.chunks.close()
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_closing_before_first_read_cleans(self):
        result = self.decode()
        result.chunks.close()
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_replaced_path_survives_cleanup(self):
        result = self.decode()
        self.addCleanup(result.chunks.close)
        path = next(self.parent.iterdir())
        moved = self.parent / "original"
        # Windows prevents replacing an open file. Simulate the same replacement
        # in close()'s real close-before-lstat window; the retained inode still
        # belongs to the original file, not this raced replacement.
        result.chunks.handle.close()
        path.rename(moved)
        path.write_bytes(b"other owner's file")
        result.chunks.close()
        self.assertEqual(path.read_bytes(), b"other owner's file")

    def test_gc_noise_is_allowed_even_between_records(self):
        noise = b"[GC_INFO] GC_End #1: Gen(0), Time(1 ms)\n"
        rows = [noise]
        for record in frames(): rows += [record, noise]
        self.assertEqual(b"".join(self.decode(rows).chunks), b"package contents")

    def test_refusals_leave_no_partial_spool(self):
        good = frames()
        bad = [[], good[:-1], good[1:], good+good, [good[0], good[1], good[1], good[2]],
               [good[0], good[2], good[1]], frames(package="wrong"),
               [*good, b"unknown diagnostic\n"], [*good, TAG + b"END 1 16 " + b"0"*64 + b"\n"],
               [good[0], good[1].replace(b"DATA 0", b"DATA 1"), good[2]],
               [good[0], good[1][:-66] + b"0"*64 + b"\n", good[2]],
               [good[0], good[1], good[2][:-66] + b"0"*64 + b"\n"],
               [good[0], good[1].replace(b"DATA 0", b"DATA 00"), good[2]],
               [b"x"*44000+b"\n"], [good[0].rstrip(b"\n")], ["not bytes"]]
        for rows in bad:
            with self.subTest(rows=repr(rows)[:100]): self.assert_refused(rows)

    def test_limits_exit_provider_and_noise_bound(self):
        self.assert_refused(frames(), max_bytes=2)
        for code in (1, -1, False, "0", None):
            with self.subTest(code=code): self.assert_refused(frames(), exit_code=lambda: code)
        self.assert_refused([b"[GC_INFO] GC_End #1: Gen(0), Time(1 ms)\n"]*4097+frames())

    def test_source_exception_closes_and_removes_spool(self):
        def broken():
            yield frames()[0]
            raise OSError("connection lost")
        with self.assertRaises(OSError): self.decode(broken())
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_git_worktree_storage_is_refused(self):
        for is_file in (False, True):
            repo = self.parent / str(is_file); repo.mkdir()
            git = repo / ".git"
            if is_file: git.write_text("gitdir: somewhere")
            else: git.mkdir()
            child = repo / "nested"; child.mkdir()
            with self.assertRaises(StreamRefused): self.decode(spool_parent=child)
            self.assertEqual(list(child.iterdir()), [])

    def test_protected_tokens_equal_profile(self):
        source = (ROOT/"tools/tv-capture/Program.cs").read_text(encoding="utf-8")
        import re
        tokens = re.findall(r'"([a-z-]+)"', source.split("Protected = {",1)[1].split("};",1)[0])
        profile = json.loads((ROOT/"public-toolkit/profiles/UN43U8000FBXZA-1300.8-armv7l-tizen9.json").read_text())
        self.assertEqual(tokens, profile["protectedIdSubstrings"])


if __name__ == "__main__": unittest.main()

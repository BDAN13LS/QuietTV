"""D2 typed read-only capture engine. Synthetic fixtures and a FAKE backend only.

The fake backend is a test double. It is never a stand-in for a real SDB or root
transport: it serves the synthetic fixtures from memory and reads nothing. Every
test writes owner storage to a temp folder outside the repository.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_public_toolkit_fixtures as fx  # noqa: E402
from tvkit import capture, transport  # noqa: E402
from tvkit.backup import ReadbackRefused  # noqa: E402
from tvkit.capsule import CapsuleError  # noqa: E402
from tvkit.capture import (  # noqa: E402
    BASE_CAPABILITIES, CAP_REGISTRY, CAP_SYSTEM_KEY, CaptureBackend, CaptureRefused, CaptureRequest,
    CaptureResult, CapturedStream, PackageRequest, RegistryRequest, SystemKeyRequest, bytes_stream,
    capture_backup,
)

CAPTURE_SOURCE = fx.TOOLKIT / "tvkit" / "capture.py"
ALL_CAPS = frozenset(BASE_CAPABILITIES | {CAP_REGISTRY, CAP_SYSTEM_KEY})

# The current (before/original) values the TV would report, matching the fixture manifest.
REGISTRY_BEFORE = {
    "org.example.alpha": {"app_disable": "false", "app_onboot": "true"},
    "org.example.bravo": {"app_disable": "0"},
}
REGISTRY_APPLIED = {
    "org.example.alpha": {"app_disable": "true", "app_onboot": "false"},
    "org.example.bravo": {"app_disable": "1"},
}
SYSTEM_KEY_ORIGINAL = {"db/appfw/startup_appid": "org.example.viewer",
                       "db/appfw/home_appid": "org.example.home"}


class ClosableChunks:
    """A one-shot iterator with an explicit close(), so a test can prove the engine
    closes the transfer iterator even when it abandons it on a refusal."""

    def __init__(self, chunks):
        self._items = list(chunks)
        self._index = 0
        self.closed = False
        self.consumed = 0

    def __iter__(self):
        return self

    def __next__(self):
        if self._index >= len(self._items):
            raise StopIteration
        item = self._items[self._index]
        self._index += 1
        self.consumed += 1
        return item

    def close(self):
        self.closed = True


class FakeBackend(CaptureBackend):
    """Serves the synthetic fixtures as bounded streams. Read-only; fault-injectable."""

    name = "fake"

    def __init__(self, *, capabilities=ALL_CAPS, facts=None, facts_sequence=None,
                 payload_mutators=None, xattr_mutators=None, payload_size=None, payload_digest=None,
                 registry=None, system_keys=None, chunk_size=capture.DEFAULT_CHUNK_BYTES):
        _, self.payloads, self.xattrs = fx.build_parts()
        self.capabilities = frozenset(capabilities)
        self._facts = dict(fx.FACTS if facts is None else facts)
        self._facts_sequence = [dict(f) for f in facts_sequence] if facts_sequence else None
        self._payload_mutators = payload_mutators or {}
        self._xattr_mutators = xattr_mutators or {}
        self._payload_size = payload_size or {}
        self._payload_digest = payload_digest or {}
        self._registry = REGISTRY_BEFORE if registry is None else registry
        self._system_keys = SYSTEM_KEY_ORIGINAL if system_keys is None else system_keys
        self._chunk_size = chunk_size
        self.calls: list[str] = []
        self.streams: list[dict] = []

    def read_device_facts(self):
        self.calls.append("facts")
        if self._facts_sequence:
            return self._facts_sequence.pop(0)
        return dict(self._facts)

    def _stream(self, real: bytes, mutator, size_override, digest_override) -> CapturedStream:
        # The TV digest/size are of the REAL file; the transferred bytes may be mutated to fake drift.
        device_sha = digest_override if digest_override is not None else fx.sha(real)
        device_size = size_override if size_override is not None else len(real)
        sent = mutator(real) if mutator else real
        tracker = {"closed": False, "yielded": 0}
        self.streams.append(tracker)

        def generate():
            try:
                for start in range(0, len(sent), self._chunk_size):
                    tracker["yielded"] += 1
                    yield sent[start:start + self._chunk_size]
            finally:
                tracker["closed"] = True

        return CapturedStream(device_sha256=device_sha, device_size=device_size, chunks=generate())

    def capture_package(self, package_id, install_root, *, max_bytes):
        self.calls.append(f"payload:{package_id}")
        assert max_bytes == capture.MAX_MEMBER_BYTES
        return self._stream(self.payloads[package_id], self._payload_mutators.get(package_id),
                            self._payload_size.get(package_id), self._payload_digest.get(package_id))

    def dump_xattrs(self, package_id, install_root, *, max_bytes):
        self.calls.append(f"xattr:{package_id}")
        assert max_bytes == capture.MAX_XATTR_MANIFEST_BYTES
        return self._stream(self.xattrs[package_id], self._xattr_mutators.get(package_id), None, None)

    def read_registry_field(self, app_id, field):
        self.calls.append(f"reg:{app_id}.{field}")
        return self._registry[app_id][field]

    def read_system_key(self, key):
        self.calls.append(f"key:{key}")
        return self._system_keys[key]


def full_request(package_ids=fx.PACKAGE_IDS):
    return CaptureRequest(
        packages=tuple(PackageRequest(pid, f"/opt/usr/apps/{pid}") for pid in package_ids),
        registry=(RegistryRequest("org.example.alpha", REGISTRY_APPLIED["org.example.alpha"]),
                  RegistryRequest("org.example.bravo", REGISTRY_APPLIED["org.example.bravo"])),
        system_keys=(SystemKeyRequest("db/appfw/startup_appid", fx.OUR_PACKAGE),
                     SystemKeyRequest("db/appfw/home_appid", fx.OUR_PACKAGE)),
        our_packages=(fx.OUR_PACKAGE,),
    )


def tree_state(root: Path):
    state = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            full = Path(dirpath) / name
            rel = full.relative_to(root).as_posix()
            state[rel] = full.read_bytes() if full.is_file() else b"<dir>"
    return state


class CaptureCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.storage = self.tmp / "owner-storage"
        self.storage.mkdir()
        self.out = self.storage / "capsule"

    def tearDown(self):
        self._tmp.cleanup()

    def run_capture(self, backend=None, request=None, out=None, **kw):
        return capture_backup(backend or FakeBackend(), request or full_request(),
                              out if out is not None else self.out, **kw)

    def assertRefused(self, fragment, out=None, **kw):
        target = out if out is not None else self.out
        with self.assertRaises(CapsuleError) as ctx:
            self.run_capture(out=target, **kw)
        self.assertIn(fragment, str(ctx.exception))
        self.assertFalse(Path(target).exists(), "owner storage was created on a refusal")
        return ctx.exception

    def assertNoStagingLeft(self):
        leftovers = [p.name for p in self.storage.iterdir() if ".tvkit-staging-" in p.name]
        self.assertEqual(leftovers, [], f"staging left behind in owner storage: {leftovers}")


class Passing(CaptureCase):
    def test_capture_writes_owner_storage_and_passes_the_gate(self):
        result = self.run_capture()
        self.assertIsInstance(result, CaptureResult)
        self.assertTrue(self.out.is_dir())
        self.assertTrue((self.out / "capsule.json").is_file())
        self.assertEqual(result.baseline, "current-state")
        self.assertEqual(dict(result.gate.device)["deviceIdSha256"], fx.device_id_sha256(fx.DEVICE_ID))
        self.assertNotIn(fx.DEVICE_ID, repr(result.gate.to_json()))
        self.assertEqual(dict(result.gate.counts)["packages"], 3)
        self.assertNoStagingLeft()

    def test_facts_are_read_twice_for_the_identity_recheck(self):
        backend = FakeBackend()
        self.run_capture(backend=backend)
        self.assertEqual(backend.calls.count("facts"), 2)

    def test_gate_hashes_are_the_tv_digests_not_a_pc_rehash(self):
        result = self.run_capture()
        got = {f.name: f.sha256 for f in result.gate.files}
        _, payloads, xattrs = fx.build_parts()
        expected = {}
        for pid in fx.PACKAGE_IDS:
            expected[f"payload/{pid}.tar"] = fx.sha(payloads[pid])
            expected[f"xattrs/{pid}.json"] = fx.sha(xattrs[pid])
        self.assertEqual(got, expected)

    def test_nothing_persists_outside_owner_storage(self):
        before = tree_state(self.tmp)
        self.run_capture()
        after = tree_state(self.tmp)
        new = set(after) - set(before)
        self.assertTrue(new and all(p.startswith("owner-storage/capsule") for p in new), new)

    def test_stock_label_passes_when_not_contradicted(self):
        result = self.run_capture(baseline="stock")
        self.assertEqual((result.baseline, result.gate.baseline_basis), ("stock", "declared"))

    def test_single_package_subset(self):
        req = CaptureRequest(packages=(PackageRequest("org.example.alpha", "/opt/usr/apps/org.example.alpha"),))
        result = self.run_capture(request=req)
        self.assertEqual(dict(result.gate.counts)["packages"], 1)


class Streaming(CaptureCase):
    def test_capture_succeeds_with_tiny_chunks(self):
        # chunk_size=1 forces many small transfer units: the capture still streams correctly.
        result = self.run_capture(backend=FakeBackend(chunk_size=1))
        self.assertIsInstance(result, CaptureResult)

    def test_transfer_iterator_is_closed_on_refusal(self):
        class BadChunk(FakeBackend):
            def capture_package(self, package_id, install_root, *, max_bytes):
                self.calls.append(f"payload:{package_id}")
                self.tracker = ClosableChunks([b"abc", "not-bytes"])
                return CapturedStream(device_sha256="a" * 64, device_size=1000, chunks=self.tracker)

        backend = BadChunk()
        self.assertRefused("is not bytes", backend=backend)
        self.assertTrue(backend.tracker.closed, "the engine did not close the transfer iterator on refusal")
        self.assertLessEqual(backend.tracker.consumed, 2)

    def test_per_chunk_cap_enforced(self):
        with mock.patch.object(capture, "MAX_CHUNK_BYTES", 4):
            self.assertRefused("per-chunk limit")

    def test_oversize_declared_size_refused_before_reading(self):
        backend = FakeBackend(payload_size={"org.example.alpha": capture.MAX_MEMBER_BYTES + 1})
        self.assertRefused("larger than the capsule limit", backend=backend)


class Identity(CaptureCase):
    def test_facts_matching_no_profile_refuse(self):
        for field, value in (("firmware", "1300.9"), ("model", "other"), ("arch", "x86_64")):
            with self.subTest(field=field):
                self.assertRefused("exactly one supported profile", backend=FakeBackend(facts={**fx.FACTS, field: value}))

    def test_incomplete_facts_refuse(self):
        missing = {k: v for k, v in fx.FACTS.items() if k != "deviceId"}
        self.assertRefused("device facts", backend=FakeBackend(facts=missing))

    def test_facts_must_be_a_mapping(self):
        backend = FakeBackend()
        backend.read_device_facts = lambda: [("model", "x")]  # type: ignore[assignment]
        self.assertRefused("must be a mapping", backend=backend)

    def test_identity_change_during_capture_refused(self):
        # First read binds the profile; the second (after capture) reports a different
        # fingerprint, as if the adapter reconnected to another device.
        swapped = {**fx.FACTS, "deviceId": "A-DIFFERENT-DEVICE-9999"}
        backend = FakeBackend(facts_sequence=[dict(fx.FACTS), swapped])
        self.assertRefused("identity or fingerprint changed", backend=backend)
        self.assertTrue(any(c.startswith("payload:") for c in backend.calls),
                        "the identity recheck must run after the capture, not instead of it")

    def test_identity_change_to_invalid_shape_refused(self):
        broken = {k: v for k, v in fx.FACTS.items() if k != "model"}
        backend = FakeBackend(facts_sequence=[dict(fx.FACTS), broken])
        self.assertRefused("changed to an invalid shape", backend=backend)


class ValidateBeforeBackend(CaptureCase):
    """Defect (3): unknown ids/roots/fields/keys are refused before any device read."""

    def _assert_no_device_reads(self, backend):
        # Reading the device facts to bind the profile is allowed; nothing else may run.
        for call in backend.calls:
            self.assertFalse(call.startswith(("payload:", "xattr:", "reg:", "key:")),
                             f"a backend reader ran before validation: {call}")

    def test_unknown_registry_field_causes_no_backend_reads(self):
        backend = FakeBackend()
        req = CaptureRequest(
            packages=full_request().packages,
            registry=(RegistryRequest("org.example.alpha", {"not_a_field": "true"}),),
        )
        self.assertRefused("not in the profile allowlist", backend=backend, request=req)
        self._assert_no_device_reads(backend)

    def test_unknown_system_key_causes_no_backend_reads(self):
        backend = FakeBackend()
        req = CaptureRequest(
            packages=full_request().packages,
            system_keys=(SystemKeyRequest("db/appfw/not_a_key", fx.OUR_PACKAGE),),
        )
        self.assertRefused("not an allowlisted key", backend=backend, request=req)
        self._assert_no_device_reads(backend)

    def test_bad_install_root_causes_no_backend_reads(self):
        backend = FakeBackend()
        req = CaptureRequest(packages=(PackageRequest("org.example.alpha", "/data/org.example.alpha"),
                                       PackageRequest("org.example.bravo", "/opt/usr/apps/org.example.bravo")))
        self.assertRefused("allowlisted root", backend=backend, request=req)
        self._assert_no_device_reads(backend)

    def test_protected_registry_app_refused(self):
        req = CaptureRequest(packages=full_request().packages,
                             registry=(RegistryRequest("com.samsung.settings", {"app_disable": "true"}),))
        self.assertRefused("protected", request=req)

    def test_too_many_packages_refused(self):
        many = tuple(PackageRequest(f"org.example.p{i}", f"/opt/usr/apps/org.example.p{i}")
                     for i in range(capture.MAX_PACKAGES + 1))
        self.assertRefused("at most", request=CaptureRequest(packages=many))


class BackendContract(CaptureCase):
    def test_missing_base_capability_refuses_before_any_read(self):
        backend = FakeBackend(capabilities=BASE_CAPABILITIES - {"capture-package"})
        self.assertRefused("lacks capabilities", backend=backend)
        self.assertEqual(backend.calls, [], "a backend missing a capability was still read")

    def test_registry_capability_required_only_when_registry_requested(self):
        self.assertRefused("lacks capabilities", backend=FakeBackend(capabilities=BASE_CAPABILITIES))
        req = CaptureRequest(packages=(PackageRequest("org.example.alpha", "/opt/usr/apps/org.example.alpha"),))
        self.assertIsInstance(self.run_capture(backend=FakeBackend(capabilities=BASE_CAPABILITIES), request=req),
                              CaptureResult)

    def test_read_only_flag_required(self):
        backend = FakeBackend()
        backend.read_only = False
        self.assertRefused("read-only", backend=backend)

    def test_backend_has_no_write_or_run_method(self):
        for attr in ("run", "write", "apply", "mutate", "remove", "delete", "install"):
            self.assertFalse(hasattr(CaptureBackend, attr), attr)


class RequestShape(CaptureCase):
    def test_empty_request_refused(self):
        self.assertRefused("at least one package", request=CaptureRequest())

    def test_protected_package_refused(self):
        req = CaptureRequest(packages=(PackageRequest("com.samsung.tv.settings",
                                                      "/opt/usr/apps/com.samsung.tv.settings"),))
        self.assertRefused("protected", request=req)

    def test_install_root_must_match_the_package_id(self):
        req = CaptureRequest(packages=(PackageRequest("org.example.alpha", "/opt/usr/apps/org.example.bravo"),))
        self.assertRefused("allowlisted root", request=req)

    def test_duplicate_package_refused(self):
        req = CaptureRequest(packages=(PackageRequest("org.example.alpha", "/opt/usr/apps/org.example.alpha"),
                                       PackageRequest("org.example.alpha", "/opt/usr/apps/org.example.alpha")))
        self.assertRefused("declared twice", request=req)


class BadBlobs(CaptureCase):
    def test_truncated_payload_refused(self):
        backend = FakeBackend(payload_mutators={"org.example.bravo": lambda d: d[:len(d) // 2]})
        self.assertRefused("size the TV reported", backend=backend)

    def test_payload_size_lie_refused(self):
        backend = FakeBackend(payload_size={"org.example.alpha": 999999})
        self.assertRefused("size the TV reported", backend=backend)

    def test_bad_device_digest_refused(self):
        backend = FakeBackend(payload_digest={"org.example.alpha": "XY" + "0" * 62})
        self.assertRefused("64 lowercase hex", backend=backend)

    def test_mutated_transfer_caught_by_the_gate(self):
        def flip(d):
            return d[:-1] + bytes([d[-1] ^ 0x01])

        backend = FakeBackend(payload_mutators={"org.example.charlie": flip})
        self.assertRefused("differ from what the TV hashed", backend=backend)

    def test_backend_returns_wrong_type(self):
        backend = FakeBackend()
        backend.capture_package = lambda pid, root, *, max_bytes: b"not a blob"  # type: ignore[assignment]
        self.assertRefused("typed payload", backend=backend)


class BadMetadata(CaptureCase):
    def test_registry_value_outside_allowlist_refused(self):
        backend = FakeBackend(registry={**REGISTRY_BEFORE,
                                         "org.example.alpha": {"app_disable": "maybe", "app_onboot": "true"}})
        self.assertRefused("allowlist", backend=backend)

    def test_registry_no_op_refused(self):
        backend = FakeBackend(registry={**REGISTRY_BEFORE,
                                        "org.example.alpha": {"app_disable": "true", "app_onboot": "true"}})
        self.assertRefused("records no change", backend=backend)

    def test_xattr_entry_set_mismatch_refused(self):
        def drop_entry(data: bytes) -> bytes:
            import json
            doc = json.loads(data)
            doc["entries"] = [e for e in doc["entries"] if e["path"] != "res/strings.txt"]
            return json.dumps(doc).encode()

        backend = FakeBackend()
        backend.xattrs["org.example.alpha"] = drop_entry(backend.xattrs["org.example.alpha"])
        self.assertRefused("entry set differs", backend=backend)

    def test_system_key_no_op_refused(self):
        backend = FakeBackend(system_keys={**SYSTEM_KEY_ORIGINAL, "db/appfw/home_appid": fx.OUR_PACKAGE})
        self.assertRefused("records no change", backend=backend)


class Traversal(CaptureCase):
    def _escaping_payload(self):
        import io
        import tarfile
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
            root = tarfile.TarInfo("./")
            root.type, root.mode = tarfile.DIRTYPE, 0o755
            archive.addfile(root)
            link = tarfile.TarInfo("escape")
            link.type, link.linkname, link.mode = tarfile.SYMTYPE, "/etc/passwd", 0o777
            archive.addfile(link)
        return buffer.getvalue()

    def test_escaping_symlink_in_payload_refused(self):
        backend = FakeBackend()
        backend.payloads["org.example.alpha"] = self._escaping_payload()
        self.assertRefused("escape", backend=backend)


class Baseline(CaptureCase):
    def test_stock_contradiction_refused(self):
        def customized():
            return FakeBackend(system_keys={**SYSTEM_KEY_ORIGINAL, "db/appfw/home_appid": fx.OUR_PACKAGE})

        req = CaptureRequest(
            packages=full_request().packages,
            system_keys=(SystemKeyRequest("db/appfw/home_appid", "org.example.home"),),
            our_packages=(fx.OUR_PACKAGE,),
        )
        self.assertEqual(self.run_capture(backend=customized(), request=req).baseline, "current-state")
        self.out = self.storage / "stock"
        self.assertRefused("contradicted", backend=customized(), request=req, baseline="stock")

    def test_label_must_be_exact(self):
        self.assertRefused("baseline must be exactly one of", baseline="Stock")


class OwnerStorage(CaptureCase):
    def test_refuses_a_path_inside_the_repository(self):
        inside = capture.REPO_ROOT / "build-should-not-happen"
        with self.assertRaises(CaptureRefused) as ctx:
            self.run_capture(out=inside)
        self.assertIn("outside the repository", str(ctx.exception))
        self.assertFalse(inside.exists())

    def test_refuses_the_repository_root_itself(self):
        with self.assertRaises(CaptureRefused):
            self.run_capture(out=capture.REPO_ROOT)

    def test_refuses_a_missing_parent(self):
        self.assertRefused("parent folder", out=self.tmp / "nope" / "capsule")

    def test_refuses_an_existing_folder(self):
        self.out.mkdir(parents=True)
        with self.assertRaises(CaptureRefused) as ctx:
            self.run_capture()
        self.assertIn("must not exist yet", str(ctx.exception))
        self.assertEqual(list(self.out.iterdir()), [], "a refusal wrote into the existing folder")

    def test_refuses_inside_another_git_repository(self):
        # A synthetic unrelated checkout: a .git directory makes this a repo root.
        repo = self.tmp / "other-repo"
        (repo / ".git").mkdir(parents=True)
        backend = FakeBackend()
        with self.assertRaises(CaptureRefused) as ctx:
            self.run_capture(backend=backend, out=repo / "capsule")
        self.assertIn("git checkout/worktree", str(ctx.exception))
        self.assertFalse((repo / "capsule").exists())
        self.assertEqual(backend.calls, [], "a backend read ran before the repository check")

    def test_refuses_inside_a_git_worktree_marker(self):
        # A linked worktree carries a .git FILE ("gitdir: ...") rather than a dir.
        wt = self.tmp / "worktree"
        wt.mkdir()
        (wt / ".git").write_text("gitdir: /somewhere/.git/worktrees/wt\n", encoding="utf-8")
        with self.assertRaises(CaptureRefused) as ctx:
            self.run_capture(out=wt / "capsule")
        self.assertIn("git checkout/worktree", str(ctx.exception))
        self.assertFalse((wt / "capsule").exists())


class Publication(CaptureCase):
    """Defect (1): the proof is the ACTUAL owner-storage copy, and cleanup is scoped."""

    def test_destination_copy_corruption_is_caught_and_cleaned(self):
        sentinel = self.storage / "KEEP.txt"
        sentinel.write_text("keep me", encoding="utf-8")
        real_publish = capture._publish

        def corrupt_publish(staged, out):
            identity = real_publish(staged, out)
            victim = out / "payload" / "org.example.alpha.tar"
            data = bytearray(victim.read_bytes())
            data[-1] ^= 0x01
            victim.write_bytes(data)
            return identity

        with mock.patch.object(capture, "_publish", corrupt_publish):
            with self.assertRaises(CapsuleError) as ctx:
                self.run_capture()
        # The readback of the ACTUAL owner-storage copy refuses the corrupted byte -
        # whether the capsule validator catches the manifest mismatch first or the
        # TV-digest comparison does; either way the proof is of the published copy.
        message = str(ctx.exception)
        self.assertTrue("do not match" in message or "differ from what the TV hashed" in message, message)
        self.assertIn("org.example.alpha", message)
        self.assertFalse(self.out.exists(), "the corrupt published copy was not cleaned up")
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep me", "a sibling in owner storage was touched")
        self.assertNoStagingLeft()

    def test_publish_preserves_an_existing_destination(self):
        self.out.mkdir()
        (self.out / "preexisting.txt").write_text("mine", encoding="utf-8")
        staged = self.tmp / "staged"
        staged.mkdir()
        (staged / "x").write_text("y", encoding="utf-8")
        with self.assertRaises(CaptureRefused):
            capture._publish(staged, self.out)
        self.assertEqual((self.out / "preexisting.txt").read_text(encoding="utf-8"), "mine")
        self.assertTrue((staged / "x").exists(), "a refused publish must not move the staged copy")

    def test_raced_destination_is_preserved(self):
        real_publish = capture._publish

        def racer(staged, out):
            out.mkdir()
            (out / "racer.txt").write_text("not mine", encoding="utf-8")
            real_publish(staged, out)  # now refuses: it will not overwrite

        with mock.patch.object(capture, "_publish", racer):
            with self.assertRaises(CaptureRefused) as ctx:
                self.run_capture()
        self.assertIn("refusing to overwrite", str(ctx.exception))
        self.assertTrue(self.out.exists(), "a raced destination we did not create was deleted")
        self.assertEqual((self.out / "racer.txt").read_text(encoding="utf-8"), "not mine")
        self.assertNoStagingLeft()

    def test_raced_empty_destination_is_preserved(self):
        # os.rename would silently replace an EMPTY raced directory on POSIX; the
        # os.mkdir reservation refuses it instead and leaves it untouched.
        real_publish = capture._publish

        def racer(staged, out):
            out.mkdir()  # an empty directory races in after the early check
            return real_publish(staged, out)

        with mock.patch.object(capture, "_publish", racer):
            with self.assertRaises(CaptureRefused) as ctx:
                self.run_capture()
        self.assertIn("refusing to overwrite", str(ctx.exception))
        self.assertTrue(self.out.is_dir(), "a raced empty destination was replaced or deleted")
        self.assertEqual(list(self.out.iterdir()), [], "a refused publish wrote into the raced directory")
        self.assertNoStagingLeft()

    def test_cleanup_preserves_a_replaced_output(self):
        # If an unrelated directory replaces our output between publication and the
        # final readback, cleanup must not delete it: it is not the output we made.
        real_gate = capture.readback_gate

        def gate(stored_path, device_hashes, facts, *, baseline="current-state"):
            if Path(stored_path) == self.out:
                shutil.rmtree(self.out)
                self.out.mkdir()  # a different directory (new inode) takes the name
                (self.out / "REPLACEMENT.txt").write_text("unrelated", encoding="utf-8")
                raise ReadbackRefused("synthetic post-publish refusal")
            return real_gate(stored_path, device_hashes, facts, baseline=baseline)

        with mock.patch.object(capture, "readback_gate", gate):
            with self.assertRaises(CapsuleError):
                self.run_capture()
        self.assertTrue(self.out.exists(), "the replacement directory was deleted")
        self.assertEqual((self.out / "REPLACEMENT.txt").read_text(encoding="utf-8"), "unrelated")
        self.assertNoStagingLeft()


class RegressionGuard(CaptureCase):
    """Red/green: the gate is only meaningful because it is given the TV's own digest.

    With a payload mutated in transfer (bytes differ from the TV digest, same
    length), the correct engine refuses. Replace the one line that threads the TV
    digest with a PC re-hash of the staged bytes, and the mutated capture is
    accepted - the attestation has become a no-op. Restored, it refuses again.
    """

    def _mutated_backend(self):
        def flip(d):
            return d[:-1] + bytes([d[-1] ^ 0x01])

        return FakeBackend(payload_mutators={"org.example.alpha": flip})

    def test_red_green_tv_digest_must_not_be_a_pc_rehash(self):
        # GREEN (guard present): the mutated transfer is refused.
        self.assertRefused("differ from what the TV hashed", backend=self._mutated_backend())
        self.assertFalse(self.out.exists())

        # RED (guard off): re-hash the staged bytes instead of using the TV digest.
        self.out = self.storage / "guard-off"
        with mock.patch.object(capture, "_device_digest",
                               lambda captured, path: hashlib.sha256(path.read_bytes()).hexdigest()):
            result = capture_backup(self._mutated_backend(), full_request(), self.out)
        self.assertIsInstance(result, CaptureResult)  # the broken engine accepts the un-attested capture

        # GREEN again (restored): refused once more.
        self.out = self.storage / "restored"
        self.assertRefused("differ from what the TV hashed", backend=self._mutated_backend())


class NoBypassAndNoWritePath(CaptureCase):
    def test_signature_has_no_force_or_skip(self):
        params = inspect.signature(capture_backup).parameters
        self.assertEqual(list(params), ["backend", "request", "out_dir", "baseline"])
        self.assertEqual(params["baseline"].kind, inspect.Parameter.KEYWORD_ONLY)

    def test_result_is_immutable(self):
        result = self.run_capture()
        import dataclasses
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.baseline = "stock"  # type: ignore[misc]
        for name in ("passed", "ok", "force", "skip"):
            self.assertFalse(hasattr(result, name))

    def test_transports_stay_unregistered(self):
        self.assertEqual(transport.TRANSPORTS, {})

    def test_module_has_no_device_network_or_tv_write_surface(self):
        tree = ast.parse(CAPTURE_SOURCE.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(("." * node.level) + (node.module or ""))
        self.assertNotIn("socket", imported)
        self.assertNotIn("subprocess", imported)
        self.assertNotIn(".transport", imported)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, {"system", "popen", "connect", "urlopen"})


if __name__ == "__main__":
    unittest.main()

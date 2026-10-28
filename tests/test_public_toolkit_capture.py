"""D2 typed read-only capture engine. Synthetic fixtures and a FAKE backend only.

The fake backend is a test double. It is never a stand-in for a real SDB or root
transport: it serves the synthetic fixtures from memory and reads nothing. Every
test writes owner storage to a temp folder outside the repository.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import inspect
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_public_toolkit_fixtures as fx  # noqa: E402
from tvkit import capture, transport  # noqa: E402
from tvkit.capsule import CapsuleError  # noqa: E402
from tvkit.capture import (  # noqa: E402
    BASE_CAPABILITIES, CAP_REGISTRY, CAP_SYSTEM_KEY, CaptureBackend, CaptureRefused, CaptureRequest,
    CaptureResult, CapturedFile, PackageRequest, RegistryRequest, SystemKeyRequest, capture_backup,
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


class FakeBackend(CaptureBackend):
    """Serves the synthetic fixtures from memory. Read-only; fully fault-injectable."""

    name = "fake"

    def __init__(self, *, capabilities=ALL_CAPS, facts=None, payload_mutators=None,
                 xattr_mutators=None, payload_size=None, registry=None, system_keys=None):
        _, self.payloads, self.xattrs = fx.build_parts()
        self.capabilities = frozenset(capabilities)
        self._facts = dict(fx.FACTS if facts is None else facts)
        self._payload_mutators = payload_mutators or {}
        self._xattr_mutators = xattr_mutators or {}
        self._payload_size = payload_size or {}
        self._registry = REGISTRY_BEFORE if registry is None else registry
        self._system_keys = SYSTEM_KEY_ORIGINAL if system_keys is None else system_keys
        self.calls: list[str] = []

    def read_device_facts(self):
        self.calls.append("facts")
        return dict(self._facts)

    def _file(self, data: bytes, mutator, size_override):
        """The TV digest is of the real file; the transferred bytes may be mutated to fake drift."""
        device_sha = fx.sha(data)
        sent = mutator(data) if mutator else data
        size = size_override if size_override is not None else len(data)
        return CapturedFile(data=sent, device_sha256=device_sha, device_size=size)

    def capture_package(self, package_id, install_root):
        self.calls.append(f"payload:{package_id}")
        return self._file(self.payloads[package_id], self._payload_mutators.get(package_id),
                          self._payload_size.get(package_id))

    def dump_xattrs(self, package_id, install_root):
        self.calls.append(f"xattr:{package_id}")
        return self._file(self.xattrs[package_id], self._xattr_mutators.get(package_id), None)

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


class Passing(CaptureCase):
    def test_capture_writes_owner_storage_and_passes_the_gate(self):
        result = self.run_capture()
        self.assertIsInstance(result, CaptureResult)
        self.assertTrue(self.out.is_dir())
        self.assertTrue((self.out / "capsule.json").is_file())
        self.assertEqual(result.baseline, "current-state")
        # The gate evidence binds the exact fingerprint, never the raw id.
        self.assertEqual(dict(result.gate.device)["deviceIdSha256"], fx.device_id_sha256(fx.DEVICE_ID))
        self.assertNotIn(fx.DEVICE_ID, repr(result.gate.to_json()))
        self.assertEqual(dict(result.gate.counts)["packages"], 3)

    def test_gate_hashes_are_the_tv_digests_not_a_pc_rehash(self):
        result = self.run_capture()
        got = {f.name: f.sha256 for f in result.gate.files}
        _, payloads, xattrs = fx.build_parts()
        expected = {}
        for pid in fx.PACKAGE_IDS:
            expected[f"payload/{pid}.tar"] = fx.sha(payloads[pid])
            expected[f"xattrs/{pid}.json"] = fx.sha(xattrs[pid])
        self.assertEqual(got, expected)

    def test_nothing_is_written_outside_owner_storage(self):
        before = tree_state(self.tmp)
        self.run_capture()
        after = tree_state(self.tmp)
        new = set(after) - set(before)
        self.assertTrue(new and all(p.startswith("owner-storage/") for p in new), new)

    def test_stock_label_passes_when_not_contradicted(self):
        result = self.run_capture(baseline="stock")
        self.assertEqual(result.baseline, "stock")
        self.assertEqual(result.gate.baseline_basis, "declared")

    def test_single_package_subset(self):
        req = CaptureRequest(packages=(PackageRequest("org.example.alpha", "/opt/usr/apps/org.example.alpha"),))
        result = self.run_capture(request=req)
        self.assertEqual(dict(result.gate.counts)["packages"], 1)


class Identity(CaptureCase):
    def test_facts_matching_no_profile_refuse(self):
        for field, value in (("firmware", "1300.9"), ("model", "other"), ("arch", "x86_64")):
            with self.subTest(field=field):
                backend = FakeBackend(facts={**fx.FACTS, field: value})
                self.assertRefused("exactly one supported profile", backend=backend)

    def test_incomplete_facts_refuse(self):
        missing = {k: v for k, v in fx.FACTS.items() if k != "deviceId"}
        self.assertRefused("device facts", backend=FakeBackend(facts=missing))

    def test_facts_must_be_a_mapping(self):
        backend = FakeBackend()
        backend.read_device_facts = lambda: [("model", "x")]  # type: ignore[assignment]
        self.assertRefused("must be a mapping", backend=backend)


class BackendContract(CaptureCase):
    def test_missing_base_capability_refuses_before_any_read(self):
        backend = FakeBackend(capabilities=BASE_CAPABILITIES - {"capture-package"})
        self.assertRefused("lacks capabilities", backend=backend)
        self.assertEqual(backend.calls, [], "a backend missing a capability was still read")

    def test_registry_capability_required_only_when_registry_requested(self):
        backend = FakeBackend(capabilities=BASE_CAPABILITIES)  # no registry/system-key caps
        self.assertRefused("lacks capabilities", backend=backend)
        # A request without registry/system-key is fine with the base capabilities.
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

    def test_install_root_must_be_allowlisted(self):
        req = CaptureRequest(packages=(PackageRequest("org.example.alpha", "/data/org.example.alpha"),))
        self.assertRefused("allowlisted root", request=req)

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

    def test_oversize_payload_refused(self):
        big = capture.MAX_MEMBER_BYTES + 1

        class Oversize(FakeBackend):
            def capture_package(self, package_id, install_root):
                return CapturedFile(data=b"\0", device_sha256="a" * 64, device_size=big)

        self.assertRefused("larger than the capsule limit", backend=Oversize())

    def test_bad_device_digest_refused(self):
        class BadDigest(FakeBackend):
            def capture_package(self, package_id, install_root):
                real = super().capture_package(package_id, install_root)
                return CapturedFile(data=real.data, device_sha256="XY" + "0" * 62, device_size=real.device_size)

        self.assertRefused("64 lowercase hex", backend=BadDigest())

    def test_mutated_transfer_caught_by_the_gate(self):
        # Bytes differ from the TV digest (same length): the device attestation no longer matches.
        def flip(d):
            return d[:-1] + bytes([d[-1] ^ 0x01])

        backend = FakeBackend(payload_mutators={"org.example.charlie": flip})
        self.assertRefused("differ from what the TV hashed", backend=backend)

    def test_backend_returns_wrong_type(self):
        backend = FakeBackend()
        backend.capture_package = lambda pid, root: b"not a blob"  # type: ignore[assignment]
        self.assertRefused("typed payload", backend=backend)


class BadMetadata(CaptureCase):
    def test_registry_value_outside_allowlist_refused(self):
        backend = FakeBackend(registry={**REGISTRY_BEFORE,
                                         "org.example.alpha": {"app_disable": "maybe", "app_onboot": "true"}})
        self.assertRefused("allowlist", backend=backend)

    def test_registry_no_op_refused(self):
        # before == applied: nothing to record. _parse_registry refuses it honestly.
        backend = FakeBackend(registry={**REGISTRY_BEFORE,
                                        "org.example.alpha": {"app_disable": "true", "app_onboot": "true"}})
        self.assertRefused("records no change", backend=backend)

    def test_xattr_entry_set_mismatch_refused(self):
        # A dump missing an entry the payload lists is missing metadata: validate refuses.
        def drop_entry(data: bytes) -> bytes:
            import json
            doc = json.loads(data)
            doc["entries"] = [e for e in doc["entries"] if e["path"] != "res/strings.txt"]
            return json.dumps(doc).encode()

        backend = FakeBackend(xattr_mutators={"org.example.alpha": drop_entry})
        # The dump's own digest is of the altered bytes, so the gate's byte check passes;
        # capsule.validate refuses because the xattr entry set differs from the payload.
        backend.xattrs["org.example.alpha"] = drop_entry(backend.xattrs["org.example.alpha"])
        backend._xattr_mutators = {}
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
        # Capture faithfully; the capsule validator is what refuses the escaping link.
        self.assertRefused("escape", backend=backend)


class Baseline(CaptureCase):
    def test_stock_contradiction_refused(self):
        # home_appid's captured ORIGINAL is our launcher: our Home was already set at
        # capture, so the gate refuses the "stock" label. Intent differs from original,
        # so the capture is otherwise well-formed.
        def customized():
            return FakeBackend(system_keys={**SYSTEM_KEY_ORIGINAL, "db/appfw/home_appid": fx.OUR_PACKAGE})

        req = CaptureRequest(
            packages=full_request().packages,
            system_keys=(SystemKeyRequest("db/appfw/home_appid", "org.example.home"),),
            our_packages=(fx.OUR_PACKAGE,),
        )
        # current-state is fine...
        self.assertEqual(self.run_capture(backend=customized(), request=req).baseline, "current-state")
        # ...stock is refused by the gate's contradiction check.
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

    def test_refuses_an_existing_folder(self):
        self.out.mkdir(parents=True)
        with self.assertRaises(CaptureRefused) as ctx:
            self.run_capture()
        self.assertIn("must not exist yet", str(ctx.exception))
        self.assertEqual(list(self.out.iterdir()), [], "a refusal wrote into the existing folder")


class RegressionGuard(CaptureCase):
    """Red/green: the gate is only meaningful because it is given the TV's own digest.

    With a payload mutated in transfer (bytes differ from the TV digest, same
    length), the correct engine refuses. Replace the one line that threads the
    TV digest with a PC re-hash of the transferred bytes, and the mutated capture
    is accepted - the attestation has become a no-op. Restored, it refuses again.
    """

    def _mutated_backend(self):
        def flip(d):
            return d[:-1] + bytes([d[-1] ^ 0x01])

        return FakeBackend(payload_mutators={"org.example.alpha": flip})

    def test_red_green_tv_digest_must_not_be_a_pc_rehash(self):
        # GREEN (guard present): the mutated transfer is refused.
        self.assertRefused("differ from what the TV hashed", backend=self._mutated_backend())
        self.assertFalse(self.out.exists())

        # RED (guard off): re-hash the transferred bytes instead of using the TV digest.
        self.out = self.storage / "guard-off"
        with mock.patch.object(capture, "_device_digest",
                               lambda c: hashlib.sha256(c.data).hexdigest()):
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
                self.assertNotIn(node.attr, {"system", "popen", "connect", "urlopen", "run"})


if __name__ == "__main__":
    unittest.main()

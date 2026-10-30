"""Orchestrate ONE live report collection over an injected, trusted ``LiveOps`` adapter.

This module is the public PC-side coordinator. It:

1. validates every owner input (absolute plain local files outside every git
   checkout, bounded reads, mandatory SHA-256 pins) and takes a byte-pinned
   immutable snapshot of each artifact;
2. validates the signed diagnostic widget's ZIP structure, its exact probe
   sources and its ``package``/``application`` ids BEFORE any TV operation;
3. asserts the run's namespace is absent, installs and launches the widget,
   reads the fresh ProductInfo report, pushes and runs the native inspector,
   all inside ONE adapter session bound to one target;
4. joins the fresh observations to the owner's trusted policy and profile
   through the EXISTING :mod:`tvkit.factsjoin` code, and projects the EXISTING
   display-only preflight;
5. writes owner-private, create-new artifacts plus a compatible
   ``quiettv-helper-preflight/1`` config; and
6. always reconciles/removes only this run's proven-owned namespace, and never
   reports success if cleanup could not be confirmed.

Freshness is lifecycle evidence, not byte novelty
-------------------------------------------------

Two runs of the same TV may legitimately return IDENTICAL report and frame
bytes; this module never compares bytes or timestamps to older runs. Freshness
comes from ordering: ``assert_absent`` is called immediately before this run's
install/launch, the read happens in the same session after this run's launch,
and every result must echo the same run id and the same opaque session
token/binding. A stale artifact with a false absence is a collision and is
refused without touching it. There is no pre-clean and no nonce.

The session token is CORRELATION, not cryptographic proof. The adapter is
explicitly trusted code; it guarantees one target binding and that the fresh
operations really ran. Nothing here claims a tamper-proof session attestation.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import os
import re
import secrets
import stat
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

from . import factsjoin as fj
from . import liveops
from . import profile as tvprofile
from . import strictjson as sj
from .paths import is_sha256

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HELPER_DIR = _REPO_ROOT / "helper"

# ---- owner-artifact contract ----

COLLECTION_SCHEMA = "quiettv-collection/1"
SUMMARY_SCHEMA = "quiettv-collection-summary/1"
PREFLIGHT_CONFIG_SCHEMA = "quiettv-helper-preflight/1"

REPORT_NAME = "device-facts.json"
FRAME_NAME = "inventory.frame"
RECEIPT_NAME = "join-receipt.json"
PREFLIGHT_NAME = "helper-preflight-config.json"
COLLECTION_NAME = "collection.json"
ARTIFACT_NAMES = (REPORT_NAME, FRAME_NAME, RECEIPT_NAME, PREFLIGHT_NAME)

DLL_NAME = "QuietTVInspect.dll"
RUNTIMECONFIG_NAME = "QuietTVInspect.runtimeconfig.json"
DEPS_NAME = "QuietTVInspect.deps.json"

# ---- bounded reads ----

MAX_WGT_BYTES = 8 * 1024 * 1024
MAX_POLICY_BYTES = 256 * 1024
MAX_PROFILE_BYTES = 256 * 1024
MAX_NATIVE_DLL_BYTES = 16 * 1024 * 1024
MAX_NATIVE_RUNTIMECONFIG_BYTES = 256 * 1024
MAX_NATIVE_DEPS_BYTES = 1024 * 1024
MAX_CONFIG_XML_BYTES = 64 * 1024
MAX_PROBE_SOURCE_BYTES = 512 * 1024

MAX_WGT_ENTRIES = 8
MAX_WGT_ENTRY_BYTES = 512 * 1024
MAX_WGT_TOTAL_BYTES = 2 * 1024 * 1024

# The deterministic probe widget is exactly these three files; a signed Tizen
# package additionally carries its signature XMLs. Nothing else is accepted.
_WGT_REQUIRED = frozenset({"config.xml", "index.html", "collector.js",
                           "author-signature.xml", "signature1.xml"})
_WGT_ALLOWED = _WGT_REQUIRED | {"signature2.xml"}
_PROBE_SOURCES = ("index.html", "collector.js")

_PACKAGE_RE = re.compile(r"[A-Za-z0-9]{10}\Z")
_PLACEHOLDERS = frozenset({"changeme", "change-me", "placeholder", "example", "todo"})
_RUN_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
_WIDGET_NS = "http://www.w3.org/ns/widgets"
_TIZEN_NS = "http://tizen.org/ns/widgets"
_ZIP_WIDGET_TAG = "{" + _WIDGET_NS + "}widget"
_ZIP_APP_TAG = "{" + _TIZEN_NS + "}application"
_REPARSE_POINT = 0x400


class LiveCollectionError(ValueError):
    """A finite refusal. It never echoes a path, value, token, DUID or adapter text."""


def _fail(message: str) -> None:
    raise LiveCollectionError(message)


# ---- load helper/owner_init.py by path (no sys.path pollution, one instance) ----

_owner_init_cache = None


def _owner_init():
    global _owner_init_cache
    if _owner_init_cache is None:
        spec = importlib.util.spec_from_file_location(
            "quiettv_livecollect_owner_init", _HELPER_DIR / "owner_init.py")
        if spec is None or spec.loader is None:
            _fail("the owner-storage privacy module could not be located")
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception:
            _fail("the owner-storage privacy module could not be loaded")
        _owner_init_cache = module
    return _owner_init_cache


# ---- local input path rules (absolute, plain, outside every checkout) ----

def _is_link_or_reparse(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)


def _ancestors_are_plain(path: Path, label: str) -> None:
    for component in (path, *path.parents):
        try:
            info = os.lstat(component)
        except FileNotFoundError:
            continue
        except (ValueError, OSError):
            _fail(f"{label} could not be inspected")
        if _is_link_or_reparse(info):
            _fail(f"{label} crosses a symlink, junction or reparse point")


def _validate_local_path(value: str, label: str) -> Path:
    if type(value) is not str or not value or "\x00" in value:
        _fail(f"{label} must be a real local path")
    if "://" in value or value.startswith(("\\\\", "//")):
        _fail(f"{label} must be a local path, not a URL or network share")
    drive, tail = os.path.splitdrive(value)
    if not os.path.isabs(value) or (drive and not tail.startswith(("\\", "/"))):
        _fail(f"{label} must be an absolute path (not relative or drive-relative)")
    if ".." in Path(value).parts:
        _fail(f"{label} must not contain '..'")
    raw = os.path.abspath(value)
    try:
        if os.path.realpath(raw).startswith(("\\\\", "//")):
            _fail(f"{label} resolves to a network share")
    except (ValueError, OSError):
        _fail(f"{label} could not be resolved")
    path = Path(raw)
    _ancestors_are_plain(path, label)
    return path


def _outside(path: str | os.PathLike, *, must_not_exist: bool) -> Path:
    oi = _owner_init()
    try:
        return oi.check_outside_repos(path, must_not_exist=must_not_exist)
    except oi.OwnerInitError as exc:
        _fail(str(exc))
    except (ValueError, OSError):
        _fail("owner storage could not be validated")


def _read_plain_file(path: Path, max_bytes: int, label: str) -> bytes:
    """Bounded single read of a plain regular file, with a swap guard around open."""
    try:
        base = os.lstat(path)
    except FileNotFoundError:
        _fail(f"{label} does not exist")
    except (ValueError, OSError):
        _fail(f"{label} could not be read")
    if _is_link_or_reparse(base) or not stat.S_ISREG(base.st_mode):
        _fail(f"{label} is not a plain file")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except (ValueError, OSError):
        _fail(f"{label} could not be read")
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            _fail(f"{label} is not a plain file")
        if (opened.st_dev, opened.st_ino) != (base.st_dev, base.st_ino):
            _fail(f"{label} changed while it was being opened")
        if opened.st_size > max_bytes:
            _fail(f"{label} is larger than the accepted bound")
        chunks: list[bytes] = []
        total = 0
        while total <= max_bytes:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                _fail(f"{label} is larger than the accepted bound")
            chunks.append(chunk)
    except OSError:
        _fail(f"{label} could not be read")
    finally:
        os.close(fd)
    return b"".join(chunks)


def _read_owner_file(value: str, label: str, max_bytes: int, *, allowed_name: str | None = None,
                     suffix: str | None = None) -> tuple[Path, bytes]:
    path = _validate_local_path(value, label)
    if allowed_name is not None and path.name != allowed_name:
        _fail(f"{label} must be named {allowed_name}")
    if suffix is not None and path.suffix.lower() != suffix:
        _fail(f"{label} must be a {suffix} file")
    _outside(path, must_not_exist=False)
    return path, _read_plain_file(path, max_bytes, label)


# ---- mandatory SHA-256 pin (a module-level guard so tests can both-way it) ----

def _require_pin(data: bytes, expected: str, label: str) -> str:
    if not is_sha256(expected):
        _fail(f"{label} needs a trusted SHA-256 pin")
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        _fail(f"{label} does not match its trusted SHA-256 pin; refusing")
    return actual


# ---- signed widget validation (all of this happens before any TV operation) ----

def _safe_zip_name(name: str) -> str:
    if type(name) is not str or not name or len(name) > 200:
        _fail("the signed widget has an invalid member name")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in name):
        _fail("the signed widget has a member name with a control character")
    if "\\" in name or name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        _fail("the signed widget has an absolute or backslash member name")
    if any(part in ("", ".", "..") for part in name.split("/")):
        _fail("the signed widget has an empty, '.' or '..' member path")
    return name


def _zip_read(zf: zipfile.ZipFile, info: zipfile.ZipInfo, cap: int, label: str) -> bytes:
    try:
        with zf.open(info) as handle:
            data = handle.read(cap + 1)
    except (zipfile.BadZipFile, OSError, RuntimeError):
        _fail(f"{label} could not be read")
    if len(data) > cap:
        _fail(f"{label} is larger than the accepted bound")
    if len(data) != info.file_size:
        _fail(f"{label} does not match its declared size")
    return data


def _probe_source(name: str) -> bytes:
    # The reviewed, current probe sources in THIS checkout are the trusted pins
    # for the two code files inside the signed widget. They are read locally.
    path = _REPO_ROOT / "tools" / "tv-device-facts" / name
    return _read_plain_file(path, MAX_PROBE_SOURCE_BYTES, f"reviewed probe source {name}").replace(b"\r\n", b"\n")


def _validate_config_xml(raw: bytes, package_id: str) -> None:
    if len(raw) > MAX_CONFIG_XML_BYTES:
        _fail("the signed widget's config.xml is larger than the accepted bound")
    try:
        text = raw.decode("utf-8", "strict")
    except UnicodeError:
        _fail("the signed widget's config.xml is not valid UTF-8")
    lowered = text.lower()
    if "<!doctype" in lowered or "<!entity" in lowered:
        _fail("the signed widget's config.xml carries a document type or entity declaration")
    try:
        root = ET.fromstring(text)
    except (ET.ParseError, ValueError, RecursionError):
        _fail("the signed widget's config.xml is not well-formed XML")
    if root.tag != _ZIP_WIDGET_TAG:
        _fail("the signed widget's config.xml is not a Tizen widget document")
    apps = root.findall(_ZIP_APP_TAG)
    if len(apps) != 1:
        _fail("the signed widget's config.xml must declare exactly one tizen:application")
    app = apps[0]
    if app.get("package") != package_id:
        _fail("the signed widget's package id does not match --package-id; refusing")
    if app.get("id") != package_id + ".Facts":
        _fail("the signed widget's application id must be <package-id>.Facts; refusing")


def _validate_signed_wgt(data: bytes, package_id: str) -> None:
    """Bounded ZIP structure, exact probe sources, exact package/app ids."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError, ValueError):
        _fail("the signed widget is not a readable ZIP archive")
    with zf:
        infos = zf.infolist()
        if not infos or len(infos) > MAX_WGT_ENTRIES:
            _fail("the signed widget has an unexpected number of entries")
        names: set[str] = set()
        folded: set[str] = set()
        total = 0
        for info in infos:
            name = _safe_zip_name(info.filename)
            if info.is_dir() or name.endswith("/"):
                _fail("the signed widget carries a directory entry")
            if name in names or name.casefold() in folded:
                _fail("the signed widget has a duplicate member name")
            names.add(name)
            folded.add(name.casefold())
            if info.flag_bits & 0x1:
                _fail("the signed widget has an encrypted member")
            if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                _fail("the signed widget uses an unsupported compression method")
            mode = (info.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                _fail("the signed widget carries a symbolic link")
            if mode and not stat.S_ISREG(mode):
                _fail("the signed widget carries a non-regular member")
            if info.file_size > MAX_WGT_ENTRY_BYTES or info.compress_size > MAX_WGT_BYTES:
                _fail("the signed widget member is larger than the accepted bound")
            if info.file_size > 64 * 1024 and info.compress_size and info.file_size > 64 * info.compress_size:
                _fail("the signed widget member has an unsafe compression ratio")
            total += info.file_size
            if total > MAX_WGT_TOTAL_BYTES:
                _fail("the signed widget expands beyond the accepted bound")
        if not _WGT_REQUIRED <= names or names - _WGT_ALLOWED:
            _fail("the signed widget must carry exactly the probe files and their signature XMLs")

        by_name = {info.filename: info for info in infos}
        config = _zip_read(zf, by_name["config.xml"], MAX_CONFIG_XML_BYTES, "the signed widget's config.xml")
        _validate_config_xml(config, package_id)
        for name in _PROBE_SOURCES:
            packed = _zip_read(zf, by_name[name], MAX_WGT_ENTRY_BYTES, f"the signed widget's {name}")
            if packed != _probe_source(name):
                _fail(f"the signed widget's {name} is not the reviewed probe source; refusing")


# ---- native artifact bundle ----

def _validate_runtimeconfig(data: bytes) -> None:
    try:
        top = sj.loads(data)
    except (sj.StrictJsonError, ValueError):
        _fail("the inspector runtimeconfig is not strict JSON")
    if not isinstance(top, dict) or set(top) != {"runtimeOptions"}:
        _fail("the inspector runtimeconfig is not a framework runtimeconfig")
    options = top["runtimeOptions"]
    if not isinstance(options, dict):
        _fail("the inspector runtimeconfig runtimeOptions is malformed")
    if options.get("tfm") != "net6.0":
        _fail("the inspector runtimeconfig does not target net6.0")
    framework = options.get("framework")
    if not isinstance(framework, dict) or framework.get("name") != "Microsoft.NETCore.App":
        _fail("the inspector runtimeconfig does not target Microsoft.NETCore.App")
    version = framework.get("version")
    if type(version) is not str or not re.fullmatch(r"6\.0\.[0-9]{1,3}", version):
        _fail("the inspector runtimeconfig target version is not a net6.0 release")
    if "includedFrameworks" in options:
        _fail("the inspector runtimeconfig must be framework-dependent, not self-contained")


def _validate_deps(data: bytes) -> None:
    try:
        top = sj.loads(data)
    except (sj.StrictJsonError, ValueError):
        _fail("the inspector deps file is not strict JSON")
    if not isinstance(top, dict) or "runtimeTarget" not in top or not isinstance(top.get("targets"), dict):
        _fail("the inspector deps file is not a .NET deps document")


def _read_native_bundle(dll: str, dll_sha: str, runtimeconfig: str, runtimeconfig_sha: str,
                        deps: str, deps_sha: str) -> tuple[tuple[str, bytes, str], ...]:
    """Read, validate and pin the three exact build artifacts before any TV operation."""
    _, dll_data = _read_owner_file(dll, "--inspector-dll", MAX_NATIVE_DLL_BYTES, allowed_name=DLL_NAME)
    _require_pin(dll_data, dll_sha, "the inspector DLL")
    _, rc_data = _read_owner_file(runtimeconfig, "--inspector-runtimeconfig",
                                  MAX_NATIVE_RUNTIMECONFIG_BYTES, allowed_name=RUNTIMECONFIG_NAME)
    _require_pin(rc_data, runtimeconfig_sha, "the inspector runtimeconfig")
    _validate_runtimeconfig(rc_data)
    _, deps_data = _read_owner_file(deps, "--inspector-deps", MAX_NATIVE_DEPS_BYTES, allowed_name=DEPS_NAME)
    _require_pin(deps_data, deps_sha, "the inspector deps file")
    _validate_deps(deps_data)
    return ((DLL_NAME, dll_data, dll_sha),
            (RUNTIMECONFIG_NAME, rc_data, runtimeconfig_sha),
            (DEPS_NAME, deps_data, deps_sha))


# ---- trusted policy / profile ----

def _load_policy(value: str) -> tuple[Path, fj.JoinPolicy]:
    path, data = _read_owner_file(value, "--policy", MAX_POLICY_BYTES)
    try:
        return path, fj.join_policy_from_json(data)
    except (fj.JoinError, RecursionError):
        _fail("the trusted join policy could not be validated")


def _load_profile(value: str) -> tuple[Path, tvprofile.Profile]:
    path, data = _read_owner_file(value, "--profile", MAX_PROFILE_BYTES)
    try:
        return path, tvprofile.parse_profile(sj.loads(data))
    except (sj.StrictJsonError, tvprofile.ProfileError, RecursionError):
        _fail("the trusted profile could not be validated")


# ---- adapter result checks (small, isolated guards) ----

def _require_result(result: Any, label: str, run_id: str) -> None:
    if not isinstance(result, liveops.OpResult):
        _fail(f"{label} did not return a typed result; refusing")
    if result.ok is not True:
        _fail(f"{label} did not report success; refusing")
    if result.run_id != run_id:
        _fail(f"{label} did not echo this run's request tag; refusing")


def _require_same_session(session: liveops.LiveSession, run_id: str, results: list) -> None:
    """Every result must name this run and this session's opaque token/binding."""
    try:
        token = session.session_token
        binding = session.binding
    except Exception:
        _fail("the live-operations session could not expose its binding; refusing")
    if type(token) is not str or not token:
        _fail("the live-operations session token is malformed; refusing")
    for result in results:
        if result.session_token != token or result.binding is not binding:
            _fail("the adapter mixed sessions or targets; refusing a cross-session pair")


def _require_absence(result: Any) -> None:
    if not isinstance(result, liveops.AbsenceResult):
        _fail("namespace absence did not return a typed result; refusing")
    if not (result.package_absent is True and result.data_absent is True):
        _fail("a diagnostic package or a previous report already exists; refusing without touching it")


def _require_install(result: Any) -> None:
    if not isinstance(result, liveops.InstallResult):
        _fail("widget install did not return a typed result; refusing")
    if not (result.pushed is True and result.install_attempted is True and result.installed is True):
        _fail("the diagnostic widget install did not complete; refusing")


def _require_launch(result: Any) -> None:
    if not isinstance(result, liveops.LaunchResult):
        _fail("widget launch did not return a typed result; refusing")
    if result.launched is not True:
        _fail("the diagnostic widget did not launch; refusing")


def _require_cleanup(cleanup: Any, owned: liveops.OwnedRun) -> None:
    if not isinstance(cleanup, liveops.CleanupReport):
        _fail("cleanup did not return a typed reconciliation; recovery is needed")
    expected = {upload.remote_path for upload in owned.uploads}
    removed = set(cleanup.removed_uploads)
    if cleanup.recovery_needed:
        _fail("cleanup could not confirm this run's namespace; recovery is needed")
    if cleanup.unconfirmed_uploads or expected - removed:
        _fail("cleanup left one of this run's uploads unconfirmed; recovery is needed")
    if removed - expected:
        _fail("cleanup removed a path this run did not own; recovery is needed")
    if not owned.absence_confirmed and cleanup.package_removed:
        _fail("cleanup removed a package this run did not prove it owned; recovery is needed")
    if owned.absence_confirmed and owned.install_attempted:
        if not (cleanup.package_removed is True and cleanup.package_absent_after is True):
            _fail("cleanup did not confirm the diagnostic package is gone; recovery is needed")


def _adapter_call(fn, *args):
    """Call one adapter operation, converting its error into a finite refusal."""
    try:
        return fn(*args)
    except LiveCollectionError:
        raise
    except liveops.LiveOpsError:
        raise LiveCollectionError("the live-operations adapter refused or failed; no success is claimed") from None
    except (OSError, ValueError, TypeError):
        raise LiveCollectionError("a live operation failed; no success is claimed") from None


# ---- owner-private outputs ----

def _create_out_dir(out_dir: str) -> Path:
    target = _outside(out_dir, must_not_exist=True)
    oi = _owner_init()
    try:
        oi.make_private_dir(target)
    except oi.OwnerInitError as exc:
        _fail(str(exc))
    except OSError:
        _fail("the output directory could not be created")
    _outside(target, must_not_exist=False)
    return target


def _write_new(path: Path, data: bytes) -> None:
    oi = _owner_init()
    try:
        oi._write_new(path, data)
        oi.make_private_file(path)
    except FileExistsError:
        _fail("an output file already exists; refusing to overwrite")
    except oi.OwnerInitError as exc:
        _fail(str(exc))
    except OSError:
        _fail("an output file could not be written")


def _write_outputs(out: Path, report: bytes, frame: bytes, receipt: fj.JoinReceipt,
                   policy_path: Path, profile_path: Path) -> dict[str, bytes]:
    oi = _owner_init()
    receipt_bytes = oi.json_bytes(receipt.body())
    config = {
        "schema": PREFLIGHT_CONFIG_SCHEMA,
        "productInfoReport": str(out / REPORT_NAME),
        "nativeInspectionFrame": str(out / FRAME_NAME),
        "joinPolicy": str(policy_path),
        "profile": str(profile_path),
        "operatorAssertions": {"trustPolicyAndProfile": True, "pairedFromSameTv": True},
    }
    written = {
        REPORT_NAME: report,
        FRAME_NAME: frame,
        RECEIPT_NAME: receipt_bytes,
        PREFLIGHT_NAME: oi.json_bytes(config),
    }
    for name, data in written.items():
        _write_new(out / name, data)
    return written


def _collection_doc(run_id: str, meta: dict | None, cleanup_status: str) -> dict:
    doc = {
        "schema": COLLECTION_SCHEMA,
        "collectionId": run_id,
        "cleanup": {"status": cleanup_status},
        "note": ("Live collection over an owner-injected trusted adapter. This record "
                 "claims two fresh same-session artifacts and a join only; it makes no "
                 "boot, root or launcher claim and enables no action."),
    }
    if meta:
        doc["artifacts"] = dict(meta.get("artifactHashes", {}))
        doc["receiptDigest"] = meta.get("receiptDigest")
        doc["profileId"] = meta.get("profileId")
        doc["policyDigest"] = meta.get("policyDigest")
    return doc


def _finish(session: liveops.LiveSession | None, owned: liveops.OwnedRun, out_dir: str,
            run_id: str, meta: dict | None, body_complete: bool,
            out_state: dict) -> LiveCollectionError | None:
    """Reconcile/close the session, then write collection.json. Returns an error, never raises."""
    error: LiveCollectionError | None = None
    cleanup = None
    if session is not None:
        try:
            cleanup = session.close_owned_run(owned)
        except Exception:
            error = LiveCollectionError("cleanup failed; this run's namespace needs recovery")
        try:
            session.close()
        except Exception:
            if error is None:
                error = LiveCollectionError("the live-operations session did not close cleanly")
    if cleanup is not None:
        try:
            _require_cleanup(cleanup, owned)
        except LiveCollectionError as exc:
            error = exc
    if error is None:
        if body_complete:
            try:
                out = _state_out_dir(out_state, out_dir)
                _write_new(out / COLLECTION_NAME, _owner_init().json_bytes(
                    _collection_doc(run_id, meta, "complete")))
            except LiveCollectionError as exc:
                error = exc
        return error
    # Cleanup failed or was unconfirmed: retain a private, explicit recovery status.
    try:
        out = _state_out_dir(out_state, out_dir)
        _write_new(out / COLLECTION_NAME, _owner_init().json_bytes(
            _collection_doc(run_id, meta, "recovery-needed")))
    except LiveCollectionError:
        pass
    return error


def _state_out_dir(state: dict, out_dir: str) -> Path:
    if state.get("path") is None:
        state["path"] = _create_out_dir(out_dir)
    return state["path"]


# ---- the one orchestration entry point ----

def collect_reports(*, ops: liveops.LiveOps, out_dir: str, package_id: str,
                    signed_wgt: str, wgt_sha256: str,
                    inspector_dll: str, dll_sha256: str,
                    inspector_runtimeconfig: str, runtimeconfig_sha256: str,
                    inspector_deps: str, deps_sha256: str,
                    policy: str, profile: str,
                    operator_trust_policy_and_profile: bool,
                    operator_paired_from_same_tv: bool,
                    run_id: str | None = None) -> dict:
    """Collect one fresh report+frame over ``ops`` and write owner-private artifacts.

    ``ops`` must be an explicit :class:`tvkit.liveops.LiveOps` instance supplied
    by the parent (or resolved from the empty public registry by the CLI). All
    device work happens inside ONE ``ops.session(run_id)``.
    """
    if operator_trust_policy_and_profile is not True or operator_paired_from_same_tv is not True:
        _fail("both operator trust assertions must be given explicitly; refusing to manufacture consent")
    if not isinstance(ops, liveops.LiveOps) or ops.implemented is not True:
        _fail("no implemented live-operations adapter was supplied; refusing")
    oi = _owner_init()
    try:
        pkg = oi.validate_package_id(package_id)
    except oi.OwnerInitError as exc:
        _fail(str(exc))
    if not _PACKAGE_RE.fullmatch(pkg) or pkg.lower() in _PLACEHOLDERS or len(set(pkg)) == 1:
        _fail("package id must be a real 10-character alphanumeric Tizen package id")

    _outside(str(out_dir), must_not_exist=True)  # refuse a bad destination before any work

    wgt_path, wgt_bytes = _read_owner_file(signed_wgt, "--signed-wgt", MAX_WGT_BYTES, suffix=".wgt")
    wgt_sha = _require_pin(wgt_bytes, wgt_sha256, "the signed widget")
    _validate_signed_wgt(wgt_bytes, pkg)

    policy_path, policy_obj = _load_policy(policy)
    profile_path, profile_obj = _load_profile(profile)
    if profile_obj.schema != tvprofile.PROFILE_SCHEMA_V2:
        _fail("the facts join needs a tvkit-profile/2 profile; refusing")
    if policy_obj.profile_id != profile_obj.profile_id:
        _fail("the trusted policy and profile do not belong together; refusing")

    native_bundle = _read_native_bundle(inspector_dll, dll_sha256, inspector_runtimeconfig,
                                        runtimeconfig_sha256, inspector_deps, deps_sha256)

    if run_id is None:
        run_id = secrets.token_hex(16)
    if type(run_id) is not str or not _RUN_ID_RE.fullmatch(run_id):
        _fail("the run id is malformed")
    try:
        run_dir = liveops.remote_run_dir(run_id)
        remote_wgt = liveops.remote_wgt_path(run_dir, pkg)
        remote_dll = liveops.remote_native_dll(run_dir)
        paths = liveops.data_paths(pkg)
    except liveops.LiveOpsError:
        raise LiveCollectionError("the tool-computed remote paths could not be built") from None

    native_uploads = tuple(liveops.Upload(f"{run_dir}/{name}", sha, data)
                           for name, data, sha in native_bundle)
    wgt_upload = liveops.Upload(remote_wgt, wgt_sha, wgt_bytes)

    ownership = liveops.OwnedRun(run_id=run_id, package_id=pkg, absence_confirmed=False,
                                 install_attempted=False, uploads=())
    out_state: dict = {"path": None}
    session = None
    meta: dict | None = None
    primary: BaseException | None = None
    try:
        try:
            session = ops.session(run_id)
        except LiveCollectionError:
            raise
        except liveops.LiveOpsError:
            raise LiveCollectionError("the live-operations adapter could not open a session") from None
        except (OSError, ValueError, TypeError):
            raise LiveCollectionError("the live-operations adapter could not open a session") from None
        if not isinstance(session, liveops.LiveSession):
            _fail("the live-operations adapter returned an untyped session; refusing")

        results: list = []
        absence = _adapter_call(session.assert_absent, pkg, paths)
        _require_result(absence, "namespace absence", run_id)
        _require_absence(absence)
        results.append(absence)
        ownership = replace(ownership, absence_confirmed=True)

        ownership = replace(ownership, install_attempted=True, uploads=(wgt_upload,))
        install = _adapter_call(session.install_widget, pkg, wgt_bytes, wgt_sha, remote_wgt)
        _require_result(install, "widget install", run_id)
        _require_install(install)
        results.append(install)

        launch = _adapter_call(session.launch_facts, pkg)
        _require_result(launch, "widget launch", run_id)
        _require_launch(launch)
        results.append(launch)

        report = _adapter_call(session.read_report, pkg, paths)
        _require_result(report, "report read", run_id)
        if not isinstance(report, liveops.ReportResult):
            _fail("the report read did not return a typed result; refusing")
        if type(report.data) is not bytes:
            _fail("the report read did not return raw bytes; refusing")
        if len(report.data) > liveops.MAX_REPORT_BYTES:
            _fail("the ProductInfo report is larger than the accepted bound; refusing")
        try:
            # Validate the report BEFORE any further TV write (the native push). The
            # trusted-policy comparison still happens in the full join below.
            fj.parse_product_info(report.data)
        except (fj.JoinError, RecursionError):
            _fail("the ProductInfo report is incomplete, malformed or carries a raw identity; refusing")
        results.append(report)

        ownership = replace(ownership, uploads=(wgt_upload, *native_uploads))
        push = _adapter_call(session.push_native, native_uploads, run_dir)
        _require_result(push, "native push", run_id)
        results.append(push)

        native = _adapter_call(session.run_native, remote_dll)
        _require_result(native, "native run", run_id)
        if not isinstance(native, liveops.NativeResult):
            _fail("the native run did not return a typed result; refusing")
        if type(native.stdout) is not bytes:
            _fail("the native run did not return raw bytes; refusing")
        if native.exit_code != 0:
            _fail("the native inventory run did not exit successfully; refusing")
        if len(native.stdout) > liveops.MAX_NATIVE_FRAME_BYTES:
            _fail("the native inventory output is larger than the accepted bound; refusing")
        results.append(native)

        _require_same_session(session, run_id, results)

        try:
            receipt = fj.join(report.data, native.stdout, policy=policy_obj, profile=profile_obj)
            shown = fj.preflight_display(receipt, report.data, native.stdout,
                                         policy=policy_obj, profile=profile_obj)
        except (fj.JoinError, tvprofile.ProfileError, sj.StrictJsonError):
            raise LiveCollectionError(
                "the fresh observations did not match the trusted policy and profile; refusing") from None
        except LiveCollectionError:
            raise
        except Exception:
            raise LiveCollectionError(
                "the fresh observations could not be joined; refusing") from None
        if not isinstance(shown, dict) or shown.get("actionsEnabled") != {} or shown.get("capabilities") != {}:
            _fail("the preflight projection enabled a capability; refusing")
        if len(shown.get("fields", {})) != len(tvprofile.MATCH_FIELDS_V2):
            _fail("the preflight projection did not carry the full fact set; refusing")

        out = _state_out_dir(out_state, str(out_dir))
        written = _write_outputs(out, report.data, native.stdout, receipt, policy_path, profile_path)
        meta = {
            "artifactHashes": {
                "productInfoSha256": hashlib.sha256(report.data).hexdigest(),
                "inspectionSha256": hashlib.sha256(native.stdout).hexdigest(),
                "receiptSha256": hashlib.sha256(written[RECEIPT_NAME]).hexdigest(),
                "signedWgtSha256": wgt_sha,
                "inspectorDllSha256": hashlib.sha256(native_bundle[0][1]).hexdigest(),
                "inspectorRuntimeconfigSha256": hashlib.sha256(native_bundle[1][1]).hexdigest(),
                "inspectorDepsSha256": hashlib.sha256(native_bundle[2][1]).hexdigest(),
            },
            "receiptDigest": receipt.receipt_digest,
            "profileId": receipt.profile_id,
            "policyDigest": receipt.policy_digest,
        }
        summary = {
            "schema": SUMMARY_SCHEMA,
            "ok": True,
            "verb": "collect",
            "status": "collected",
            "collectionId": run_id,
            "productInfoSha256": meta["artifactHashes"]["productInfoSha256"],
            "inspectionSha256": meta["artifactHashes"]["inspectionSha256"],
            "receiptDigest": receipt.receipt_digest,
            "preflightFields": len(shown["fields"]),
            "artifacts": [*ARTIFACT_NAMES, COLLECTION_NAME],
            "cleanup": "complete",
        }
    except BaseException as exc:  # noqa: BLE001 - re-raised below after cleanup
        primary = exc
    finally:
        cleanup_error = _finish(session, ownership, str(out_dir), run_id, meta, primary is None, out_state)

    if primary is not None:
        raise primary
    if cleanup_error is not None:
        raise cleanup_error
    return summary

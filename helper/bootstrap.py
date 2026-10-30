"""QuietTV bootstrap (D7, PREVIEW): owner-invoked certificate path over the pinned Apps2Samsung CLI.

Owner-invoked command-line tool only: nothing here is reachable from the loopback Helper, no HTTP
route takes a path/DUID/command or runs it, and there is no generic shell runner.

It drives the official Apps2Samsung stable CLI (``apps2samsung-cli``, v3.1.0-win-x64, repo
Apps2Samsung/Apps2Samsung at commit dea1eb391d559b3176f40f612550bdae0cebc493, MIT, by Patrick
Stel). NO upstream code is copied: the command / JSON / exit-code contract encoded below was read
off that pinned source. The CLI binary is downloaded, extracted and run by the parent, into owner
storage; this tool only verifies (by pinned bytes) and invokes a binary the owner points it at.

Writes honesty: this slice makes NO TV writes. It is NOT free of PC writes: an explicit,
owner-invoked ``prepare --allow-samsung-signin`` writes owner-private CANDIDATE certificate state
and, only after full validation, activates it into the owner certificate store. ``verify``,
``inspect``, ``acquire`` and the reuse path write no certificate state.

Scope and safety: we only ever run the LOCAL ``cert`` verb with no ``--tv`` (profile inspection
and ``--prepare --duids``). We never run ``--tv``/``scan``/``install``/``tvstore``: the upstream
composition root wires ``InProcessSdbEngine.ClearCompetingSdbServer = ProcessHelper.KillSdbServers``
(CliServices.cs), so any TV-connecting verb can kill unrelated Tizen SDK/sdb servers on this PC.
Local cert work never connects to a TV, so it never triggers that.
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True  # importing owner_init/tvkit must never create files under the repo

import argparse
import datetime
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "helper") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "helper"))
if str(REPO_ROOT / "public-toolkit") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "public-toolkit"))
import owner_init as oi  # noqa: E402  (reuse check_outside_repos, make_private_dir/file, _write_new)
from tvkit import strictjson as sj  # noqa: E402

# A mature PKCS#12 parser for the author CERTIFICATE fingerprint. Pinned dependency: the official
# PyPI package `cryptography` (tested here against 49.0.0; the parent pins the exact version in the
# Helper's isolated runtime). The import is optional so Windows and Linux CI imports work without it;
# the fingerprint functions refuse clearly when it is absent. No hand-written PKCS#12 crypto.
try:
    from cryptography.hazmat.primitives import hashes as _crypto_hashes
    from cryptography.hazmat.primitives.serialization import pkcs12 as _crypto_pkcs12
    _HAVE_CRYPTO = True
except Exception:  # pragma: no cover - exercised only where cryptography is missing
    _HAVE_CRYPTO = False

# ---- pinned upstream dependency bytes (the parent verified these against the official release) ----
UPSTREAM_REPO = "Apps2Samsung/Apps2Samsung"
UPSTREAM_COMMIT = "dea1eb391d559b3176f40f612550bdae0cebc493"
CLI_VERSION = "3.1.0"
CLI_ZIP_NAME = "Apps2Samsung-cli-v3.1.0-win-x64.zip"
CLI_ZIP_SIZE = 34492438
CLI_ZIP_SHA256 = "02e9228c81f6b519879036a7041b40233ddca0e908a46952ebbcd1e1ed8e5a42"
CLI_EXE_NAME = "apps2samsung-cli.exe"
CLI_EXE_SIZE = 79738754
CLI_EXE_SHA256 = "29b64e7791b83ebfe95d1ff3f697e0bc4f856f070ad0fbf27e433e4cdfb58a55"
UPSTREAM_VERBS = ("scan", "tv", "catalog", "installed", "install", "uninstall", "launch", "cert", "tvstore")

# ---- upstream contract ----
EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_STOPPED = 0, 1, 2, 3   # Output.cs
LEVELS = ("Public", "Partner")
PROFILE_DIR_NAME = {"Public": "Jelly2Sams - Public", "Partner": "Jelly2Sams - Partner"}
CERT_SUBDIR = "Certificate"                            # CliPaths.CertificateStore = <data-dir>/Certificate
AUTHOR_FILE = "author.p12"                             # CertificateProvisioningService.AuthorFileName
DISTRIBUTOR_FILE = "distributor.p12"
PASSWORD_FILE = "password.txt"
DUID_RE = re.compile(r"[A-Za-z0-9]{10,64}")            # Models/TizenDuid.cs
HEX64_RE = re.compile(r"[0-9a-fA-F]{64}")              # ambiguous: a raw DUID may be 64 hex, so may our token
VERSION_RE = re.compile(r"3\.1\.0([-+].*)?")
MAX_DUIDS_PER_PROFILE = 32

# ---- our own owner-private artifacts ----
PIN_SUBDIR = "quiettv-bootstrap"
CANDIDATE_SUBDIR = "quiettv-bootstrap/candidates"
PIN_SCHEMA = "quiettv-bootstrap-author-cert/1"         # distinct schema: a CERT fingerprint, not a container hash
MAX_PIN_BYTES = 4096
ACQUIRE_SCHEMA = "quiettv-bootstrap-acquisition/1"

# ---- subprocess containment ----
RUN_TIMEOUT_S = 60.0
VERSION_TIMEOUT_S = 20.0
MAX_STDOUT_BYTES = 1 * 1024 * 1024
MAX_STDERR_BYTES = 256 * 1024
HASH_CHUNK = 1024 * 1024
MAX_ZIP_BYTES = 128 * 1024 * 1024
MAX_EXE_BYTES = 256 * 1024 * 1024
MAX_AUTHOR_BYTES = 1 * 1024 * 1024
MAX_CERT_FILE_BYTES = 1 * 1024 * 1024
ENV_ALLOW = ("SystemRoot", "windir", "SystemDrive", "PATH", "PATHEXT", "TEMP", "TMP",
             "COMSPEC", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE")


class BootstrapError(ValueError):
    """A refusal. Its message is fixed/non-sensitive; it never carries a path, DUID or child output."""


def _fail(message: str) -> None:
    raise BootstrapError(message)


def _no_repo(path: Path, *, must_not_exist: bool) -> Path:
    try:
        return oi.check_outside_repos(str(path), must_not_exist=must_not_exist)
    except oi.OwnerInitError as exc:
        raise BootstrapError(str(exc)) from None


def _make_private_dir(path: Path) -> None:
    try:
        oi.make_private_dir(path)
    except oi.OwnerInitError as exc:
        raise BootstrapError(str(exc)) from None


def _make_private_file(path: Path) -> None:
    try:
        oi.make_private_file(path)
    except oi.OwnerInitError as exc:
        raise BootstrapError(str(exc)) from None


# ---- path validation (no links/reparse/UNC, no '..'; leaf AND every ancestor) ----

def _is_link_or_reparse(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _ancestors_are_plain(path: Path, on_fail: str) -> None:
    """Refuse if the leaf or ANY ancestor is a symlink/junction/reparse point."""
    for component in (path, *path.parents):
        try:
            info = os.lstat(component)
        except FileNotFoundError:
            continue
        except (ValueError, OSError):
            _fail(on_fail)
        if _is_link_or_reparse(info):
            _fail(on_fail)


def _validate_local_path(value: str, label: str, *, must_exist: bool) -> Path:
    if type(value) is not str or not value or "\x00" in value:
        _fail(f"{label} must be a real local path")
    if "://" in value or value.startswith(("\\\\", "//")):
        _fail(f"{label} must be a local path, not a URL or network share")
    # Reject a raw relative path BEFORE abspath() can silently anchor it to the current directory.
    # This also catches a Windows drive-relative path (e.g. "C:foo"), which has a drive but no root.
    drive, tail = os.path.splitdrive(value)
    if not os.path.isabs(value) or (drive and not tail.startswith(("\\", "/"))):
        _fail(f"{label} must be an absolute path (not relative or drive-relative)")
    raw = os.path.abspath(value)
    path = Path(raw)
    if not path.is_absolute() or ".." in Path(value).parts:
        _fail(f"{label} must be an absolute path without '..'")
    try:
        if os.path.realpath(raw).startswith(("\\\\", "//")):
            _fail(f"{label} resolves to a network share")
    except (ValueError, OSError):
        _fail(f"{label} could not be resolved")
    _ancestors_are_plain(path, f"{label} crosses a symlink, junction or reparse point")
    if must_exist and not path.exists():
        _fail(f"{label} does not exist")
    return path


def _plain_file_or_absent(path: Path, label: str) -> Path | None:
    """Return the path if it is a plain regular file with plain ancestors; None only if ABSENT.

    An unreadable path, a link/reparse (leaf or ancestor) or a non-regular file is a refusal, never
    treated as 'absent' — so a swapped-in junction cannot masquerade as a missing file.
    """
    _ancestors_are_plain(path.parent, f"{label} crosses a symlink, junction or reparse point")
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except (ValueError, OSError):
        _fail(f"{label} could not be read")
    if _is_link_or_reparse(info):
        _fail(f"{label} is a symlink, junction or reparse point")
    if not stat.S_ISREG(info.st_mode):
        _fail(f"{label} is not a plain file")
    return path


def _sha256_file(path: Path, max_bytes: int, label: str) -> str:
    h = hashlib.sha256()
    total = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(HASH_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                _fail(f"{label} is larger than the pinned size")
            h.update(chunk)
    return h.hexdigest()


def _read_bounded(path: Path, max_bytes: int, label: str) -> bytes:
    with open(path, "rb") as f:
        data = f.read(max_bytes + 1)
    if len(data) > max_bytes:
        _fail(f"{label} is larger than expected")
    return data


# ---- data-dir: private, per-owner, outside every git checkout ----

def resolve_data_dir(value: str, *, create: bool) -> Path:
    path = _validate_local_path(value, "--data-dir", must_exist=False)
    if path.exists():
        if not path.is_dir():
            _fail("--data-dir must be a directory")
        _no_repo(path, must_not_exist=False)
        return path
    if not create:
        _fail("--data-dir does not exist")
    target = _no_repo(path, must_not_exist=True)
    if not target.parent.is_dir():
        _fail("--data-dir parent directory must already exist")
    _make_private_dir(target)
    _no_repo(target, must_not_exist=False)
    return target


# ---- the process runner: streamed caps on BOTH pipes, whole-tree kill, reaped children ----

def _launcher_for(exe: Path) -> list[str]:
    """Production launcher for a verified exe. Overridden ONLY through this explicit test seam."""
    return [str(exe)]


def _child_env(data_dir: Path | None) -> dict:
    env = {k: os.environ[k] for k in ENV_ALLOW if k in os.environ}
    if data_dir is not None:
        env["APPS2SAMSUNG_DATA_DIR"] = str(data_dir)
    return env


def _kill_tree(proc: subprocess.Popen) -> None:
    """Terminate the launched process AND its descendants, then reap."""
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=10)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except Exception:
        pass


def _stream_reader(pipe, cap: int, out: bytearray, overflow: threading.Event) -> None:
    """Read a pipe in chunks up to ``cap`` bytes; stop and flag on overflow. Bounded memory."""
    try:
        while True:
            chunk = pipe.read(65536)
            if not chunk:
                return
            room = cap - len(out)
            if room <= 0 or len(chunk) > room:
                out.extend(chunk[:max(room, 0)])
                overflow.set()
                return
            out.extend(chunk)
    except (OSError, ValueError):
        return
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def _run_cli(launcher: list[str], cli_args: list[str], *, data_dir: Path | None,
             timeout: float) -> tuple[int, str]:
    """Run ``launcher + cli_args`` and return (exit code, stdout text). Never returns child stderr.

    No shell; a hard deadline; independent byte caps on stdout AND stderr enforced while streaming;
    on overflow or timeout the whole process tree is killed and the pipes/children reaped. A refusal
    here is a fixed BootstrapError; the child's output is never placed in the error.
    """
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    popen_kwargs: dict = {}
    if os.name != "nt":
        popen_kwargs["start_new_session"] = True  # own process group, so os.killpg reaps descendants
    try:
        proc = subprocess.Popen(
            [*launcher, *cli_args],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=_child_env(data_dir), cwd=str(data_dir) if data_dir is not None else None,
            creationflags=creationflags, close_fds=True, **popen_kwargs,
        )
    except (OSError, ValueError):
        _fail("could not start the Apps2Samsung CLI")

    out_buf, err_buf = bytearray(), bytearray()
    overflow = threading.Event()
    t_out = threading.Thread(target=_stream_reader, args=(proc.stdout, MAX_STDOUT_BYTES, out_buf, overflow),
                             daemon=True)
    t_err = threading.Thread(target=_stream_reader, args=(proc.stderr, MAX_STDERR_BYTES, err_buf, overflow),
                             daemon=True)
    t_out.start()
    t_err.start()
    deadline = time.monotonic() + timeout
    timed_out = False
    try:
        while True:
            if proc.poll() is not None:
                break
            if overflow.is_set():
                break
            if time.monotonic() >= deadline:
                timed_out = True
                break
            time.sleep(0.02)
    finally:
        reaped = proc.poll() is not None
        if not reaped:
            _kill_tree(proc)
        t_out.join(timeout=5)
        t_err.join(timeout=5)
    if timed_out:
        _fail("the Apps2Samsung CLI did not finish in time")
    if overflow.is_set():
        _fail("the Apps2Samsung CLI produced more output than expected")
    return proc.returncode if proc.returncode is not None else EXIT_FAILED, out_buf.decode("utf-8", "replace")


# ---- strict JSON + exact profile validation ----

def _parse_cli_json(text: str) -> dict:
    try:
        value = sj.loads(text)
    except (sj.StrictJsonError, RecursionError, ValueError):
        _fail("the Apps2Samsung CLI did not return strict JSON")
    if not isinstance(value, dict) or value.get("ok") is not True:
        _fail("the Apps2Samsung CLI did not return the expected result")
    return value


def _require_bool(value: Any, label: str) -> bool:
    if type(value) is not bool:      # reject "false"/0/1 posing as a boolean
        _fail(f"{label} must be a real boolean")
    return value


def _require_iso_date(value: Any, label: str) -> str:
    if type(value) is not str:
        _fail(f"{label} must be a date string")
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        _fail(f"{label} must be an ISO date")
    return value


def _validate_duid_list(value: Any, label: str) -> list[str]:
    duids = sj.require_list(value, label, max_items=MAX_DUIDS_PER_PROFILE)
    out: list[str] = []
    for d in duids:
        if type(d) is not str or not DUID_RE.fullmatch(d):
            _fail(f"{label} has an entry that is not a valid DUID")
        out.append(d)
    return out


def _validate_profile_state(entry: Any, label: str) -> dict:
    """One upstream ProfileState -> a validated status-only dict. Never forwards a store/profile path."""
    try:
        sj.require_object(entry, label, required=("level", "exists", "usable", "duids"),
                          optional=("validFrom", "validUntil", "path"))
    except sj.StrictJsonError:
        _fail(f"{label} is not a valid profile object")
    level = entry["level"]
    if level not in LEVELS:
        _fail(f"{label} has an unknown level")
    exists = _require_bool(entry["exists"], f"{label}.exists")
    usable = _require_bool(entry["usable"], f"{label}.usable")
    duids = _validate_duid_list(entry["duids"], f"{label}.duids")
    vf, vu = entry.get("validFrom"), entry.get("validUntil")
    valid_from = _require_iso_date(vf, f"{label}.validFrom") if vf is not None else None
    valid_until = _require_iso_date(vu, f"{label}.validUntil") if vu is not None else None
    return {"level": level, "exists": exists, "usable": usable, "duids": duids,
            "validFrom": valid_from, "validUntil": valid_until}


def _validate_inspect(top: dict) -> dict:
    try:
        sj.require_object(top, "cert result", required=("ok", "profiles"),
                          optional=("store", "tv", "tvDuid"))
    except sj.StrictJsonError:
        _fail("the Apps2Samsung CLI did not return the expected result")
    profiles_raw = sj.require_list(top["profiles"], "cert profiles", max_items=8)
    if len(profiles_raw) != 2:
        _fail("the Apps2Samsung CLI did not return exactly the Public and Partner profiles")
    profiles = [_validate_profile_state(p, f"profile[{i}]") for i, p in enumerate(profiles_raw)]
    levels = {p["level"] for p in profiles}
    if levels != set(LEVELS):
        _fail("the Apps2Samsung CLI did not return unique Public and Partner profiles")
    return {p["level"]: p for p in profiles}


def _validate_prepare(top: dict, level: str) -> dict:
    try:
        sj.require_object(top, "prepare result", required=("ok", "duid", "level", "profile"),
                          optional=("tv",))
    except sj.StrictJsonError:
        _fail("the Apps2Samsung CLI did not return the expected result")
    if top["level"] != level or type(top["duid"]) is not str or not DUID_RE.fullmatch(top["duid"]):
        _fail("the Apps2Samsung CLI returned a mismatched prepare result")
    profile = _validate_profile_state(top["profile"], "prepare profile")
    if profile["level"] != level:
        _fail("the Apps2Samsung CLI returned a profile for the wrong level")
    return profile


# ---- author CERTIFICATE fingerprint (public cert, not the encrypted container) ----

def author_cert_fingerprint(p12_bytes: bytes, password: str) -> str:
    """SHA-256 of the author X.509 certificate (DER). Stable across re-encryption of the same cert.

    Uses the pinned `cryptography` PKCS#12 loader. Malformed bytes or a container with no certificate
    is a refusal. The password and private key are used only to open the container; neither they nor
    any key material are returned or logged.
    """
    if not _HAVE_CRYPTO:
        _fail("the pinned cryptography package is required to fingerprint the author certificate")
    try:
        _key, cert, _extra = _crypto_pkcs12.load_key_and_certificates(p12_bytes, password.encode("utf-8"))
    except Exception:
        _fail("the author certificate container could not be read")
    if cert is None:
        _fail("the author container holds no certificate")
    return cert.fingerprint(_crypto_hashes.SHA256()).hex()


def _author_fingerprint(profile_dir: Path) -> str | None:
    """The author cert fingerprint for a profile dir, or None if the profile has no author. Refuses a
    link/reparse (leaf or ancestor), an unreadable file (not 'absent') or malformed bytes."""
    author = _plain_file_or_absent(profile_dir / AUTHOR_FILE, "author certificate")
    if author is None:
        return None
    pw_path = _plain_file_or_absent(profile_dir / PASSWORD_FILE, "certificate password file")
    if pw_path is None:
        _fail("the signing profile has an author certificate but no password file")
    p12_bytes = _read_bounded(author, MAX_AUTHOR_BYTES, "author certificate")
    try:
        password = _read_bounded(pw_path, MAX_CERT_FILE_BYTES,
                                 "certificate password file").decode("utf-8", "strict").strip()
    except UnicodeError:
        _fail("the certificate password file is not valid UTF-8")
    return author_cert_fingerprint(p12_bytes, password)


# ---- author-cert pin (distinct schema; bounded; shape-checked) ----

def _pin_path(data_dir: Path, level: str) -> Path:
    return data_dir / PIN_SUBDIR / f"author-cert-{level.lower()}.json"


def _load_pin(data_dir: Path, level: str) -> str | None:
    path = _plain_file_or_absent(_pin_path(data_dir, level), "author-identity pin")
    if path is None:
        return None
    raw = _read_bounded(path, MAX_PIN_BYTES, "author-identity pin")
    try:
        obj = sj.loads(raw)
    except (sj.StrictJsonError, ValueError):
        _fail("the author-identity pin is unreadable")
    if not isinstance(obj, dict):
        _fail("the author-identity pin is malformed")
    sha = obj.get("authorCertSha256")
    if obj.get("schema") != PIN_SCHEMA or type(sha) is not str or not re.fullmatch(r"[0-9a-f]{64}", sha):
        _fail("the author-identity pin is malformed")
    return sha


def _write_pin(data_dir: Path, level: str, sha: str) -> None:
    pin_dir = data_dir / PIN_SUBDIR
    if not pin_dir.exists():
        _make_private_dir(pin_dir)
    path = _pin_path(data_dir, level)
    payload = json.dumps({"schema": PIN_SCHEMA, "level": level, "authorCertSha256": sha}, indent=2) + "\n"
    oi._write_new(path, payload.encode("ascii"))
    _make_private_file(path)


# ---- DUID validation (distinct identity domain; no silent conversion) ----

def validate_duid(value: Any, *, assert_raw_hex: bool = False) -> str:
    """The TV's raw Tizen DUID (^[A-Za-z0-9]{10,64}$). A 64-hex value is ambiguous: a real raw DUID
    may be 64 hex, and so may a QuietTV hashed-identity token or a stripped UUID. We never guess —
    a bare 64-hex value is accepted only when the owner explicitly asserts it is the raw DUID."""
    if type(value) is not str or not DUID_RE.fullmatch(value):
        _fail("the DUID must be the TV's raw device id (10-64 letters and digits)")
    if HEX64_RE.fullmatch(value) and not assert_raw_hex:
        _fail("that 64-hex value is ambiguous with a QuietTV device-id token; pass --assert-raw-duid "
              "only if it really is the TV's raw DUID from apps2samsung-cli")
    return value


# ---- dependency verification: MANDATORY exe byte pin before any command runs ----

@dataclass(frozen=True)
class DependencyContract:
    launcher: tuple[str, ...]
    version: str


def verify_dependency(*, cli_exe: str, cli_zip: str | None = None,
                      runner: Callable[..., tuple[int, str]] = _run_cli) -> DependencyContract:
    """Bind the exe to the pinned bytes BEFORE running it; optionally verify the archive bytes too.

    The exe's pinned size and SHA-256 are the gate on every real command; ``--version`` is only a
    secondary confirmation. A binary is never accepted on filename or version alone, and the launcher
    is derived here from the verified exe (never from a caller-supplied override).
    """
    if cli_zip is not None:
        zpath = _validate_local_path(cli_zip, "--cli-zip", must_exist=True)
        if _plain_file_or_absent(zpath, "--cli-zip") is None:
            _fail("--cli-zip does not exist")
        if zpath.stat().st_size != CLI_ZIP_SIZE:
            _fail("the Apps2Samsung CLI archive is not the pinned release (size differs)")
        if _sha256_file(zpath, MAX_ZIP_BYTES, "--cli-zip") != CLI_ZIP_SHA256:
            _fail("the Apps2Samsung CLI archive is not the pinned release (digest differs)")

    exe = _validate_local_path(cli_exe, "--cli-exe", must_exist=True)
    if _plain_file_or_absent(exe, "--cli-exe") is None:
        _fail("--cli-exe does not exist")
    _no_repo(exe, must_not_exist=False)  # the exe must live in owner storage, not a repo
    if exe.stat().st_size != CLI_EXE_SIZE:
        _fail("the Apps2Samsung CLI executable is not the pinned release (size differs)")
    if _sha256_file(exe, MAX_EXE_BYTES, "--cli-exe") != CLI_EXE_SHA256:
        _fail("the Apps2Samsung CLI executable is not the pinned release (digest differs)")

    launcher = _launcher_for(exe)
    code, out = runner(launcher, ["--version"], data_dir=None, timeout=VERSION_TIMEOUT_S)
    reported = out.strip().splitlines()[0].strip() if out.strip() else ""
    if code != EXIT_OK or not VERSION_RE.fullmatch(reported):
        _fail(f"the Apps2Samsung CLI did not report version {CLI_VERSION}")
    return DependencyContract(tuple(launcher), reported)


# ---- inspect (read-only; no TV, no network, no sign-in) ----

def _raw_inspect(dep: DependencyContract, data_dir: Path,
                 runner: Callable[..., tuple[int, str]]) -> dict:
    code, out = runner(list(dep.launcher), ["cert", "--json", "-q"], data_dir=data_dir, timeout=RUN_TIMEOUT_S)
    if code != EXIT_OK:
        _fail("the Apps2Samsung CLI could not read the signing profiles")
    return _validate_inspect(_parse_cli_json(out))


def inspect_profiles(dep: DependencyContract, data_dir: Path, *,
                     runner: Callable[..., tuple[int, str]] = _run_cli) -> dict:
    by_level = _raw_inspect(dep, data_dir, runner)
    profiles = []
    for level in LEVELS:
        p = by_level[level]
        profiles.append({"level": level, "exists": p["exists"], "usable": p["usable"],
                         "validFrom": p["validFrom"], "validUntil": p["validUntil"],
                         "coveredDuidCount": len(p["duids"])})
    return {"ok": True, "verb": "inspect", "profiles": profiles}


# ---- prepare / reuse, staged in an owner-private candidate store before activation ----

def _covers(profile: dict, duid: str) -> bool:
    return profile["usable"] and any(d.lower() == duid.lower() for d in profile["duids"])


def _bounded_copy(src_dir: Path, dst_dir: Path) -> None:
    """Copy author.p12, distributor.p12 and password.txt (plain, bounded) into a fresh candidate dir."""
    if not dst_dir.parent.exists():
        _make_private_dir(dst_dir.parent)
    _make_private_dir(dst_dir)
    for name, cap in ((AUTHOR_FILE, MAX_AUTHOR_BYTES), (DISTRIBUTOR_FILE, MAX_AUTHOR_BYTES),
                      (PASSWORD_FILE, MAX_CERT_FILE_BYTES)):
        src = _plain_file_or_absent(src_dir / name, f"existing {name}")
        if src is None:
            continue
        data = _read_bounded(src, cap, f"existing {name}")
        oi._write_new(dst_dir / name, data)
        _make_private_file(dst_dir / name)


def _activate(candidate_profile_dir: Path, real_profile_dir: Path) -> Path | None:
    """Move the validated candidate into the owner store, returning the backup of any prior profile.

    The originals are byte-unchanged until this point; only here does the owner store change. The
    backup is NOT deleted here: the caller keeps it until every remaining fallible step (the final
    fingerprint confirm and the pin write) has succeeded, then deletes it, or restores from it on any
    failure. On a rename failure the originals are restored here and a finite BootstrapError is raised.
    """
    real_profile_dir.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(8)
    backup = None
    if real_profile_dir.exists():
        backup = real_profile_dir.with_name(real_profile_dir.name + ".bak-" + token)
        os.rename(real_profile_dir, backup)
    try:
        os.rename(candidate_profile_dir, real_profile_dir)
    except OSError:
        if backup is not None and not real_profile_dir.exists():
            os.rename(backup, real_profile_dir)  # restore the originals unchanged
        _fail("the signing profile could not be activated; the originals were left unchanged")
    return backup


def _restore_profile(real_profile_dir: Path, backup: Path | None) -> bool:
    """Undo an activation: discard whatever was activated and move the backup back, if any."""
    try:
        shutil.rmtree(real_profile_dir, ignore_errors=True)
        if backup is not None:
            os.rename(backup, real_profile_dir)
        return True
    except OSError:
        return False  # keep the prior-profile backup for owner recovery


def _remove_pin_quietly(data_dir: Path, level: str) -> bool:
    """Best-effort removal of a freshly written pin during rollback (there was none before)."""
    try:
        path = _pin_path(data_dir, level)
        if path.exists():
            path.unlink()
        return True
    except OSError:
        return False


def prepare_certificate(dep: DependencyContract, data_dir: Path, *, duid: str, level: str = "Public",
                        allow_sign_in: bool = False, trust_current_author: bool = False,
                        assert_raw_hex: bool = False,
                        runner: Callable[..., tuple[int, str]] = _run_cli) -> dict:
    """Prepare or reuse the signing certificate for ``duid`` at ``level``, preserving author identity.

    Reuse (DUID already covered by a usable author) returns the validated existing coverage WITHOUT
    calling the CLI. A mint/extend (needs a Samsung sign-in) is refused unless ``allow_sign_in``; when
    allowed it runs in a fresh owner-private CANDIDATE store, is validated (author cert fingerprint +
    exact requested-DUID coverage + usable profile), and only then activated into the owner store. The
    originals stay byte-unchanged on every failed/non-zero/timeout/malformed/wrong-author/wrong-DUID
    case. ``--force-login`` is never passed.
    """
    if level not in LEVELS:
        _fail("level must be Public or Partner")
    duid = validate_duid(duid, assert_raw_hex=assert_raw_hex)
    real_profile_dir = data_dir / CERT_SUBDIR / PROFILE_DIR_NAME[level]

    pinned = _load_pin(data_dir, level)
    before = _author_fingerprint(real_profile_dir)   # None if absent; refuses on unreadable/link/malformed
    if pinned is not None:
        if before is None:
            _fail("the pinned author identity is missing from the signing profile; refusing")
        if before != pinned:
            _fail("the signing profile's author identity does not match the pinned identity; refusing")

    existing = _raw_inspect(dep, data_dir, runner)[level]

    # -- reuse branch: never calls the CLI --
    if _covers(existing, duid):
        if before is None:
            _fail("the profile reports coverage but has no readable author certificate; refusing")
        if pinned is None:
            if not trust_current_author:
                _fail("an author certificate already covers this TV but is not pinned; re-run with "
                      "--trust-current-author to adopt the current author identity")
            _write_pin(data_dir, level, before)
        return {"ok": True, "verb": "prepare", "level": level, "reused": True,
                "coveredDuidCount": len(existing["duids"]),
                "authorPinRecorded": pinned is None and trust_current_author}

    # -- mint/extend branch: staged, validated, then activated --
    if not allow_sign_in:
        _fail("covering this TV needs a Samsung sign-in (a new or extended certificate); re-run with "
              "--allow-samsung-signin to sign in on Samsung's own page")

    base_dir = data_dir / PIN_SUBDIR
    if not base_dir.exists():
        _make_private_dir(base_dir)
    candidates_root = data_dir / CANDIDATE_SUBDIR
    if not candidates_root.exists():
        _make_private_dir(candidates_root)
    candidate = candidates_root / secrets.token_hex(8)
    _make_private_dir(candidate)
    try:
        candidate_profile_dir = candidate / CERT_SUBDIR / PROFILE_DIR_NAME[level]
        if before is not None:
            # Extend: give the candidate a copy of the existing profile so the author keypair is reused.
            _bounded_copy(real_profile_dir, candidate_profile_dir)

        args = ["cert", "--prepare", "--duids", duid, "--json", "-q"]
        if level == "Partner":
            args.append("--partner")
        # --force-login is intentionally never passed, so an existing author keypair is reused.
        code, out = runner(list(dep.launcher), args, data_dir=candidate, timeout=RUN_TIMEOUT_S)
        if code != EXIT_OK:
            _fail("the Apps2Samsung CLI could not prepare the certificate")
        profile = _validate_prepare(_parse_cli_json(out), level)
        if not _covers(profile, duid):
            _fail("the prepared certificate does not cover the requested TV; refusing")

        candidate_fp = _author_fingerprint(candidate_profile_dir)
        if candidate_fp is None:
            _fail("the prepared certificate has no author certificate; refusing")
        if before is not None and candidate_fp != before:
            _fail("the author identity changed during prepare; refusing")
        expected = pinned if pinned is not None else before
        if expected is not None and candidate_fp != expected:
            _fail("the prepared author identity does not match the pinned identity; refusing")

        backup = _activate(candidate_profile_dir, real_profile_dir)
    finally:
        shutil.rmtree(candidate, ignore_errors=True)

    # The owner store now holds the candidate, but the backup is retained until BOTH remaining
    # fallible steps succeed. Any failure here (final verify, pin write, pin ACL) rolls the original
    # profile bytes AND the pin back, so no originals are silently deleted on a failed operation.
    pin_recorded = False
    committed = False
    try:
        try:
            final_fp = _author_fingerprint(real_profile_dir)
            if final_fp is None or final_fp != candidate_fp:
                _fail("the activated certificate could not be confirmed; refusing")
            if pinned is None:
                _write_pin(data_dir, level, final_fp)
                pin_recorded = True
            committed = True
        finally:
            if committed:
                if backup is not None:
                    shutil.rmtree(backup, ignore_errors=True)
            else:
                pin_restored = pinned is not None or _remove_pin_quietly(data_dir, level)
                profile_restored = _restore_profile(real_profile_dir, backup)
                if not pin_restored or not profile_restored:
                    _fail("certificate rollback is incomplete; stop and recover the retained prior "
                          "profile backup before retrying")
    except BootstrapError:
        raise
    except OSError:
        _fail("the signing profile could not be finalized; it was rolled back unchanged")
    return {"ok": True, "verb": "prepare", "level": level, "reused": False,
            "coveredDuidCount": len(profile["duids"]), "authorPinRecorded": pin_recorded}


# ---- honest refused acquisition state ----

def acquisition_state() -> dict:
    return {
        "ok": True,
        "verb": "acquire",
        "schema": ACQUIRE_SCHEMA,
        "status": "refused",
        "reason": "The pinned Apps2Samsung stable CLI cannot retrieve the ProductInfo report or run "
                  "the fixed native inventory collector on the TV.",
        "upstream": {"repo": UPSTREAM_REPO, "commit": UPSTREAM_COMMIT, "cliVersion": CLI_VERSION,
                     "verbs": list(UPSTREAM_VERBS)},
        "gap": {
            "missingOperation": "a pinned SDB pull / shell / file-read or a dedicated capture verb "
                                "that returns a file's bytes from the TV",
            "whyTvVerbIsNotEnough": "cert/tv read only {model, developerMode, tizenVersion, duid, "
                                    "ready}; neither returns the ProductInfo report bytes nor our "
                                    "native frame, and a raw DUID is not our hashed ProductInfo "
                                    "identity and must never be substituted for it",
            "sdbKillConstraint": "any TV-connecting verb (tv/scan/install/cert --tv) triggers the "
                                 "upstream InProcessSdbEngine.ClearCompetingSdbServer = "
                                 "ProcessHelper.KillSdbServers, which can kill unrelated Tizen SDK "
                                 "servers on this PC; this slice stays on the local no---tv cert path",
            "neededForGoal": "stage/sign/install/launch/read/remove the isolated diagnostic widget "
                             "and the read-only native inventory, then feed the result to the "
                             "existing Helper --preflight-config seam",
        },
        "fallback": "Until that operation exists, use the manual saved-report preflight "
                    "(helper/server.py --preflight-config) to feed tvkit.factsjoin.",
    }


# ---- CLI ----

def _print(result: dict) -> int:
    print(json.dumps(result, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bootstrap.py",
                                 description="QuietTV bootstrap (preview): owner-invoked certificate "
                                             "path over the pinned Apps2Samsung CLI. No TV writes.")
    sub = ap.add_subparsers(dest="verb", required=True)

    pv = sub.add_parser("verify", help="verify the pinned exe (and optional archive) bytes and version")
    pv.add_argument("--cli-exe", required=True)
    pv.add_argument("--cli-zip", default=None)

    pi = sub.add_parser("inspect", help="read the on-disk signing profiles (no TV, no network, no sign-in)")
    pi.add_argument("--cli-exe", required=True)
    pi.add_argument("--data-dir", required=True)

    pp = sub.add_parser("prepare", help="prepare/reuse the per-owner certificate (author identity preserved)")
    pp.add_argument("--cli-exe", required=True)
    pp.add_argument("--data-dir", required=True)
    pp.add_argument("--duid", required=True, help="the TV's raw Tizen DUID (not a QuietTV hash)")
    pp.add_argument("--partner", action="store_true", help="use the Partner level instead of Public")
    pp.add_argument("--assert-raw-duid", action="store_true",
                    help="assert that a 64-hex --duid really is the TV's raw DUID")
    pp.add_argument("--allow-samsung-signin", action="store_true",
                    help="permit a Samsung sign-in (on Samsung's own page) when reuse is not possible")
    pp.add_argument("--trust-current-author", action="store_true",
                    help="adopt an existing, not-yet-pinned author identity")

    sub.add_parser("acquire", help="print the honest refused report-acquisition state")

    args = ap.parse_args(argv)
    try:
        if args.verb == "acquire":
            return _print(acquisition_state())
        if args.verb == "verify":
            dep = verify_dependency(cli_exe=args.cli_exe, cli_zip=args.cli_zip)
            return _print({"ok": True, "verb": "verify", "version": dep.version})
        dep = verify_dependency(cli_exe=args.cli_exe)
        data_dir = resolve_data_dir(args.data_dir, create=True)
        if args.verb == "inspect":
            return _print(inspect_profiles(dep, data_dir))
        return _print(prepare_certificate(
            dep, data_dir, duid=args.duid, level="Partner" if args.partner else "Public",
            allow_sign_in=args.allow_samsung_signin, trust_current_author=args.trust_current_author,
            assert_raw_hex=args.assert_raw_duid))
    except BootstrapError as exc:
        print("bootstrap refused: " + str(exc), file=sys.stderr)
        return 2
    except (OSError, subprocess.SubprocessError):
        print("bootstrap refused: a local operation failed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

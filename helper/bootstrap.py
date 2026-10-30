"""QuietTV bootstrap (D7, PREVIEW): owner-invoked certificate path over the pinned Apps2Samsung CLI.

This is the first real step away from the manual saved-report preflight toward a live bootstrap.
It is an OWNER-INVOKED command-line tool only: nothing here is reachable from the loopback Helper,
no HTTP route takes a path/DUID/command or runs it, and there is no generic shell runner.

It drives the official Apps2Samsung stable CLI (``apps2samsung-cli``, v3.1.0-win-x64, repo
Apps2Samsung/Apps2Samsung at commit dea1eb391d559b3176f40f612550bdae0cebc493, MIT, by Patrick
Stel). NO upstream code is copied: the command / JSON / exit-code contract encoded below was read
off that pinned source (Apps2Samsung.Cli Program.cs, Output.cs, CliOptions.cs, CliPaths.cs,
Commands/CertCommand.cs, Certificates.cs, Core Certificate/CertificateProvisioningService.cs,
Models/TizenDuid.cs). The CLI binary is downloaded, extracted and run by the parent, into owner
storage; this tool only verifies and invokes a binary the owner points it at.

What it ships:
  * verify   -- pin/verify the dependency bytes (optional zip) and the resolved exe's version.
  * inspect  -- read the on-disk signing profiles (upstream ``cert --json`` with no --tv:
                no TV, no network, no Samsung sign-in); print a STATUS-ONLY projection.
  * prepare  -- the author-identity-preserving certificate step (upstream ``cert --prepare
                --duids``); never --force-login; reuse-only unless --allow-samsung-signin.
  * acquire  -- the HONEST refused acquisition state for the ProductInfo/native reports, naming
                the exact upstream API gap (no SDB pull/shell/capture verb in the stable CLI).

Privacy: the raw DUID, certificate paths, passwords, tokens, child stdout/stderr and the command
line never appear in a result or an error. Results carry status only.
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True  # importing owner_init/tvkit must never create files under the repo

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "helper") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "helper"))
import owner_init as oi  # noqa: E402  (reuse check_outside_repos, make_private_dir/file, _is_link)

# ---- pinned upstream dependency (read from the release API by the parent; never trusted blindly) ----
UPSTREAM_REPO = "Apps2Samsung/Apps2Samsung"
UPSTREAM_COMMIT = "dea1eb391d559b3176f40f612550bdae0cebc493"
CLI_VERSION = "3.1.0"
CLI_ZIP_NAME = "Apps2Samsung-cli-v3.1.0-win-x64.zip"
CLI_ZIP_SIZE = 34492438
CLI_ZIP_SHA256 = "02e9228c81f6b519879036a7041b40233ddca0e908a46952ebbcd1e1ed8e5a42"
# The complete, finite verb set of the stable CLI (Program.cs dispatch). Used to document the
# acquisition gap; we only ever invoke the 'cert' verb from here.
UPSTREAM_VERBS = ("scan", "tv", "catalog", "installed", "install", "uninstall", "launch", "cert", "tvstore")

# ---- upstream contract ----
# Exit codes (Output.cs): 0 ok, 1 failed, 2 usage, 3 stopped-at-a-check.
EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_STOPPED = 0, 1, 2, 3
LEVELS = ("Public", "Partner")
PROFILE_DIR_NAME = {"Public": "Jelly2Sams - Public", "Partner": "Jelly2Sams - Partner"}
CERT_SUBDIR = "Certificate"           # CliPaths.CertificateStore = <data-dir>/Certificate
AUTHOR_FILE = "author.p12"            # CertificateProvisioningService.AuthorFileName
DUID_RE = re.compile(r"[A-Za-z0-9]{10,64}")            # Models/TizenDuid.cs
QUIETTV_HASH_RE = re.compile(r"[0-9a-f]{64}")          # our device-id sha256 token; refused as a raw DUID
VERSION_RE = re.compile(r"3\.1\.0([-+].*)?")           # 3.1.0, optionally with build metadata

# ---- our own owner-private artifacts ----
PIN_SUBDIR = "quiettv-bootstrap"
PIN_SCHEMA = "quiettv-bootstrap-author-pin/1"
ACQUIRE_SCHEMA = "quiettv-bootstrap-acquisition/1"

# ---- subprocess containment ----
RUN_TIMEOUT_S = 60.0
VERSION_TIMEOUT_S = 20.0
MAX_STDOUT_BYTES = 1 * 1024 * 1024
MAX_STDERR_BYTES = 256 * 1024
HASH_CHUNK = 1024 * 1024
MAX_ZIP_BYTES = 64 * 1024 * 1024       # the pinned zip is ~34 MiB; refuse anything far larger
MAX_AUTHOR_BYTES = 1 * 1024 * 1024     # an author.p12 is a few KiB; cap the hash read
# Environment variables that reach the child. The CLI reads APPS2SAMSUNG_DATA_DIR and GITHUB_TOKEN
# from the environment (CliPaths/CliOptions); we set the first ourselves and keep the second out.
ENV_ALLOW = ("SystemRoot", "windir", "SystemDrive", "PATH", "PATHEXT", "TEMP", "TMP",
             "COMSPEC", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE")


class BootstrapError(ValueError):
    """A refusal. Its message is fixed/non-sensitive; it never carries a path, DUID or child output."""


def _fail(message: str) -> None:
    raise BootstrapError(message)


def _no_repo(path: Path, *, must_not_exist: bool) -> Path:
    """owner_init.check_outside_repos, with its fixed OwnerInitError refusal re-raised as ours."""
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


# ---- path validation (no links/reparse/UNC, no '..'; leaf and every ancestor) ----

def _is_link_or_reparse(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _validate_local_path(value: str, label: str, *, must_exist: bool) -> Path:
    """Absolute local path, no '..', no symlink/junction/reparse (leaf or any parent), no UNC/URL."""
    if type(value) is not str or not value or "\x00" in value:
        _fail(f"{label} must be a real local path")
    if "://" in value or value.startswith(("\\\\", "//")):
        _fail(f"{label} must be a local path, not a URL or network share")
    raw = os.path.abspath(value)
    path = Path(raw)
    if not path.is_absolute() or ".." in Path(value).parts:
        _fail(f"{label} must be an absolute path without '..'")
    try:
        if os.path.realpath(raw).startswith(("\\\\", "//")):
            _fail(f"{label} resolves to a network share")
    except (ValueError, OSError):
        _fail(f"{label} could not be resolved")
    for component in (path, *path.parents):
        try:
            info = os.lstat(component)
        except FileNotFoundError:
            continue
        except (ValueError, OSError):
            _fail(f"{label} could not be inspected")
        if _is_link_or_reparse(info):
            _fail(f"{label} crosses a symlink, junction or reparse point")
    if must_exist and not path.exists():
        _fail(f"{label} does not exist")
    return path


def _require_regular_file(path: Path, label: str) -> Path:
    try:
        info = os.lstat(path)
    except (ValueError, OSError):
        _fail(f"{label} does not exist")
    if _is_link_or_reparse(info) or not stat.S_ISREG(info.st_mode):
        _fail(f"{label} must be a plain local file")
    return path


def _sha256_file(path: Path, max_bytes: int) -> str:
    h = hashlib.sha256()
    total = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(HASH_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                _fail("file is larger than expected")
            h.update(chunk)
    return h.hexdigest()


# ---- data-dir: private, per-owner, outside every git checkout ----

def resolve_data_dir(value: str, *, create: bool) -> Path:
    """Validate and (optionally) create a private per-owner data directory outside every repository."""
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
    _no_repo(target, must_not_exist=False)  # re-check the created dir, links resolved
    return target


# ---- the process runner (shell=False arglist, hard timeout, caps, env containment, hidden) ----

def _child_env(data_dir: Path | None) -> dict:
    env = {k: os.environ[k] for k in ENV_ALLOW if k in os.environ}
    if data_dir is not None:
        env["APPS2SAMSUNG_DATA_DIR"] = str(data_dir)
    return env


def _run_cli(launcher: list[str], cli_args: list[str], *, data_dir: Path | None,
             timeout: float) -> tuple[int, str]:
    """Run ``launcher + cli_args`` and return (exit code, stdout text). Never returns child stderr.

    No shell, a hard timeout with child cleanup, output caps and a contained environment. On
    Windows the child gets no console window. A refusal here is a fixed BootstrapError; the child's
    output is never placed in the error.
    """
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        proc = subprocess.Popen(
            [*launcher, *cli_args],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=_child_env(data_dir), cwd=str(data_dir) if data_dir is not None else None,
            creationflags=creationflags, close_fds=True,
        )
    except (OSError, ValueError):
        _fail("could not start the Apps2Samsung CLI")
    try:
        out, _err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.communicate(timeout=5)
        except Exception:
            pass
        _fail("the Apps2Samsung CLI did not finish in time")
    except Exception:
        proc.kill()
        _fail("the Apps2Samsung CLI could not be run")
    if len(out) > MAX_STDOUT_BYTES:
        _fail("the Apps2Samsung CLI produced more output than expected")
    return proc.returncode, out.decode("utf-8", "replace")


def _parse_json(text: str) -> dict:
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        _fail("the Apps2Samsung CLI did not return the expected result")
    if not isinstance(value, dict) or value.get("ok") is not True:
        _fail("the Apps2Samsung CLI did not return the expected result")
    return value


# ---- verb: verify the dependency ----

@dataclass(frozen=True)
class DependencyContract:
    launcher: tuple[str, ...]
    version: str


def verify_dependency(*, cli_exe: str, cli_zip: str | None = None,
                      runner: Callable[..., tuple[int, str]] = _run_cli,
                      launcher_override: list[str] | None = None) -> DependencyContract:
    """Verify the dependency: optional zip bytes against the pin, and the exe's reported version.

    A binary is never accepted on filename or version alone; the version is read by running it.
    When a zip is supplied, its exact size and SHA-256 must match the pinned release.
    """
    if cli_zip is not None:
        zpath = _require_regular_file(_validate_local_path(cli_zip, "--cli-zip", must_exist=True), "--cli-zip")
        size = zpath.stat().st_size
        if size != CLI_ZIP_SIZE:
            _fail("the Apps2Samsung CLI archive is not the pinned release (size differs)")
        if _sha256_file(zpath, MAX_ZIP_BYTES) != CLI_ZIP_SHA256:
            _fail("the Apps2Samsung CLI archive is not the pinned release (digest differs)")

    exe = _require_regular_file(_validate_local_path(cli_exe, "--cli-exe", must_exist=True), "--cli-exe")
    _no_repo(exe, must_not_exist=False)  # the exe must live in owner storage, not a repo
    launcher = launcher_override if launcher_override is not None else [str(exe)]
    code, out = runner(launcher, ["--version"], data_dir=None, timeout=VERSION_TIMEOUT_S)
    reported = out.strip().splitlines()[0].strip() if out.strip() else ""
    if code != EXIT_OK or not VERSION_RE.fullmatch(reported):
        _fail(f"the Apps2Samsung CLI did not report version {CLI_VERSION}")
    return DependencyContract(tuple(launcher), reported)


# ---- verb: inspect the on-disk signing profiles (no TV, no network, no sign-in) ----

def _raw_inspect(dep: DependencyContract, data_dir: Path,
                 runner: Callable[..., tuple[int, str]]) -> dict:
    code, out = runner(list(dep.launcher), ["cert", "--json", "-q"], data_dir=data_dir, timeout=RUN_TIMEOUT_S)
    if code != EXIT_OK:
        _fail("the Apps2Samsung CLI could not read the signing profiles")
    top = _parse_json(out)
    if not isinstance(top.get("profiles"), list):
        _fail("the Apps2Samsung CLI did not return the expected result")
    return top


def _project_profile(entry: Any) -> dict:
    """Status-only projection of one upstream ProfileState. No raw DUIDs, no store/profile path."""
    if not isinstance(entry, dict) or entry.get("level") not in LEVELS:
        _fail("the Apps2Samsung CLI did not return the expected result")
    duids = entry.get("duids", [])
    if not isinstance(duids, list):
        _fail("the Apps2Samsung CLI did not return the expected result")
    return {
        "level": entry["level"],
        "exists": bool(entry.get("exists", False)),
        "usable": bool(entry.get("usable", False)),
        "validFrom": entry.get("validFrom"),
        "validUntil": entry.get("validUntil"),
        "coveredDuidCount": len(duids),
    }


def inspect_profiles(dep: DependencyContract, data_dir: Path, *,
                     runner: Callable[..., tuple[int, str]] = _run_cli) -> dict:
    top = _raw_inspect(dep, data_dir, runner)
    profiles = [_project_profile(e) for e in top["profiles"]]
    return {"ok": True, "verb": "inspect", "profiles": profiles}


# ---- verb: prepare / reuse the per-owner certificate, preserving author identity ----

def _author_path(data_dir: Path, level: str) -> Path:
    return data_dir / CERT_SUBDIR / PROFILE_DIR_NAME[level] / AUTHOR_FILE


def _author_sha(data_dir: Path, level: str) -> str | None:
    """SHA-256 of the on-disk author.p12 container, or None if absent. Refuses a link/reparse."""
    path = _author_path(data_dir, level)
    try:
        info = os.lstat(path)
    except (ValueError, OSError):
        return None
    if _is_link_or_reparse(info) or not stat.S_ISREG(info.st_mode):
        _fail("the signing profile's author file is not a plain local file")
    return _sha256_file(path, MAX_AUTHOR_BYTES)


def _pin_path(data_dir: Path, level: str) -> Path:
    return data_dir / PIN_SUBDIR / f"author-pin-{level.lower()}.json"


def _load_pin(data_dir: Path, level: str) -> str | None:
    path = _pin_path(data_dir, level)
    try:
        info = os.lstat(path)
    except (ValueError, OSError):
        return None
    if _is_link_or_reparse(info) or not stat.S_ISREG(info.st_mode):
        _fail("the author-identity pin is not a plain local file")
    try:
        obj = json.loads(path.read_text("ascii"))
    except (ValueError, OSError, UnicodeDecodeError):
        _fail("the author-identity pin is unreadable")
    sha = obj.get("authorKeyContainerSha256") if isinstance(obj, dict) else None
    if obj.get("schema") != PIN_SCHEMA or type(sha) is not str or not re.fullmatch(r"[0-9a-f]{64}", sha):
        _fail("the author-identity pin is malformed")
    return sha


def _write_pin(data_dir: Path, level: str, sha: str) -> None:
    pin_dir = data_dir / PIN_SUBDIR
    if not pin_dir.exists():
        _make_private_dir(pin_dir)
    path = _pin_path(data_dir, level)
    payload = json.dumps({"schema": PIN_SCHEMA, "level": level, "authorKeyContainerSha256": sha},
                         indent=2) + "\n"
    oi._write_new(path, payload.encode("ascii"))
    _make_private_file(path)


def validate_duid(value: Any) -> str:
    """The TV's raw Tizen DUID. A different identity domain from a network UUID or our hashed token."""
    if type(value) is not str or not DUID_RE.fullmatch(value):
        _fail("the DUID must be the TV's raw device id (10-64 letters and digits)")
    if QUIETTV_HASH_RE.fullmatch(value):
        _fail("that looks like a QuietTV device-id hash, not the TV's raw DUID; use the raw DUID "
              "apps2samsung-cli prints")
    return value


def prepare_certificate(dep: DependencyContract, data_dir: Path, *, duid: str, level: str = "Public",
                        allow_sign_in: bool = False, trust_current_author: bool = False,
                        runner: Callable[..., tuple[int, str]] = _run_cli) -> dict:
    """Prepare or reuse the signing certificate for ``duid`` at ``level``, preserving author identity.

    Never passes --force-login. Reuse-only by default: if covering the DUID would need a Samsung
    sign-in, it refuses unless ``allow_sign_in`` is set. The on-disk author container is pinned and
    must be unchanged across the run; any author-identity mismatch is refused.
    """
    if level not in LEVELS:
        _fail("level must be Public or Partner")
    duid = validate_duid(duid)

    pinned = _load_pin(data_dir, level)
    before = _author_sha(data_dir, level)
    if pinned is not None:
        if before is None:
            _fail("the pinned author identity is missing from the signing profile; refusing")
        if before != pinned:
            _fail("the signing profile's author identity does not match the pinned identity; refusing")
    elif before is not None and not trust_current_author:
        _fail("an author certificate already exists but is not pinned; re-run with "
              "--trust-current-author to adopt the current author identity")

    # Decide reuse vs sign-in from the on-disk profiles (raw read, never exposed).
    top = _raw_inspect(dep, data_dir, runner)
    covered = False
    for entry in top["profiles"]:
        if isinstance(entry, dict) and entry.get("level") == level and entry.get("usable") is True:
            duids = entry.get("duids", [])
            if isinstance(duids, list) and any(isinstance(d, str) and d.lower() == duid.lower() for d in duids):
                covered = True
    if not covered and not allow_sign_in:
        _fail("covering this TV needs a Samsung sign-in (a new or extended certificate); re-run "
              "with --allow-samsung-signin to sign in on Samsung's own page")

    args = ["cert", "--prepare", "--duids", duid, "--json", "-q"]
    if level == "Partner":
        args.append("--partner")
    # --force-login is intentionally never passed, so an existing author keypair is reused, not replaced.
    code, out = runner(list(dep.launcher), args, data_dir=data_dir, timeout=RUN_TIMEOUT_S)
    if code != EXIT_OK:
        _fail("the Apps2Samsung CLI could not prepare the certificate")
    result = _parse_json(out)
    profile = result.get("profile")
    if not isinstance(profile, dict) or profile.get("level") != level or profile.get("usable") is not True:
        _fail("the Apps2Samsung CLI did not return a usable signing profile")

    after = _author_sha(data_dir, level)
    if after is None:
        _fail("the signing profile has no author certificate after prepare; refusing")
    if before is not None and after != before:
        _fail("the author identity changed during prepare; refusing")
    pin_recorded = False
    if pinned is None:
        _write_pin(data_dir, level, after)   # first-seen author: freshly minted, or adopted with --trust
        pin_recorded = True
    elif after != pinned:
        _fail("the author identity does not match the pinned identity after prepare; refusing")

    covered_now = len(profile.get("duids", [])) if isinstance(profile.get("duids"), list) else 0
    return {"ok": True, "verb": "prepare", "level": level, "reused": covered,
            "coveredDuidCount": covered_now, "authorPinRecorded": pin_recorded}


# ---- verb: honest refused acquisition state ----

def acquisition_state() -> dict:
    """The report-acquisition seam, refused, with the exact upstream API gap for the parent."""
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
                                    "native frame",
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

    pv = sub.add_parser("verify", help="verify the dependency bytes and the exe's version")
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
            allow_sign_in=args.allow_samsung_signin, trust_current_author=args.trust_current_author))
    except BootstrapError as exc:
        print("bootstrap refused: " + str(exc), file=sys.stderr)
        return 2
    except (OSError, subprocess.SubprocessError):
        print("bootstrap refused: a local operation failed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

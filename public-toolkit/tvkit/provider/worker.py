"""Isolated child worker for one vendored ``archive_root.probe`` operation.

Run as ``python -I -B worker.py`` by :mod:`tvkit.provider.channel`. It reads one
strict JSON request from stdin, loads the vendored package by absolute path,
patches only this process's module globals, performs exactly one probe, and
writes one sanitized JSON result to stdout.

The child never modifies the parent's environment or ``sys.path``. It returns
no target address, path, credential, secret or vendor error text. The upstream
probe still does its own staging and verifies ``/etc/passwd`` and
``/usr/share/sdbd/public.pem`` before it reports success.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True

_MAX_REQUEST_BYTES = 64 * 1024
_MAX_RESULT_COMMANDS = 128
_MAX_COMMAND_OUTPUT_BYTES = 1024 * 1024
_MAX_GROUP_OUTPUT_BYTES = 64 * 1024
_SHELL_INJECTION_DELAY_SECONDS = 6.0
_SHELL_INJECTION_MINIMUM_DELTA = 4.0
_SDK_INLINE_LIMIT_BYTES = 300
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_GROUP_RE = re.compile(r"[0-9]{1,10}\Z")
_EXIT_TRAILER_RE = re.compile(r"\n\[exit:([0-9]{1,3})\]\n?\Z")


def _verify(path: str, expected: str, max_bytes: int) -> Path:
    if type(path) is not str or type(expected) is not str or not _SHA256.fullmatch(expected):
        raise RuntimeError("artifact request is malformed")
    target = Path(path)
    info = os.lstat(target)
    if info.st_size > max_bytes:
        raise RuntimeError("artifact is too large")
    data = target.read_bytes()
    if len(data) > max_bytes:
        raise RuntimeError("artifact is too large")
    if hashlib.sha256(data).hexdigest() != expected:
        raise RuntimeError("artifact hash mismatch")
    return target


def _load_vendor(vendor_directory: str):
    if type(vendor_directory) is not str:
        raise RuntimeError("vendor directory is malformed")
    root = Path(vendor_directory)
    init = root / "__init__.py"
    if not init.is_file():
        raise RuntimeError("vendor package is missing")
    name = "samsung_tv_root"
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(
        name, init, submodule_search_locations=[str(root)])
    if spec is None or spec.loader is None:
        raise RuntimeError("vendor package could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _runtime_fallback(archive, module_sdb) -> None:
    """Install the local root_compat behaviour once, in this child only.

    The reference TV rejects ``dotnet --list-runtimes`` but has a verified
    Microsoft.NETCore.App shared runtime. Only the 6.x runtime is accepted here;
    the provider rejects 3.1 before staging. The upstream inline-injection
    ceiling is lowered to the hardware-tested 300 bytes. No vendored file is
    edited.
    """
    if getattr(archive._capture, "_quiettv_runtime_fallback", False):
        return
    module_sdb.SDB_APPINSTALL_MAX_SAFE_BYTES = _SDK_INLINE_LIMIT_BYTES
    original = archive._capture

    def capture(client, command, timeout=15.0):
        try:
            return original(client, command, timeout)
        except archive.ArchiveRootError as error:
            if (command != "/usr/bin/dotnet --list-runtimes"
                    or "Unknown option --list-runtimes" not in str(error)):
                raise
        base = "/usr/share/dotnet/shared/Microsoft.NETCore.App/"
        listing = original(client, "ls -d " + base + "*", timeout)
        versions = []
        for line in listing.splitlines():
            match = re.fullmatch(re.escape(base) + r"(\d+\.\d+\.\d+)", line.strip())
            if not match:
                continue
            version = match.group(1)
            major, _, _ = map(int, version.split("."))
            if major != 6:
                continue
            path = base + version
            check = ("cd " + path + "; test -s libcoreclr.so && "
                     "test -s System.Private.CoreLib.dll && echo verified")
            if original(client, check, timeout).strip() != "verified":
                raise archive.ArchiveRootError("Runtime files were not verified")
            versions.append("Microsoft.NETCore.App " + version + " [" + base.rstrip("/") + "]")
        if not versions:
            raise archive.ArchiveRootError("No supported runtime with verified core files")
        return "\n".join(versions)

    capture._quiettv_runtime_fallback = True
    archive._capture = capture


def _explicit_openssl(archive, executable: str, expected: str):
    def openssl(*arguments: str) -> None:
        _verify(executable, expected, 64 * 1024 * 1024)
        try:
            result = subprocess.run(
                (executable, *arguments), capture_output=True, text=True,
                timeout=60, check=False)
        except (OSError, subprocess.SubprocessError):
            raise archive.ArchiveRootError("the local signing helper failed") from None
        if result.returncode != 0:
            raise archive.ArchiveRootError("the local signing helper failed")
    return openssl


def _sanitize(evidence: dict) -> dict:
    commands = evidence.get("commands") or []
    if not isinstance(commands, list) or len(commands) > _MAX_RESULT_COMMANDS:
        raise RuntimeError("probe returned a malformed command list")
    cleaned = []
    for item in commands:
        if not isinstance(item, dict):
            raise RuntimeError("probe returned a malformed command result")
        exit_code = item.get("exit_code")
        timed_out = item.get("timed_out")
        stdout = item.get("stdout")
        if type(exit_code) is not int or type(timed_out) is not bool or type(stdout) is not str:
            raise RuntimeError("probe returned a malformed command result")
        encoded = stdout.encode("utf-8")
        if len(encoded) > _MAX_COMMAND_OUTPUT_BYTES:
            raise RuntimeError("probe command output exceeded its bound")
        cleaned.append({"exit_code": exit_code, "timed_out": timed_out, "stdout": stdout})
    result = {
        "ok": True,
        "authenticated": evidence.get("authenticated") is True,
        "uid": evidence.get("uid"),
        "euid": evidence.get("euid"),
        "gid": evidence.get("gid"),
        "egid": evidence.get("egid"),
        "runtime": evidence.get("runtime"),
        "system_files_unchanged": evidence.get("system_files_unchanged") is True,
        "staging_cleaned": evidence.get("staging_cleaned") is True,
        "commands": cleaned,
    }
    for key in ("uid", "euid", "gid", "egid"):
        if type(result[key]) is not int:
            raise RuntimeError("probe returned a malformed root identity")
    return result


def _parse_group_capture(output: str) -> int:
    """Parse the vendored capture's command output plus its terminal exit marker."""
    if type(output) is not str or len(output.encode("utf-8")) > _MAX_GROUP_OUTPUT_BYTES:
        raise RuntimeError("the SDK group read was too large")
    match = _EXIT_TRAILER_RE.search(output)
    if match is None or int(match.group(1)) != 0:
        raise RuntimeError("the SDK group read did not exit cleanly")
    body = output[:match.start()]
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if len(lines) != 1 or not _GROUP_RE.fullmatch(lines[0]):
        raise RuntimeError("the SDK group read was malformed")
    value = int(lines[0])
    if not 0 <= value <= 2**31 - 1:
        raise RuntimeError("the SDK group read was malformed")
    return value


def _sdk_group(request: dict, module_sdb, sdk: Path) -> dict:
    """One fixed SDK capture callback for ``id -g``; never root and never a write."""
    target = request.get("target")
    callback = request.get("callbackHost")
    seconds = request.get("captureSeconds")
    if (type(target) is not str or type(callback) is not str
            or type(seconds) not in (int, float) or not 0 < float(seconds) <= 120):
        raise RuntimeError("the SDK group request is malformed")
    client = module_sdb.SdbClient(sdk, target, timeout=float(seconds))
    result = client.capture("id -g", callback_host=callback, bind_host=callback,
                            timeout=float(seconds))
    # The reference launcher exits 1 for the deliberately-invalid package name
    # even when the callback completed; upstream accepts transport 0 or 1.
    # Success is still decided only by the strict body and terminal [exit:0].
    if result.transport_returncode not in (0, 1):
        raise RuntimeError("the SDK group read did not succeed")
    return {"ok": True, "group": _parse_group_capture(result.output)}


def _require_net6(runtime: object) -> None:
    if runtime != "6.0":
        raise RuntimeError("the target does not offer the supported .NET 6 runtime")


def _apply_sdk_timing(module_sdb) -> None:
    """Apply the measured timing and tested inline limit in this child only."""
    module_sdb.SHELL_INJECTION_DELAY_SECONDS = _SHELL_INJECTION_DELAY_SECONDS
    module_sdb.SHELL_INJECTION_MINIMUM_DELTA = _SHELL_INJECTION_MINIMUM_DELTA
    module_sdb.SDB_APPINSTALL_MAX_SAFE_BYTES = _SDK_INLINE_LIMIT_BYTES


async def _run(request: dict) -> dict:
    operation = request.get("operation")
    vendor = _load_vendor(request["vendorDirectory"])
    module_sdb = importlib.import_module("samsung_tv_root.sdb")
    _apply_sdk_timing(module_sdb)
    sdk = _verify(request["sdkExecutable"], request["sdkSha256"], 256 * 1024 * 1024)

    if operation == "sdk-group":
        return _sdk_group(request, module_sdb, sdk)
    if operation != "root-probe":
        raise RuntimeError("the isolated operation is malformed")

    archive = importlib.import_module("samsung_tv_root.archive_root")
    openssl = _verify(request["opensslExecutable"], request["opensslSha256"],
                      64 * 1024 * 1024)
    payload_directory = Path(request["payloadDirectory"])
    pin = request["payloadSha256"]
    for name in ("SamsungTvArchiveRoot.dll", "SamsungTvRootAgent.dll"):
        if type(pin.get(name)) is not str:
            raise RuntimeError("payload pin is malformed")
        _verify(str(payload_directory / name), pin[name], 64 * 1024 * 1024)

    archive.find_sdb = lambda: sdk
    archive._openssl = _explicit_openssl(archive, str(openssl), request["opensslSha256"])
    _runtime_fallback(archive, module_sdb)

    commands = request["commands"]
    if not isinstance(commands, list) or not 1 <= len(commands) <= _MAX_RESULT_COMMANDS:
        raise RuntimeError("probe command list is malformed")
    if any(type(command) is not str or not command for command in commands):
        raise RuntimeError("probe command list is malformed")

    # Reject 3.1 targets before archive_root selects payloads and stages any
    # net6 executable. This is the same vendored assessment the probe runs;
    # the provider only narrows the accepted runtime and adds no capability.
    preflight = module_sdb.SdbClient(
        sdk, request["target"], timeout=float(request["sdbSeconds"]))
    assessment = await asyncio.to_thread(archive._check_target, preflight)
    _require_net6(assessment.runtime)

    evidence = await archive.probe(
        request["target"],
        callback_host=request["callbackHost"],
        bind_host=request["callbackHost"],
        sdb_timeout=float(request["sdbSeconds"]),
        accept_timeout=float(request["acceptSeconds"]),
        command_timeout=float(request["commandSeconds"]),
        commands=tuple(commands),
        payloads=payload_directory,
    )
    return _sanitize(evidence)


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(_MAX_REQUEST_BYTES + 1)
        if len(raw) > _MAX_REQUEST_BYTES:
            raise RuntimeError("request is too large")
        request = json.loads(raw.decode("utf-8"))
        if not isinstance(request, dict):
            raise RuntimeError("request is not an object")
        result = asyncio.run(_run(request))
    except BaseException:
        # Never print an exception text: it can carry a target, path or token.
        result = {"ok": False, "code": "probe-failed"}
    sys.stdout.write(json.dumps(result, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())

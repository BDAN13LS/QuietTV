"""Strict owner-private provider configuration for the one fixed provider.

The document is ordinary JSON, but it is read with the toolkit's strict loader
(duplicate keys and NaN/Infinity refused) and its shape is checked exactly. It
contains only an owner's private target data and explicit hashes; there are no
defaults, no maintainer literals and no fallback target.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .. import strictjson as sj
from ..paths import is_sha256
from .. import livecollect as _lc

CONFIG_SCHEMA = "quiettv-live-provider/1"
MAX_CONFIG_BYTES = 64 * 1024
MAX_SDK_BYTES = 256 * 1024 * 1024
MAX_DLL_BYTES = 64 * 1024 * 1024
MAX_OPENSSL_BYTES = 64 * 1024 * 1024

# RFC 1918 boundaries, using the same numeric form as the public source guard.
_TARGET_NETS = (
    ipaddress.ip_network((0x0A000000, 8)),
    ipaddress.ip_network((0xAC100000, 12)),
    ipaddress.ip_network((0xC0A80000, 16)),
)
_DUID_RE = re.compile(r"[0-9A-Za-z._:-]{1,128}\Z")
_PLACEHOLDERS = frozenset({"changeme", "change-me", "change_me", "placeholder",
                           "example", "todo", "replace", "your-"})
_SYSTEM_SID = "S-1-5-18"
_ADMINISTRATORS_SID = "S-1-5-32-544"
_SID_RE = re.compile(r"S-1-[0-9]+(?:-[0-9]+)*\Z")
_MAX_ACL_STDOUT = 64 * 1024
_WINDOWS_ACL_SCRIPT = (
    "$ErrorActionPreference='Stop';"
    "$acl=Get-Acl -LiteralPath $env:QTV_CONFIG_PATH;"
    "$owner=$acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value;"
    "$id=[System.Security.Principal.WindowsIdentity]::GetCurrent();"
    "$aces=@($acl.GetAccessRules($true,$true,"
    "[System.Security.Principal.SecurityIdentifier]) | ForEach-Object {"
    "[pscustomobject]@{sid=$_.IdentityReference.Value;"
    "type=$_.AccessControlType.ToString()}});"
    "[pscustomobject]@{ownerSid=$owner;currentUserSid=$id.User.Value;"
    "isAdmin=([System.Security.Principal.WindowsPrincipal]$id).IsInRole("
    "[System.Security.Principal.WindowsBuiltInRole]::Administrator);"
    "aces=$aces} | ConvertTo-Json -Compress -Depth 4")

_TOP_KEYS = ("schema", "target", "callbackHost", "expectedSdkDuid", "sdk",
             "rootPayloads", "openssl", "timeouts")
_SDK_KEYS = ("executable", "sha256")
_ROOT_KEYS = ("archiveRootDll", "rootAgentDll")
_ARTIFACT_KEYS = ("path", "sha256")
_OPENSSL_KEYS = ("executable", "sha256")
_TIMEOUT_KEYS = ("sdkSeconds", "probeSeconds", "commandSeconds",
                 "nativeSeconds", "reportAttempts")


class ProviderConfigError(ValueError):
    """A finite configuration refusal. It never echoes owner values."""


def _fail(message: str) -> None:
    raise ProviderConfigError(message)


def _private_ipv4(value: object, label: str) -> str:
    if type(value) is not str:
        _fail(f"{label} must be an IPv4 address")
    try:
        address = ipaddress.IPv4Address(value)
    except ValueError:
        _fail(f"{label} must be an IPv4 address")
    if (address.is_loopback or address.is_link_local or address.is_multicast
            or address.is_unspecified or address.is_reserved):
        _fail(f"{label} must be a private LAN address")
    if not any(address in network for network in _TARGET_NETS):
        _fail(f"{label} must be a private LAN address")
    return str(address)


def _bounded_artifact(value: object, label: str, name: str, max_bytes: int) -> tuple[Path, str]:
    if not isinstance(value, dict):
        _fail(f"{label} must be an object")
    try:
        item = sj.require_object(value, label, _ARTIFACT_KEYS)
    except sj.StrictJsonError as exc:
        _fail(str(exc))
    path_value = item["path"]
    digest = item["sha256"]
    if type(path_value) is not str or not path_value:
        _fail(f"{label} path must be a string")
    if not is_sha256(digest):
        _fail(f"{label} needs a trusted SHA-256 pin")
    path = _plain_file(path_value, label)
    if path.name != name:
        _fail(f"{label} must be named {name}")
    verify_file(path, digest, label, max_bytes=max_bytes)
    return path, digest


def _plain_file(value: str, label: str) -> Path:
    try:
        path = _lc._validate_local_path(value, label)
    except Exception:
        _fail(f"{label} must be an explicit absolute local file")
    try:
        _lc._outside(path, must_not_exist=False)
    except Exception:
        _fail(f"{label} must live outside every repository")
    return path


def _owner_private(path: Path) -> None:
    try:
        info = os.lstat(path)
    except (OSError, ValueError):
        _fail("the provider configuration could not be inspected")
    if _lc._is_link_or_reparse(info) or not stat.S_ISREG(info.st_mode):
        _fail("the provider configuration must be a plain regular file")
    if os.name != "nt":
        if stat.S_IMODE(info.st_mode) & 0o077:
            _fail("the provider configuration must be owner-only (mode 0600)")
        return
    _owner_private_windows(path)


def _owner_private_windows(path: Path) -> None:
    """Validate owner and allow ACEs by SID, never by localised account names."""
    root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
    powershell = Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    # This child must not inherit an outer PowerShell 7 module path into
    # Windows PowerShell 5.1: Get-Acl can then find its module but fail to
    # autoload it. The parent environment is never modified.
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() != "PSMODULEPATH"}
    environment["QTV_CONFIG_PATH"] = str(path)
    try:
        result = subprocess.run(
            (str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", _WINDOWS_ACL_SCRIPT),
            capture_output=True, text=True, timeout=20, check=False,
            env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        _fail("the provider configuration permissions could not be checked")
    if result.returncode != 0 or type(result.stdout) is not str:
        _fail("the provider configuration permissions could not be checked")
    try:
        raw = result.stdout.encode("utf-8")
    except UnicodeError:
        _fail("the provider configuration permissions could not be checked")
    if len(raw) > _MAX_ACL_STDOUT:
        _fail("the provider configuration permissions could not be checked")
    try:
        document = json.loads(result.stdout)
    except (ValueError, TypeError):
        _fail("the provider configuration permissions could not be checked")
    if not isinstance(document, dict) or set(document) != {
            "ownerSid", "currentUserSid", "isAdmin", "aces"}:
        _fail("the provider configuration permissions could not be parsed")
    owner = _acl_sid(document["ownerSid"])
    current = _acl_sid(document["currentUserSid"])
    is_admin = document["isAdmin"]
    aces = document["aces"]
    if type(is_admin) is not bool or not isinstance(aces, list) or len(aces) > 64:
        _fail("the provider configuration permissions could not be parsed")
    allowed = {owner, _SYSTEM_SID, _ADMINISTRATORS_SID}
    access = owner == current
    for ace in aces:
        if (not isinstance(ace, dict) or set(ace) != {"sid", "type"}
                or type(ace["type"]) is not str
                or ace["type"] not in ("Allow", "Deny")):
            _fail("the provider configuration permissions could not be parsed")
        sid = _acl_sid(ace["sid"])
        if ace["type"] == "Allow":
            if sid not in allowed:
                _fail("the provider configuration must be owner-private")
            if sid == current or (is_admin and sid == _ADMINISTRATORS_SID):
                access = True
        elif sid == current:
            _fail("the current user is denied access to the provider configuration")
    if not access:
        _fail("the current user must own or be allowed to read the provider configuration")


def _acl_sid(value: object) -> str:
    if type(value) is not str or not _SID_RE.fullmatch(value):
        _fail("the provider configuration permissions could not be parsed")
    return value


def verify_file(path: Path, expected: str, label: str, *, max_bytes: int) -> bytes:
    """Read one plain file and match its exact SHA-256 pin. Used again at every use."""
    try:
        data = _lc._read_plain_file(path, max_bytes, label)
    except Exception:
        _fail(f"{label} could not be read")
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        _fail(f"{label} does not match its trusted SHA-256 pin; refusing")
    return data


@dataclass(frozen=True)
class ProviderConfig:
    target: str
    callback_host: str
    expected_sdk_duid: str
    sdk_executable: Path
    sdk_sha256: str
    archive_root_dll: Path
    archive_root_sha256: str
    root_agent_dll: Path
    root_agent_sha256: str
    openssl_executable: Path
    openssl_sha256: str
    sdk_seconds: float
    probe_seconds: float
    command_seconds: float
    native_seconds: float
    report_attempts: int


def _timeout(value: object, label: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        _fail(f"{label} is out of range")
    return value


def load_config(path_value: str) -> ProviderConfig:
    """Validate the owner file and every pinned artifact before any device work."""
    if type(path_value) is not str or not path_value:
        _fail("an explicit --provider-config file is required")
    path = _plain_file(path_value, "--provider-config")
    _owner_private(path)
    try:
        raw = _lc._read_plain_file(path, MAX_CONFIG_BYTES, "the provider configuration")
    except Exception:
        _fail("the provider configuration could not be read")
    try:
        document = sj.loads(raw)
    except sj.StrictJsonError:
        _fail("the provider configuration is not strict JSON")
    try:
        top = sj.require_object(document, "provider configuration", _TOP_KEYS)
    except sj.StrictJsonError as exc:
        _fail(str(exc))
    if top["schema"] != CONFIG_SCHEMA:
        _fail(f"provider configuration schema must be {CONFIG_SCHEMA}")

    target = _private_ipv4(top["target"], "target")
    callback = _private_ipv4(top["callbackHost"], "callbackHost")
    duid = top["expectedSdkDuid"]
    if type(duid) is not str or not _DUID_RE.fullmatch(duid) or duid.lower() in _PLACEHOLDERS:
        _fail("expectedSdkDuid must be the explicit private SDK DUID pin")

    try:
        sdk = sj.require_object(top["sdk"], "sdk", _SDK_KEYS)
    except sj.StrictJsonError as exc:
        _fail(str(exc))
    if type(sdk["executable"]) is not str or not sdk["executable"]:
        _fail("sdk.executable must be a string")
    if not is_sha256(sdk["sha256"]):
        _fail("sdk.sha256 must be a SHA-256 pin")
    sdk_path = _plain_file(sdk["executable"], "sdk.executable")
    if sdk_path.name.lower() not in ("sdb", "sdb.exe"):
        _fail("sdk.executable must be the Samsung sdb program")
    verify_file(sdk_path, sdk["sha256"], "the SDK executable", max_bytes=MAX_SDK_BYTES)

    try:
        root = sj.require_object(top["rootPayloads"], "rootPayloads", _ROOT_KEYS)
    except sj.StrictJsonError as exc:
        _fail(str(exc))
    archive_path, archive_sha = _bounded_artifact(
        root["archiveRootDll"], "rootPayloads.archiveRootDll",
        "SamsungTvArchiveRoot.dll", MAX_DLL_BYTES)
    agent_path, agent_sha = _bounded_artifact(
        root["rootAgentDll"], "rootPayloads.rootAgentDll",
        "SamsungTvRootAgent.dll", MAX_DLL_BYTES)

    try:
        openssl = sj.require_object(top["openssl"], "openssl", _OPENSSL_KEYS)
    except sj.StrictJsonError as exc:
        _fail(str(exc))
    if type(openssl["executable"]) is not str or not openssl["executable"]:
        _fail("openssl.executable must be a string")
    if not is_sha256(openssl["sha256"]):
        _fail("openssl.sha256 must be a SHA-256 pin")
    openssl_path = _plain_file(openssl["executable"], "openssl.executable")
    if openssl_path.name.lower() not in ("openssl", "openssl.exe"):
        _fail("openssl.executable must be the OpenSSL program")
    verify_file(openssl_path, openssl["sha256"], "the OpenSSL executable",
                max_bytes=MAX_OPENSSL_BYTES)

    try:
        timeouts = sj.require_object(top["timeouts"], "timeouts", _TIMEOUT_KEYS)
    except sj.StrictJsonError as exc:
        _fail(str(exc))
    sdk_seconds = _timeout(timeouts["sdkSeconds"], "timeouts.sdkSeconds", 5, 120)
    probe_seconds = _timeout(timeouts["probeSeconds"], "timeouts.probeSeconds", 10, 600)
    command_seconds = _timeout(timeouts["commandSeconds"], "timeouts.commandSeconds", 5, 300)
    native_seconds = _timeout(timeouts["nativeSeconds"], "timeouts.nativeSeconds", 10, 600)
    report_attempts = _timeout(timeouts["reportAttempts"], "timeouts.reportAttempts", 1, 5)
    if probe_seconds <= max(command_seconds, native_seconds):
        _fail("timeouts.probeSeconds must exceed commandSeconds and nativeSeconds")

    return ProviderConfig(
        target=target, callback_host=callback, expected_sdk_duid=duid,
        sdk_executable=sdk_path, sdk_sha256=sdk["sha256"],
        archive_root_dll=archive_path, archive_root_sha256=archive_sha,
        root_agent_dll=agent_path, root_agent_sha256=agent_sha,
        openssl_executable=openssl_path, openssl_sha256=openssl["sha256"],
        sdk_seconds=float(sdk_seconds), probe_seconds=float(probe_seconds),
        command_seconds=float(command_seconds), native_seconds=float(native_seconds),
        report_attempts=report_attempts,
    )


def revalidate(config: ProviderConfig) -> None:
    """Re-check every pinned executable and DLL immediately before device use."""
    verify_file(config.sdk_executable, config.sdk_sha256, "the SDK executable",
                max_bytes=MAX_SDK_BYTES)
    verify_file(config.archive_root_dll, config.archive_root_sha256,
                "the archive-root payload", max_bytes=MAX_DLL_BYTES)
    verify_file(config.root_agent_dll, config.root_agent_sha256,
                "the root-agent payload", max_bytes=MAX_DLL_BYTES)
    verify_file(config.openssl_executable, config.openssl_sha256,
                "the OpenSSL executable", max_bytes=MAX_OPENSSL_BYTES)

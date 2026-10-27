"""Owner-local private configuration. There are no defaults for device identity."""
from __future__ import annotations

import ipaddress
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .profiles import PROFILES, DeviceProfile

SCHEMA = "quiettv-mcp-config/1"
CONFIG_ENV = "QUIETTV_MCP_CONFIG"
MAX_CONFIG_BYTES = 64 * 1024
_MAC = re.compile(r"^[0-9A-F]{2}(?::[0-9A-F]{2}){5}$")
_TOP_KEYS = {"schema", "profile", "tv", "control_key_file", "lan_prefix", "capture_dir", "allowed_apps", "capture_max_age_s"}
_TV_KEYS = {"ip", "mac", "model", "firmware"}


class ConfigError(ValueError):
    """The owner configuration is missing or invalid. Messages never carry key material."""


@dataclass(frozen=True)
class OwnerConfig:
    profile: DeviceProfile
    tv_ip: ipaddress.IPv4Address
    tv_mac: str
    control_key_file: Path
    lan_prefix: int = 24
    capture_dir: Path | None = None
    allowed_apps: tuple[str, ...] = field(default_factory=tuple)
    capture_max_age_s: int = 120
    # Set only by in-process test harnesses; never by a file, flag or environment variable.
    fixture_mode: bool = False

    @property
    def host(self) -> str:
        return str(self.tv_ip)


def default_config_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        return base / "QuietTV-MCP" / "config.json"
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "quiettv-mcp" / "config.json"


def resolve_config_path(cli_path: str | None) -> Path:
    if cli_path:
        return Path(cli_path).expanduser()
    if os.environ.get(CONFIG_ENV):
        return Path(os.environ[CONFIG_ENV]).expanduser()
    return default_config_path()


def normalize_mac(value: object) -> str:
    if not isinstance(value, str):
        raise ConfigError("tv.mac must be a string like 02:00:00:00:00:01")
    mac = value.strip().upper().replace("-", ":")
    if not _MAC.match(mac):
        raise ConfigError("tv.mac must be six colon-separated hex pairs")
    first = int(mac[:2], 16)
    if mac in ("00:00:00:00:00:00", "FF:FF:FF:FF:FF:FF") or first & 1:
        raise ConfigError("tv.mac must be a unicast device address")
    return mac


def parse_tv_ip(value: object, *, allow_loopback_fixture: bool = False) -> ipaddress.IPv4Address:
    if not isinstance(value, str):
        raise ConfigError("tv.ip must be a literal IPv4 address")
    try:
        ip = ipaddress.IPv4Address(value.strip())
    except ipaddress.AddressValueError:
        raise ConfigError("tv.ip must be a literal IPv4 address; host names are not accepted") from None
    if ip.is_loopback:
        if allow_loopback_fixture:
            return ip
        raise ConfigError("tv.ip must be the TV's LAN address, not loopback")
    if ip.is_multicast or ip.is_unspecified or ip.is_link_local or ip.is_reserved or not ip.is_private:
        raise ConfigError("tv.ip must be a private LAN address (10/8, 172.16/12 or 192.168/16)")
    return ip


def _absolute_path(value: object, name: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} must be an absolute path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ConfigError(f"{name} must be an absolute path")
    return path


def validate_config(data: Mapping[str, Any], *, allow_loopback_fixture: bool = False) -> OwnerConfig:
    if not isinstance(data, Mapping):
        raise ConfigError("configuration must be a JSON object")
    unknown = set(data) - _TOP_KEYS
    if unknown:
        raise ConfigError("unknown configuration keys: " + ", ".join(sorted(unknown)))
    if data.get("schema") != SCHEMA:
        raise ConfigError(f"schema must be {SCHEMA!r}")
    profile = PROFILES.get(data.get("profile"))  # type: ignore[arg-type]
    if profile is None:
        raise ConfigError("unsupported profile; supported: " + ", ".join(PROFILES))
    tv = data.get("tv")
    if not isinstance(tv, Mapping):
        raise ConfigError("tv must be an object with ip, mac, model and firmware")
    if set(tv) != _TV_KEYS:
        raise ConfigError("tv must contain exactly: ip, mac, model, firmware")
    if tv["model"] != profile.model:
        raise ConfigError(f"tv.model does not match profile {profile.profile_id}")
    if profile.firmware_status(tv["firmware"]) != "match":
        raise ConfigError(f"tv.firmware does not match profile {profile.profile_id}")
    ip = parse_tv_ip(tv["ip"], allow_loopback_fixture=allow_loopback_fixture)
    mac = normalize_mac(tv["mac"])
    key_file = _absolute_path(data.get("control_key_file"), "control_key_file")

    prefix = data.get("lan_prefix", 24)
    if not isinstance(prefix, int) or isinstance(prefix, bool) or not 16 <= prefix <= 30:
        raise ConfigError("lan_prefix must be an integer from 16 to 30")
    capture_dir = None
    if data.get("capture_dir") is not None:
        capture_dir = _absolute_path(data["capture_dir"], "capture_dir")
    apps = data.get("allowed_apps", list(profile.apps))
    if not isinstance(apps, list) or not apps or any(a not in profile.apps for a in apps):
        raise ConfigError("allowed_apps must be a non-empty subset of the profile's app IDs")
    max_age = data.get("capture_max_age_s", 120)
    if not isinstance(max_age, int) or isinstance(max_age, bool) or not 5 <= max_age <= 600:
        raise ConfigError("capture_max_age_s must be an integer from 5 to 600")
    return OwnerConfig(
        profile=profile,
        tv_ip=ip,
        tv_mac=mac,
        control_key_file=key_file,
        lan_prefix=prefix,
        capture_dir=capture_dir,
        allowed_apps=tuple(dict.fromkeys(apps)),
        capture_max_age_s=max_age,
        fixture_mode=bool(allow_loopback_fixture and ip.is_loopback),
    )


def load_config(path: Path, *, allow_loopback_fixture: bool = False) -> OwnerConfig:
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_CONFIG_BYTES + 1)
    except FileNotFoundError:
        raise ConfigError(f"no configuration at {path}; run `quiettv-mcp setup`") from None
    except OSError as exc:
        raise ConfigError(f"cannot read configuration: {exc.strerror}") from None
    if len(raw) > MAX_CONFIG_BYTES:
        raise ConfigError("configuration file is too large")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ConfigError("configuration is not valid UTF-8 JSON") from None
    return validate_config(data, allow_loopback_fixture=allow_loopback_fixture)


def config_to_dict(config: OwnerConfig) -> dict[str, Any]:
    data: dict[str, Any] = {
        "schema": SCHEMA,
        "profile": config.profile.profile_id,
        "tv": {
            "ip": str(config.tv_ip),
            "mac": config.tv_mac,
            "model": config.profile.model,
            "firmware": config.profile.firmware,
        },
        "control_key_file": str(config.control_key_file),
        "lan_prefix": config.lan_prefix,
        "allowed_apps": list(config.allowed_apps),
        "capture_max_age_s": config.capture_max_age_s,
    }
    if config.capture_dir is not None:
        data["capture_dir"] = str(config.capture_dir)
    return data

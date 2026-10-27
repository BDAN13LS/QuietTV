"""User configuration. The shipped template names the public project repository
(``PROJECT_REPO_URL``); every owner-specific field is a ``<CONFIGURE: ...>``
placeholder, and any command that needs one fails closed until it is set."""

from __future__ import annotations

import urllib.parse
from pathlib import Path
from typing import Any, Mapping

from . import strictjson as sj
from .profile import is_placeholder

CONFIG_SCHEMA = "tvkit-config/1"
CONFIG_FIELDS = ("schema", "profileId", "releaseRepoUrl", "capsuleStore", "transport")
PROJECT_REPO_URL = "https://github.com/BDAN13LS/QuietTV"
TEMPLATE = Path(__file__).resolve().parent.parent / "config" / "tvkit.config.example.json"


class ConfigError(ValueError):
    pass


def load_config(path: str | Path) -> dict[str, Any]:
    try:
        data = sj.require_object(sj.load_file(path, 64 * 1024), "config", CONFIG_FIELDS)
    except sj.StrictJsonError as exc:
        raise ConfigError(str(exc)) from exc
    if data["schema"] != CONFIG_SCHEMA:
        raise ConfigError(f"config schema must be {CONFIG_SCHEMA}")
    return dict(data)


def unconfigured(config: Mapping[str, Any]) -> list[str]:
    return [key for key in CONFIG_FIELDS if is_placeholder(config.get(key))]


def require(config: Mapping[str, Any], key: str) -> str:
    value = config.get(key)
    if type(value) is not str or value == "" or is_placeholder(value):
        raise ConfigError(f"config {key} is not configured yet; set it in your own config file")
    if key == "releaseRepoUrl":
        parts = urllib.parse.urlsplit(value)
        if parts.scheme != "https" or parts.hostname != "github.com" or parts.username or parts.query or parts.fragment:
            raise ConfigError("releaseRepoUrl must be a plain https://github.com/OWNER/REPO address")
    return value

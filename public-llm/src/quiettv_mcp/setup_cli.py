"""Guided setup, doctor and MCP client-config fragments.

Secrets are never taken as command-line arguments: the control key is read
from a private file, or typed at a hidden prompt and written to one.
"""
from __future__ import annotations

import getpass
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any, Callable, TextIO

from .config import (SCHEMA, ConfigError, OwnerConfig, load_config, normalize_mac, parse_tv_ip, validate_config)
from .device import DeviceError, QuietTV
from .profiles import PROFILES

InputFn = Callable[[str], str]
SERVER_NAME = "quiettv"
CLIENTS = ("claude-desktop", "codex", "opencode")


def _write_private(path: Path, data: bytes, *, overwrite: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_BINARY", 0) | (os.O_TRUNC if overwrite else os.O_EXCL)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    if os.name == "posix":
        os.chmod(path, 0o600)


def _ask(prompt: str, input_fn: InputFn, check: Callable[[str], Any], out: TextIO, attempts: int = 3) -> Any:
    for _ in range(attempts):
        try:
            return check(input_fn(prompt).strip())
        except ConfigError as exc:
            print(f"  {exc}", file=out)
    raise ConfigError("too many invalid answers; nothing was written")


def run_setup(config_path: Path, *, input_fn: InputFn = input, secret_fn: InputFn = getpass.getpass,
              out: TextIO = sys.stdout) -> OwnerConfig:
    print("Quiet TV MCP setup. Values are stored only in your private config:", config_path, file=out)
    if config_path.exists():
        if input_fn("A config already exists. Replace it? [y/N] ").strip().lower() != "y":
            raise ConfigError("kept the existing configuration; nothing was written")
    profiles = list(PROFILES.values())
    print("Supported profiles (others are rejected):", file=out)
    for p in profiles:
        print(f"  {p.profile_id}: {p.model}, firmware {p.firmware}, {p.os} {p.os_version}, {p.arch}", file=out)
    profile = profiles[0]

    def check_model(value: str) -> str:
        if value.upper() != profile.model:
            raise ConfigError(f"model {value!r} is not supported (expected {profile.model})")
        return profile.model

    def check_firmware(value: str) -> str:
        if profile.firmware_status(value) != "match":
            raise ConfigError(f"firmware {value!r} is not supported (expected {profile.firmware})")
        return profile.firmware

    model = _ask("TV model code (Settings > Support > About This TV): ", input_fn, check_model, out)
    firmware = _ask("TV software version (same screen): ", input_fn, check_firmware, out)
    ip = _ask("TV IP address on your LAN: ", input_fn, lambda v: str(parse_tv_ip(v)), out)
    mac = _ask("TV MAC address (Settings > Network > Network Status): ", input_fn, normalize_mac, out)

    key_path_text = input_fn("Path to your existing control key file (leave empty to type the key now): ").strip()
    if key_path_text:
        key_path = Path(key_path_text).expanduser()
        if not key_path.is_absolute():
            raise ConfigError("control key file path must be absolute; nothing was written")
    else:
        key = secret_fn("Control key (hidden, not stored in shell history): ").strip()
        if not (16 <= len(key) <= 256 and key.isascii() and key.isprintable() and " " not in key):
            raise ConfigError("the key must be 16-256 printable ASCII characters without spaces; nothing was written")
        key_path = config_path.parent / "control.key"
        try:
            _write_private(key_path, (key + "\n").encode("ascii"), overwrite=False)
        except FileExistsError:
            raise ConfigError(f"{key_path} already exists; point setup at it instead") from None
        print(f"  Key written to {key_path} (owner-only permissions).", file=out)

    captures = input_fn("Folder for private screenshots (leave empty to keep captures in memory only): ").strip()
    data: dict[str, Any] = {
        "schema": SCHEMA,
        "profile": profile.profile_id,
        "tv": {"ip": ip, "mac": mac, "model": model, "firmware": firmware},
        "control_key_file": str(key_path.resolve() if key_path.exists() else key_path),
        "lan_prefix": 24,
        "allowed_apps": list(profile.apps),
        "capture_max_age_s": 120,
    }
    if captures:
        data["capture_dir"] = str(Path(captures).expanduser())
    config = validate_config(data)
    _write_private(config_path, (json.dumps(data, indent=2) + "\n").encode("utf-8"), overwrite=True)
    print(f"Saved {config_path}.", file=out)
    print("Next: `quiettv-mcp doctor` then `quiettv-mcp client-config --client all`.", file=out)
    return config


def _inside_git_tree(path: Path) -> bool:
    for parent in [path, *path.parents]:
        if (parent / ".git").exists():
            return True
    return False


def run_doctor(config_path: Path, *, online: bool = False, out: TextIO = sys.stdout,
               device_factory: Callable[[OwnerConfig], QuietTV] = QuietTV) -> int:
    failures = 0

    def report(ok: bool | None, label: str, detail: str = "") -> None:
        nonlocal failures
        mark = {True: "PASS", False: "FAIL", None: "WARN"}[ok]
        failures += ok is False
        print(f"[{mark}] {label}" + (f": {detail}" if detail else ""), file=out)

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        report(False, "config", str(exc))
        return 1
    report(True, "config", f"profile {config.profile.profile_id}")
    tv = device_factory(config)
    try:
        tv.read_key()
        report(True, "control key file", "present and well-formed (value not shown)")
    except DeviceError as exc:
        report(False, "control key file", str(exc))
    key_path = config.control_key_file
    if key_path.exists():
        if os.name == "posix" and stat.S_IMODE(key_path.stat().st_mode) & 0o077:
            report(None, "control key permissions", "readable by other users; run chmod 600 on it")
        if _inside_git_tree(key_path.resolve().parent):
            report(None, "control key location", "inside a git working tree; move it somewhere never committed")
    if config.capture_dir and _inside_git_tree(config.capture_dir):
        report(None, "capture folder", "inside a git working tree; screenshots are private output")
    try:
        tv.check_lan()
        report(True, "same LAN", f"TV is inside this machine's /{config.lan_prefix} network")
    except DeviceError as exc:
        report(False, "same LAN", str(exc))
    if not online:
        report(None, "device checks", "skipped (run with --online to check identity and service status, read-only)")
        return 1 if failures else 0
    try:
        identity = tv.verify_identity()
        report(True, "identity", f"model and MAC match; firmware {identity.firmware}")
        status = tv.status()["status"]
        report(True, "TV service", f"authenticated; ready={status.get('ready')}")
        caps = status.get("service_capabilities")
        report(None if caps is None else True, "capture capability",
               "not advertised by the service" if caps is None else ("advertised" if "capture" in caps else "not available"))
    except DeviceError as exc:
        report(False, "device", str(exc))
    return 1 if failures else 0


def _protected_dirs() -> list[Path]:
    home = Path.home()
    dirs = [home / ".codex", home / ".claude", home / ".config" / "opencode", home / ".config" / "Claude",
            home / "Library" / "Application Support" / "Claude"]
    if os.environ.get("APPDATA"):
        dirs.append(Path(os.environ["APPDATA"]) / "Claude")
    return [d.resolve() for d in dirs]


def server_command(config_path: Path) -> tuple[str, list[str]]:
    return sys.executable, ["-m", "quiettv_mcp", "serve", "--config", str(config_path)]


def client_fragments(config_path: Path, command: tuple[str, list[str]] | None = None) -> dict[str, str]:
    exe, args = command or server_command(config_path)
    claude = {"mcpServers": {SERVER_NAME: {"command": exe, "args": args}}}
    toml_args = ", ".join(json.dumps(a) for a in args)
    codex = f"[mcp_servers.{SERVER_NAME}]\ncommand = {json.dumps(exe)}\nargs = [{toml_args}]\n"
    opencode = {"$schema": "https://opencode.ai/config.json",
                "mcp": {SERVER_NAME: {"type": "local", "command": [exe, *args], "enabled": True}}}
    return {
        "claude-desktop": json.dumps(claude, indent=2) + "\n",
        "codex": codex,
        "opencode": json.dumps(opencode, indent=2) + "\n",
    }


FRAGMENT_FILES = {"claude-desktop": "claude_desktop.mcp-fragment.json", "codex": "codex.mcp-fragment.toml",
                  "opencode": "opencode.mcp-fragment.json"}


def write_client_fragments(config_path: Path, clients: list[str], output_dir: Path | None, out: TextIO) -> list[Path]:
    fragments = client_fragments(config_path)
    if output_dir is None:
        for client in clients:
            print(f"# {client}: merge this into the client's MCP settings yourself", file=out)
            print(fragments[client], file=out)
        return []
    target_dir = output_dir.expanduser().resolve()
    for protected in _protected_dirs():
        if target_dir == protected or protected in target_dir.parents:
            raise ConfigError(f"refusing to write into a client's global settings folder ({protected}); "
                              "fragments are written elsewhere and merged by you")
    written: list[Path] = []
    for client in clients:
        path = target_dir / FRAGMENT_FILES[client]
        try:
            _write_private(path, fragments[client].encode("utf-8"), overwrite=False)
        except FileExistsError:
            raise ConfigError(f"{path} already exists; nothing overwritten") from None
        written.append(path)
        print(f"wrote {path}", file=out)
    return written

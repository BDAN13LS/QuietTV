"""QuietTV owner-init (D5b, PREVIEW ONLY): owner-local launcher data from explicit inputs.

Writes one new owner directory OUTSIDE every source/release repository:
    host-config.json   quiettv-host-config/1, the exact launcher/host/HostConfig.cs contract
    apps.json          installed, enabled, launchable apps only (IPTV hidden)
    settings.json      Order from the inventory, no favourites, Wallpaper 0
    widget-config.json quiettv-widget-config/1 for helper/build_widget.py
    owner-state.json   profile id + device-id HASH; preview true, consumerApplyEligible false
    backend-token      only with --backend-port (the future loopback service); never logged

Nothing here talks to a TV, installs, roots, changes Home/startup or signs anything.
A matching tvkit-profile/1 makes preview data only; it never makes a TV eligible for
consumer apply.

API:
    generate(owner_dir, *, profile_id, device_facts, inventory, package_id, owner_uid,
             input_paths, runtime_root, app_name="QuietTV", home_key_code=63,
             backend_port=None, token_factory=None) -> dict (paths + deviceIdSha256 only)
    validate_host_config(obj) -> obj        narrow mirror of HostConfig.Parse
    validate_apps(list, runtime_root)       mirror of OwnerData.ParseApps
    validate_settings(obj, apps)            mirror of OwnerData.ParseSettings
    validate_widget_config(obj) -> obj
    check_outside_repos(path, must_not_exist=True) -> Path   shared with build_widget
CLI: python helper/owner_init.py --help
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True  # importing tvkit must never create files under public-toolkit/

import argparse
import json
import os
import re
import secrets
import subprocess
from pathlib import Path
from typing import Any, Callable, Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "public-toolkit"))
from tvkit import profile as tvprofile  # noqa: E402
from tvkit import strictjson as sj  # noqa: E402


class OwnerInitError(ValueError):
    pass


# ---- exact HostConfig.cs contract (tests/test_owner_init.py checks these against the C# source) ----
HOST_SCHEMA = "quiettv-host-config/1"
ROOT_PREFIXES = ("/home/owner/share/", "/opt/usr/home/owner/share/")
HOST_KNOWN = ("schema", "runtimeRoot", "ownerUid", "ownAppId", "homeKeyCode", "observedInputPaths",
              "remoteDeviceNames", "remoteGrabEnabled", "iptvEnabled", "webUiEnabled", "snapshotEnabled")
PLACEHOLDERS = ("changeme", "change_me", "change-me", "replace", "placeholder", "todo", "example",
                "xxxx", "your-", "your_")
PLACEHOLDER_CHARS = "<>{}$%"
OWN_APP_ID_RE = r"[A-Za-z0-9]{10}\.[A-Za-z][A-Za-z0-9_]{0,49}"
INPUT_PATH_RE = r"/dev/input/event[0-9]{1,3}"
ROOT_SEGMENT_RE = r"[A-Za-z0-9._-]+"
ROOT_LEAF_RE = r"quiet-tv(-[a-z0-9]{1,24})?"
APP_ID_RE = r"[A-Za-z0-9_.-]{1,160}"
# frontend runtime-catalog.mjs (window.QuietTVApps) is stricter: alphanumeric first character, at most 128
RUNTIME_ID_RE = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
RUNTIME_SCHEMA = "quiettv-widget/1"
OWNER_UID_RANGE = (5000, 5999)
HOME_KEY_RANGE = (1, 0x2FF)
MAX_INPUT_PATHS = 8
MAX_APPS = 200
SYSTEM_LAUNCH_IDS = ("com.samsung.tv.quicksetting", "com.samsung.tv.store")
IPTV_APP_ID = "quiet.iptv"

# ---- owner-init's own contract ----
WIDGET_SCHEMA = "quiettv-widget-config/1"
STATE_SCHEMA = "quiettv-owner-state/1"
PACKAGE_ID_RE = r"[A-Za-z0-9]{10}"
APP_NAME_RE = r"[A-Za-z][A-Za-z0-9_]{0,49}"
TOKEN_RE = r"[A-Za-z0-9_-]{32,128}"
WIDGET_VERSION = "0.1.0"
LEGACY_PORTS = (4190, 4196)  # existing control service and boot hand-off; a new backend never reuses them
PORT_RANGE = (1024, 65535)
MAX_INVENTORY = 500
INVENTORY_KEYS = ("Id", "Name", "Installed", "Disabled", "Launchable")
TOKEN_FILE = "backend-token"
OWNER_FILES = ("host-config.json", "apps.json", "settings.json", "widget-config.json", "owner-state.json")


def _fail(message: str) -> None:
    raise OwnerInitError(message)


def is_placeholder(value: str) -> bool:
    v = value.lower()
    return any(c in v for c in PLACEHOLDER_CHARS) or any(p in v for p in PLACEHOLDERS)


def _full(pattern: str, value: Any) -> bool:
    return type(value) is str and re.fullmatch(pattern, value) is not None


def validate_package_id(value: Any) -> str:
    if not _full(PACKAGE_ID_RE, value) or is_placeholder(value) or len(set(value)) == 1:
        _fail("package id must be a real 10-character alphanumeric Tizen package id")
    return value


def validate_own_app_id(value: Any) -> str:
    if not _full(OWN_APP_ID_RE, value) or is_placeholder(value) or len(set(value.split(".")[0])) == 1:
        _fail("ownAppId must be a real Tizen app id (10-char package id + name)")
    return value


def validate_runtime_root(root: Any) -> str:
    if type(root) is not str or len(root) > 200 or "\\" in root or "\0" in root:
        _fail("runtimeRoot is not a plain POSIX path")
    if is_placeholder(root):
        _fail("runtimeRoot is still a placeholder")
    prefix = next((p for p in ROOT_PREFIXES if root.startswith(p)), None)
    if prefix is None:
        _fail("runtimeRoot must live below /home/owner/share/")
    segments = root[len(prefix):].split("/")
    for s in segments:
        if s in ("", ".", "..") or not _full(ROOT_SEGMENT_RE, s):
            _fail("runtimeRoot has an empty, relative or unsafe segment")
    if not _full(ROOT_LEAF_RE, segments[-1]):
        _fail("runtimeRoot must end in the host's own quiet-tv folder")
    return root


def _int(value: Any, label: str, lo: int, hi: int) -> int:
    if type(value) is not int or not lo <= value <= hi:
        _fail(f"{label} must be an integer {lo}..{hi}")
    return value


def _str_list(value: Any, label: str) -> list:
    if not isinstance(value, list) or any(type(v) is not str for v in value):
        _fail(f"{label} must be a list of strings")
    if len(set(value)) != len(value):
        _fail(f"{label} has duplicates")
    return value


def validate_host_config(obj: Any) -> dict:
    """Same accept/refuse rules as HostConfig.Parse for every value owner-init can emit."""
    if not isinstance(obj, dict):
        _fail("host config must be a JSON object")
    unknown = sorted(set(obj) - set(HOST_KNOWN))
    if unknown:
        _fail("host config has an unknown key " + unknown[0])
    for key in ("schema", "runtimeRoot", "ownerUid", "ownAppId", "homeKeyCode", "observedInputPaths"):
        if key not in obj:
            _fail(key + " is required")
    if obj["schema"] != HOST_SCHEMA:
        _fail("schema must be " + HOST_SCHEMA)
    validate_runtime_root(obj["runtimeRoot"])
    _int(obj["ownerUid"], "ownerUid", *OWNER_UID_RANGE)
    validate_own_app_id(obj["ownAppId"])
    _int(obj["homeKeyCode"], "homeKeyCode", *HOME_KEY_RANGE)
    paths = _str_list(obj["observedInputPaths"], "observedInputPaths")
    if not 1 <= len(paths) <= MAX_INPUT_PATHS:
        _fail("observedInputPaths needs 1-8 entries")
    if not all(_full(INPUT_PATH_RE, p) for p in paths):
        _fail("observedInputPaths entries must be /dev/input/eventN")
    names = _str_list(obj.get("remoteDeviceNames", []), "remoteDeviceNames")
    if len(names) > 8:
        _fail("remoteDeviceNames allows at most 8 entries")
    for n in names:
        if not 1 <= len(n) <= 64 or any(ord(c) < 0x20 or ord(c) > 0x7E or c in '"\\' for c in n) or is_placeholder(n):
            _fail("remoteDeviceNames entries must be 1-64 printable characters without quotes")
    for key in ("remoteGrabEnabled", "iptvEnabled", "webUiEnabled", "snapshotEnabled"):
        if key in obj and type(obj[key]) is not bool:
            _fail(key + " must be true or false")
    if obj.get("remoteGrabEnabled") and not names:
        _fail("remoteGrabEnabled requires remoteDeviceNames")
    return obj


def valid_app_id(value: Any) -> bool:
    return _full(APP_ID_RE, value) and not value.startswith(".") and ".." not in value


def launchable_id(value: Any) -> bool:
    """Valid for both the host (apps.json) and the frontend runtime list (window.QuietTVApps)."""
    return valid_app_id(value) and _full(RUNTIME_ID_RE, value)


def _valid_name(name: Any) -> bool:
    # C# string.Length counts UTF-16 units; char.IsControl covers C0, DEL and C1; lone surrogates never parse there.
    if type(name) is not str or any(0xD800 <= ord(c) <= 0xDFFF for c in name):
        return False
    units = len(name.encode("utf-16-le")) // 2
    return 1 <= units <= 64 and not any(ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F for c in name)


def validate_apps(apps: Any, runtime_root: str) -> list:
    if not isinstance(apps, list):
        _fail("apps.json must be an array")
    seen = set()
    for item in apps:
        if not isinstance(item, dict) or set(item) - {"Id", "Name", "Icon"} or not {"Id", "Name"} <= set(item):
            _fail("apps.json entries must be objects with Id, Name and optional Icon")
        if not valid_app_id(item["Id"]):
            _fail("apps.json has an unsafe app id")
        if item["Id"] in seen:
            _fail("apps.json has a duplicate app id")
        seen.add(item["Id"])
        if not _valid_name(item["Name"]):
            _fail("apps.json has an invalid app name")
        icon = item.get("Icon", "")
        if type(icon) is not str:
            _fail("Icon must be a string")
        if icon:
            rel = icon[len(runtime_root) + 1:] if icon.startswith(runtime_root + "/") else None
            if rel is None or any(s in ("", ".", "..") or not _full(ROOT_SEGMENT_RE, s) for s in rel.split("/")):
                _fail("apps.json icon must be a file inside the runtime root")
    if not 1 <= len(apps) <= MAX_APPS:
        _fail("apps.json must list 1-200 apps")
    return apps


def validate_settings(obj: Any, apps: list) -> dict:
    if not isinstance(obj, dict) or set(obj) - {"Order", "Favorites", "Wallpaper", "HomeKeyCode"}:
        _fail("settings.json has an unknown key")
    for key in ("Order", "Favorites"):
        values = _str_list(obj.get(key, []), key)
        if not all(valid_app_id(v) for v in values):
            _fail("settings.json has an unsafe app id")
    _int(obj.get("Wallpaper", 0), "Wallpaper", 0, 2)
    return obj


def validate_token(token: Any) -> str:
    if not _full(TOKEN_RE, token) or is_placeholder(token) or len(set(token)) < 8:
        _fail("backend token is malformed")
    return token


def validate_backend_port(port: Any) -> int:
    _int(port, "backend port", *PORT_RANGE)
    if port in LEGACY_PORTS:
        _fail("backend port must not be an existing QuietTV service port")
    return port


def validate_widget_config(obj: Any) -> dict:
    keys = ("schema", "packageId", "appName", "ownAppId", "version", "preview", "onBoot",
            "registerAsHome", "iptvEnabled", "apps", "backend")
    if not isinstance(obj, dict) or set(obj) != set(keys):
        _fail("widget config must have exactly: " + ", ".join(keys))
    if obj["schema"] != WIDGET_SCHEMA:
        _fail("widget config schema must be " + WIDGET_SCHEMA)
    validate_package_id(obj["packageId"])
    if not _full(APP_NAME_RE, obj["appName"]):
        _fail("appName is not a valid Tizen app name")
    if validate_own_app_id(obj["ownAppId"]) != obj["packageId"] + "." + obj["appName"]:
        _fail("ownAppId must be packageId.appName")
    if obj["version"] != WIDGET_VERSION:
        _fail("widget version is fixed for the preview")
    if obj["preview"] is not True:
        _fail("widget config must be preview:true")
    if obj["onBoot"] is not False:
        _fail("onBoot is forbidden for the preview widget")
    if obj["registerAsHome"] is not False:
        _fail("registerAsHome is forbidden for the preview widget")
    if obj["iptvEnabled"] is not False:
        _fail("IPTV stays off: there is no working IPTV support")
    apps = obj["apps"]
    if not isinstance(apps, list) or not 1 <= len(apps) <= MAX_APPS:
        _fail("widget apps must list 1-200 apps")
    seen = set()
    for a in apps:
        if not isinstance(a, dict) or set(a) != {"Id", "Name"} or not launchable_id(a["Id"]) or not _valid_name(a["Name"]):
            _fail("widget apps entries must be {Id, Name} with a safe id and name")
        if a["Id"] in seen or a["Id"] in (IPTV_APP_ID, obj["ownAppId"]):
            _fail("widget apps has a duplicate, IPTV or own app id")
        seen.add(a["Id"])
    backend = obj["backend"]
    if backend == {"enabled": False}:
        pass
    elif isinstance(backend, dict) and set(backend) == {"enabled", "port"} and backend["enabled"] is True:
        validate_backend_port(backend["port"])
    else:
        _fail("backend must be {enabled:false} or {enabled:true, port}")
    return obj


def filter_inventory(data: Any, own_app_id: str) -> list:
    """[{Id,Name,Installed,Disabled,Launchable}] or {records:[...]}; fail-closed on any malformed record."""
    records = data.get("records") if isinstance(data, dict) and set(data) == {"records"} else data
    if not isinstance(records, list) or not records or len(records) > MAX_INVENTORY:
        _fail("inventory must be a non-empty list of records (or {records:[...]})")
    seen, apps = set(), []
    for r in records:
        if not isinstance(r, dict) or set(r) != set(INVENTORY_KEYS):
            _fail("inventory records must have exactly " + ", ".join(INVENTORY_KEYS))
        if any(type(r[k]) is not bool for k in ("Installed", "Disabled", "Launchable")):
            _fail("inventory Installed/Disabled/Launchable must be booleans")
        if type(r["Id"]) is not str or r["Id"] in seen:
            _fail("inventory ids must be unique strings")
        seen.add(r["Id"])
        if not (r["Installed"] and not r["Disabled"] and r["Launchable"]):
            continue
        if r["Id"] in (IPTV_APP_ID, own_app_id):
            continue
        if not launchable_id(r["Id"]):
            _fail("inventory has an unsafe app id")
        if not _valid_name(r["Name"]) or r["Name"] != r["Name"].strip():
            _fail("inventory has an invalid app name")
        apps.append({"Id": r["Id"], "Name": r["Name"], "Icon": ""})
    if not apps:
        _fail("inventory has no installed, enabled, launchable apps")
    if len(apps) > MAX_APPS:
        _fail("inventory has more than 200 eligible apps")
    return apps


# ---- owner storage containment ----

def _norm(p: str) -> str:
    return os.path.normcase(os.path.abspath(p))


def _inside(child: str, parent: str) -> bool:
    c, p = _norm(child), _norm(parent)
    try:
        return os.path.commonpath([c, p]) == p
    except ValueError:  # different drives
        return False


def _is_link(p: str) -> bool:
    return os.path.islink(p) or (hasattr(os.path, "isjunction") and os.path.isjunction(p))


def check_outside_repos(path: str | os.PathLike, *, must_not_exist: bool = True,
                        forbidden: Iterable[str | os.PathLike] = ()) -> Path:
    """Refuse any destination inside a git checkout/worktree or this repo, literally or via links."""
    raw = os.fspath(path)
    if not raw or not os.path.isabs(raw) or ".." in Path(raw).parts:
        _fail("destination must be an explicit absolute path without '..'")
    target = Path(os.path.abspath(raw))
    if _is_link(str(target)):
        _fail("destination is a symlink or junction")
    if must_not_exist and os.path.lexists(target):
        _fail("destination already exists; refusing to overwrite")
    parent = target.parent
    if not parent.is_dir():
        _fail("destination parent directory must already exist")
    real = Path(os.path.realpath(target))
    roots = [REPO_ROOT, *[Path(f) for f in forbidden]]
    for candidate in (target, real):
        for root in roots:
            for r in (str(root), os.path.realpath(root)):
                if _inside(str(candidate), r):
                    _fail("destination is inside a source/release repository")
        for anc in [candidate, *candidate.parents]:
            if os.path.lexists(anc / ".git"):
                _fail("destination is inside a source/release repository")
    return target


# Replace the object's DACL in a single write with one current-user FullControl ACE.
# icacls /inheritance:r /grant:r only removes inherited ACEs and rewrites the named SID's
# grant; any explicit ACE for another principal (e.g. an inherited-then-copied Everyone
# grant) survived, so the object was not owner-only and the strict checker refused it.
# This builds a fresh, protected FileSecurity/DirectorySecurity in memory and writes it
# once, so no stray ACE can remain. Owner and SACL are left untouched, inheritance is
# blocked (protected), and no privilege or UAC change is made. A directory's ACE inherits
# to children (ContainerInherit|ObjectInherit); a file's does not. The script reads the
# DACL back and refuses unless it is exactly the one protected current-user ACE.
_ACL_OK = "QTV-ACL-OK"
_PRIVACY_ACL_SCRIPT = (
    "$ErrorActionPreference='Stop';"
    "try{"
    "$p=$env:QTV_PRIVATE_PATH;$kind=$env:QTV_PRIVATE_KIND;"
    "$sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User;"
    "if($kind -eq 'dir'){"
    "if(-not [System.IO.Directory]::Exists($p)){exit 6};"
    "$sec=New-Object System.Security.AccessControl.DirectorySecurity;"
    "$inh=[System.Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit';"
    "}elseif($kind -eq 'file'){"
    "if(-not [System.IO.File]::Exists($p)){exit 6};"
    "$sec=New-Object System.Security.AccessControl.FileSecurity;"
    "$inh=[System.Security.AccessControl.InheritanceFlags]::None;"
    "}else{exit 7};"
    "$sec.SetAccessRuleProtection($true,$false);"
    "$rule=New-Object System.Security.AccessControl.FileSystemAccessRule($sid,"
    "[System.Security.AccessControl.FileSystemRights]::FullControl,$inh,"
    "[System.Security.AccessControl.PropagationFlags]::None,"
    "[System.Security.AccessControl.AccessControlType]::Allow);"
    "$sec.AddAccessRule($rule);"
    "if($kind -eq 'dir'){[System.IO.Directory]::SetAccessControl($p,$sec)}"
    "else{[System.IO.File]::SetAccessControl($p,$sec)};"
    "$chk=if($kind -eq 'dir'){[System.IO.Directory]::GetAccessControl($p)}"
    "else{[System.IO.File]::GetAccessControl($p)};"
    "if(-not $chk.AreAccessRulesProtected){exit 8};"
    "$rules=@($chk.GetAccessRules($true,$true,"
    "[System.Security.Principal.SecurityIdentifier]));"
    "if($rules.Count -ne 1){exit 9};$r=$rules[0];"
    "if($r.IdentityReference.Value -ne $sid.Value){exit 10};"
    "if($r.AccessControlType -ne"
    " [System.Security.AccessControl.AccessControlType]::Allow){exit 11};"
    "if($r.IsInherited){exit 12};"
    "[System.Console]::Out.Write('" + _ACL_OK + "');"
    "}catch{exit 5}")


def _owner_only_acl(path: Path, inherit: bool) -> None:
    powershell = Path(os.environ.get("SystemRoot") or os.environ.get("WINDIR")
                      or r"C:\Windows") / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    # A PowerShell 7 PSMODULEPATH inherited into Windows PowerShell 5.1 breaks module
    # autoload; strip it case-insensitively. The parent environment is never modified.
    environment = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
    environment["QTV_PRIVATE_PATH"] = str(path)
    environment["QTV_PRIVATE_KIND"] = "dir" if inherit else "file"
    try:
        result = subprocess.run(
            (str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", _PRIVACY_ACL_SCRIPT),
            capture_output=True, text=True, timeout=30, check=False,
            stdin=subprocess.DEVNULL, env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        _fail("owner-only ACL could not be applied")
    if result.returncode != 0 or type(result.stdout) is not str or result.stdout.strip() != _ACL_OK:
        _fail("owner-only ACL could not be applied")


def make_private_file(path: Path) -> str:
    """Owner-only access for an existing file (0o600 at creation does nothing on Windows)."""
    if os.name == "nt":
        _owner_only_acl(path, inherit=False)
        return "windows-acl-owner-only"
    os.chmod(path, 0o600)
    return "posix-0600"


def make_private_dir(path: Path) -> str:
    """Create a new owner-only directory. Returns how privacy was applied."""
    os.mkdir(path, 0o700)
    try:
        if os.name == "nt":
            _owner_only_acl(path, inherit=True)
            return "windows-acl-owner-only"
        os.chmod(path, 0o700)
        return "posix-0700"
    except Exception:
        os.rmdir(path)
        raise


def _write_new(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=True) + "\n").encode("ascii")


def _default_token() -> str:
    return secrets.token_urlsafe(32)


def build_owner_documents(*, profile_id: str, device_facts: Any, inventory: Any, package_id: str,
                          owner_uid: int, input_paths: list, runtime_root: str, app_name: str = "QuietTV",
                          home_key_code: int = 63, backend_port: int | None = None) -> tuple[dict, str]:
    """Validate everything and return ({file name: object}, deviceIdSha256). Writes nothing."""
    try:
        prof = tvprofile.get_profile(profile_id)
        facts = tvprofile.parse_device_facts(device_facts)
        tvprofile.match_device(prof, facts)
        device_hash = tvprofile.device_id_sha256(facts["deviceId"])
    except tvprofile.ProfileError as exc:
        _fail(str(exc))
    validate_package_id(package_id)
    if not _full(APP_NAME_RE, app_name):
        _fail("app name is not a valid Tizen app name")
    own_app_id = validate_own_app_id(package_id + "." + app_name)
    host = validate_host_config({
        "schema": HOST_SCHEMA, "runtimeRoot": runtime_root, "ownerUid": owner_uid, "ownAppId": own_app_id,
        "homeKeyCode": home_key_code, "observedInputPaths": list(input_paths), "remoteDeviceNames": [],
        "remoteGrabEnabled": False, "iptvEnabled": False, "webUiEnabled": False, "snapshotEnabled": False})
    apps = validate_apps(filter_inventory(inventory, own_app_id), runtime_root)
    settings = validate_settings({"Order": [a["Id"] for a in apps], "Favorites": [], "Wallpaper": 0}, apps)
    backend = {"enabled": False} if backend_port is None else {"enabled": True, "port": backend_port}
    widget = validate_widget_config({
        "schema": WIDGET_SCHEMA, "packageId": package_id, "appName": app_name, "ownAppId": own_app_id,
        "version": WIDGET_VERSION, "preview": True, "onBoot": False, "registerAsHome": False,
        "iptvEnabled": False, "apps": [{"Id": a["Id"], "Name": a["Name"]} for a in apps], "backend": backend})
    state = {"schema": STATE_SCHEMA, "profileId": prof.profile_id, "profileSchema": prof.schema,
             "deviceIdSha256": device_hash, "preview": True, "consumerApplyEligible": False}
    docs = {"host-config.json": host, "apps.json": apps, "settings.json": settings,
            "widget-config.json": widget, "owner-state.json": state}
    return docs, device_hash


def generate(owner_dir: str | os.PathLike, *, token_factory: Callable[[], str] | None = None,
             **inputs: Any) -> dict:
    docs, device_hash = build_owner_documents(**inputs)
    token = None
    if docs["widget-config.json"]["backend"]["enabled"]:
        if token_factory is not None:
            token = validate_token(token_factory())  # injected factories are single-shot
        else:
            # a random token can, rarely, contain a placeholder word such as "todo"; draw again
            for _ in range(16):
                try:
                    token = validate_token(_default_token())
                    break
                except OwnerInitError:
                    continue
            else:
                _fail("could not draw a valid backend token")
    target = check_outside_repos(owner_dir)
    privacy = make_private_dir(target)
    check_outside_repos(target, must_not_exist=False)  # re-check the created directory, links resolved
    for name in OWNER_FILES:
        _write_new(target / name, json_bytes(docs[name]))
    files = list(OWNER_FILES)
    if token is not None:
        _write_new(target / TOKEN_FILE, token.encode("ascii"))
        files.append(TOKEN_FILE)
    return {"ownerDir": str(target), "files": sorted(files), "deviceIdSha256": device_hash,
            "permissions": privacy, "preview": True, "consumerApplyEligible": False}


def load_owner_dir(owner_dir: str | os.PathLike) -> dict:
    """Read and re-validate an owner directory (used by build_widget). Token returned only if enabled."""
    root = Path(owner_dir)
    if not root.is_dir() or _is_link(str(root)):
        _fail("owner directory is missing or a link")
    def load(name: str) -> Any:
        p = root / name
        if _is_link(str(p)) or not p.is_file():
            _fail(name + " is missing or a link")
        try:
            return sj.load_file(p, max_bytes=1024 * 1024)
        except sj.StrictJsonError as exc:
            _fail(f"{name}: {exc}")
    host = validate_host_config(load("host-config.json"))
    apps = validate_apps(load("apps.json"), host["runtimeRoot"])
    validate_settings(load("settings.json"), apps)
    widget = validate_widget_config(load("widget-config.json"))
    state = load("owner-state.json")
    if not isinstance(state, dict) or state.get("schema") != STATE_SCHEMA or state.get("consumerApplyEligible") is not False:
        _fail("owner-state.json is not a preview owner state")
    if widget["ownAppId"] != host["ownAppId"] or [a["Id"] for a in widget["apps"]] != [a["Id"] for a in apps]:
        _fail("widget-config.json does not match host-config.json/apps.json")
    token_path = root / TOKEN_FILE
    token = None
    if widget["backend"]["enabled"]:
        if _is_link(str(token_path)) or not token_path.is_file() or token_path.stat().st_size > 256:
            _fail("backend is enabled but backend-token is missing")
        token = validate_token(token_path.read_text("ascii"))
    elif os.path.lexists(token_path):
        _fail("backend-token present while backend is disabled; refusing")
    return {"host": host, "apps": apps, "widget": widget, "state": state, "token": token}


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="QuietTV owner-init (preview only; writes owner storage only)")
    ap.add_argument("--owner-dir", required=True, help="new directory OUTSIDE every repository; must not exist")
    ap.add_argument("--profile-id", required=True)
    ap.add_argument("--device-facts", required=True, help="JSON file: model, firmware, arch, osName, osVersion, deviceId")
    ap.add_argument("--inventory", required=True, help="JSON file: [{Id,Name,Installed,Disabled,Launchable}]")
    ap.add_argument("--package-id", required=True)
    ap.add_argument("--app-name", default="QuietTV")
    ap.add_argument("--owner-uid", required=True, type=int)
    ap.add_argument("--input-path", required=True, action="append", dest="input_paths")
    ap.add_argument("--runtime-root", required=True)
    ap.add_argument("--home-key-code", type=int, default=63)
    ap.add_argument("--backend-port", type=int, default=None,
                    help="enable the future loopback backend (creates backend-token); default disabled")
    args = ap.parse_args(argv)
    try:
        def read(path: str) -> Any:
            try:
                return sj.load_file(path, max_bytes=4 * 1024 * 1024)
            except sj.StrictJsonError as exc:
                _fail(f"{Path(path).name}: {exc}")
        result = generate(args.owner_dir, profile_id=args.profile_id, device_facts=read(args.device_facts),
                          inventory=read(args.inventory), package_id=args.package_id, owner_uid=args.owner_uid,
                          input_paths=args.input_paths, runtime_root=args.runtime_root, app_name=args.app_name,
                          home_key_code=args.home_key_code, backend_port=args.backend_port)
    except (OwnerInitError, OSError, subprocess.SubprocessError) as exc:
        print("owner-init refused: " + (str(exc) if isinstance(exc, OwnerInitError) else type(exc).__name__),
              file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

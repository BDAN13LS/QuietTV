"""Fixed remote command strings for the owner provider.

Only tool-computed values are substituted: 10-character alphanumeric package
ids, 32-hex run ids, fixed data/report names and fixed executable paths. There
is no caller-supplied command, path, shell expression or extra argument.

Commands deliberately contain no backslash and no double quote. The upstream
root agent wraps a command for ``/bin/sh -c`` by escaping those characters, and
the reference SDK has an escaping caveat for staged scripts; avoiding both keeps
the command text identical after wrapping. Single quotes, ``$()``, redirections
and newlines (for a here-document) are used instead.
"""

from __future__ import annotations

import re

from .. import liveops

MAX_COMMAND_BYTES = 64 * 1024
NATIVE_CHUNK_BYTES = 699051  # base64 stays below the upstream 1 MiB command cap
NATIVE_MAX_CHUNKS = 7       # seven chunks cover 4 MiB + 1 with one spare
SDK_UID_ROOT = "/home/owner/share/tmp/sdk_tools"
SQLITE = "/usr/bin/sqlite3"
PACKAGE_DB = "/opt/dbspace/.pkgmgr_parser.db"
APP_LAUNCHER = "/usr/bin/app_launcher"
DOTNET = "/usr/bin/dotnet"
NATIVE_OUT = "native.out"

_PACKAGE_RE = re.compile(r"[A-Za-z0-9]{10}\Z")
_RUN_DIR_RE = re.compile(r"/home/owner/share/tmp/sdk_tools/quiettv-collect-[0-9a-f]{32}\Z")
_PATH_RE = re.compile(r"/[A-Za-z0-9_./:-]{1,400}\Z")


class CommandError(ValueError):
    """A finite refusal for an internal command-shape violation."""


def _safe(text: str) -> str:
    if type(text) is not str or not text or len(text.encode("utf-8")) > MAX_COMMAND_BYTES:
        raise CommandError("a remote command is malformed")
    if "\\" in text or '"' in text or "\x00" in text:
        raise CommandError("a remote command carries a forbidden character")
    for character in text:
        if ord(character) < 0x20 and character != "\n":
            raise CommandError("a remote command carries a control character")
    return text


def _package(package_id: str) -> str:
    if type(package_id) is not str or not _PACKAGE_RE.fullmatch(package_id):
        raise CommandError("a package id is malformed")
    return package_id


def _run_dir(value: str) -> str:
    if type(value) is not str or not _RUN_DIR_RE.fullmatch(value):
        raise CommandError("a remote run directory is malformed")
    return value


def _path(value: str) -> str:
    if type(value) is not str or not _PATH_RE.fullmatch(value) or ".." in value.split("/"):
        raise CommandError("a remote path is malformed")
    return value


def _package_db_command(sql: str) -> str:
    return _safe(
        f"{SQLITE} -init /dev/null -readonly -batch -bail -noheader -list "
        f"{PACKAGE_DB} <<'QTVSQL'\n{sql}\nQTVSQL\n"
    )


def package_count_command(package_id: str) -> str:
    """Number of package_info rows for the exact package."""
    package = _package(package_id)
    return _package_db_command(
        f"SELECT count(*) FROM package_info WHERE package='{package}';")


def package_and_app_count_command(package_id: str) -> str:
    """Fixed ``package:application`` counts used as the install postcondition."""
    package = _package(package_id)
    app_id = package + ".Facts"
    return _package_db_command(
        "SELECT (SELECT count(*) FROM package_info WHERE package='"
        f"{package}') || ':' || (SELECT count(*) FROM package_app_info "
        f"WHERE app_id='{app_id}');")


def path_presence_command(path: str) -> str:
    """``LINK``, ``PRESENT`` or ``ABSENT``; a link is never followed."""
    target = _path(path)
    return _safe(
        f"if [ -L {target} ]; then echo LINK; "
        f"elif [ -e {target} ]; then echo PRESENT; else echo ABSENT; fi")


def create_run_dir_command(run_dir: str, group_id: int) -> str:
    """Non-recursive fresh mkdir with owner UID from the existing SDK-tools dir."""
    target = _run_dir(run_dir)
    if type(group_id) is not int or not 0 <= group_id <= 2**31 - 1:
        raise CommandError("a group id is malformed")
    return _safe(
        f"if mkdir {target}; then "
        f"set -- $(stat -c %u {SDK_UID_ROOT}); "
        "if [ $# -eq 1 ] && "
        f"chown $1:{group_id} {target} && chmod 770 {target}; then "
        f"echo $1:$(stat -c %u:%g:%a {target}); else echo INVALID; fi; "
        "else echo EXISTS; fi")


def sha256sum_command(path: str) -> str:
    target = _path(path)
    return _safe(f"sha256sum {target}")


def launch_command(package_id: str) -> str:
    package = _package(package_id)
    return _safe(f"{APP_LAUNCHER} -s {package}.Facts")


def report_read_command(path: str) -> str:
    """Read at most cap+1 bytes from a regular file; marker states are explicit."""
    target = _path(path)
    cap = liveops.MAX_REPORT_READ_BYTES
    return _safe(
        f"if [ -L {target} ]; then echo '!symlink!'; "
        f"elif [ ! -e {target} ]; then echo '!absent!'; "
        f"elif [ ! -f {target} ]; then echo '!nonregular!'; "
        f"else head -c {cap} {target} | base64; fi")


def native_run_command(run_dir: str) -> str:
    target = _run_dir(run_dir)
    return _safe(
        f"if [ -L {target}/{NATIVE_OUT} ]; then echo '!output-link!'; "
        f"elif [ -e {target}/{NATIVE_OUT} ] && [ ! -f {target}/{NATIVE_OUT} ]; "
        f"then echo '!output-nonregular!'; else "
        f"{DOTNET} {target}/QuietTVInspect.dll --inventory-framed "
        f"> {target}/{NATIVE_OUT} 2>/dev/null; echo EXIT:$?; fi")


def native_guard_run_command(run_dir: str, dll_sha256: str,
                             runtimeconfig_sha256: str, deps_sha256: str) -> str:
    """Re-hash the three pushed bundle files, then run the fixed inspector."""
    target = _run_dir(run_dir)
    digests = []
    for digest in (dll_sha256, runtimeconfig_sha256, deps_sha256):
        if type(digest) is not str or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise CommandError("a bundle digest is malformed")
        digests.append(digest)
    dll, config, deps = (f"{target}/QuietTVInspect.dll",
                         f"{target}/QuietTVInspect.runtimeconfig.json",
                         f"{target}/QuietTVInspect.deps.json")
    checks = " && ".join(
        f"echo '{digest}  {path}' | sha256sum -c - >/dev/null 2>&1"
        for digest, path in zip(digests, (dll, config, deps)))
    return _safe(
        f"if {checks}; then "
        f"if [ -L {target}/{NATIVE_OUT} ]; then echo '!output-link!'; "
        f"elif [ -e {target}/{NATIVE_OUT} ] && [ ! -f {target}/{NATIVE_OUT} ]; "
        f"then echo '!output-nonregular!'; else "
        f"{DOTNET} {target}/QuietTVInspect.dll --inventory-framed "
        f"> {target}/{NATIVE_OUT} 2>/dev/null; echo EXIT:$?; fi; "
        "else echo '!bundle-mismatch!'; fi")


def native_size_command(run_dir: str) -> str:
    target = _run_dir(run_dir)
    return _safe(
        f"if [ -L {target}/{NATIVE_OUT} ]; then echo '!symlink!'; "
        f"elif [ ! -e {target}/{NATIVE_OUT} ]; then echo '!missing!'; "
        f"elif [ ! -f {target}/{NATIVE_OUT} ]; then echo '!nonregular!'; "
        f"else wc -c < {target}/{NATIVE_OUT}; fi")


def native_chunk_command(run_dir: str, index: int) -> str:
    target = _run_dir(run_dir)
    if type(index) is not int or not 0 <= index < NATIVE_MAX_CHUNKS:
        raise CommandError("a native chunk index is malformed")
    return _safe(
        f"if [ -L {target}/{NATIVE_OUT} ]; then echo '!symlink!'; "
        f"elif [ ! -f {target}/{NATIVE_OUT} ]; then echo '!nonregular!'; "
        f"else dd if={target}/{NATIVE_OUT} bs={NATIVE_CHUNK_BYTES} "
        f"skip={index} count=1 2>/dev/null | base64; fi")


def native_chunks(size: int) -> int:
    if type(size) is not int or size < 0 or size > liveops.MAX_NATIVE_FRAME_BYTES:
        raise CommandError("the native output size is out of range")
    if size == 0:
        return 0
    return (size + NATIVE_CHUNK_BYTES - 1) // NATIVE_CHUNK_BYTES


def remove_file_command(path: str, digest: str) -> str:
    """Remove only a regular file whose current SHA-256 equals ``digest``."""
    target = _path(path)
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise CommandError("an upload digest is malformed")
    return _safe(
        f"if [ -L {target} ]; then echo LINK; "
        f"elif [ ! -e {target} ]; then echo ABSENT; "
        f"elif sha256sum {target} | grep -q '^{digest} '; then "
        f"rm -f {target}; echo REMOVED; else echo MISMATCH; fi")


def remove_scratch_command(path: str) -> str:
    """Remove this run's own scratch file, refusing a link or a non-file."""
    target = _path(path)
    return _safe(
        f"if [ -L {target} ]; then echo LINK; "
        f"elif [ ! -e {target} ]; then echo ABSENT; "
        f"elif [ -f {target} ]; then rm -f {target}; echo REMOVED; "
        "else echo MISMATCH; fi")


def rmdir_command(run_dir: str) -> str:
    target = _run_dir(run_dir)
    return _safe(
        f"if [ ! -e {target} ] && [ ! -L {target} ]; then echo ABSENT; "
        f"elif rmdir {target}; then echo REMOVED; else echo NOTEMPTY; fi")


def sdb_serial(target: str) -> str:
    if type(target) is not str or not re.fullmatch(
            r"(?:[0-9]{1,3}\.){3}[0-9]{1,3}", target):
        raise CommandError("the target is malformed")
    return f"{target}:26101"


def install_shell_argument(package_id: str, remote_wgt: str) -> str:
    package = _package(package_id)
    target = _path(remote_wgt)
    if not target.endswith(".wgt"):
        raise CommandError("the widget path is malformed")
    return _safe(f"0 vd_appinstall {package} {target}")


def uninstall_shell_argument(package_id: str) -> str:
    package = _package(package_id)
    return _safe(f"0 vd_appuninstall {package}")

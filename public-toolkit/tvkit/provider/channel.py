"""Parent-side SDK and isolated-root channel for the owner provider.

The SDK is always the exact absolute executable pinned in the owner config; it
is never discovered through ``PATH`` or the ``SDB`` environment variable. The
root route runs the vendored upstream probe in a fresh child process with an
explicit environment, so no module global or environment change is made in the
parent.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import commands, config as provider_config, proc


class ChannelError(RuntimeError):
    """Fixed channel failure; the session converts it to its own finite error."""


@dataclass(frozen=True)
class CommandEvidence:
    exit_code: int
    timed_out: bool
    stdout: str


@dataclass(frozen=True)
class ProbeEvidence:
    authenticated: bool
    uid: int
    euid: int
    gid: int
    egid: int
    runtime: str
    system_files_unchanged: bool
    staging_cleaned: bool
    commands: tuple[CommandEvidence, ...]


@dataclass(frozen=True)
class SdkOutcome:
    returncode: int
    reported_error: bool


_DUID_RE = re.compile(r"[0-9A-Za-z._:-]{1,128}\Z")
_SHA_LINE_RE = re.compile(r"([0-9a-f]{64})\s+\S.*\Z")
_CHILD_STDOUT_MAX = 16 * 1024 * 1024
_CHILD_STDERR_MAX = 64 * 1024
_SDK_STDOUT_MAX = 4 * 1024 * 1024
_SDK_STDERR_MAX = 64 * 1024


def _reported_error(stdout: str, stderr: str) -> bool:
    return any(
        line.lstrip().lower().startswith("error:")
        for output in (stdout, stderr)
        for line in output.splitlines()
    )


class LiveChannel:
    """Concrete SDK/root channel. Tests substitute the same small interface."""

    def __init__(self, config: provider_config.ProviderConfig):
        self.config = config
        self._serial = commands.sdb_serial(config.target)
        self._connected = False
        self._temp_dir = Path(tempfile.mkdtemp(prefix="quiettv-provider-"))
        try:
            if os.name != "nt":
                os.chmod(self._temp_dir, 0o700)
            self._payload_dir = self._temp_dir / "payloads"
            os.mkdir(self._payload_dir, 0o700)
        except OSError:
            shutil.rmtree(self._temp_dir, ignore_errors=True)
            raise ChannelError("the provider scratch directory could not be created") from None

    # ---- environment and artifact revalidation ----

    def _system_root(self) -> str:
        return os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR") or "C:\\Windows"

    def _sdk_env(self) -> dict[str, str]:
        sdk_dir = str(self.config.sdk_executable.parent)
        if os.name == "nt":
            root = self._system_root()
            return {
                "PATH": f"{sdk_dir};{root}\\System32;{root}",
                "SYSTEMROOT": root,
                "WINDIR": root,
                "TEMP": str(self._temp_dir),
                "TMP": str(self._temp_dir),
                "PYTHONIOENCODING": "utf-8",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        return {
            "PATH": f"{sdk_dir}:/usr/bin:/bin",
            "LANG": "C",
            "LC_ALL": "C",
            "TMPDIR": str(self._temp_dir),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    def verify_artifacts(self) -> None:
        try:
            provider_config.revalidate(self.config)
        except provider_config.ProviderConfigError as error:
            raise ChannelError(str(error)) from None

    def _verify_sdk(self) -> None:
        try:
            provider_config.verify_file(
                self.config.sdk_executable, self.config.sdk_sha256,
                "the SDK executable", max_bytes=provider_config.MAX_SDK_BYTES)
        except provider_config.ProviderConfigError as error:
            raise ChannelError(str(error)) from None

    # ---- raw SDK shell (no capture, no staging) ----

    def _sdk(self, args: tuple[str, ...]) -> proc.ProcessResult:
        self._verify_sdk()
        try:
            return proc.run(
                (str(self.config.sdk_executable), *args),
                timeout=self.config.sdk_seconds,
                max_stdout=_SDK_STDOUT_MAX, max_stderr=_SDK_STDERR_MAX,
                env=self._sdk_env(), cwd=str(self._temp_dir))
        except proc.ProcessError as error:
            raise ChannelError(str(error)) from None

    def ensure_connected(self) -> None:
        """One explicit fixed ``sdb connect`` per channel; never disconnect."""
        if self._connected:
            return
        result = self._sdk(("connect", self._serial))
        if result.returncode != 0:
            raise ChannelError("the SDK connection did not succeed")
        try:
            out = result.stdout.decode("utf-8", "replace")
            err = result.stderr.decode("utf-8", "replace")
        except Exception:
            out = err = ""
        if _reported_error(out, err):
            raise ChannelError("the SDK connection reported an error")
        self._connected = True

    def getduid(self) -> str:
        self.ensure_connected()
        result = self._sdk(("-s", self._serial, "shell", "0 getduid"))
        if result.returncode != 0:
            raise ChannelError("the SDK DUID read did not succeed")
        try:
            text = result.stdout.decode("utf-8", "strict")
        except UnicodeError:
            raise ChannelError("the SDK DUID read was not text") from None
        # The SDK transport terminates the exact identity with CR, CRLF or
        # CRCRLF. Strip only those trailing terminators; the whole remaining
        # string must still match the identity pattern.
        value = text.rstrip("\r\n")
        if not _DUID_RE.fullmatch(value):
            raise ChannelError("the SDK DUID read was malformed")
        return value

    def group_id(self) -> int:
        """SDK group via the vendored capture callback, not a raw shell command."""
        self.ensure_connected()
        self.verify_artifacts()
        capture_seconds = max(30.0, float(self.config.sdk_seconds))
        request = {
            "operation": "sdk-group",
            "vendorDirectory": self._vendor_directory(),
            "sdkExecutable": str(self.config.sdk_executable),
            "sdkSha256": self.config.sdk_sha256,
            "target": self.config.target,
            "callbackHost": self.config.callback_host,
            "captureSeconds": capture_seconds,
        }
        result = self._worker(request, timeout=capture_seconds + 30.0)
        value = result.get("group")
        if type(value) is not int or not 0 <= value <= 2**31 - 1:
            raise ChannelError("the SDK group read was malformed")
        return value

    def push_bytes(self, data: bytes, remote_path: str) -> None:
        if type(data) is not bytes:
            raise ChannelError("an upload is not a byte string")
        commands._path(remote_path)
        self.ensure_connected()
        handle_path = self._temp_dir / ("push-" + secrets.token_hex(16))
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        try:
            fd = os.open(handle_path, flags, 0o600)
        except OSError:
            raise ChannelError("a local upload copy could not be created") from None
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            written = handle_path.read_bytes()
            if hashlib.sha256(written).hexdigest() != hashlib.sha256(data).hexdigest():
                raise ChannelError("a local upload copy did not match its bytes")
            result = self._sdk(("-s", self._serial, "push",
                                str(handle_path), remote_path))
            if result.returncode != 0:
                raise ChannelError("the SDK upload did not succeed")
            try:
                out = result.stdout.decode("utf-8", "replace")
                err = result.stderr.decode("utf-8", "replace")
            except Exception:
                out = err = ""
            if _reported_error(out, err):
                raise ChannelError("the SDK upload reported an error")
            expected = hashlib.sha256(data).hexdigest()
            evidence = self.probe((commands.sha256sum_command(remote_path),))
            match = _SHA_LINE_RE.fullmatch(evidence.commands[0].stdout.strip())
            if match is None or match.group(1) != expected:
                raise ChannelError("the uploaded artifact did not read back with its exact hash")
        finally:
            try:
                handle_path.unlink()
            except OSError:
                pass

    def install(self, package_id: str, remote_wgt: str) -> SdkOutcome:
        self.ensure_connected()
        argument = commands.install_shell_argument(package_id, remote_wgt)
        result = self._sdk(("-s", self._serial, "shell", argument))
        return SdkOutcome(result.returncode,
                          _reported_error(result.stdout.decode("utf-8", "replace"),
                                          result.stderr.decode("utf-8", "replace")))

    def uninstall(self, package_id: str) -> SdkOutcome:
        self.ensure_connected()
        argument = commands.uninstall_shell_argument(package_id)
        result = self._sdk(("-s", self._serial, "shell", argument))
        return SdkOutcome(result.returncode,
                          _reported_error(result.stdout.decode("utf-8", "replace"),
                                          result.stderr.decode("utf-8", "replace")))

    # ---- isolated root probe ----

    def _payload_request(self) -> tuple[Path, dict[str, str]]:
        """Revalidate and materialise the exact payload bytes for this probe."""
        self.verify_artifacts()
        pins = {
            "SamsungTvArchiveRoot.dll": self.config.archive_root_sha256,
            "SamsungTvRootAgent.dll": self.config.root_agent_sha256,
        }
        sources = {
            "SamsungTvArchiveRoot.dll": self.config.archive_root_dll,
            "SamsungTvRootAgent.dll": self.config.root_agent_dll,
        }
        try:
            for name, source in sources.items():
                data = provider_config.verify_file(
                    source, pins[name], f"the {name} payload",
                    max_bytes=provider_config.MAX_DLL_BYTES)
                target = self._payload_dir / name
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
        except (provider_config.ProviderConfigError, OSError) as error:
            raise ChannelError(str(error)) from None
        return self._payload_dir, pins

    def probe(self, commands_to_run: tuple[str, ...], *,
              command_seconds: float | None = None) -> ProbeEvidence:
        self.ensure_connected()
        if (not isinstance(commands_to_run, tuple) or not commands_to_run
                or len(commands_to_run) > 128
                or any(type(item) is not str or not item for item in commands_to_run)):
            raise ChannelError("a root probe request is malformed")
        if command_seconds is None:
            effective_command_seconds = self.config.command_seconds
        else:
            if (type(command_seconds) not in (int, float)
                    or not 0 < float(command_seconds) <= 600):
                raise ChannelError("a root probe timeout is malformed")
            effective_command_seconds = float(command_seconds)
        payload_dir, pins = self._payload_request()
        self._verify_sdk()
        request = {
            "operation": "root-probe",
            "vendorDirectory": self._vendor_directory(),
            "sdkExecutable": str(self.config.sdk_executable),
            "sdkSha256": self.config.sdk_sha256,
            "opensslExecutable": str(self.config.openssl_executable),
            "opensslSha256": self.config.openssl_sha256,
            "payloadDirectory": str(payload_dir),
            "payloadSha256": pins,
            "target": self.config.target,
            "callbackHost": self.config.callback_host,
            "sdbSeconds": self.config.sdk_seconds,
            "acceptSeconds": max(5.0, self.config.probe_seconds - 20.0),
            "commandSeconds": effective_command_seconds,
            "commands": list(commands_to_run),
        }
        # The upstream probe bounds startup/accept and each remote command
        # separately and then runs its staging-cleanup and system-hash checks.
        # The outer kill must outlast all of those so a probe is not killed
        # while it is cleaning its own root namespace.
        outer_timeout = (self.config.probe_seconds
                         + len(commands_to_run) * effective_command_seconds + 180.0)
        result = self._worker(request, timeout=outer_timeout)
        return self._evidence(result, len(commands_to_run))

    def _vendor_directory(self) -> str:
        return str(Path(__file__).resolve().parent.parent.parent
                   / "vendor" / "samsung_tv_root")

    def _worker(self, request: dict, *, timeout: float) -> dict:
        raw = json.dumps(request, separators=(",", ":")).encode("utf-8")
        worker = Path(__file__).with_name("worker.py")
        try:
            process = proc.run(
                (sys.executable, "-I", "-B", str(worker)),
                timeout=timeout,
                max_stdout=_CHILD_STDOUT_MAX, max_stderr=_CHILD_STDERR_MAX,
                env=self._sdk_env(), cwd=str(self._temp_dir), input_bytes=raw)
        except proc.ProcessError as error:
            raise ChannelError(str(error)) from None
        if process.returncode != 0:
            raise ChannelError("the isolated operation did not succeed")
        try:
            result = json.loads(process.stdout.decode("utf-8", "strict"))
        except (UnicodeError, ValueError, json.JSONDecodeError):
            raise ChannelError("the isolated operation returned no usable result") from None
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise ChannelError("the isolated operation did not succeed")
        return result

    @staticmethod
    def _evidence(result: dict, expected_commands: int) -> ProbeEvidence:
        if result.get("authenticated") is not True:
            raise ChannelError("the root callback was not authenticated")
        if result.get("runtime") != "6.0":
            raise ChannelError("the target does not offer the supported .NET 6 runtime")
        if result.get("system_files_unchanged") is not True:
            raise ChannelError("the protected system files changed; refusing")
        if result.get("staging_cleaned") is not True:
            raise ChannelError("the root staging directory was not cleaned; refusing")
        identities = []
        for key in ("uid", "euid", "gid", "egid"):
            value = result.get(key)
            if type(value) is not int or value != 0:
                raise ChannelError("the root callback did not have root identity")
            identities.append(value)
        raw_commands = result.get("commands")
        if not isinstance(raw_commands, list) or len(raw_commands) != expected_commands:
            raise ChannelError("the root operation did not return the expected results")
        cleaned = []
        for item in raw_commands:
            if not isinstance(item, dict):
                raise ChannelError("the root operation returned a malformed result")
            exit_code = item.get("exit_code")
            timed_out = item.get("timed_out")
            stdout = item.get("stdout")
            if type(exit_code) is not int or exit_code != 0 or timed_out is not False:
                raise ChannelError("a root command did not exit cleanly")
            if type(stdout) is not str:
                raise ChannelError("a root command result was malformed")
            cleaned.append(CommandEvidence(exit_code, False, stdout))
        return ProbeEvidence(True, *identities, result["runtime"], True, True,
                             tuple(cleaned))

    def close(self) -> None:
        shutil.rmtree(self._temp_dir, ignore_errors=True)

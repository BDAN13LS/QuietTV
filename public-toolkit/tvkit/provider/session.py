"""The one fixed, owner-configured live-operations provider.

Provider name: ``owner-sdk-root``. It is constructed only from an explicit
owner-private configuration file (see :mod:`tvkit.provider.config`); the public
registry stays empty unless a parent registers this provider for one run.

Every operation is bounded and finite. The raw SDK ``0 getduid`` guard runs
immediately before each operation and again before each write; any mismatch or
failure poisons the session so no later operation (including cleanup) can touch
a changed target. Root work is one vendored ``archive_root.probe`` per group of
fixed commands, in a child process, and requires authenticated root identity,
clean staging, unchanged protected system files and all commands exiting zero.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
import time

from .. import liveops
from ..paths import is_sha256
from . import channel as provider_channel
from . import commands
from . import config as provider_config

PROVIDER_NAME = "owner-sdk-root"
NATIVE_NAMES = ("QuietTVInspect.dll", "QuietTVInspect.runtimeconfig.json",
                "QuietTVInspect.deps.json")
_COUNT_RE = re.compile(r"[0-9]{1,6}\Z")
_SIZE_RE = re.compile(r"[0-9]{1,10}\Z")
_COUNTS_RE = re.compile(r"([0-9]{1,6}):([0-9]{1,6})\Z")
_EXIT_RE = re.compile(r"EXIT:([0-9]{1,3})\Z")
_OWNED_MARKERS = ("LINK", "PRESENT", "ABSENT")
_B64_RE = re.compile(r"[A-Za-z0-9+/]*={0,2}\Z")
_TRAILER_RE = re.compile(rb"\n\[exit:[0-9]{1,3}\]\n?\Z")


def _fail(message: str) -> None:
    raise liveops.LiveOpsError(message)


def _decode_base64(text: str) -> bytes:
    compact = "".join(text.split())
    if not compact or not _B64_RE.fullmatch(compact):
        _fail("a bounded remote read was not base64 text")
    padded = compact + "=" * (-len(compact) % 4)
    try:
        return base64.b64decode(padded, validate=True)
    except (ValueError, TypeError):
        _fail("a bounded remote read was not base64 text")


def _strip_exit_trailer(data: bytes) -> bytes:
    match = _TRAILER_RE.search(data)
    if match is None:
        return data
    return data[:match.start()]


def _markers(evidence) -> list[str]:
    return [item.stdout.strip() for item in evidence.commands]


class ProviderSession(liveops.LiveSession):
    """One session bound to one configured target; never re-targets."""

    def __init__(self, config: provider_config.ProviderConfig, channel):
        self._config = config
        self._channel = channel
        self.run_id = ""
        self._token = secrets.token_hex(16)
        self._binding = object()
        self._blocked = False
        self._closed = False
        self._run_dir_owned: bool | None = None
        self._run_dir_created = False
        self._install_attempted = False
        self._install_confirmed = False
        self._package_absent_confirmed: bool | None = None
        self._data_absent_confirmed: bool | None = None
        self._uploaded_paths: set[str] = set()
        self._native_pins: dict[str, str] | None = None
        self._package_id: str | None = None

    @property
    def session_token(self) -> str:
        return self._token

    @property
    def binding(self) -> object:
        return self._binding

    # ---- guards and evidence ----

    def _usable(self) -> None:
        if self._closed or self._blocked:
            _fail("the live session is no longer usable; refusing every operation")

    def _poison(self) -> None:
        self._blocked = True

    def _guard(self) -> None:
        """Raw SDK ``0 getduid``; a mismatch or failure blocks all later work."""
        self._usable()
        try:
            duid = self._channel.getduid()
        except Exception:
            self._poison()
            _fail("the target identity could not be confirmed; refusing every operation")
        if duid != self._config.expected_sdk_duid:
            self._poison()
            _fail("the target identity changed; refusing every operation")

    def _require_package(self, package: str) -> str:
        if self._package_id is None:
            self._package_id = package
        elif self._package_id != package:
            _fail("a different package id was used in this session; refusing")
        return package

    def _probe(self, remote_commands: tuple[str, ...], *,
               command_seconds: float | None = None):
        self._usable()
        try:
            if command_seconds is None:
                return self._channel.probe(tuple(remote_commands))
            return self._channel.probe(tuple(remote_commands),
                                       command_seconds=command_seconds)
        except Exception:
            _fail("a root operation did not provide acceptable evidence; refusing")

    def _result(self, cls, **fields):
        return cls(ok=True, run_id=self.run_id, session_token=self._token,
                   binding=self._binding, **fields)

    # ---- namespace absence ----

    def assert_absent(self, package_id: str, data_paths: tuple[str, str]) -> liveops.AbsenceResult:
        package = self._require_package(commands._package(package_id))
        fixed = liveops.data_paths(package)
        if type(data_paths) is not tuple or tuple(data_paths) != fixed:
            _fail("the report paths are not the fixed tool-computed paths; refusing")
        self._guard()
        evidence = self._probe((
            commands.package_count_command(package),
            commands.path_presence_command(fixed[0]),
            commands.path_presence_command(fixed[1]),
        ))
        count, first, second = _markers(evidence)
        if not _COUNT_RE.fullmatch(count):
            _fail("the package database state was malformed; refusing")
        if first not in _OWNED_MARKERS or second not in _OWNED_MARKERS:
            _fail("a report path state was malformed; refusing")
        package_absent = count == "0"
        data_absent = first == "ABSENT" and second == "ABSENT"
        self._package_absent_confirmed = package_absent
        self._data_absent_confirmed = data_absent
        return self._result(
            liveops.AbsenceResult,
            package_absent=package_absent, data_absent=data_absent)

    # ---- install / launch ----

    def _ensure_run_dir(self) -> None:
        if self._run_dir_created:
            return
        # The DUID pin is checked before the first staging/capture operation,
        # including the SDK group capture used to build the mkdir command.
        self._guard()
        try:
            group = self._channel.group_id()
        except Exception:
            self._poison()
            _fail("the SDK group could not be confirmed; refusing every operation")
        self._guard()
        output = None
        try:
            evidence = self._probe((commands.create_run_dir_command(
                liveops.remote_run_dir(self.run_id), group),))
            output = _markers(evidence)[0]
        except liveops.LiveOpsError:
            # The mkdir may have happened before the refusal: ownership is unknown.
            self._run_dir_owned = None
            raise
        if output == "EXISTS":
            self._run_dir_owned = False
            _fail("this run's staging directory already exists; refusing to touch it")
        match = re.fullmatch(
            r"([0-9]{1,10}):([0-9]{1,10}):([0-9]{1,10}):770", output)
        if (match is None or match.group(1) != match.group(2)
                or int(match.group(3)) != group):
            self._run_dir_owned = None
            _fail("this run's staging directory could not be verified; refusing")
        self._run_dir_owned = True
        self._run_dir_created = True

    def install_widget(self, package_id: str, wgt: bytes, sha256: str,
                       remote_tmp_wgt: str) -> liveops.InstallResult:
        package = self._require_package(commands._package(package_id))
        if type(wgt) is not bytes or len(wgt) > liveops.MAX_UPLOAD_BYTES:
            _fail("the signed widget is not a bounded byte string; refusing")
        if not is_sha256(sha256) or hashlib.sha256(wgt).hexdigest() != sha256:
            _fail("the signed widget does not match its pinned hash; refusing")
        expected_wgt = liveops.remote_wgt_path(liveops.remote_run_dir(self.run_id), package)
        if remote_tmp_wgt != expected_wgt:
            _fail("the widget path is not this run's fixed staging path; refusing")
        self._guard()
        self._ensure_run_dir()
        self._guard()
        try:
            self._channel.push_bytes(wgt, expected_wgt)
        except Exception:
            _fail("the widget upload did not succeed; refusing")
        self._uploaded_paths.add(expected_wgt)
        self._install_attempted = True
        self._guard()
        try:
            outcome = self._channel.install(package, expected_wgt)
        except Exception:
            _fail("the widget install did not complete; refusing")
        if outcome.returncode != 0 or outcome.reported_error:
            return self._result(liveops.InstallResult, pushed=True,
                                install_attempted=True, installed=False)
        self._guard()
        evidence = self._probe((commands.package_and_app_count_command(package),))
        counts = _markers(evidence)[0]
        match = _COUNTS_RE.fullmatch(counts)
        if match is None:
            _fail("the widget install could not be confirmed from the package database; refusing")
        installed = match.group(1) == "1" and match.group(2) == "1"
        if installed:
            self._install_confirmed = True
        return self._result(liveops.InstallResult, pushed=True,
                            install_attempted=True, installed=installed)

    def launch_facts(self, package_id: str) -> liveops.LaunchResult:
        package = self._require_package(commands._package(package_id))
        self._guard()
        self._probe((commands.launch_command(package),))
        return self._result(liveops.LaunchResult, launched=True)

    # ---- report read ----

    def read_report(self, package_id: str,
                    data_paths: tuple[str, str]) -> liveops.ReportResult:
        package = self._require_package(commands._package(package_id))
        fixed = liveops.data_paths(package)
        if type(data_paths) is not tuple or tuple(data_paths) != fixed:
            _fail("the report paths are not the fixed tool-computed paths; refusing")
        attempts = max(1, int(self._config.report_attempts))
        for attempt in range(attempts):
            self._guard()
            evidence = self._probe((commands.report_read_command(fixed[0]),
                                    commands.report_read_command(fixed[1])))
            present = []
            for marker in _markers(evidence):
                if marker == "!absent!":
                    continue
                if marker == "!symlink!":
                    _fail("a report path is a link; refusing to follow it")
                if marker == "!nonregular!":
                    _fail("a report path is not a regular file; refusing")
                data = _decode_base64(marker)
                if len(data) > liveops.MAX_REPORT_BYTES:
                    _fail("the ProductInfo report is larger than the accepted bound; refusing")
                present.append(data)
            if len(present) == 1:
                return self._result(liveops.ReportResult, data=present[0])
            if len(present) > 1:
                _fail("both report locations are populated; refusing an ambiguous report")
            if attempt + 1 < attempts:
                time.sleep(1.0)
        _fail("the ProductInfo report was not produced by this run; refusing")

    # ---- native inspector ----

    def push_native(self, artifacts: tuple[liveops.Upload, ...],
                    remote_tmp_dir: str) -> liveops.OpResult:
        if remote_tmp_dir != liveops.remote_run_dir(self.run_id):
            _fail("the inspector directory is not this run's fixed staging path; refusing")
        if not self._run_dir_created or self._run_dir_owned is not True:
            _fail("this session has not verified its own run directory; refusing")
        if type(artifacts) is not tuple or len(artifacts) != len(NATIVE_NAMES):
            _fail("the inspector bundle is not exactly the three fixed files; refusing")
        by_name: dict[str, liveops.Upload] = {}
        for upload in artifacts:
            if not isinstance(upload, liveops.Upload):
                _fail("the inspector bundle is malformed; refusing")
            name = upload.remote_path.rsplit("/", 1)[-1]
            if name not in NATIVE_NAMES or upload.remote_path != f"{remote_tmp_dir}/{name}":
                _fail("the inspector bundle path is not fixed; refusing")
            if name in by_name:
                _fail("the inspector bundle repeats a file; refusing")
            if type(upload.data) is not bytes or len(upload.data) > liveops.MAX_UPLOAD_BYTES:
                _fail("an inspector file is not a bounded byte string; refusing")
            if (not is_sha256(upload.sha256)
                    or hashlib.sha256(upload.data).hexdigest() != upload.sha256):
                _fail("an inspector file does not match its pinned hash; refusing")
            by_name[name] = upload
        self._guard()
        evidence = self._probe((commands.path_presence_command(remote_tmp_dir),))
        if _markers(evidence)[0] != "PRESENT":
            _fail("this run's staging directory is not present; refusing")
        for name in NATIVE_NAMES:
            upload = by_name[name]
            self._guard()
            try:
                self._channel.push_bytes(upload.data, upload.remote_path)
            except Exception:
                _fail("an inspector upload did not succeed; refusing")
            self._uploaded_paths.add(upload.remote_path)
        self._native_pins = {name: by_name[name].sha256 for name in NATIVE_NAMES}
        return self._result(liveops.OpResult)

    def run_native(self, remote_dll: str) -> liveops.NativeResult:
        run_dir = liveops.remote_run_dir(self.run_id)
        if remote_dll != liveops.remote_native_dll(run_dir):
            _fail("the inspector path is not this run's fixed path; refusing")
        if not self._run_dir_created or self._run_dir_owned is not True:
            _fail("this session has not verified its own run directory; refusing")
        pins = self._native_pins
        if (not isinstance(pins, dict) or set(pins) != set(NATIVE_NAMES)
                or any(type(value) is not str for value in pins.values())):
            _fail("this session has not pushed its pinned inspector bundle; refusing")
        self._guard()
        guard = commands.native_guard_run_command(
            run_dir, pins["QuietTVInspect.dll"],
            pins["QuietTVInspect.runtimeconfig.json"],
            pins["QuietTVInspect.deps.json"])
        evidence = self._probe((guard,),
                               command_seconds=self._config.native_seconds)
        marker = _markers(evidence)[0]
        if marker == "!bundle-mismatch!":
            _fail("the uploaded inspector bundle no longer matches its pinned hashes; refusing")
        if marker in ("!output-link!", "!output-nonregular!"):
            _fail("the native inventory output is not a regular file; refusing")
        match = _EXIT_RE.fullmatch(marker)
        if match is None:
            _fail("the native inventory run did not report an exit state; refusing")
        if int(match.group(1)) != 0:
            _fail("the native inventory run did not exit successfully; refusing")
        self._guard()
        size_text = _markers(self._probe((commands.native_size_command(run_dir),)))[0]
        if size_text == "!missing!":
            _fail("the native inventory run produced no frame; refusing")
        if size_text in ("!symlink!", "!nonregular!"):
            _fail("the native inventory output is not a regular file; refusing")
        if not _SIZE_RE.fullmatch(size_text):
            _fail("the native inventory output size was malformed; refusing")
        size = int(size_text)
        if size == 0 or size > liveops.MAX_NATIVE_FRAME_BYTES:
            _fail("the native inventory output is outside the accepted bound; refusing")
        chunk_count = commands.native_chunks(size)
        self._guard()
        chunks = _markers(self._probe(tuple(
            commands.native_chunk_command(run_dir, index)
            for index in range(chunk_count))))
        if len(chunks) != chunk_count:
            _fail("the native inventory output could not be read in full; refusing")
        if any(marker in ("!symlink!", "!nonregular!") for marker in chunks):
            _fail("the native inventory output is not a regular file; refusing")
        parts = []
        for index, marker in enumerate(chunks):
            data = _decode_base64(marker)
            expected = (commands.NATIVE_CHUNK_BYTES if index + 1 < chunk_count
                        else size - commands.NATIVE_CHUNK_BYTES * (chunk_count - 1))
            if len(data) != expected:
                _fail("the native inventory output was truncated; refusing")
            parts.append(data)
        stdout = _strip_exit_trailer(b"".join(parts))
        if len(stdout) > liveops.MAX_NATIVE_FRAME_BYTES:
            _fail("the native inventory output is outside the accepted bound; refusing")
        return self._result(liveops.NativeResult, exit_code=0, stdout=stdout)

    # ---- reconciliation ----

    def close_owned_run(self, owned: liveops.OwnedRun) -> liveops.CleanupReport:
        self._usable()
        if not isinstance(owned, liveops.OwnedRun):
            _fail("cleanup was not given this run's typed ownership record; recovery is needed")
        if owned.run_id != self.run_id or type(owned.run_id) is not str:
            _fail("cleanup was given another run's record; recovery is needed")
        package = commands._package(owned.package_id)
        if self._package_id is not None and self._package_id != package:
            _fail("cleanup was given another session's package; recovery is needed")
        data_paths = liveops.data_paths(package)
        run_dir = liveops.remote_run_dir(self.run_id)
        allowed = {liveops.remote_wgt_path(run_dir, package),
                   *(f"{run_dir}/{name}" for name in NATIVE_NAMES)}
        uploads = []
        seen = set()
        for upload in owned.uploads:
            if not isinstance(upload, liveops.Upload):
                _fail("cleanup was given a malformed upload record; recovery is needed")
            if upload.remote_path not in allowed or upload.remote_path in seen:
                _fail("cleanup was given an upload this run did not own; recovery is needed")
            if not is_sha256(upload.sha256):
                _fail("cleanup was given a malformed upload hash; recovery is needed")
            seen.add(upload.remote_path)
            uploads.append(upload)

        self._guard()
        evidence = self._probe((
            commands.package_count_command(package),
            commands.path_presence_command(data_paths[0]),
            commands.path_presence_command(data_paths[1]),
            commands.path_presence_command(run_dir),
            commands.path_presence_command(f"{run_dir}/{commands.NATIVE_OUT}"),
            *(commands.path_presence_command(upload.remote_path) for upload in uploads),
        ))
        markers = _markers(evidence)
        count = markers[0]
        if not _COUNT_RE.fullmatch(count):
            _fail("the package database state was malformed; recovery is needed")
        data_markers = markers[1:3]
        run_marker, scratch_marker = markers[3], markers[4]
        upload_markers = dict(zip((u.remote_path for u in uploads), markers[5:]))
        for marker in (*data_markers, run_marker, scratch_marker, *upload_markers.values()):
            if marker not in _OWNED_MARKERS:
                _fail("a cleanup path state was malformed; recovery is needed")

        package_present = count != "0"
        package_removed = False
        package_absent_after = not package_present
        recovery = False
        if package_present:
            if (self._package_absent_confirmed is not True
                    or self._install_confirmed is not True):
                # A failed, errored or partial install leaves ownership
                # ambiguous: never uninstall it or delete its data.
                return liveops.CleanupReport(
                    package_removed=False, package_absent_after=False,
                    removed_uploads=(), unconfirmed_uploads=tuple(
                        upload.remote_path for upload in uploads),
                    recovery_needed=True)
            self._guard()
            try:
                self._channel.uninstall(package)
            except Exception:
                _fail("the diagnostic package could not be removed; recovery is needed")
            self._guard()
            verify = _markers(self._probe(
                (commands.package_and_app_count_command(package),)))[0]
            match = _COUNTS_RE.fullmatch(verify)
            if match is None:
                _fail("the diagnostic package state could not be confirmed; recovery is needed")
            if match.group(1) == "0" and match.group(2) == "0":
                package_removed = True
                package_absent_after = True
            else:
                package_absent_after = False
                recovery = True

        removal_commands: list[str] = []
        removal_kinds: list[tuple[str, object]] = []
        removed_uploads: list[str] = []
        unconfirmed_uploads: list[str] = []
        clean = self._data_absent_confirmed is True
        for upload in uploads:
            marker = upload_markers[upload.remote_path]
            if marker == "ABSENT":
                removal_commands.append(
                    commands.remove_file_command(upload.remote_path, upload.sha256))
                removal_kinds.append(("upload", upload))
            elif (clean and self._run_dir_owned is True and package_absent_after
                    and upload.remote_path in self._uploaded_paths):
                removal_commands.append(
                    commands.remove_file_command(upload.remote_path, upload.sha256))
                removal_kinds.append(("upload", upload))
            else:
                unconfirmed_uploads.append(upload.remote_path)
                recovery = True

        for path, marker in zip(data_paths, data_markers):
            if marker == "ABSENT":
                continue
            if (marker == "PRESENT" and clean and self._install_confirmed
                    and self._run_dir_owned is not False):
                removal_commands.append(commands.remove_scratch_command(path))
                removal_kinds.append(("data", path))
            else:
                recovery = True

        if scratch_marker == "PRESENT":
            if clean and self._run_dir_owned is True:
                removal_commands.append(commands.remove_scratch_command(
                    f"{run_dir}/{commands.NATIVE_OUT}"))
                removal_kinds.append(("scratch", None))
            elif self._run_dir_owned is not False:
                recovery = True

        if self._run_dir_owned is True:
            removal_commands.append(commands.rmdir_command(run_dir))
            removal_kinds.append(("rmdir", None))
        elif run_marker != "ABSENT" and self._run_dir_owned is None:
            recovery = True

        if removal_commands:
            self._guard()
            results = _markers(self._probe(tuple(removal_commands)))
            if len(results) != len(removal_kinds):
                _fail("cleanup did not return the expected results; recovery is needed")
            for (kind, value), marker in zip(removal_kinds, results):
                if kind == "upload":
                    if marker in ("REMOVED", "ABSENT"):
                        removed_uploads.append(value.remote_path)
                    else:
                        unconfirmed_uploads.append(value.remote_path)
                        recovery = True
                elif marker not in ("REMOVED", "ABSENT"):
                    recovery = True

            self._guard()
            final = _markers(self._probe((
                commands.package_count_command(package),
                commands.path_presence_command(data_paths[0]),
                commands.path_presence_command(data_paths[1]),
                commands.path_presence_command(run_dir),
            )))
            if not _COUNT_RE.fullmatch(final[0]):
                _fail("the final package state was malformed; recovery is needed")
            if final[0] != "0":
                package_absent_after = False
                recovery = True
            if final[1] != "ABSENT" or final[2] != "ABSENT":
                recovery = True
            if self._run_dir_owned is True and final[3] != "ABSENT":
                recovery = True

        if recovery and not package_absent_after:
            package_absent_after = False
        if package_removed and not package_absent_after:
            recovery = True
        return liveops.CleanupReport(
            package_removed=package_removed,
            package_absent_after=package_absent_after,
            removed_uploads=tuple(removed_uploads),
            unconfirmed_uploads=tuple(dict.fromkeys(unconfirmed_uploads)),
            recovery_needed=recovery)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._channel.close()
        except Exception:
            pass


class ProviderOps(liveops.LiveOps):
    """The fixed provider; constructed only from a validated owner config."""

    name = PROVIDER_NAME
    implemented = True
    read_only = False
    write_scope = "own-run-namespace"

    def __init__(self, config: provider_config.ProviderConfig,
                 channel_factory=provider_channel.LiveChannel):
        self._config = config
        self._channel_factory = channel_factory

    def session(self, run_id: str) -> liveops.LiveSession:
        try:
            liveops.remote_run_dir(run_id)
        except liveops.LiveOpsError:
            raise liveops.LiveOpsError("the run id is malformed; refusing") from None
        try:
            channel = self._channel_factory(self._config)
        except Exception:
            raise liveops.LiveOpsError(
                "the provider channel could not be opened; refusing") from None
        session = ProviderSession(self._config, channel)
        session.run_id = run_id
        return session


def load_provider(path_value: str, channel_factory=provider_channel.LiveChannel) -> ProviderOps:
    """Load one validated owner config and return the fixed provider."""
    config = provider_config.load_config(path_value)
    return ProviderOps(config, channel_factory)

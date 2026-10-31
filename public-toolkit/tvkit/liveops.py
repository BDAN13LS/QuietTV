"""Typed, finite privileged-operation contract for one live report collection.

The default registry has no active adapter. An explicitly configured reviewed
provider in :mod:`tvkit.provider`, or a trusted parent adapter, supplies the
TV channel through this finite contract. Collection operates only on
tool-computed values; configuration never loads an arbitrary plugin.

What this contract deliberately is NOT
--------------------------------------

* It is not a shell or command runner. Every operation is a fixed verb with
  fixed argument shapes; there is no free-form command, path override, SERIAL,
  key or callback token parameter.
* It is not a target picker. The adapter binds ONE owner target for the whole
  ``session()``; the public side never names or learns the TV address.
* It is not a freshness proof. ``session_token`` is opaque correlation chosen
  by the adapter, not a cryptographic nonce; a trusted adapter is trusted code.
  Freshness is established by the collector's ordering (namespace absence
  asserted immediately before this run's install/launch, then a read in the
  same session), never by comparing timestamps or byte digests to older runs:
  two runs may legitimately return IDENTICAL fresh bytes.
* It is not a Home/launcher API. Nothing here changes Home, boot or startup,
  and no operation may stop a shared SDK server or disconnect another session.

Writes are limited to this run's own namespace: the tool-computed package id,
the exact remote upload paths handed in, and nothing else.

Binding stability
-----------------

``session_token``/``binding`` are opaque and identity-compared only. The
collector freezes the pair once at session open, checks each result against it
immediately after every operation, re-checks the live pair before every
subsequent operation, and at cleanup asks only the session that still holds the
initial pair to reconcile its own namespace. No async-provider race guarantee is
offered: an adapter that changes its target mid-session is refused before the
next operation, and a changed target is never "fixed" by uninstalling anything.
"""

from __future__ import annotations

import abc
import re
from dataclasses import dataclass
from typing import Callable

# ---- tool-computed bounds (the adapter must enforce them before returning) ----

MAX_REPORT_BYTES = 256 * 1024          # factsjoin refuses a report larger than this
MAX_REPORT_READ_BYTES = MAX_REPORT_BYTES + 1  # adapter reads at most cap+1 to report oversize
MAX_NATIVE_FRAME_BYTES = 4 * 1024 * 1024      # one framed stdout capture, bounded
MAX_UPLOAD_BYTES = 16 * 1024 * 1024           # one pushed artifact, bounded

# Fixed data-path templates. Only the validated 10-character package id is
# substituted; a caller never supplies a remote path.
DATA_PATH_TEMPLATES = (
    "/opt/usr/home/owner/apps_rw/{package}/data/device-facts.json",
    "/opt/usr/apps/{package}/data/device-facts.json",
)
# The reference Samsung SDK refuses pushes to /tmp; this is its tested staging root.
REMOTE_TMP_ROOT = "/home/owner/share/tmp/sdk_tools"
_RUN_ID_RE = re.compile(r"[0-9a-f]{32}\Z")


class LiveOpsError(ValueError):
    """A refusal from the adapter. Its message is fixed/non-sensitive.

    It never carries a SERIAL, target address, path, report byte, DUID, session
    value or credential; the collector wraps it in its own finite refusal.
    """


def data_paths(package_id: str) -> tuple[str, str]:
    """The two fixed report locations for ``package_id``. No caller path input."""
    if type(package_id) is not str or not package_id:
        raise LiveOpsError("a package id is required to compute the report paths")
    return tuple(template.format(package=package_id) for template in DATA_PATH_TEMPLATES)  # type: ignore[return-value]


def remote_run_dir(run_id: str) -> str:
    """The tool-owned remote staging directory for one run: fixed root + random run id."""
    if type(run_id) is not str or not _RUN_ID_RE.fullmatch(run_id):
        raise LiveOpsError("the run id is malformed")
    return f"{REMOTE_TMP_ROOT}/quiettv-collect-{run_id}"


def remote_wgt_path(run_dir: str, package_id: str) -> str:
    """The exact remote .wgt path this run pushes. Fixed shape, tool-computed."""
    if type(run_dir) is not str or not run_dir.startswith(REMOTE_TMP_ROOT + "/quiettv-collect-"):
        raise LiveOpsError("the remote staging directory is malformed")
    if type(package_id) is not str or not re.fullmatch(r"[A-Za-z0-9]{10}", package_id):
        raise LiveOpsError("the package id is malformed")
    return f"{run_dir}/{package_id}.wgt"


def remote_native_dll(run_dir: str) -> str:
    """The exact remote inspector DLL path: fixed name inside the run's own directory."""
    if type(run_dir) is not str or not run_dir.startswith(REMOTE_TMP_ROOT + "/quiettv-collect-"):
        raise LiveOpsError("the remote staging directory is malformed")
    return f"{run_dir}/QuietTVInspect.dll"


# ---- result objects -------------------------------------------------------
# Every result echoes the run id, the adapter's opaque session token and the
# session's opaque in-memory binding. The collector refuses mixed values; it
# never inspects the binding, prints it or serialises it.


@dataclass(frozen=True)
class OpResult:
    """Base result for an operation that reports success or a refusal."""

    ok: bool
    run_id: str
    session_token: str
    binding: object  # opaque; identity-compared only


@dataclass(frozen=True)
class AbsenceResult(OpResult):
    """Whether this run starts from a clean namespace (package AND data absent)."""

    package_absent: bool
    data_absent: bool

    @property
    def absent(self) -> bool:
        return self.package_absent and self.data_absent


@dataclass(frozen=True)
class InstallResult(OpResult):
    """What the adapter actually did: pushed, attempted install, read the package back."""

    pushed: bool
    install_attempted: bool
    installed: bool


@dataclass(frozen=True)
class LaunchResult(OpResult):
    """The fixed ``<package>.Facts`` launch this run performed."""

    launched: bool


@dataclass(frozen=True)
class ReportResult(OpResult):
    """Bounded report bytes read in THIS session. The collector parses these bytes."""

    data: bytes


@dataclass(frozen=True)
class NativeResult(OpResult):
    """The framed native run: exit code, bounded stdout (one trailer already stripped)."""

    exit_code: int
    stdout: bytes


@dataclass(frozen=True)
class Upload:
    """One artifact this run owns remotely: exact path, pinned hash, verified bytes.

    The adapter pushes ``data`` (never re-reads a mutable local path), so a file
    swapped after hashing cannot change what reaches the TV.
    """

    remote_path: str
    sha256: str
    data: bytes


@dataclass(frozen=True)
class OwnedRun:
    """What the collector knows it asked the adapter to do. Input to reconciliation.

    ``absence_confirmed`` is true only when ``assert_absent`` passed immediately
    before this run's install. ``install_attempted`` is set before the install
    call, so a failed or partial install is still reconciled. ``uploads`` lists
    only exact paths this run pushed or was about to push.
    """

    run_id: str
    package_id: str
    absence_confirmed: bool
    install_attempted: bool
    uploads: tuple[Upload, ...]


@dataclass(frozen=True)
class CleanupReport:
    """The adapter's reconciliation of one run's namespace.

    The adapter must verify package ownership (the exact package id was absent
    before this run) before uninstalling, and must remove only the exact upload
    paths listed in the :class:`OwnedRun`, re-checked by their expected hash.
    ``recovery_needed`` is true when it could not confirm either half.

    A failed or partial install may leave no package behind: reporting
    ``package_removed=False`` with ``package_absent_after=True`` is a confirmed
    clean state (no mutation was needed), not a failure.
    """

    package_removed: bool
    package_absent_after: bool
    removed_uploads: tuple[str, ...]
    unconfirmed_uploads: tuple[str, ...]
    recovery_needed: bool


class LiveSession(abc.ABC):
    """One provider session bound to ONE target. Use as a context manager.

    ``close_owned_run`` reconciles this run's own namespace before ``close``;
    ``close`` releases only this session's resources and must never disconnect a
    shared SDK client, kill a server, or change a host setting.

    The token and binding must stay stable for the session's whole life. The
    collector freezes them at open, refuses before the next operation if they
    change, and will not call ``close_owned_run`` on a session whose pair no
    longer matches, so a drifted target can never be uninstalled.
    """

    run_id: str = ""

    @property
    @abc.abstractmethod
    def session_token(self) -> str:
        """Adapter-chosen opaque correlation value. Never printed or serialised."""

    @property
    @abc.abstractmethod
    def binding(self) -> object:
        """Opaque in-memory target binding. Identity-compared, never inspected."""

    @abc.abstractmethod
    def assert_absent(self, package_id: str, data_paths: tuple[str, str]) -> AbsenceResult:
        """True only if the package is not installed and neither data path has bytes."""

    @abc.abstractmethod
    def install_widget(self, package_id: str, wgt: bytes, sha256: str, remote_tmp_wgt: str) -> InstallResult:
        """Push the verified bytes, then run the fixed vendor install for this package."""

    @abc.abstractmethod
    def launch_facts(self, package_id: str) -> LaunchResult:
        """Run the fixed ``<package>.Facts`` launch."""

    @abc.abstractmethod
    def read_report(self, package_id: str, data_paths: tuple[str, str]) -> ReportResult:
        """Read at most ``MAX_REPORT_READ_BYTES`` from the two fixed data paths."""

    @abc.abstractmethod
    def push_native(self, artifacts: tuple[Upload, ...], remote_tmp_dir: str) -> OpResult:
        """Push the fixed inspector artifact bundle into this run's remote directory."""

    @abc.abstractmethod
    def run_native(self, remote_dll: str) -> NativeResult:
        """Run ``dotnet <remote_dll> --inventory-framed``; strip exactly one ``[exit:N]`` trailer."""

    @abc.abstractmethod
    def close_owned_run(self, owned: OwnedRun) -> CleanupReport:
        """Reconcile and remove only this run's proven-owned package state and uploads."""

    @abc.abstractmethod
    def close(self) -> None:
        """Release only this session's resources."""

    def __enter__(self) -> "LiveSession":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False


class LiveOps(abc.ABC):
    """The privileged-operation provider. Empty registry: the toolkit ships none."""

    name = "abstract"
    implemented = False
    read_only = False
    write_scope = "own-run-namespace"

    @abc.abstractmethod
    def session(self, run_id: str) -> LiveSession:
        """Open the single session for this run, bound to one owner target."""


# Empty on purpose. A parent's private adapter registers itself here from its
# own reviewed code; nothing in this repository does.
LIVEOPS: dict[str, Callable[[], LiveOps]] = {}


def get_liveops(name: str | None) -> LiveOps:
    """Return a registered, implemented adapter or refuse. Mirrors ``get_transport``."""
    if not name or type(name) is not str:
        raise LiveOpsError("no live-operations adapter named; this toolkit ships none")
    factory = LIVEOPS.get(name)
    if factory is None:
        raise LiveOpsError("no implemented live-operations adapter is registered; refusing")
    try:
        ops = factory()
    except Exception:
        # Never echo a factory's own message: it could carry a credential or target.
        raise LiveOpsError("the live-operations adapter could not be constructed") from None
    if not isinstance(ops, LiveOps):
        raise LiveOpsError("the live-operations adapter is not implemented; refusing")
    try:
        implemented = ops.implemented
    except Exception:
        raise LiveOpsError("the live-operations adapter is not implemented; refusing") from None
    if implemented is not True:
        raise LiveOpsError("the live-operations adapter is not implemented; refusing")
    return ops

"""QuietTV Rescue: restore-only, offline, on the public tvkit engine.

READ THIS FIRST. In this build Rescue CANNOT recover a real TV by itself:
no transport adapter ships (``tvkit.transport.TRANSPORTS`` is empty) and no
cached bootstrap/root method is bundled (deferred to D5b publishing). There
is NO hard-brick guarantee. If the TV will not start, use Samsung's own
recovery options (Self Diagnosis / Reset in Settings, or Samsung support).

What it does today:
  validate  re-read and verify a capsule offline (optionally against facts)
  plan      print the restore plan (dry run, changes nothing)
  restore   dry run unless --apply; --apply resolves --transport only from
            the empty TRANSPORTS registry, so it refuses (exit 3)
  rehearse  same gate, restores into the TV rehearsal area only

The library entry points ``restore`` and ``rehearse`` accept an injected,
implemented ``RescueTransport`` instance (tests, and the owner's own code
later). Nothing here loads adapter code from a path or from user config.

Gate order for every device run, nothing written before the end of it:
  1. capsule.validate(path) with no facts (a partial backup stops here)
  2. fresh facts from the transport -> parse_device_facts -> match_device
  3. refuse tvkit-profile/2 (capsule/1 cannot bind firmwareBuild/region)
  4. capsule.validate(path, facts) again: bytes and identity re-verified
  5. refuse ourPackages and systemKeyChanges (lifecycle/boot: not this slice)
  6. typed inspection of every target; present-and-different refuses
  7. every capability the run needs, checked before the first write
  8. facts re-read and required identical, then run_plan on the plan cut
     just BEFORE its first manual-acceptance step

Owner acceptance is never synthesized: the best outcome is
"adapter-verified, owner acceptance pending" (exit 4), never "restored".

After the run every restored target is read back and compared again; a
step that reported success without changing anything stops with exit 5.
Once the first device step is handed to the adapter, any stop (an
unverified step, an adapter exception, a failed read-back) is exit 5,
never "nothing changed": a step that raised may still have written.

Exit codes: 0 ok (validate, dry run, rehearsal), 1 refused (nothing
changed), 2 usage, 3 no implemented adapter (nothing changed), 4
adapter-verified (or already matching) and the owner's own checks are
still pending, 5 stopped partway or failed read-back (the TV may be changed).
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

_TOOLKIT = Path(__file__).resolve().parent.parent / "public-toolkit"
if _TOOLKIT.is_dir() and str(_TOOLKIT) not in sys.path:
    sys.path.insert(0, str(_TOOLKIT))

from tvkit import strictjson as sj  # noqa: E402
from tvkit.capsule import Capsule, CapsuleError, Entry, Package, RegistryChange, validate  # noqa: E402
from tvkit.plan import Plan, restore_plan, setup_plan  # noqa: E402
from tvkit.profile import (PROFILE_SCHEMA, Profile, ProfileError, get_profile,  # noqa: E402
                           load_profile, match_device, parse_device_facts)
from tvkit.transport import (TRANSPORTS, StepFailed, Transport, TransportUnavailable,  # noqa: E402
                             get_transport, run_plan)

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2
EXIT_NO_ADAPTER = 3
EXIT_PENDING_OWNER = 4
EXIT_STOPPED = 5

MANUAL = "manual-acceptance"
INSPECT_TARGET = "inspect-path"
REGISTRY_READ = "registry-read"
REHEARSAL_GATE = "rehearsal-passed"
# A rehearsal may only touch the scratch area. Any of these in it is a bug.
REHEARSAL_FORBIDDEN = frozenset({
    "run-guarded-sql", "run-guarded-sql-on-copy", "system-key", "system-key-read",
    "move-into-place", "install-package", "remove-package", MANUAL,
})

BANNER = (
    "QuietTV Rescue (restore-only, offline). This build CANNOT recover a real TV by itself: "
    "no transport adapter ships and no cached bootstrap/root method is bundled yet (D5b). "
    "No hard-brick guarantee. If the TV will not start, use Samsung's own recovery "
    "(Settings > Support > Self Diagnosis / Reset, or Samsung support)."
)


class RescueRefused(RuntimeError):
    """A gate refused the run before any write. Nothing was changed."""


class RunStopped(RuntimeError):
    """Execution started (a device step was attempted) and then stopped. The TV may be changed.

    ``completed`` counts plan steps that finished verified; ``attempted`` counts
    device steps handed to the adapter, including one that raised. A step that
    raised may still have written before it raised, so it never counts as "no change".
    """

    def __init__(self, message: str, completed: int, attempted: int = 0, total: int = 0):
        super().__init__(message)
        self.completed = completed
        self.attempted = attempted
        self.total = total


class ReadbackFailed(RunStopped):
    """Every step reported success but the target does not match. The TV may be changed."""


# --------------------------------------------------------------------------
# Typed evidence and the transport contract Rescue needs


@dataclass(frozen=True)
class TargetEvidence:
    """What is at a package's install root now, as read from the TV.

    Rescue decides "identical" itself from ``entries`` and the xattr dump
    hash; a transport never reports identity as a boolean.
    """
    package_id: str
    install_root: str
    state: str  # "absent" | "present"
    entries: tuple[Entry, ...] = ()
    xattr_manifest_sha256: str | None = None  # SHA-256 of an XattrBackup v1 dump, same format
    version: str | None = None  # informational only, for the refusal message


@dataclass(frozen=True)
class RegistryEvidence:
    app_id: str
    fields: Mapping[str, str]


class RescueTransport(Transport):
    """A transport that can also read the evidence Rescue needs before writing."""

    def inspect_target(self, package: Package) -> TargetEvidence:
        raise TransportUnavailable("adapter cannot inspect install targets")

    def read_registry(self, app_id: str, fields: Sequence[str]) -> RegistryEvidence:
        raise TransportUnavailable("adapter cannot read registry fields")


@dataclass
class RescueResult:
    status: str  # nothing-to-do | pending-owner-acceptance | rehearsed
    completed: int
    total: int
    skipped_packages: tuple[str, ...] = ()
    skipped_registry: tuple[str, ...] = ()
    pending: tuple[str, ...] = ()
    not_covered: tuple[str, ...] = ()
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Gate


def _require_transport(transport: Any) -> RescueTransport:
    if not isinstance(transport, RescueTransport):
        raise TransportUnavailable("Rescue needs a RescueTransport (typed inspection); refusing")
    if transport.implemented is not True:
        raise TransportUnavailable(f"transport adapter {transport.name!r} is not implemented")
    if not isinstance(transport.capabilities, (frozenset, set)):
        raise TransportUnavailable("transport capabilities must be a set of names")
    return transport


def _fresh_facts(transport: RescueTransport, profile: Profile) -> dict[str, str]:
    facts = parse_device_facts(dict(transport.read_device_facts()))
    match_device(profile, facts)
    if profile.schema != PROFILE_SCHEMA:
        raise RescueRefused(
            f"{profile.schema} profiles need capsule v2 (firmwareBuild/region binding), "
            "which Rescue does not support yet; refusing")
    return facts


def _expect_scope(capsule: Capsule) -> None:
    if capsule.our_packages:
        raise RescueRefused(
            "capsule lists ourPackages; removing the launcher or root helper needs an "
            "independently accepted final control check, which this Rescue slice does not do; refusing")
    if capsule.system_key_changes:
        raise RescueRefused("capsule has startup/Home key changes (boot changes); "
                            "not supported by this Rescue slice; refusing")


def _check_target(package: Package, evidence: Any) -> bool:
    """True if the target already matches the capsule exactly (skip), False if absent.

    Anything else refuses the whole run.
    """
    pid = package.package_id
    if type(evidence) is not TargetEvidence:
        raise RescueRefused(f"{pid}: adapter returned no typed target evidence; refusing")
    if evidence.package_id != pid or evidence.install_root != package.install_root:
        raise RescueRefused(f"{pid}: target evidence is for a different package or path; refusing")
    if evidence.state == "absent":
        if evidence.entries or evidence.xattr_manifest_sha256 is not None:
            raise RescueRefused(f"{pid}: target evidence is contradictory; refusing")
        return False
    if evidence.state != "present":
        raise RescueRefused(f"{pid}: target evidence state is not absent or present; refusing")
    if not isinstance(evidence.entries, tuple) or any(type(e) is not Entry for e in evidence.entries):
        raise RescueRefused(f"{pid}: target evidence entries are not typed; refusing")
    same_tree = tuple(sorted(evidence.entries, key=lambda e: e.path)) == package.entries
    same_xattrs = evidence.xattr_manifest_sha256 == package.xattr_manifest.sha256
    if same_tree and same_xattrs:
        return True
    version = f" (installed version {evidence.version})" if evidence.version else ""
    raise RescueRefused(
        f"{pid} is present and differs from the capsule{version}; it may be newer. "
        "Not overwriting it; nothing was changed")


def _check_registry(change: RegistryChange, evidence: Any) -> bool:
    """Three-way: all fields at ``before`` -> skip; all at ``applied`` -> restore; else refuse."""
    if (type(evidence) is not RegistryEvidence or evidence.app_id != change.app_id
            or not isinstance(evidence.fields, Mapping)
            or any(type(evidence.fields.get(name)) is not str for name in change.fields)):
        raise RescueRefused(f"{change.app_id}: adapter returned no typed registry evidence; refusing")
    current = {name: evidence.fields.get(name) for name in change.fields}
    if all(current[name] == pair[0] for name, pair in change.fields.items()):
        return True
    if all(current[name] == pair[1] for name, pair in change.fields.items()):
        return False
    raise RescueRefused(f"{change.app_id}: registry fields changed since the backup; "
                        "not overwriting someone else's change; nothing was changed")


def _cut_before_manual(plan: Plan) -> tuple[Plan, tuple[str, ...]]:
    for index, step in enumerate(plan.steps):
        if step.requires == MANUAL:
            pending = tuple(s.action for s in plan.steps[index:])
            return dataclasses.replace(plan, steps=plan.steps[:index]), pending
    return plan, ()


def _preflight(path: str | Path, profile: Profile, transport: Any) -> tuple[RescueTransport, Capsule, dict]:
    # 1. The stored copy is whole before the TV is even asked anything.
    validate(path, profile, None)
    transport = _require_transport(transport)
    # 2-3. Fresh facts, exact match, v2 refused.
    facts = _fresh_facts(transport, profile)
    # 4. Re-read the capsule against those facts: bytes and identity verified.
    capsule = validate(path, profile, facts)
    if not (capsule.payload_verified and capsule.device_verified):
        raise RescueRefused("capsule payload and device identity were not both verified; refusing")
    # 5. Scope of this slice.
    _expect_scope(capsule)
    return transport, capsule, facts


def _require_capabilities(transport: RescueTransport, needed: set[str]) -> None:
    missing = needed - set(transport.capabilities)
    if missing:
        raise TransportUnavailable(
            f"adapter {transport.name!r} lacks capabilities: {', '.join(sorted(missing))}")


class _Tracked(Transport):
    """Forwards to the real adapter and records which device steps were handed to it."""

    def __init__(self, inner: RescueTransport):
        self.inner = inner
        self.name = inner.name
        self.implemented = inner.implemented
        self.capabilities = frozenset(inner.capabilities)
        self.attempted: list[Any] = []

    def read_device_facts(self):
        return self.inner.read_device_facts()

    def run(self, step):
        self.attempted.append(step)
        return self.inner.run(step)


def _execute(plan: Plan, transport: RescueTransport, capsule: Capsule, read_back=None) -> int:
    """run_plan, then the optional read-back, reporting any stop after the first device step as RunStopped.

    Before the first device step is handed over, errors pass through unchanged
    (nothing was attempted). From then on every adapter exception, unverified
    step or failed read-back means the TV may be changed.
    """
    tracked = _Tracked(transport)
    total = len(plan.steps)
    completed = None
    try:
        completed = run_plan(plan, tracked, capsule_verified=capsule.payload_verified,
                             device_verified=capsule.device_verified)
        if read_back is not None:
            read_back(completed)
        return completed
    except RunStopped as exc:
        exc.attempted, exc.total = len(tracked.attempted), total
        raise
    except Exception as exc:
        if not tracked.attempted:
            raise
        if completed is None:
            last = tracked.attempted[-1]
            done = plan.steps.index(last)
            where = f"step {last.number} ({last.action} {last.target})"
        else:
            done, where = completed, "read-back after the run"
        if isinstance(exc, StepFailed):
            message = str(exc)
        else:
            message = f"{where} raised {type(exc).__name__}: {exc}"
        raise RunStopped(message, done, len(tracked.attempted), total) from exc


def _same_device(transport: RescueTransport, profile: Profile, facts: dict[str, str]) -> None:
    again = _fresh_facts(transport, profile)
    if again != facts:
        raise RescueRefused("device facts changed between checks; refusing before any write")


# --------------------------------------------------------------------------
# Library entry points


def restore(path: str | Path, profile: Profile, transport: Any) -> RescueResult:
    transport, capsule, facts = _preflight(path, profile, transport)
    full = restore_plan(capsule, profile)
    front, pending = _cut_before_manual(full)
    # 7. Every capability, from the uncut-by-skips plan, before any read of targets.
    needed = front.capabilities() | {"read-device-facts"}
    if capsule.packages:
        needed.add(INSPECT_TARGET)
    if capsule.registry_changes:
        needed.add(REGISTRY_READ)
    _require_capabilities(transport, needed)

    # 6. Inspect every target before any write.
    remaining_packages, skipped_packages = [], []
    for package in capsule.packages:
        if _check_target(package, transport.inspect_target(package)):
            skipped_packages.append(package.package_id)
        else:
            remaining_packages.append(package)
    remaining_registry, skipped_registry = [], []
    for change in capsule.registry_changes:
        evidence = transport.read_registry(change.app_id, tuple(change.fields))
        if _check_registry(change, evidence):
            skipped_registry.append(change.app_id)
        else:
            remaining_registry.append(change)

    if not remaining_packages and not remaining_registry:
        # No record says the owner ever accepted an earlier run, so the checks stay pending.
        return RescueResult("nothing-to-do", 0, 0, tuple(skipped_packages), tuple(skipped_registry), pending,
                            notes=["every target already matches the capsule (adapter evidence); no writes",
                                   "the owner's own checks are still pending"])

    todo = dataclasses.replace(capsule, packages=tuple(remaining_packages),
                               registry_changes=tuple(remaining_registry))
    plan, pending = _cut_before_manual(restore_plan(todo, profile))
    # 8. Same TV, then the shared gate: capabilities again, stop at first unverified step.
    _same_device(transport, profile, facts)
    completed = _execute(plan, transport, capsule, lambda done: _read_back(
        transport, remaining_packages, remaining_registry, done))
    return RescueResult("pending-owner-acceptance", completed, len(plan.steps),
                        tuple(skipped_packages), tuple(skipped_registry), pending,
                        notes=["adapter-verified only; the owner's own checks have NOT passed"])


def _read_back(transport: RescueTransport, packages: Sequence[Package],
               changes: Sequence[RegistryChange], completed: int) -> None:
    """Steps reporting success is not enough: read every restored target again."""
    for package in packages:
        try:
            identical = _check_target(package, transport.inspect_target(package))
        except RescueRefused:
            identical = False
        if not identical:
            raise ReadbackFailed(f"{package.package_id}: adapter reported success but the target "
                                 "does not match the capsule", completed)
    for change in changes:
        try:
            restored = _check_registry(change, transport.read_registry(change.app_id, tuple(change.fields)))
        except RescueRefused:
            restored = False
        if not restored:
            raise ReadbackFailed(f"{change.app_id}: adapter reported success but the registry "
                                 "fields are not back to their backed-up values", completed)


def rehearsal_plan(capsule: Capsule, profile: Profile) -> tuple[Plan, tuple[str, ...]]:
    """The package part of the setup plan's rehearsal, cut at its gate."""
    full = setup_plan(capsule, profile, None)
    steps, not_covered = [], []
    for step in full.steps:
        if step.action == REHEARSAL_GATE:
            break
        if step.phase != "rehearsal":
            continue
        if step.requires in ("run-guarded-sql-on-copy", "system-key-read"):
            not_covered.append(step.action)  # expects the pre-setup state, not Rescue's
            continue
        steps.append(step)
    for step in steps:
        if step.requires in REHEARSAL_FORBIDDEN:
            raise RescueRefused(f"rehearsal plan contains {step.requires}; refusing")
    return dataclasses.replace(full, kind="rescue-rehearsal", steps=steps), tuple(not_covered)


def rehearse(path: str | Path, profile: Profile, transport: Any) -> RescueResult:
    transport, capsule, facts = _preflight(path, profile, transport)
    plan, not_covered = rehearsal_plan(capsule, profile)
    _require_capabilities(transport, plan.capabilities() | {"read-device-facts"})
    _same_device(transport, profile, facts)
    completed = _execute(plan, transport, capsule)
    return RescueResult("rehearsed", completed, len(plan.steps), not_covered=not_covered,
                        notes=["scratch area only; nothing moved into place"])


# --------------------------------------------------------------------------
# CLI


def _profile(args) -> Profile:
    if args.profile_file:
        return load_profile(args.profile_file)
    if not args.profile:
        raise ProfileError("--profile or --profile-file is required")
    return get_profile(args.profile)


def _facts(path: str | None):
    return None if not path else parse_device_facts(sj.load_file(path, 64 * 1024))


def _print_plan(plan: Plan, pending: Sequence[str]) -> None:
    for step in plan.steps:
        print(f"  {step.number:>3}. [{step.phase}] {step.action} {step.target} (needs {step.requires})")
    for action in pending:
        print(f"   --  {action}: owner checks it by hand; Rescue never marks it passed")


def cmd_validate(args) -> int:
    capsule = validate(args.capsule, _profile(args), _facts(args.device_facts))
    device = "VERIFIED" if capsule.device_verified else "NOT checked (no --device-facts)"
    print(f"capsule OK: {capsule.counts}; device identity {device}")
    return EXIT_OK


def cmd_plan(args) -> int:
    profile = _profile(args)
    capsule = validate(args.capsule, profile, _facts(args.device_facts))
    _expect_scope(capsule)
    if args.command == "rehearse":
        plan, not_covered = rehearsal_plan(capsule, profile)
        pending = tuple(f"{a} (not rehearsed in this slice)" for a in not_covered)
    else:
        plan, pending = _cut_before_manual(restore_plan(capsule, profile))
    print(f"DRY RUN {plan.kind} plan for {profile.profile_id} (identical targets are skipped at run time):")
    _print_plan(plan, pending)
    for item in profile.manual_prerequisites:
        print(f"  prerequisite: {item}")
    if args.command in ("restore", "rehearse") and not args.apply:
        print("DRY RUN: nothing was changed. --apply needs an implemented transport adapter; none ships.")
    return EXIT_OK


def cmd_run(args) -> int:
    if not args.apply:
        return cmd_plan(args)
    profile = _profile(args)
    validate(args.capsule, profile, None)
    transport = get_transport(args.transport)  # TRANSPORTS is empty: refuses here
    run = rehearse if args.command == "rehearse" else restore
    result = run(args.capsule, profile, transport)
    print(f"{result.status}: {result.completed} of {result.total} steps adapter-verified")
    for note in result.notes:
        print(f"  {note}")
    for action in result.pending:
        print(f"  pending owner check: {action}")
    return EXIT_PENDING_OWNER if result.pending else EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rescue", description=BANNER)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, helptext in (("validate", "re-read and verify a capsule offline"),
                           ("plan", "print the restore plan (dry run)"),
                           ("restore", "restore; dry run unless --apply"),
                           ("rehearse", "restore into the TV rehearsal area; dry run unless --apply")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--capsule", required=True)
        p.add_argument("--profile", help="built-in profile id")
        p.add_argument("--profile-file", help="profile JSON file")
        p.add_argument("--device-facts", help="facts JSON for an offline identity check")
        if name in ("restore", "rehearse"):
            p.add_argument("--apply", action="store_true")
            p.add_argument("--transport", help="registered adapter name (none ships)")
            p.set_defaults(func=cmd_run)
        else:
            p.set_defaults(func=cmd_validate if name == "validate" else cmd_plan, apply=False)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    print(BANNER, file=sys.stderr)
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else EXIT_OK
    try:
        return args.func(args)
    except RunStopped as exc:
        print(f"STOPPED: {exc}. {exc.completed} of {exc.total} plan steps finished verified; "
              f"{exc.attempted} device steps were handed to the adapter. The run is NOT complete "
              "and the TV may be partly changed (a step that raised may still have written).",
              file=sys.stderr)
        return EXIT_STOPPED
    except TransportUnavailable as exc:
        print(f"refused: {exc}. Nothing was changed on any device. "
              f"({len(TRANSPORTS)} adapters registered.)", file=sys.stderr)
        return EXIT_NO_ADAPTER
    except StepFailed as exc:
        print(f"STOPPED: {exc}. {exc.completed} earlier steps ran; the restore is NOT complete "
              "and the TV may be partly changed.", file=sys.stderr)
        return EXIT_STOPPED
    except (RescueRefused, CapsuleError, ProfileError, OSError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())

"""Command line: doctor, profiles, capsule validate/assemble/bundle, fetch, plan, apply, export.

Every command is read-only or dry-run by default. ``apply`` changes nothing
without ``--apply``, and with it still needs an implemented transport
adapter plus a capsule verified against live device facts. Exit codes:
0 ok, 1 refused/invalid, 2 usage, 3 no implemented adapter (nothing changed).
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
from pathlib import Path
from typing import Sequence

from . import __version__
from . import strictjson as sj
from .assemble import assemble, bundle
from .capsule import Capsule, CapsuleError, validate
from .config import PROJECT_REPO_URL, TEMPLATE, ConfigError, load_config, unconfigured
from .export import ExportError, build_report, load_allowlist, load_deny_values, write_export
from .fetch import FetchError, fetch
from .frontend_export import TransformError, load_spec
from .plan import Plan, restore_plan, setup_plan
from .profile import ProfileError, builtin_profiles, get_profile, parse_device_facts
from .transport import TRANSPORTS, StepFailed, TransportUnavailable, get_transport, run_plan

EXIT_OK, EXIT_REFUSED, EXIT_USAGE, EXIT_NO_ADAPTER = 0, 1, 2, 3
DEFAULT_ALLOWLIST = Path(__file__).resolve().parent.parent / "export-allowlist.json"
DEFAULT_FRONTEND_SPEC = Path(__file__).resolve().parent.parent / "frontend-export.json"
DEFAULT_SOURCE = Path(__file__).resolve().parent.parent.parent


def _profile(args):
    if args.profile:
        return get_profile(args.profile)
    profiles = builtin_profiles()
    if len(profiles) != 1:
        raise ProfileError("more than one profile is installed; pass --profile")
    return next(iter(profiles.values()))


def _facts(path: str | None):
    if not path:
        return None
    try:
        return parse_device_facts(sj.load_file(path, 64 * 1024))
    except sj.StrictJsonError as exc:
        raise ProfileError(str(exc)) from exc


def _print_plan(plan: Plan, as_json: bool) -> None:
    if as_json:
        print(json.dumps(plan.to_json(), indent=2))
        return
    print(f"{plan.kind.upper()} PLAN for {plan.profile_id} (dry run; nothing is changed)")
    for step in plan.steps:
        print(f"  {step.number:3d}. [{step.phase}] {step.action} {step.target}  (needs: {step.requires})")
    print("Invariants:")
    for line in plan.invariants:
        print(f"  - {line}")
    print("Manual prerequisites the owner must satisfy:")
    for line in plan.manual_prerequisites:
        print(f"  - {line}")


def _summary(capsule: Capsule) -> str:
    counts = ", ".join(f"{k}={v}" for k, v in capsule.counts.items())
    device = "device identity VERIFIED" if capsule.device_verified else "device identity NOT checked (no --device-facts)"
    return f"capsule valid: {counts}; {device}"


def cmd_doctor(args) -> int:
    ok = True
    print(f"tvkit {__version__} on Python {platform.python_version()} ({platform.system()})")
    print(f"  project: {PROJECT_REPO_URL} (developer preview; releases: {PROJECT_REPO_URL}/releases)")
    if sys.version_info < (3, 9):
        print("  FAIL: Python 3.9 or newer is required")
        ok = False
    try:
        for profile in builtin_profiles().values():
            print(f"  profile ok: {profile.profile_id}")
    except ProfileError as exc:
        print(f"  FAIL: profile: {exc}")
        ok = False
    print(f"  transport adapters implemented: {len(TRANSPORTS)}"
          + ("" if TRANSPORTS else " (none ship with this toolkit; apply will refuse)"))
    config_path = args.config or TEMPLATE
    try:
        missing = unconfigured(load_config(config_path))
        if missing:
            print(f"  NOT CONFIGURED: {', '.join(missing)} (edit a copy of the example config)")
        else:
            print("  config: all fields set")
    except ConfigError as exc:
        print(f"  FAIL: config: {exc}")
        ok = False
    if args.device_facts:
        profile = _profile(args)
        from .profile import match_device
        try:
            match_device(profile, _facts(args.device_facts))
            print(f"  device facts match {profile.profile_id}")
        except ProfileError as exc:
            print(f"  FAIL: {exc}")
            ok = False
    print("  Manual prerequisites (not checked by this toolkit):")
    for line in _profile(args).manual_prerequisites:
        print(f"    - {line}")
    return EXIT_OK if ok else EXIT_REFUSED


def cmd_profiles(args) -> int:
    for profile in builtin_profiles().values():
        print(profile.profile_id + "  " + json.dumps(dict(profile.match)))
    return EXIT_OK


def cmd_capsule_validate(args) -> int:
    capsule = validate(args.capsule, _profile(args), _facts(args.device_facts))
    print(_summary(capsule))
    return EXIT_OK


def cmd_capsule_assemble(args) -> int:
    facts = _facts(args.device_facts)
    if facts is None:
        raise ProfileError("--device-facts is required to bind the capsule to the device")
    capsule = assemble(args.spec, _profile(args), facts, args.out)
    print(_summary(capsule))
    return EXIT_OK


def cmd_capsule_bundle(args) -> int:
    digest = bundle(args.capsule_dir, args.out, _profile(args))
    print(f"bundle written; pin this SHA-256 when fetching it: {digest}")
    return EXIT_OK


def cmd_fetch(args) -> int:
    result = fetch(args.source, args.out, args.sha256, max_bytes=args.max_bytes,
                   read_timeout=args.timeout, total_timeout=args.total_timeout)
    print(f"verified {result.size} bytes from {result.source}; SHA-256 {result.sha256}")
    return EXIT_OK


def _build_plan(kind: str, capsule: Capsule, profile, args) -> Plan:
    if kind == "restore":
        return restore_plan(capsule, profile)
    install = None
    if args.install_package:
        install = {"packageId": args.install_package, "sha256": args.install_sha256}
    return setup_plan(capsule, profile, install)


def cmd_plan(args) -> int:
    profile = _profile(args)
    capsule = validate(args.capsule, profile, _facts(args.device_facts))
    print(_summary(capsule), file=sys.stderr)
    _print_plan(_build_plan(args.kind, capsule, profile, args), args.json)
    return EXIT_OK


def cmd_apply(args) -> int:
    profile = _profile(args)
    if not args.apply:
        capsule = validate(args.capsule, profile, _facts(args.device_facts))
        print(_summary(capsule), file=sys.stderr)
        _print_plan(_build_plan(args.kind, capsule, profile, args), False)
        print("DRY RUN: pass --apply with an implemented --transport to run this plan.")
        return EXIT_OK
    # Verify the capsule bytes before even looking for a transport.
    validate(args.capsule, profile, None)
    transport = get_transport(args.transport)
    facts = parse_device_facts(dict(transport.read_device_facts()))
    capsule = validate(args.capsule, profile, facts)
    plan = _build_plan(args.kind, capsule, profile, args)
    completed = run_plan(plan, transport, capsule_verified=capsule.payload_verified,
                         device_verified=capsule.device_verified)
    print(f"{plan.kind} plan applied and verified: {completed} of {len(plan.steps)} steps")
    return EXIT_OK


def cmd_export(args) -> int:
    allowlist = load_allowlist(args.allowlist)
    deny = load_deny_values(args.deny_values_file) if args.deny_values_file else []
    frontend = None
    if args.with_frontend:
        try:
            frontend = load_spec(args.frontend_spec)
        except TransformError as exc:
            raise ExportError(str(exc)) from None
    report = build_report(args.source, allowlist, deny, frontend)
    for path, size, _ in report.files:
        if args.list or not args.out:
            names = report.origins.get(path, ("", ()))[1]
            print(f"  export  {path}  ({size} bytes)" + (f"  [{', '.join(names)}]" if names else ""))
    for path, reason in report.excluded:
        print(f"  EXCLUDED  {path}: {reason}")
    for path in report.absent_optional:
        print(f"  OPTIONAL, NOT PRESENT  {path}")
    for path, reason in report.not_exported:
        print(f"  NOT IN THIS EXPORT  {path}: {reason}")
    if report.ignored_generated:
        print(f"  ignored generated cache files: {report.ignored_generated}")
    if deny:
        print(f"  deny values checked: {len(deny)} (values are never printed)")
    if report.violations:
        for violation in report.violations:
            print(f"  REFUSED  {violation.path}: {violation.rule}")
        print(f"export refused: {len(report.violations)} problem(s); nothing written")
        return EXIT_REFUSED
    import hashlib
    planned = hashlib.sha256(report.manifest_bytes()).hexdigest()
    if not args.out:
        print(f"LIST ONLY: {len(report.files)} files; manifest SHA-256 would be {planned}")
        return EXIT_OK
    digest = write_export(args.source, report, args.out, deny)
    print(f"exported {len(report.files)} files; EXPORT-MANIFEST.json SHA-256 {digest}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tvkit",
        description="Host-side setup/recovery toolkit for ONE owned TV profile. "
                    "Read-only and dry-run by default; ships no device transport.",
    )
    parser.add_argument("--profile", help="profile id (default: the only built-in profile)")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor", help="check this PC, the profile and your config")
    p.add_argument("--config", help="your config file (default: the shipped example)")
    p.add_argument("--device-facts", help="JSON facts your transport read from the TV")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("profiles", help="list supported device profiles")
    p.set_defaults(func=cmd_profiles)

    cap = sub.add_parser("capsule", help="backup capsule tools").add_subparsers(dest="capsule_cmd", required=True)
    p = cap.add_parser("validate", help="strictly validate a capsule folder or bundle")
    p.add_argument("capsule")
    p.add_argument("--device-facts")
    p.set_defaults(func=cmd_capsule_validate)
    p = cap.add_parser("assemble", help="build a capsule folder from captured payloads")
    p.add_argument("spec")
    p.add_argument("--device-facts", required=True)
    p.add_argument("--out", required=True, help="new folder on the PC or NAS")
    p.set_defaults(func=cmd_capsule_assemble)
    p = cap.add_parser("bundle", help="pack a capsule folder into one tar and print its SHA-256")
    p.add_argument("capsule_dir")
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_capsule_bundle)

    p = sub.add_parser("fetch", help="copy or download a file and verify a pinned SHA-256")
    p.add_argument("source", help="local path or https:// URL")
    p.add_argument("--sha256", required=True)
    p.add_argument("--out", required=True, help="destination file (must not exist)")
    p.add_argument("--max-bytes", type=int, default=8 * 1024 * 1024 * 1024)
    p.add_argument("--timeout", type=float, default=30.0, help="per-read timeout in seconds")
    p.add_argument("--total-timeout", type=float, default=1800.0)
    p.set_defaults(func=cmd_fetch)

    for name, helptext in (("plan", "print an ordered setup or restore plan (dry run)"),
                           ("apply", "run a plan; refuses without --apply and an implemented transport")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("kind", choices=("setup", "restore"))
        p.add_argument("--capsule", required=True)
        p.add_argument("--device-facts")
        p.add_argument("--install-package", help="setup: our launcher package id")
        p.add_argument("--install-sha256", help="setup: pinned SHA-256 of the launcher package")
        if name == "plan":
            p.add_argument("--json", action="store_true")
            p.set_defaults(func=cmd_plan)
        else:
            p.add_argument("--apply", action="store_true", help="actually run (needs --transport)")
            p.add_argument("--transport", help="name of an implemented transport adapter")
            p.set_defaults(func=cmd_apply)

    p = sub.add_parser("export", help="list or write a clean public source export")
    p.add_argument("--allowlist", default=str(DEFAULT_ALLOWLIST))
    p.add_argument("--source", default=str(DEFAULT_SOURCE))
    p.add_argument("--deny-values-file", help="owner-only file of exact values that must never ship")
    p.add_argument("--list", action="store_true", help="list only (the default when --out is absent)")
    p.add_argument("--out", help="new, empty folder outside the source tree")
    p.add_argument("--with-frontend", action="store_true",
                   help="also export the launcher frontend source through its transforms")
    p.add_argument("--frontend-spec", default=str(DEFAULT_FRONTEND_SPEC))
    p.set_defaults(func=cmd_export)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except TransportUnavailable as exc:
        print(f"refused: {exc}. Nothing was changed on any device.", file=sys.stderr)
        return EXIT_NO_ADAPTER
    except StepFailed as exc:
        print(f"STOPPED: {exc}. {exc.completed} earlier steps ran; the plan is NOT complete.",
              file=sys.stderr)
        return EXIT_REFUSED
    except (CapsuleError, ProfileError, ConfigError, FetchError, ExportError, OSError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED

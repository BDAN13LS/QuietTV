"""Owner-invoked live report collection CLI (thin wrapper over tvkit.livecollect).

This tool NEVER talks to a TV itself and NEVER runs a command. It resolves one
adapter name from the toolkit's EMPTY reviewed registry and hands it to the
public orchestrator; the privileged channel lives entirely in that parent-owned
adapter. With no registered adapter the tool refuses and writes nothing.

There is no module-path/plugin loading, no shell/command argument, no SERVER or
SERIAL input, no network address and no Helper HTTP route.

CLI:
    python collect.py --adapter <name> --out-dir <new dir> \
        --signed-wgt <file>.wgt --wgt-sha256 <64 hex> \
        --inspector-dll QuietTVInspect.dll --dll-sha256 <64 hex> \
        --inspector-runtimeconfig QuietTVInspect.runtimeconfig.json --runtimeconfig-sha256 <64 hex> \
        --inspector-deps QuietTVInspect.deps.json --deps-sha256 <64 hex> \
        --package-id <10 alphanumerics> --policy <trusted policy.json> --profile <trusted profile.json> \
        --operator-trust-policy-and-profile --operator-paired-from-same-tv
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True  # importing tvkit/owner_init must never create files under the repo

import argparse
import json
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "helper") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "helper"))
if str(REPO_ROOT / "public-toolkit") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "public-toolkit"))
from tvkit import livecollect as lc  # noqa: E402
from tvkit import liveops  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="collect.py",
        description="QuietTV live report collection over an owner-injected trusted adapter. "
                    "Ships no adapter; refuses without one. No TV, signing or root work here.")
    ap.add_argument("--adapter", required=True,
                    help="name of a registered live-operations adapter (the public registry is empty)")
    ap.add_argument("--out-dir", required=True,
                    help="NEW owner-private output directory OUTSIDE every repository")
    ap.add_argument("--signed-wgt", required=True, help="owner-signed diagnostic .wgt (package.Facts)")
    ap.add_argument("--wgt-sha256", required=True, help="trusted SHA-256 of the signed .wgt")
    ap.add_argument("--inspector-dll", required=True, help="QuietTVInspect.dll from the pinned build")
    ap.add_argument("--dll-sha256", required=True, help="trusted SHA-256 of the inspector DLL")
    ap.add_argument("--inspector-runtimeconfig", required=True,
                    help="QuietTVInspect.runtimeconfig.json from the SAME build")
    ap.add_argument("--runtimeconfig-sha256", required=True, help="trusted SHA-256 of the runtimeconfig")
    ap.add_argument("--inspector-deps", required=True,
                    help="QuietTVInspect.deps.json from the SAME build")
    ap.add_argument("--deps-sha256", required=True, help="trusted SHA-256 of the deps file")
    ap.add_argument("--package-id", required=True, help="10-character Tizen package id of the probe widget")
    ap.add_argument("--policy", required=True, help="owner-trusted tvkit-facts-join-policy/1 file")
    ap.add_argument("--profile", required=True, help="owner-trusted tvkit-profile/2 file")
    ap.add_argument("--operator-trust-policy-and-profile", action="store_true",
                    help="explicitly assert the policy and profile are the owner's trusted ones")
    ap.add_argument("--operator-paired-from-same-tv", action="store_true",
                    help="explicitly assert the two observations come from the same TV")
    args = ap.parse_args(argv)

    try:
        ops = liveops.get_liveops(args.adapter)
    except liveops.LiveOpsError as exc:
        print("collect refused: " + str(exc), file=sys.stderr)
        return 2
    try:
        summary = lc.collect_reports(
            ops=ops, out_dir=args.out_dir, package_id=args.package_id,
            signed_wgt=args.signed_wgt, wgt_sha256=args.wgt_sha256,
            inspector_dll=args.inspector_dll, dll_sha256=args.dll_sha256,
            inspector_runtimeconfig=args.inspector_runtimeconfig,
            runtimeconfig_sha256=args.runtimeconfig_sha256,
            inspector_deps=args.inspector_deps, deps_sha256=args.deps_sha256,
            policy=args.policy, profile=args.profile,
            operator_trust_policy_and_profile=args.operator_trust_policy_and_profile,
            operator_paired_from_same_tv=args.operator_paired_from_same_tv)
    except lc.LiveCollectionError as exc:
        print("collect refused: " + str(exc), file=sys.stderr)
        return 2
    except (OSError, ValueError):
        print("collect refused: a local operation failed", file=sys.stderr)
        return 2
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

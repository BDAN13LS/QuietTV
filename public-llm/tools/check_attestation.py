#!/usr/bin/env python3
"""Check a maintainer hardware attestation against the exact commit it claims to cover.

This does NOT test hardware. It checks that a human maintainer's record of a
real-device run is complete and bound to the PR head SHA, so any later push
invalidates it. See docs/HARDWARE-GATE.md.

Usage: check_attestation.py ATTESTATION.json --head-sha SHA
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SCHEMA = "quiettv-hardware-attestation/1"
SUPPORTED_PROFILES = {"UN43U8000FBXZA-1300.8"}
_SHA = re.compile(r"^[0-9a-f]{40}$")


def check(doc: object, head_sha: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(doc, dict):
        return ["attestation must be a JSON object"]
    if doc.get("schema") != SCHEMA:
        errors.append(f"schema must be {SCHEMA}")
    sha = doc.get("commit_sha")
    if not isinstance(sha, str) or not _SHA.match(sha):
        errors.append("commit_sha must be a full 40-character lowercase SHA")
    elif sha != head_sha.lower():
        errors.append("commit_sha does not match the PR head; re-test the exact commit")
    if doc.get("profile") not in SUPPORTED_PROFILES:
        errors.append("profile is not a supported device profile")
    for field in ("tested_by", "tested_at", "restore_evidence"):
        if not isinstance(doc.get(field), str) or not doc[field].strip():
            errors.append(f"{field} is required")
    if doc.get("offline_checks") != "pass":
        errors.append("offline_checks must be pass")
    checks = doc.get("device_checks")
    if not isinstance(checks, list) or not checks:
        errors.append("device_checks must list at least one real-device check")
    else:
        for i, item in enumerate(checks):
            if not isinstance(item, dict) or item.get("result") != "pass" or not str(item.get("evidence", "")).strip():
                errors.append(f"device_checks[{i}] must have result pass and evidence")
    if doc.get("video_and_sound") not in ("checked-directly", "not-affected"):
        errors.append("video_and_sound must be checked-directly or not-affected (captures cannot prove them)")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("attestation", type=Path)
    parser.add_argument("--head-sha", required=True)
    args = parser.parse_args(argv)
    try:
        doc = json.loads(args.attestation.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read attestation: {type(exc).__name__}", file=sys.stderr)
        return 1
    errors = check(doc, args.head_sha)
    for error in errors:
        print(error)
    print("attestation " + ("INVALID" if errors else "valid for " + args.head_sha), file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())

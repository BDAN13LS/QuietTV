#!/usr/bin/env python3
"""Public source guard: fail if a tree about to be published carries private material.

Stdlib only, read-only. Generic rules catch owner-specific values without this
file having to contain them; --denylist-sha256 adds exact owner values by hash
(one lowercase SHA-256 hex digest per line of a UTF-8 token, e.g. a MAC or IP),
so the private values themselves never enter the public tree.

Binary and large files (images, archives, packages) fail by default. A
maintainer-reviewed asset can pass only through --allow-binaries FILE, whose
lines are "<relative/posix/path> <sha256>": the exact bytes are approved, so a
changed or newly added binary (a screenshot, say) still fails.

Test files that deliberately carry fake LAN values or planted secrets (negative
controls) can be approved by exact hash with --approve-fixtures; see
load_fixture_approvals. Nothing is excluded by folder name alone.

Usage: public_source_guard.py ROOT [--denylist-sha256 FILE] [--allow-binaries FILE] [--approve-fixtures FILE]
Exit 0 = clean, 1 = findings, 2 = usage error.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import re
import sys
from pathlib import Path

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", "build", "dist"}
SKIP_SUFFIXES = {".egg-info"}
BLOCKED_SUFFIXES = {".key", ".pem", ".p12", ".pfx", ".token", ".sqlite3", ".db", ".png", ".jpg", ".jpeg", ".bmp",
                    ".tar", ".gz", ".tgz", ".zip", ".whl", ".tpk", ".wgt"}
BLOCKED_NAMES = {".env", "credentials.json", "id_rsa", "id_ed25519"}
MAX_TEXT_BYTES = 2 * 1024 * 1024

# Documentation/fixture values that may appear in public source.
ALLOWED_MAC_PREFIXES = ("02:00:00:00:00:", "00:00:5E:00:53:")  # locally administered fixture, RFC 7042 doc range
# RFC 1918 ranges, written numerically so this file does not trip its own rule.
LAN_NETS = [ipaddress.ip_network((0x0A000000, 8)), ipaddress.ip_network((0xAC100000, 12)),
            ipaddress.ip_network((0xC0A80000, 16))]
ALLOWED_NETS = [ipaddress.ip_network((0x0A140000, 16))]  # the example LAN (10.20.x.x) used in docs and tests
EXAMPLE_USERS = {"alice", "bob", "example", "username", "user", "you", "owner", "runner", "[user]", "<user>", "name"}

RULES: list[tuple[str, re.Pattern[str]]] = [
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("API key shape", re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,})\b")),
    ("control key header with a value", re.compile(r"(?i)x-quiettv-key\s*[:=]\s*[\"']?(?![\"'<{$\[]|\s|\(|planted)[A-Za-z0-9_-]{12,}")),
    ("private repository path", re.compile(r"(?i)\bprivate/(?:signing|backups|keys|tokens|credentials)\b")),
    ("self-hosted runner", re.compile(r"runs-on:\s*\[?\s*self-hosted")),
    ("pull_request_target trigger", re.compile(r"^\s*pull_request_target\s*:", re.M)),
]
MAC = re.compile(r"(?i)(?<![0-9a-f:])[0-9a-f]{2}(?::[0-9a-f]{2}){5}(?![0-9a-f:])")
IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
WIN_USER = re.compile(r"(?i)\b[a-z]:\\+users\\+([a-z0-9][^\\/\s\"'`]*)")
POSIX_USER = re.compile(r"(?:/home/|/Users/)([A-Za-z0-9][^/\s\"'`\\]*)")
TOKEN = re.compile(r"[A-Za-z0-9:._-]{4,}")


def iter_files(root: Path):
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS or part.endswith(tuple(SKIP_SUFFIXES)) for part in rel.parts):
            continue
        if path.is_file():
            yield path, rel


def scan_text(text: str, deny: set[str], *, generic: bool = True) -> list[str]:
    found: list[str] = []
    for name, pattern in (RULES if generic else []):
        if pattern.search(text):
            found.append(name)
    for mac in (MAC.findall(text) if generic else []):
        first = int(mac[:2], 16)
        if first & 1 or mac.upper() == "00:00:00:00:00:00":
            continue  # broadcast/multicast/null are never a device's own address
        if not mac.upper().startswith(ALLOWED_MAC_PREFIXES):
            found.append(f"MAC address outside fixture ranges ({mac[:8]}…)")
    for raw in (IPV4.findall(text) if generic else []):
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if any(ip in net for net in LAN_NETS) and not any(ip in net for net in ALLOWED_NETS):
            found.append("private LAN IP outside example ranges")
    for user in (WIN_USER.findall(text) + POSIX_USER.findall(text) if generic else []):
        if user.lower() not in EXAMPLE_USERS and not user.startswith(("$", "%", "<", "{", "[")):
            found.append("personal user path")
    if deny:
        for token in set(TOKEN.findall(text)):
            for candidate in (token, token.upper(), token.lower()):
                if hashlib.sha256(candidate.encode("utf-8")).hexdigest() in deny:
                    found.append("denylisted owner value")
                    break
    return sorted(set(found))


def load_binary_allowlist(path: Path) -> dict[str, str]:
    allowed: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) == 2 and re.fullmatch(r"[0-9a-f]{64}", parts[1]) and not line.lstrip().startswith("#"):
            allowed[parts[0]] = parts[1]
    return allowed


class ApprovalError(ValueError):
    pass


def load_fixture_approvals(path: Path) -> dict[str, str]:
    """Lines "<relative/posix/path> <sha256> <reason>". Only test-fixture paths may be approved, each with a reason.

    An approved file skips the GENERIC rules (fake LAN/MAC values, planted secret shapes used as negative
    controls) for those exact bytes only. The hashed owner denylist still applies to it, and any edit voids
    the approval, so a real value added later is still caught.
    """
    approvals: dict[str, str] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split(maxsplit=2)
        if len(parts) < 3 or not re.fullmatch(r"[0-9a-f]{64}", parts[1]) or len(parts[2].strip()) < 10:
            raise ApprovalError(f"line {number}: expected <path> <sha256> <reason of 10+ characters>")
        segments = parts[0].split("/")
        if ".." in segments or parts[0].startswith("/") or not ({"tests", "fixtures"} & set(segments[:-1])):
            raise ApprovalError(f"line {number}: only files under a tests/ or fixtures/ folder can be approved")
        approvals[parts[0]] = parts[1]
    return approvals


def scan(root: Path, deny: set[str] | None = None, allow_binaries: dict[str, str] | None = None,
         fixtures: dict[str, str] | None = None) -> list[str]:
    deny = deny or set()
    allow_binaries = allow_binaries or {}
    fixtures = fixtures or {}
    problems: list[str] = []
    for path, rel in iter_files(root):
        data = path.read_bytes()
        approved = allow_binaries.get(rel.as_posix())
        if approved is not None:
            if path.name in BLOCKED_NAMES:
                problems.append(f"{rel}: file type not allowed in public source")
            elif hashlib.sha256(data).hexdigest() != approved:
                problems.append(f"{rel}: differs from the reviewed bytes in the binary allowlist")
            continue
        if path.name in BLOCKED_NAMES or path.suffix.lower() in BLOCKED_SUFFIXES:
            problems.append(f"{rel}: file type not allowed in public source")
            continue
        if len(data) > MAX_TEXT_BYTES:
            problems.append(f"{rel}: file larger than {MAX_TEXT_BYTES} bytes")
            continue
        if b"\x00" in data[:8192]:
            problems.append(f"{rel}: binary file")
            continue
        text = data.decode("utf-8", errors="replace")
        approved_fixture = fixtures.get(rel.as_posix())
        generic = True
        if approved_fixture is not None:
            if hashlib.sha256(data).hexdigest() == approved_fixture:
                generic = False
            else:
                problems.append(f"{rel}: changed since its fixture approval; re-review and re-hash")
        problems.extend(f"{rel}: {finding}" for finding in scan_text(text, deny, generic=generic))
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", type=Path)
    parser.add_argument("--denylist-sha256", type=Path)
    parser.add_argument("--allow-binaries", type=Path, help="reviewed binaries: lines of '<path> <sha256>'")
    parser.add_argument("--approve-fixtures", type=Path,
                        help="reviewed test fixtures: lines of '<path> <sha256> <reason>' (tests/ or fixtures/ only)")
    args = parser.parse_args(argv)
    if not args.root.is_dir():
        print("root is not a directory", file=sys.stderr)
        return 2
    deny: set[str] = set()
    if args.denylist_sha256:
        deny = {line.strip().lower() for line in args.denylist_sha256.read_text(encoding="utf-8").splitlines()
                if re.fullmatch(r"[0-9a-fA-F]{64}", line.strip())}
    allow = load_binary_allowlist(args.allow_binaries) if args.allow_binaries else {}
    try:
        fixtures = load_fixture_approvals(args.approve_fixtures) if args.approve_fixtures else {}
    except ApprovalError as exc:
        print(f"fixture approvals: {exc}", file=sys.stderr)
        return 2
    problems = scan(args.root, deny, allow, fixtures)
    for problem in problems:
        print(problem)
    print(f"public source guard: {len(problems)} finding(s)", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

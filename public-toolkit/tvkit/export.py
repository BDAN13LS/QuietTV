"""Clean source export: a positive allowlist, scanned, copied into a new folder.

Only paths the allowlist names are considered; ``.gitignore`` is not trusted
and git history is never read or copied. Every candidate file is checked
against forbidden path parts and extensions (builds, packages, binaries,
certificates, logs, archives, dumps, databases) and scanned as raw bytes,
in UTF-8 and UTF-16LE form, for generic secret patterns (MAC addresses,
private IPv4 addresses, PEM keys, Windows user-profile paths, cloud tokens,
real email addresses) and for the owner's own values from an optional
``--deny-values-file``. Any hit refuses the whole export and names the file
and rule, never the matched value.

Exclusions are never silent: a file can only be left out by an ``exclude``
entry with a reason, and the report lists it. Generated Python caches
(``__pycache__``) are the single ignored category, and their count is shown.
``includeIfPresent`` entries may be absent (they are listed as absent);
``requireMarker`` files ship only when they carry their public marker.

Every PNG passes the metadata filter in ``pngmeta`` and the scans run on the
bytes that will actually be written. ``--with-frontend`` adds the launcher
frontend through ``frontend_export`` (see that module). JPEG, WebP, GIF,
TIFF, HEIC and AVIF images are refused because their metadata is not
filtered here.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import frontend_export as fe
from . import strictjson as sj

ALLOWLIST_SCHEMA = "tvkit-export-allowlist/1"
MANIFEST_SCHEMA = "tvkit-export-manifest/1"
MANIFEST_NAME = "EXPORT-MANIFEST.json"
MAX_FILE_BYTES = 16 * 1024 * 1024

FORBIDDEN_PARTS = frozenset({
    ".git", ".claude", "private", "build", "dist", "bin", "obj", "node_modules",
    ".venv", "venv", ".venv-codex", "backups", "captures", "dumps",
})
IGNORED_GENERATED_PARTS = frozenset({"__pycache__"})
FORBIDDEN_SUFFIXES = frozenset({
    ".wgt", ".tpk", ".rpm", ".dll", ".exe", ".so", ".dylib", ".pdb", ".p12", ".pfx",
    ".pem", ".key", ".crt", ".cer", ".der", ".csr", ".jks", ".keystore", ".log",
    ".tar", ".gz", ".tgz", ".zip", ".7z", ".xz", ".bz2", ".zst", ".img", ".bin",
    ".dump", ".dmp", ".core", ".sqlite", ".sqlite3", ".db", ".pcap", ".pyc",
    ".partial", ".tvcapsule", ".token", ".env",
})
UNFILTERED_IMAGE_SUFFIXES = frozenset({
    ".jpg", ".jpeg", ".webp", ".gif", ".tif", ".tiff", ".heic", ".heif", ".avif",
})
FORBIDDEN_NAMES = frozenset({
    "credentials.json", "control-config.js", ".env", "capsule.json", "id_rsa",
    "id_ed25519", ".npmrc", ".pypirc", ".netrc",
})

_B = rb"(?<![0-9A-Za-z])"
_E = rb"(?![0-9A-Za-z])"
BUILTIN_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    ("mac-address", re.compile(_B + rb"(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}" + _E)),
    ("private-ipv4", re.compile(
        rb"(?<![0-9.])(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}(?![0-9]|\.\d)")),
    ("pem-block", re.compile(rb"-----BEGIN [A-Z ]*(?:PRIVATE KEY|CERTIFICATE)-----")),
    ("windows-user-path", re.compile(rb"(?i)" + _B + rb"[a-z]:(?:\\\\|\\|/)+users(?:\\\\|\\|/)+(?![<%{$])[^\\/\s\"'<>]+")),
    ("posix-home-path", re.compile(rb"/home/(?!owner(?![0-9A-Za-z_-])|<)[A-Za-z0-9_.-]+/")),
    ("github-token", re.compile(rb"(?:ghp|gho|ghs|ghu)_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}")),
    ("aws-key", re.compile(_B + rb"AKIA[0-9A-Z]{16}" + _E)),
    ("slack-token", re.compile(rb"xox[abprs]-[A-Za-z0-9-]{10,}")),
    ("api-secret", re.compile(_B + rb"sk-[A-Za-z0-9_-]{32,}")),
    ("email-address", re.compile(
        rb"[A-Za-z0-9._%+-]+@(?!example\.(?:com|org|net)(?![A-Za-z0-9.-]))[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"
    )),
)


class ExportError(RuntimeError):
    pass


@dataclass
class Violation:
    path: str
    rule: str


@dataclass
class ExportReport:
    files: list[tuple[str, int, str]] = field(default_factory=list)  # path, size, sha256 (as written)
    excluded: list[tuple[str, str]] = field(default_factory=list)
    not_exported: list[tuple[str, str]] = field(default_factory=list)
    violations: list[Violation] = field(default_factory=list)
    ignored_generated: int = 0
    absent_optional: list[str] = field(default_factory=list)
    origins: dict[str, tuple[str, tuple[str, ...]]] = field(default_factory=dict)  # source sha256, transforms

    def _file(self, path: str, size: int, digest: str) -> dict[str, Any]:
        item: dict[str, Any] = {"path": path, "size": size, "sha256": digest}
        source_sha, names = self.origins.get(path, (digest, ()))
        if names:
            item["sourceSha256"] = source_sha
            item["transforms"] = list(names)
        return item

    def manifest(self) -> dict[str, Any]:
        return {
            "schema": MANIFEST_SCHEMA,
            "files": [self._file(p, s, h) for p, s, h in self.files],
            "excluded": [{"path": p, "reason": r} for p, r in self.excluded],
            "notExported": [{"path": p, "reason": r} for p, r in self.not_exported],
            "absentOptional": list(self.absent_optional),
            "note": "No git history, private data, builds, packages, certificates or device captures.",
        }

    def manifest_bytes(self) -> bytes:
        return sj.dumps(self.manifest()).encode("utf-8")


# --------------------------------------------------------------------------
# Deny values


@dataclass(frozen=True)
class DenyValue:
    index: int
    needles: tuple[bytes, ...]
    digits_only: bool


_MAC_LIKE = re.compile(r"^[0-9A-Fa-f]{2}([:.-]?)(?:[0-9A-Fa-f]{2}\1){4}[0-9A-Fa-f]{2}$")


def _variants(value: str) -> set[str]:
    out = {value.lower()}
    if _MAC_LIKE.match(value):
        hexes = re.sub(r"[^0-9A-Fa-f]", "", value).lower()
        pairs = [hexes[i:i + 2] for i in range(0, 12, 2)]
        out |= {sep.join(pairs) for sep in (":", "-", "", ".")}
    if "\\" in value or "/" in value:
        forward = value.replace("\\", "/").lower()
        out |= {forward, forward.replace("/", "\\"), forward.replace("/", "\\\\")}
    return out


def load_deny_values(path: str | Path) -> list[DenyValue]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ExportError("cannot read the deny-values file") from exc
    values: list[DenyValue] = []
    for line in text.splitlines():
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        if len(value) < 4:
            raise ExportError(f"deny value #{len(values) + 1} is shorter than 4 characters")
        needles: set[bytes] = set()
        for variant in _variants(value):
            needles.add(variant.encode("utf-8"))
            needles.add(variant.encode("utf-16-le"))
        values.append(DenyValue(len(values) + 1, tuple(sorted(needles)), value.isdigit()))
    return values


def _ascii_lower(data: bytes) -> bytes:
    return data.lower()  # bytes.lower only folds ASCII letters


def _utf16_views(data: bytes) -> list[bytes]:
    """Both byte lanes of possible UTF-16 text, so patterns see plain ASCII."""
    if data.count(b"\x00") < len(data) // 4:
        return []
    return [data[0::2], data[1::2]]


def scan_bytes(data: bytes, deny: Iterable[DenyValue] = ()) -> list[str]:
    """Rule names that hit. Never returns matched text."""
    hits: list[str] = []
    views = [data] + _utf16_views(data)
    for name, pattern in BUILTIN_PATTERNS:
        if any(pattern.search(view) for view in views):
            hits.append(name)
    lowered = _ascii_lower(data)
    for item in deny:
        for needle in item.needles:
            start = lowered.find(needle)
            found = False
            while start != -1:
                if not item.digits_only:
                    found = True
                    break
                before = lowered[start - 1:start]
                after = lowered[start + len(needle):start + len(needle) + 1]
                if not before.isdigit() and not after.isdigit():
                    found = True
                    break
                start = lowered.find(needle, start + 1)
            if found:
                hits.append(f"deny-value-#{item.index}")
                break
    return hits


# --------------------------------------------------------------------------
# Allowlist and walk


@dataclass(frozen=True)
class Allowlist:
    include: tuple[str, ...]
    exclude: tuple[tuple[str, str], ...]
    not_exported: tuple[tuple[str, str], ...]
    include_if_present: tuple[str, ...] = ()
    require_marker: tuple[tuple[str, str, str], ...] = ()  # path, marker, reason when missing


def _check_entry(value: Any, label: str) -> str:
    text = sj.require_str(value, label)
    if "\\" in text or text.startswith("/") or ".." in text.split("/") or re.match(r"^[A-Za-z]:", text):
        raise ExportError(f"{label} must be a relative forward-slash path")
    return text


def load_allowlist(path: str | Path) -> Allowlist:
    try:
        data = sj.require_object(sj.load_file(path, 1024 * 1024), "allowlist",
                                 ("schema", "include", "exclude", "notExported"),
                                 ("includeIfPresent", "requireMarker"))
        if data["schema"] != ALLOWLIST_SCHEMA:
            raise ExportError(f"allowlist schema must be {ALLOWLIST_SCHEMA}")
        include = [_check_entry(v, "include[]") for v in sj.require_list(data["include"], "include", max_items=500)]
        optional = [_check_entry(v, "includeIfPresent[]")
                    for v in sj.require_list(data.get("includeIfPresent", []), "includeIfPresent", max_items=100)]
        if any(ch in v for v in optional for ch in "*?["):
            raise ExportError("includeIfPresent entries must be plain paths")
        if len(set(include + optional)) != len(include) + len(optional):
            raise ExportError("allowlist include has duplicates")
        markers = []
        for i, item in enumerate(sj.require_list(data.get("requireMarker", []), "requireMarker", max_items=100)):
            obj = sj.require_object(item, f"requireMarker[{i}]", ("path", "marker", "reason"))
            marker = sj.require_str(obj["marker"], f"requireMarker[{i}].marker")
            if len(marker) < 8:
                raise ExportError(f"requireMarker[{i}].marker is too short")
            markers.append((_check_entry(obj["path"], f"requireMarker[{i}].path"), marker,
                            sj.require_str(obj["reason"], f"requireMarker[{i}].reason")))

        def pairs(key: str) -> tuple[tuple[str, str], ...]:
            out = []
            for i, item in enumerate(sj.require_list(data[key], key, max_items=500)):
                obj = sj.require_object(item, f"{key}[{i}]", ("path", "reason"))
                out.append((_check_entry(obj["path"], f"{key}[{i}].path"),
                            sj.require_str(obj["reason"], f"{key}[{i}].reason")))
            return tuple(out)

        return Allowlist(tuple(include), pairs("exclude"), pairs("notExported"), tuple(optional), tuple(markers))
    except sj.StrictJsonError as exc:
        raise ExportError(str(exc)) from exc


def _walk(root: Path, rel: str, report: ExportReport) -> list[str]:
    start = root / rel
    found: list[str] = []
    if start.is_symlink():
        report.violations.append(Violation(rel, "symbolic-link"))
        return found
    if start.is_file():
        return [rel]
    if not start.is_dir():
        report.violations.append(Violation(rel, "allowlisted-path-missing"))
        return found
    stack = [start]
    while stack:
        current = stack.pop()
        with os.scandir(current) as it:
            for item in it:
                path = Path(item.path)
                relpath = path.relative_to(root).as_posix()
                if item.name in IGNORED_GENERATED_PARTS:
                    report.ignored_generated += sum(1 for _ in path.rglob("*"))
                    continue
                if item.is_symlink():
                    report.violations.append(Violation(relpath, "symbolic-link"))
                elif item.is_dir(follow_symlinks=False):
                    stack.append(path)
                elif item.is_file(follow_symlinks=False):
                    found.append(relpath)
                else:
                    report.violations.append(Violation(relpath, "special-file"))
    return found


def _expand(root: Path, entry: str, report: ExportReport) -> list[str]:
    if any(ch in entry for ch in "*?["):
        base = entry.rsplit("/", 1)[0] if "/" in entry else ""
        candidates = _walk(root, base, report) if base else [
            p.name for p in root.iterdir() if p.is_file() and not p.is_symlink()]
        matched = [p for p in candidates if fnmatch.fnmatchcase(p, entry)]
        if not matched:
            report.violations.append(Violation(entry, "allowlisted-pattern-matched-nothing"))
        return matched
    return _walk(root, entry.rstrip("/"), report)


def _path_rule(relpath: str) -> str | None:
    parts = relpath.split("/")
    lowered = [p.lower() for p in parts]
    for part in lowered[:-1]:
        if part in FORBIDDEN_PARTS:
            return f"forbidden-directory:{part}"
    name = lowered[-1]
    if name in FORBIDDEN_NAMES:
        return "forbidden-file-name"
    for suffix in FORBIDDEN_SUFFIXES:
        if name.endswith(suffix):
            return f"forbidden-extension:{suffix}"
    for suffix in UNFILTERED_IMAGE_SUFFIXES:
        if name.endswith(suffix):
            return f"image-format-not-filtered:{suffix}"
    return None


def _has_key(value: Any, key: str, depth: int = 0) -> bool:
    if depth > 32:
        return False
    if isinstance(value, dict):
        return key in value or any(_has_key(v, key, depth + 1) for v in value.values())
    if isinstance(value, list):
        return any(_has_key(v, key, depth + 1) for v in value)
    return False


def _json_rule(relpath: str, data: bytes) -> str | None:
    """Owner files that look harmless to the byte patterns: device facts, a filled config."""
    if not relpath.lower().endswith(".json"):
        return None
    try:
        doc = json.loads(data.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError):
        return None
    if _has_key(doc, "deviceId"):
        return "device-facts-file"
    if isinstance(doc, dict) and doc.get("schema") == "tvkit-config/1":
        for key in ("capsuleStore", "transport"):
            value = doc.get(key)
            if not (isinstance(value, str) and value.strip().startswith("<CONFIGURE")):
                return "filled-user-config"
    return None


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _prepare(relpath: str, raw: bytes, names: tuple[str, ...]) -> bytes:
    try:
        return fe.apply(names, raw)
    except (fe.TransformError, UnicodeDecodeError) as exc:
        raise ExportError(f"transform-refused: {exc}") from None


def _frontend_candidates(root: Path, spec: fe.FrontendSpec, report: ExportReport) -> dict[str, tuple[str, ...]]:
    """Every file under the frontend root must be either included or excluded with a reason."""
    froot = root / spec.root
    if froot.is_symlink() or not froot.is_dir():
        report.violations.append(Violation(spec.root, "frontend-root-missing"))
        return {}
    counts = {path: 0 for path, _ in spec.exclude}
    seen: set[str] = set()
    stack = [froot]
    while stack:
        with os.scandir(stack.pop()) as it:
            for item in it:
                path = Path(item.path)
                rel = path.relative_to(froot).as_posix()
                hit = fe.excluded_by(spec, rel)
                if hit:
                    counts[hit[0]] += 1 if not item.is_dir(follow_symlinks=False) else \
                        sum(1 for p in path.rglob("*") if not p.is_dir())
                    continue
                full = f"{spec.root}/{rel}"
                if item.is_symlink():
                    report.violations.append(Violation(full, "symbolic-link"))
                elif item.is_dir(follow_symlinks=False):
                    stack.append(path)
                elif not item.is_file(follow_symlinks=False):
                    report.violations.append(Violation(full, "special-file"))
                elif rel in spec.include:
                    seen.add(rel)
                else:
                    report.violations.append(Violation(full, "frontend-file-not-accounted"))
    for rel in spec.include:
        if rel not in seen:
            report.violations.append(Violation(f"{spec.root}/{rel}", "allowlisted-path-missing"))
    for path, reason in spec.exclude:
        n = counts[path]
        report.excluded.append((f"{spec.root}/{path}", f"{reason} ({n} file{'s' if n != 1 else ''})"
                                if n else f"{reason} (not present)"))
    return {f"{spec.root}/{rel}": fe.transforms_for(spec, rel) for rel in sorted(seen)}


def _check_references(spec: fe.FrontendSpec, written: dict[str, bytes], report: ExportReport,
                      deny: list[DenyValue]) -> None:
    """A frontend file may only point at files this export ships (or a declared, reasoned gap)."""
    prefix = spec.root + "/"
    shipped = {p[len(prefix):] for p in written}
    allowed = {(f, r) for f, r, _ in spec.allowed_missing_refs}
    used: set[tuple[str, str]] = set()
    for full, data in sorted(written.items()):
        rel = full[len(prefix):]
        for ref, target in fe.references(rel, data):
            if target in shipped:
                continue
            if (rel, ref) in allowed:
                used.add((rel, ref))
                continue
            shown = ref if not scan_bytes(ref.encode("utf-8"), deny) else "<withheld>"
            report.violations.append(Violation(full, f"unresolved-reference:{shown}"))
    for item in sorted(allowed - used):
        report.violations.append(Violation(f"{prefix}{item[0]}", "allowed-missing-ref-unused"))


def build_report(source_root: str | Path, allowlist: Allowlist, deny: Iterable[DenyValue] = (),
                 frontend: fe.FrontendSpec | None = None) -> ExportReport:
    root = Path(source_root).resolve()
    deny = list(deny)
    not_exported = list(allowlist.not_exported)
    if frontend is not None:
        not_exported = [(p, r) for p, r in not_exported if p.rstrip("/") != frontend.root]
    report = ExportReport(not_exported=not_exported)
    candidates: dict[str, tuple[str, ...]] = {}
    for entry in allowlist.include:
        for relpath in _expand(root, entry, report):
            candidates.setdefault(relpath, ())
    for entry in allowlist.include_if_present:
        target = root / entry.rstrip("/")
        if not target.exists() and not target.is_symlink():
            report.absent_optional.append(entry)
            continue
        for relpath in _walk(root, entry.rstrip("/"), report):
            candidates.setdefault(relpath, ())
    frontend_paths: dict[str, tuple[str, ...]] = {}
    if frontend is not None:
        frontend_paths = _frontend_candidates(root, frontend, report)
        for relpath in frontend_paths:
            if relpath in candidates:
                report.violations.append(Violation(relpath, "frontend-file-also-in-allowlist"))
        candidates.update(frontend_paths)
    markers = {p: (m, r) for p, m, r in allowlist.require_marker}
    used_excludes: set[str] = set()
    written: dict[str, bytes] = {}
    for relpath in sorted(candidates):
        match = next((p for p, _ in allowlist.exclude
                      if relpath == p or relpath.startswith(p.rstrip("/") + "/")), None)
        if match is not None:
            report.excluded.append((relpath, dict(allowlist.exclude)[match]))
            used_excludes.add(match)
            continue
        rule = _path_rule(relpath)
        if rule:
            report.violations.append(Violation(relpath, rule))
            continue
        path = root / relpath
        if path.stat().st_size > MAX_FILE_BYTES:
            report.violations.append(Violation(relpath, "file-too-large"))
            continue
        raw = path.read_bytes()
        if relpath in markers and markers[relpath][0].encode("utf-8") not in raw:
            report.excluded.append((relpath, markers[relpath][1]))
            continue
        names = candidates[relpath]
        if not names and relpath.lower().endswith(".png"):
            names = (fe.PNG_STRIP,)
        try:
            data = _prepare(relpath, raw, names)
        except ExportError as exc:
            report.violations.append(Violation(relpath, str(exc)))
            continue
        for hit in scan_bytes(data, deny):
            report.violations.append(Violation(relpath, hit))
        json_hit = _json_rule(relpath, data)
        if json_hit:
            report.violations.append(Violation(relpath, json_hit))
        report.files.append((relpath, len(data), _sha(data)))
        report.origins[relpath] = (_sha(raw), names)
        if relpath in frontend_paths:
            written[relpath] = data
    for p, _ in allowlist.exclude:
        if p not in used_excludes:
            report.violations.append(Violation(p, "exclude-entry-matched-nothing"))
    if frontend is not None:
        _check_references(frontend, written, report, deny)
    return report


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def write_export(source_root: str | Path, report: ExportReport, out_dir: str | Path,
                 deny: Iterable[DenyValue] = ()) -> str:
    """Copy the reported files into a new or empty folder. Returns the manifest SHA-256."""
    if report.violations:
        raise ExportError("refusing to export while violations exist")
    root = Path(source_root).resolve()
    out = Path(out_dir)
    if out.is_symlink():
        raise ExportError("output folder must not be a symbolic link")
    out_resolved = out.resolve()
    if _is_within(out_resolved, root) or _is_within(root, out_resolved):
        raise ExportError("output folder must be outside the source tree")
    if out.exists():
        if not out.is_dir() or any(out.iterdir()):
            raise ExportError("output folder must be new or empty")
    else:
        out.mkdir(parents=False)
    deny = list(deny)
    for relpath, size, digest in report.files:
        raw = (root / relpath).read_bytes()
        source_sha, names = report.origins[relpath]
        # Re-check: the source must be unchanged and the bytes written exactly what was scanned.
        if _sha(raw) != source_sha:
            raise ExportError(f"{relpath} changed after scanning; export stopped")
        data = _prepare(relpath, raw, names)
        if len(data) != size or _sha(data) != digest or scan_bytes(data, deny) or _json_rule(relpath, data):
            raise ExportError(f"{relpath} changed after scanning; export stopped")
        target = out / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "xb") as handle:
            handle.write(data)
    manifest = report.manifest_bytes()
    with open(out / MANIFEST_NAME, "xb") as handle:
        handle.write(manifest)
    return hashlib.sha256(manifest).hexdigest()

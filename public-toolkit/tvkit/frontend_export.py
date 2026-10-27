"""Source-only transformations for exporting the launcher frontend.

The private ``frontend/`` folder is never edited. ``export --with-frontend``
reads it through ``frontend-export.json`` (a positive file list plus a reason
for everything else in the folder) and passes each file through the
transforms named here before the normal export scans:

- ``weather-unconfigured``: the owner's default ZIP code and place in
  ``src/weather.mjs`` become unconfigured values, and weather starts off until
  a ZIP code is entered;
- ``drop-extracted-brand-icons``: ``src/main.jsx`` stops importing the two
  brand PNGs that were extracted from the TV, so those tiles use the
  launcher's fallback art;
- ``png-metadata-strip`` (``pngmeta``): applied to every exported PNG.

Each anchor is a pattern that must match exactly once; anything else stops
the export, so an upstream edit can never pass through half-transformed.
The anchors hold no personal values, only the code shape around them.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import pngmeta
from . import strictjson as sj

SPEC_SCHEMA = "tvkit-frontend-export/1"
PNG_STRIP = "png-metadata-strip"


class TransformError(ValueError):
    pass


def _replace_once(text: str, pattern: str, replacement: str, label: str) -> str:
    regex = re.compile(pattern, re.MULTILINE)
    found = len(regex.findall(text))
    if found != 1:
        raise TransformError(f"{label}: anchor matched {found} times (needs exactly 1)")
    return regex.sub(lambda _: replacement, text, count=1)


# Line anchors end at (?=\r?$) so a CRLF checkout keeps its line endings.
WEATHER_RULES = (
    ("DEFAULT_WEATHER",
     r"^export const DEFAULT_WEATHER=Object\.freeze\(\{[^}\r\n]*\}\);(?=\r?$)",
     "export const DEFAULT_WEATHER=Object.freeze({enabled:false,zip:'',unit:'F'});"),
    ("DEFAULT_PLACE comment",
     r"^// Resolved by the geocoding API for [^\r\n]*(?=\r?$)",
     "// Unconfigured in the public source: no place is known until the owner enters a ZIP code, so the sunrise"
     " fallback uses fixed times. zip:null never equals a settings ZIP, so placeFor never returns this."),
    ("DEFAULT_PLACE",
     r"^export const DEFAULT_PLACE=Object\.freeze\(\{[^}\r\n]*\}\);(?=\r?$)",
     "export const DEFAULT_PLACE=Object.freeze({zip:null,name:'',region:'',latitude:null,longitude:null});"),
    ("weather enabled default",
     r"enabled:value\.enabled!==false,",
     "enabled:typeof value.enabled==='boolean'?value.enabled:DEFAULT_WEATHER.enabled,"),
)

BRAND_ICON_RULES = (
    ("Prime Video import", r"import prime from '\./assets/Prime-Video\.png';", ""),
    ("Philo import", r"import philo from '\./assets/Philo\.png';", ""),
    ("Prime Video tile art", r"'org\.tizen\.primevideo':prime,", ""),
    ("Philo tile art", r"'EYm8vc1St4\.Philo':philo,", ""),
)
_LEFTOVER_BRAND = re.compile(r"(?<![\w'\".-])(?:prime|philo)(?![\w'\"])")


def weather_unconfigured(data: bytes) -> bytes:
    text = data.decode("utf-8")
    for label, pattern, replacement in WEATHER_RULES:
        text = _replace_once(text, pattern, replacement, label)
    return text.encode("utf-8")


def drop_extracted_brand_icons(data: bytes) -> bytes:
    text = data.decode("utf-8")
    for label, pattern, replacement in BRAND_ICON_RULES:
        text = _replace_once(text, pattern, replacement, label)
    if _LEFTOVER_BRAND.search(text):
        raise TransformError("main.jsx still refers to the removed brand images")
    return text.encode("utf-8")


def png_metadata_strip(data: bytes) -> bytes:
    try:
        return pngmeta.strip_metadata(data)[0]
    except pngmeta.PngError as exc:
        raise TransformError(f"PNG refused: {exc}") from None


TRANSFORMS: dict[str, Callable[[bytes], bytes]] = {
    "weather-unconfigured": weather_unconfigured,
    "drop-extracted-brand-icons": drop_extracted_brand_icons,
    PNG_STRIP: png_metadata_strip,
}


def apply(names: tuple[str, ...], data: bytes) -> bytes:
    for name in names:
        data = TRANSFORMS[name](data)
    return data


# --------------------------------------------------------------------------
# Spec


@dataclass(frozen=True)
class FrontendSpec:
    root: str
    include: tuple[str, ...]
    exclude: tuple[tuple[str, str], ...]
    transforms: dict[str, str]
    allowed_missing_refs: tuple[tuple[str, str, str], ...]  # file, ref, reason


def _rel(value: Any, label: str) -> str:
    text = sj.require_str(value, label)
    if "\\" in text or text.startswith("/") or ".." in text.split("/") or re.match(r"^[A-Za-z]:", text) \
            or any(ch in text for ch in "*?["):
        raise TransformError(f"{label} must be a plain relative forward-slash path")
    return text


def load_spec(path: str | Path) -> FrontendSpec:
    try:
        data = sj.require_object(sj.load_file(path, 1024 * 1024), "frontend spec",
                                 ("schema", "root", "include", "exclude", "transforms", "allowedMissingRefs"))
        if data["schema"] != SPEC_SCHEMA:
            raise TransformError(f"frontend spec schema must be {SPEC_SCHEMA}")
        root = _rel(data["root"], "root").rstrip("/")
        include = tuple(_rel(v, "include[]") for v in sj.require_list(data["include"], "include", max_items=500))
        if len(set(include)) != len(include):
            raise TransformError("frontend include has duplicates")
        exclude = []
        for i, item in enumerate(sj.require_list(data["exclude"], "exclude", max_items=500)):
            obj = sj.require_object(item, f"exclude[{i}]", ("path", "reason"))
            exclude.append((_rel(obj["path"], f"exclude[{i}].path"), sj.require_str(obj["reason"], "reason")))
        transforms = {}
        for key, name in sj.require_object(data["transforms"], "transforms", (), tuple(include)).items():
            if name not in TRANSFORMS or name == PNG_STRIP:
                raise TransformError(f"transforms[{key}] names an unknown transform")
            transforms[key] = name
        refs = []
        for i, item in enumerate(sj.require_list(data["allowedMissingRefs"], "allowedMissingRefs", max_items=100)):
            obj = sj.require_object(item, f"allowedMissingRefs[{i}]", ("file", "ref", "reason"))
            refs.append((_rel(obj["file"], "file"), sj.require_str(obj["ref"], "ref"),
                         sj.require_str(obj["reason"], "reason")))
        return FrontendSpec(root, include, tuple(exclude), transforms, tuple(refs))
    except sj.StrictJsonError as exc:
        raise TransformError(str(exc)) from None


def transforms_for(spec: FrontendSpec, rel: str) -> tuple[str, ...]:
    names = (spec.transforms[rel],) if rel in spec.transforms else ()
    if rel.lower().endswith(".png"):
        names += (PNG_STRIP,)
    return names


def excluded_by(spec: FrontendSpec, rel: str) -> tuple[str, str] | None:
    for path, reason in spec.exclude:
        base = path.rstrip("/")
        if rel == base or rel.startswith(base + "/"):
            return path, reason
    return None


# --------------------------------------------------------------------------
# References between exported files

_TEXT_SUFFIXES = (".mjs", ".js", ".jsx", ".cjs", ".css", ".html")
_QUOTED_RELATIVE = re.compile(r"""['"`](\.{1,2}/[^'"`\s]*)['"`]""")
_ROOT_ATTR = re.compile(r"""(?:src|href)\s*=\s*["'](/[^"'\s]+)["']""")


def references(rel: str, data: bytes) -> list[tuple[str, str]]:
    """(raw reference, resolved frontend-relative path) for every relative file reference."""
    if not rel.lower().endswith(_TEXT_SUFFIXES):
        return []
    text = data.decode("utf-8", errors="replace")
    base = posixpath.dirname(rel)
    out = []
    for match in _QUOTED_RELATIVE.finditer(text):
        ref = match.group(1)
        clean = ref.split("?", 1)[0].split("#", 1)[0]
        if clean in ("./", "../") or clean.endswith("/"):
            continue
        out.append((ref, posixpath.normpath(posixpath.join(base, clean))))
    if rel.lower().endswith(".html"):
        for match in _ROOT_ATTR.finditer(text):
            ref = match.group(1)
            out.append((ref, posixpath.normpath(ref.lstrip("/").split("?", 1)[0])))
    return out

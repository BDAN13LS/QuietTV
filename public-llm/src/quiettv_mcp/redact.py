"""Redaction for anything that leaves the server. Whitelisting comes first; this is the backstop."""
from __future__ import annotations

import re
from typing import Any, Iterable

_HEADER = re.compile(r"(?i)\b(x-quiettv-key|authorization|proxy-authorization|cookie|set-cookie|x-api-key)\b(\s*[:=]\s*)(?:bearer\s+|basic\s+)?[^\s,;\"']+")
_URL = re.compile(r"(?i)\b([a-z][a-z0-9+.-]{1,15})://(?:[^\s/@\"'<>]*@)?([^\s/?#\"'<>:]+)(:\d{1,5})?[^\s\"'<>]*")
_ASSIGN = re.compile(r"(?i)\b([a-z0-9_.-]*(?:token|secret|passw(?:or)?d|passwd|pwd|api[_-]?key|apikey|session|signature|sig|auth|credential|key))(\"?\s*[:=]\s*\"?)([^\s\"&,;}]+)")
_BLOB = re.compile(r"[A-Za-z0-9+_-]{32,}={0,2}")
_WIN_USER = re.compile(r"(?i)\b([a-z]:\\+(?:users|documents and settings)\\+)[^\\/\s\"']+")
_POSIX_USER = re.compile(r"(/(?:opt/usr/)?home/|/Users/|/var/home/)[^/\s\"']+")
_IPV4 = re.compile(r"(?<![\d.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\d.])")
_MAC = re.compile(r"(?i)\b[0-9a-f]{2}(?:[:-][0-9a-f]{2}){5}\b")


def _ip(match: re.Match[str]) -> str:
    parts = [int(p) for p in match.groups()]
    if any(p > 255 for p in parts):
        return match.group(0)
    if parts[0] == 127 or parts == [0, 0, 0, 0]:
        return match.group(0)
    return "[ip]"


def redact_text(text: str, secrets: Iterable[str] = ()) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    text = _HEADER.sub(lambda m: m.group(1) + m.group(2) + "[redacted]", text)
    text = _URL.sub(lambda m: f"{m.group(1)}://{m.group(2)}/[redacted]", text)
    text = _ASSIGN.sub(lambda m: m.group(1) + m.group(2) + "[redacted]", text)
    text = _WIN_USER.sub(lambda m: m.group(1) + "[user]", text)
    text = _POSIX_USER.sub(lambda m: m.group(1) + "[user]", text)
    text = _MAC.sub("[mac]", text)
    text = _IPV4.sub(_ip, text)
    text = _BLOB.sub(_blob, text)
    return text


def _blob(match: re.Match[str]) -> str:
    value = match.group(0)
    has_digit = any(c.isdigit() for c in value)
    has_alpha = any(c.isalpha() for c in value)
    return "[redacted]" if has_digit and has_alpha else value


def redact_value(value: Any, secrets: Iterable[str] = (), *, max_string: int = 300) -> Any:
    secrets = tuple(secrets)
    if isinstance(value, str):
        out = redact_text(value, secrets)
        return out if len(out) <= max_string else out[:max_string] + "…"
    if isinstance(value, dict):
        return {redact_text(str(k), secrets)[:80]: redact_value(v, secrets, max_string=max_string) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(v, secrets, max_string=max_string) for v in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(str(value), secrets)[:max_string]

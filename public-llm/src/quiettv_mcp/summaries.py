"""Whitelisted summaries of TV service responses. Raw settings and config never pass through."""
from __future__ import annotations

import json
from typing import Any, Iterable

from .redact import redact_text, redact_value

MAX_DIAG_LINES = 40
MAX_DIAG_LINE = 160


def _get(doc: Any, *path: str) -> Any:
    for part in path:
        if not isinstance(doc, dict):
            return None
        doc = doc.get(part)
    return doc


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _num(value: Any) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _present(value: Any) -> bool:
    return value not in (None, "", "null") and str(value).strip() != ""


def summarize_status(doc: Any, secrets: Iterable[str] = ()) -> dict[str, Any]:
    if not isinstance(doc, dict):
        return {"parsed": False}
    error = doc.get("error")
    caps = doc.get("capabilities")
    summary: dict[str, Any] = {
        "parsed": True,
        "ready": _bool(doc.get("ready")),
        "root_connected": _bool(_get(doc, "root", "connected")),
        "platform_prepared": _bool(_get(doc, "platform", "prepared")),
        "health_ready": _bool(_get(doc, "health", "ready")),
        "health_exit_code": _num(_get(doc, "health", "exit")),
        "health_checked_at_ms": _num(_get(doc, "health", "at")),
        "service_started_at_ms": _num(doc.get("started")),
        "dashboard_running": _present(doc.get("dashboardPid")),
        "viewer_running": _present(doc.get("viewerPid")),
        "home_ui_visible": _bool(_get(doc, "ui", "visible")),
        "player_active": isinstance(doc.get("player"), dict),
        "error": redact_value(error, secrets, max_string=120) if isinstance(error, str) else None,
    }
    if isinstance(caps, list):
        summary["service_capabilities"] = sorted({c for c in caps if isinstance(c, str) and c.isascii() and len(c) <= 40})
    summary["fields_withheld"] = len(set(doc) - {"ready", "root", "platform", "health", "started", "dashboardPid",
                                                  "viewerPid", "ui", "player", "error", "capabilities"})
    return summary


def summarize_command_result(doc: Any) -> dict[str, Any]:
    """platform.execute results: exit code only, never stdout."""
    if not isinstance(doc, dict):
        return {"parsed": False}
    exit_code = _num(doc.get("exit_code"))
    return {"parsed": True, "exit_code": exit_code, "succeeded": exit_code == 0}


def _scalar_view(obj: dict[str, Any], secrets: Iterable[str], depth: int = 0) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in list(obj.items())[:40]:
        name = redact_text(str(key), secrets)[:60]
        if isinstance(value, bool) or _num(value) is not None:
            out[name] = value
        elif value is None:
            out[name] = None
        elif isinstance(value, dict) and depth < 1:
            out[name] = _scalar_view(value, secrets, depth + 1)
        elif isinstance(value, str):
            out[name] = "[text withheld]"
        else:
            out[name] = f"[{type(value).__name__} withheld]"
    return out


def summarize_diagnostics(doc: Any, secrets: Iterable[str] = ()) -> dict[str, Any]:
    secrets = tuple(secrets)
    if not isinstance(doc, dict):
        return {"parsed": False}
    stdout = doc.get("stdout") if isinstance(doc.get("stdout"), str) else ""
    sections: list[dict[str, Any]] = []
    lines: list[str] = []
    total = withheld = 0
    decoder = json.JSONDecoder()
    pos = 0
    while pos < len(stdout):
        end = stdout.find("\n", pos)
        end = len(stdout) if end < 0 else end
        line = stdout[pos:end].strip()
        if line.startswith("{"):
            # JSON documents (often pretty-printed config files) become scalar-only views.
            try:
                parsed, stop = decoder.raw_decode(stdout, stdout.index("{", pos))
            except json.JSONDecodeError:
                parsed, stop = None, end
            total += 1
            if isinstance(parsed, dict) and len(sections) < 10:
                sections.append({"json_fields": _scalar_view(parsed, secrets)})
            else:
                withheld += 1
            pos = max(stop, end) if parsed is None else stop
            continue
        pos = end + 1
        if not line:
            continue
        total += 1
        if line[0] in "\"}]," or len(lines) >= MAX_DIAG_LINES:
            withheld += 1
            continue
        clean = redact_text(line, secrets)
        lines.append(clean if len(clean) <= MAX_DIAG_LINE else clean[:MAX_DIAG_LINE] + "…")
    return {
        "parsed": True,
        "exit_code": _num(doc.get("exit_code")),
        "lines": lines,
        "json_sections": sections,
        "items_total": total,
        "items_withheld": withheld,
        "redaction": "strings in JSON withheld; tokens, URLs, credentials, IP/MAC and user paths redacted",
    }

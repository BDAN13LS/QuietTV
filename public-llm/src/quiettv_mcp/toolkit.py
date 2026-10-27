"""Maintenance (backup / cleanup / restore) through an external, validated toolkit adapter.

This package implements no backup, cleanup or restore transport of its own.
Mutations are reachable only when ALL of these hold:
  1. the owner launched the server with --enable-maintenance;
  2. exactly one adapter is installed under the entry-point group below and
     reports api_version 1;
  3. the adapter reports the operation available with validated_transport true;
  4. the apply call names a plan_id this server obtained from the same adapter
     in this session;
  5. for restore, the adapter verified the capsule (verified true + sha256);
  6. the adapter's apply result carries applied true AND non-empty evidence.
Anything else is reported as unavailable or unverified, never as applied.
"""
from __future__ import annotations

import re
from importlib.metadata import entry_points
from typing import Any, Literal, Protocol, runtime_checkable

ENTRY_POINT_GROUP = "quiettv_mcp.toolkit_adapters"
API_VERSION = 1
OPERATIONS = ("backup", "cleanup", "restore")
Operation = Literal["backup", "cleanup", "restore"]
_REF = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@runtime_checkable
class ToolkitAdapter(Protocol):
    """The interface public-toolkit (or any validated toolkit) implements. See docs/TOOLKIT-ADAPTER.md."""

    api_version: int

    def describe(self) -> dict[str, Any]:
        """{"name": str, "version": str, "operations": {op: {"available": bool,
        "validated_transport": bool, "reason": str}}}. Read-only."""

    def plan(self, operation: str, capsule_ref: str | None) -> dict[str, Any]:
        """Read-only dry run: {"plan_id": str, "summary": str, "steps": [str], "mutates": bool}."""

    def verify_capsule(self, capsule_ref: str) -> dict[str, Any]:
        """Read-only: {"verified": bool, "sha256": str | None, "reason": str}."""

    def apply(self, operation: str, plan_id: str, capsule_ref: str | None) -> dict[str, Any]:
        """Mutation: {"applied": bool, "evidence": {...}, "reason": str}."""


class MaintenanceError(Exception):
    pass


def discover_adapter() -> tuple[ToolkitAdapter | None, str]:
    found = list(entry_points(group=ENTRY_POINT_GROUP))
    if not found:
        return None, "no toolkit adapter installed (entry-point group quiettv_mcp.toolkit_adapters is empty)"
    if len(found) > 1:
        return None, "more than one toolkit adapter installed; refusing to choose"
    try:
        factory = found[0].load()
        adapter = factory() if callable(factory) and not isinstance(factory, ToolkitAdapter) else factory
    except Exception as exc:  # an adapter that fails to load is reported, not trusted
        return None, f"toolkit adapter failed to load ({type(exc).__name__})"
    if not isinstance(adapter, ToolkitAdapter) or getattr(adapter, "api_version", None) != API_VERSION:
        return None, "toolkit adapter does not implement adapter API version 1"
    return adapter, "toolkit adapter loaded"


def _check_ref(capsule_ref: str | None) -> None:
    if capsule_ref is not None and not _REF.match(capsule_ref):
        raise MaintenanceError("capsule_ref must be an identifier (letters, digits, . _ -), not a path")


class MaintenanceGate:
    def __init__(self, *, enabled: bool, adapter: ToolkitAdapter | None, reason: str = ""):
        self.enabled = enabled
        self.adapter = adapter
        self.reason = reason
        self._plans: dict[str, tuple[str, str | None]] = {}

    def _operations(self) -> dict[str, dict[str, Any]]:
        if self.adapter is None:
            return {}
        try:
            described = self.adapter.describe()
        except Exception as exc:
            return {"_error": {"available": False, "reason": f"describe failed ({type(exc).__name__})"}}
        ops = described.get("operations") if isinstance(described, dict) else None
        return ops if isinstance(ops, dict) else {}

    def capabilities(self) -> dict[str, Any]:
        result: dict[str, Any] = {"maintenance_enabled": self.enabled, "adapter_installed": self.adapter is not None}
        ops = self._operations()
        for op in OPERATIONS:
            info = ops.get(op) if isinstance(ops.get(op), dict) else {}
            plan_ok = self.enabled and self.adapter is not None and info.get("available") is True
            apply_ok = plan_ok and info.get("validated_transport") is True
            if not self.enabled:
                why = "maintenance is off; the owner must launch the server with --enable-maintenance"
            elif self.adapter is None:
                why = self.reason or "no toolkit adapter installed"
            elif not plan_ok:
                why = str(info.get("reason") or "adapter does not offer this operation")[:200]
            elif not apply_ok:
                why = "adapter has no validated transport for this operation; planning only"
            else:
                why = "available through the toolkit adapter"
            result[op] = {"plan": plan_ok, "apply": apply_ok, "reason": why}
        return result

    def _require(self, operation: str, *, apply: bool) -> None:
        if operation not in OPERATIONS:
            raise MaintenanceError("unknown maintenance operation")
        caps = self.capabilities()[operation]
        if not caps["apply" if apply else "plan"]:
            raise MaintenanceError(f"{operation} is not available: {caps['reason']}")

    def plan(self, operation: str, capsule_ref: str | None) -> dict[str, Any]:
        _check_ref(capsule_ref)
        self._require(operation, apply=False)
        assert self.adapter is not None
        planned = self.adapter.plan(operation, capsule_ref)
        plan_id = planned.get("plan_id") if isinstance(planned, dict) else None
        if not isinstance(plan_id, str) or not _REF.match(plan_id):
            raise MaintenanceError("adapter returned no usable plan_id")
        self._plans[plan_id] = (operation, capsule_ref)
        return {
            "operation": operation,
            "plan_id": plan_id,
            "summary": str(planned.get("summary", ""))[:500],
            "steps": [str(s)[:200] for s in planned.get("steps", [])][:50] if isinstance(planned.get("steps"), list) else [],
            "mutates": planned.get("mutates") is not False,
            "applied": False,
        }

    def apply(self, operation: str, plan_id: str, capsule_ref: str | None) -> dict[str, Any]:
        _check_ref(capsule_ref)
        self._require(operation, apply=True)
        assert self.adapter is not None
        if self._plans.get(plan_id) != (operation, capsule_ref):
            raise MaintenanceError("plan_id was not issued by maintenance_plan in this session for this operation and capsule")
        capsule = None
        if operation == "restore":
            if capsule_ref is None:
                raise MaintenanceError("restore requires a capsule_ref")
            capsule = self.adapter.verify_capsule(capsule_ref)
            if not (isinstance(capsule, dict) and capsule.get("verified") is True
                    and isinstance(capsule.get("sha256"), str) and _SHA256.match(capsule["sha256"])):
                raise MaintenanceError("capsule is not verified by the toolkit; restore refused")
        outcome = self.adapter.apply(operation, plan_id, capsule_ref)
        self._plans.pop(plan_id, None)
        evidence = outcome.get("evidence") if isinstance(outcome, dict) else None
        applied = isinstance(outcome, dict) and outcome.get("applied") is True and isinstance(evidence, dict) and bool(evidence)
        return {
            "operation": operation,
            "plan_id": plan_id,
            "result": "applied" if applied else "unverified",
            "applied": applied,
            "capsule_sha256": capsule.get("sha256") if capsule else None,
            "evidence": evidence if applied else None,
            "reason": str(outcome.get("reason", ""))[:300] if isinstance(outcome, dict) else "adapter returned no result",
        }

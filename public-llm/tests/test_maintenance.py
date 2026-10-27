"""Maintenance gating. The adapters here are test doubles of the documented interface, not shipped code."""
from __future__ import annotations

import pytest

from quiettv_mcp.toolkit import MaintenanceError, MaintenanceGate, ToolkitAdapter, discover_adapter


class RecordingAdapter:
    api_version = 1

    def __init__(self, *, validated=True, verified=True, evidence=None, applied=True):
        self.validated, self.verified, self.applied = validated, verified, applied
        self.evidence = {"post_check": "sha256 of restored tree matched capsule"} if evidence is None else evidence
        self.apply_calls: list[tuple] = []

    def describe(self):
        op = {"available": True, "validated_transport": self.validated, "reason": "test"}
        return {"name": "test", "version": "0", "operations": {"backup": op, "cleanup": op, "restore": op}}

    def plan(self, operation, capsule_ref):
        return {"plan_id": f"plan-{operation}", "summary": "dry run", "steps": ["step"], "mutates": True}

    def verify_capsule(self, capsule_ref):
        return {"verified": self.verified, "sha256": "ab" * 32 if self.verified else None, "reason": "test"}

    def apply(self, operation, plan_id, capsule_ref):
        self.apply_calls.append((operation, plan_id, capsule_ref))
        return {"applied": self.applied, "evidence": self.evidence, "reason": "test"}


def test_adapter_double_satisfies_protocol():
    assert isinstance(RecordingAdapter(), ToolkitAdapter)


def test_no_adapter_installed_is_reported_honestly():
    adapter, reason = discover_adapter()
    assert adapter is None and "no toolkit adapter installed" in reason
    caps = MaintenanceGate(enabled=True, adapter=None, reason=reason).capabilities()
    assert all(caps[op] == {"plan": False, "apply": False, "reason": reason} for op in ("backup", "cleanup", "restore"))


def test_flag_off_blocks_everything_even_with_adapter():
    adapter = RecordingAdapter()
    gate = MaintenanceGate(enabled=False, adapter=adapter)
    with pytest.raises(MaintenanceError, match="--enable-maintenance"):
        gate.plan("backup", None)
    with pytest.raises(MaintenanceError):
        gate.apply("backup", "plan-backup", None)
    assert adapter.apply_calls == []


def test_unvalidated_transport_allows_planning_only():
    adapter = RecordingAdapter(validated=False)
    gate = MaintenanceGate(enabled=True, adapter=adapter)
    assert gate.plan("cleanup", None)["applied"] is False
    with pytest.raises(MaintenanceError, match="validated transport"):
        gate.apply("cleanup", "plan-cleanup", None)
    assert adapter.apply_calls == []


def test_apply_requires_plan_from_this_session():
    adapter = RecordingAdapter()
    gate = MaintenanceGate(enabled=True, adapter=adapter)
    with pytest.raises(MaintenanceError, match="plan_id"):
        gate.apply("backup", "plan-backup", None)
    gate.plan("backup", None)
    with pytest.raises(MaintenanceError, match="plan_id"):
        gate.apply("cleanup", "plan-backup", None)
    assert adapter.apply_calls == []


def test_restore_refused_without_verified_capsule():
    adapter = RecordingAdapter(verified=False)
    gate = MaintenanceGate(enabled=True, adapter=adapter)
    gate.plan("restore", "capsule-1")
    with pytest.raises(MaintenanceError, match="not verified"):
        gate.apply("restore", "plan-restore", "capsule-1")
    assert adapter.apply_calls == []


def test_restore_refused_when_capsule_digest_is_not_sha256():
    adapter = RecordingAdapter()
    adapter.verify_capsule = lambda ref: {"verified": True, "sha256": "not-a-digest", "reason": "x"}
    gate = MaintenanceGate(enabled=True, adapter=adapter)
    gate.plan("restore", "capsule-1")
    with pytest.raises(MaintenanceError, match="not verified"):
        gate.apply("restore", "plan-restore", "capsule-1")
    assert adapter.apply_calls == []


def test_applied_without_evidence_reported_unverified():
    gate = MaintenanceGate(enabled=True, adapter=RecordingAdapter(evidence={}))
    gate.plan("backup", None)
    result = gate.apply("backup", "plan-backup", None)
    assert result["result"] == "unverified" and result["applied"] is False


def test_full_path_with_evidence_reports_applied_once():
    adapter = RecordingAdapter()
    gate = MaintenanceGate(enabled=True, adapter=adapter)
    gate.plan("restore", "capsule-1")
    result = gate.apply("restore", "plan-restore", "capsule-1")
    assert result["result"] == "applied" and result["capsule_sha256"] == "ab" * 32
    with pytest.raises(MaintenanceError):  # plan ids are single-use
        gate.apply("restore", "plan-restore", "capsule-1")


@pytest.mark.parametrize("ref", ["../x", "C:\\x", "/etc/passwd", "a b", "x" * 129])
def test_capsule_ref_is_an_identifier_not_a_path(ref):
    gate = MaintenanceGate(enabled=True, adapter=RecordingAdapter())
    with pytest.raises(MaintenanceError, match="identifier"):
        gate.plan("restore", ref)

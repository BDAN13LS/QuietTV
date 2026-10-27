"""Truthful component states after a run: removed, disabled, kept, held, failed.

"removed" needs proof: for every package of the component, a read of its
exact install path that found it absent, carrying an evidence SHA-256. A
registry flag is never proof of removal and is not even an input to it. A
component that was meant to be removed but is still present is "failed",
with the reason naming whether it was found disabled; it is never relabelled
"removed" or quietly reported as "disabled".

"disabled" exists for components the run did not act on: every path present,
and the registry flag read as disabled. D1 change sets never disable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from .catalogue import ChangeSet, Component, Inventory
from .paths import is_sha256

STATES = ("removed", "disabled", "kept", "held", "failed")


@dataclass(frozen=True)
class PathObservation:
    path: str
    present: bool
    evidence_sha256: str


@dataclass(frozen=True)
class ComponentReport:
    component_id: str
    state: str
    reason: str = ""


def _observed(path: str, observations: Mapping[str, PathObservation]) -> PathObservation | None:
    obs = observations.get(path)
    if not isinstance(obs, PathObservation) or obs.path != path or type(obs.present) is not bool:
        return None
    if not is_sha256(obs.evidence_sha256):
        return None
    return obs


def report(changeset: ChangeSet, observations: Mapping[str, PathObservation],
           disabled_flags: Mapping[str, bool] | None = None) -> tuple[ComponentReport, ...]:
    """States for every component in the change set: removed, failed or held."""
    disabled_flags = disabled_flags or {}
    reports = [ComponentReport(h.component_id, "held", h.reason) for h in changeset.held]
    for action in changeset.actions:
        reason = ""
        for target in action.packages:
            obs = _observed(target.path, observations)
            if obs is None:
                reason = f"no-absence-proof:{target.package_id}"
            elif obs.present:
                suffix = "-disabled" if disabled_flags.get(target.package_id) is True else ""
                reason = f"still-present{suffix}:{target.package_id}"
            if reason:
                break
        reports.append(ComponentReport(action.component_id, "failed" if reason else "removed", reason))
    return tuple(sorted(reports, key=lambda r: r.component_id))


def untouched_state(component: Component, inventory: Inventory, observations: Mapping[str, PathObservation],
                    disabled_flags: Mapping[str, bool]) -> ComponentReport:
    """State of a component the run did not act on. Anything unproven is 'failed', never guessed."""
    cid = component.component_id
    paths = []
    for pid in component.package_ids:
        installed = inventory.packages.get(pid)
        if installed is None:
            return ComponentReport(cid, "failed", f"not-in-inventory:{pid}")
        paths.append((pid, f"{installed.install_root}/{pid}"))
    for pid, path in paths:
        obs = _observed(path, observations)
        if obs is None or not obs.present:
            return ComponentReport(cid, "failed", f"presence-not-proven:{pid}")
    flags = [disabled_flags.get(pid) for pid, _ in paths]
    if all(flag is True for flag in flags):
        return ComponentReport(cid, "disabled")
    if all(flag is False for flag in flags):
        return ComponentReport(cid, "kept")
    return ComponentReport(cid, "failed", "disable-flag-unknown-or-mixed")


def summary(reports: Iterable[ComponentReport]) -> dict[str, int]:
    counts = {state: 0 for state in STATES}
    for item in reports:
        counts[item.state] += 1
    return counts

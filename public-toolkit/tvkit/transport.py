"""Transport adapters: the only way a plan could ever reach a TV.

The public toolkit ships NO implemented adapter. Developer Mode access,
root on this firmware, signing and the privileged channel are the owner's
own and stay outside this code. ``apply`` therefore fails closed unless an
adapter is registered that implements every capability the plan needs, and
it reports "applied" only when every step returned a verified success.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

from .plan import Plan, Step


class TransportUnavailable(RuntimeError):
    """No implemented adapter, or one that lacks a needed capability. Nothing ran."""


class StepFailed(RuntimeError):
    def __init__(self, step: Step, reason: str, completed: int):
        super().__init__(f"step {step.number} ({step.action} {step.target}) failed: {reason}")
        self.step = step
        self.completed = completed


@dataclass(frozen=True)
class StepResult:
    ok: bool
    verified: bool
    reason: str = ""


class Transport:
    name = "abstract"
    implemented = False
    capabilities: frozenset[str] = frozenset()

    def read_device_facts(self) -> Mapping[str, str]:
        raise TransportUnavailable("adapter cannot read device facts")

    def run(self, step: Step) -> StepResult:
        raise TransportUnavailable("adapter cannot run steps")


# Empty on purpose. A private adapter registers itself here from the owner's
# own code; nothing in this repository does.
TRANSPORTS: dict[str, Callable[[], Transport]] = {}


def get_transport(name: str | None) -> Transport:
    if not name:
        raise TransportUnavailable("no transport adapter named; this toolkit ships none")
    factory = TRANSPORTS.get(name)
    if factory is None:
        raise TransportUnavailable(f"no implemented transport adapter named {name!r}")
    transport = factory()
    if not transport.implemented:
        raise TransportUnavailable(f"transport adapter {name!r} is not implemented")
    return transport


def run_plan(plan: Plan, transport: Transport, *, capsule_verified: bool, device_verified: bool) -> int:
    """Run every step in order, stopping at the first failure. Returns steps completed."""
    if not capsule_verified or not device_verified:
        raise TransportUnavailable("capsule bytes and device identity must both be verified first")
    missing = plan.capabilities() - set(transport.capabilities)
    if missing:
        raise TransportUnavailable(
            f"adapter {transport.name!r} lacks capabilities: {', '.join(sorted(missing))}"
        )
    completed = 0
    for step in plan.steps:
        if step.requires == "local":
            completed += 1
            continue
        result = transport.run(step)
        if not (isinstance(result, StepResult) and result.ok and result.verified):
            reason = result.reason if isinstance(result, StepResult) else "adapter returned no result"
            raise StepFailed(step, reason or "not verified", completed)
        completed += 1
    return completed

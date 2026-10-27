"""Append-only run journal (``tvkit-journal/1``), kept on the PC.

One JSON object per line. Line 0 binds the journal to a plan digest, a
profile id and the device id HASH (never the raw id or any key). Every later
line records one step event:

  started  -> the step is about to run; its effect on the TV is now unknown
  verified -> the step ran and the adapter returned an evidence SHA-256
  failed   -> the step did not verify; the reason is an enumerated code

Each line carries ``prevSha256`` and its own ``sha256`` (a hash chain), so an
edited, reordered or inserted line is refused. The chain has no external
anchor and no key: it cannot detect a whole trailing record being cut off
cleanly, and anyone who can rewrite the file can recompute it. It guards
against corruption and accidents, not a deliberate forger.

Fail-closed rules: a partial last line, bad JSON, an unknown field or state,
a sequence gap, a different plan/profile/device, or an out-of-order step is
refused. Nothing is repaired automatically.

``resume`` continues only after the caller has re-read the TV and proved the
last verified step's evidence still holds. A step that was ``started`` but
never finished, or ``failed``, stops the run: its effect is unknown, so it is
neither assumed complete nor blindly repeated.

No command output, free text or credential is ever written: records hold
hashes, step numbers, action names from the plan and reason codes only.

Plans are mutable dataclasses, so a journal never trusts the object it was
given. It keeps a private deep copy of the approved plan, runs only from that
copy, hands each executor its own copy of the step, and re-checks the bound
plan digest of BOTH the copy and the caller's plan before ``started``,
immediately before the executor, and after it returns. A changed plan is
never approved: before ``started`` nothing is written and nothing runs; once
a step has started, it is closed as ``failed`` (``plan-changed``).
"""

from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from . import strictjson as sj
from .catalogue import canonical_sha256
from .paths import is_sha256
from .plan import Plan, Step
from .profile import Profile, ProfileError, device_id_sha256, match_device

JOURNAL_SCHEMA = "tvkit-journal/1"
GENESIS = "0" * 64
MAX_JOURNAL_BYTES = 16 * 1024 * 1024
REASON_CODES = frozenset({
    "adapter-error", "not-verified", "precondition-failed", "timeout", "connection-lost",
    "aborted", "unexpected-write", "plan-changed",
})
_FIELDS = {
    "open": ("seq", "kind", "schema", "planSha256", "profileId", "deviceIdSha256", "steps",
             "prevSha256", "sha256"),
    "started": ("seq", "kind", "step", "action", "stepSha256", "prevSha256", "sha256"),
    "verified": ("seq", "kind", "step", "action", "stepSha256", "evidenceSha256", "prevSha256", "sha256"),
    "failed": ("seq", "kind", "step", "action", "stepSha256", "reason", "prevSha256", "sha256"),
}
_ACTION = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


class JournalError(RuntimeError):
    pass


class JournalInterrupted(JournalError):
    """A step started and its outcome was never recorded. Inspect before anything runs."""


class PlanChanged(JournalError):
    """The plan no longer hashes to the digest the journal was bound to."""


def _snapshot(plan: Plan) -> Plan:
    """A private deep copy; nothing outside the journal holds a reference into it."""
    return copy.deepcopy(plan)


def plan_sha256(plan: Plan) -> str:
    return canonical_sha256(plan.to_json())


def step_sha256(step: Step) -> str:
    return canonical_sha256(asdict(step))


def _seal(record: dict[str, Any]) -> dict[str, Any]:
    body = {k: v for k, v in record.items() if k != "sha256"}
    return {**body, "sha256": canonical_sha256(body)}


def _line(record: Mapping[str, Any]) -> bytes:
    return (json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")


@dataclass(frozen=True)
class _State:
    seq: int
    head: str
    kind: str
    step: int  # step of the last event; 0 after "open"
    evidence: str | None
    total: int


def _check_transition(state: _State, record: Mapping[str, Any], plan: Plan) -> _State:
    """The one place the journal's state machine is defined (replay and append)."""
    kind = record.get("kind")
    if kind not in _FIELDS or kind == "open":
        raise JournalError("journal has an unknown or misplaced record kind")
    try:
        sj.require_object(record, f"journal record {state.seq + 1}", _FIELDS[kind])
        seq = sj.require_int(record["seq"], "journal seq")
        step = sj.require_int(record["step"], "journal step", minimum=1, maximum=state.total)
    except sj.StrictJsonError as exc:
        raise JournalError(str(exc)) from exc
    if seq != state.seq + 1:
        raise JournalError("journal sequence is not contiguous")
    if record["prevSha256"] != state.head:
        raise JournalError("journal chain is broken")
    if record["sha256"] != _seal(dict(record))["sha256"]:
        raise JournalError("journal record hash does not match its content")
    plan_step = plan.steps[step - 1]
    if record["stepSha256"] != step_sha256(plan_step) or record["action"] != plan_step.action:
        raise JournalError("journal step does not match the plan")
    if kind == "started":
        if state.kind not in ("open", "verified") or step != state.step + 1:
            raise JournalError("journal starts a step out of order")
        return _State(seq, record["sha256"], kind, step, None, state.total)
    if state.kind != "started" or step != state.step:
        raise JournalError("journal records an outcome for a step that was not started")
    if kind == "verified":
        if not is_sha256(record["evidenceSha256"]):
            raise JournalError("verified step has no evidence SHA-256")
        return _State(seq, record["sha256"], kind, step, record["evidenceSha256"], state.total)
    if record["reason"] not in REASON_CODES:
        raise JournalError("failed step has an unknown reason code")
    return _State(seq, record["sha256"], kind, step, None, state.total)


def _split_records(raw: bytes) -> list[bytes]:
    # A last line without its newline was cut mid-write, even if it parses.
    if not raw.endswith(b"\n"):
        raise JournalError("journal ends with a partial record; refusing")
    return raw[:-1].split(b"\n")


class Journal:
    """Use ``create`` or ``open``; both bind to plan, profile and device first."""

    def __init__(self, path: Path, plan: Plan, state: _State, size: int, plan_digest: str) -> None:
        self.path = path
        self._source = plan  # the caller's object: watched, never executed from
        self._plan = _snapshot(plan)  # the approved plan: the only one steps come from
        self._plan_digest = plan_digest
        self._state = state
        self._size = size
        self._check_plan()

    @property
    def plan(self) -> Plan:
        """A fresh copy of the approved plan; editing it changes nothing here."""
        return _snapshot(self._plan)

    def approved_step(self, number: int) -> Step:
        """A defensive copy of approved step ``number`` (1-based)."""
        self._check_plan()
        return copy.deepcopy(self._plan.steps[number - 1])

    def _check_plan(self) -> None:
        for candidate in (self._source, self._plan):
            try:
                digest = plan_sha256(candidate)
            except Exception as exc:  # noqa: BLE001 - an unserialisable plan is a changed plan
                raise PlanChanged("plan can no longer be read; refusing") from exc
            if digest != self._plan_digest:
                raise PlanChanged("plan changed after the journal was bound to it; refusing")

    # ---- construction

    @staticmethod
    def _binding(plan: Plan, profile: Profile, facts: Mapping[str, str]) -> dict[str, Any]:
        if plan.profile_id != profile.profile_id:
            raise JournalError("plan was built for a different profile")
        try:
            match_device(profile, facts)
            device_hash = device_id_sha256(facts.get("deviceId"))
        except ProfileError as exc:
            raise JournalError(str(exc)) from exc
        return {"schema": JOURNAL_SCHEMA, "planSha256": plan_sha256(plan), "profileId": profile.profile_id,
                "deviceIdSha256": device_hash, "steps": len(plan.steps)}

    @classmethod
    def create(cls, path: str | Path, plan: Plan, profile: Profile, facts: Mapping[str, str]) -> "Journal":
        target = Path(path)
        record = _seal({"seq": 0, "kind": "open", **cls._binding(plan, profile, facts), "prevSha256": GENESIS})
        data = _line(record)
        try:
            with open(target, "xb") as handle:  # never overwrite an existing journal
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError as exc:
            raise JournalError("a journal already exists at that path; resume it instead") from exc
        state = _State(0, record["sha256"], "open", 0, None, len(plan.steps))
        return cls(target, plan, state, len(data), record["planSha256"])

    @classmethod
    def open(cls, path: str | Path, plan: Plan, profile: Profile, facts: Mapping[str, str]) -> "Journal":
        target = Path(path)
        binding = cls._binding(plan, profile, facts)
        try:
            raw = target.read_bytes()
        except OSError as exc:
            raise JournalError(f"cannot read journal: {exc.strerror}") from exc
        if len(raw) > MAX_JOURNAL_BYTES:
            raise JournalError("journal is too large")
        if not raw:
            raise JournalError("journal is empty")
        records = []
        for number, line in enumerate(_split_records(raw)):
            try:
                records.append(sj.loads(line))
            except sj.StrictJsonError as exc:
                raise JournalError(f"journal line {number} is not valid JSON") from exc
        head = records[0]
        if not isinstance(head, dict) or head.get("kind") != "open":
            raise JournalError("journal does not start with an open record")
        try:
            sj.require_object(head, "journal open record", _FIELDS["open"])
        except sj.StrictJsonError as exc:
            raise JournalError(str(exc)) from exc
        if head["seq"] != 0 or head["prevSha256"] != GENESIS or head["sha256"] != _seal(dict(head))["sha256"]:
            raise JournalError("journal open record is damaged")
        for key, value in binding.items():
            if head[key] != value:
                raise JournalError(f"journal {key} does not match this run; refusing")
        state = _State(0, head["sha256"], "open", 0, None, len(plan.steps))
        for record in records[1:]:
            if not isinstance(record, dict):
                raise JournalError("journal record is not an object")
            state = _check_transition(state, record, plan)
        return cls(target, plan, state, len(raw), head["planSha256"])

    # ---- state

    @property
    def last_kind(self) -> str:
        return self._state.kind

    @property
    def last_step(self) -> int:
        return self._state.step

    @property
    def last_evidence(self) -> str | None:
        return self._state.evidence

    @property
    def complete(self) -> bool:
        return self._state.kind == "verified" and self._state.step == self._state.total

    # ---- appends (each one durable before it returns)

    def _append(self, kind: str, step: Step, **extra: Any) -> None:
        if kind != "failed":
            # A failure must always be recordable; nothing else is, once the plan changed.
            self._check_plan()
        record = _seal({"seq": self._state.seq + 1, "kind": kind, "step": step.number, "action": step.action,
                        "stepSha256": step_sha256(step), **extra, "prevSha256": self._state.head})
        new_state = _check_transition(self._state, record, self._plan)
        try:
            if self.path.stat().st_size != self._size:
                raise JournalError("journal changed on disk since it was read; refusing")
        except OSError as exc:
            raise JournalError(f"cannot read journal: {exc.strerror}") from exc
        data = _line(record)
        with open(self.path, "ab") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        self._state = new_state
        self._size += len(data)

    def started(self, step: Step) -> None:
        self._append("started", step)

    def verified(self, step: Step, evidence_sha256: str) -> None:
        if not is_sha256(evidence_sha256):
            raise JournalError("a step is verified only with an evidence SHA-256")
        self._append("verified", step, evidenceSha256=evidence_sha256)

    def failed(self, step: Step, reason: str) -> None:
        self._append("failed", step, reason=reason if reason in REASON_CODES else "adapter-error")


# --------------------------------------------------------------------------
# Resume and the journaled step driver


@dataclass(frozen=True)
class ResumePoint:
    journal: Journal
    next_step: int  # 1-based; len(plan.steps) + 1 when the run is complete
    reverified_step: int  # 0 when no step had been verified yet


def resume(path: str | Path, plan: Plan, profile: Profile, facts: Mapping[str, str],
           reverify: Callable[[Step], str | None]) -> ResumePoint:
    """Re-open a journal and decide where a run may continue.

    ``reverify(step)`` must re-read the TV now (read-only) and return the
    evidence SHA-256 for that step's post-condition, or None. It is called
    for the last verified step only, and the result must equal the journal.
    """
    journal = Journal.open(path, plan, profile, facts)
    _refuse_unfinished(journal)
    last = journal.last_step
    if last:
        current = reverify(journal.approved_step(last))
        if not is_sha256(current) or current != journal.last_evidence:
            raise JournalError(f"step {last} no longer verifies on the TV; refusing to continue")
    return ResumePoint(journal, last + 1, last)


def _refuse_unfinished(journal: Journal) -> None:
    if journal.last_kind == "started":
        raise JournalInterrupted(
            f"step {journal.last_step} started but its outcome was never recorded; "
            "inspect the TV before anything else runs")
    if journal.last_kind == "failed":
        raise JournalError(f"step {journal.last_step} failed; this run is closed")


@dataclass(frozen=True)
class StepOutcome:
    evidence_sha256: str | None
    reason: str = "not-verified"


class StepExecutor(Protocol):
    def execute(self, step: Step) -> StepOutcome: ...


def run_with_journal(point: ResumePoint, executor: StepExecutor) -> int:
    """Run the remaining steps, journaling each boundary. Returns the last verified step.

    Not a transport: nothing in this repository registers or ships an
    executor. Tests drive it with a fake. An executor exception is journaled
    as ``failed`` (adapter-error) and re-raised; only a crash of the process
    itself leaves a bare ``started`` line, which ``resume`` then refuses.

    The executor gets a copy of the approved step. The plan digest is
    re-checked before ``started`` (refusal: nothing written, nothing run),
    immediately before the executor and after it returns (the started step is
    closed as ``failed``/``plan-changed`` and no later step runs).
    """
    journal = point.journal
    journal._check_plan()
    if journal.complete:
        return journal.last_step
    if journal.last_kind not in ("open", "verified") or journal.last_step != point.next_step - 1:
        raise JournalError("resume point does not match the journal")
    for number in range(point.next_step, journal._state.total + 1):
        step = journal._plan.steps[number - 1]
        journal.started(step)  # checks the plan first
        _close_if_plan_changed(journal, step)
        try:
            outcome = executor.execute(copy.deepcopy(step))
        except Exception:
            journal.failed(step, "adapter-error")
            raise
        _close_if_plan_changed(journal, step)
        if not isinstance(outcome, StepOutcome) or not is_sha256(outcome.evidence_sha256):
            reason = outcome.reason if isinstance(outcome, StepOutcome) else "adapter-error"
            journal.failed(step, reason)
            raise JournalError(f"step {step.number} ({step.action}) did not verify")
        journal.verified(step, outcome.evidence_sha256)
    return journal.last_step


def _close_if_plan_changed(journal: Journal, step: Step) -> None:
    try:
        journal._check_plan()
    except PlanChanged:
        journal.failed(step, "plan-changed")
        raise

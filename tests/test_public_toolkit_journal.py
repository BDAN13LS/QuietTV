"""Run journal: durable appends, fail-closed replay and conservative resume.

A fake executor stands in for the TV. Crashes are simulated with a
BaseException (like a power cut, it bypasses the driver's failure record)
at every boundary of a step.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_public_toolkit_fixtures  # noqa: E402,F401  (puts public-toolkit on sys.path)
import test_public_toolkit_profile_v2 as pv2  # noqa: E402
from tvkit import journal as jn  # noqa: E402
from tvkit.journal import (Journal, JournalError, JournalInterrupted, StepOutcome, resume,  # noqa: E402
                           run_with_journal)
from tvkit.plan import Plan, Step  # noqa: E402
from tvkit.profile import parse_device_facts, parse_profile  # noqa: E402

CONSUMER = Path(__file__).resolve().parent / "fixtures" / "consumer"


def load(name):
    return json.loads((CONSUMER / name).read_text(encoding="utf-8"))


class SimulatedCrash(BaseException):
    pass


class FakeTV:
    """Each step 'mutates' one slot. inspect() re-reads it, read-only."""

    def __init__(self, crash_after_mutation_at=None, fail_at=None, raise_at=None, secret=None):
        self.state: dict[int, str] = {}
        self.mutations: list[int] = []
        self.crash_after_mutation_at = crash_after_mutation_at
        self.fail_at = fail_at
        self.raise_at = raise_at
        self.secret = secret

    @staticmethod
    def evidence(number, value):
        return hashlib.sha256(f"{number}:{value}".encode()).hexdigest()

    def execute(self, step):
        if step.number == self.raise_at:
            raise RuntimeError(f"stdout: {self.secret}")
        self.mutations.append(step.number)
        self.state[step.number] = "done"
        if step.number == self.crash_after_mutation_at:
            raise SimulatedCrash()
        if step.number == self.fail_at:
            return StepOutcome(None, f"command printed {self.secret}")
        return StepOutcome(self.evidence(step.number, "done"))

    def inspect(self, step):
        value = self.state.get(step.number)
        return self.evidence(step.number, value) if value else None


class JournalCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "run.jsonl"
        self.profile = parse_profile(load("profile-v2.json"))
        self.facts = parse_device_facts(pv2.consumer_facts())
        self.plan = Plan("remove", self.profile.profile_id, [
            Step(1, "preflight", "match-device", "device", "read-device-facts"),
            Step(2, "remove", "remove-package", "org.example.guide", "remove-package"),
            Step(3, "verify", "verify-absent", "org.example.guide", "inspect-path",
                 {"paths": ["/opt/usr/apps/org.example.guide"]}),
        ])

    def create(self):
        return Journal.create(self.path, self.plan, self.profile, self.facts)

    def resume(self, tv, facts=None, plan=None):
        return resume(self.path, plan or self.plan, self.profile, facts or self.facts, tv.inspect)

    def lines(self):
        return self.path.read_bytes().decode("ascii").splitlines()

    def rewrite(self, lines, tail="\n"):
        self.path.write_bytes(("\n".join(lines) + tail).encode("ascii"))


class Basics(JournalCase):
    def test_full_run_and_resume_complete(self):
        tv = FakeTV()
        point = jn.ResumePoint(self.create(), 1, 0)
        self.assertEqual(run_with_journal(point, tv), 3)
        kinds = [json.loads(line)["kind"] for line in self.lines()]
        self.assertEqual(kinds, ["open"] + ["started", "verified"] * 3)
        again = self.resume(tv)
        self.assertEqual((again.next_step, again.reverified_step), (4, 3))
        self.assertEqual(run_with_journal(again, tv), 3)
        self.assertEqual(tv.mutations, [1, 2, 3])

    def test_header_binds_hashes_never_raw_id(self):
        self.create()
        raw = self.path.read_text(encoding="ascii")
        self.assertNotIn(self.facts["deviceId"], raw)
        head = json.loads(self.lines()[0])
        self.assertEqual(head["planSha256"], jn.plan_sha256(self.plan))
        self.assertEqual(head["profileId"], self.profile.profile_id)

    def test_create_never_overwrites(self):
        self.create()
        with self.assertRaises(JournalError):
            self.create()

    def test_mismatched_run_refused(self):
        self.create()
        other_plan = Plan("remove", self.profile.profile_id, self.plan.steps[:2])
        cases = [
            lambda: self.resume(FakeTV(), plan=other_plan),
            lambda: self.resume(FakeTV(), facts={**self.facts, "deviceId": "SYNTHETIC-OTHER-DEVICE"}),
            lambda: self.resume(FakeTV(), facts={**self.facts, "region": "SYNTHETIC-OTHER"}),
        ]
        for index, case in enumerate(cases):
            with self.subTest(index), self.assertRaises(JournalError) as ctx:
                case()
            for value in (self.facts["deviceId"], "SYNTHETIC-OTHER"):
                self.assertNotIn(value, str(ctx.exception))

    def test_verified_needs_evidence_and_order(self):
        journal = self.create()
        with self.assertRaises(JournalError):
            journal.verified(self.plan.steps[0], "f" * 64)  # never started
        with self.assertRaises(JournalError):
            journal.started(self.plan.steps[1])  # out of order
        journal.started(self.plan.steps[0])
        for bad in (None, "", "not-a-hash", "F" * 64):
            with self.subTest(bad=bad), self.assertRaises(JournalError):
                journal.verified(self.plan.steps[0], bad)

    def test_no_free_text_is_journaled(self):
        secret = "SYNTHETIC-TOKEN-abc123"
        for tv in (FakeTV(fail_at=2, secret=secret), FakeTV(raise_at=2, secret=secret)):
            with self.subTest(tv=tv.fail_at or tv.raise_at):
                if self.path.exists():
                    self.path.unlink()
                with self.assertRaises((JournalError, RuntimeError)):
                    run_with_journal(jn.ResumePoint(self.create(), 1, 0), tv)
                raw = self.path.read_text(encoding="ascii")
                self.assertNotIn(secret, raw)
                last = json.loads(self.lines()[-1])
                self.assertEqual(last["kind"], "failed")
                self.assertIn(last["reason"], jn.REASON_CODES)

    def test_external_append_detected(self):
        journal = self.create()
        with open(self.path, "ab") as handle:
            handle.write(b"\n")
        with self.assertRaises(JournalError):
            journal.started(self.plan.steps[0])


class Tampering(JournalCase):
    def setUp(self):
        super().setUp()
        run_with_journal(jn.ResumePoint(self.create(), 1, 0), FakeTV())
        self.original = self.lines()

    def assertRefused(self, lines, tail="\n"):
        self.rewrite(lines, tail)
        with self.assertRaises(JournalError):
            self.resume(FakeTV())

    def test_edited_record(self):
        lines = list(self.original)
        record = json.loads(lines[4])
        record["evidenceSha256"] = "a" * 64
        lines[4] = json.dumps(record)
        self.assertRefused(lines)

    def test_resealed_record_breaks_chain(self):
        lines = list(self.original)
        record = json.loads(lines[4])
        record["evidenceSha256"] = "a" * 64
        lines[4] = jn._line(jn._seal(record)).decode().strip()
        self.assertRefused(lines)

    def test_reordered_dropped_duplicated_lines(self):
        o = self.original
        for label, lines in (("swap", [o[0], o[2], o[1]] + o[3:]), ("drop middle", o[:3] + o[4:]),
                             ("duplicate", o[:3] + [o[2]] + o[3:]), ("no header", o[1:])):
            with self.subTest(label):
                self.assertRefused(lines)

    def test_partial_tail_and_junk(self):
        self.assertRefused(self.original, tail="")
        self.assertRefused(self.original + ['{"seq": 7, "kind": "start'], tail="")
        self.assertRefused(self.original + [""])
        self.assertRefused(self.original + ["[]"])

    def test_unknown_state_or_field(self):
        for mutate in (lambda r: r.__setitem__("kind", "skipped"), lambda r: r.__setitem__("note", "x"),
                       lambda r: r.__setitem__("step", 9), lambda r: r.__setitem__("seq", True)):
            lines = list(self.original)
            record = json.loads(lines[1])
            mutate(record)
            lines[1] = jn._line(jn._seal(record)).decode().strip()
            with self.subTest(record=record):
                self.assertRefused(lines)


class CrashAtEveryBoundary(JournalCase):
    """Crash at each boundary of step 2, then check what resume allows."""

    def crash_run(self, tv, patch=None):
        point = jn.ResumePoint(self.create(), 1, 0)
        with self.assertRaises(SimulatedCrash):
            if patch:
                with patch:
                    run_with_journal(point, tv)
            else:
                run_with_journal(point, tv)

    @staticmethod
    def crash_on(method, step_number, after):
        original = getattr(Journal, method)

        def wrapper(self, step, *args):
            if step.number == step_number and not after:
                raise SimulatedCrash()
            original(self, step, *args)
            if step.number == step_number and after:
                raise SimulatedCrash()
        return mock.patch.object(Journal, method, wrapper)

    def test_before_started(self):
        tv = FakeTV()
        self.crash_run(tv, self.crash_on("started", 2, after=False))
        point = self.resume(tv)
        self.assertEqual((point.next_step, point.reverified_step), (2, 1))
        run_with_journal(point, tv)
        self.assertEqual(tv.mutations, [1, 2, 3])

    def test_after_started_before_mutation(self):
        tv = FakeTV()
        self.crash_run(tv, self.crash_on("started", 2, after=True))
        with self.assertRaises(JournalInterrupted):
            self.resume(tv)
        self.assertEqual(tv.mutations, [1])

    def test_after_mutation_before_verified(self):
        tv = FakeTV(crash_after_mutation_at=2)
        self.crash_run(tv)
        with self.assertRaises(JournalInterrupted):
            self.resume(tv)
        self.assertEqual(tv.mutations, [1, 2])  # never repeated

    def test_after_verified(self):
        tv = FakeTV()
        self.crash_run(tv, self.crash_on("verified", 2, after=True))
        point = self.resume(tv)
        self.assertEqual((point.next_step, point.reverified_step), (3, 2))
        run_with_journal(point, tv)
        self.assertEqual(tv.mutations, [1, 2, 3])

    def test_failed_step_closes_run(self):
        tv = FakeTV(fail_at=2)
        with self.assertRaises(JournalError):
            run_with_journal(jn.ResumePoint(self.create(), 1, 0), tv)
        with self.assertRaises(JournalError) as ctx:
            self.resume(tv)
        self.assertNotIsInstance(ctx.exception, JournalInterrupted)
        self.assertEqual(tv.mutations, [1, 2])

    def test_state_changed_since_verified(self):
        tv = FakeTV()
        self.crash_run(tv, self.crash_on("verified", 2, after=True))
        tv.state[2] = "changed-by-someone-else"
        with self.assertRaises(JournalError):
            self.resume(tv)
        tv.state.pop(2)
        with self.assertRaises(JournalError):
            self.resume(tv)  # reverify returning None is never "fine"

    def test_mid_append_power_cut(self):
        tv = FakeTV()
        self.crash_run(tv, self.crash_on("started", 2, after=False))
        with open(self.path, "ab") as handle:
            handle.write(b'{"seq":3,"kind":"star')
        with self.assertRaises(JournalError):
            self.resume(tv)


class RecordingTV(FakeTV):
    """Records exactly what each execute() call was handed."""

    def __init__(self, during=None, **kwargs):
        super().__init__(**kwargs)
        self.seen = []
        self.during = during or {}

    def execute(self, step):
        self.seen.append((step.number, step.action, step.target, json.dumps(step.detail, sort_keys=True)))
        if step.number in self.during:
            self.during[step.number](step)
        return super().execute(step)


class PlanBinding(JournalCase):
    """The plan object is mutable; the journal must run only the plan it was bound to."""

    def mutations(self):
        return {
            "action": lambda plan: setattr(plan.steps[1], "action", "remove-everything"),
            "target": lambda plan: setattr(plan.steps[1], "target", "org.example.other"),
            "nested detail": lambda plan: plan.steps[2].detail["paths"].append("/opt/usr/apps/org.example.other"),
            "appended step": lambda plan: plan.steps.append(
                Step(4, "remove", "remove-package", "org.example.other", "remove-package")),
            "removed step": lambda plan: plan.steps.pop(),
            "replaced detail": lambda plan: setattr(plan.steps[0], "detail", {"x": 1}),
        }

    def journal_kinds(self):
        return [json.loads(line)["kind"] for line in self.lines()]

    def test_unchanged_plan_runs(self):
        tv = RecordingTV()
        self.assertEqual(run_with_journal(jn.ResumePoint(self.create(), 1, 0), tv), 3)
        self.assertEqual([seen[2] for seen in tv.seen], ["device", "org.example.guide", "org.example.guide"])

    def test_mutation_after_create_refused_before_anything_runs(self):
        for label, mutate in self.mutations().items():
            with self.subTest(label):
                if self.path.exists():
                    self.path.unlink()
                self.setUp()
                journal = self.create()
                mutate(self.plan)
                tv = RecordingTV()
                with self.assertRaises(jn.PlanChanged):
                    run_with_journal(jn.ResumePoint(journal, 1, 0), tv)
                self.assertEqual(tv.seen, [])
                self.assertEqual(self.journal_kinds(), ["open"])
                with self.assertRaises(jn.PlanChanged):
                    journal.started(self.plan.steps[0])
                self.assertEqual(self.journal_kinds(), ["open"])

    def test_mutation_after_resume_refused(self):
        tv = RecordingTV()
        CrashAtEveryBoundary.crash_run(self, tv, CrashAtEveryBoundary.crash_on("verified", 1, after=True))
        point = self.resume(tv)
        before = self.lines()
        self.plan.steps[2].detail["paths"][0] = "/opt/usr/apps/org.example.other"
        with self.assertRaises(jn.PlanChanged):
            run_with_journal(point, tv)
        self.assertEqual([seen[0] for seen in tv.seen], [1])
        self.assertEqual(self.lines(), before)

    def test_mutation_between_started_and_executor(self):
        tv = RecordingTV()
        original = Journal.started

        def started_then_mutate(journal, step):
            original(journal, step)
            if step.number == 2:
                self.plan.steps[1].target = "org.example.other"
        with mock.patch.object(Journal, "started", started_then_mutate):
            with self.assertRaises(jn.PlanChanged):
                run_with_journal(jn.ResumePoint(self.create(), 1, 0), tv)
        self.assertEqual([seen[0] for seen in tv.seen], [1])  # step 2 never reached the executor
        last = json.loads(self.lines()[-1])
        self.assertEqual((last["kind"], last["step"], last["reason"]), ("failed", 2, "plan-changed"))

    def test_mutation_during_executor_fails_closed(self):
        tv = RecordingTV(during={1: lambda step: self.plan.steps[1].detail.update({"force": True})})
        with self.assertRaises(jn.PlanChanged):
            run_with_journal(jn.ResumePoint(self.create(), 1, 0), tv)
        self.assertEqual([seen[0] for seen in tv.seen], [1])  # no later step called
        self.assertEqual(self.journal_kinds(), ["open", "started", "failed"])
        last = json.loads(self.lines()[-1])
        self.assertEqual(last["reason"], "plan-changed")
        self.plan.steps[1].detail.pop("force")  # even with the plan restored, the run is closed
        with self.assertRaises(JournalError):
            self.resume(tv)

    def test_executor_gets_a_copy(self):
        def scribble(step):
            step.target = "org.example.other"
            step.detail["paths"] = ["/"]
        tv = RecordingTV(during={2: scribble, 3: scribble})
        self.assertEqual(run_with_journal(jn.ResumePoint(self.create(), 1, 0), tv), 3)
        self.assertEqual(tv.seen[2][2], "org.example.guide")
        self.assertEqual(self.plan.steps[2].detail, {"paths": ["/opt/usr/apps/org.example.guide"]})

    def test_plan_property_is_a_copy(self):
        journal = self.create()
        journal.plan.steps[1].target = "org.example.other"
        journal.plan.steps.clear()
        self.assertEqual(run_with_journal(jn.ResumePoint(journal, 1, 0), RecordingTV()), 3)

    def test_negative_control_old_defect(self):
        """Guard off (shared plan object, no digest re-check): the mutated step runs under the old binding."""
        with mock.patch.object(jn, "_snapshot", lambda plan: plan),                 mock.patch.object(Journal, "_check_plan", lambda self: None):
            journal = self.create()
            self.plan.steps[1].target = "org.example.other"
            self.plan.steps[2].detail["paths"].append("/opt/usr/apps/org.example.other")
            tv = RecordingTV()
            # Nothing notices: the per-record step hash is computed from the same mutated object.
            self.assertEqual(run_with_journal(jn.ResumePoint(journal, 1, 0), tv), 3)
        self.assertEqual(tv.seen[1][2], "org.example.other")  # the unapproved steps reached the executor
        self.assertIn("org.example.other", tv.seen[2][3])
        head = json.loads(self.lines()[0])
        self.assertNotEqual(head["planSha256"], jn.plan_sha256(self.plan))  # under the original binding


class NegativeControls(JournalCase):
    def test_chain_check(self):
        run_with_journal(jn.ResumePoint(self.create(), 1, 0), FakeTV())
        lines = self.lines()
        record = json.loads(lines[4])
        record["evidenceSha256"] = "a" * 64
        lines[4] = jn._line(jn._seal(record)).decode().strip()
        self.rewrite(lines)
        with self.assertRaises(JournalError):
            self.resume(FakeTV())
        # Guard off: with the chain check gone, the forged evidence is accepted.
        original = jn._check_transition

        def no_chain(state, rec, plan):
            unchained = jn._State(state.seq, rec.get("prevSha256"), state.kind, state.step, state.evidence,
                                  state.total)
            return original(unchained, rec, plan)
        with mock.patch.object(jn, "_check_transition", no_chain):
            jn.Journal.open(self.path, self.plan, self.profile, self.facts)

    def test_reverify_required(self):
        tv = FakeTV()
        CrashAtEveryBoundary.crash_run(self, tv, CrashAtEveryBoundary.crash_on("verified", 2, after=True))
        tv.state[2] = "changed-by-someone-else"
        with self.assertRaises(JournalError):
            self.resume(tv)
        # Guard off: comparing the TV with itself instead of with the journal lets it through.
        with mock.patch.object(Journal, "last_evidence", property(lambda self: tv.inspect(self.plan.steps[1]))):
            self.assertEqual(self.resume(tv).next_step, 3)

    def test_interrupted_refusal(self):
        tv = FakeTV(crash_after_mutation_at=2)
        CrashAtEveryBoundary.crash_run(self, tv)
        with self.assertRaises(JournalInterrupted):
            self.resume(tv)
        # Guard off: the interrupted verdict (and its "inspect the TV" instruction) is lost.
        # The evidence re-check still refuses (defence in depth), so only the error type changes.
        with mock.patch.object(jn, "_refuse_unfinished", lambda journal: None):
            with self.assertRaises(JournalError) as ctx:
                self.resume(tv)
            self.assertNotIsInstance(ctx.exception, JournalInterrupted)
        self.assertEqual(tv.mutations, [1, 2])

    def test_header_binding(self):
        self.create()
        other = Plan("remove", self.profile.profile_id, self.plan.steps[:2] + [
            Step(3, "verify", "verify-absent", "org.example.other", "inspect-path")])
        with self.assertRaises(JournalError):
            self.resume(FakeTV(), plan=other)
        head = json.loads(self.lines()[0])
        with mock.patch.object(jn, "plan_sha256", lambda plan: head["planSha256"]):
            self.assertEqual(self.resume(FakeTV(), plan=other).next_step, 1)

    def test_partial_tail(self):
        journal = self.create()
        journal.started(self.plan.steps[0])
        self.path.write_bytes(self.path.read_bytes()[:-1])  # whole record, newline cut
        with self.assertRaises(JournalError):
            Journal.open(self.path, self.plan, self.profile, self.facts)
        with mock.patch.object(jn, "_split_records", lambda raw: raw.rstrip(b"\n").split(b"\n")):
            self.assertEqual(Journal.open(self.path, self.plan, self.profile, self.facts).last_kind, "started")

    def test_every_write_is_fsynced(self):
        calls = []
        real = jn.os.fsync
        with mock.patch.object(jn.os, "fsync", lambda fd: (calls.append(fd), real(fd))):
            journal = self.create()
            journal.started(self.plan.steps[0])
            journal.verified(self.plan.steps[0], "e" * 64)
        self.assertEqual(len(calls), 3)

    def test_evidence_required(self):
        journal = self.create()
        journal.started(self.plan.steps[0])
        with self.assertRaises(JournalError):
            journal.verified(self.plan.steps[0], "no-evidence")
        with mock.patch.object(jn, "is_sha256", lambda value: True):
            journal.verified(self.plan.steps[0], "no-evidence")


if __name__ == "__main__":
    unittest.main()

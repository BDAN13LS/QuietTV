"""Focused tests for the ecore-runner probe's file-descriptor and handle-map acceptance verdicts.

The probe (probe/ecore-runner/Program.cs) diagnoses the ONLY failing on-TV check -- a whole-run FD count
that rose +5 (44 -> 49) past a +2 threshold -- by replacing a flawed whole-run comparison (it charged
one-time .NET/Process init and a DELIBERATELY still-alive coexist child's own handles as a "leak") with a
regression-sensitive, warmup-based steady-state ACCEPTANCE over 10/30/60-command batches, plus an
assertion that the ecore handle-tracking maps are empty between every command.

The AUTHORITATIVE comparators are the C# Program.FdClassify and Program.DiagAccept; the parent runs them
on Windows with `dotnet EcoreRunnerProbe.dll fd-selftest` (pinned SDK 6.0.428). This file MIRRORS them in
Python as CORROBORATION ONLY, so the logic can be exercised with no .NET toolchain, and statically guards
the probe source so the mirror cannot silently drift and the +2 tolerance can never be quietly loosened.
Both verdicts FAIL CLOSED: missing / too-few / negative / nonzero inputs must NOT be accepted.
"""

from __future__ import annotations

import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PROBE_CS = REPO / "probe" / "ecore-runner" / "Program.cs"

ACCEPT, LEAK, UNAVAILABLE = "Accept", "Leak", "Unavailable"


def _fd_classify(fd_totals, tolerance: int, required: int) -> str:
    """Mirror of C# Program.FdClassify. Fails CLOSED: None, fewer than `required` ordered samples, or any
    negative count => Unavailable. A later sample > tolerance above the baseline => Leak. Else Accept."""
    if fd_totals is None or len(fd_totals) < required:
        return UNAVAILABLE
    if any(fd < 0 for fd in fd_totals):
        return UNAVAILABLE
    baseline = fd_totals[0]
    if any(fd - baseline > tolerance for fd in fd_totals):
        return LEAK
    return ACCEPT


def _diag_accept(diags, required: int) -> bool:
    """Mirror of C# Program.DiagAccept. Every between-command (byHandle,byId,orphans) must be (0,0,0).
    Fails CLOSED: None list, fewer than `required` snapshots, or any missing (None) snapshot => False."""
    if diags is None or len(diags) < required:
        return False
    for d in diags:
        if d is None or any(v != 0 for v in d):
            return False
    return True


class FdClassifyComparator(unittest.TestCase):
    """FdClassify, tested BOTH ways and its fail-closed cases, on synthetic FD sample sets."""

    def test_stable_warm_set_is_accept(self):
        self.assertEqual(_fd_classify([50, 50, 51, 50], 2, 4), ACCEPT)

    def test_steadily_leaking_set_is_leak(self):
        self.assertEqual(_fd_classify([50, 52, 56, 62], 2, 4), LEAK)

    def test_tolerance_boundary(self):
        self.assertEqual(_fd_classify([50, 51, 52, 52], 2, 4), ACCEPT, "exactly the tolerance is accepted")
        self.assertEqual(_fd_classify([50, 51, 52, 53], 2, 4), LEAK, "tolerance+1 is a leak")

    def test_slow_leak_caught_across_largest_batch(self):
        self.assertEqual(_fd_classify([40, 41, 42, 43], 2, 4), LEAK)

    def test_transient_dip_below_baseline_is_accept(self):
        self.assertEqual(_fd_classify([48, 50, 47, 49], 2, 4), ACCEPT)

    def test_fail_closed_on_missing_or_bad_data(self):
        self.assertEqual(_fd_classify(None, 2, 4), UNAVAILABLE, "null FD totals must not pass")
        self.assertEqual(_fd_classify([50, 50, 51], 2, 4), UNAVAILABLE, "fewer than 4 stages must not pass")
        self.assertEqual(_fd_classify([], 2, 4), UNAVAILABLE, "empty must not pass")
        self.assertEqual(_fd_classify([50, -1, 50, 50], 2, 4), UNAVAILABLE, "a negative count must not pass")


class DiagAcceptComparator(unittest.TestCase):
    """DiagAccept: between-command ecore maps known 0 (accept) vs nonzero (reject) vs missing (fail closed)."""

    def test_all_zero_maps_accept(self):
        self.assertTrue(_diag_accept([(0, 0, 0), (0, 0, 0), (0, 0, 0)], 3))

    def test_nonzero_map_is_rejected(self):
        self.assertFalse(_diag_accept([(0, 0, 0), (1, 0, 0), (0, 0, 0)], 3), "a leaked handle must be caught")
        self.assertFalse(_diag_accept([(0, 0, 0), (0, 0, 2), (0, 0, 0)], 3), "a leaked orphan must be caught")

    def test_fail_closed_on_missing(self):
        self.assertFalse(_diag_accept([(0, 0, 0), None, (0, 0, 0)], 3), "a missing snapshot must not pass")
        self.assertFalse(_diag_accept([(0, 0, 0), (0, 0, 0)], 6), "fewer than required must not pass")
        self.assertFalse(_diag_accept(None, 3), "null diagnostics must not pass")


class ProbeSourceGuards(unittest.TestCase):
    """Static guards so the Python mirror cannot drift from the C# and the fix cannot be quietly undone."""

    def setUp(self):
        self.src = PROBE_CS.read_text(encoding="utf-8")
        self.flat = self.src.replace(" ", "")

    def test_authoritative_comparators_present_and_fail_closed(self):
        self.assertIn("internal static FdVerdict FdClassify(", self.src)
        self.assertIn("internal static bool DiagAccept(", self.src)
        # FdClassify fails closed on too-few / negative samples (the mirror would be stale otherwise).
        self.assertIn("fdTotals.Count<required)returnFdVerdict.Unavailable", self.flat)
        self.assertIn("fd<0)returnFdVerdict.Unavailable", self.flat)

    def test_verdict_tolerance_is_two_not_loosened(self):
        # The brief forbids simply raising +2 to +5. The FD verdict must call FdClassify(totals,2,4).
        self.assertIn("FdClassify(totals,2,4)", self.flat,
                      "the FD verdict tolerance/required was changed -- the brief forbids loosening it")
        self.assertNotIn("FdClassify(totals,5", self.flat)

    def test_fd_verdict_fails_on_unavailable(self):
        # Unavailable must be a FAILED check, not a pass: the Rec passes only on FdVerdict.Accept.
        self.assertIn("fdVerdict==FdVerdict.Accept", self.flat)

    def test_coexist_cleanup_is_bounded_and_disposed_before_snapshot(self):
        self.assertIn("coexist.WaitForExit(2000)", self.flat, "cleanup must bound the reap")
        self.assertIn("coexist.Dispose()", self.flat, "the Process must be disposed before the snapshot")
        # Dispose must come before the reclaim snapshot, which must come before the FD verdict.
        i_dispose = self.src.index("coexist.Dispose()")
        i_snapshot = self.src.index('Capture("coexist-dead"')
        i_verdict = self.src.index("FdClassify(totals")
        self.assertLess(i_dispose, i_snapshot, "Dispose must precede the coexist-dead snapshot")
        self.assertLess(i_snapshot, i_verdict, "the snapshot must precede the verdict")

    def test_warmup_batches_and_between_command_diag_present(self):
        for marker in ('"warmup-baseline"', '"after-batch-10"', '"after-batch-30"', '"after-batch-60"'):
            self.assertIn(marker, self.src, f"missing the explicit warmup/batch stage {marker}")
        self.assertIn("DiagAccept(diags,diagStages.Length)", self.flat, "between-command map assertion missing")

    def test_preserved_flood_timeout_and_identity(self):
        self.assertIn('"flood-child"', self.src, "the finite flood test was dropped")
        self.assertIn('Psi("/bin/sleep","60")', self.flat, "the hung/timeout test was dropped")
        self.assertIn("IdentityMatches", self.src, "the exact-pid kill identity guard was dropped")

    def test_fd_enumeration_is_own_process_only(self):
        self.assertIn("/proc/self/fd", self.src)
        for forbidden in ("/proc/self/environ", "/proc/self/cmdline"):
            self.assertNotIn(forbidden, self.src, f"the probe must not read {forbidden}")


if __name__ == "__main__":
    unittest.main()

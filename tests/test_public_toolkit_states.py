"""Component states: 'removed' only with a proven-absent path, never from a flag."""

from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_public_toolkit_catalogue import CatalogueCase  # noqa: E402
from tvkit import states as st  # noqa: E402
from tvkit.states import PathObservation, report, summary, untouched_state  # noqa: E402

EVIDENCE = hashlib.sha256(b"synthetic read").hexdigest()
GUIDE = "/opt/usr/apps/org.example.guide"


class States(CatalogueCase):
    def setUp(self):
        super().setUp()
        self.cs = self.resolve(["example.guide", "example.voice"])

    def states(self, observations, flags=None):
        return {r.component_id: (r.state, r.reason) for r in report(self.cs, observations, flags)}

    def test_removed_needs_absent_path_with_evidence(self):
        got = self.states({GUIDE: PathObservation(GUIDE, False, EVIDENCE)})
        self.assertEqual(got["example.guide"], ("removed", ""))
        self.assertEqual(got["example.voice"], ("held", "not-reviewed"))

    def test_no_proof_is_failed(self):
        for label, observations in (("missing", {}),
                                    ("no evidence", {GUIDE: PathObservation(GUIDE, False, "")}),
                                    ("other path", {GUIDE: PathObservation(GUIDE + "2", False, EVIDENCE)}),
                                    ("truthy int", {GUIDE: PathObservation(GUIDE, 0, EVIDENCE)})):
            with self.subTest(label):
                self.assertEqual(self.states(observations)["example.guide"][0], "failed")

    def test_disabled_flag_is_never_removed(self):
        got = self.states({GUIDE: PathObservation(GUIDE, True, EVIDENCE)}, {"org.example.guide": True})
        self.assertEqual(got["example.guide"], ("failed", "still-present-disabled:org.example.guide"))
        got = self.states({}, {"org.example.guide": True})
        self.assertEqual(got["example.guide"][0], "failed")

    def test_untouched_states(self):
        comp = self.catalogue.components["example.feeds"]
        path = "/opt/usr/apps/org.example.feeds"
        present = {path: PathObservation(path, True, EVIDENCE)}
        self.assertEqual(untouched_state(comp, self.inventory, present, {"org.example.feeds": True}).state,
                         "disabled")
        self.assertEqual(untouched_state(comp, self.inventory, present, {"org.example.feeds": False}).state,
                         "kept")
        self.assertEqual(untouched_state(comp, self.inventory, present, {}).state, "failed")
        self.assertEqual(untouched_state(comp, self.inventory, {}, {"org.example.feeds": False}).state, "failed")

    def test_summary_counts(self):
        counts = summary(report(self.cs, {GUIDE: PathObservation(GUIDE, False, EVIDENCE)}))
        self.assertEqual(counts, {"removed": 1, "disabled": 0, "kept": 0, "held": 1, "failed": 0})

    def test_negative_control_absence_proof(self):
        """Guard off: without the proof check, a flag-only 'disabled' package would read as removed."""
        flags = {"org.example.guide": True}
        self.assertEqual(self.states({}, flags)["example.guide"][0], "failed")
        with mock.patch.object(st, "_observed", lambda path, obs: PathObservation(path, False, EVIDENCE)):
            self.assertEqual(self.states({}, flags)["example.guide"][0], "removed")


if __name__ == "__main__":
    unittest.main()

"""Integration tests for the OMR rhythmic constraint solver."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from omr_constraint_client import OMRConstraintClient


ROOT = Path(__file__).resolve().parents[1]


class OMRConstraintSolverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.binary = Path(cls.temp.name) / "omr_constraint_solver"
        compiler = "clang++"
        if subprocess.run(["which", compiler], capture_output=True).returncode != 0:
            compiler = "g++"
        subprocess.run(
            [
                compiler, "-std=c++17", "-O2",
                str(ROOT / "omr_constraint_solver.cpp"),
                "-o", str(cls.binary),
            ],
            check=True,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def setUp(self) -> None:
        self.solver = OMRConstraintClient(self.binary)

    def tearDown(self) -> None:
        self.solver.close()

    def test_unique_duration_from_measure_constraint(self) -> None:
        result = self.solver.resolve(
            "4/4",
            [
                {"id": "a", "candidates": [{"duration": "1/2", "confidence": 0.99}]},
                {"id": "b", "candidates": [{"duration": "1/4", "confidence": 0.99}]},
                {"id": "c", "candidates": [
                    {"duration": "1/2", "confidence": 0.52},
                    {"duration": "1/4", "confidence": 0.48},
                ]},
            ],
        )
        self.assertEqual(result["status"], "resolved_unique")
        self.assertEqual(result["solutions"][0]["events"][2]["duration_whole"], "1/4")
        self.assertEqual(result["solutions"][0]["events"][2]["onset_whole"], "3/4")
        self.assertEqual(result["event_support"][2]["eliminated_candidates"], ["1/2"])

    def test_ambiguous_keeps_multiple_musically_valid_readings(self) -> None:
        result = self.solver.resolve(
            "4/4",
            [
                {"id": "a", "candidates": [{"duration": "1/2"}, {"duration": "1/4"}]},
                {"id": "b", "candidates": [{"duration": "1/2"}, {"duration": "1/4"}]},
                {"id": "c", "candidates": [{"duration": "1/2"}, {"duration": "1/4"}]},
            ],
        )
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(result["valid_assignments"], 3)

    def test_inconsistent_candidates_are_not_forced(self) -> None:
        result = self.solver.resolve(
            "4/4",
            [
                {"id": "a", "candidates": [{"duration": "1/2"}]},
                {"id": "b", "candidates": [{"duration": "1/4"}]},
            ],
        )
        self.assertEqual(result["status"], "inconsistent")
        self.assertEqual(result["solutions"], [])

    def test_visual_bbox_is_returned_for_reinspection(self) -> None:
        result = self.solver.resolve(
            "4/4",
            [
                {"id": "a", "candidates": [{"duration": "1/2"}]},
                {"id": "b", "candidates": [{"duration": "1/4"}]},
                {"id": "c", "candidates": [
                    {"duration": "1/2"}, {"duration": "1/4"}
                ], "bbox": {"x": 12, "y": 34, "width": 20, "height": 30}},
            ],
        )
        self.assertEqual(result["event_support"][2]["bbox"]["x"], 12)
        self.assertEqual(result["event_support"][2]["eliminated_candidates"], ["1/2"])

    def test_search_budget_never_claims_unique(self) -> None:
        result = self.solver.resolve(
            "4/4",
            [
                {"id": f"e{i}", "candidates": [
                    {"duration": "1/4"}, {"duration": "1/8"}
                ]}
                for i in range(1, 6)
            ],
            max_nodes=1,
        )
        self.assertEqual(result["status"], "incomplete")
        self.assertTrue(result["truncated"])

    def test_mixed_meter_three_four(self) -> None:
        result = self.solver.resolve(
            "3/4",
            [
                {"id": "a", "candidates": [{"duration": "1/2"}]},
                {"id": "b", "candidates": [{"duration": "1/4"}]},
            ],
        )
        self.assertEqual(result["status"], "resolved_unique")
        self.assertEqual(result["solutions"][0]["events"][1]["onset_whole"], "1/2")

    def test_quintuplet_durations_are_exact(self) -> None:
        result = self.solver.resolve(
            "4/4",
            [{"id": f"q{i}", "candidates": [{"duration": "1/5"}, {"duration": "1/4"}]} for i in range(5)],
        )
        self.assertEqual(result["status"], "resolved_unique")
        self.assertTrue(all(e["duration_whole"] == "1/5" for e in result["solutions"][0]["events"]))

    def test_spurious_event_can_be_removed(self) -> None:
        result = self.solver.resolve(
            "4/4",
            [
                {"id": "a", "candidates": [{"duration": "1/2"}]},
                {"id": "b", "candidates": [{"duration": "1/4"}]},
                {"id": "dup", "candidates": [{"duration": "1/4", "confidence": 0.6},
                                             {"duration": "none", "confidence": 0.4}]},
                {"id": "c", "candidates": [{"duration": "1/4"}]},
            ],
        )
        self.assertEqual(result["status"], "resolved_unique")
        dup = result["solutions"][0]["events"][2]
        self.assertFalse(dup["present"])
        self.assertIn("symbol_existence", result["event_support"][2]["suggested_visual_checks"])

    def test_missing_time_is_reported(self) -> None:
        result = self.solver.resolve(
            "4/4",
            [{"id": "a", "candidates": [{"duration": "1/2"}]}, {"id": "b", "candidates": [{"duration": "1/4"}]}],
        )
        self.assertEqual(result["status"], "inconsistent")
        self.assertEqual(result["gap"], "missing")
        self.assertEqual(result["gap_whole"], "1/4")

    def test_excess_time_is_reported(self) -> None:
        result = self.solver.resolve(
            "3/4",
            [{"id": "a", "candidates": [{"duration": "1/2"}]}, {"id": "b", "candidates": [{"duration": "1/2"}]}],
        )
        self.assertEqual(result["gap"], "excess")
        self.assertEqual(result["gap_whole"], "1/4")

    def _dotted_pair_measure(self, **kw):
        # 2/4: [dotted eighth, sixteenth] [eighth, eighth]; the sixteenth's short beam was missed,
        # so b is read as an eighth: the measure is 1/16 too long, and any of b, c, d could be shortened
        c8 = [{"duration": "1/8", "confidence": 0.6}, {"duration": "1/16", "confidence": 0.4}]
        events = [
            {"id": "a", "candidates": [{"duration": "3/16", "confidence": 0.9}, {"duration": "1/8", "confidence": 0.1}]},
            {"id": "b", "candidates": c8}, {"id": "c", "candidates": c8}, {"id": "d", "candidates": c8},
        ]
        return self.solver.resolve("2/4", events, limit=5, **kw)

    def test_beam_groups_prefer_beat_aligned_solution(self) -> None:
        plain = self._dotted_pair_measure()
        top = plain["solutions"][0]["log_visual_score"]
        self.assertEqual(sum(abs(s["log_visual_score"] - top) < 1e-9 for s in plain["solutions"]), 3)
        grouped = self._dotted_pair_measure(beat="1/4", groups=[["a", "b"], ["c", "d"]])
        best = grouped["solutions"][0]["events"]
        self.assertEqual([e["duration_whole"] for e in best], ["3/16", "1/16", "1/8", "1/8"])
        self.assertGreater(best and grouped["solutions"][0]["log_visual_score"],
                           grouped["solutions"][1]["log_visual_score"] + 1.0)

    def test_groups_require_known_consecutive_events(self) -> None:
        with self.assertRaises(ValueError):
            self._dotted_pair_measure(beat="1/4", groups=[["a", "c"]])
        with self.assertRaises(ValueError):
            self._dotted_pair_measure(groups=[["a", "b"]])


if __name__ == "__main__":
    unittest.main()

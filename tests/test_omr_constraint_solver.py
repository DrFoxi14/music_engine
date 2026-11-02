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


if __name__ == "__main__":
    unittest.main()

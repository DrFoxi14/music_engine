"""Integration tests for the OMR rhythmic constraint solver."""

from __future__ import annotations

import math
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

    def test_quintuplet_slot_durations_are_representable(self) -> None:
        # Quarter + five sixteenth-note quintuplet slots (1/20 each) in 2/4.
        events = [{"id": "q", "candidates": [{"duration": "1/4"}]}]
        events += [
            {"id": f"t{i}", "candidates": [{"duration": "1/20"}, {"duration": "1/16"}]}
            for i in range(5)
        ]
        result = self.solver.resolve("2/4", events)
        self.assertEqual(result["status"], "resolved_unique")
        onsets = [e["onset_whole"] for e in result["solutions"][0]["events"]]
        self.assertEqual(onsets, ["0/1", "1/4", "3/10", "7/20", "2/5", "9/20"])
        for support in result["event_support"][1:]:
            self.assertEqual(support["eliminated_candidates"], ["1/16"])

    def test_septuplet_and_eleven_tuplet_slots_are_representable(self) -> None:
        events = [{"id": f"s{i}", "candidates": [{"duration": "1/28"}]} for i in range(7)]
        events += [{"id": f"e{i}", "candidates": [{"duration": "1/44"}]} for i in range(11)]
        events.append({"id": "h", "candidates": [{"duration": "1/2"}]})
        result = self.solver.resolve("4/4", events)
        self.assertEqual(result["status"], "resolved_unique")

    def test_any_tuplet_and_nested_tuplets_are_exact(self) -> None:
        # A 13-tuplet of sixteenths in one beat: slot 1/52.
        result = self.solver.resolve("1/4", [{"id": f"n{i}", "candidates": [{"duration": "1/52"}]} for i in range(13)])
        self.assertEqual(result["status"], "resolved_unique")
        # 9:8 sixteenths whose last two slots hold a 9:8 of sixty-fourths: 7 x 1/18 + 9 x 1/81.
        events = [{"id": f"o{i}", "candidates": [{"duration": "1/18"}]} for i in range(7)]
        events += [{"id": f"i{i}", "candidates": [{"duration": "1/81"}, {"duration": "1/64"}]} for i in range(9)]
        result = self.solver.resolve("1/2", events, max_note_denominator=64)
        self.assertEqual(result["status"], "resolved_unique")
        self.assertEqual(result["event_support"][-1]["eliminated_candidates"], ["1/64"])

    def test_counts_beyond_64_bits_are_exact_in_log2(self) -> None:
        # 120 events of 1/128 or 1/256 filling 3/4: exactly C(120, 72) readings.
        events = [{"id": f"e{i}", "candidates": [{"duration": "1/128"}, {"duration": "1/256"}]} for i in range(120)]
        result = self.solver.resolve("3/4", events)
        self.assertEqual(result["status"], "ambiguous")
        self.assertTrue(result["valid_assignments_is_lower_bound"])
        self.assertAlmostEqual(result["valid_assignments_log2"], math.log2(math.comb(120, 72)), places=4)
        share = 2 ** (result["event_support"][0]["solution_counts_log2"][0] - result["valid_assignments_log2"])
        self.assertAlmostEqual(share, 72 / 120, places=6)

    def test_grid_overflow_is_rejected(self) -> None:
        events = [{"id": f"p{i}", "candidates": [{"duration": f"1/{p}"}]}
                  for i, p in enumerate((999983, 999979, 999961))]
        with self.assertRaises(ValueError):
            self.solver.resolve("4/4", events)

    def test_solution_counts_and_determined_events(self) -> None:
        # Three bare stems in 4/4: each could be a quarter or a half.
        bare = [{"id": k, "candidates": [{"duration": "1/4"}, {"duration": "1/2"}]} for k in "abc"]
        result = self.solver.resolve("4/4", bare)
        self.assertEqual(result["valid_assignments"], 3)
        for support in result["event_support"]:
            self.assertEqual(support["solution_counts"], [2, 1])
            self.assertFalse(support["determined"])
        # First and last known as quarters: the middle one is implied.
        known = [{"id": "a", "candidates": [{"duration": "1/4"}]}, bare[1], {"id": "c", "candidates": [{"duration": "1/4"}]}]
        result = self.solver.resolve("4/4", known)
        self.assertEqual(result["status"], "resolved_unique")
        self.assertTrue(all(s["determined"] for s in result["event_support"]))
        self.assertEqual(result["event_support"][1]["supported_candidates"], ["1/2"])

    def test_dense_ambiguous_measure_is_solved_exactly_within_default_budget(self) -> None:
        # 3^24 raw combinations; enumeration used to exhaust the node budget.
        events = [
            {"id": f"e{i}", "candidates": [
                {"duration": "1/16", "confidence": 0.6},
                {"duration": "1/32", "confidence": 0.4},
                {"duration": "1/8", "confidence": 0.3},
            ]}
            for i in range(24)
        ]
        result = self.solver.resolve("4/4", events)
        self.assertEqual(result["status"], "ambiguous")
        self.assertFalse(result["truncated"])
        self.assertEqual(result["valid_assignments"], 1606803)
        self.assertFalse(result["valid_assignments_is_lower_bound"])
        for support in result["event_support"]:
            self.assertTrue(support["support_is_complete"])
            self.assertEqual(support["unverified_candidates"], [])
            self.assertEqual(sorted(support["supported_candidates"]), ["1/16", "1/32", "1/8"])
        scores = [s["log_visual_score"] for s in result["solutions"]]
        self.assertEqual(len(scores), 20)
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_dense_measure_elimination_is_exact(self) -> None:
        # Sixteen events in 4/4 can only be sixteenth notes.
        events = [
            {"id": f"e{i}", "candidates": [{"duration": "1/8"}, {"duration": "1/16"}]}
            for i in range(16)
        ]
        result = self.solver.resolve("4/4", events)
        self.assertEqual(result["status"], "resolved_unique")
        for support in result["event_support"]:
            self.assertEqual(support["eliminated_candidates"], ["1/8"])

    def test_solutions_are_the_best_scoring_assignments(self) -> None:
        import itertools
        from fractions import Fraction

        options = [
            [("1/4", 0.9), ("1/2", 0.3), ("1/8", 0.5)],
            [("1/4", 0.6), ("1/8", 0.8)],
            [("1/2", 0.7), ("1/4", 0.4), ("3/8", 0.2)],
            [("1/8", 0.9), ("1/4", 0.5)],
            [("1/4", 0.4), ("1/8", 0.6), ("1/2", 0.1)],
        ]
        events = [
            {"id": f"e{i}", "candidates": [{"duration": d, "confidence": c} for d, c in opts]}
            for i, opts in enumerate(options)
        ]
        valid = []
        for combo in itertools.product(*options):
            if sum(Fraction(d) for d, _ in combo) == 1:
                score = sum(math.log(c) for _, c in combo)
                valid.append(round(score, 4))
        valid.sort(reverse=True)

        result = self.solver.resolve("4/4", events, limit=4)
        self.assertEqual(result["valid_assignments"], len(valid))
        self.assertEqual([round(s["log_visual_score"], 4) for s in result["solutions"]], valid[:4])
        for solution in result["solutions"]:
            total = sum(Fraction(e["duration_whole"]) for e in solution["events"])
            self.assertEqual(total, 1)


if __name__ == "__main__":
    unittest.main()

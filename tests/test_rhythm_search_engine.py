"""Small integration suite for the C++ rhythm search engine."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from rhythm_search_client import RhythmSearchClient  # noqa: E402


class RhythmSearchEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.executable = Path(cls.temp_dir.name) / "rhythm_search_engine"
        compiler = os.environ.get("CXX", "clang++")
        if subprocess.run(["which", compiler], capture_output=True).returncode != 0:
            compiler = "g++"
        subprocess.run(
            [compiler, "-std=c++17", "-O2", str(ROOT / "rhythm_search_engine.cpp"), "-o", str(cls.executable)],
            check=True,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp_dir.cleanup()

    def setUp(self) -> None:
        self.engine = RhythmSearchClient(self.executable, 4, 4, 16)

    def tearDown(self) -> None:
        self.engine.close()

    def test_exact_half_and_two_quarters_has_three_orders(self) -> None:
        result = self.engine.search("NOTES", exact={"note@2": 1, "note@4": 2}, limit=10, max_nodes=10000)
        self.assertEqual(len(result["patterns"]), 3)
        self.assertFalse(result["summary"]["truncated"])
        self.assertFalse(result["summary"]["has_more"])
        orders = [tuple(event["descriptor"] for event in p["events"]) for p in result["patterns"]]
        self.assertEqual(len(set(orders)), 3)

    def test_dotted_duration_filter_returns_all_twelve_permutations(self) -> None:
        result = self.engine.search(
            "NOTES",
            exact={"dotted_note@4": 1, "note@4": 2, "note@8": 1},
            durations=["3/8", "1/4", "1/8"],
            limit=20,
            max_nodes=10000,
        )
        self.assertEqual(len(result["patterns"]), 12)
        for pattern in result["patterns"]:
            durations = {event["duration_whole"] for event in pattern["events"]}
            self.assertTrue(durations.issubset({"3/8", "1/4", "1/8"}))
            self.assertEqual(pattern["summary"]["dotted_events"], 1)

    def test_triplet_slot_filter(self) -> None:
        result = self.engine.search(
            "RESTS",
            exact={"triplet@8": 1, "note@2": 1, "note@4": 1},
            kinds=["triplet", "note"],
            triplet_patterns=["NRN"],
            limit=20,
            max_nodes=10000,
        )
        self.assertEqual(len(result["patterns"]), 6)
        for pattern in result["patterns"]:
            triplets = [event for event in pattern["events"] if event["kind"] == "triplet"]
            self.assertEqual(len(triplets), 1)
            self.assertEqual(triplets[0]["triplet_slots"], ["note", "rest", "note"])

    def test_prefix_and_contains_order_filters(self) -> None:
        prefix = self.engine.search(
            "NOTES", exact={"note@2": 1, "note@4": 2}, prefix=["note@2"], limit=10
        )
        self.assertEqual(len(prefix["patterns"]), 1)
        self.assertEqual(prefix["patterns"][0]["events"][0]["descriptor"], "note@2")

        contains = self.engine.search(
            "NOTES", exact={"note@2": 1, "note@4": 2}, contains=["note@4", "note@2"], limit=10
        )
        self.assertEqual(len(contains["patterns"]), 2)

    def test_known_beginning_asks_what_can_follow(self) -> None:
        # the vision engine has read a quarter and a half and sees one more notehead:
        # only one more quarter fits (OMR dialogue, IDSR)
        notes = self.engine.search(
            "NOTES", prefix=["note@4", "note@2"], min_events=3, max_events=3, max_note_slots=3, limit=10
        )
        self.assertEqual([[e["descriptor"] for e in p["events"]] for p in notes["patterns"]],
                         [["note@4", "note@2", "note@4"]])
        # no notehead there: the prefix is enforced while searching, so a rest is found at once
        rests = self.engine.search(
            "RESTS", prefix=["note@4", "note@2"], min_events=3, max_events=3, max_note_slots=2, limit=10
        )
        self.assertFalse(rests["summary"]["truncated"])
        self.assertIn(["note@4", "note@2", "rest@4"], [[e["descriptor"] for e in p["events"]] for p in rests["patterns"]])
        self.assertLess(rests["summary"]["nodes_visited"], 1000)

    def test_positions_constrain_each_event(self) -> None:
        # what the vision is sure of, symbol by symbol: a quarter, a half, then one notehead whose
        # value is unknown among four; only a quarter completes the measure
        result = self.engine.search(
            "NOTES", positions=[["note@4"], ["note@2"], ["note@4", "note@8", "dotted_note@8", "note@16"]], limit=10
        )
        self.assertEqual([[e["descriptor"] for e in p["events"]] for p in result["patterns"]],
                         [["note@4", "note@2", "note@4"]])
        self.assertFalse(result["summary"]["has_more"])

    def test_positions_tuplet_masks_and_wildcard(self) -> None:
        result = self.engine.search(
            "RESTS", positions=[["*"], ["rest@4"], ["triplet@8#NRN"]], limit=50, max_nodes=100000
        )
        self.assertTrue(result["patterns"])
        for p in result["patterns"]:
            self.assertEqual(len(p["events"]), 3)
            self.assertEqual(p["events"][1]["descriptor"], "rest@4")
            self.assertEqual(p["events"][2]["tuplet_slots"], ["note", "rest", "note"])
        with self.assertRaises(ValueError):
            self.engine.search("NOTES", positions=[["note@4"]], max_events=0)

    def test_page_limit_reports_more_results(self) -> None:
        result = self.engine.search("NOTES", exact={"note@2": 1, "note@4": 2}, limit=1)
        self.assertEqual(len(result["patterns"]), 1)
        self.assertTrue(result["summary"]["has_more"])

    def test_node_budget_reports_truncation(self) -> None:
        result = self.engine.search("RESTS", max_nodes=1, min_events=1, max_events=8, limit=10)
        self.assertTrue(result["summary"]["truncated"])

    def test_note_category_contains_no_rests(self) -> None:
        result = self.engine.search("NOTES", limit=5, max_nodes=10000)
        for pattern in result["patterns"]:
            self.assertEqual(pattern["summary"]["rest_slots"], 0)

    def test_full_measure_rest_is_unique_in_four_four(self) -> None:
        result = self.engine.search("RESTS", exact={"rest@1": 1}, limit=5)
        self.assertEqual(len(result["patterns"]), 1)

    def test_full_measure_rest_for_three_four(self) -> None:
        with RhythmSearchClient(self.executable, 3, 4, 16) as engine:
            result = engine.search("RESTS", exact={"measure_rest@0": 1}, limit=5)
            self.assertEqual(len(result["patterns"]), 1)
            self.assertEqual(result["patterns"][0]["events"][0]["kind"], "measure_rest")

    def test_maximum_subdivision_256th_notes(self) -> None:
        with RhythmSearchClient(self.executable, 4, 4, 256) as engine:
            result = engine.search("NOTES", exact={"note@256": 256}, limit=1, max_nodes=10000)
            self.assertEqual(len(result["patterns"]), 1)
            self.assertEqual(result["patterns"][0]["event_count"], 256)
            self.assertEqual(result["patterns"][0]["events"][0]["duration_whole"], "1/256")


    def test_quintuplet_fills_four_four_exactly(self) -> None:
        result = self.engine.search(
            "NOTES",
            exact={"tuplet@4:5:4": 1},
            kinds=["tuplet"],
            tuplet_ratios=["5:4"],
            limit=10,
        )
        self.assertEqual(len(result["patterns"]), 1)
        event = result["patterns"][0]["events"][0]
        self.assertEqual(event["duration_whole"], "1/1")
        self.assertEqual(event["tuplet_ratio"], "5:4")
        self.assertEqual(event["tuplet_slots"], ["note"] * 5)
        self.assertEqual(event["tuplet_slot_duration_whole"], "1/5")
        self.assertEqual(result["patterns"][0]["summary"]["tuplet_groups"], 1)

    def test_quadrupet_ratio_four_to_three_in_three_four(self) -> None:
        with RhythmSearchClient(self.executable, 3, 4, 16) as engine:
            result = engine.search(
                "NOTES",
                exact={"tuplet@4:4:3": 1},
                tuplet_ratios=["4:3"],
                limit=5,
            )
        self.assertEqual(len(result["patterns"]), 1)
        event = result["patterns"][0]["events"][0]
        self.assertEqual(event["tuplet_ratio"], "4:3")
        self.assertEqual(event["tuplet_slot_duration_whole"], "3/16")
        self.assertEqual(event["duration_whole"], "3/4")

    def test_sextuplet_ratio_six_to_four(self) -> None:
        result = self.engine.search(
            "NOTES",
            exact={"tuplet@4:6:4": 1},
            tuplet_ratios=["6:4"],
            limit=5,
        )
        event = result["patterns"][0]["events"][0]
        self.assertEqual(event["tuplet_ratio"], "6:4")
        self.assertEqual(event["tuplet_slot_duration_whole"], "1/6")
        self.assertEqual(len(event["tuplet_slots"]), 6)

    def test_septuplet_ratio_seven_to_four(self) -> None:
        result = self.engine.search(
            "NOTES",
            exact={"tuplet@4:7:4": 1},
            tuplet_ratios=["7:4"],
            limit=5,
        )
        event = result["patterns"][0]["events"][0]
        self.assertEqual(event["tuplet_ratio"], "7:4")
        self.assertEqual(event["tuplet_slot_duration_whole"], "1/7")
        self.assertEqual(len(event["tuplet_slots"]), 7)

    def test_nonuplet_ratio_nine_to_eight(self) -> None:
        result = self.engine.search(
            "NOTES",
            exact={"tuplet@8:9:8": 1},
            tuplet_ratios=["9:8"],
            limit=5,
        )
        event = result["patterns"][0]["events"][0]
        self.assertEqual(event["tuplet_ratio"], "9:8")
        self.assertEqual(event["tuplet_slot_duration_whole"], "1/9")
        self.assertEqual(len(event["tuplet_slots"]), 9)

    def test_uncommon_ratio_is_generated_on_demand(self) -> None:
        with RhythmSearchClient(self.executable, 3, 4, 16) as engine:
            result = engine.search(
                "NOTES",
                exact={"tuplet@4:5:3": 1},
                limit=5,
            )
        self.assertEqual(len(result["patterns"]), 1)
        event = result["patterns"][0]["events"][0]
        self.assertEqual(event["tuplet_ratio"], "5:3")
        self.assertEqual(event["duration_whole"], "3/4")
        self.assertEqual(event["tuplet_slot_duration_whole"], "3/20")

    def test_uncommon_ratio_can_be_requested_by_tuplet_pattern_alone(self) -> None:
        with RhythmSearchClient(self.executable, 3, 4, 16) as engine:
            result = engine.search(
                "RESTS",
                exact={"tuplet@4:5:3": 1},
                tuplet_patterns=["5:3:NNRNN"],
                limit=5,
            )
        self.assertEqual(len(result["patterns"]), 1)
        event = result["patterns"][0]["events"][0]
        self.assertEqual(event["tuplet_ratio"], "5:3")
        self.assertEqual(event["tuplet_slots"], ["note", "note", "rest", "note", "note"])
        self.assertEqual(event["tuplet_slot_duration_whole"], "3/20")

    def test_undecuplet_ratio_eleven_to_eight(self) -> None:
        result = self.engine.search(
            "NOTES",
            exact={"tuplet@8:11:8": 1},
            tuplet_ratios=["11:8"],
            limit=5,
        )
        self.assertEqual(len(result["patterns"]), 1)
        event = result["patterns"][0]["events"][0]
        self.assertEqual(event["tuplet_ratio"], "11:8")
        self.assertEqual(event["duration_whole"], "1/1")
        self.assertEqual(event["tuplet_slot_duration_whole"], "1/11")
        self.assertEqual(len(event["tuplet_slots"]), 11)

    def test_generic_tuplet_rest_and_note_slot_pattern_filter(self) -> None:
        result = self.engine.search(
            "RESTS",
            exact={"tuplet@4:5:4": 1},
            tuplet_ratios=["5:4"],
            tuplet_patterns=["5:4:NNRNN"],
            limit=5,
        )
        self.assertEqual(len(result["patterns"]), 1)
        event = result["patterns"][0]["events"][0]
        self.assertEqual(event["tuplet_slots"], ["note", "note", "rest", "note", "note"])
        self.assertEqual(event["note_slots"], 4)
        self.assertEqual(event["rest_slots"], 1)

    def test_tuplet_ratio_filter_excludes_other_ratios(self) -> None:
        result = self.engine.search(
            "NOTES",
            exact={"tuplet@4:5:4": 1},
            tuplet_ratios=["7:4"],
            limit=5,
        )
        self.assertEqual(len(result["patterns"]), 0)
        self.assertEqual(result["summary"]["truncated"], False)

    def test_legacy_triplet_descriptor_and_pattern_remain_compatible(self) -> None:
        result = self.engine.search(
            "RESTS",
            exact={"triplet@8": 1, "note@2": 1, "note@4": 1},
            kinds=["triplet", "note"],
            triplet_patterns=["NRN"],
            limit=20,
        )
        self.assertGreater(len(result["patterns"]), 0)
        for pattern in result["patterns"]:
            event = next(item for item in pattern["events"] if item["kind"] == "triplet")
            self.assertEqual(event["tuplet_ratio"], "3:2")
            self.assertEqual(event["triplet_slots"], ["note", "rest", "note"])

    def test_contains_kmp_handles_overlapping_prefixes(self) -> None:
        with RhythmSearchClient(self.executable, 4, 4, 16) as engine:
            result = engine.search(
                "NOTES",
                exact={"note@8": 6, "note@4": 1},
                contains=["note@8", "note@8", "note@4"],
                limit=20,
                max_nodes=10000,
            )
            self.assertFalse(result["summary"]["truncated"])
            self.assertEqual(len(result["patterns"]), 5)
            for pattern in result["patterns"]:
                descriptors = [event["descriptor"] for event in pattern["events"]]
                self.assertTrue(any(
                    descriptors[i:i + 3] == ["note@8", "note@8", "note@4"]
                    for i in range(len(descriptors) - 2)
                ))



if __name__ == "__main__":
    unittest.main()

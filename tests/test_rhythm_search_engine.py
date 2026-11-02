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


if __name__ == "__main__":
    unittest.main()

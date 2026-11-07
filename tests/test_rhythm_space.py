"""Tests for the complete rhythm space (COUNT / GET / PAGE / SAMPLE / PARSE).

Small configurations are cross-checked pattern-by-pattern against an
independent brute-force enumerator of the same grammar.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from search_engine_client import SearchEngineClient  # noqa: E402
from rhythm_space_reference import Reference, matches_template  # noqa: E402


def all_patterns(engine, category, template=None, **options):
    out = []
    start = 0
    while True:
        page = engine.page(start, 1000, category, template, **options)
        out += page["patterns"]
        start += 1000
        if not page["summary"]["has_more"]:
            return out


def compositions_into_powers_of_two(n, kinds):
    """Sequences of power-of-two parts summing to n, each part in `kinds` forms."""
    ways = [0] * (n + 1)
    ways[0] = 1
    for total in range(1, n + 1):
        p = 1
        while p <= total:
            ways[total] += kinds * ways[total - p]
            p *= 2
    return ways[n]


class RhythmSpaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.executable = Path(cls.temp_dir.name) / "search_engine"
        compiler = os.environ.get("CXX", "clang++")
        if subprocess.run(["which", compiler], capture_output=True).returncode != 0:
            compiler = "g++"
        subprocess.run(
            [compiler, "-std=c++17", "-O2", str(ROOT / "search_engine.cpp"), "-o", str(cls.executable)],
            check=True,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp_dir.cleanup()

    def engine(self, num, den, max_den):
        engine = SearchEngineClient(self.executable, num, den, max_den)
        self.addCleanup(engine.close)
        return engine

    # -- exact agreement with brute force -------------------------------------------------

    def assert_matches_reference(self, num, den, max_den, **cfg):
        reference = Reference(num, den, max_den, **cfg)
        expected = {"ALL": set(), "NOTES": set(), "RESTS": set()}
        for items, has_rest in reference.measure():
            text = " ".join(t for t, _ in items)
            expected["ALL"].add(text)
            expected["RESTS" if has_rest else "NOTES"].add(text)
        engine = self.engine(num, den, max_den)
        for category, want in expected.items():
            self.assertEqual(int(engine.count(category, **cfg)["count"]), len(want), category)
            patterns = all_patterns(engine, category, **cfg)
            got = [p["notation"] for p in patterns]
            self.assertEqual(len(got), len(set(got)), "duplicate patterns")
            self.assertEqual(set(got), want, category)
            for pattern in patterns:
                total = sum(Fraction(e["duration"]) for e in pattern["events"])
                self.assertEqual(total, Fraction(num, den))
                rests = sum(e["kind"] != "note" for e in pattern["events"])
                if category == "NOTES":
                    self.assertEqual(rests, 0)
                if category == "RESTS":
                    self.assertGreater(rests, 0)

    def test_nested_tuplets_match_brute_force(self) -> None:
        self.assert_matches_reference(3, 8, 8, max_dots=1, depth=2, max_actual=4, longest=1)

    def test_three_levels_of_nesting_match_brute_force(self) -> None:
        self.assert_matches_reference(1, 4, 8, max_dots=1, depth=3, max_actual=4, longest=1, unit_dots=1)

    def test_any_normals_and_breves_match_brute_force(self) -> None:
        self.assert_matches_reference(2, 4, 4, max_dots=1, depth=1, max_actual=4, longest=2,
                                      tuplet_normals="any", max_normal=3)

    def test_single_item_tuplets_and_double_dots_match_brute_force(self) -> None:
        self.assert_matches_reference(1, 4, 8, max_dots=0, depth=3, max_actual=3, longest=1,
                                      min_tuplet_items=1)
        self.assert_matches_reference(2, 4, 4, max_dots=2, depth=2, max_actual=3, longest=1)

    def test_explicit_ratio_list_matches_brute_force(self) -> None:
        cfg = dict(max_dots=1, depth=1, longest=1)
        reference = Reference(5, 8, 8, ratios=[(5, 4), (2, 3), (7, 6)], **cfg)
        engine = self.engine(5, 8, 8)
        result = engine.count("ALL", tuplet_ratios="5:4,2:3,7:6", **cfg)
        self.assertEqual(int(result["count"]), len(reference.measure()))

    def test_larger_space_count_matches_brute_force(self) -> None:
        cfg = dict(max_dots=1, depth=2, max_actual=4, longest=1)
        reference = Reference(2, 4, 8, **cfg).measure()
        result = self.engine(2, 4, 8).count("ALL", **cfg)
        self.assertEqual(int(result["count"]), len(reference))  # 221155
        self.assertEqual(int(result["notes_only"]), sum(1 for _, rest in reference if not rest))

    def test_templates_match_brute_force(self) -> None:
        cfg = dict(max_dots=1, depth=2, max_actual=4, longest=1)
        reference = Reference(3, 8, 8, **cfg)
        sequences = reference.measure()
        engine = self.engine(3, 8, 8)
        templates = [
            ["n8", "*"],
            ["*", "r8"],
            ["?", "n8.", "*"],
            ["*", "[3:2 n8 n8 n8]", "*"],
            ["[3:2 n8 r4]", "?"],
            ["*=1/8", "+"],
            ["+", "*=3/16"],
            ["n8", "*=1/8", "?"],
        ]
        for tokens in templates:
            want = [(items, rest) for items, rest in sequences
                    if matches_template(items, tokens, reference.G)]
            text = " ".join(tokens)
            for category, keep in (("ALL", lambda r: True), ("NOTES", lambda r: not r), ("RESTS", lambda r: r)):
                expected = {" ".join(t for t, _ in items) for items, rest in want if keep(rest)}
                got = [p["notation"] for p in all_patterns(engine, category, text, **cfg)]
                self.assertEqual(len(got), len(set(got)), (text, category))
                self.assertEqual(set(got), expected, (text, category))

    # -- big numbers ----------------------------------------------------------------------

    def test_huge_counts_are_exact(self) -> None:
        # Without tuplets or dots, a 4/4 bar at 1/256 resolution is a composition
        # of 256 into powers of two; with rests each part has two forms.
        engine = self.engine(4, 4, 256)
        result = engine.count("ALL", depth=0, max_dots=0, longest=1, measure_rest=False)
        self.assertEqual(int(result["notes_only"]), compositions_into_powers_of_two(256, 1))
        self.assertEqual(int(result["all"]), compositions_into_powers_of_two(256, 2))
        self.assertGreater(result["digits"], 70)

    def test_any_index_of_a_huge_space_decodes_to_a_valid_bar(self) -> None:
        engine = self.engine(4, 4, 256)
        total = int(engine.count("RESTS")["count"])
        self.assertGreater(len(str(total)), 400)
        for index in (0, 1, total // 3, total // 2 + 12345, total - 1):
            pattern = engine.get(index, "RESTS")
            self.assertEqual(int(pattern["index"]), index)
            self.assertEqual(sum(Fraction(e["duration"]) for e in pattern["events"]), 1)
            self.assertTrue(any(e["kind"] != "note" for e in pattern["events"]))
        with self.assertRaises(ValueError):
            engine.get(total, "RESTS")

    def test_sample_is_reproducible_and_valid(self) -> None:
        engine = self.engine(7, 8, 32)
        first = engine.sample(5, "ALL", seed=42)["patterns"]
        second = engine.sample(5, "ALL", seed=42)["patterns"]
        self.assertEqual([p["index"] for p in first], [p["index"] for p in second])
        for pattern in first:
            self.assertEqual(sum(Fraction(e["duration"]) for e in pattern["events"]), Fraction(7, 8))

    # -- nested tuplets as notation ------------------------------------------------------

    def test_triplet_inside_septuplet_is_part_of_the_space(self) -> None:
        engine = self.engine(1, 4, 16)
        options = dict(depth=2, tuplet_ratios="7:4,3:2", values="16", dots="0", max_dots=0)
        notations = {p["notation"] for p in all_patterns(engine, "NOTES", **options)}
        self.assertIn("[7:4 n16 n16 [3:2 n16 n16 n16] n16 n16 n16]", notations)
        self.assertIn("[7:4 [3:2 n16 n16 n16] n16 n16 n16 n16 n16]", notations)

    def test_parse_reports_exact_nested_durations(self) -> None:
        engine = self.engine(2, 4, 16)
        result = engine.parse("[7:4 n16 n16 [3:2 n16 n16 n16] n16 n16 n16] n4")
        self.assertTrue(result["fills_measure"])
        durations = [e["duration"] for e in result["events"]]
        self.assertEqual(durations[:2], ["1/28", "1/28"])
        self.assertEqual(durations[2:5], ["1/42", "1/42", "1/42"])
        self.assertEqual(result["events"][2]["tuplets"], ["7:4", "3:2"])
        self.assertEqual(result["events"][-1]["onset"], "1/4")
        self.assertEqual(result["summary"]["max_tuplet_depth"], 2)

    def test_template_with_nested_tuplet_and_gap(self) -> None:
        engine = self.engine(2, 4, 16)
        template = "[7:4 n16 n16 [3:2 n16 n16 n16] n16 n16 n16] *=1/4"
        result = engine.count("ALL", template, depth=1, max_dots=0)
        engine_all = all_patterns(engine, "ALL", template, depth=1, max_dots=0)
        self.assertEqual(int(result["count"]), len(engine_all))
        self.assertTrue(all(p["notation"].startswith("[7:4 ") for p in engine_all))

    def test_invalid_notation_is_rejected(self) -> None:
        engine = self.engine(4, 4, 16)
        with self.assertRaises(ValueError):
            engine.parse("[3:2 n8 n8]")  # two eighths are not a whole number of units of 3
        with self.assertRaises(ValueError):
            engine.parse("n3")
        with self.assertRaises(ValueError):
            engine.count("ALL", depth=9)


if __name__ == "__main__":
    unittest.main()

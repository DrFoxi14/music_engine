"""Optimal inference: deduce what the bar implies, ask the Visual Engine the rest.

The scenario from the design discussion: three bare stems in 4/4. The Visual
Engine says the first and last are quarters; the measure alone implies the
middle one is a half, so nothing is asked. Only then is its pitch read.
"""

from __future__ import annotations

import itertools
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
from stem_first_client import (StemFirstClient, infer_measure, optimal_question_count,  # noqa: E402
                               pitch_inputs, _feature_map, _restrict)
from omr_constraint_client import OMRConstraintClient  # noqa: E402
from pitch_context_client import PitchContextClient  # noqa: E402
from search_engine_client import SearchEngineClient  # noqa: E402
from pipeline_sweep import Sweep, patterns  # noqa: E402

S = 10


def head(head_id, x, y):
    return {"id": head_id, "bbox": {"left": x - 6, "top": y - 4.5, "right": x + 6, "bottom": y + 4.5}, "fill": "unknown"}


def bare_bar(n, ys=None):
    """n stems with nothing attached (no flag, no beam), heads at the given heights."""
    ys = ys or [140, 130, 120, 110, 100, 135, 125][:n]
    heads = [head(f"h{i}", 100 + 60 * i, ys[i]) for i in range(n)]
    stems = []
    for i, y in enumerate(ys):
        x = 100 + 60 * i
        if y >= 120:
            stems.append({"id": f"s{i}", "x1": x + 6, "y1": y, "x2": x + 6, "y2": y - 35})
        else:
            stems.append({"id": f"s{i}", "x1": x - 6, "y1": y, "x2": x - 6, "y2": y + 35})
    return stems, heads


def compile_binary(temp, name):
    compiler = "clang++"
    if subprocess.run(["which", compiler], capture_output=True).returncode != 0:
        compiler = "g++"
    out = Path(temp) / name
    subprocess.run([compiler, "-std=c++17", "-O2", str(ROOT / f"{name}.cpp"), "-o", str(out)], check=True)
    return out


class OptimalInferenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        for name in ("stem_first_engine", "omr_constraint_solver", "pitch_context_engine", "search_engine"):
            setattr(cls, name, compile_binary(cls.temp.name, name))

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.stem = StemFirstClient(self.stem_first_engine)
        self.solver = OMRConstraintClient(self.omr_constraint_solver)
        self.addCleanup(self.stem.close)
        self.addCleanup(self.solver.close)

    def never_ask(self, event_id, feature):
        raise AssertionError(f"asked {feature} of {event_id} although the bar implies it")

    # -- the scenario --------------------------------------------------------------------

    def test_three_bare_stems_first_and_last_quarters_imply_a_half(self):
        stems, heads = bare_bar(3)
        result = infer_measure(self.stem, self.solver, meter="4/4", spacing=S, stems=stems, noteheads=heads,
                               known={"s0": {"duration": "1/4"}, "s2": {"duration": "1/4"}},
                               answer=self.never_ask)
        self.assertEqual(result["status"], "resolved_unique")
        self.assertEqual(result["durations"], {"s0": "1/4", "s1": "1/2", "s2": "1/4"})
        self.assertEqual(result["questions"], [])
        self.assertIn("s1", result["implied_without_questions"])

        # Only now: which note is the half? Pitch from the notehead position.
        notes, pitch_stems = pitch_inputs(result["analysis"], heads, stems)
        with PitchContextClient(self.pitch_context_engine) as pitch:
            reading = pitch.resolve(clef="treble", staff_top_y=100, line_spacing=10, notes=notes, stems=pitch_stems)
        letters = {n["id"]: f"{n['letter']}{n['octave']}" for n in reading["notes"]}
        self.assertEqual(letters["h1"], "G4")

    def test_structure_alone_can_decide_everything(self):
        # Six beamed eighths fill 3/4 of a 4/4 bar; the bare stem must be a quarter,
        # even with head fill and dots never looked at.
        xs = [100 + 30 * i for i in range(6)]
        stems = [{"id": f"e{i}", "x1": x + 6, "y1": 130, "x2": x + 6, "y2": 90} for i, x in enumerate(xs)]
        heads = [head(f"he{i}", x, 130) for i, x in enumerate(xs)]
        beam = {"id": "b", "x1": xs[0] + 6, "y1": 90, "x2": xs[-1] + 6, "y2": 90, "thickness": 4}
        last_stems, last_heads = bare_bar(1, [120])
        last_stems[0]["x1"] = last_stems[0]["x2"] = 306
        last_heads[0]["bbox"] = {"left": 294, "top": 115.5, "right": 306, "bottom": 124.5}
        result = infer_measure(self.stem, self.solver, meter="4/4", spacing=S, stems=stems + last_stems,
                               noteheads=heads + last_heads, beams=[beam], answer=self.never_ask)
        self.assertEqual(result["status"], "resolved_unique")
        self.assertEqual(result["durations"]["s0"], "1/4")
        self.assertEqual(result["questions"], [])

    # -- optimality -------------------------------------------------------------------------

    def assert_optimal_for_every_truth(self, meter, stems, heads, **extra):
        """For every reading the bar allows, the loop asks exactly the optimal worst case."""
        analysis = self.stem.analyze(S, meter=meter, stems=stems, noteheads=heads,
                                     unchecked=("fill", "dots"), **extra)
        events = _restrict(analysis, {})
        rhythm = self.solver.resolve(meter, events, limit=100, max_nodes=10_000_000)
        self.assertLessEqual(rhythm["valid_assignments"], 100)
        ids = [e["id"] for e in events]
        solutions = [[ev["duration_whole"] for ev in s["events"]] for s in rhythm["solutions"]]
        features = _feature_map(analysis)
        optimum, _ = optimal_question_count(solutions, ids, features)
        self.assertIsNotNone(optimum, "exact planner exceeded its budget")
        worst = 0
        for truth in solutions:
            facts = {i: features[i][d] for i, d in zip(ids, truth)}
            result = infer_measure(self.stem, self.solver, meter=meter, spacing=S, stems=stems, noteheads=heads,
                                   answer=lambda e, f: facts[e][f], **extra)
            self.assertEqual(result["status"], "resolved_unique")
            self.assertEqual([result["durations"][i] for i in ids], truth)
            worst = max(worst, len(result["questions"]))
        self.assertEqual(worst, optimum)
        # A yes/no-style check cannot beat the information bound.
        self.assertGreaterEqual(2 ** max(worst, 0) * 2, len(solutions))
        return len(solutions), worst

    def test_question_count_is_optimal_for_bare_stems(self):
        for n, meter in ((2, "2/4"), (3, "4/4"), (4, "4/4"), (3, "3/4"), (4, "5/4")):
            stems, heads = bare_bar(n)
            readings, worst = self.assert_optimal_for_every_truth(meter, stems, heads)
            self.assertGreaterEqual(readings, 1)

    def test_three_bare_stems_without_hints_need_three_checks(self):
        stems, heads = bare_bar(3)
        readings, worst = self.assert_optimal_for_every_truth("4/4", stems, heads)
        self.assertEqual(readings, 6)   # (1/4 1/4 1/2) x3 orders + (1/4 3/8 3/8) x3 orders
        # No check splits these six readings 3/3 (every split leaves 4 or 5 on
        # one side), so 3 checks cannot always suffice: the guaranteed optimum is 4.
        self.assertEqual(worst, 4)

    def test_double_dots_are_never_silently_misread(self):
        # 9/8 = double-dotted half + quarter, or dotted half + dotted quarter.
        # Assuming at most one dot would "deduce" the wrong reading; the default
        # range (two dots) keeps both and asks.
        stems, heads = bare_bar(2)
        truth = {"s0": {"fill": "hollow", "dots": 2}, "s1": {"fill": "filled", "dots": 0}}  # 7/8 + 1/4
        result = infer_measure(self.stem, self.solver, meter="9/8", spacing=S, stems=stems, noteheads=heads,
                               answer=lambda e, f: truth[e][f])
        self.assertEqual(result["status"], "resolved_unique")
        self.assertEqual(result["durations"], {"s0": "7/8", "s1": "1/4"})
        self.assertGreater(len(result["questions"]), 0)

    def test_dot_range_widens_when_vision_reports_more_dots(self):
        stems, heads = bare_bar(2)
        truth = {"s0": {"fill": "hollow", "dots": 3}, "s1": {"fill": "filled", "dots": 2}}  # 15/16 + 7/16
        result = infer_measure(self.stem, self.solver, meter="11/8", spacing=S, stems=stems, noteheads=heads,
                               answer=lambda e, f: truth[e][f])
        self.assertEqual(result["status"], "resolved_unique")
        self.assertEqual(result["durations"], {"s0": "15/16", "s1": "7/16"})
        self.assertIn(("s0", "dots", 3), [(q["event_id"], q["feature"], q["answer"]) for q in result["questions"]])

    def test_deduction_is_exact_only_inside_the_unchecked_range(self):
        # 19/16 = triple-dotted half + quarter. With at most two dots assumed the
        # truth is not among the readings, so the loop converges on a wrong one
        # (dotted half + double-dotted quarter). Declaring the wider range
        # restores the truth.
        stems, heads = bare_bar(2)
        truth = {"s0": {"fill": "hollow", "dots": 3}, "s1": {"fill": "filled", "dots": 0}}
        narrow = infer_measure(self.stem, self.solver, meter="19/16", spacing=S, stems=stems, noteheads=heads,
                               answer=lambda e, f: truth[e][f])
        self.assertEqual(narrow["durations"], {"s0": "3/4", "s1": "7/16"})
        wide = infer_measure(self.stem, self.solver, meter="19/16", spacing=S, stems=stems, noteheads=heads,
                             unchecked_max_dots=3, answer=lambda e, f: truth[e][f])
        self.assertEqual(wide["durations"], {"s0": "15/16", "s1": "1/4"})

    # -- exhaustive and sampled end-to-end sweeps -----------------------------------------

    def run_sweep(self, meter, max_den, options, mode, count=0, category="ALL"):
        with SearchEngineClient(self.search_engine, meter[0], meter[1], max_den) as search:
            sweep = Sweep(self.stem, self.solver)
            for i, pattern in enumerate(patterns(search, mode, count, options, category=category)):
                sweep.check(pattern["notation"], meter, seed=i)
        self.assertEqual(sweep.failures, [], sweep.summary())
        return sweep

    def test_every_bar_of_a_small_space_is_read_back(self):
        sweep = self.run_sweep((2, 4), 8, {"longest": 1, "max_dots": 1, "depth": 0}, "all")
        self.assertEqual(sweep.stats["patterns"], 79)

    def test_every_bar_with_triplets_and_septuplets_nested(self):
        options = {"longest": 1, "max_dots": 0, "depth": 2, "tuplet_ratios": "7:4,3:2", "values": "8,16"}
        sweep = self.run_sweep((1, 4), 16, options, "all", category="NOTES")
        self.assertEqual(sweep.stats["patterns"], 2079)

    def test_sampled_bars_from_huge_spaces(self):
        for meter, max_den, options in (
            ((4, 4), 32, {}),
            ((3, 4), 16, {"max_dots": 2}),
            ((6, 8), 32, {"depth": 1}),
            ((7, 8), 16, {}),
            ((5, 4), 16, {"depth": 2}),
        ):
            self.run_sweep(meter, max_den, options, "sample", 15)


if __name__ == "__main__":
    unittest.main()

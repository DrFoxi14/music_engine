"""Tests for the STEM-first engine (DrFoxi14/stem, STEM_CENTRIC_OMR.md).

Synthetic geometry: staff spacing S = 10 px, noteheads 12 x 9 px, image y
grows downwards. Up-stems sit on the right of the head, down-stems on the left.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from stem_first_client import StemFirstClient, pitch_inputs, read_measure, solver_events  # noqa: E402
from omr_constraint_client import OMRConstraintClient  # noqa: E402
from pitch_context_client import PitchContextClient  # noqa: E402

S = 10


def head(head_id, x, y, fill="filled", confidence=1.0):
    return {"id": head_id, "bbox": {"left": x - 6, "top": y - 4.5, "right": x + 6, "bottom": y + 4.5},
            "fill": fill, "fill_confidence": confidence}


def up_stem(stem_id, x, y, length=35, swap=False, tilt=0.0):
    """Stem on the right of a head at (x, y), rising `length` px."""
    a = (x + 6, y)
    b = (x + 6 + tilt, y - length)
    if swap:
        a, b = b, a
    return {"id": stem_id, "x1": a[0], "y1": a[1], "x2": b[0], "y2": b[1]}


def down_stem(stem_id, x, y, length=35, swap=False, tilt=0.0):
    a = (x - 6, y)
    b = (x - 6 + tilt, y + length)
    if swap:
        a, b = b, a
    return {"id": stem_id, "x1": a[0], "y1": a[1], "x2": b[0], "y2": b[1]}


def top(stem):
    return min((stem["x1"], stem["y1"]), (stem["x2"], stem["y2"]), key=lambda p: p[1])


def bottom(stem):
    return max((stem["x1"], stem["y1"]), (stem["x2"], stem["y2"]), key=lambda p: p[1])


def flag_at(flag_id, point, count=1, upward_stem=True):
    x, y = point
    box = ({"left": x, "top": y, "right": x + 8, "bottom": y + 15} if upward_stem
           else {"left": x, "top": y - 15, "right": x + 8, "bottom": y})
    return {"id": flag_id, "bbox": box, "count": count}


def beams_between(prefix, stems, ends, count, inward):
    """`count` parallel beams joining the given stem ends; inward = +1 (down) or -1 (up)."""
    first, last = ends[0], ends[-1]
    out = []
    for k in range(count):
        dy = inward * k * 0.75 * S
        out.append({"id": f"{prefix}{k}", "x1": first[0], "y1": first[1] + dy,
                    "x2": last[0], "y2": last[1] + dy, "thickness": 4})
    return out


def compile_binary(name):
    compiler = "clang++"
    if subprocess.run(["which", compiler], capture_output=True).returncode != 0:
        compiler = "g++"
    out = Path(StemFirstEngineTests.temp.name) / name
    subprocess.run([compiler, "-std=c++17", "-O2", str(ROOT / f"{name}.cpp"), "-o", str(out)], check=True)
    return out


class StemFirstEngineTests(unittest.TestCase):
    temp: tempfile.TemporaryDirectory

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.stem_binary = compile_binary("stem_first_engine")
        cls.solver_binary = compile_binary("omr_constraint_solver")
        cls.pitch_binary = compile_binary("pitch_context_engine")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.engine = StemFirstClient(self.stem_binary)

    def tearDown(self):
        self.engine.close()

    def analyze(self, **elements):
        return self.engine.analyze(S, **elements)

    @staticmethod
    def durations(result):
        return [[c["duration"] for c in e["candidates"]] for e in result["events"]]

    # -- end logic (section 3 of the document) ----------------------------------------

    def test_flag_marks_free_end_for_both_directions(self):
        up = up_stem("up", 100, 120)
        down = down_stem("down", 200, 100)
        result = self.analyze(
            stems=[up, down],
            noteheads=[head("h_up", 100, 120), head("h_down", 200, 100)],
            flags=[flag_at("f_up", top(up)), flag_at("f_down", bottom(down), upward_stem=False)],
        )
        stems = {s["id"]: s for s in result["stems"]}
        self.assertEqual(stems["up"]["branch"], "flag")
        self.assertEqual(stems["up"]["direction"], "up")
        self.assertEqual(stems["up"]["anchor_notehead"], "h_up")
        self.assertEqual(stems["down"]["direction"], "down")
        self.assertEqual(stems["down"]["anchor_notehead"], "h_down")
        self.assertEqual(self.durations(result), [["1/8"], ["1/8"]])
        self.assertEqual(result["status"], "resolved")

    def test_beam_marks_free_end_for_both_directions(self):
        ups = [up_stem(f"u{i}", 100 + 30 * i, 120) for i in range(2)]
        downs = [down_stem(f"d{i}", 200 + 30 * i, 100) for i in range(2)]
        result = self.analyze(
            stems=ups + downs,
            noteheads=[head(f"hu{i}", 100 + 30 * i, 120) for i in range(2)] +
                      [head(f"hd{i}", 200 + 30 * i, 100) for i in range(2)],
            beams=beams_between("bu", ups, [top(s) for s in ups], 1, +1) +
                  beams_between("bd", downs, [bottom(s) for s in downs], 1, -1),
        )
        for stem in result["stems"]:
            self.assertEqual(stem["branch"], "beam")
            self.assertEqual(stem["direction"], "up" if stem["id"].startswith("u") else "down")
        self.assertEqual(self.durations(result), [["1/8"]] * 4)

    def test_quarter_branch_tests_both_ends(self):
        up = up_stem("up", 100, 120, swap=True)
        down = down_stem("down", 200, 100)
        result = self.analyze(stems=[up, down], noteheads=[head("a", 100, 120), head("b", 200, 100)])
        stems = {s["id"]: s for s in result["stems"]}
        for stem_id, expected in (("up", "up"), ("down", "down")):
            self.assertEqual(stems[stem_id]["branch"], "quarter_branch")
            self.assertEqual(stems[stem_id]["direction"], expected)
        # One hypothesis confirmed, the other rejected.
        self.assertEqual(stems["up"]["hypotheses"], {"A": [], "B": ["a"]})
        self.assertEqual(self.durations(result), [["1/4"], ["1/4"]])

    def test_endpoint_order_never_matters(self):
        for swap in (False, True):
            stem = up_stem("s", 100, 120, swap=swap)
            result = self.analyze(stems=[stem], noteheads=[head("h", 100, 120)], flags=[flag_at("f", top(stem), 2)])
            self.assertEqual(result["stems"][0]["direction"], "up")
            self.assertEqual(self.durations(result), [["1/16"]])

    # -- stem length is never a rule (section 4) --------------------------------------

    def test_stem_length_and_handwritten_tilt_do_not_change_the_reading(self):
        readings = set()
        for length, tilt in ((18, 0), (35, 0), (90, 0), (40, 5), (60, -6)):
            stem = up_stem("s", 100, 120, length=length, tilt=tilt)
            result = self.analyze(stems=[stem], noteheads=[head("h", 100, 120)], flags=[flag_at("f", top(stem))])
            s = result["stems"][0]
            readings.add((s["branch"], s["direction"], s["anchor_notehead"], tuple(self.durations(result)[0])))
        self.assertEqual(readings, {("flag", "up", "h", ("1/8",))})

    # -- beams: count gives the subdivision, never the end (sections 3.3, 9.2) --------

    def test_multiple_beams_give_subdivision_not_ambiguity(self):
        for count, expected in ((1, "1/8"), (2, "1/16"), (3, "1/32"), (4, "1/64")):
            stems = [up_stem(f"s{i}", 100 + 25 * i, 120, length=45) for i in range(4)]
            result = self.analyze(
                stems=stems,
                noteheads=[head(f"h{i}", 100 + 25 * i, 120) for i in range(4)],
                beams=beams_between("b", stems, [top(s) for s in stems], count, +1),
            )
            for stem in result["stems"]:
                self.assertEqual(stem["beams"]["B" if stem["A"][1] > stem["B"][1] else "A"], count)
                self.assertEqual(stem["direction"], "up")
            self.assertEqual(self.durations(result), [[expected]] * 4, count)

    def test_partial_beam_gives_mixed_values_in_one_group(self):
        # eighth + two sixteenths: primary beam over all, secondary over the last two.
        stems = [up_stem(f"s{i}", 100 + 25 * i, 120) for i in range(3)]
        ends = [top(s) for s in stems]
        beams = beams_between("p", stems, ends, 1, +1)
        beams.append({"id": "sec", "x1": ends[1][0], "y1": ends[1][1] + 7.5,
                      "x2": ends[2][0], "y2": ends[2][1] + 7.5, "thickness": 4})
        result = self.analyze(stems=stems, noteheads=[head(f"h{i}", 100 + 25 * i, 120) for i in range(3)], beams=beams)
        self.assertEqual(self.durations(result), [["1/8"], ["1/16"], ["1/16"]])

    def test_beam_crossing_the_middle_of_a_stem_is_not_an_end(self):
        stem = up_stem("s", 100, 120, length=90)
        middle = {"id": "b", "x1": 80, "y1": 75, "x2": 130, "y2": 75, "thickness": 4}
        result = self.analyze(stems=[stem], noteheads=[head("h", 100, 120)], beams=[middle])
        self.assertEqual(result["stems"][0]["branch"], "quarter_branch")
        self.assertIn("beam_crosses_stem_away_from_its_ends",
                      [g["reason"] for g in result["visual_guidance"]])

    # -- notehead role (section 5) ----------------------------------------------------

    def test_head_fill_only_separates_half_and_quarter(self):
        result = self.analyze(
            stems=[up_stem("q", 100, 120), up_stem("h", 150, 120), up_stem("u", 200, 120), up_stem("p", 250, 120)],
            noteheads=[head("hq", 100, 120, "filled"), head("hh", 150, 120, "hollow"),
                       head("hu", 200, 120, "unknown"), head("hp", 250, 120, "filled", 0.7)],
        )
        events = {e["id"]: e for e in result["events"]}
        self.assertEqual([c["duration"] for c in events["q"]["candidates"]], ["1/4"])
        self.assertEqual([c["duration"] for c in events["h"]["candidates"]], ["1/2"])
        self.assertEqual(sorted(c["duration"] for c in events["u"]["candidates"]), ["1/2", "1/4"])
        self.assertEqual([(c["duration"], round(c["confidence"], 3)) for c in events["p"]["candidates"]],
                         [("1/4", 0.7), ("1/2", 0.3)])

    def test_chord_on_one_stem_is_one_event_anchored_at_the_notehead_end(self):
        stem = {"id": "s", "x1": 106, "y1": 130, "x2": 106, "y2": 70}
        result = self.analyze(
            stems=[stem],
            noteheads=[head("low", 100, 130), head("mid", 100, 120), head("high", 100, 110)],
            flags=[flag_at("f", top(stem))],
        )
        event = result["events"][0]
        self.assertEqual(event["kind"], "chord")
        self.assertEqual(sorted(event["noteheads"]), ["high", "low", "mid"])
        self.assertEqual(result["stems"][0]["anchor_notehead"], "low")
        self.assertEqual(self.durations(result), [["1/8"]])

    def test_dots_extend_duration(self):
        result = self.analyze(
            stems=[up_stem("a", 100, 120), up_stem("b", 200, 120), up_stem("c", 300, 120)],
            noteheads=[head("ha", 100, 120), head("hb", 200, 120, "hollow"), head("hc", 300, 120)],
            dots=[{"id": "d1", "x": 113, "y": 118},
                  {"id": "d2", "x": 213, "y": 118}, {"id": "d3", "x": 220, "y": 118},
                  {"id": "d4", "x": 313, "y": 118, "confidence": 0.6}],
        )
        events = {e["id"]: e for e in result["events"]}
        self.assertEqual(events["a"]["dots"], 1)
        self.assertEqual([c["duration"] for c in events["a"]["candidates"]], ["3/8"])
        self.assertEqual([c["duration"] for c in events["b"]["candidates"]], ["7/8"])
        self.assertEqual([(c["duration"], round(c["confidence"], 3)) for c in events["c"]["candidates"]],
                         [("3/8", 0.6), ("1/4", 0.4)])

    # -- tuplets, including nesting ---------------------------------------------------

    def test_triplet_inside_septuplet(self):
        xs = [100 + 20 * i for i in range(8)]
        stems = [up_stem(f"s{i}", x, 120) for i, x in enumerate(xs)]
        result = self.analyze(
            stems=stems,
            noteheads=[head(f"h{i}", x, 120) for i, x in enumerate(xs)],
            beams=beams_between("b", stems, [top(s) for s in stems], 2, +1),
            tuplets=[{"id": "t7", "number": 7, "members": [s["id"] for s in stems]},
                     {"id": "t3", "number": 3, "members": ["s2", "s3", "s4"]}],
        )
        self.assertEqual(self.durations(result),
                         [["1/28"], ["1/28"], ["1/42"], ["1/42"], ["1/42"], ["1/28"], ["1/28"], ["1/28"]])
        self.assertEqual(sorted(result["events"][3]["tuplets"]), ["t3", "t7"])
        self.assertEqual(result["events"][0]["tuplets"], ["t7"])

    def test_inferred_duplet_normal_and_explicit_ratio(self):
        stems = [up_stem(f"s{i}", 100 + 25 * i, 120) for i in range(2)]
        result = self.analyze(
            stems=stems, noteheads=[head(f"h{i}", 100 + 25 * i, 120) for i in range(2)],
            beams=beams_between("b", stems, [top(s) for s in stems], 1, +1),
            tuplets=[{"id": "t", "number": 2, "members": ["s0", "s1"]}],
        )
        self.assertEqual(self.durations(result), [["3/16"], ["3/16"]])
        result = self.analyze(
            stems=stems, noteheads=[head(f"h{i}", 100 + 25 * i, 120) for i in range(2)],
            beams=beams_between("b", stems, [top(s) for s in stems], 1, +1),
            tuplets=[{"id": "t", "number": 5, "normal": 6, "members": ["s0", "s1"]}],
        )
        self.assertEqual(self.durations(result), [["3/20"], ["3/20"]])

    # -- rests (separate module), stemless notes, conflicts ---------------------------

    def test_rests_are_placed_on_the_timeline_without_stems(self):
        result = self.analyze(
            meter="3/4",
            stems=[up_stem("a", 100, 120), up_stem("b", 200, 120)],
            noteheads=[head("ha", 100, 120), head("hb", 200, 120)],
            rests=[{"id": "r", "bbox": {"left": 145, "top": 105, "right": 155, "bottom": 130}, "value": 4, "dots": 0}],
        )
        self.assertEqual([e["id"] for e in result["events"]], ["a", "r", "b"])
        self.assertEqual(result["events"][1]["kind"], "rest")
        measure = self.analyze(meter="3/4", rests=[{"id": "m", "bbox": {"left": 140, "top": 100, "right": 160, "bottom": 105},
                                                    "value": "measure"}])
        self.assertEqual(self.durations(measure), [["3/4"]])

    def test_stemless_heads(self):
        result = self.analyze(noteheads=[head("w1", 100, 120, "hollow"), head("w2", 100, 110, "hollow"),
                                         head("lost", 200, 120, "filled")])
        events = {e["id"]: e for e in result["events"]}
        self.assertEqual(events["w1"]["kind"], "chord")
        self.assertEqual([c["duration"] for c in events["w1"]["candidates"]], ["1/1"])
        self.assertFalse(events["lost"]["confirmed"])
        self.assertIn("filled_notehead_without_stem", [g["reason"] for g in result["visual_guidance"]])

    def test_conflicts_are_reported_not_guessed(self):
        both = up_stem("both", 100, 120)
        result = self.analyze(
            stems=[both, up_stem("two_heads", 200, 120), up_stem("barline", 300, 140)],
            noteheads=[head("h1", 100, 120), head("h2", 200, 120), head("h3", 206 + 6, 85)],
            flags=[flag_at("f1", top(both)), flag_at("f2", bottom(both), upward_stem=False)],
        )
        stems = {s["id"]: s for s in result["stems"]}
        self.assertEqual(stems["both"]["status"], "flag_or_beam_at_both_ends")
        self.assertIsNone(stems["both"]["free_end"])
        self.assertEqual(stems["two_heads"]["status"], "noteheads_at_both_ends")
        self.assertEqual(stems["barline"]["status"], "no_notehead_at_either_end")
        self.assertEqual(result["events"], [])
        self.assertEqual(result["status"], "review_required")

    # -- pipeline: STEM-first -> solver and pitch engine ------------------------------

    def test_measure_constraint_resolves_unknown_fill(self):
        stems = [up_stem("a", 100, 120), up_stem("b", 150, 120), up_stem("c", 200, 120)]
        heads = [head("ha", 100, 120), head("hb", 150, 120), head("hc", 200, 120, "unknown")]
        with OMRConstraintClient(self.solver_binary) as solver:
            result = read_measure(self.engine, solver, meter="4/4", spacing=S, stems=stems, noteheads=heads)
        self.assertEqual(result["rhythm"]["status"], "resolved_unique")
        self.assertEqual([e["duration"] for e in result["events"]], ["1/4", "1/4", "1/2"])
        self.assertEqual(result["events"][2]["eliminated_durations"], ["1/4"])

    def test_nested_tuplet_measure_resolves_exactly(self):
        xs = [100 + 20 * i for i in range(8)]
        stems = [up_stem(f"s{i}", x, 120) for i, x in enumerate(xs)] + [up_stem("q", 300, 120)]
        heads = [head(f"h{i}", x, 120) for i, x in enumerate(xs)] + [head("hq", 300, 120)]
        with OMRConstraintClient(self.solver_binary) as solver:
            result = read_measure(
                self.engine, solver, meter="2/4", spacing=S, stems=stems, noteheads=heads,
                beams=beams_between("b", stems[:8], [top(s) for s in stems[:8]], 2, +1),
                tuplets=[{"id": "t7", "number": 7, "members": [f"s{i}" for i in range(8)]},
                         {"id": "t3", "number": 3, "members": ["s2", "s3", "s4"]}],
            )
        self.assertEqual(result["rhythm"]["status"], "resolved_unique")
        self.assertEqual(result["events"][-1]["duration"], "1/4")

    def test_stem_directions_feed_pitch_engine(self):
        stem = {"id": "s", "x1": 106, "y1": 140, "x2": 106, "y2": 95}
        heads = [head("e4", 100, 140), head("g4", 100, 130)]
        analysis = self.analyze(stems=[stem], noteheads=heads)
        notes, stems = pitch_inputs(analysis, heads, [stem])
        self.assertEqual(stems[0]["direction"], "up")
        with PitchContextClient(self.pitch_binary) as pitch:
            result = pitch.resolve(clef="treble", staff_top_y=100, line_spacing=10, notes=notes, stems=stems)
        letters = {n["id"]: (n["letter"], n["octave"]) for n in result["notes"]}
        self.assertEqual(letters, {"e4": ("E", 4), "g4": ("G", 4)})
        self.assertEqual(result["stem_checks"][0]["status"], "attached")
        self.assertEqual(result["stem_checks"][0]["anchor_note_id"], "e4")

    def test_solver_events_preserve_order_and_candidates(self):
        result = self.analyze(stems=[up_stem("b", 200, 120), up_stem("a", 100, 120)],
                              noteheads=[head("hb", 200, 120, "hollow"), head("ha", 100, 120, "unknown")])
        events = solver_events(result)
        self.assertEqual([e["id"] for e in events], ["a", "b"])
        self.assertEqual(len(events[0]["candidates"]), 2)

    def test_invalid_input_is_rejected(self):
        with self.assertRaises(ValueError):
            self.engine.analyze(0, stems=[up_stem("s", 100, 120)])
        with self.assertRaises(ValueError):
            self.analyze(noteheads=[{**head("h", 1, 1), "fill": "striped"}])
        with self.assertRaises(ValueError):
            self.analyze(rests=[{"id": "m", "bbox": {"left": 1, "top": 1, "right": 2, "bottom": 2}, "value": "measure"}])


if __name__ == "__main__":
    unittest.main()

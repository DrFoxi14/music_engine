"""Acceptance tests of the Vision specification (section 8) on the synthetic Vision, and of the dialogue.

The engines are compiled into a temporary directory. Measures are written by hand, so every
expected observation and every expected question is known.
"""
from __future__ import annotations

import json
import random
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "omr_dialogue"))


def compile_all(out: Path) -> None:
    compiler = "clang++" if subprocess.run(["which", "clang++"], capture_output=True).returncode == 0 else "g++"
    for name in ("math_engine", "search_engine", "omr_constraint_solver", "pitch_context_engine"):
        subprocess.run([compiler, "-std=c++17", "-O2", str(ROOT / f"{name}.cpp"), "-o", str(out / name)], check=True)


def measure(meter, symbols, key="C"):
    from synth import Measure, _pitches
    m = Measure(meter, 16, key, symbols)
    _pitches(m, random.Random(3))
    assert sum((s.duration for s in m.symbols), Fraction(0)) == m.capacity
    return m


def vision(m, edit=None, seed=0):
    from vision_sim import VisionSim
    v = VisionSim(m, random.Random(seed), scale=0.0)
    first = v.observe()
    if edit:
        edit(first["measures"][0], v)
    v.observe = lambda: json.loads(json.dumps(first))
    return v


class VisionSpecTests(unittest.TestCase):
    def test_observation_has_geometry_ids_and_no_durations(self) -> None:
        from synth import Symbol
        m = measure((2, 4), [Symbol(False, 8, dots=1), Symbol(False, 16), Symbol(False, 4)])
        obs = vision(m).observe()
        ms = obs["measures"][0]
        self.assertEqual(obs["status"], "ok")
        ids = [s["id"] for s in ms["symbols"]]
        self.assertEqual(len(ids), len(set(ids)))
        for s in ms["symbols"]:
            self.assertEqual(len(s["bbox"]), 4)
            self.assertIn("x", s["center"])
            self.assertIn("order", s)
            self.assertIn(s["head"]["fill"], ("filled", "hollow", "unknown"))
            self.assertIn("confidence", s["head"])
            self.assertIn(s["stem"]["state"], ("present", "absent", "unknown"))
            self.assertIn("confidence", s["dots"])
        self.assertNotIn("duration", json.dumps(obs))           # Vision never assigns a duration
        self.assertEqual(len(ms["staff"]["lines_y"]), 5)

    def test_beams_flags_partial_beams_and_tuplets_are_separate(self) -> None:
        from synth import Symbol
        m = measure((2, 4), [Symbol(False, 8, dots=1), Symbol(False, 16)] +
                    [Symbol(False, 8, tuplet=(1, i, 3, 2)) for i in range(3)])
        ms = vision(m).observe()["measures"][0]
        b = ms["beam_groups"][0]
        self.assertEqual(b["symbol_ids"], ["n1", "n2"])
        self.assertEqual([(s["beam_count"], s["partial"]) for s in b["stems"]], [(1, False), (2, True)])
        n1 = next(s for s in ms["symbols"] if s["id"] == "n1")
        self.assertEqual((n1["flags"]["count"], n1["beam_group_ids"], n1["dots"]["count"]), (0, ["b1"], 1))
        self.assertEqual(ms["tuplets"][0]["symbol_ids"], ["n3", "n4", "n5"])
        self.assertEqual(ms["tuplets"][0]["number"], 3)

    def test_unknown_stays_unknown_in_the_adapter(self) -> None:
        import adapter as A
        from synth import Symbol
        m = measure((2, 4), [Symbol(False, 4), Symbol(False, 4)])

        def blur(ms, v):
            ms["symbols"][0]["dots"]["count"] = "unknown"
            ms["symbols"][0]["dots"]["confidence"] = 0.4
        syms = A.parse(vision(m, blur).observe()["measures"][0])
        self.assertEqual(sorted(syms[0].readings()), ["dotted_note@4", "note@4"])
        self.assertEqual(sorted(syms[1].readings()), ["note@4"])

    def test_relook_by_id_returns_only_the_requested_properties(self) -> None:
        from synth import Symbol
        m = measure((2, 4), [Symbol(False, 4), Symbol(True, 4)])
        rep = vision(m).relook({"request_id": "q1", "measure_id": "m1", "target_symbol_ids": ["n1"],
                                "requested_properties": ["head.fill", "dots.count"]})
        props = rep["results"][0]["properties"]
        self.assertEqual(sorted(props), ["dots.count", "head.fill"])
        self.assertEqual(props["head.fill"]["value"], "filled")
        self.assertIn("evidence", props["head.fill"])
        self.assertNotIn("duration", json.dumps(rep))


class DialogueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        compile_all(Path(cls.temp.name))
        from engines import Engines
        cls.E = Engines(Path(cls.temp.name))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.E.close()
        cls.temp.cleanup()

    def talk(self, m, edit=None, seed=0):
        from dialogue import Dialogue
        v = vision(m, edit, seed)
        d = Dialogue(self.E)
        return d, v, d.run(m.meter, v)

    def test_perfect_reading_is_resolved_without_questions(self) -> None:
        from synth import Symbol
        m = measure((2, 4), [Symbol(False, 8, dots=1), Symbol(False, 16), Symbol(False, 4)])
        d, v, r = self.talk(m)
        self.assertEqual(r["status"], "resolved")
        self.assertEqual(r["variants"][0]["events"], ["dotted_note@8", "note@16", "note@4"])
        self.assertEqual(d.log[0]["type"], "observation")

    def test_variants_are_separated_by_asking_vision(self) -> None:
        from synth import Symbol
        m = measure((2, 4), [Symbol(False, 8, dots=1), Symbol(False, 16), Symbol(False, 4)])

        def blur(ms, v):                     # the dot and the partial beam are unreadable
            n1 = ms["symbols"][0]
            n1["dots"] = {"count": "unknown", "confidence": 0.3}
            ms["beam_groups"][0]["stems"][1]["beam_count"] = "unknown"
            ms["beam_groups"][0]["stems"][1]["confidence"] = 0.3
        d, v, r = self.talk(m, blur)
        first = next(x for x in d.log if x["type"] == "interpretations")
        self.assertGreater(first["count"], 1)                  # 8.+16 and 8+8 both fill the measure
        asks = [x["request"] for x in d.log if x["type"] == "relook"]
        self.assertTrue(asks)
        self.assertTrue(set(asks[0]["requested_properties"]) & {"dots.count", "beam_groups.beam_count"})
        self.assertEqual(r["status"], "resolved")
        self.assertEqual(r["variants"][0]["events"], ["dotted_note@8", "note@16", "note@4"])

    def test_missed_rest_is_found_by_re_examining_the_region(self) -> None:
        from synth import Symbol
        m = measure((3, 4), [Symbol(False, 4), Symbol(True, 4), Symbol(False, 4)])

        def drop(ms, v):
            ms["symbols"] = [s for s in ms["symbols"] if s["type"] != "rest"]
            v.missed.add("r2")
        d, v, r = self.talk(m, drop)
        regions = [x for x in d.log if x["type"] == "examine_region"]
        self.assertTrue(regions)
        self.assertEqual(r["status"], "resolved")
        self.assertEqual(r["variants"][0]["events"], ["note@4", "rest@4", "note@4"])

    def test_impossible_measure_is_reported_not_forced(self) -> None:
        from synth import Symbol
        m = measure((3, 4), [Symbol(False, 4), Symbol(False, 4), Symbol(False, 4)])

        def drop(ms, v):                     # a notehead the vision cannot find again
            ms["symbols"] = ms["symbols"][:2]
            v.missed.add("n3")
            v.rates["symbol_miss"] = 1.0
            import vision_sim
            vision_sim.FIND = 0.0
        try:
            d, v, r = self.talk(m, drop)
        finally:
            import vision_sim
            vision_sim.FIND = 0.85
        self.assertEqual(r["status"], "contradiction")
        self.assertEqual(d.log[-1]["type"], "contradiction_report")

    def test_pitches_after_the_rhythm(self) -> None:
        from synth import Symbol
        m = measure((3, 4), [Symbol(False, 4), Symbol(False, 4), Symbol(False, 4)], key="E")
        for s, (step, alter, acc) in zip(m.symbols, [(2, -1, "flat"), (1, 1, None), (2, -1, None)]):
            s.step, s.alter, s.accidental = step, alter, acc
        d, v, r = self.talk(m)
        p = d.pitches("E", r, v)
        self.assertEqual([p[i] for i in ("n1", "n2", "n3")], [("G", 4, -1), ("F", 4, 1), ("G", 4, -1)])


if __name__ == "__main__":
    unittest.main()

"""The vision <-> music-engine dialogue (training/): every message type, on hand-made measures.

The engines are compiled into a temporary directory, as in the other tests; the vision is the
simulator from training/vision_sim.py with all error rates at zero, and each test injects exactly
one mistake, so the dialogue that must follow is known.
"""
from __future__ import annotations

import os
import random
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "training"))


def compile_all(out: Path) -> None:
    compiler = "clang++" if subprocess.run(["which", "clang++"], capture_output=True).returncode == 0 else "g++"
    for name in ("ritmuri_engine", "rhythm_search_engine", "omr_constraint_solver", "pitch_context_engine"):
        subprocess.run([compiler, "-std=c++17", "-O2", str(ROOT / f"{name}.cpp"), "-o", str(out / name)], check=True)


class TrainingDialogueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        compile_all(Path(cls.temp.name))
        os.environ["MUSIC_ENGINE_BUILD"] = cls.temp.name
        from engines import Engines
        cls.E = Engines(Path(cls.temp.name))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.E.close()
        cls.temp.cleanup()

    def measure(self, meter, symbols, key="C"):
        from synth import Measure, _pitches
        m = Measure(meter, 16, key, symbols)
        _pitches(m, random.Random(3))
        self.assertEqual(sum((s.duration for s in m.symbols), Fraction(0)), m.capacity)
        return m

    def talk(self, m, edit=None, rng=0):
        from dialogue import Calibration, Dialogue
        from run import info
        from vision_sim import VisionSim

        class Vision(VisionSim):
            def observe(self):
                obs = super().observe()
                return edit(obs, self) if edit else obs
        v = Vision(m, random.Random(rng), scale=0.0)
        d = Dialogue(self.E, Calibration())
        obs, status = d.run(info(m), v)
        return d, v, obs, status

    def durations(self, obs):
        from dialogue import duration
        return [duration(o) for o in obs]

    def verdicts(self, d):
        return [x for x in d.log if x["type"] == "verdict"]

    def types(self, d):
        return [(x["from"], x["to"], x["type"]) for x in d.log]

    def test_perfect_vision_is_accepted_at_once(self) -> None:
        from synth import Symbol
        m = self.measure((2, 4), [Symbol(False, 8, dots=1), Symbol(False, 16), Symbol(False, 4)])
        d, v, obs, status = self.talk(m)
        self.assertEqual(status, "valid")
        self.assertEqual((v.relooks, v.searches), (0, 0))
        self.assertEqual(self.types(d)[0], ("vision", "rhythm", "observation"))
        self.assertTrue(self.verdicts(d)[0]["valid"])

    def test_missed_dot_is_found_by_a_relook(self) -> None:
        from synth import Symbol
        m = self.measure((2, 4), [Symbol(False, 8, dots=1), Symbol(False, 16), Symbol(False, 4)])

        def drop_dot(obs, vision):
            obs[0]["features"]["dots"] = (0, 0.55)       # the dot was not seen, without much certainty
            return obs
        d, v, obs, status = self.talk(m, drop_dot)
        self.assertFalse(self.verdicts(d)[0]["valid"])
        relooks = [x for x in d.log if x["type"] == "relook"]
        self.assertTrue(relooks and [obs[0]["sid"], "dots"] in relooks[0]["features"])
        self.assertEqual(status, "valid")
        self.assertEqual(self.durations(obs), [Fraction(3, 16), Fraction(1, 16), Fraction(1, 4)])

    def test_missed_rest_is_searched_where_the_spacing_leaves_room(self) -> None:
        from synth import Symbol
        m = self.measure((3, 4), [Symbol(False, 4), Symbol(True, 4), Symbol(False, 4)])

        def drop_rest(obs, vision):
            for o in obs:
                if o["kind"] == "rest":
                    vision.forget(o["sid"])
            return [o for o in obs if o["kind"] != "rest"]
        for seed in range(6):                             # the simulated search may miss (FIND < 1)
            d, v, obs, status = self.talk(m, drop_rest, rng=seed)
            if [x for x in d.log if x["type"] == "search_result" and x["found"]]:
                break
        searches = [x for x in d.log if x["type"] == "search"]
        self.assertTrue(searches)
        self.assertIn("rest@4", searches[0]["would_fit"])     # rhythm_search_engine says what fits there
        self.assertEqual(status, "valid")
        self.assertEqual([o["kind"] for o in obs], ["head", "rest", "head"])

    def test_triplet_needs_three_slots(self) -> None:
        from dialogue import tokens
        from synth import Symbol
        m = self.measure((2, 4), [Symbol(False, 8, tuplet=(1, i, 3, 2)) for i in range(3)] + [Symbol(False, 4)])
        d, v, obs, status = self.talk(m)
        self.assertEqual(status, "valid")
        self.assertEqual([t["desc"] for t in tokens(obs)], ["triplet@8", "note@4"])

    def test_pitches_follow_accidental_memory(self) -> None:
        from synth import Symbol
        m = self.measure((3, 4), [Symbol(False, 4), Symbol(False, 4), Symbol(False, 4)], key="E")
        for s, (step, alter, acc) in zip(m.symbols, [(2, -1, "flat"), (1, 1, None), (2, -1, None)]):
            s.step, s.alter, s.accidental = step, alter, acc       # G-flat, F-sharp (key), G-flat (memory)
        d, v, obs, status = self.talk(m)
        p = d.pitches({"key": "E"}, obs, vision=v)
        self.assertEqual([p[o["sid"]] for o in obs], [("G", 4, -1), ("F", 4, 1), ("G", 4, -1)])
        self.assertEqual(self.types(d)[-2:], [("rhythm", "pitch", "resolve"), ("pitch", "output", "pitches")])


if __name__ == "__main__":
    unittest.main()

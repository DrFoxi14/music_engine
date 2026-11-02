"""A deliberately imperfect vision engine.

It sees the printed symbols of a synthetic measure the way a real OMR model does: a notehead with a
fill, a stem, some beams or flags, dots, a triplet "3", a staff position and an accidental; a rest
with its shape. Every feature can be wrong, with rates you choose (`RATES`), and each comes with a
confidence that is only loosely related to being right (wrong readings are not always unsure).
Whole symbols can be missed and spurious ones invented.

Two requests let the music engines push back, as on a real page:
  relook(sid, feature)   look again at one feature of one symbol (a closer, costlier look: errors
                         become RELOOK times rarer); feature "exists" asks if the symbol is real
  search(x0, x1)         look for a symbol missed between two x positions

The truth stays inside this class; the dialogue only sees observations. `truth_of` is for scoring.
"""
from __future__ import annotations

import random

from synth import Measure, Symbol

RATES = {
    "beams": 0.12,        # beam / flag count off by one (mostly a missed short beam)
    "dot_miss": 0.15,     # augmentation dot not seen
    "dot_extra": 0.03,    # a speck read as a dot
    "fill": 0.06,         # filled <-> hollow head
    "stem": 0.03,         # stem not seen (or seen on a whole note)
    "rest_den": 0.08,     # rest shape read one value off
    "rest_miss": 0.08,    # rest not seen at all
    "head_miss": 0.03,    # notehead not seen at all
    "extra": 0.03,        # spurious notehead per symbol
    "tuplet": 0.10,       # triplet "3" not seen
    "step": 0.05,         # notehead one staff step off
    "acc_miss": 0.10,     # printed accidental not seen
}
RELOOK = 0.2              # a targeted second look makes each error this much rarer
FIND = 0.85               # chance that a targeted search finds a missed symbol
FALSE_FIND = 0.02         # chance that a search invents one


class VisionSim:
    def __init__(self, measure: Measure, rng: random.Random, rates: dict | None = None, scale: float = 1.0):
        self.m, self.rng = measure, rng
        self.rates = {k: v * scale for k, v in (rates or RATES).items()}
        self.false_find = FALSE_FIND * scale
        self.relooks = self.searches = 0
        self._truth: dict[str, int | None] = {}
        self._seen: set[int] = set()
        self._n = 0

    # ---- what a confidence looks like ----------------------------------------------------------
    def _conf(self, right: bool) -> float:
        if right:
            return round(1.0 - self.rng.betavariate(1.2, 10.0), 3)
        return round(self.rng.uniform(0.3, 0.95), 3)

    def _feat(self, true, wrong, rate: float, factor: float = 1.0):
        if self.rng.random() < rate * factor:
            return wrong, self._conf(False)
        return true, self._conf(True)

    def _read(self, s: Symbol, factor: float = 1.0) -> dict:
        r = self.rates
        f = {}
        if s.rest:
            wrong_den = s.den * 2 if s.den == 1 or (s.den < 64 and self.rng.random() < 0.5) else s.den // 2
            f["rest_den"] = self._feat(s.den, wrong_den, r["rest_den"], factor)
        else:
            f["fill"] = self._feat(s.den >= 4, s.den < 4, r["fill"], factor)
            f["stem"] = self._feat(s.den >= 2, s.den < 2, r["stem"], factor)
            wrong_beams = max(0, s.beams + (-1 if self.rng.random() < 0.75 else 1)) if s.beams else 1
            f["beams"] = self._feat(s.beams, wrong_beams, r["beams"], factor)
            f["step"] = self._feat(s.step, s.step + self.rng.choice([-1, 1]), r["step"], factor)
            f["accidental"] = (self._feat(s.accidental, None, r["acc_miss"], factor) if s.accidental
                               else (None, self._conf(True)))
        if s.dots:
            f["dots"] = self._feat(1, 0, r["dot_miss"], factor)
        else:
            f["dots"] = self._feat(0, 1, r["dot_extra"], factor)
        if s.tuplet:
            f["tuplet"] = self._feat(True, False, r["tuplet"], factor)
        else:
            f["tuplet"] = (False, self._conf(True))
        return f

    def _spurious(self, x: float) -> dict:
        s = Symbol(rest=False, den=self.rng.choice([4, 8, 16]), step=self.rng.randint(0, 8))
        f = self._read(s)
        f["exists"] = (True, self._conf(False))
        return {"kind": "head", "x": x, "features": f}

    def _new(self, obs: dict, truth: int | None) -> dict:
        self._n += 1
        sid = f"s{self._n}"
        self._truth[sid] = truth
        obs["sid"] = sid
        obs.setdefault("features", {}).setdefault("exists", (True, self._conf(truth is not None)))
        return obs

    # ---- the three calls the dialogue may make ----------------------------------------------------
    def observe(self) -> list[dict]:
        out = []
        for i, s in enumerate(self.m.symbols):
            if self.rng.random() < self.rates["rest_miss" if s.rest else "head_miss"]:
                continue
            self._seen.add(i)
            out.append(self._new({"kind": "rest" if s.rest else "head", "x": s.x, "features": self._read(s)}, i))
            if self.rng.random() < self.rates["extra"]:
                out.append(self._new(self._spurious(s.x + 6.0), None))
        return out

    def relook(self, sid: str, feature: str):
        self.relooks += 1
        t = self._truth[sid]
        if feature == "exists":
            real = t is not None
            return self._feat(real, not real, 0.05 if real else 0.15)
        if t is None:                       # a spurious head: a second look reads noise again
            return self._read(Symbol(rest=False, den=self.rng.choice([4, 8, 16])), RELOOK).get(feature, (None, 0.5))
        return self._read(self.m.symbols[t], RELOOK)[feature]

    def search(self, x0: float, x1: float) -> dict | None:
        self.searches += 1
        missed = [i for i, s in enumerate(self.m.symbols) if i not in self._seen and x0 < s.x < x1]
        if missed and self.rng.random() < FIND:
            i = missed[0]
            self._seen.add(i)
            s = self.m.symbols[i]
            return self._new({"kind": "rest" if s.rest else "head", "x": s.x, "features": self._read(s, RELOOK)}, i)
        if self.rng.random() < self.false_find:
            return self._new(self._spurious((x0 + x1) / 2), None)
        return None

    def forget(self, sid: str) -> None:
        """Make an observed symbol unseen (tests inject misses after observe)."""
        self._seen.discard(self._truth.get(sid))

    def truth_of(self, sid: str) -> int | None:
        return self._truth.get(sid)

    def true_features(self, sid: str) -> dict:
        """What each feature should have read (for calibration on training measures only)."""
        t = self._truth.get(sid)
        if t is None:
            return {"exists": False}
        s = self.m.symbols[t]
        f = {"exists": True, "dots": s.dots, "tuplet": bool(s.tuplet)}
        if s.rest:
            f["rest_den"] = s.den
        else:
            f.update(fill=s.den >= 4, stem=s.den >= 2, beams=s.beams, step=s.step, accidental=s.accidental)
        return f

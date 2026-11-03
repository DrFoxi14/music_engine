"""A synthetic Vision engine that follows the Vision specification.

It reports what is *in the image* of a synthetic measure (synth.py), never a duration:
noteheads (fill), stems (state, direction), flags (count), beam groups (members, lines at each
stem, partial beams), dots, rests (visual shape), tuplet marks, accidentals, staff geometry.
Every property has its own confidence. A property it cannot read is reported as "unknown"
(unknown is not absent). It never invents a symbol and never removes an uncertain one.

Errors are deliberate (`RATES`): a property is misread with some probability; most misreads
are reported as "unknown", the rest as a wrong value with a confidence that is not always low.
Whole symbols, beam groups and tuplet marks can be missed.

Requests it answers (the dialogue's only way to get more information):
  relook({"request_id", "measure_id", "target_symbol_ids", "requested_properties"})
      re-examines the region of each target with context; returns only the requested properties,
      each with value, confidence and geometric evidence
  examine_region({"request_id", "measure_id", "region": {"x0", "x1"}})
      looks again between two x positions for symbols it missed; returns them (or nothing)
"""
from __future__ import annotations

import random
from fractions import Fraction

from synth import SPACING, STAFF_TOP, Measure

REST_SHAPES = {1: "whole", 2: "half", 4: "quarter", 8: "eighth", 16: "sixteenth", 32: "thirty_second",
               64: "sixty_fourth"}
RATES = {
    "head.fill": 0.06, "stem.state": 0.03, "stem.direction": 0.03, "flags.count": 0.10,
    "beam_groups.beam_count": 0.12, "partial_beam": 0.25,   # a short partial beam is misread more often
    "dots.count": 0.12, "rest.shape": 0.08, "accidental.kind": 0.06, "tuplet.number": 0.08,
    "symbol_miss": 0.03, "beam_group_miss": 0.03, "tuplet_miss": 0.05,
}
UNKNOWN_WHEN_WRONG = 0.6   # an honest vision says "unknown" for most of what it cannot read
UNKNOWN_WHEN_RIGHT = 0.02
RELOOK = 0.2               # a targeted re-examination makes errors this much rarer
FIND = 0.85                # a region re-examination finds a missed symbol with this probability
PROPERTIES_NOTE = ("head.fill", "stem.state", "stem.direction", "flags.count", "beam_groups.beam_count",
                   "dots.count", "tuplet.member", "tuplet.number", "accidental.kind")
PROPERTIES_REST = ("rest.shape", "dots.count", "tuplet.member", "tuplet.number")


def y_of(step: int) -> float:
    return STAFF_TOP + 4 * SPACING - step * SPACING / 2


def _beat(meter) -> Fraction:
    num, den = meter
    if den == 8 and num % 3 == 0 and num > 3:
        return Fraction(3, 8)
    if den == 8:
        return Fraction(num, 8)
    return Fraction(1, 4)


class Graphics:
    """The true printed graphics of a synthetic measure (what a perfect vision would report)."""

    def __init__(self, m: Measure, mid: str = "m1"):
        self.m, self.mid = m, mid
        self.symbols, self.beam_groups, self.tuplets, self.accidentals = [], [], [], []
        onset = Fraction(0)
        for i, s in enumerate(m.symbols):
            sid = f"{'r' if s.rest else 'n'}{i + 1}"
            y = y_of(4) if s.rest else y_of(s.step)
            g = {"id": sid, "type": "rest" if s.rest else "note", "index": i, "x": s.x, "y": y, "onset": onset,
                 "props": {}}
            if s.rest:
                g["props"] = {"rest.shape": REST_SHAPES[s.den], "dots.count": s.dots}
            else:
                g["props"] = {"head.fill": "filled" if s.den >= 4 else "hollow",
                              "stem.state": "present" if s.den >= 2 else "absent",
                              "stem.direction": "up" if s.step < 4 else "down",
                              "flags.count": 0, "beam_groups.beam_count": None, "dots.count": s.dots,
                              "accidental.kind": s.accidental}
                if s.accidental:
                    self.accidentals.append({"id": f"a{i + 1}", "note": sid, "kind": s.accidental, "x": s.x - 7, "y": y})
            g["props"]["tuplet.member"] = s.tuplet is not None
            self.symbols.append(g)
            onset += s.duration
        # beams: consecutive notes of an eighth or shorter inside one beat (a rest breaks the beam)
        beat, run = _beat(m.meter), []

        def close(run):
            if len(run) >= 2:
                bid = f"b{len(self.beam_groups) + 1}"
                counts = [self.m.symbols[g["index"]].beams for g in run]
                stems = []
                for k, g in enumerate(run):
                    nb = [counts[j] for j in (k - 1, k + 1) if 0 <= j < len(run)]
                    stems.append({"symbol_id": g["id"], "beam_count": counts[k], "partial": counts[k] > max(nb)})
                    g["props"]["beam_groups.beam_count"] = counts[k]
                    g["beam"] = bid
                self.beam_groups.append({"id": bid, "symbol_ids": [g["id"] for g in run], "stems": stems})
            else:
                for g in run:
                    g["props"]["flags.count"] = self.m.symbols[g["index"]].beams
        for g in self.symbols:
            s = m.symbols[g["index"]]
            if g["type"] == "note" and s.den >= 8 and (not run or (g["onset"] // beat == run[0]["onset"] // beat)):
                run.append(g)
                continue
            close(run)
            run = [g] if g["type"] == "note" and s.den >= 8 else []
        close(run)
        groups = {}
        for g in self.symbols:
            t = m.symbols[g["index"]].tuplet
            if t:
                groups.setdefault(t[0], []).append(g["id"])
        for k, ids in groups.items():
            self.tuplets.append({"id": f"t{k}", "number": 3, "symbol_ids": ids})


class VisionSim:
    def __init__(self, m: Measure, rng: random.Random, scale: float = 1.0, rates: dict | None = None):
        self.g = Graphics(m)
        self.rng = rng
        self.rates = {k: v * scale for k, v in (rates or RATES).items()}
        self.perfect = scale == 0
        self.requests = self.relooked = self.regions = 0
        self.missed = {g["id"] for g in self.g.symbols if self.rng.random() < self.rates["symbol_miss"]}
        self.missed_beams = {b["id"] for b in self.g.beam_groups if self.rng.random() < self.rates["beam_group_miss"]}
        self.missed_tuplets = {t["id"] for t in self.g.tuplets if self.rng.random() < self.rates["tuplet_miss"]}

    # ---- reading one property ----------------------------------------------------------------------
    def _read(self, true, domain, rate):
        if self.perfect:
            return {"value": true, "confidence": 0.99}
        if self.rng.random() < rate:
            if self.rng.random() < UNKNOWN_WHEN_WRONG or len(domain) < 2:
                return {"value": "unknown", "confidence": round(self.rng.uniform(0.2, 0.6), 3)}
            wrong = self.rng.choice([d for d in domain if d != true])
            return {"value": wrong, "confidence": round(self.rng.uniform(0.5, 0.95), 3)}
        if self.rng.random() < UNKNOWN_WHEN_RIGHT:
            return {"value": "unknown", "confidence": round(self.rng.uniform(0.3, 0.6), 3)}
        return {"value": true, "confidence": round(1.0 - self.rng.betavariate(1.2, 25.0), 3)}

    def _prop(self, g, prop, factor=1.0):
        t = g["props"][prop] if prop in g["props"] else None
        r = self.rates
        if prop == "head.fill":
            return self._read(t, ["filled", "hollow"], r[prop] * factor)
        if prop in ("stem.state",):
            return self._read(t, ["present", "absent"], r[prop] * factor)
        if prop == "stem.direction":
            return self._read(t, ["up", "down"], r[prop] * factor)
        if prop == "flags.count":
            return self._read(t, [c for c in range(5) if abs(c - t) == 1], r[prop] * factor)
        if prop == "beam_groups.beam_count":
            if t is None:
                return {"value": None, "confidence": 0.99}
            stem = next(s for b in self.g.beam_groups for s in b["stems"] if s["symbol_id"] == g["id"])
            rate = r["partial_beam"] if stem["partial"] else r[prop]
            return self._read(t, [c for c in range(1, 5) if abs(c - t) == 1] or [t + 1], rate * factor)
        if prop == "dots.count":
            return self._read(t, [0, 1], r[prop] * factor)
        if prop == "rest.shape":
            shapes = list(REST_SHAPES.values())
            k = shapes.index(t)
            return self._read(t, [shapes[j] for j in (k - 1, k + 1) if 0 <= j < len(shapes)], r[prop] * factor)
        if prop == "accidental.kind":
            if t is None:
                return {"value": None, "confidence": 0.99}
            return self._read(t, [a for a in ("sharp", "flat", "natural") if a != t], r[prop] * factor)
        if prop == "tuplet.member":
            missed = any(g["id"] in tp["symbol_ids"] and tp["id"] in self.missed_tuplets for tp in self.g.tuplets)
            if missed and factor == 1.0:
                return {"value": False, "confidence": round(self.rng.uniform(0.6, 0.95), 3)}
            return self._read(t, [not t], r["tuplet.number"] * factor)
        raise KeyError(prop)

    def _box(self, g):
        return [round(g["x"] - 6, 1), round(g["y"] - 5, 1), round(g["x"] + 6, 1), round(g["y"] + 5, 1)]

    def _symbol(self, g, factor=1.0):
        out = {"id": g["id"], "type": g["type"], "staff_id": "st1", "measure_id": self.g.mid, "voice_id": None,
               "bbox": self._box(g), "center": {"x": g["x"], "y": g["y"]}}
        if g["type"] == "rest":
            out["rest"] = {"shape": self._prop(g, "rest.shape", factor)}
            out["dots"] = {"count": self._prop(g, "dots.count", factor)}
            return out
        fill = self._prop(g, "head.fill", factor)
        stem = self._prop(g, "stem.state", factor)
        out["head"] = {"fill": fill["value"], "confidence": fill["confidence"]}
        d = self._prop(g, "stem.direction", factor)
        out["stem"] = {"state": stem["value"], "confidence": stem["confidence"],
                       "direction": d["value"], "direction_confidence": d["confidence"]}
        in_beam = g.get("beam") and g["beam"] not in self.missed_beams
        if in_beam:
            out["flags"] = {"count": 0, "confidence": 0.99}
        elif g.get("beam"):                                   # the beam was missed: the stem looks bare
            out["flags"] = {"count": 0, "confidence": round(self.rng.uniform(0.6, 0.95), 3)}
        else:
            fl = self._prop(g, "flags.count", factor)
            out["flags"] = {"count": fl["value"], "confidence": fl["confidence"]}
        out["beam_group_ids"] = [g["beam"]] if in_beam else []
        dt = self._prop(g, "dots.count", factor)
        out["dots"] = {"count": dt["value"], "confidence": dt["confidence"]}
        return out

    # ---- the observation ---------------------------------------------------------------------------
    def observe(self) -> dict:
        symbols = [self._symbol(g) for g in self.g.symbols if g["id"] not in self.missed]
        for k, s in enumerate(sorted(symbols, key=lambda s: s["center"]["x"])):
            s["order"] = k
        beams = []
        for b in self.g.beam_groups:
            if b["id"] in self.missed_beams:
                continue
            stems = []
            for st in b["stems"]:
                if st["symbol_id"] in self.missed:
                    continue
                g = next(g for g in self.g.symbols if g["id"] == st["symbol_id"])
                c = self._prop(g, "beam_groups.beam_count")
                stems.append({"symbol_id": st["symbol_id"], "beam_count": c["value"], "confidence": c["confidence"],
                              "partial": st["partial"]})
            beams.append({"id": b["id"], "symbol_ids": [s["symbol_id"] for s in stems], "stems": stems})
        tuplets = []
        for t in self.g.tuplets:
            if t["id"] in self.missed_tuplets:
                continue
            num = self._read(3, [2, 4, 5], self.rates["tuplet.number"])
            tuplets.append({"id": t["id"], "number": num["value"], "confidence": num["confidence"],
                            "symbol_ids": [i for i in t["symbol_ids"] if i not in self.missed]})
        accs = []
        for a in self.g.accidentals:
            if a["note"] in self.missed:
                continue
            g = next(g for g in self.g.symbols if g["id"] == a["note"])
            k = self._prop(g, "accidental.kind")
            accs.append({"id": a["id"], "type": "accidental", "kind": k["value"], "confidence": k["confidence"],
                         "bbox": [a["x"] - 3, a["y"] - 6, a["x"] + 2, a["y"] + 6], "center": {"x": a["x"], "y": a["y"]}})
        first, last = self.g.symbols[0], self.g.symbols[-1]
        return {"status": "ok", "measures": [{
            "id": self.g.mid, "staff": {"id": "st1", "top_y": STAFF_TOP, "spacing": SPACING, "slope": 0.0,
                                        "lines_y": [STAFF_TOP + k * SPACING for k in range(5)]},
            "barlines": {"left_x": first["x"] - 12.0,
                         "right_x": last["x"] + 12.0 + 120.0 * float(self.g.m.symbols[-1].duration)},
            "symbols": symbols, "beam_groups": beams, "tuplets": tuplets, "accidentals": accs}]}

    # ---- requests ---------------------------------------------------------------------------------
    def relook(self, request: dict) -> dict:
        self.requests += 1
        results = []
        for sid in request["target_symbol_ids"]:
            g = next((g for g in self.g.symbols if g["id"] == sid), None)
            if g is None:
                results.append({"symbol_id": sid, "error": "unknown symbol id"})
                continue
            props = {}
            for p in request["requested_properties"]:
                if p not in (PROPERTIES_REST if g["type"] == "rest" else PROPERTIES_NOTE):
                    continue
                self.relooked += 1
                if p == "flags.count" and g.get("beam") in self.missed_beams and self.rng.random() > RELOOK:
                    # a closer look at the stem end finds the beam that was missed: beams, not flags
                    b = next(b for b in self.g.beam_groups if b["id"] == g["beam"])
                    self.missed_beams.discard(g["beam"])
                    c = self._prop(g, "beam_groups.beam_count", RELOOK)
                    c["evidence"] = {"beam_group": {"id": b["id"], "symbol_ids": b["symbol_ids"]}}
                    props["beam_groups.beam_count"] = c
                    props["flags.count"] = {"value": 0, "confidence": 0.99}
                    continue
                if (p == "beam_groups.beam_count" and g["props"].get(p) is None) or \
                        (p == "accidental.kind" and g["props"].get(p) is None):
                    continue                          # not a property of this symbol: nothing to report
                if p in ("tuplet.member", "tuplet.number"):
                    props[p] = self._tuplet_look(g, p)
                    continue
                r = self._prop(g, p, RELOOK)
                r["evidence"] = {"region": [round(g["x"] - 3 * SPACING, 1), round(g["y"] - 4 * SPACING, 1),
                                            round(g["x"] + 3 * SPACING, 1), round(g["y"] + 4 * SPACING, 1)]}
                props[p] = r
            results.append({"symbol_id": sid, "properties": props})
        return {"request_id": request["request_id"], "measure_id": request["measure_id"], "results": results}

    def _tuplet_look(self, g, prop):
        """A closer look for a tuplet mark over this symbol: whether there is one, its number, and the
        symbols it spans (as evidence)."""
        t = next((t for t in self.g.tuplets if g["id"] in t["symbol_ids"]), None)
        seen = t is not None and self.rng.random() > self.rates["tuplet_miss"] * RELOOK
        if prop == "tuplet.member":
            r = self._read(seen, [not seen], 0.0)
        else:
            r = self._read(3, [2, 4, 5], self.rates["tuplet.number"] * RELOOK) if seen else {"value": None, "confidence": 0.95}
        if seen:
            self.missed_tuplets.discard(t["id"])
            r["evidence"] = {"tuplet": {"id": t["id"], "symbol_ids": [i for i in t["symbol_ids"] if i not in self.missed]}}
        return r

    def examine_region(self, request: dict) -> dict:
        self.regions += 1
        x0, x1 = request["region"]["x0"], request["region"]["x1"]
        found = []
        for g in self.g.symbols:
            if g["id"] in self.missed and x0 < g["x"] < x1 and self.rng.random() < FIND:
                self.missed.discard(g["id"])
                found.append(self._symbol(g, RELOOK))
        return {"request_id": request["request_id"], "measure_id": request["measure_id"], "new_symbols": found}

    def truth_events(self):
        return self.g

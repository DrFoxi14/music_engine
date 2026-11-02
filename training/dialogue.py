"""The two-way conversation between the vision engine and the music engines, one measure at a time.

  vision  -> rhythm   "observation": what I see, symbol by symbol, each feature with a confidence
  rhythm  -> vision   "verdict": valid / not valid (rhythm_search_engine: does this exact sequence fill
                      the measure?) and, from omr_constraint_solver, the most likely valid readings
  rhythm  -> vision   "relook": the features that separate my reading from the valid ones; look again
  rhythm  -> vision   "search": time is missing here (where the spacing leaves room), and these are the
                      symbols that would fit (rhythm_search_engine with the known beginning and end)
  vision  -> rhythm   "relook_result" / "search_result"
  ...until the reading is valid and no other valid reading is close; then
  rhythm  -> pitch    the settled noteheads, accidentals and key go to pitch_context_engine
  pitch   -> output   letter, octave, alteration of every note

The vision's confidences are calibrated on training measures (`Calibration`): a 0.8 from the vision
becomes the measured probability that a feature read with confidence 0.8 is right.
"""
from __future__ import annotations

import itertools
import json
import math
from collections import defaultdict
from fractions import Fraction

from synth import SPACING, STAFF_TOP

DUR_FEATURES = {"head": ("fill", "stem", "beams", "dots", "tuplet", "exists"),
                "rest": ("rest_den", "dots", "tuplet", "exists")}
MAX_CHANGES = 2         # alternatives per symbol: readings with at most this many features changed
AMBIGUOUS = 0.25        # a second valid reading this close (score ratio) to the best one is worth a look
MAX_ROUNDS = 4          # at least; a dense measure gets one more round per ROUND_PER symbols
ROUND_PER = 4
SOLUTIONS = 10          # solver readings checked by rhythm_search_engine each round
APPLY_BEST = False      # rounds over, still not valid: take the solver's most likely reading (no: −9 pt notes)
PITCH_SURE = 0.9        # a staff position or accidental less sure than this is looked at again
MAX_RELOOKS = 2         # per feature
MAX_SEARCHES = 2        # per round


# ---- calibration (the trained part) ------------------------------------------------------------
class Calibration:
    """P(feature is right | confidence bin), per feature, separately for a first look and a relook."""
    BINS = 10

    def __init__(self, table=None, p_missing=0.08):
        self.table = table or {}
        self.p_missing = p_missing

    def _bin(self, c):
        return min(int(c * self.BINS), self.BINS - 1)

    def p(self, feature, conf, relook=False):
        key = f"{feature}{'@relook' if relook else ''}"
        row = self.table.get(key)
        if not row:
            return min(max(conf, 0.02), 0.995)
        n, k = row[self._bin(conf)]
        return (k + 1.0) / (n + 2.0) if n else min(max(conf, 0.02), 0.995)

    @classmethod
    def fit(cls, records, p_missing):
        t = defaultdict(lambda: [[0, 0] for _ in range(cls.BINS)])
        for feature, conf, right, relook in records:
            cell = t[f"{feature}{'@relook' if relook else ''}"][min(int(conf * cls.BINS), cls.BINS - 1)]
            cell[0] += 1
            cell[1] += int(right)
        return cls(dict(t), p_missing)

    def to_json(self):
        return json.dumps({"table": self.table, "p_missing": self.p_missing})

    @classmethod
    def from_json(cls, text):
        d = json.loads(text)
        return cls(d["table"], d["p_missing"])


# ---- reading a symbol from its features ------------------------------------------------------------
def _val(o, f):
    return o["features"][f]["v"]


def base_den(o, override=None):
    g = (lambda f: override[f]) if override else (lambda f: _val(o, f))
    if o["kind"] == "rest":
        return g("rest_den")
    if not g("fill"):
        return 2 if g("stem") else 1
    return 4 * 2 ** g("beams")


def duration(o, override=None):
    g = (lambda f: override[f]) if override else (lambda f: _val(o, f))
    if o.get("alone_in") is not None and o["kind"] == "rest" and base_den(o, override) == 1 and not g("dots"):
        return o["alone_in"]                       # a whole rest alone in the measure: the measure rest
    d = Fraction(1, base_den(o, override)) * (Fraction(3, 2) if g("dots") else 1)
    return d * Fraction(2, 3) if g("tuplet") else d


def tokens(obs):
    """Rhythm-engine descriptors in order: one per symbol, three tuplet slots make one triplet."""
    out, run = [], []

    def plain(o):
        if o.get("alone_in") is not None and duration(o) == o["alone_in"] and o["alone_in"] != 1 and o["kind"] == "rest":
            return {"desc": "measure_rest@0", "sids": [o["sid"]], "mask": None}
        kind = ("dotted_" if _val(o, "dots") else "") + ("rest" if o["kind"] == "rest" else "note")
        return {"desc": f"{kind}@{base_den(o)}", "sids": [o["sid"]], "mask": None}
    for o in obs:
        if _val(o, "tuplet"):
            run.append(o)
            if len(run) == 3:
                dens = {base_den(x) for x in run}
                ok = len(dens) == 1 and not any(_val(x, "dots") for x in run)
                out.append({"desc": f"triplet@{dens.pop()}" if ok else None, "sids": [x["sid"] for x in run],
                            "mask": "".join("R" if x["kind"] == "rest" else "N" for x in run)})
                run = []
            continue
        out += [{"desc": None, "sids": [x["sid"]], "mask": None} for x in run]   # an unfinished triplet
        run = []
        out.append(plain(o))
    out += [{"desc": None, "sids": [x["sid"]], "mask": None} for x in run]
    return out


# ---- the dialogue ------------------------------------------------------------------------------
class Dialogue:
    def __init__(self, engines, calib: Calibration, log=None, record=None):
        self.E, self.cal = engines, calib
        self.log = log if log is not None else []
        self.record = record                 # training: (feature, conf, right, relook) after each relook

    def say(self, rnd, frm, to, typ, **payload):
        self.log.append({"round": rnd, "from": frm, "to": to, "type": typ, **payload})

    def p(self, o, f):
        x = o["features"][f]
        return x["p"] if "p" in x else self.cal.p(f, x["c"], x.get("relook", False))

    # rhythm_search_engine: is this exact sequence a valid measure?
    def valid(self, meter, toks):
        if not toks or any(t["desc"] is None for t in toks):
            return False
        rests = any("rest" in t["desc"] or (t["mask"] and "R" in t["mask"]) for t in toks)
        masks = sorted({t["mask"] for t in toks if t["mask"]})
        flt = dict(prefix=[t["desc"] for t in toks], min_events=len(toks), max_events=len(toks), limit=1,
                   max_nodes=200_000)
        if masks:
            flt["triplet_patterns"] = masks
        try:
            r = self.E.search(meter).search("RESTS" if rests else "NOTES", **flt)
        except ValueError:
            return False
        return bool(r["patterns"])

    # rhythm_search_engine: what single symbol fits between a known beginning and end?
    def what_fits(self, meter, before, after):
        if any(t["desc"] is None for t in before + after):
            return []
        fits = set()
        for cat in ("NOTES", "RESTS"):
            flt = dict(min_events=len(before) + len(after) + 1, max_events=len(before) + len(after) + 1,
                       limit=40, max_nodes=200_000)
            if before:
                flt["prefix"] = [t["desc"] for t in before]
            if after:
                flt["suffix"] = [t["desc"] for t in after]
            masks = sorted({t["mask"] for t in before + after if t["mask"]})
            if masks:
                flt["triplet_patterns"] = masks
            try:
                r = self.E.search(meter).search(cat, **flt)
            except ValueError:
                continue
            for pat in r["patterns"]:
                fits.add(pat["events"][len(before)]["descriptor"])
        return sorted(fits)

    def alternatives(self, o):
        """{duration: (confidence, changed features)} for one symbol, from its calibrated features."""
        feats = [f for f in DUR_FEATURES[o["kind"]] if f in o["features"]]
        choices = {}
        for f in feats:
            v = _val(o, f)
            if f in ("fill", "stem", "tuplet", "exists"):
                alts = [not v]
            elif f == "dots":
                alts = [1 - v]
            elif f == "beams":
                alts = [b for b in (v - 1, v + 1) if 0 <= b <= 4]
            else:                                                      # rest_den
                alts = [d for d in (v * 2, v // 2) if 1 <= d <= 64 and d != v]
            p = self.p(o, f)
            choices[f] = [(v, p)] + [(a, (1 - p) / len(alts)) for a in alts]
        out = {}
        for k in range(MAX_CHANGES + 1):
            for changed in itertools.combinations(feats, k):
                for picks in itertools.product(*[choices[f][1:] for f in changed]):
                    conf = 1.0
                    over = {f: choices[f][0][0] for f in feats}
                    for f in feats:
                        conf *= choices[f][0][1] if f not in changed else 1.0
                    for f, (val, pv) in zip(changed, picks):
                        over[f] = val
                        conf *= pv
                    dur = Fraction(0) if not over["exists"] else duration(o, over)
                    if dur and dur.denominator > 4096:
                        continue
                    spec = {f: over[f] for f in changed}
                    if dur not in out or out[dur][0] < conf:
                        out[dur] = (max(conf, 1e-9), spec)
        return out

    def solve(self, meter, obs, deficit):
        """omr_constraint_solver over every symbol's alternatives (+ a missed symbol if time is missing)."""
        events, alts = [], []
        for i, o in enumerate(obs):
            a = self.alternatives(o)
            alts.append(a)
            events.append({"id": f"e{i}", "candidates": [
                {"duration": "none" if d == 0 else f"{d.numerator}/{d.denominator}", "confidence": min(c, 1.0)}
                for d, (c, _) in sorted(a.items(), key=lambda kv: -kv[1][0])[:24]]})
        if deficit > 0:
            gap = [{"duration": "none", "confidence": 1.0}]
            for den in (1, 2, 4, 8, 16, 32, 64):
                for d in (Fraction(1, den), Fraction(3, 2 * den)):
                    if d <= deficit:
                        gap.append({"duration": f"{d.numerator}/{d.denominator}", "confidence": self.cal.p_missing})
            events.append({"id": "gap", "candidates": gap})
        if not events:
            return [], alts
        r = self.E.solver.resolve(f"{meter[0]}/{meter[1]}", events, max_note_denominator=64, limit=SOLUTIONS,
                                  max_nodes=300_000)
        return r.get("solutions", []), alts

    def gap_segments(self, obs, x_start, x_end):
        """Where time is missing: spacing on a page grows with duration, so the segment with the most
        room left over (after a fitted width = a + b·duration) is the first place to look."""
        xs = [x_start] + [o["x"] for o in obs] + [x_end]
        durs = [Fraction(0)] + [duration(o) for o in obs]
        pts = [(float(durs[i + 1]), obs[i + 1]["x"] - obs[i]["x"]) for i in range(len(obs) - 1)]
        if len(pts) >= 3:
            n = len(pts)
            mx, my = sum(p[0] for p in pts) / n, sum(p[1] for p in pts) / n
            sxx = sum((p[0] - mx) ** 2 for p in pts)
            b = sum((p[0] - mx) * (p[1] - my) for p in pts) / sxx if sxx > 1e-12 else 0.0
            a = my - b * mx
        else:
            a, b = 12.0, 120.0
        seg = []
        for i in range(len(xs) - 1):
            expect = a + b * float(durs[i]) if i else a
            seg.append(((xs[i + 1] - xs[i]) - expect, i, xs[i], xs[i + 1]))
        return sorted(seg, reverse=True)

    def run(self, measure_info, vision):
        meter, cap = measure_info["meter"], measure_info["capacity"]
        obs = sorted(vision.observe(), key=lambda o: o["x"])
        for o in obs:
            o["features"] = {f: {"v": v, "c": c} for f, (v, c) in o["features"].items()}
        self.say(0, "vision", "rhythm", "observation", symbols=[
            {"sid": o["sid"], "kind": o["kind"], **{f: [x["v"], x["c"]] for f, x in o["features"].items()}} for o in obs])
        self.initial = [{**o, "features": {f: dict(x) for f, x in o["features"].items()}} for o in obs]
        looks = defaultdict(int)
        status = "forced"
        for rnd in range(1, max(MAX_ROUNDS, len(obs) // ROUND_PER) + 1):
            for o in obs:
                o["alone_in"] = cap if len(obs) == 1 else None
            toks = tokens(obs)
            ok = self.valid(meter, toks)
            total = sum((duration(o) for o in obs), Fraction(0))
            sols, alts = self.solve(meter, obs, cap - total)
            reading = [duration(o) for o in obs]

            def changes(sol):
                out, gap = [], False
                for ev in sol["events"]:
                    if ev["event_id"] == "gap":
                        gap = ev["present"]
                        continue
                    i = int(ev["event_id"][1:])
                    d = Fraction(ev["duration_whole"]) if ev["present"] else Fraction(0)
                    if d != reading[i]:
                        out.append((i, d))
                return out, gap
            # the solver fills the measure symbol by symbol; rhythm_search_engine keeps only the readings that
            # are music (a triplet has three slots, a measure rest is alone ...)
            kept = []
            for sol in sols:
                chg, gap = changes(sol)
                if not gap and chg:
                    trial = [{**o, "features": {f: dict(x) for f, x in o["features"].items()}} for o in obs]
                    self.apply(trial, alts, chg)
                    trial = [o for o in trial if o["features"]["exists"]["v"]]
                    for o in trial:
                        o["alone_in"] = cap if len(trial) == 1 else None
                    if not self.valid(meter, tokens(trial)):
                        continue
                kept.append((sol, (chg, gap)))
            self.say(rnd, "rhythm", "rhythm", "filter", solver_readings=len(sols), musically_valid=len(kept))
            if not kept:                      # none is music yet: still ask about what the solver would change
                kept = [(sol, changes(sol)) for sol in sols]
            best = kept[0][1] if kept else ([], False)
            close = [c for s_, c in kept[1:]
                     if s_["log_visual_score"] > kept[0][0]["log_visual_score"] + math.log(AMBIGUOUS)]
            self.say(rnd, "rhythm", "vision", "verdict", valid=ok, total=str(total), measure=str(cap),
                     best_changes=[[obs[i]["sid"], str(d)] for i, d in best[0]], missing_symbol=best[1],
                     close_alternatives=len(close))
            if ok and not best[0] and not best[1] and not close:
                status = "valid"
                break
            # what to look at again: the features that turn my reading into the valid ones
            asks = []
            for chg, _ in [best] + close:
                for i, d in chg:
                    spec = alts[i].get(d, (0, {}))[1]
                    asks += [(obs[i]["sid"], f) for f in spec]
            asks = [a for a in dict.fromkeys(asks) if looks[a] < MAX_RELOOKS]
            found = False
            if best[1] and cap - total > 0:
                toks_now = tokens(obs)
                for _, i, x0, x1 in self.gap_segments(obs, measure_info["x_start"], measure_info["x_end"])[:MAX_SEARCHES]:
                    k = sum(1 for t in toks_now if any(s in [o["sid"] for o in obs[:i]] for s in t["sids"]))
                    fits = self.what_fits(meter, toks_now[:k], toks_now[k:])
                    self.say(rnd, "rhythm", "vision", "search", between=[round(x0, 1), round(x1, 1)],
                             missing=str(cap - total), would_fit=fits[:12])
                    new = vision.search(x0, x1)
                    self.say(rnd, "vision", "rhythm", "search_result", found=bool(new),
                             symbol=None if not new else {"sid": new["sid"], "kind": new["kind"]})
                    if new:
                        new["features"] = {f: {"v": v, "c": c, "relook": True} for f, (v, c) in new["features"].items()}
                        obs = sorted(obs + [new], key=lambda o: o["x"])
                        found = True
                        break
            if not asks and not found:
                if sols and not ok and APPLY_BEST:
                    self.apply(obs, alts, best[0])
                    self.say(rnd, "rhythm", "vision", "accept_best", note="vision cannot tell more; most likely valid reading taken")
                    status = "solver"
                else:
                    status = "valid" if ok else "unresolved"
                break
            if asks:
                self.say(rnd, "rhythm", "vision", "relook", features=[list(a) for a in asks])
            res = []
            for sid, f in asks:
                looks[(sid, f)] += 1
                o = next(o for o in obs if o["sid"] == sid)
                v, c = vision.relook(sid, f)
                old = o["features"][f]
                p_new = self.cal.p(f, c, True)
                if v == old["v"]:          # two looks agree
                    p = 1 - (1 - self.p(o, f)) * (1 - p_new)
                else:
                    p = p_new
                if self.record is not None:
                    self.record(o, f, c, v)
                o["features"][f] = {"v": v, "c": c, "relook": True, "p": min(p, 0.999)}
                res.append([sid, f, v, round(p, 3)])
            if res:
                self.say(rnd, "vision", "rhythm", "relook_result", features=res)
            obs = [o for o in obs if o["features"]["exists"]["v"]]
        obs = [o for o in obs if o["features"]["exists"]["v"]]
        for o in obs:
            o["alone_in"] = cap if len(obs) == 1 else None
        return obs, status

    def apply(self, obs, alts, chg):
        for i, d in chg:
            for f, v in alts[i].get(d, (0, {}))[1].items():
                obs[i]["features"][f] = {**obs[i]["features"][f], "v": v}

    # pitch_context_engine: the settled noteheads -> pitches; unsure positions and accidentals, and the
    # accidentals the engine cannot place, are looked at again first
    def pitches(self, measure_info, obs, mid="0", vision=None):
        heads = [o for o in obs if o["kind"] == "head" and o["features"]["exists"]["v"]]
        if not heads:
            return {}
        if vision is not None:
            asks = [(o, f) for o in heads for f in ("step", "accidental") if f in o["features"]
                    and not o["features"][f].get("relook") and self.p(o, f) < PITCH_SURE]
            if asks:
                self.say(8, "pitch", "vision", "relook", features=[[o["sid"], f] for o, f in asks])
                res = []
                for o, f in asks:
                    v, c = vision.relook(o["sid"], f)
                    old = o["features"][f]
                    p_new = self.cal.p(f, c, True)
                    p = 1 - (1 - self.p(o, f)) * (1 - p_new) if v == old["v"] else p_new
                    if self.record is not None:
                        self.record(o, f, c, v)
                    o["features"][f] = {"v": v, "c": c, "relook": True, "p": min(p, 0.999)}
                    res.append([o["sid"], f, v, round(p, 3)])
                self.say(8, "vision", "pitch", "relook_result", features=res)
        notes, accs = [], []
        for o in heads:
            y = STAFF_TOP + 4 * SPACING - _val(o, "step") * SPACING / 2
            notes.append({"id": o["sid"], "x": o["x"], "y": y})
            a = _val(o, "accidental")
            if a:
                accs.append({"id": "a" + o["sid"], "kind": a, "anchor_y": y,
                             "bbox": {"left": o["x"] - 9, "top": y - 6, "right": o["x"] - 4, "bottom": y + 6}})
        r = self.E.pitch.resolve(clef="treble", staff_top_y=STAFF_TOP, line_spacing=SPACING,
                                 key_signature=measure_info["key"], measure_id=mid, notes=notes, accidentals=accs)
        self.say(9, "rhythm", "pitch", "resolve", notes=len(notes), accidentals=len(accs))
        out = {n["id"]: (n["letter"], n["octave"], n["effective_alteration"]) for n in r["notes"]}
        self.say(9, "pitch", "output", "pitches", status=r.get("status"),
                 uncertain=[n["id"] for n in r["notes"] if not n.get("pitch_certain", True)])
        return out

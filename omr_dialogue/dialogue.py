"""The dialogue between Vision and the rhythm Search Engine (Vision specification, section 5).

  1. Vision observes and reports the measure (JSON).
  2. The adapter turns the observation into per-event constraints (adapter.py).
  3. rhythm_search_engine returns every musically valid interpretation that respects them.
  4. Several interpretations: the dialogue finds the visual properties by which they differ.
  5. Vision is asked to re-examine exactly those properties of exactly those symbols (by ID).
  6. Vision answers with updated observations.
  7. The search runs again.
  8. Until one interpretation is left, or the remaining ambiguity cannot be removed safely.

Nothing is forced. With no interpretation at all, the dialogue looks for the observations that are
contradictory (a certain property whose relaxation makes the measure possible) and asks Vision about
them; if a symbol seems to be missing, it asks Vision to re-examine the region where time is missing.
If that does not help, it reports the contradiction instead of choosing.

The question asked each round is the one that splits the remaining interpretations best. A property
Vision has re-examined and still cannot read is not asked again.

When the rhythm is settled, the noteheads, their positions and the accidentals go to
pitch_context_engine.
"""
from __future__ import annotations

from collections import Counter
from fractions import Fraction

import adapter as A

LIMIT = 200              # interpretations requested from the search per structure
MAX_ROUNDS = 40
MAX_ASKS = 2             # times one property of one symbol may be re-examined
TRUST_SUM = False        # one interpretation left but Vision disagrees: let the sum decide (status "sum")
VERIFY = 0.97            # before a measure is settled, decisive properties read with less confidence than
                         # this (and not yet confirmed) are re-examined
SPACING_ROOM = 0.5       # a gap wider than its durations need by this fraction of the fixed spacing (a)
                         # may hide a missed symbol: the region is re-examined
NODES = 300_000          # search budget per call


class Dialogue:
    def __init__(self, engines, log=None):
        self.E = engines
        self.log = log if log is not None else []
        self._q = 0

    def say(self, frm, to, typ, **payload):
        self.log.append({"from": frm, "to": to, "type": typ, **payload})

    def _rid(self):
        self._q += 1
        return f"q{self._q}"

    # ---- 3. the search -----------------------------------------------------------------------------
    def interpretations(self, meter, symbols, limit=LIMIT):
        out, more = [], False
        for evts in A.structures(symbols):
            category, positions = A.search_args(evts, symbols)
            try:
                r = self.E.search(meter).search(category, positions=positions, limit=limit, max_nodes=NODES)
            except ValueError:
                continue
            more |= r["summary"]["has_more"] or r["summary"]["truncated"]
            for p in r["patterns"]:
                implied, descs = {}, []
                for ev, pe in zip(evts, p["events"]):
                    k = A.key_of(pe)
                    descs.append(k)
                    implied.update(ev["alternatives"][k])
                out.append({"events": descs, "symbols": [e["symbols"] for e in evts], "implied": implied})
        return out, more

    # ---- 4. what separates the interpretations -------------------------------------------------------
    def questions(self, variants, symbols, asked):
        by = {s.id: s for s in symbols}
        split = []
        keys = {(sid, p) for v in variants for sid, props in v["implied"].items() for p in props}
        for sid, p in keys:
            if asked[(sid, p)] >= MAX_ASKS or (sid in by and by[sid].props.get(p, [0, 0, False])[2]):
                continue
            values = Counter(str(v["implied"].get(sid, {}).get(p)) for v in variants)
            if len(values) > 1:
                split.append((len(variants) - max(values.values()), sid, p))
        split.sort(reverse=True)
        if not split:
            return None
        sid = split[0][1]
        return sid, sorted({p for _, s, p in split if s == sid})

    # ---- no interpretation: which observations contradict each other -------------------------------
    def contradictions(self, meter, symbols, asked):
        """Observations that cannot all be true, in three tiers: a symbol that contradicts itself (a hollow
        head under a beam); a certain but unconfirmed property whose relaxation makes the measure
        possible; the same for a confirmed property (a re-examination can be wrong too)."""
        own = []
        for s in symbols:
            if s.readings():
                continue
            own += [(s.props[p][1], s.id, p) for p in s.props if asked[(s.id, p)] < MAX_ASKS]
        if own:
            return [(sid, p) for _, sid, p in sorted(own)]
        for confirmed in (False, True):
            suspects = []
            for s in symbols:
                for p, (v, c, ok) in s.props.items():
                    if ok != confirmed or not A.certain(v, c, ok) or asked[(s.id, p)] >= MAX_ASKS:
                        continue
                    saved = s.props[p]
                    s.props[p] = ["unknown", 0.0, False]
                    found, _ = self.interpretations(meter, symbols, limit=1)
                    s.props[p] = saved
                    if found:
                        suspects.append((c, s.id, p))
            if suspects:
                return [(sid, p) for _, sid, p in sorted(suspects)]
        return []

    def tuplet_suspects(self, meter, symbols, asked, unmarked=False):
        """Tuplet marks that may be misread (a certain number whose doubt makes the measure possible), or,
        with `unmarked`, three consecutive symbols that would make it possible under a mark not seen."""
        out = []
        if not unmarked:
            marks = {}
            for s in symbols:
                if s.tuplet:
                    marks.setdefault(s.tuplet[0], []).append(s)
            for tid, ss in marks.items():
                t = ss[0].tuplet
                if asked[(tid, "tuplet.number")] >= MAX_ASKS or not A.certain(t[1], t[2]):
                    continue
                saved = {s.id: s.tuplet for s in ss}
                for s in ss:
                    s.tuplet = (tid, t[1], 0.0)
                found, _ = self.interpretations(meter, symbols, limit=1)
                for s in ss:
                    s.tuplet = saved[s.id]
                if found:
                    out.append(("tuplet.number", tid, [s.id for s in ss]))
            return out
        for i in range(len(symbols) - 2):
            win = symbols[i:i + 3]
            key = ("tuplet.member",) + tuple(s.id for s in win)
            if any(s.tuplet for s in win) or asked[key] >= 1:
                continue
            for s in win:
                s.tuplet = ("?", 3, 1.0)
            found, _ = self.interpretations(meter, symbols, limit=1)
            for s in win:
                s.tuplet = None
            if found:
                out.append(("tuplet.member", key, [s.id for s in win]))
        return out

    def missing_regions(self, meter, symbols, barlines):
        """Where a missed symbol would make the measure possible, widest spacing first."""
        xs = [barlines["left_x"]] + [s.x for s in symbols] + [barlines["right_x"]]
        gaps = sorted(((xs[i + 1] - xs[i], i) for i in range(len(xs) - 1)), reverse=True)
        out = []
        if not A.structures(symbols):
            return []
        for _, i in gaps:
            evts_all = A.structures(symbols)
            if not evts_all:
                break
            evts = evts_all[0]
            k = sum(1 for e in evts if all(sid in [s.id for s in symbols[:i]] for sid in e["symbols"]))
            category, positions = A.search_args(evts, symbols)
            for cat in dict.fromkeys((category, "RESTS")):        # the missed symbol may be a rest
                try:
                    r = self.E.search(meter).search(cat, positions=positions[:k] + [["*"]] + positions[k:], limit=1,
                                                   max_nodes=NODES)
                except ValueError:
                    continue
                if r["patterns"]:
                    out.append((xs[i], xs[i + 1]))
                    break
        return out

    def roomy_gaps(self, symbols, variant, barlines):
        """Gaps with more room than the durations of the settled reading need (spacing grows with
        duration on a page: width = a + b·duration, fitted on this measure)."""
        from dialogue import durations_of as _d
        dur = _d(variant)
        xs = [barlines["left_x"]] + [s.x for s in symbols] + [barlines["right_x"]]
        ds = [0.0] + [float(dur.get(s.id) or 0) for s in symbols]
        pts = [(ds[i + 1], symbols[i + 1].x - symbols[i].x) for i in range(len(symbols) - 1)]
        if len(pts) >= 3:
            n = len(pts)
            mx, my = sum(p[0] for p in pts) / n, sum(p[1] for p in pts) / n
            sxx = sum((p[0] - mx) ** 2 for p in pts)
            b = sum((p[0] - mx) * (p[1] - my) for p in pts) / sxx if sxx > 1e-12 else 0.0
            a = my - b * mx
        else:
            return []
        if a <= 0:
            return []
        out = []
        for i in range(len(xs) - 1):
            need = a + b * ds[i] if i else a
            if (xs[i + 1] - xs[i]) - need > SPACING_ROOM * a:
                out.append((xs[i], xs[i + 1]))
        return out

    def region(self, vision, measure, rnd, x0, x1, why):
        req = {"request_id": self._rid(), "measure_id": measure["id"], "region": {"x0": x0, "x1": x1}}
        self.say("dialogue", "vision", "examine_region", round=rnd, reason=why, request=req)
        rep = vision.examine_region(req)
        self.say("vision", "dialogue", "region_result", round=rnd, response=rep)
        if rep["new_symbols"]:
            measure["symbols"].extend(rep["new_symbols"])
            return True
        return False

    # ---- the whole dialogue ------------------------------------------------------------------------
    def run(self, meter, vision):
        obs = vision.observe()
        measure = obs["measures"][0]
        self.say("vision", "dialogue", "observation", measure=measure)
        symbols = A.parse(measure)
        asked = Counter()
        searched_regions = set()
        region_looks = Counter()
        status, variants = "unresolved", []
        for rnd in range(1, MAX_ROUNDS + 1):
            gaps = [g for g in A.incomplete_tuplets(symbols) if tuple(x.id for x in g) not in searched_regions]
            if gaps:
                g = gaps[0]
                searched_regions.add(tuple(x.id for x in g))
                xs = [s.x for s in symbols]
                lo = max([x for x in xs if x < g[0].x], default=measure["barlines"]["left_x"])
                hi = min([x for x in xs if x > g[-1].x], default=measure["barlines"]["right_x"])
                if self.region(vision, measure, rnd, lo, hi, "a triplet mark over fewer than three symbols"):
                    symbols = self.reparse(measure, symbols)
                continue
            variants, more = self.interpretations(meter, symbols)
            self.say("search", "dialogue", "interpretations", round=rnd, count=len(variants), more=more,
                     sample=[v["events"] for v in variants[:5]])
            if len(variants) == 1 and not more:
                # before settling: decisive properties read without enough certainty are re-examined,
                # and room in the spacing that no duration explains is searched for a missed symbol
                unsure, unconfirmed = {}, []
                for sid, props in variants[0]["implied"].items():
                    s = next(x for x in symbols if x.id == sid)
                    for p, want in props.items():
                        v, c, ok = s.props[p]
                        if ok or c >= VERIFY:
                            if v == want:
                                continue
                        if asked[(sid, p)] < MAX_ASKS:
                            unsure.setdefault(sid, []).append(p)
                        elif not (A.certain(v, c, ok) and v == want):
                            unconfirmed.append([sid, p, v, c])
                if not unsure and unconfirmed and TRUST_SUM:
                    # Paul: the sum decides. One interpretation is left; Vision's reading disagrees with it on
                    # these properties, but the measure's arithmetic allows nothing else
                    status = "sum"
                    self.say("dialogue", "output", "sum", round=rnd, overruled=unconfirmed[:12])
                    break
                if not unsure and unconfirmed:
                    # musically valid is not visually certain: the reading this interpretation needs was
                    # not confirmed by Vision, so the measure is not settled
                    status = "unconfirmed"
                    self.say("dialogue", "output", "unconfirmed", round=rnd, observations=unconfirmed[:12])
                    break
                if unsure:
                    targets = list(unsure)
                    props = sorted({p for ps in unsure.values() for p in ps})
                    self.say("dialogue", "dialogue", "verify", round=rnd, symbols=targets)
                else:
                    roomy = [r for r in self.roomy_gaps(symbols, variants[0], measure["barlines"])
                             if r not in searched_regions]
                    if roomy:
                        searched_regions.add(roomy[0])
                        if self.region(vision, measure, rnd, *roomy[0], "more room than the durations need"):
                            symbols = self.reparse(measure, symbols)
                        continue
                    status = "resolved"
                    break
            elif variants:
                q = self.questions(variants, symbols, asked)
                if q is None:
                    status = "ambiguous"
                    break
                targets, props = [q[0]], q[1]
            else:
                tsus = self.tuplet_suspects(meter, symbols, asked)
                if not tsus and not self.contradictions(meter, symbols, asked):
                    tsus = self.tuplet_suspects(meter, symbols, asked, unmarked=True)
                if tsus:
                    prop, key, ids = tsus[0]
                    req = {"request_id": self._rid(), "measure_id": measure["id"], "target_symbol_ids": ids,
                           "requested_properties": [prop]}
                    self.say("dialogue", "vision", "relook", round=rnd, reason="tuplet", request=req)
                    rep = vision.relook(req)
                    self.say("vision", "dialogue", "relook_result", round=rnd, response=rep)
                    asked[(key, prop) if prop == "tuplet.number" else key] += 1
                    self.update_tuplets(symbols, measure, rep)
                    continue
                sus = self.contradictions(meter, symbols, asked)
                if sus:
                    targets = list(dict.fromkeys(sid for sid, _ in sus[:3]))
                    props = sorted({p for sid, p in sus[:3]})
                    self.say("dialogue", "dialogue", "contradiction", round=rnd, suspects=[list(x) for x in sus[:6]])
                else:
                    regions = [r for r in self.missing_regions(meter, symbols, measure["barlines"]) if r not in searched_regions]
                    if not regions:
                        status = "contradiction"
                        report = [[s.id, p, v, c, ok] for s in symbols for p, (v, c, ok) in s.props.items()
                                  if not s.readings() or not A.certain(v, c, ok)]
                        self.say("dialogue", "output", "contradiction_report", round=rnd,
                                 note="no valid interpretation and nothing left to re-examine", observations=report[:20])
                        break
                    region = regions[0]
                    region_looks[region] += 1
                    if region_looks[region] >= MAX_ASKS:
                        searched_regions.add(region)
                    if self.region(vision, measure, rnd, *region, "a symbol may be missing here"):
                        symbols = self.reparse(measure, symbols)
                    continue
            req = {"request_id": self._rid(), "measure_id": measure["id"], "target_symbol_ids": targets,
                   "requested_properties": props}
            self.say("dialogue", "vision", "relook", round=rnd, request=req)
            rep = vision.relook(req)
            self.say("vision", "dialogue", "relook_result", round=rnd, response=rep)
            self.update(symbols, rep, asked)
        return {"status": status, "symbols": symbols, "variants": variants, "measure": measure}

    def reparse(self, measure, old):
        """Symbols after Vision added some: everything already re-examined is kept."""
        keep = {s.id: s for s in old}
        new = A.parse(measure)
        for s in new:
            if s.id in keep:
                s.props, s.tuplet = keep[s.id].props, keep[s.id].tuplet
        return new

    def update_tuplets(self, symbols, measure, reply):
        by = {s.id: s for s in symbols}
        for res in reply["results"]:
            for p, r in res.get("properties", {}).items():
                ev = (r.get("evidence") or {}).get("tuplet")
                if p == "tuplet.member" and r["value"] is True and ev:
                    for sid in ev["symbol_ids"]:
                        if sid in by and not by[sid].tuplet:
                            by[sid].tuplet = (ev["id"], 3, r["confidence"])
                    if not any(t["id"] == ev["id"] for t in measure["tuplets"]):
                        measure["tuplets"].append({"id": ev["id"], "number": "unknown", "confidence": 0.0,
                                                   "symbol_ids": ev["symbol_ids"]})
                if p == "tuplet.number" and r["value"] not in (None, "unknown") and by.get(res["symbol_id"]):
                    tid = by[res["symbol_id"]].tuplet[0] if by[res["symbol_id"]].tuplet else (ev or {}).get("id")
                    for s in symbols:
                        if s.tuplet and s.tuplet[0] == tid:
                            s.tuplet = (tid, r["value"], max(r["confidence"], A.CERTAIN) if r["confidence"] >= 0.6 else r["confidence"])

    def update(self, symbols, reply, asked):
        by = {s.id: s for s in symbols}
        for res in reply["results"]:
            s = by.get(res["symbol_id"])
            if s is None:
                continue
            props = res.get("properties", {})
            if isinstance((props.get("beam_groups.beam_count") or {}).get("value"), int) and "beam_groups.beam_count" not in s.props:
                # Vision found a beam where it had seen none: the symbol's stem now counts beams, not flags
                s.props.pop("flags.count", None)
                s.props["beam_groups.beam_count"] = ["unknown", 0.0, False]
            for p, r in props.items():
                if p not in s.props:
                    continue
                asked[(s.id, p)] += 1
                old = s.props[p]
                v, c = r["value"], r["confidence"]
                # a re-examination that agrees with a known first reading confirms it
                confirmed = v not in ("unknown", None) and (c >= A.CERTAIN or (v == old[0] and c >= 0.6))
                s.props[p] = [v, c, confirmed]

    # ---- pitch -------------------------------------------------------------------------------------
    def pitches(self, key, result, vision=None):
        measure = result["measure"]
        notes = [s for s in measure["symbols"] if s["type"] == "note"]
        if not notes:
            return {}
        accs = []
        for a in measure.get("accidentals", []):
            kind, c = a["kind"], a["confidence"]
            if not A.certain(kind, c) and vision is not None:
                note = min(notes, key=lambda n: abs(n["center"]["x"] - a["center"]["x"] - 7))
                req = {"request_id": self._rid(), "measure_id": measure["id"], "target_symbol_ids": [note["id"]],
                       "requested_properties": ["accidental.kind"]}
                self.say("pitch", "vision", "relook", request=req)
                rep = vision.relook(req)
                self.say("vision", "pitch", "relook_result", response=rep)
                got = rep["results"][0]["properties"].get("accidental.kind")
                if got:
                    kind, c = got["value"], got["confidence"]
            if kind not in (None, "unknown"):
                x, y = a["center"]["x"], a["center"]["y"]
                accs.append({"id": a["id"], "kind": kind, "anchor_y": y, "confidence": c,
                             "bbox": {"left": x - 3, "top": y - 6, "right": x + 2, "bottom": y + 6}})
        st = measure["staff"]
        r = self.E.pitch.resolve(clef="treble", staff_top_y=st["top_y"], line_spacing=st["spacing"], key_signature=key,
                                 measure_id=measure["id"], notes=[{"id": n["id"], "x": n["center"]["x"], "y": n["center"]["y"]}
                                                                  for n in notes], accidentals=accs)
        self.say("dialogue", "pitch", "resolve", notes=len(notes), accidentals=len(accs))
        self.say("pitch", "dialogue", "pitches", status=r.get("status"),
                 uncertain=[n["id"] for n in r["notes"] if not n.get("pitch_certain", True)])
        return {n["id"]: (n["letter"], n["octave"], n["effective_alteration"]) for n in r["notes"]}


def durations_of(variant):
    """Durations of each symbol in one interpretation (for scoring only)."""
    out = {}
    for desc, sids in zip(variant["events"], variant["symbols"]):
        base = desc.split("#")[0]
        kind, den = base.split("@")
        den = int(den)
        if kind == "measure_rest":
            out[sids[0]] = None
            continue
        if kind == "triplet":
            for sid in sids:
                out[sid] = Fraction(2, 3 * den)
            continue
        d = Fraction(1, den) * (Fraction(3, 2) if kind.startswith("dotted") else 1)
        out[sids[0]] = d
    return out

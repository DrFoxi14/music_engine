"""Adapter: Vision observations -> rhythm_search_engine constraints, and back.

The rhythm engine knows descriptors (note@8, dotted_rest@4, triplet@8#NRN ...), not pixels. The
adapter turns what Vision saw into, for each event of the measure, the set of descriptors that agree
with every visual property that is *certain*; an unknown or unsure property allows all its values.
Each allowed descriptor keeps the visual properties it implies for every symbol it covers, so a
variant returned by the search can be compared with the others property by property.

A property counts as certain when its value is known and its confidence is at least CERTAIN, or when
the dialogue has confirmed it. Unknown is not absent: an unknown dot count allows 0 and 1.
"""
from __future__ import annotations

import itertools

CERTAIN = 0.85
MAX_COUNT = 4                         # beams / flags: up to 1/64
SHAPE_DEN = {"whole": 1, "half": 2, "quarter": 4, "eighth": 8, "sixteenth": 16, "thirty_second": 32,
             "sixty_fourth": 64}
DEN_SHAPE = {v: k for k, v in SHAPE_DEN.items()}


def certain(value, confidence, confirmed=False):
    return value is not None and value != "unknown" and (confirmed or confidence >= CERTAIN)


class Symbol:
    """One observed symbol with its properties as (value, confidence, confirmed)."""

    def __init__(self, raw: dict, beams: dict, tuplets: dict):
        self.id, self.type, self.x = raw["id"], raw["type"], raw["center"]["x"]
        self.raw = raw
        self.props = {}
        if self.type == "rest":
            self.props["rest.shape"] = [raw["rest"]["shape"]["value"], raw["rest"]["shape"]["confidence"], False]
            self.props["dots.count"] = [raw["dots"]["count"]["value"], raw["dots"]["count"]["confidence"], False]
        else:
            self.props["head.fill"] = [raw["head"]["fill"], raw["head"]["confidence"], False]
            self.props["stem.state"] = [raw["stem"]["state"], raw["stem"]["confidence"], False]
            self.props["dots.count"] = [raw["dots"]["count"], raw["dots"]["confidence"], False]
            b = beams.get(self.id)
            if b is not None:
                self.props["beam_groups.beam_count"] = [b["beam_count"], b["confidence"], False]
            else:
                self.props["flags.count"] = [raw["flags"]["count"], raw["flags"]["confidence"], False]
        t = tuplets.get(self.id)
        self.tuplet = t                                   # (tuplet id, number, confidence) or None

    def allowed(self, prop, domain):
        v, c, ok = self.props[prop]
        return [v] if certain(v, c, ok) else list(domain)

    @property
    def beamed(self):
        return "beam_groups.beam_count" in self.props

    def readings(self, tuplet_slot=False):
        """{descriptor kind@den (dotted_ prefix): implied properties} for this symbol alone."""
        out = {}
        dots = [0] if tuplet_slot else self.allowed("dots.count", [0, 1])
        if self.type == "rest":
            for shape in self.allowed("rest.shape", list(SHAPE_DEN)):
                for d in dots:
                    out[("dotted_" if d else "") + f"rest@{SHAPE_DEN[shape]}"] = {"rest.shape": shape, "dots.count": d}
            return out
        count_prop = "beam_groups.beam_count" if self.beamed else "flags.count"
        counts = self.allowed(count_prop, range(1 if self.beamed else 0, MAX_COUNT + 1))
        for fill, stem, n, d in itertools.product(self.allowed("head.fill", ["filled", "hollow"]),
                                                  self.allowed("stem.state", ["present", "absent"]), counts, dots):
            if fill == "hollow":
                if n:
                    continue
                den = 2 if stem == "present" else 1
            else:
                if stem != "present":
                    continue
                den = 4 * 2 ** n
            out[("dotted_" if d else "") + f"note@{den}"] = {"head.fill": fill, "stem.state": stem, count_prop: n,
                                                              "dots.count": d}
        return out


def parse(measure: dict) -> list[Symbol]:
    beams = {}
    for b in measure.get("beam_groups", []):
        for st in b["stems"]:
            beams[st["symbol_id"]] = st
    tuplets = {}
    for t in measure.get("tuplets", []):
        for sid in t["symbol_ids"]:
            tuplets[sid] = (t["id"], t["number"], t["confidence"])
    return sorted([Symbol(s, beams, tuplets) for s in measure["symbols"]], key=lambda s: s.x)


def events(symbols: list[Symbol], tuplet_override=None) -> list[dict] | None:
    """The events of one structure: [{"symbols": [ids], "alternatives": {descriptor: {sid: props}}}].
    Symbols marked by one certain 3-tuplet form one triplet event. None if no event is possible."""
    out, i = [], 0
    tuplet_of = {s.id: s.tuplet for s in symbols}
    if tuplet_override is not None:
        tuplet_of = tuplet_override
    while i < len(symbols):
        s = symbols[i]
        t = tuplet_of.get(s.id)
        if t is not None and t[1] == 3:
            group = [x for x in symbols[i:i + 3] if tuplet_of.get(x.id) and tuplet_of[x.id][0] == t[0]]
            if len(group) < 3:            # the mark spans three symbols and fewer are seen: one is missing,
                return None               # which only a re-examination of the image may settle
            if len(group) == 3:
                per = [g.readings(tuplet_slot=True) for g in group]
                mask = "".join("R" if g.type == "rest" else "N" for g in group)
                alts = {}
                for den in (1, 2, 4, 8, 16, 32, 64):
                    picks = [next(((k, v) for k, v in r.items() if k.endswith(f"@{den}")), None) for r in per]
                    if all(picks):
                        alts[f"triplet@{den}#{mask}"] = {g.id: dict(p[1]) for g, p in zip(group, picks)}
                if not alts:
                    return None
                out.append({"symbols": [g.id for g in group], "alternatives": alts})
                i += 3
                continue
        alts = {k: {s.id: v} for k, v in s.readings().items()}
        if not alts:
            return None
        out.append({"symbols": [s.id], "alternatives": alts})
        i += 1
    if len(out) == 1 and symbols[0].type == "rest":
        r = symbols[0]
        if "whole" in r.allowed("rest.shape", list(SHAPE_DEN)) and 0 in r.allowed("dots.count", [0, 1]):
            out[0]["alternatives"]["measure_rest@0"] = {r.id: {"rest.shape": "whole", "dots.count": 0}}
    return out


def structures(symbols: list[Symbol]) -> list[list[dict]]:
    """One structure per reading of the tuplet marks: a mark whose number or confidence is unsure is
    tried both as a triplet and as no tuplet."""
    marks = {}
    for s in symbols:
        if s.tuplet:
            marks.setdefault(s.tuplet[0], s.tuplet)
    unsure = [k for k, (tid, num, c) in marks.items() if not certain(num, c)]
    out = []
    for keep in itertools.product([True, False], repeat=len(unsure)):
        drop = {k for k, kept in zip(unsure, keep) if not kept}
        override = {s.id: (None if s.tuplet and s.tuplet[0] in drop else
                           ((s.tuplet[0], 3, 1.0) if s.tuplet and s.tuplet[0] in unsure else s.tuplet))
                    for s in symbols}
        ev = events(symbols, override)
        if ev is not None:
            out.append(ev)
    return out


def incomplete_tuplets(symbols: list[Symbol]) -> list[list[Symbol]]:
    """Certain triplet marks over fewer than three observed symbols."""
    groups = {}
    for s in symbols:
        if s.tuplet and certain(s.tuplet[1], s.tuplet[2]) and s.tuplet[1] == 3:
            groups.setdefault(s.tuplet[0], []).append(s)
    return [g for g in groups.values() if len(g) < 3]


def search_args(evts: list[dict], symbols: list[Symbol]):
    category = "RESTS" if any(s.type == "rest" for s in symbols) else "NOTES"
    return category, [sorted(e["alternatives"]) for e in evts]


def key_of(pattern_event: dict) -> str:
    """The adapter's descriptor for one event of a search result (tuplets carry their slot mask)."""
    d = pattern_event["descriptor"]
    if pattern_event.get("tuplet_slots"):
        d += "#" + "".join("N" if x == "note" else "R" for x in pattern_event["tuplet_slots"])
    return d

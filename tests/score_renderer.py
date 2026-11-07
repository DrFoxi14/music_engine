"""Render rhythm-space notation into the primitives a Visual Engine would report.

Used by the end-to-end tests: every rhythm from Search Engine's complete space
is drawn as stems, noteheads, flags or beams (with partial beams), dots, rests,
tuplet brackets and occasional chords, at varied pitches and stem directions.
The truth (sounding duration, head fill, dots per event) is returned alongside,
so a test can check that STEM-first + solver read the bar back exactly.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from fractions import Fraction

S = 10.0          # staff spacing
STEP_X = 40.0     # horizontal distance between events
BEAM_GAP = 7.5    # distance between stacked beams


@dataclass
class Node:
    kind: str                      # note | rest | measure_rest | tuplet
    value: str = ""
    dots: int = 0
    actual: int = 0
    normal: int = 0
    children: list["Node"] = field(default_factory=list)


def parse(text: str) -> list[Node]:
    tokens = text.replace("[", " [ ").replace("]", " ] ").split()
    pos = 0

    def item() -> Node:
        nonlocal pos
        tok = tokens[pos]
        pos += 1
        if tok == "[":
            a, n = (int(x) for x in tokens[pos].split(":"))
            pos += 1
            node = Node("tuplet", actual=a, normal=n)
            while tokens[pos] != "]":
                node.children.append(item())
            pos += 1
            return node
        if tok == "M":
            return Node("measure_rest")
        dots = len(tok) - len(tok.rstrip("."))
        return Node("note" if tok[0] == "n" else "rest", value=tok[1:len(tok) - dots], dots=dots)

    out = []
    while pos < len(tokens):
        out.append(item())
    return out


def base_duration(value: str) -> Fraction:
    return {"B": Fraction(2), "L": Fraction(4)}.get(value) or Fraction(1, int(value))


def conventional_normals(a: int) -> list[int]:
    if a & (a - 1):
        p = 1
        while p * 2 < a:
            p *= 2
        return [p]
    return [a * 3 // 2] + ([a * 3 // 4] if a >= 4 else [])


@dataclass
class Leaf:
    node: Node
    duration: Fraction
    tuplets: list[str]
    parent: int   # id of the containing sequence, for beaming


def flatten(nodes, meter: Fraction):
    leaves: list[Leaf] = []
    tuplets: list[dict] = []
    counter = [0]

    def walk(seq, scale, path, parent):
        for node in seq:
            if node.kind == "tuplet":
                tid = f"t{len(tuplets)}"
                record = {"id": tid, "number": node.actual, "members": []}
                if conventional_normals(node.actual) != [node.normal]:
                    record["normal"] = node.normal
                tuplets.append(record)
                counter[0] += 1
                walk(node.children, scale * Fraction(node.normal, node.actual), path + [tid], counter[0])
            else:
                if node.kind == "measure_rest":
                    d = meter
                else:
                    d = base_duration(node.value) * (2 - Fraction(1, 2 ** node.dots)) * scale
                leaves.append(Leaf(node, d, path, parent))

    walk(nodes, Fraction(1), [], 0)
    return leaves, tuplets


def render(notation: str, meter: tuple[int, int], seed: int = 0, beams: bool = True, chords: bool = True):
    """Return (elements for StemFirstClient.analyze, truth list in time order)."""
    rng = random.Random(seed)
    nodes = parse(notation)
    leaves, tuplet_records = flatten(nodes, Fraction(*meter))
    stems, heads, flags, beam_list, dots, rests = [], [], [], [], [], []
    truth = []
    by_id = {t["id"]: t for t in tuplet_records}

    # Note geometry first, so beamed groups can share a direction and beam line.
    info = []
    for i, leaf in enumerate(leaves):
        x = 100 + STEP_X * i
        node = leaf.node
        entry = {"leaf": leaf, "x": x}
        if node.kind == "note":
            y = 100 + 5 * rng.randint(-2, 10)          # ledger lines included
            k = max(0, int(node.value).bit_length() - 3) if node.value not in ("B", "L") else 0
            entry.update(y=y, k=k, up=y >= 120, stemmed=node.value not in ("1", "B", "L"))
        info.append(entry)

    # Beam groups: consecutive flagged notes in the same sequence.
    groups, current = [], []
    for i, e in enumerate(info):
        flagged = e["leaf"].node.kind == "note" and e.get("k", 0) >= 1
        if flagged and current and info[current[-1]]["leaf"].parent == e["leaf"].parent and current[-1] == i - 1:
            current.append(i)
        else:
            if current:
                groups.append(current)
            current = [i] if flagged else []
    if current:
        groups.append(current)
    beamed = {}
    for g in groups:
        if beams and len(g) >= 2 and rng.random() < 0.8:
            for i in g:
                beamed[i] = g
            up = info[g[0]]["y"] >= 120
            for i in g:
                info[i]["up"] = up

    def stem_end(i):
        e = info[i]
        if i in beamed:
            g = beamed[i]
            kmax = max(info[j]["k"] for j in g)
            if e["up"]:
                return min(info[j]["y"] for j in g) - 35 - BEAM_GAP * (kmax - 1)
            return max(info[j]["y"] for j in g) + 35 + BEAM_GAP * (kmax - 1)
        length = 35 + BEAM_GAP * max(0, e.get("k", 0) - 1)
        return e["y"] - length if e["up"] else e["y"] + length

    for i, e in enumerate(info):
        leaf, node, x = e["leaf"], e["leaf"].node, e["x"]
        if node.kind in ("rest", "measure_rest"):
            rid = f"r{i}"
            rests.append({"id": rid, "bbox": {"left": x - 5, "top": 105, "right": x + 5, "bottom": 130},
                          "value": "measure" if node.kind == "measure_rest" else node.value, "dots": node.dots})
            event_id, members, fill = rid, [rid], "rest"
        else:
            y = e["y"]
            hid = f"h{i}"
            fill = "hollow" if node.value in ("1", "2", "B", "L") else "filled"
            heads.append({"id": hid, "bbox": {"left": x - 6, "top": y - 4.5, "right": x + 6, "bottom": y + 4.5},
                          "fill": fill, "fill_confidence": 1.0})
            for d in range(node.dots):
                dots.append({"id": f"d{i}_{d}", "x": x + 12 + 6 * d, "y": y - 2})
            members = [hid]
            event_id = hid
            if e["stemmed"]:
                sid = f"s{i}"
                sx = x + 6 if e["up"] else x - 6
                end_y = stem_end(i)
                stems.append({"id": sid, "x1": sx, "y1": y, "x2": sx, "y2": end_y})
                event_id = sid
                members.append(sid)
                if chords and rng.random() < 0.15:
                    cy = y - 10 if e["up"] else y + 10
                    heads.append({"id": f"c{i}", "bbox": {"left": x - 6, "top": cy - 4.5, "right": x + 6,
                                                          "bottom": cy + 4.5}, "fill": fill, "fill_confidence": 1.0})
                if e["k"] >= 1 and i not in beamed:
                    box = ({"left": sx, "top": end_y, "right": sx + 8, "bottom": end_y + 15} if e["up"]
                           else {"left": sx, "top": end_y - 15, "right": sx + 8, "bottom": end_y})
                    flags.append({"id": f"f{i}", "bbox": box, "count": e["k"]})
        for tid in leaf.tuplets:
            by_id[tid]["members"].append(members[-1])
        truth.append({"id": event_id, "duration": leaf.duration, "fill": fill, "dots": node.dots})

    # Beams: level j spans maximal runs with k >= j; single stems get a stub.
    for g in groups:
        if g[0] not in beamed:
            continue
        up = info[g[0]]["up"]
        end_y = stem_end(g[0])
        sx = {i: info[i]["x"] + (6 if up else -6) for i in g}
        kmax = max(info[i]["k"] for i in g)
        for level in range(1, kmax + 1):
            y = end_y + (BEAM_GAP * (level - 1) if up else -BEAM_GAP * (level - 1))
            run = []
            for pos, i in enumerate(g + [None]):
                if i is not None and info[i]["k"] >= level:
                    run.append(i)
                    continue
                if len(run) >= 2:
                    beam_list.append({"id": f"b{run[0]}_{level}", "x1": sx[run[0]], "y1": y,
                                      "x2": sx[run[-1]], "y2": y, "thickness": 4})
                elif len(run) == 1:
                    j = run[0]
                    towards = 10 if g.index(j) < len(g) - 1 else -10
                    beam_list.append({"id": f"b{j}_{level}", "x1": sx[j], "y1": y,
                                      "x2": sx[j] + towards, "y2": y, "thickness": 4})
                run = []

    elements = {"stems": stems, "noteheads": heads, "flags": flags, "beams": beam_list,
                "dots": dots, "rests": rests, "tuplets": [t for t in tuplet_records if t["members"]]}
    return elements, truth

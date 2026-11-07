"""Python client for the C++ STEM-first engine, plus pipeline helpers.

The Visual Engine detects primitives (stems, noteheads, flags, beams, dots,
rests, tuplet brackets). The STEM-first engine decides, per stem, which end is
free (flag/beam) and which carries the notehead, and derives duration
candidates. Those candidates feed the OMR constraint solver; the stem
directions and chord groupings feed the pitch-context engine.

    with StemFirstClient("./stem_first_engine") as stem, \\
         OMRConstraintClient("./omr_constraint_solver") as solver:
        result = read_measure(stem, solver, meter="4/4", spacing=10,
                              stems=[...], noteheads=[...], beams=[...])
"""

from __future__ import annotations

import json
import math
import subprocess
import threading
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


def _num(value: Any, label: str) -> str:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    return format(number, ".10g")


def _id(value: Any, label: str) -> str:
    text = str(value)
    if not text or any(not (c.isalnum() or c in "_-") for c in text):
        raise ValueError(f"{label} may contain only letters, digits, _ and -")
    return text


def _box(item: Mapping[str, Any], label: str) -> list[str]:
    box = item["bbox"]
    return [_num(box["left"], f"{label}.left"), _num(box["top"], f"{label}.top"),
            _num(box["right"], f"{label}.right"), _num(box["bottom"], f"{label}.bottom")]


class StemFirstClient:
    """Persistent client for STEM-first analysis of one voice in one measure."""

    def __init__(self, executable: str | Path) -> None:
        self._lock = threading.Lock()
        self._process = subprocess.Popen(
            [str(executable)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=None, text=True, encoding="utf-8", bufsize=1,
        )
        assert self._process.stdout is not None
        line = self._process.stdout.readline()
        if not line:
            self.close()
            raise RuntimeError("STEM-first engine failed to start.")
        self.info = json.loads(line)
        if self.info.get("type") != "ready":
            self.close()
            raise RuntimeError(f"Unexpected STEM-first startup response: {self.info}")

    def analyze(
        self,
        spacing: float,
        *,
        stems: Iterable[Mapping[str, Any]] = (),
        noteheads: Iterable[Mapping[str, Any]] = (),
        flags: Iterable[Mapping[str, Any]] = (),
        beams: Iterable[Mapping[str, Any]] = (),
        dots: Iterable[Mapping[str, Any]] = (),
        rests: Iterable[Mapping[str, Any]] = (),
        tuplets: Iterable[Mapping[str, Any]] = (),
        meter: str | None = None,
        unchecked: Iterable[str] = (),
        unchecked_max_dots: int = 1,
    ) -> dict[str, Any]:
        """Analyze one voice of one measure.

        stem:     {"id", "x1", "y1", "x2", "y2"}            (endpoints in any order)
        notehead: {"id", "bbox", "fill": filled|hollow|unknown, "fill_confidence"}
        flag:     {"id", "bbox", "count"}                    (hooks in the glyph)
        beam:     {"id", "x1", "y1", "x2", "y2", "thickness"}
        dot:      {"id", "x", "y", "confidence"}
        rest:     {"id", "bbox", "value": 4 | "B" | "measure", "dots"}
        tuplet:   {"id", "number": 3, "normal": None, "members": [ids]}

        unchecked: visual features not inspected yet ("fill", "dots"); the
        engine then lists every value they could take.
        """
        tokens = ["ANALYZE", f"spacing={_num(spacing, 'spacing')}"]
        if meter is not None:
            num, den = (int(p) for p in str(meter).split("/"))
            tokens.append(f"meter={num}/{den}")
        unchecked = [str(u) for u in unchecked]
        if unchecked:
            if any(u not in {"fill", "dots"} for u in unchecked):
                raise ValueError("unchecked accepts 'fill' and 'dots'")
            tokens.append("unchecked=" + ",".join(unchecked))
            tokens.append(f"unchecked_max_dots={int(unchecked_max_dots)}")
        for s in stems:
            tokens.append("stem=" + ",".join([_id(s["id"], "stem id"), _num(s["x1"], "x1"), _num(s["y1"], "y1"),
                                               _num(s["x2"], "x2"), _num(s["y2"], "y2")]))
        for n in noteheads:
            fill = str(n.get("fill", "unknown"))
            tokens.append("notehead=" + ",".join([_id(n["id"], "notehead id")] + _box(n, "notehead") +
                                                  [fill, _num(n.get("fill_confidence", 1.0), "fill_confidence")]))
        for f in flags:
            tokens.append("flag=" + ",".join([_id(f["id"], "flag id")] + _box(f, "flag") + [str(int(f.get("count", 1)))]))
        for b in beams:
            thickness = b.get("thickness")
            tokens.append("beam=" + ",".join([_id(b["id"], "beam id"), _num(b["x1"], "x1"), _num(b["y1"], "y1"),
                                               _num(b["x2"], "x2"), _num(b["y2"], "y2"),
                                               "-" if thickness is None else _num(thickness, "thickness")]))
        for d in dots:
            tokens.append("dot=" + ",".join([_id(d["id"], "dot id"), _num(d["x"], "x"), _num(d["y"], "y"),
                                              _num(d.get("confidence", 1.0), "confidence")]))
        for r in rests:
            value = str(r["value"])
            if value not in {"B", "measure"} and not value.isdigit():
                raise ValueError("rest value must be a power of two, 'B' or 'measure'")
            tokens.append("rest=" + ",".join([_id(r["id"], "rest id")] + _box(r, "rest") +
                                              [value, str(int(r.get("dots", 0)))]))
        for t in tuplets:
            label = str(int(t["number"]))
            if t.get("normal") is not None:
                label += f":{int(t['normal'])}"
            members = ";".join(_id(m, "tuplet member") for m in t["members"])
            tokens.append("tuplet=" + ",".join([_id(t["id"], "tuplet id"), label, members]))

        with self._lock:
            if self._process.poll() is not None:
                raise RuntimeError("STEM-first engine has stopped.")
            assert self._process.stdin is not None and self._process.stdout is not None
            self._process.stdin.write(" ".join(tokens) + "\n")
            self._process.stdin.flush()
            line = self._process.stdout.readline()
        if not line:
            raise RuntimeError("STEM-first engine closed its output.")
        result = json.loads(line)
        if result.get("type") == "error":
            raise ValueError(result.get("message", "STEM-first engine error."))
        return result

    def plan(self, spacing: float, *, stems: Iterable[Mapping[str, Any]],
             flags: Iterable[Mapping[str, Any]] = (), beams: Iterable[Mapping[str, Any]] = ()) -> dict[str, Any]:
        """STEM-first search plan from stems, flags and beams only.

        For each stem: which end is free, the direction, the duration the stem
        already implies, and the regions where the Visual Engine should look
        for the notehead (one, or one per end in the quarter-note branch), for
        chord members and for augmentation dots.
        """
        tokens = ["PLAN", f"spacing={_num(spacing, 'spacing')}"]
        for s in stems:
            tokens.append("stem=" + ",".join([_id(s["id"], "stem id"), _num(s["x1"], "x1"), _num(s["y1"], "y1"),
                                               _num(s["x2"], "x2"), _num(s["y2"], "y2")]))
        for f in flags:
            tokens.append("flag=" + ",".join([_id(f["id"], "flag id")] + _box(f, "flag") + [str(int(f.get("count", 1)))]))
        for b in beams:
            thickness = b.get("thickness")
            tokens.append("beam=" + ",".join([_id(b["id"], "beam id"), _num(b["x1"], "x1"), _num(b["y1"], "y1"),
                                               _num(b["x2"], "x2"), _num(b["y2"], "y2"),
                                               "-" if thickness is None else _num(thickness, "thickness")]))
        with self._lock:
            if self._process.poll() is not None:
                raise RuntimeError("STEM-first engine has stopped.")
            assert self._process.stdin is not None and self._process.stdout is not None
            self._process.stdin.write(" ".join(tokens) + "\n")
            self._process.stdin.flush()
            line = self._process.stdout.readline()
        if not line:
            raise RuntimeError("STEM-first engine closed its output.")
        result = json.loads(line)
        if result.get("type") == "error":
            raise ValueError(result.get("message", "STEM-first engine error."))
        return result

    def close(self) -> None:
        process = getattr(self, "_process", None)
        if process is None:
            return
        try:
            if process.poll() is None and process.stdin is not None:
                process.stdin.write("EXIT\n")
                process.stdin.flush()
                process.wait(timeout=3)
        except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
            process.terminate()
        finally:
            if process.poll() is None:
                process.kill()
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass

    def __enter__(self) -> "StemFirstClient":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


def solver_events(analysis: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Ordered events in OMRConstraintClient.resolve() format."""
    events = []
    for event in analysis["events"]:
        candidates = [
            {"duration": c["duration"], "confidence": max(min(float(c["confidence"]), 1.0), 1e-9)}
            for c in event["candidates"]
        ]
        if candidates:
            events.append({"id": event["id"], "candidates": candidates})
    return events


def pitch_inputs(
    analysis: Mapping[str, Any], noteheads: Sequence[Mapping[str, Any]], stems: Sequence[Mapping[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(notes, stems) in PitchContextClient.resolve() format.

    Every notehead assigned to an event becomes a note grouped by the event
    (chord_id); every resolved stem carries the direction found by STEM-first.
    """
    heads = {n["id"]: n for n in noteheads}
    stem_geometry = {s["id"]: s for s in stems}
    notes, pitch_stems = [], []
    for event in analysis["events"]:
        if event["kind"] == "rest":
            continue
        for head_id in event["noteheads"]:
            box = heads[head_id]["bbox"]
            notes.append({
                "id": head_id,
                "x": (box["left"] + box["right"]) / 2,
                "y": (box["top"] + box["bottom"]) / 2,
                "bbox": dict(box),
                "stem_id": event["stem_id"],
                "chord_id": event["id"],
            })
        if event["stem_id"] and event["direction"]:
            s = stem_geometry[event["stem_id"]]
            pitch_stems.append({
                "id": event["stem_id"],
                "chord_id": event["id"],
                "x": (s["x1"] + s["x2"]) / 2,
                "y_top": min(s["y1"], s["y2"]),
                "y_bottom": max(s["y1"], s["y2"]),
                "direction": event["direction"],
            })
    return notes, pitch_stems


def read_measure(stem_client: StemFirstClient, solver_client: Any, *, meter: str, spacing: float,
                 max_note_denominator: int = 256, **elements: Any) -> dict[str, Any]:
    """STEM-first analysis followed by exact measure resolution.

    Returns {"analysis", "rhythm", "events"} where each event lists the
    durations that survive the measure constraint.
    """
    analysis = stem_client.analyze(spacing, meter=meter, **elements)
    events = solver_events(analysis)
    if not events:
        return {"analysis": analysis, "rhythm": None, "events": []}
    rhythm = solver_client.resolve(meter, events, max_note_denominator=max_note_denominator)
    support = {item["event_id"]: item for item in rhythm["event_support"]}
    combined = []
    for event in analysis["events"]:
        item = support.get(event["id"])
        if item is None:
            continue
        combined.append({
            "id": event["id"],
            "kind": event["kind"],
            "noteheads": event["noteheads"],
            "direction": event["direction"],
            "possible_durations": item["supported_candidates"],
            "eliminated_durations": item["eliminated_candidates"],
            "duration": item["supported_candidates"][0] if len(item["supported_candidates"]) == 1 else None,
        })
    return {"analysis": analysis, "rhythm": rhythm, "events": combined}


# ---------------------------------------------------------------------------
# Optimal inference: deduce everything the bar implies, ask only the rest.
# ---------------------------------------------------------------------------

FEATURES = ("fill", "dots")
EXACT_PLAN_LIMIT = 100  # solver's maximum solution page


def _feature_map(analysis: Mapping[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    """event id -> duration -> candidate (with the fill/dots it implies)."""
    return {e["id"]: {c["duration"]: c for c in e["candidates"]} for e in analysis["events"]}


def _restrict(analysis: Mapping[str, Any], known: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Solver events with candidates filtered by what the Visual Engine confirmed."""
    events = solver_events(analysis)
    features = _feature_map(analysis)
    for event in events:
        facts = known.get(event["id"], {})
        if not facts:
            continue
        keep = []
        for cand in event["candidates"]:
            info = features[event["id"]][cand["duration"]]
            if "duration" in facts and cand["duration"] != facts["duration"]:
                continue
            if any(f in facts and info[f] not in ("any", facts[f]) for f in FEATURES):
                continue
            keep.append(cand)
        if not keep:
            raise ValueError(f"visual answer for {event['id']} contradicts every candidate")
        event["candidates"] = keep
    return events


def plan_checks(analysis: Mapping[str, Any], rhythm: Mapping[str, Any]) -> dict[str, Any]:
    """Which events the bar already implies, and which visual check helps most.

    Each check asks one feature (head fill, or dot count) of one event. Its
    worst case is the largest number of valid readings that could remain.
    """
    features = _feature_map(analysis)
    # Exact counts while they fit in 64 bits; beyond that the solver's log2
    # counts (which never saturate) give the shares of each outcome.
    saturated = bool(rhythm.get("valid_assignments_is_lower_bound"))
    total_log2 = rhythm.get("valid_assignments_log2") or 0.0
    total = int(rhythm["valid_assignments"])
    implied, checks = [], []
    for support in rhythm["event_support"]:
        event_id = support["event_id"]
        if support["determined"]:
            implied.append(event_id)
            continue
        if saturated:
            logs = support["solution_counts_log2"]
            shares = {d: 2.0 ** (lg - total_log2) for d, lg in zip(support["input_candidates"], logs) if lg is not None}
        else:
            shares = {d: int(n) / total for d, n in zip(support["input_candidates"], support["solution_counts"])
                      if int(n) > 0}
        for feature in FEATURES:
            groups: dict[Any, float] = {}
            for duration, share in shares.items():
                value = features[event_id][duration][feature]
                if value == "any":
                    groups = {}
                    break
                groups[value] = groups.get(value, 0.0) + share
            if len(groups) < 2:
                continue  # the answer is already known or does not separate readings
            worst = max(groups.values())
            checks.append({
                "event_id": event_id,
                "feature": feature,
                "worst_case_share": worst,
                "worst_case_remaining": round(worst * total) if not saturated else None,
                "worst_case_remaining_log2": total_log2 + math.log2(worst),
                "expected_share": sum(g * g for g in groups.values()),
                "outcomes": groups,
            })
    checks.sort(key=lambda c: (round(c["worst_case_share"], 12), round(c["expected_share"], 12)))
    return {"implied": implied, "checks": checks, "next": checks[0] if checks else None}


class PlanningBudgetExceeded(Exception):
    """The exact planner gave up; callers fall back to the greedy check."""


def optimal_question_count(solutions: Sequence[Sequence[str]], event_ids: Sequence[str],
                           features: Mapping[str, Mapping[str, Mapping[str, Any]]],
                           budget: int = 50_000) -> tuple[int | None, tuple[str, str] | None]:
    """Exact minimax: fewest checks that always identify the reading, and the first check.

    Iterative deepening: "can every reading be told apart within d checks?"
    for d = information bound, d + 1, ... Checks that split the readings the
    same way are merged, and a branch is cut as soon as one of its parts is
    larger than d - 1 more checks could ever separate. Returns (None, None)
    when `budget` subproblems are exceeded.
    """
    solutions = [tuple(s) for s in solutions]
    n = len(solutions)
    if n <= 1:
        return 0, None
    checks: list[tuple[tuple[str, str], list[Any]]] = []
    seen_vectors = set()
    for i, event_id in enumerate(event_ids):
        for feature in FEATURES:
            values = [features[event_id][s[i]][feature] for s in solutions]
            if "any" in values or len(set(values)) < 2:
                continue
            labels: dict[Any, int] = {}
            canonical = tuple(labels.setdefault(v, len(labels)) for v in values)
            if canonical in seen_vectors:
                continue
            seen_vectors.add(canonical)
            checks.append(((event_id, feature), list(canonical)))
    if not checks:
        return None, None
    branching = max(max(v) + 1 for _, v in checks)

    def bound(size: int) -> int:
        k, reach = 0, 1
        while reach < size:
            reach *= branching
            k += 1
        return k

    memo: dict[tuple[frozenset[int], int], bool] = {}
    calls = [0]

    def split(subset: frozenset[int], values: list[int]) -> list[frozenset[int]]:
        parts: dict[int, set[int]] = {}
        for s in subset:
            parts.setdefault(values[s], set()).add(s)
        return [frozenset(p) for p in parts.values()]

    def feasible(subset: frozenset[int], depth: int) -> tuple[str, str] | bool:
        if len(subset) <= 1:
            return True
        if depth == 0 or bound(len(subset)) > depth:
            return False
        key = (subset, depth)
        if key in memo:
            return memo[key]
        calls[0] += 1
        if calls[0] > budget:
            raise PlanningBudgetExceeded
        options = []
        for check, values in checks:
            parts = split(subset, values)
            if len(parts) < 2:
                continue
            largest = max(len(p) for p in parts)
            if bound(largest) > depth - 1:
                continue
            options.append((largest, check, parts))
        options.sort(key=lambda o: o[0])
        result: tuple[str, str] | bool = False
        for _, check, parts in options:
            if all(feasible(p, depth - 1) is not False for p in sorted(parts, key=len, reverse=True)):
                result = check
                break
        memo[key] = result
        return result

    try:
        depth = bound(n)
        while depth <= n:
            first = feasible(frozenset(range(n)), depth)
            if first is not False:
                return depth, first if isinstance(first, tuple) else None
            depth += 1
    except PlanningBudgetExceeded:
        return None, None
    return None, None


def infer_measure(stem_client: StemFirstClient, solver_client: Any, *, meter: str, spacing: float,
                  answer: Any = None, known: Mapping[str, Mapping[str, Any]] | None = None,
                  unchecked: Iterable[str] = ("fill", "dots"), unchecked_max_dots: int = 2,
                  max_note_denominator: int = 256, max_nodes: int = 10_000_000,
                  **elements: Any) -> dict[str, Any]:
    """Read one voice of one measure asking the Visual Engine as little as possible.

    1. STEM-first analysis from structure only (fill and dots unchecked).
    2. The measure constraint fixes every event it can ("implied").
    3. While readings remain, ask the single most informative check:
       answer(event_id, feature) -> value ("filled"/"hollow" or a dot count).
       With <= 100 readings the question order is exactly optimal (minimax).
    If the bar is too large for the solver's state budget with features
    unchecked, one whole-bar detector pass is requested instead (dots, then
    fill: the supplied dot primitives / head fills are then used) and
    reported in "batch_checks".
    `known` holds facts the Visual Engine volunteers up front, per event:
    {"fill": ..., "dots": ..., "duration": ...}.

    Deductions are exact relative to the unchecked range: with
    unchecked_max_dots=2 a triple-dotted note could be mistaken for another
    reading that fills the bar. Raise it when such notes are expected.
    """
    unchecked = tuple(unchecked)
    analysis = stem_client.analyze(spacing, meter=meter, unchecked=unchecked,
                                   unchecked_max_dots=unchecked_max_dots, **elements)
    facts: dict[str, dict[str, Any]] = {k: dict(v) for k, v in (known or {}).items()}
    features = _feature_map(analysis)
    questions: list[dict[str, Any]] = []
    first_implied: list[str] | None = None
    batch_checks: list[str] = []
    while True:
        events = _restrict(analysis, facts)
        rhythm = solver_client.resolve(meter, events, max_note_denominator=max_note_denominator,
                                       limit=EXACT_PLAN_LIMIT, max_nodes=max_nodes)
        if rhythm["status"] == "incomplete" and unchecked:
            # Too many readings for the state budget (e.g. hundreds of 1/256
            # notes with nothing checked). Ask the Visual Engine to run one
            # detector over the whole bar - dots first, then head fill - and
            # use the primitives it reports, instead of per-note questions.
            feature = "dots" if "dots" in unchecked else unchecked[0]
            unchecked = tuple(u for u in unchecked if u != feature)
            batch_checks.append(feature)
            analysis = stem_client.analyze(spacing, meter=meter, unchecked=unchecked,
                                           unchecked_max_dots=unchecked_max_dots, **elements)
            features = _feature_map(analysis)
            first_implied = None
            continue
        plan = plan_checks(analysis, rhythm)
        if first_implied is None:
            first_implied = plan["implied"]
        if rhythm["status"] != "ambiguous" or answer is None:
            break
        chosen = plan["next"]
        if chosen is None:
            break  # remaining readings differ in nothing the Visual Engine can check
        pick = (chosen["event_id"], chosen["feature"])
        if int(rhythm["valid_assignments"]) <= EXACT_PLAN_LIMIT:
            ids = [e["id"] for e in events]
            sols = [[ev["duration_whole"] for ev in s["events"]] for s in rhythm["solutions"]]
            _, exact = optimal_question_count(sols, ids, features)
            if exact is not None:
                pick = exact
        value = answer(*pick)
        questions.append({"event_id": pick[0], "feature": pick[1], "answer": value,
                          "readings_before": int(rhythm["valid_assignments"]),
                          "readings_before_log2": rhythm.get("valid_assignments_log2")})
        facts.setdefault(pick[0], {})[pick[1]] = value
        if pick[1] == "dots" and "dots" in unchecked and int(value) > unchecked_max_dots:
            # More dots than assumed: widen the unchecked range instead of failing.
            unchecked_max_dots = int(value)
            analysis = stem_client.analyze(spacing, meter=meter, unchecked=unchecked,
                                           unchecked_max_dots=unchecked_max_dots, **elements)
            features = _feature_map(analysis)

    durations = None
    if rhythm["status"] == "resolved_unique":
        durations = {e["event_id"]: e["duration_whole"] for e in rhythm["solutions"][0]["events"]}
    return {
        "analysis": analysis,
        "rhythm": rhythm,
        "status": rhythm["status"],
        "durations": durations,
        "implied_without_questions": first_implied or [],
        "questions": questions,
        "batch_checks": batch_checks,
    }


# ---------------------------------------------------------------------------
# STEM-first order end to end: stems -> search plan -> heads only where the
# plan says -> analysis -> exact measure resolution.
# ---------------------------------------------------------------------------

def _inside(box: Mapping[str, float], x: float, y: float) -> bool:
    return box["left"] <= x <= box["right"] and box["top"] <= y <= box["bottom"]


def regions_of(plan: Mapping[str, Any], *purposes: str) -> list[dict[str, Any]]:
    """Every planned region with the given purposes, tagged with its stem id."""
    out = []
    for stem in plan["stems"]:
        for region in stem["regions"]:
            if not purposes or region["purpose"] in purposes:
                out.append({**region, "stem_id": stem["id"]})
    return out


def in_regions(items: Iterable[Mapping[str, Any]], regions: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Items (noteheads with a bbox, or dots with x/y) whose center lies inside a region."""
    found = []
    for item in items:
        if "bbox" in item:
            b = item["bbox"]
            x, y = (b["left"] + b["right"]) / 2, (b["top"] + b["bottom"]) / 2
        else:
            x, y = item["x"], item["y"]
        if any(_inside(r["box"], x, y) for r in regions):
            found.append(dict(item))
    return found


def read_measure_stem_first(stem_client: StemFirstClient, solver_client: Any, *, meter: str, spacing: float,
                            stems: Sequence[Mapping[str, Any]], detect_heads: Any, detect_dots: Any = None,
                            flags: Sequence[Mapping[str, Any]] = (), beams: Sequence[Mapping[str, Any]] = (),
                            stemless_heads: Sequence[Mapping[str, Any]] = (),
                            rests: Sequence[Mapping[str, Any]] = (), tuplets: Sequence[Mapping[str, Any]] = (),
                            max_note_denominator: int = 256) -> dict[str, Any]:
    """Read one voice of one measure in STEM-first order.

    detect_heads(regions) -> noteheads found inside the notehead/chord regions
    detect_dots(regions)  -> dots found inside the dot regions (optional)
    Stemless heads (whole notes) and rests come from their own modules.
    """
    plan = stem_client.plan(spacing, stems=stems, flags=flags, beams=beams)
    head_regions = regions_of(plan, "notehead", "chord_noteheads")
    heads = list(detect_heads(head_regions)) + [dict(h) for h in stemless_heads]
    dots = list(detect_dots(regions_of(plan, "augmentation_dots"))) if detect_dots else []
    measure = read_measure(stem_client, solver_client, meter=meter, spacing=spacing,
                           max_note_denominator=max_note_denominator, stems=stems, noteheads=heads,
                           flags=flags, beams=beams, dots=dots, rests=rests, tuplets=tuplets)
    measure["plan"] = plan
    measure["searched_regions"] = len(head_regions)
    return measure

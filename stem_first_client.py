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
    ) -> dict[str, Any]:
        """Analyze one voice of one measure.

        stem:     {"id", "x1", "y1", "x2", "y2"}            (endpoints in any order)
        notehead: {"id", "bbox", "fill": filled|hollow|unknown, "fill_confidence"}
        flag:     {"id", "bbox", "count"}                    (hooks in the glyph)
        beam:     {"id", "x1", "y1", "x2", "y2", "thickness"}
        dot:      {"id", "x", "y", "confidence"}
        rest:     {"id", "bbox", "value": 4 | "B" | "measure", "dots"}
        tuplet:   {"id", "number": 3, "normal": None, "members": [ids]}
        """
        tokens = ["ANALYZE", f"spacing={_num(spacing, 'spacing')}"]
        if meter is not None:
            num, den = (int(p) for p in str(meter).split("/"))
            tokens.append(f"meter={num}/{den}")
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

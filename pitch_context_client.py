"""Python client for the C++ pitch-context engine.

The Visual Engine supplies staff geometry, notehead centers/bounds, accidental
glyph bounds, and stem geometry. The C++ engine maps Y positions to pitches,
applies key signatures and measure-local accidental memory, and returns
ambiguous symbol associations for visual reinspection.
"""

from __future__ import annotations

import json
import math
import subprocess
import threading
from pathlib import Path
from typing import Any, Mapping, Sequence


class PitchContextClient:
    """Persistent client for pitch mapping, accidental scope, and stem checks."""

    def __init__(self, executable: str | Path) -> None:
        self._lock = threading.Lock()
        self._process = subprocess.Popen(
            [str(executable)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        assert self._process.stdin is not None
        assert self._process.stdout is not None
        first_line = self._process.stdout.readline()
        if not first_line:
            self.close()
            raise RuntimeError("Pitch context engine failed to start.")
        self.info = json.loads(first_line)
        if self.info.get("type") != "ready":
            self.close()
            raise RuntimeError(f"Unexpected pitch engine startup response: {self.info}")

    @staticmethod
    def _number(value: Any, label: str) -> str:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"{label} must be finite")
        return format(number, ".10g")

    @staticmethod
    def _safe_id(value: Any, label: str) -> str:
        text = str(value)
        if not text or any(not (c.isalnum() or c in "_-") for c in text):
            raise ValueError(f"{label} may contain only letters, digits, _ and -")
        return text

    @classmethod
    def _bbox_fields(cls, bbox: Mapping[str, Any] | None, *, required: bool) -> list[str]:
        if bbox is None:
            if required:
                raise ValueError("a bounding box is required for accidental glyphs")
            return ["-", "-", "-", "-"]
        try:
            return [
                cls._number(bbox["left"], "bbox.left"),
                cls._number(bbox["right"], "bbox.right"),
                cls._number(bbox["top"], "bbox.top"),
                cls._number(bbox["bottom"], "bbox.bottom"),
            ]
        except KeyError as exc:
            raise ValueError(f"bbox is missing {exc.args[0]}") from exc

    def resolve(
        self,
        *,
        clef: str,
        staff_top_y: float,
        line_spacing: float,
        key_signature: str = "C",
        measure_id: str = "0",
        staff_id: str = "staff0",
        voice_id: str = "voice0",
        origin_x: float = 0.0,
        staff_slope: float = 0.0,
        notes: Sequence[Mapping[str, Any]],
        accidentals: Sequence[Mapping[str, Any]] = (),
        stems: Sequence[Mapping[str, Any]] = (),
        grid_tolerance: float = 0.28,
        association_margin: float = 0.22,
        association_max_cost: float = 0.95,
    ) -> dict[str, Any]:
        """Resolve one staff/voice in one measure.

        Notes:
          {"id":"n1", "x":200, "y":120, "confidence":0.99,
           "bbox":{"left":190,"top":115,"right":210,"bottom":125},
           "stem_id":"s1", "chord_id":"ch1",
           "tie_id":"tie-1", "tie_from_previous":False, "tie_to_next":True}

        Accidentals:
          {"id":"a1", "kind":"sharp",
           "bbox":{"left":170,"top":115,"right":182,"bottom":127},
           "confidence":0.9}

        Stems:
          {"id":"s1", "chord_id":"ch1", "x":210, "y_top":80,
           "y_bottom":120, "direction":"up"}

        One call is one measure: local accidental state resets each call.
        Explicit tie carries persist across calls for the same staff_id/voice_id,
        provided the same client process is kept alive. tie_id identifies the
        continuation; use tie_from_previous on the target and tie_to_next on
        the source. A tie target inherits only its tied pitch, not measure-wide
        accidental state.
        """
        if not notes:
            raise ValueError("at least one note is required")

        tokens = [
            "RESOLVE",
            f"clef={self._safe_id(clef, 'clef')}",
            f"top_y={self._number(staff_top_y, 'staff_top_y')}",
            f"spacing={self._number(line_spacing, 'line_spacing')}",
            f"origin_x={self._number(origin_x, 'origin_x')}",
            f"slope={self._number(staff_slope, 'staff_slope')}",
            f"key={self._safe_id(key_signature, 'key_signature')}",
            f"measure={self._safe_id(measure_id, 'measure_id')}",
            f"staff={self._safe_id(staff_id, 'staff_id')}",
            f"voice={self._safe_id(voice_id, 'voice_id')}",
            f"grid_tolerance={self._number(grid_tolerance, 'grid_tolerance')}",
            f"association_margin={self._number(association_margin, 'association_margin')}",
            f"association_max_cost={self._number(association_max_cost, 'association_max_cost')}",
        ]

        seen_note_ids: set[str] = set()
        for note in notes:
            note_id = self._safe_id(note["id"], "note id")
            if note_id in seen_note_ids:
                raise ValueError(f"duplicate note id: {note_id}")
            seen_note_ids.add(note_id)
            box = self._bbox_fields(note.get("bbox"), required=False)
            stem_id = self._safe_id(note["stem_id"], "stem_id") if note.get("stem_id") else "-"
            chord_id = self._safe_id(note["chord_id"], "chord_id") if note.get("chord_id") else "-"
            tie_from_previous = bool(note.get("tie_from_previous", False))
            tie_to_next = bool(note.get("tie_to_next", False))
            tie_id = self._safe_id(note["tie_id"], "tie_id") if note.get("tie_id") else "-"
            if (tie_from_previous or tie_to_next) and tie_id == "-":
                raise ValueError("tie_id is required when tie_from_previous or tie_to_next is true")
            fields = [
                note_id,
                self._number(note["x"], "note.x"),
                self._number(note["y"], "note.y"),
                self._number(note.get("confidence", 1.0), "note.confidence"),
                box[0], box[1], box[2], box[3], stem_id, chord_id,
                tie_id, "1" if tie_from_previous else "0",
                "1" if tie_to_next else "0",
            ]
            tokens.append("note=" + ",".join(fields))

        seen_accidental_ids: set[str] = set()
        for accidental in accidentals:
            accidental_id = self._safe_id(accidental["id"], "accidental id")
            if accidental_id in seen_accidental_ids:
                raise ValueError(f"duplicate accidental id: {accidental_id}")
            seen_accidental_ids.add(accidental_id)
            kind = self._safe_id(accidental["kind"], "accidental kind")
            box = self._bbox_fields(accidental.get("bbox"), required=True)
            anchor_y = (
                self._number(accidental["anchor_y"], "accidental.anchor_y")
                if accidental.get("anchor_y") is not None else "-"
            )
            fields = [
                accidental_id, kind, box[0], box[2], box[1], box[3], anchor_y,
                self._number(accidental.get("confidence", 1.0), "accidental.confidence"),
            ]
            tokens.append("accidental=" + ",".join(fields))

        seen_stem_ids: set[str] = set()
        for stem in stems:
            stem_id = self._safe_id(stem["id"], "stem id")
            if stem_id in seen_stem_ids:
                raise ValueError(f"duplicate stem id: {stem_id}")
            seen_stem_ids.add(stem_id)
            chord_id = self._safe_id(stem["chord_id"], "stem chord_id")
            direction = self._safe_id(stem["direction"], "stem direction")
            fields = [
                stem_id, chord_id,
                self._number(stem["x"], "stem.x"),
                self._number(stem["y_top"], "stem.y_top"),
                self._number(stem["y_bottom"], "stem.y_bottom"),
                direction,
            ]
            tokens.append("stem=" + ",".join(fields))

        command = " ".join(tokens)
        with self._lock:
            if self._process.poll() is not None:
                raise RuntimeError("Pitch context engine process has stopped.")
            assert self._process.stdin is not None
            assert self._process.stdout is not None
            self._process.stdin.write(command + "\n")
            self._process.stdin.flush()
            line = self._process.stdout.readline()
            if not line:
                raise RuntimeError("Pitch context engine closed its response stream.")
            result = json.loads(line)

        if result.get("type") == "error":
            raise ValueError(result.get("message", "Pitch context engine error."))
        return result

    def reset_ties(self, *, staff_id: str | None = None, voice_id: str | None = None) -> dict[str, Any]:
        """Reset pending tie state for one staff/voice, or for the whole engine.

        Provide both staff_id and voice_id to reset one context; omit both to
        clear every pending tie in this engine process.
        """
        if (staff_id is None) != (voice_id is None):
            raise ValueError("provide both staff_id and voice_id, or neither")
        command = "RESET_TIES"
        if staff_id is not None and voice_id is not None:
            command += (
                f" staff={self._safe_id(staff_id, 'staff_id')}"
                f" voice={self._safe_id(voice_id, 'voice_id')}"
            )
        with self._lock:
            if self._process.poll() is not None:
                raise RuntimeError("Pitch context engine process has stopped.")
            assert self._process.stdin is not None
            assert self._process.stdout is not None
            self._process.stdin.write(command + "\\n")
            self._process.stdin.flush()
            line = self._process.stdout.readline()
            if not line:
                raise RuntimeError("Pitch context engine closed its response stream.")
            result = json.loads(line)
        if result.get("type") == "error":
            raise ValueError(result.get("message", "Could not reset tie state."))
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

    def __enter__(self) -> "PitchContextClient":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


if __name__ == "__main__":
    import sys

    binary = sys.argv[1] if len(sys.argv) > 1 else "./pitch_context_engine"
    example_notes = [
        {"id": "n1", "x": 200, "y": 120, "confidence": 0.99,
         "bbox": {"left": 190, "top": 115, "right": 210, "bottom": 125}},
        {"id": "n2", "x": 200, "y": 110, "confidence": 0.99,
         "bbox": {"left": 190, "top": 105, "right": 210, "bottom": 115}},
    ]
    with PitchContextClient(binary) as engine:
        print(json.dumps(engine.resolve(
            clef="treble", staff_top_y=100, line_spacing=10,
            key_signature="G", measure_id="demo",
            notes=example_notes,
        ), indent=2))

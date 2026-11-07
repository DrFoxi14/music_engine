"""Python interface for OMR candidate resolution using the C++ constraint solver."""

from __future__ import annotations

import json
import math
import subprocess
import threading
from pathlib import Path
from typing import Any, Mapping, Sequence


class OMRConstraintClient:
    """Send visual candidates to C++ and receive mathematically valid readings.

    The visual recognizer supplies candidate durations. This module never
    invents pixel evidence: it only tests timing constraints and returns
    guidance for features that may disambiguate remaining candidates.
    """

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
        line = self._process.stdout.readline()
        if not line:
            self.close()
            raise RuntimeError("OMR solver failed to start.")
        self.info = json.loads(line)
        if self.info.get("type") != "ready":
            self.close()
            raise RuntimeError(f"Unexpected OMR solver startup response: {self.info}")

    @staticmethod
    def _validate_id(event_id: str) -> str:
        if not event_id or any(not (c.isalnum() or c in "_-") for c in event_id):
            raise ValueError("event id may contain only letters, digits, _ and -")
        return event_id

    @staticmethod
    def _candidate_text(candidate: Mapping[str, Any]) -> str:
        duration = str(candidate["duration"])
        confidence = float(candidate.get("confidence", 1.0))
        if not math.isfinite(confidence) or not 0.0 < confidence <= 1.0:
            raise ValueError("confidence must be in (0, 1]")
        if duration == "none":                      # the symbol may not exist
            return f"none:{confidence:.12g}"
        parts = duration.split("/")
        if len(parts) != 2:
            raise ValueError(f"duration must look like 1/4, got {duration!r}")
        numerator, denominator = (int(part) for part in parts)
        if numerator <= 0 or denominator <= 0:
            raise ValueError("duration components must be positive")

        confidence = float(candidate.get("confidence", 1.0))
        if not math.isfinite(confidence) or not 0.0 < confidence <= 1.0:
            raise ValueError("confidence must be in (0, 1]")

        return f"{numerator}/{denominator}:{confidence:.12g}"

    def resolve(
        self,
        meter: str,
        events: Sequence[Mapping[str, Any]],
        *,
        max_note_denominator: int = 256,
        max_nodes: int = 200_000,
        limit: int = 20,
        beat: str | None = None,
        groups: Sequence[Sequence[str]] = (),
        group_penalty: float = 0.2,
    ) -> dict[str, Any]:
        """Resolve one sequential monophonic part/voice inside a single measure.

        Each event:
          {"id": "n1", "candidates": [{"duration":"1/4","confidence":0.9}],
           "bbox": {"x":10,"y":20,"width":40,"height":50},
           "visual_checks":["head_fill"]}

        `beat` ("1/4", "3/8") with `groups` (lists of consecutive event ids, e.g.
        the notes under one beam): a group that starts or ends off the beat
        multiplies the visual score by `group_penalty` (a preference, not a rule).
        """
        try:
            meter_num, meter_den = (int(part) for part in meter.split("/"))
        except Exception as exc:
            raise ValueError("meter must look like 4/4") from exc
        if meter_num <= 0 or meter_den <= 0:
            raise ValueError("meter values must be positive")
        if not events:
            raise ValueError("at least one event is required")

        tokens = [
            "RESOLVE", str(meter_num), str(meter_den), str(max_note_denominator),
            f"max_nodes={int(max_nodes)}", f"limit={int(limit)}",
        ]
        metadata: dict[str, Mapping[str, Any]] = {}
        for event in events:
            event_id = self._validate_id(str(event["id"]))
            if event_id in metadata:
                raise ValueError(f"duplicate event id: {event_id}")
            candidates = event.get("candidates")
            if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)) or not candidates:
                raise ValueError(f"event {event_id} must have a non-empty candidates list")
            candidate_token = "|".join(self._candidate_text(candidate) for candidate in candidates)
            tokens.append(f"{event_id}={candidate_token}")
            metadata[event_id] = event

        if groups:
            if not beat:
                raise ValueError("groups need a beat")
            tokens.append(f"beat={beat}")
            tokens.append(f"group_penalty={float(group_penalty)!r}")
            for group in groups:
                ids = [self._validate_id(str(i)) for i in group]
                if not ids or any(i not in metadata for i in ids):
                    raise ValueError("a group must name known events")
                tokens.append("group=" + ",".join(ids))

        command = " ".join(tokens)
        with self._lock:
            if self._process.poll() is not None:
                raise RuntimeError("OMR solver process has stopped.")
            assert self._process.stdin is not None
            assert self._process.stdout is not None
            self._process.stdin.write(command + "\n")
            self._process.stdin.flush()
            line = self._process.stdout.readline()
            if not line:
                raise RuntimeError("OMR solver closed its response stream.")
            result = json.loads(line)

        if result.get("type") == "error":
            raise ValueError(result.get("message", "OMR solver error."))

        # Restore geometric context from the Visual Engine input. The C++ solver
        # deliberately remains image-agnostic.
        for item in result.get("event_support", []):
            source = metadata.get(item["event_id"], {})
            if "bbox" in source:
                item["bbox"] = source["bbox"]
            if "visual_checks" in source:
                item["requested_visual_checks"] = source["visual_checks"]

        result["scope"] = {
            "assumption": "one ordered monophonic voice/part in one measure",
            "visual_evidence_source": "caller",
            "confidence_role": "ranking only; not proof of visual correctness",
        }
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

    def __enter__(self) -> "OMRConstraintClient":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


if __name__ == "__main__":
    import sys

    binary = sys.argv[1] if len(sys.argv) > 1 else "./omr_constraint_solver"
    events = [
        {"id": "n1", "candidates": [{"duration": "1/2", "confidence": 0.99}]},
        {"id": "n2", "candidates": [{"duration": "1/4", "confidence": 0.98}]},
        {
            "id": "n3",
            "candidates": [
                {"duration": "1/2", "confidence": 0.52},
                {"duration": "1/4", "confidence": 0.48},
            ],
            "bbox": {"x": 1240, "y": 850, "width": 90, "height": 120},
        },
    ]
    with OMRConstraintClient(binary) as solver:
        print(json.dumps(solver.resolve("4/4", events), indent=2))

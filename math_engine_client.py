"""Python client for the persistent C++ Music Engine process."""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from typing import Any


class MathEngineClient:
    """Synchronous, thread-safe client for one running rhythm-engine process."""

    def __init__(
        self,
        executable: str | Path,
        numerator: int = 4,
        denominator: int = 4,
        max_note_denominator: int = 256,
    ) -> None:
        self._lock = threading.Lock()
        self._process = subprocess.Popen(
            [
                str(executable),
                str(numerator),
                str(denominator),
                str(max_note_denominator),
            ],
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
            raise RuntimeError("The C++ engine did not send its startup message.")

        self.info: dict[str, Any] = json.loads(first_line)
        if self.info.get("type") != "ready":
            self.close()
            raise RuntimeError(f"Unexpected engine startup response: {self.info}")

    def _send_one(self, command: str) -> dict[str, Any]:
        assert self._process.stdin is not None
        assert self._process.stdout is not None

        if self._process.poll() is not None:
            raise RuntimeError("The C++ engine process has stopped.")

        self._process.stdin.write(command + "\n")
        self._process.stdin.flush()

        line = self._process.stdout.readline()
        if not line:
            raise RuntimeError("The C++ engine closed its response stream.")

        result = json.loads(line)
        if result.get("type") == "error":
            raise ValueError(result.get("message", "Unknown engine error."))
        return result

    @staticmethod
    def _validate_category(category: str) -> str:
        normalized = category.upper()
        if normalized not in {"NOTES", "RESTS"}:
            raise ValueError("category must be NOTES or RESTS")
        return normalized

    def count(self, category: str = "NOTES") -> int:
        category = self._validate_category(category)
        with self._lock:
            result = self._send_one(f"COUNT {category}")
        return int(result["count"])

    def get(self, index: int, category: str = "NOTES") -> dict[str, Any]:
        category = self._validate_category(category)
        if index < 0:
            raise ValueError("index must be >= 0")

        with self._lock:
            return self._send_one(f"GET {category} {index}")

    def page(
        self,
        start: int = 0,
        limit: int = 20,
        category: str = "NOTES",
    ) -> list[dict[str, Any]]:
        category = self._validate_category(category)
        if start < 0:
            raise ValueError("start must be >= 0")
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")

        results: list[dict[str, Any]] = []

        with self._lock:
            assert self._process.stdin is not None
            assert self._process.stdout is not None

            if self._process.poll() is not None:
                raise RuntimeError("The C++ engine process has stopped.")

            self._process.stdin.write(f"PAGE {category} {start} {limit}\n")
            self._process.stdin.flush()

            while True:
                line = self._process.stdout.readline()
                if not line:
                    raise RuntimeError("The C++ engine closed the stream during PAGE.")

                item = json.loads(line)
                if item.get("type") == "page_end":
                    break
                if item.get("type") == "error":
                    raise ValueError(item.get("message", "Unknown engine error."))

                results.append(item)

        return results

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

    def __enter__(self) -> "MathEngineClient":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


# Backward-compatible alias for existing scripts.
RhythmEngineClient = MathEngineClient


if __name__ == "__main__":
    import sys

    executable = sys.argv[1] if len(sys.argv) > 1 else "./math_engine"
    with MathEngineClient(executable, 4, 4, 256) as engine:
        print("Engine:", engine.info)
        print("Note-only patterns:", engine.count("NOTES"))
        print("First note-only pattern:", engine.get(0, "NOTES"))
        print("First 3 patterns with rests:", engine.page(0, 3, "RESTS"))

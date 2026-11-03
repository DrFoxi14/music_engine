"""Python client for the persistent C++ Music Engine search process."""

from __future__ import annotations

import json
import re
import subprocess
import threading
from pathlib import Path
from typing import Any, Mapping


class RhythmSearchClient:
    """Thread-safe client for filtered rhythm search and pattern generation."""

    _ALLOWED_FILTERS = {
        "limit", "offset", "max_nodes", "min_events", "max_events",
        "min_triplets", "max_triplets", "min_dotted", "max_dotted",
        "min_rest_slots", "max_rest_slots", "min_note_slots", "max_note_slots",
        "min_event_ticks", "max_event_ticks", "durations", "triplet_patterns",
        "tuplet_patterns", "tuplet_ratios",
        "kinds", "denominators", "subdivisions", "exact", "first", "last",
        "prefix", "suffix", "contains", "positions",
    }

    def __init__(
        self,
        executable: str | Path,
        numerator: int = 4,
        denominator: int = 4,
        max_note_denominator: int = 256,
    ) -> None:
        self._lock = threading.Lock()
        self._process = subprocess.Popen(
            [str(executable), str(numerator), str(denominator), str(max_note_denominator)],
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
            raise RuntimeError("C++ search engine closed before its ready message.")
        self.info: dict[str, Any] = json.loads(first_line)
        if self.info.get("type") != "ready":
            self.close()
            raise RuntimeError(f"Unexpected startup response: {self.info}")

    @staticmethod
    def _encode_value(key: str, value: Any) -> str:
        if key == "exact":
            if isinstance(value, Mapping):
                encoded = ",".join(
                    f"{descriptor}:{int(count)}" for descriptor, count in value.items()
                )
            elif isinstance(value, str):
                encoded = value
            else:
                raise TypeError("exact must be a mapping like {'note@2': 1} or a string")
        elif key == "positions":
            # [["note@4", "note@8"], ["rest@4"], ["*"]]: allowed descriptors per event
            encoded = value if isinstance(value, str) else ">".join("|".join(str(a) for a in alts) for alts in value)
        elif key in {"prefix", "suffix", "contains"}:
            encoded = ">".join(str(item) for item in value) if not isinstance(value, str) else value
        elif key in {"durations", "triplet_patterns", "tuplet_patterns", "tuplet_ratios", "kinds", "denominators", "subdivisions"}:
            encoded = ",".join(str(item) for item in value) if not isinstance(value, str) else value
        else:
            encoded = str(value)

        if not encoded or re.search(r"[\s\r\n\x00]", encoded):
            raise ValueError(f"invalid whitespace in filter value for {key}")
        return encoded

    def search(self, category: str = "NOTES", **filters: Any) -> dict[str, Any]:
        """Return {'patterns': [...], 'summary': {...}} for NOTES or RESTS."""
        category = category.upper()
        if category not in {"NOTES", "RESTS"}:
            raise ValueError("category must be NOTES or RESTS")
        unknown = set(filters) - self._ALLOWED_FILTERS
        if unknown:
            raise ValueError(f"unsupported filter(s): {', '.join(sorted(unknown))}")

        parts = ["SEARCH", category]
        for key, value in filters.items():
            if value is not None:
                parts.append(f"{key}={self._encode_value(key, value)}")
        command = " ".join(parts)

        patterns: list[dict[str, Any]] = []
        with self._lock:
            if self._process.poll() is not None:
                raise RuntimeError("C++ search engine process has stopped.")
            assert self._process.stdin is not None
            assert self._process.stdout is not None
            self._process.stdin.write(command + "\n")
            self._process.stdin.flush()

            while True:
                line = self._process.stdout.readline()
                if not line:
                    raise RuntimeError("C++ search engine closed during a search request.")
                item = json.loads(line)
                if item.get("type") == "error":
                    raise ValueError(item.get("message", "Search engine error."))
                if item.get("type") == "search_summary":
                    return {"patterns": patterns, "summary": item}
                if item.get("type") != "pattern":
                    raise RuntimeError(f"Unexpected search response: {item}")
                patterns.append(item)

    def generate(self, category: str = "NOTES", **filters: Any) -> dict[str, Any]:
        """Alias for search(); matching patterns are generated lazily."""
        return self.search(category, **filters)

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

    def __enter__(self) -> "RhythmSearchClient":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


if __name__ == "__main__":
    import sys

    executable = sys.argv[1] if len(sys.argv) > 1 else "./rhythm_search_engine"
    with RhythmSearchClient(executable, 4, 4, 256) as engine:
        result = engine.search("NOTES", exact={"note@2": 1, "note@4": 2}, limit=20)
        print("Engine:", engine.info)
        print("Summary:", result["summary"])
        for pattern in result["patterns"]:
            print(pattern)

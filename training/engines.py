"""The four C++ engines, started once and reused (one process per meter where the protocol needs it).

Binaries are looked up in ../build (or $MUSIC_ENGINE_BUILD); every call goes through the repository's
own Python clients, so this lab exercises exactly the protocol the vision side will use.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omr_constraint_client import OMRConstraintClient      # noqa: E402
from pitch_context_client import PitchContextClient        # noqa: E402
from rhythm_search_client import RhythmSearchClient        # noqa: E402
from ritmuri_client import RhythmEngineClient              # noqa: E402

BUILD = Path(os.environ.get("MUSIC_ENGINE_BUILD", ROOT / "build"))
MAX_DEN = 64


class Engines:
    def __init__(self, build: Path = BUILD):
        self.build = Path(build)
        self._ritmuri, self._search, self._counts = {}, {}, {}
        self.solver = OMRConstraintClient(self.build / "omr_constraint_solver")
        self.pitch = PitchContextClient(self.build / "pitch_context_engine")

    def ritmuri(self, meter, resolution) -> RhythmEngineClient:
        k = (tuple(meter), resolution)
        if k not in self._ritmuri:
            self._ritmuri[k] = RhythmEngineClient(self.build / "ritmuri_engine", meter[0], meter[1], resolution)
        return self._ritmuri[k]

    def count(self, meter, resolution, category) -> int:
        k = (tuple(meter), resolution, category)
        if k not in self._counts:
            self._counts[k] = self.ritmuri(meter, resolution).count(category)
        return self._counts[k]

    def search(self, meter) -> RhythmSearchClient:
        k = tuple(meter)
        if k not in self._search:
            self._search[k] = RhythmSearchClient(self.build / "rhythm_search_engine", meter[0], meter[1], MAX_DEN)
        return self._search[k]

    def close(self) -> None:
        for c in [*self._ritmuri.values(), *self._search.values(), self.solver, self.pitch]:
            c.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

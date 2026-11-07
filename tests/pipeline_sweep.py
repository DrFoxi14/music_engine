"""End-to-end sweep: every rhythm of a space, drawn as primitives, read back.

For each pattern (all of them, or a uniform random sample of a huge space):
  full evidence    -> STEM-first + solver must give exactly the true durations
  minimal evidence -> fill and dots unchecked; the truth must stay possible,
                      implied events must equal the truth, and the question
                      loop must reach the truth asking only real ambiguities

Run directly for large sweeps:
  python3 tests/pipeline_sweep.py BUILD_DIR 2/4 16 "longest=1 max_dots=1 depth=0" all
  python3 tests/pipeline_sweep.py BUILD_DIR 4/4 64 "" sample 500
"""

from __future__ import annotations

import math
import sys
from collections import Counter
from fractions import Fraction
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from score_renderer import render  # noqa: E402
from search_engine_client import SearchEngineClient  # noqa: E402
from stem_first_client import StemFirstClient, infer_measure, read_measure  # noqa: E402
from omr_constraint_client import OMRConstraintClient  # noqa: E402


def options_from(text: str) -> dict:
    out = {}
    for token in text.split():
        key, value = token.split("=", 1)
        out[key] = value
    return out


def patterns(search, mode, count, options, seed=1, category="ALL"):
    if mode == "all":
        start = 0
        while True:
            page = search.page(start, 1000, category, **options)
            yield from page["patterns"]
            start += 1000
            if not page["summary"]["has_more"]:
                return
    else:
        yield from search.sample(count, category, seed=seed, **options)["patterns"]


class Sweep:
    def __init__(self, stem: StemFirstClient, solver: OMRConstraintClient):
        self.stem = stem
        self.solver = solver
        self.stats = Counter()
        self.question_hist = Counter()
        self.failures: list[str] = []

    def check(self, notation: str, meter: tuple[int, int], seed: int) -> None:
        meter_text = f"{meter[0]}/{meter[1]}"
        elements, truth = render(notation, meter, seed=seed)
        want = [str(t["duration"].numerator) + "/" + str(t["duration"].denominator) for t in truth]
        ids = [t["id"] for t in truth]
        self.stats["patterns"] += 1

        # 1. Full evidence: the reading must be exactly the truth.
        full = read_measure(self.stem, self.solver, meter=meter_text, spacing=10, **elements)
        got = [e["duration"] for e in full["events"]]
        if [e["id"] for e in full["events"]] != ids or got != want:
            self.failures.append(f"full evidence: {notation} -> {got} (want {want})")
            return

        # 2. Minimal evidence + questions answered by the truth.
        by_id = {t["id"]: t for t in truth}
        asked = []

        def answer(event_id, feature):
            asked.append((event_id, feature))
            return by_id[event_id][feature]

        result = infer_measure(self.stem, self.solver, meter=meter_text, spacing=10, answer=answer, **elements)
        if result["status"] != "resolved_unique":
            self.failures.append(f"minimal evidence: {notation} -> {result['status']}")
            return
        got = [result["durations"][i] for i in ids]
        if got != want:
            self.failures.append(f"minimal evidence: {notation} -> {got} (want {want})")
            return
        implied = set(result["implied_without_questions"])
        if any(event_id in implied for event_id, _ in asked):
            self.failures.append(f"asked about an implied event: {notation} {asked}")
        if len(asked) > len(set(asked)):
            self.failures.append(f"asked the same check twice: {notation} {asked}")
        readings = result["questions"][0]["readings_before"] if result["questions"] else 1
        if result["questions"] and readings <= 100 and len(asked) > len(ids) * 2:
            self.failures.append(f"too many questions: {notation} {asked}")
        self.question_hist[len(asked)] += 1
        self.stats["events"] += len(ids)
        self.stats["implied_events"] += len(implied)
        self.stats["questions"] += len(asked)
        self.stats["lower_bound"] += math.ceil(math.log2(readings)) if readings > 1 else 0

    def summary(self) -> str:
        n = self.stats["patterns"]
        events = max(self.stats["events"], 1)
        zero = self.question_hist[0]
        return (f"patterns={n} failures={len(self.failures)} "
                f"bars_needing_no_question={zero} ({100 * zero / max(n, 1):.1f}%) "
                f"events_implied={self.stats['implied_events']}/{events} "
                f"({100 * self.stats['implied_events'] / events:.1f}%) "
                f"questions={self.stats['questions']} log2_worst_case_bound={self.stats['lower_bound']} "
                f"histogram={dict(sorted(self.question_hist.items()))}")


def main() -> None:
    build, meter_text, max_den, opts, mode = sys.argv[1:6]
    count = int(sys.argv[6]) if len(sys.argv) > 6 else 0
    meter = tuple(int(x) for x in meter_text.split("/"))
    build = Path(build)
    with SearchEngineClient(build / "search_engine", meter[0], meter[1], int(max_den)) as search, \
         StemFirstClient(build / "stem_first_engine") as stem, \
         OMRConstraintClient(build / "omr_constraint_solver") as solver:
        sweep = Sweep(stem, solver)
        for i, pattern in enumerate(patterns(search, mode, count, options_from(opts))):
            sweep.check(pattern["notation"], meter, seed=i)
        print(meter_text, max_den, opts, mode, sweep.summary())
        for failure in sweep.failures[:10]:
            print("  ", failure)


if __name__ == "__main__":
    main()

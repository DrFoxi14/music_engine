# Music Engine

A C++17 toolkit for exact rhythmic pattern generation, enumeration, and filtered search.

## Components

- `ritmuri_engine.cpp` — compact dynamic-programming index for counting and retrieving patterns by index.
- `rhythm_search_engine.cpp` — bounded, filter-driven search and lazy pattern generation.
- `ritmuri_client.py` — Python client for the original index engine.
- `rhythm_search_client.py` — Python client for filtered search and generation.

The engines keep their working indexes and event tables in process memory. They do not materialize the full combinatorial pattern space or write a giant TXT file.

## Build on macOS / Apple Silicon

Using Apple Clang:

```bash
clang++ -std=c++17 -O3 -DNDEBUG -Wall -Wextra -Wpedantic ritmuri_engine.cpp -o ritmuri_engine
clang++ -std=c++17 -O3 -DNDEBUG -Wall -Wextra -Wpedantic rhythm_search_engine.cpp -o rhythm_search_engine
```

Or with CMake:

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
```

Start the search process for 4/4 with note values down to 1/256:

```bash
./rhythm_search_engine 4 4 256
```

Arguments are:

```
rhythm_search_engine <numerator> <denominator> <maximum_note_denominator>
```

The maximum note denominator is a power of two from 2 through 256.

## Search and generation protocol

The process stays alive and accepts one command per input line. Each result is emitted as one JSON object per line (JSONL).

```
HELP
SEARCH NOTES limit=50 max_nodes=200000 exact=note@2:1,note@4:2
SEARCH RESTS limit=50 kinds=note,dotted_note,rest,triplet subdivisions=2,4,8
GENERATE NOTES limit=20 exact=note@2:1,note@4:2 prefix=note@2
EXIT
```

- `NOTES` — patterns containing only notes.
- `RESTS` — patterns containing at least one rest, including rests inside triplet groups.
- `SEARCH` and `GENERATE` currently use the same lazy generation pipeline.
- The final `search_summary` reports results returned, nodes visited, whether the node budget truncated the search, and whether more matching patterns exist.
- Search does not store every possible pattern. It walks the candidate space and emits only matches.

## Filters

All filters are optional unless noted.

| Filter | Meaning | Example |
|---|---|---|
| `exact` | Exact multiset of event descriptors; returns all distinct orderings | `exact=note@2:1,note@4:2` |
| `kinds` | Allowed event kinds | `kinds=note,dotted_note,triplet` |
| `denominators` / `subdivisions` | Base note denominators allowed | `subdivisions=4,8,16` |
| `durations` | Actual event durations, expressed as fractions of a whole note | `durations=1/4,3/8,1/8` |
| `triplet_patterns` | Triplet slots, left to right: N = note, R = rest | `triplet_patterns=NNN,NRN,RNN` |
| `min_events`, `max_events` | Minimum / maximum number of event groups | `min_events=3 max_events=8` |
| `min_triplets`, `max_triplets` | Bounds on triplet groups | `min_triplets=1 max_triplets=2` |
| `min_dotted`, `max_dotted` | Bounds on dotted note/rest events | `min_dotted=1 max_dotted=2` |
| `min_rest_slots`, `max_rest_slots` | Bounds on rests, including rests inside triplets | `min_rest_slots=1 max_rest_slots=3` |
| `min_note_slots`, `max_note_slots` | Bounds on noteheads, including triplet slots | `min_note_slots=3 max_note_slots=8` |
| `min_event_ticks`, `max_event_ticks` | Event duration bounds on the engine's exact internal time grid | `min_event_ticks=12` |
| `first`, `last` | First / last event descriptor | `first=note@2 last=note@4` |
| `prefix`, `suffix` | Ordered beginning / ending sequence | `prefix=note@2>note@4` |
| `contains` | Ordered contiguous sequence anywhere in the pattern | `contains=note@2>triplet@8` |
| `limit` | Maximum results returned (1–1000; default 50) | `limit=100` |
| `offset` | Skip this many matches before returning results | `offset=100` |
| `max_nodes` | Search work budget (default 200,000; maximum 10,000,000) | `max_nodes=1000000` |

### Descriptor syntax

An event descriptor is `kind@denominator`:

- `note@2` — half note.
- `note@4` — quarter note.
- `dotted_note@4` — dotted quarter note (actual duration `3/8` of a whole note).
- `rest@8` — eighth rest.
- `triplet@8` — one group of three eighth-note triplet slots spanning `1/4` of a whole note.
- `measure_rest@0` — full-measure rest, used when the bar length is not one whole note.

For a triplet, the JSON result also exposes the three slots and their individual duration. For example, `NRN` means note–rest–note. The `durations` filter applies to the whole event group; the triplet slot duration is returned separately.

### Example: one half note and two quarter notes in 4/4

```text
SEARCH NOTES exact=note@2:1,note@4:2 limit=10
```

This produces the three distinct event orders:

1. half, quarter, quarter
2. quarter, half, quarter
3. quarter, quarter, half

### Example: dotted quarter, two quarters, and an eighth

```text
SEARCH NOTES exact=dotted_note@4:1,note@4:2,note@8:1 durations=3/8,1/4,1/8 limit=20
```

### Example: triplet with a rest in the middle

```text
SEARCH RESTS exact=triplet@8:1,note@2:1,note@4:1 triplet_patterns=NRN limit=20
```

## Python client

```python
from rhythm_search_client import RhythmSearchClient

with RhythmSearchClient("./rhythm_search_engine", 4, 4, 256) as engine:
    result = engine.search(
        "NOTES",
        exact={"note@2": 1, "note@4": 2},
        limit=20,
    )

    print(result["summary"])
    for pattern in result["patterns"]:
        print(pattern)
```

The Python process is only a client. The search and generation run in C++.

## Search limits and completeness

This is a bounded depth-first search, not a database containing every possible rhythm. It is designed for targeted requests from a bot.

- If `has_more=true`, additional matching results exist after the returned page.
- If `truncated=true`, the configured node budget was reached before the search space was exhausted. Increase `max_nodes` or make the filters more selective.
- If both are false, the search completed and all matches for that query were examined.
- Exact multiset queries such as `exact=note@2:1,note@4:2` are especially efficient because the engine only explores the specified event inventory.

The search engine currently supports normal notes/rests, single-dotted notes/rests, and triplet groups. Double/triple dots, quintuplets, septuplets, ties, accents, and notation engraving are not implemented yet.


## OMR Constraint Solver

`omr_constraint_solver.cpp` resolves rhythmic duration candidates supplied by a Visual Engine. It is image-agnostic: the visual model detects stems, flags, beams, noteheads, dots, and candidate durations; this solver checks exact musical timing.

Build:

```bash
clang++ -std=c++17 -O3 -DNDEBUG -Wall -Wextra -Wpedantic omr_constraint_solver.cpp -o omr_constraint_solver
```

Python API:

```python
from omr_constraint_client import OMRConstraintClient

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

with OMRConstraintClient("./omr_constraint_solver") as solver:
    result = solver.resolve("4/4", events)
    print(result["status"])
    print(result["solutions"])
    print(result["event_support"])
```

The example resolves the last note to a quarter note because the other candidates do not fill the 4/4 measure. Candidate confidence only ranks valid solutions; it never overrides the exact duration constraint.

Statuses:

- `resolved_unique` — exactly one valid assignment was found and the search completed.
- `ambiguous` — multiple valid assignments remain.
- `inconsistent` — no assignment satisfies the supplied candidates and measure.
- `incomplete` — the node budget was reached; uniqueness cannot be claimed.

The solver returns exact onset positions, candidate support/elimination per event, and suggested visual checks. The Python client attaches caller-provided bounding boxes to those guidance records.

**Scope limitation:** version 1 handles one ordered monophonic voice/part in one measure. It must be called separately for each voice. Polyphonic SATB alignment, pickup measures, ties across barlines, tuplets beyond supplied effective durations, and cross-measure constraints require later extensions. Musical validity is not proof that the visual candidate list is correct: if the Visual Engine omitted the true candidate, the solver can report `inconsistent`, but cannot reconstruct missing pixel evidence.

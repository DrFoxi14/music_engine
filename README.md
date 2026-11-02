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


## Pitch Context Engine (Y-axis / pitch reasoning)

`pitch_context_engine.cpp` maps notehead geometry to diatonic and chromatic pitch. The Visual Engine supplies the detected staff geometry and symbol bounding boxes; this module does not perform image recognition.

Capabilities:

- Clef-aware staff mapping for treble, bass, alto, and tenor clefs.
- Staff skew compensation using a linear slope.
- Key signatures for common major keys and their relative minor keys.
- Measure-local accidental memory keyed by note letter and octave.
- Sharp, flat, natural, double-sharp, and double-flat accidental values.
- Geometric accidental-to-notehead association, with optional Visual Engine-provided `anchor_y` (falls back to glyph bounding-box center).
- Explicit `associated`, `ambiguous`, and `orphan` accidental outcomes.
- Advisory chord interval diagnostics for semitone clusters and tritones, with optional visual-review regions; these never reject or rewrite notes.
- Chord stem attachment checks using the actual attachment notehead: lower chord note for an up-stem, upper chord note for a down-stem.
- Visual guidance bounding boxes for ambiguous accidentals, off-grid noteheads, and suspicious stem attachments.

Build:

```bash
clang++ -std=c++17 -O3 -DNDEBUG -Wall -Wextra -Wpedantic pitch_context_engine.cpp -o pitch_context_engine
```

Python API:

```python
from pitch_context_client import PitchContextClient

notes = [
    {
        "id": "n1", "x": 200, "y": 120,
        "bbox": {"left": 190, "top": 115, "right": 210, "bottom": 125},
        "stem_id": "s1", "chord_id": "ch1",
    },
    {
        "id": "n2", "x": 200, "y": 110,
        "bbox": {"left": 190, "top": 105, "right": 210, "bottom": 115},
        "stem_id": "s1", "chord_id": "ch1",
    },
]
accidentals = [{
    "id": "a1", "kind": "sharp",
    "bbox": {"left": 170, "top": 103, "right": 182, "bottom": 117},
}]
stems = [{
    "id": "s1", "chord_id": "ch1", "x": 210,
    "y_top": 80, "y_bottom": 120, "direction": "up",
}]

with PitchContextClient("./pitch_context_engine") as engine:
    result = engine.resolve(
        clef="treble",
        staff_top_y=100,
        line_spacing=10,
        key_signature="G",
        measure_id="bar-12",
        notes=notes,
        accidentals=accidentals,
        stems=stems,
    )
    print(result["notes"])
    print(result["accidental_associations"])
    print(result["visual_guidance"])
```

### Accidental-to-note assignment

Accidental association uses page geometry, not harmony as a hard rule. Candidate noteheads must be to the right of the accidental glyph and within a staff-spacing-based vertical/horizontal window. Candidates are ranked by vertical anchor distance, with horizontal distance as a weaker tie-breaker. If the Visual Engine can estimate the symbol's musical anchor independently of its bounding-box center (for example, the bulb/center topology of a flat or the center of a natural), pass it as `anchor_y`.

- A clear geometric winner is assigned.
- If two noteheads have similar geometric scores (for example, the accidental glyph overlaps two staff positions), the result is `ambiguous`; both candidate noteheads are returned for visual reinspection.
- If no notehead fits the association window, the accidental is `orphan`.
- The engine never assigns an accidental merely because doing so would make a chord more consonant.

A new `resolve()` request represents a new measure and starts with an empty local accidental register. Within a measure, a uniquely assigned accidental affects subsequent occurrences of the same letter and octave. Key-signature defaults apply when no local accidental is active.

### Important scope and notation limits

This is a geometry-and-pitch context module, not a complete notation interpreter. It currently models one staff/voice and one measure per request. It does not yet handle cross-barline ties, octave-transposing clefs/instruments, ottava lines, microtonal accidentals, historical key signatures, multi-staff grand-staff accidental policy, or full chord-voice separation.

Stem checks validate the stem's attachment to the appropriate chord anchor notehead. They do not require every notehead in a chord to touch the same stem: chord noteheads are commonly staggered around one shared stem.

Dissonance is reported as musical context for later modules, never used to reject a visually and notationally plausible pitch.

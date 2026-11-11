# Music Engine

A C++17 toolkit for exact rhythmic pattern generation, enumeration, and filtered search.

## Components

- `stem_first_engine.cpp` — STEM-first reading of stemmed notes (see [DrFoxi14/stem](https://github.com/DrFoxi14/stem)): free end from flag/beam, notehead at the opposite end, both ends tested for quarter notes, duration candidates from flag/beam count, head fill, dots and (nested) tuplets.
- `omr_constraint_solver.cpp` — exact measure arithmetic over duration candidates.
- `pitch_context_engine.cpp` — staff position, clef, key signature and accidentals to pitch.
- `search_engine.cpp` + `rhythm_space.hpp` — the complete rhythmic space of a measure (nested tuplets, multiple dots, any ratio) with exact counting, indexing, sampling and template search; plus the legacy filtered SEARCH.
- `math_engine.cpp` — compact dynamic-programming index for counting and retrieving rhythmic patterns by index, with separate note-only and at-least-one-rest categories.
- `*_client.py` — Python clients for each engine; `stem_first_client.py` also has pipeline helpers.
- `omr_dialogue/` — the Vision ⇄ Search Engine dialogue after Paul's Vision specification: a synthetic Vision that reports only what is in the image (per-property confidence, `unknown` kept unknown, never a duration), targeted re-examination, and `positions` constraints for the Search Engine. See `omr_dialogue/README.md`. `infer_measure()` in `stem_first_client.py` is the solver-based counterpart (exact counts, minimax question order).

### OMR pipeline (no visual detection here)

```
Visual Engine (pixels) ──► stems, noteheads, flags, beams, dots, rests, tuplet brackets
                              │
                    stem_first_engine          free end / notehead end, stem direction,
                              │                chords, duration candidates per event
              ┌───────────────┴────────────────┐
     omr_constraint_solver             pitch_context_engine
     (which durations fill the bar)    (pitch from notehead position)
              │
     search_engine templates           (what could fill a gap the image missed)
```

The engines keep their working indexes and event tables in process memory. They do not materialize the full combinatorial pattern space or write a giant TXT file.

## Math Engine

Math Engine counts the total number of valid rhythmic patterns for a selected meter and note-resolution limit. It can retrieve a specific pattern by index or return a page of patterns. `NOTES` contains patterns with no rests; `RESTS` contains patterns with at least one rest (and may also contain notes). It is an enumerator/index, not the filtered search engine.

Build it directly with:

```bash
clang++ -std=c++17 -O3 -DNDEBUG math_engine.cpp -o math_engine
```

Python example:

```python
from math_engine_client import MathEngineClient

with MathEngineClient("./math_engine", 4, 4, 256) as engine:
    print("Note-only patterns:", engine.count("NOTES"))
    print("First pattern:", engine.get(0, "NOTES"))
    print("First patterns containing rests:", engine.page(0, 3, "RESTS"))
```

## Build on macOS / Apple Silicon

Using Apple Clang:

```bash
clang++ -std=c++17 -O3 -DNDEBUG -Wall -Wextra -Wpedantic math_engine.cpp -o math_engine
clang++ -std=c++17 -O3 -DNDEBUG -Wall -Wextra -Wpedantic search_engine.cpp -o search_engine
```

Or with CMake:

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
```

Start the search process for 4/4 with note values down to 1/256:

```bash
./search_engine 4 4 256
```

Arguments are:

```
search_engine <numerator> <denominator> <maximum_note_denominator>
```

The maximum note denominator is a power of two from 2 through 256.

## Search Engine

`search_engine.cpp` is the filtered rhythm-search interface intended for clients such as the Visual Engine. It searches for patterns matching constraints and returns matching results; Vision does not need to call `math_engine` for ordinary filtered rhythm queries. The two C++ programs are currently independent: Search Engine does not invoke Math Engine internally. Math Engine remains useful for total counts and indexed enumeration.

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
- `RESTS` — patterns containing at least one rest, including rests inside tuplet groups.
- `SEARCH` and `GENERATE` currently use the same lazy generation pipeline.
- The final `search_summary` reports results returned, nodes visited, whether the node budget truncated the search, and whether more matching patterns exist.
- Search does not store every possible pattern. It walks the candidate space and emits only matches.

## Filters

All filters are optional unless noted.

| Filter | Meaning | Example |
|---|---|---|
| `exact` | Exact multiset of event descriptors; returns all distinct orderings | `exact=note@2:1,note@4:2` |
| `kinds` | Allowed event kinds | `kinds=note,dotted_note,triplet,tuplet` |
| `denominators` / `subdivisions` | Base note denominators allowed | `subdivisions=4,8,16` |
| `durations` | Actual event durations, expressed as fractions of a whole note | `durations=1/4,3/8,1/8` |
| `triplet_patterns` | Triplet slots, left to right: N = note, R = rest | `triplet_patterns=NNN,NRN,RNN` |
| `tuplet_ratios` | Restrict generic tuplet actual:normal ratios | `tuplet_ratios=4:3,5:4,6:4,7:4,9:8` |
| `tuplet_patterns` | Restrict generic tuplet slots using actual:normal:pattern | `tuplet_patterns=5:4:NNRNN,7:4:NNRNNRN` |
| `min_events`, `max_events` | Minimum / maximum number of event groups | `min_events=3 max_events=8` |
| `min_triplets`, `max_triplets` | Bounds on triplet groups | `min_triplets=1 max_triplets=2` |
| `min_dotted`, `max_dotted` | Bounds on dotted note/rest events | `min_dotted=1 max_dotted=2` |
| `min_rest_slots`, `max_rest_slots` | Bounds on rests, including rests inside tuplets | `min_rest_slots=1 max_rest_slots=3` |
| `min_note_slots`, `max_note_slots` | Bounds on noteheads, including tuplet slots | `min_note_slots=3 max_note_slots=8` |
| `min_event_ticks`, `max_event_ticks` | Event duration bounds on the engine's exact internal time grid | `min_event_ticks=12` |
| `first`, `last` | First / last event descriptor | `first=note@2 last=note@4` |
| `prefix`, `suffix` | Ordered beginning / ending sequence | `prefix=note@2>note@4` |
| `contains` | Ordered contiguous sequence anywhere in the pattern | `contains=note@2>triplet@8` |
| `positions` | Allowed descriptors for each event in order (`|` alternatives, `>` next event, `*` anything, `#NRN` a tuplet slot mask); fixes the event count | `positions=note@4>note@2>note@4|note@8` |
| `limit` | Maximum results returned (1–1000; default 50) | `limit=100` |
| `offset` | Skip this many matches before returning results | `offset=100` |
| `max_nodes` | Search work budget (default 200,000; maximum 10,000,000) | `max_nodes=1000000` |

### Descriptor syntax

An event descriptor is usually `kind@denominator`:

- `note@2` — half note.
- `note@4` — quarter note.
- `dotted_note@4` — dotted quarter note (actual duration `3/8` of a whole note).
- `rest@8` — eighth rest.
- `triplet@8` — legacy spelling for three eighth-note triplet slots spanning `1/4` of a whole note.
- `tuplet@4:5:4` — five quarter-note slots in the time normally occupied by four quarter notes; the group spans one whole note.
- `tuplet@8:9:8` — nine eighth-note slots in the time normally occupied by eight eighth notes.
- `measure_rest@0` — full-measure rest, used when the bar length is not one whole note.

The engine pre-generates these common actual:normal ratios: `3:2`, `4:3`, `5:4`, `6:4`, `7:4`, and `9:8`. Other ratios with `3 <= actual <= 12` and `1 <= normal <= 16` are generated on demand when supplied through `tuplet_ratios`, `tuplet_patterns`, or a tuplet descriptor in `exact`, `first`, `last`, `prefix`, `suffix`, or `contains`, including ratios such as `11:8`. Only `tuplet_ratios` and `tuplet_patterns` restrict results: a ratio named in a descriptor is generated but never widens an explicit `tuplet_ratios` list or excludes other tuplet ratios. The old `triplet@denominator` and `triplet_patterns` interface remains supported. Generic tuplet descriptors include the ratio so different group sizes never collide in exact filters or ordered sequence matching.

For generic tuplets, the JSON result exposes the ratio, every note/rest slot, and each slot's exact duration. For example, `5:4:NNRNN` means note–note–rest–note–note in a five-slot group. The `durations` filter applies to the entire tuplet group; slot duration is returned separately.

The timing grid uses integer ticks (`27720 = lcm(1..12)` ticks per whole note, times the maximum note denominator) to represent the supported tuplet denominators exactly (actual counts up to twelve), so no floating-point rounding is used for bar filling.

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
from search_engine_client import SearchEngineClient

with SearchEngineClient("./search_engine", 4, 4, 256) as engine:
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

The legacy `SEARCH`/`GENERATE` vocabulary is flat: notes/rests with at most one dot and tuplet groups whose slots are equal notes or rests. For nested tuplets, mixed values inside tuplets, multiple dots and exact counts use the complete rhythm space below.

## Complete rhythm space (COUNT / GET / PAGE / SAMPLE / PARSE)

`rhythm_space.hpp` defines every notated rhythm of one measure as a grammar:

```
measure := item+                      (fills the bar exactly)
item    := note | rest | M | tuplet
tuplet  := [a:n item+]                content of nominal length a*u sounds as n*u
```

A tuplet's content is any item sequence, so it can hold mixed values, rests, dots and further tuplets. Examples: `[3:2 n4 n8]`, `[5:4 n16 r16 n8.]` and `[7:4 n16 n16 [3:2 n16 n16 n16] n16 n16 n16]`, a triplet inside a septuplet. "Every possibility" is infinite without limits, so the options below bound the space; inside those limits nothing is missing.

| Option | Default | Meaning |
|---|---|---|
| `max_denominator` | startup value | shortest base value (power of two, up to 1024) |
| `longest` | `2` | longest base value: `1` whole, `2` breve, `4` longa |
| `max_dots` | `2` | dots per note/rest (0..4) |
| `depth` | `2` | tuplet nesting depth (0 = no tuplets, up to 4) |
| `max_actual`, `max_normal` | `12`, `16` | tuplet size limits |
| `tuplet_normals` | `standard` | `standard`: a:largest power of two below a (3:2, 5:4, 7:4, 9:8 ...) and 2:3, 4:3, 4:6, 8:6 ...; `any`: every n from 1..max_normal |
| `tuplet_ratios` | — | explicit list such as `3:2,5:6,7:4` (or `none`) |
| `unit_dots` | `1` | tuplets may be counted in dotted units |
| `min_tuplet_items` | `2` | `1` also allows a bracket over a single item |
| `values`, `dots` | all | restrict note/rest values (`4,8,16,B`) and dot counts (`0,1`) |
| `measure_rest` | `1` | include `M` (whole-bar rest) when the bar is not one whole note |
| `template=` | — | must be last; consumes the rest of the line |

Notation: `n4` quarter note, `r8.` dotted eighth rest, `n2..` double-dotted half, `nB` breve, `nL` longa, `M` full-measure rest, `[a:n ...]` tuplet (its unit is the content length divided by a, so it is not written).

Commands (categories: `ALL`, `NOTES` = no rest anywhere, `RESTS` = at least one rest):

```text
COUNT  ALL                                    exact number of patterns (big integer)
GET    RESTS 123456789123456789               decode one index directly
PAGE   NOTES 0 100 depth=1 max_dots=1          a page of consecutive patterns
SAMPLE ALL 10 seed=7                          uniformly random patterns
COUNT  ALL template=n4 * [3:2 n8 n8 n8]        patterns starting with a quarter, ending with a triplet
PAGE   ALL 0 50 template=n4. *=1/8 ? r4        known items, a 1/8 gap, exactly one unknown item, a rest
PARSE  [7:4 n16 n16 [3:2 n16 n16 n16] n16 n16 n16] n4
```

Template tokens: a concrete item, `?` (exactly one item), `*` (any items, possibly none), `+` (one or more), `*=3/16` (any items lasting exactly 3/16). Wildcards range over the configured space; concrete items may be any valid notation.

Each pattern lists its notation and every note/rest with notated value, exact sounding `duration`, `onset` and enclosing tuplet ratios.

```python
with SearchEngineClient("./search_engine", 4, 4, 256) as engine:
    print(engine.count("ALL")["digits"])          # 513-digit count for 4/4 at 1/256
    print(engine.get(10**200, "RESTS")["notation"])
    print(engine.page(0, 20, "ALL", template="n4 * r8", depth=1)["patterns"])
    print(engine.parse("[7:4 n16 n16 [3:2 n16 n16 n16] n16 n16 n16] n4")["events"])
```

How it works: inside a tuplet, the content is an ordinary sequence measured in notated (nominal) values; only sounding time is scaled by n/a. Counting is therefore a dynamic program per nesting level over nominal ticks, with exact big integers. Decoding an index walks the choices, using exact completion counts, without enumerating earlier patterns. A 4/4 bar at 1/256 with defaults is counted in about 0.05 s; decoding runs at roughly 1000 patterns per second.

Correctness: `tests/rhythm_space_reference.py` is an independent brute-force enumerator of the same grammar. The tests compare the C++ results pattern by pattern (all three categories and template queries) on several small configurations.

Not included: ties, grace notes, multiple voices in one sequence, beaming/spelling variants of the same rhythm, and irrational meters (denominators that are not powers of two).

## STEM-first Engine

`stem_first_engine.cpp` implements the logic of [DrFoxi14/stem](https://github.com/DrFoxi14/stem) on primitives supplied by the Visual Engine. It does not read pixels.

For each stem with real endpoints A and B (no orientation assumed):

| Evidence | Free end | Notehead searched at |
|---|---|---|
| flag at A / B | A / B | B / A |
| beam connected at A / B | A / B | B / A |
| neither (quarter-note branch) | undetermined | both ends tested; the end with a connected notehead is accepted |

- Stem length is never a rule. Tolerances are multiples of the staff spacing, so short, long or tilted handwritten stems give the same reading.
- The number of flags or stacked beams at the free end gives the subdivision (1 → 1/8, 2 → 1/16, ...). It never makes the end ambiguous.
- Without flag or beam, the head fill separates half (hollow) from quarter (filled). An uncertain fill yields both candidates with the fill confidence.
- Noteheads along the stem form one chord event, anchored at the notehead end. A chord head sits on the head side of the stem (left of an up stem, right of a down stem); across the stem only the displaced head of a second (one step toward the free end) or of a unison belongs to the chord. Any other head across the stem is another voice's head that touches it; it is left to its own stem, or becomes a stemless head (guidance `notehead_across_stem_not_a_second`). Dots to the right of a head extend the duration. Tuplet brackets (`3`, or explicit `5:6`) scale their members and multiply when nested.
- Rests come from the dedicated rest recognizer and are only placed on the timeline. Stemless hollow heads are whole notes. A stemless filled head lists every filled-head reading and asks for a stem recheck.
- Contradictions are reported, never guessed: flag or beam at both ends, noteheads at both ends, a stem without a notehead, a notehead at the free end, a notehead shared by several stems.

Build:

```bash
clang++ -std=c++17 -O3 -DNDEBUG -Wall -Wextra -Wpedantic stem_first_engine.cpp -o stem_first_engine
```

Pipeline:

```python
from stem_first_client import StemFirstClient, read_measure, pitch_inputs
from omr_constraint_client import OMRConstraintClient
from pitch_context_client import PitchContextClient

with StemFirstClient("./stem_first_engine") as stem, OMRConstraintClient("./omr_constraint_solver") as solver:
    measure = read_measure(
        stem, solver, meter="4/4", spacing=10,
        stems=[{"id": "s1", "x1": 106, "y1": 120, "x2": 106, "y2": 85}],
        noteheads=[{"id": "h1", "bbox": {"left": 94, "top": 115, "right": 106, "bottom": 125},
                    "fill": "unknown", "fill_confidence": 1.0}],
        rests=[{"id": "r1", "bbox": {"left": 140, "top": 100, "right": 150, "bottom": 125}, "value": 2, "dots": 1}],
    )
    print(measure["events"])      # duration resolved by the bar: the unknown head must be a quarter

    notes, stems = pitch_inputs(measure["analysis"], noteheads=[...], stems=[...])
    # PitchContextClient(...).resolve(..., notes=notes, stems=stems)
```

### STEM-first order: the stems tell Vision where to look

The stem is the unit; noteheads, dots, rests and pitch are completions. `PLAN` turns the cheapest detections into a search plan before any notehead is looked for:

1. The Visual Engine finds only stems and what touches their ends (flags, beams).
2. `PLAN` decides, for each stem, the free end and the direction, plus the duration the stem already implies (`durations_before_heads`: 1 beam → 1/8, ..., none → 1/4 or 1/2). It also returns the regions to search:
   - `notehead`: one box at the notehead end, beside the stem on the side the direction implies. In the quarter-note branch there is one box per end, and exactly one of them should hold the head.
   - `chord_noteheads`: along the stem from the notehead end to 1.5 spaces before the free end, on both sides of the stem (the head of a second is set on the other side). In the quarter-note branch there is one per end, with that end's hypothesis: a chord is as common without flag or beam.
   - `augmentation_dots`: right of the head, on its row.
3. The Visual Engine confirms noteheads and dots only inside those regions, then `ANALYZE` reads the bar as before.

All region sizes are multiples of the staff spacing; the stem's length is never used. Whole notes (no stem) and rests stay with their own detectors.

```python
from stem_first_client import read_measure_stem_first, in_regions

result = read_measure_stem_first(
    stem, solver, meter="4/4", spacing=10, stems=stems, flags=flags, beams=beams, rests=rests,
    detect_heads=lambda regions: vision.find_heads(regions),   # search only these boxes
    detect_dots=lambda regions: vision.find_dots(regions))
```

In the end-to-end sweeps every stemmed notehead lies inside its planned region, and reading the bar from those regions alone gives exactly the true durations. On the synthetic layout (one event every four staff spaces, denser than real engraving; 4/4, down to 1/64, 300 sampled bars) the notehead and chord regions cover 15% of the bar (10% before the chord regions took both sides of the stem and the quarter-note branch).

### Optimal inference: ask Vision only what the bar does not imply

`infer_measure()` reads a measure from structure alone and asks the Visual Engine only real ambiguities:

1. The Visual Engine reports cheap structure: stems, flags, beams, notehead positions, rests and tuplet brackets. Head fill and augmentation dots are left unchecked (`unchecked=fill,dots`), so a bare stem may be a quarter, a half, or either one dotted.
2. The solver keeps every reading that fills the bar exactly. Events with one surviving duration are `implied`, and nobody looks at them.
3. While several readings remain, one check is asked: the head fill or the dot count of one event. With at most 100 readings the order is exactly optimal (minimax over all readings, by iterative deepening). Above 100 readings, or if the exact planner exceeds its budget, the check with the smallest worst case is chosen.
If a bar is too large for the solver's state budget with features unchecked (hundreds of 1/256 notes), one whole-bar detector pass is requested instead of per-note questions: dots first, then head fill (`batch_checks` in the result).
4. Pitch follows, from the notehead positions (`pitch_inputs` → `pitch_context_engine`).

```python
# Three bare stems in 4/4. Vision: "first and last are quarters".
result = infer_measure(stem, solver, meter="4/4", spacing=10, stems=stems, noteheads=heads,
                       known={"s0": {"duration": "1/4"}, "s2": {"duration": "1/4"}},
                       answer=ask_vision)       # never called here
result["durations"]   # {"s0": "1/4", "s1": "1/2", "s2": "1/4"}  - the half is implied
result["questions"]   # []
```

Without the hint the same bar has 6 readings ((¼ ¼ ½) and (¼ ⅜ ⅜) in any order). No single check splits them 3/3, so the guaranteed minimum is 4 checks; the exact planner achieves it for every possible truth.

Deductions are exact relative to the unchecked range. `unchecked_max_dots` defaults to 2: with 1, a double-dotted half plus a quarter in 9/8 would be "deduced" as a dotted half plus a dotted quarter. If the Visual Engine reports more dots than assumed, the range widens automatically.

The solver accepts any exact rational duration: its grid is the lcm of every candidate denominator, so a 13-tuplet or a 9-tuplet inside a 9-tuplet are exact. Each event also reports `solution_counts` (how many valid readings use each candidate) and `determined`. Exact counts are 64-bit; `valid_assignments_log2` and `solution_counts_log2` carry the same counts as log2 and never saturate (a 2/2 bar of 524 events at 1/256 has about 2^309 readings). DP states are stored in compact sorted layers, and the ranked solution list has a memory budget: when it binds, fewer than `limit` solutions are returned and `solutions_limited_by_memory` is true.

### End-to-end testing over the rhythm space

`tests/score_renderer.py` draws any rhythm of the complete space as Visual Engine primitives. It uses varied pitches and stem directions, flags or beams with partial beams, dots, rests, nested tuplet brackets and occasional chords. `tests/pipeline_sweep.py` then reads every bar back twice:

- with full evidence, STEM-first + solver must return exactly the true durations;
- with fill and dots unchecked, the truth must stay possible, implied events must be correct, and the question loop must reach the truth without asking about implied events or repeating a check.

```bash
python3 tests/pipeline_sweep.py build 2/4 16 "longest=1 max_dots=1 depth=0" all   # every bar
python3 tests/pipeline_sweep.py build 4/4 32 "" sample 300                        # uniform sample
```

Input elements: `stem {id,x1,y1,x2,y2}`, `notehead {id,bbox,fill,fill_confidence}`, `flag {id,bbox,count}`, `beam {id,x1,y1,x2,y2,thickness}`, `dot {id,x,y,confidence}`, `rest {id,bbox,value,dots}` with `value` a power of two, `"B"` or `"measure"`, and `tuplet {id,number,normal,members}`. Each request is one voice of one measure.


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

The solver uses an exact dynamic program over (event index, elapsed ticks) instead of enumerating candidate combinations. Its cost grows with the number of distinct onset positions, so dense measures with many ambiguous events (for example 24 events with three candidates each) are counted and resolved exactly. `valid_assignments` is the exact number of valid readings (`valid_assignments_is_lower_bound` is true only if the count exceeds 64-bit range), every candidate is classified as supported or eliminated, and `solutions` holds the `limit` best readings by visual log-score. `max_nodes` bounds the number of DP states; exceeding it yields `incomplete`.

Durations are exact fractions. The grid starts from 27720 ticks per whole note (lcm of 1..12, the Search Engine grid) times the maximum note denominator, and is extended by the lcm of every candidate denominator, so any rational duration is exact: a 13-tuplet slot, or nested tuplets such as 9 inside 9 (`1/81`). Only durations whose common grid would overflow are rejected.

A candidate may also be `{"duration": "none"}`: the Visual Engine is not sure the symbol exists at all (a duplicated or spurious notehead). It occupies no time; a solution that drops it reports `"present": false` for that event and suggests the `symbol_existence` check.

When no assignment fills the measure, the result says why: `gap` is `missing` (even the longest candidates leave `gap_whole` unfilled: a symbol was probably missed), `excess` (even the shortest candidates overflow by `gap_whole`: a spurious symbol or a missed barline), or `no_exact_combination`. `min_total_whole` and `max_total_whole` are always reported.

Beam groups are an optional preference: pass `beat="1/4"` (or `"3/8"` in compound meters) and `groups=[["n2", "n3"], ...]`, lists of consecutive events under one beam. Engraving starts and ends a beamed group on a beat, so each group edge that falls off the beat multiplies the visual score by `group_penalty` (default 0.2). This tells apart solutions that fill the measure equally well, e.g. which eighth of a measure is really the sixteenth whose short beam was missed. Protocol tokens: `beat=1/4 group_penalty=0.2 group=n2,n3`.

Statuses:

- `resolved_unique` — exactly one valid assignment was found and the search completed.
- `ambiguous` — multiple valid assignments remain.
- `inconsistent` — no assignment satisfies the supplied candidates and measure.
- `incomplete` — the DP state budget (`max_nodes`) was reached; uniqueness cannot be claimed.

The solver returns exact onset positions, candidate support/elimination per event, and suggested visual checks. The Python client attaches caller-provided bounding boxes to those guidance records.

**Scope limitation:** version 1 handles one ordered monophonic voice/part in one measure. It must be called separately for each voice. Polyphonic SATB alignment, pickup measures, ties across barlines, tuplet grouping beyond supplied effective slot durations, and cross-measure constraints require later extensions. Musical validity is not proof that the visual candidate list is correct: if the Visual Engine omitted the true candidate, the solver can report `inconsistent`, but cannot reconstruct missing pixel evidence.


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
- Explicit cross-barline tie carry, scoped by staff, voice, and tie ID, including multi-measure tie chains.
- Visual guidance bounding boxes for ambiguous accidentals, off-grid noteheads, suspicious stem attachments, and unresolved tie relationships.

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
        "tie_id": "tie-1", "tie_from_previous": False, "tie_to_next": True,
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
        staff_id="staff-1",
        voice_id="soprano",
        notes=notes,
        accidentals=accidentals,
        stems=stems,
    )
    print(result["notes"])
    print(result["accidental_associations"])
    print(result["visual_guidance"])

    # Resolve the following measure in the SAME client/process.
    next_measure = engine.resolve(
        clef="treble",
        staff_top_y=100,
        line_spacing=10,
        key_signature="G",
        measure_id="bar-13",
        staff_id="staff-1",
        voice_id="soprano",
        notes=[{
            "id": "n3", "x": 200, "y": 120,
            "tie_id": "tie-1", "tie_from_previous": True,
        }],
    )
    print(next_measure["notes"][0]["alteration_source"])  # tie_carry

    # Optional: clear pending ties when switching scores.
    engine.reset_ties()
```

### Accidental-to-note assignment

Accidental association uses page geometry, not harmony as a hard rule. Candidate noteheads must be to the right of the accidental glyph and within a staff-spacing-based vertical/horizontal window. An accidental is engraved right before its note: a candidate further right is blocked by a candidate at least half a space nearer on the same (or a closer) row, so a repeated note just after an altered note is never a competing candidate. Members of one chord (same `chord_id` or `stem_id`) never block each other, because chord heads are displaced around a shared stem and all their accidentals stand left of the chord. Candidates are ranked by vertical anchor distance, with horizontal distance as a weaker tie-breaker. If the Visual Engine can estimate the symbol's musical anchor independently of its bounding-box center (for example, the bulb/center topology of a flat or the center of a natural), pass it as `anchor_y`.

- A clear geometric winner is assigned.
- If two noteheads have similar geometric scores (for example, the accidental glyph overlaps two staff positions), the result is `ambiguous`; both candidate noteheads are returned for visual reinspection.
- If no notehead fits the association window, the accidental is `orphan`.
- The engine never assigns an accidental merely because doing so would make a chord more consonant.

A new `resolve()` request represents a new measure and starts with an empty local accidental register. Within a measure, a uniquely assigned accidental affects subsequent occurrences of the same letter and octave. Key-signature defaults apply when no local accidental is active.

If an accidental is `ambiguous`, every candidate letter/octave becomes uncertain for the rest of the measure: later notes of that pitch report `pitch_certain=false`, `alteration_source="ambiguous_accidental_memory"`, and include the ambiguous glyph's alteration in `effective_alteration_options`. A later uniquely assigned accidental on the same letter/octave clears that uncertainty.

### Cross-barline ties

Tie state is a separate persistent register; it does not extend measure-local accidental memory. Keep the same `PitchContextClient` process alive while resolving consecutive measures of the same score. Identify each tie chain with a stable `tie_id` and pass the source note with `tie_to_next=True`, then the target note with `tie_from_previous=True`. Pass the same `staff_id` and `voice_id` for all measures in that part.

- The source's fully resolved chromatic pitch is stored, including a local sharp, flat, or natural that overrides the key signature.
- The target inherits that alteration only for its tied note. Other notes of the same letter/octave in the new measure still follow the new measure's accidental register and key signature.
- A target must resolve to the same diatonic letter and octave as its source. A mismatch, missing source, duplicate tie ID, uncertain source pitch, or undetected target is reported for visual review.
- A tie chain spanning multiple measures can use the same `tie_id`; an intermediate note can set both `tie_from_previous=True` and `tie_to_next=True`.
- A cautionary accidental printed at the target is accepted only when it agrees with the carried pitch. A conflicting accidental never overrides the tie source; the source pitch is retained and the result is marked uncertain.
- Use `engine.reset_ties()` when changing scores, or `engine.reset_ties(staff_id="staff-1", voice_id="soprano")` to clear one part.

The engine keeps pending ties in memory for the life of its process. Restarting the process clears this state; persistent on-disk checkpointing is not implemented.

### Important scope and notation limits

This is a geometry-and-pitch context module, not a complete notation interpreter. Each request models one staff/voice and one measure; consecutive requests can share explicit tie state. It does not yet handle octave-transposing clefs/instruments, ottava lines, microtonal accidentals, historical key signatures, multi-staff grand-staff accidental policy, or full chord-voice separation.

Stem checks validate the stem's attachment to the appropriate chord anchor notehead. They do not require every notehead in a chord to touch the same stem: chord noteheads are commonly staggered around one shared stem.

Dissonance is reported as musical context for later modules, never used to reject a visually and notationally plausible pitch.

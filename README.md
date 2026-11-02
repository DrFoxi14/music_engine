# Music Engine

A compact C++17 rhythm-combination engine for exact musical time signatures.

## What it does

- Counts all supported ordered rhythmic patterns for a selected time signature.
- Retrieves a pattern by its zero-based index without storing the entire result set.
- Streams result pages as JSON Lines (JSONL), suitable for bots and other programs.
- Separates patterns containing only notes from patterns containing at least one rest.
- Supports regular notes, single-dotted notes, and complete triplet groups.
- Uses arbitrary-precision non-negative integers for exact counts and large indexes.

The engine builds a dynamic-programming index in memory. It does **not** generate a giant text file or reserve a fixed amount of RAM.

## Project files

- `ritmuri_engine.cpp` — C++17 engine and JSONL command server.
- `ritmuri_client.py` — Python client that keeps the engine process alive.

## Build on Apple Silicon / macOS

```bash
clang++ -std=c++17 -O3 -DNDEBUG -Wall -Wextra -pedantic ritmuri_engine.cpp -o ritmuri_engine
```

Run 4/4 with note values down to 1/256:

```bash
./ritmuri_engine 4 4 256
```

Arguments:

```
./ritmuri_engine <numerator> <denominator> <maximum_note_denominator>
```

The maximum note denominator must be a power of two from 2 through 256.

## JSONL protocol

The process stays running and accepts one command per input line:

```
COUNT NOTES
COUNT RESTS
GET NOTES 0
GET RESTS 12345678901234567890
PAGE NOTES 0 20
PAGE RESTS 100 50
HELP
EXIT
```

- `NOTES`: ordered patterns containing only notes.
- `RESTS`: ordered patterns containing at least one rest.
- `COUNT`: returns an exact count as a decimal string.
- `GET`: reconstructs one pattern by index.
- `PAGE`: returns between 1 and 1000 patterns.
- Indexing starts at zero.
- Each response is one JSON object per line.

## Python client

```python
from ritmuri_client import RhythmEngineClient

with RhythmEngineClient("./ritmuri_engine", 4, 4, 256) as engine:
    print(engine.info)
    print("Patterns without rests:", engine.count("NOTES"))
    print("Pattern 0:", engine.get(0, "NOTES"))
    print("First 25 patterns with rests:", engine.page(0, 25, "RESTS"))
```

Python is only a client. The combinatorial calculations happen in C++.

## Rhythm model and current scope

The internal time grid is exact for normal note values, single dots, and triplets at the configured resolution. The engine currently includes:

- Normal note values from 1/1 to the selected maximum denominator.
- Single-dotted notes and rests.
- Complete triplet groups, with note/rest slot variants in the `RESTS` category.
- All ordered sequences of supported events that exactly fill the measure.
- A full-measure rest event where needed.

Not implemented yet:

- Double or triple dots.
- Tuplets other than triplets (quintuplets, septuplets, etc.).
- Ties across events, accents, articulations, pickups, or overlapping polyrhythms.
- Semantic filters such as “exactly one half note and two quarter notes”.

## Storage and performance

The engine stores dynamic-programming counts for remaining durations, not every possible pattern. This makes arbitrary-index retrieval possible without writing the full combinatorial space to an SSD.

No fixed 25 GB allocation is made: memory use is proportional to the measure resolution and the size of the exact counters.

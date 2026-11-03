# OMR dialogue: Vision ↔ Search Engine

This module implements the Vision specification on synthetic measures:
- **Vision** reports what is in the image and never assigns a duration.
- **The adapter** turns those observations into constraints for `rhythm_search_engine`.
- **The dialogue** asks Vision to re-examine exactly the properties that separate the valid interpretations.

```bash
python3 omr_dialogue/run.py --n=300                   # 300 measures, Vision with deliberate errors
python3 omr_dialogue/run.py --n=150 --scale=0         # perfect Vision: must resolve everything
python3 -m unittest tests.test_omr_dialogue           # acceptance tests (specification, section 8)
```

| File | Role |
|---|---|
| `vision_sim.py` | Synthetic Vision, following the specification. |
| `adapter.py` | Observations → per-event allowed descriptors (`positions` filter of the search engine). |
| `dialogue.py` | The loop of section 5, then `pitch_context_engine`. |
| `synth.py` | Ground truth drawn by `ritmuri_engine` (1/4 … 1/64, triplets, measure rests, keys, accidentals). |
| `run.py` | Evaluation and transcripts. |

## Vision (`vision_sim.py`)

Each measure is reported as JSON with:
- the staff geometry and the barlines;
- every symbol with its ID, bbox, center and order;
- notes: head fill, stem state and direction, flags count, beam group IDs and dots, each with its own confidence;
- rests: visual shape and dots;
- beam groups: member symbols, the beam count at each stem and partial beams;
- tuplet marks: number and members;
- accidentals.

A property that cannot be read is `"unknown"`. Symbols are never invented and uncertain ones are never dropped.

The errors are deliberate (`RATES`). Most misreads are reported as unknown, the rest as a wrong value with a confidence that is not always low. Symbols, beam groups and tuplet marks can be missed.

Requests Vision answers:
- `relook`: symbol IDs plus properties. It answers only those properties, each with value, confidence and evidence.
- `examine_region`: a span of x. It returns symbols it had missed there, or nothing.

## Search Engine interface

`rhythm_search_engine` had only measure-level filters and exact sequences (`prefix`, `suffix`, `contains`). It could not say "event 3 is a quarter or an eighth".

I added one controlled filter: `positions`, the allowed descriptors for each event. It supports alternatives, `*` for anything, and tuplet slot masks (`triplet@8#NRN`).

## The adapter (`adapter.py`)

For each symbol it lists the descriptors that agree with every **certain** property:
- a property is certain if its value is known and its confidence is ≥ 0.85, or if the dialogue confirmed it;
- an unknown property allows all its values.

Each descriptor keeps the visual properties it implies, symbol by symbol. Three symbols under a certain 3-mark form one triplet event. A mark with an unsure number is tried both as a triplet and as no tuplet.

## The dialogue (`dialogue.py`, section 5)

1. **Search** returns every valid interpretation.
2. **Several interpretations:** Vision re-examines the symbol and properties that split them best. A property re-examined twice without an answer is not asked again; if nothing is left to ask, the result is **ambiguous**, with all variants kept.
3. **One interpretation:** before it is accepted, two checks run.
   - Decisive properties read with confidence < 0.97 and not yet confirmed are re-examined.
   - Gaps with more room than the durations need (spacing = a + b·duration, fitted on the measure) are re-examined for a missed symbol.
4. **No interpretation:** the dialogue looks for contradictions, in order:
   - a symbol that contradicts itself;
   - a tuplet number in doubt;
   - certain but unconfirmed properties whose doubt makes the measure possible;
   - three symbols under an unseen triplet mark;
   - confirmed properties;
   - a region where a missed symbol would make the measure possible.

   If nothing is left, the result is a **contradiction report**. Nothing is forced.
5. A triplet mark over fewer than three symbols makes Vision re-examine that region.
6. After the rhythm is settled, `pitch_context_engine` gives the pitches. Unsure accidentals are re-examined first.

## Results (300 measures, default errors, 3 Nov 2026)

| Shortest value | Resolved right: one pass → dialogue | Resolved wrong: one pass → dialogue | Contradiction reported |
|---|---|---|---|
| all | 32.0% → 70.7% | 18 → 8 | 78 |
| 1/4 | 57.9% → 94.7% | 1 → 1 | 1 |
| 1/8 | 61.2% → 87.5% | 3 → 2 | 8 |
| 1/16 | 22.1% → 68.4% | 9 → 1 | 29 |
| 1/32 | 8.7% → 56.5% | 2 → 1 | 18 |
| 1/64 | 0% → 36.6% | 3 → 3 | 22 |

"One pass" means the same adapter and search, with every reported value taken as certain and no question asked.

With a perfect Vision, all 150 measures are resolved right.

Engine fixes made while building this:
- `rhythm_search_engine`:
  - `positions` candidates are computed once per position (10× faster);
  - `prefix` is enforced while searching;
  - all-note tuplets are allowed in RESTS patterns.
- `pitch_context_engine`: a nearer note at the same staff position blocks a later one from taking the accidental.

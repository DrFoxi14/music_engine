# Training lab: the vision ↔ music-engine dialogue

This lab checks, on synthetic measures, how a vision model talks with the four engines, separately from any real OMR. The vision makes mistakes on purpose; the engines push back; the vision looks again where they point.

```bash
python3 training/run.py --train=400 --test=400            # calibrate on 400 measures, test on 400 new ones
python3 training/run.py --scale=0                           # perfect vision: must give 100%
python3 training/run.py --scale=2 --set=AMBIGUOUS=0.5       # twice the errors; any dialogue.py constant
python3 -m unittest tests.test_training_dialogue            # every message type on hand-made measures
```

| File | Role |
|---|---|
| `synth.py` | Ground truth. `ritmuri_engine` counts every rhythm of the meter and returns one at a random index: a uniform draw over all of them, at a resolution (shortest value) from 1/4 to 1/64. Pitches come with a key signature, accidentals and their measure-local memory. |
| `vision_sim.py` | The imperfect vision. It reads head fill, stem, beams/flags, dots, the triplet "3", staff step, accidental and rest shape, each with a confidence. Rates are in `RATES`. It can also be asked to `relook` one feature (a closer look, 5× fewer errors) or to `search` between two x positions for a missed symbol. |
| `dialogue.py` | The conversation, see below. Every message is logged. |
| `engines.py` | Starts the engines through the repository's Python clients. |
| `run.py` | Training (calibration of the vision's confidences, then of its relooks), testing, report, and transcripts. |

## The conversation, per measure

1. **vision → rhythm `observation`:** each symbol with its features and confidences.
2. **rhythm → vision `verdict`:**
   - `rhythm_search_engine` says whether the exact sequence is a valid measure.
   - `omr_constraint_solver` gives the most likely readings that fill the measure, built from every symbol's alternatives, plus "a symbol was missed".
   - `rhythm_search_engine` keeps only the readings that are music (`filter`), for example a triplet needs three slots.
3. **rhythm → vision `relook`:** the features that separate the vision's reading from the valid ones. **vision → rhythm `relook_result`.**
4. **rhythm → vision `search`:** time is missing.
   - The place is where the spacing leaves the most room (fitted width = a + b·duration).
   - The message says which symbols would fit there (`rhythm_search_engine` with the known beginning and end).
   - **vision → rhythm `search_result`.**
5. Steps 2–4 repeat until the reading is valid and no other valid reading is close. Without the vision's confirmation nothing is changed: taking the solver's guess cost 9 points of notes.
6. **pitch → vision `relook`:** unsure staff positions and accidentals are looked at again.
7. **rhythm → pitch `resolve`:** `pitch_context_engine` gives letter, octave and alteration.

## Results

400 test measures, default error rates, 2 Nov 2026 (`out/results.json`):

| Shortest value | Measures exact, vision → dialogue | Durations right | Notes identical (duration + pitch) |
|---|---|---|---|
| all | 10.2% → 44.5% | 73.7% → 87.1% | 66.9% → 83.3% |
| 1/4 | 31.9% → 89.4% | 71.8% → 96.6% | 66.7% → 94.9% |
| 1/8 | 23.9% → 87.5% | 70.3% → 96.0% | 65.7% → 92.3% |
| 1/16 | 5.7% → 47.7% | 72.8% → 92.2% | 65.9% → 88.8% |
| 1/32 | 0% → 15.6% | 73.3% → 87.5% | 66.8% → 83.3% |
| 1/64 | 0% → 2.5% | 74.6% → 83.9% | 67.4% → 80.1% |

With a perfect vision the result is 150/150 measures and 100% of notes. The dialogue costs about 18 relooks and 0.5 searches per measure, mostly in the dense measures.

Dense measures (1/32, 1/64, ~40 symbols, ~10 wrong) stay hard: one arithmetic constraint per measure cannot place ten errors. The next constraint to add is beat groups, which real pages carry as beams. A beam group spans whole beats, so each beat becomes its own small measure.

Fixed in the engines while building this:
- `rhythm_search_engine` enforced `prefix` only at the end. A "what follows?" query with rests ran out of nodes.
- `rhythm_search_engine` dropped all-note triplets from RESTS patterns, so a measure with a rest and a normal triplet could not be found.
- `pitch_context_engine` called a flat "ambiguous" between its note and a later note at the same pitch, so neither note got the flat.

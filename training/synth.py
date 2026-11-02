"""Synthetic ground truth for the vision <-> music-engine dialogue.

A measure is drawn uniformly from *every* possible rhythm of its meter: the
ritmuri engine counts them (COUNT) and returns the pattern with a random index
(GET), so nothing is hand-picked. The resolution (the shortest note value
allowed, 1/4 .. 1/64) is drawn per measure, so easy and very dense measures are
mixed. Pitches get a key signature, accidentals with measure-local memory, and
staff geometry, as a printed page would have them.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from fractions import Fraction

from engines import Engines

METERS = [(2, 4), (3, 4), (4, 4), (3, 8), (6, 8), (2, 2)]
RESOLUTIONS = [4, 8, 16, 32, 64]
KEYS = {"C": 0, "G": 1, "D": 2, "A": 3, "E": 4, "F": -1, "Bb": -2, "Eb": -3, "Ab": -4}
SHARP_ORDER, FLAT_ORDER = "FCGDAEB", "BEADGCF"
LETTERS = "CDEFGAB"
STAFF_TOP, SPACING = 100.0, 10.0          # treble staff: top line F5, bottom line E4


@dataclass
class Symbol:
    """One printed symbol: a notehead (with stem/beams/dots) or a rest."""
    rest: bool
    den: int                     # base value: 1 whole, 2 half, 4 quarter, 8 eighth ...
    dots: int = 0
    tuplet: tuple | None = None  # (group id, slot index, 3, 2) inside a triplet
    step: int = 0                # staff step from the bottom line (notes)
    alter: int = 0               # sounding alteration
    accidental: str | None = None  # printed accidental, if any
    x: float = 0.0
    measure: Fraction | None = None   # a whole-measure rest: drawn as a whole rest, lasts the measure

    @property
    def duration(self) -> Fraction:
        if self.measure is not None:
            return self.measure
        d = Fraction(1, self.den) * (Fraction(3, 2) if self.dots else 1)
        return d * Fraction(2, 3) if self.tuplet else d

    @property
    def beams(self) -> int:
        """Beams or flags on the stem: 0 for a quarter or longer, 1 for an eighth ..."""
        return max(0, self.den.bit_length() - 3)

    def pitch(self) -> tuple[str, int, int]:
        letter, octave = _letter_octave(self.step)
        return letter, octave, self.alter


@dataclass
class Measure:
    meter: tuple[int, int]
    resolution: int
    key: str
    symbols: list[Symbol] = field(default_factory=list)

    @property
    def capacity(self) -> Fraction:
        return Fraction(self.meter[0], self.meter[1])


def _letter_octave(step: int) -> tuple[str, int]:
    k = step + 2 + 4 * 7            # bottom line of the treble staff is E4
    return LETTERS[k % 7], k // 7


def key_alteration(key: str, letter: str) -> int:
    f = KEYS[key]
    if f > 0 and letter in SHARP_ORDER[:f]:
        return 1
    if f < 0 and letter in FLAT_ORDER[:-f]:
        return -1
    return 0


def _symbols_of(pattern: dict, capacity: Fraction) -> list[Symbol]:
    out, group = [], 0
    for e in pattern["events"]:
        if e["kind"] == "measure_rest":
            out.append(Symbol(rest=True, den=1, measure=capacity))
            continue
        kind, den = e["kind"], int(e["denominator"])
        if kind == "triplet":
            group += 1
            for i, slot in enumerate(e["slots"]):
                out.append(Symbol(rest=slot == "rest", den=den, tuplet=(group, i, 3, 2)))
        else:
            out.append(Symbol(rest=kind.endswith("rest"), den=den, dots=int(kind.startswith("dotted"))))
    return out


def _pitches(m: Measure, rng: random.Random) -> None:
    memory: dict[tuple[str, int], int] = {}
    step = rng.randint(0, 8)
    x = 40.0
    for s in m.symbols:
        s.x = x
        x += 12.0 + 120.0 * float(s.duration)
        if s.rest:
            continue
        step = max(-5, min(13, step + rng.choice([-2, -1, -1, 0, 1, 1, 2, 3, -3])))
        s.step = step
        letter, octave = _letter_octave(step)
        current = memory.get((letter, octave), key_alteration(m.key, letter))
        s.alter = current if rng.random() < 0.85 else rng.choice([a for a in (-1, 0, 1) if a != current])
        if s.alter != current:
            s.accidental = {-1: "flat", 0: "natural", 1: "sharp"}[s.alter]
            memory[(letter, octave)] = s.alter


def make_measure(engines: Engines, rng: random.Random, meter=None, resolution=None, rests=None) -> Measure:
    meter = meter or rng.choice(METERS)
    resolution = resolution or rng.choice([r for r in RESOLUTIONS if r >= meter[1]])
    category = ("RESTS" if rng.random() < 0.5 else "NOTES") if rests is None else ("RESTS" if rests else "NOTES")
    rit = engines.ritmuri(meter, resolution)
    pattern = rit.get(rng.randrange(engines.count(meter, resolution, category)), category)
    m = Measure(meter, resolution, rng.choice(list(KEYS)), _symbols_of(pattern, Fraction(*meter)))
    assert sum((s.duration for s in m.symbols), Fraction(0)) == m.capacity
    _pitches(m, rng)
    return m

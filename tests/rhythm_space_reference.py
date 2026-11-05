"""Independent brute-force enumerator of the rhythm grammar.

Used only by tests to cross-check the C++ counting/decoding. It enumerates
every notation explicitly, so it is meant for small configurations.
"""

from __future__ import annotations

from functools import lru_cache


def is_pow2(x: int) -> bool:
    return x > 0 and x & (x - 1) == 0


def conventional_normals(a: int) -> list[int]:
    if not is_pow2(a):
        p = 1
        while p * 2 < a:
            p *= 2
        return [p]
    out = [a * 3 // 2]
    if a >= 4:
        out.append(a * 3 // 4)
    return out


def value_name(e: int) -> str:
    return {-2: "L", -1: "B"}.get(e, str(1 << e) if e >= 0 else "?")


class Reference:
    def __init__(self, num, den, max_den, max_dots=2, depth=2, max_actual=12,
                 max_normal=16, unit_dots=1, longest=2, min_tuplet_items=2,
                 measure_rest=True, ratios=None, tuplet_normals="standard"):
        self.G = max_den * (1 << max_dots)
        self.M = num * self.G // den
        e_min = {1: 0, 2: -1, 4: -2}[longest]
        e_max = max_den.bit_length() - 1

        def ticks(e, k):
            base = self.G >> e if e >= 0 else self.G << (-e)
            return base * ((2 << k) - 1) // (1 << k)

        self.simple = []
        for e in range(e_min, e_max + 1):
            for k in range(max_dots + 1):
                t = ticks(e, k)
                name = value_name(e) + "." * k
                self.simple.append(("n" + name, t, False))
                self.simple.append(("r" + name, t, True))
        if ratios is None and tuplet_normals == "any":
            ratios = [(a, n) for a in range(2, max_actual + 1)
                      for n in range(1, max_normal + 1) if n != a]
        elif ratios is None:
            ratios = [(a, n) for a in range(2, max_actual + 1)
                      for n in conventional_normals(a) if n <= max_normal]
        self.tuplets = []
        for a, n in ratios:
            for e in range(e_min, e_max + 1):
                for k in range(min(unit_dots, max_dots) + 1):
                    u = ticks(e, k)
                    self.tuplets.append((a, n, u * n, u * a))
        self.depth = depth
        self.min_items = min_tuplet_items
        self.measure_rest = measure_rest and self.M != self.G

    @lru_cache(maxsize=None)
    def sequences(self, level: int, total: int, top: bool = False):
        """All item sequences of nominal length total.

        Each sequence is (items, has_rest) where items is a tuple of
        (notation_text, nominal_ticks) pairs.
        """
        if total == 0:
            return [((), False)]
        out = []
        for text, span, rest in self.simple:
            if span <= total:
                for tail, tail_rest in self.sequences(level, total - span, top):
                    out.append((((text, span),) + tail, rest or tail_rest))
        if top and self.measure_rest and total == self.M:
            out.append(((("M", self.M),), True))
        if level < self.depth:
            for a, n, span, content in self.tuplets:
                if span > total:
                    continue
                inner = [s for s in self.sequences(level + 1, content)
                         if len(s[0]) >= self.min_items]
                if not inner:
                    continue
                tails = self.sequences(level, total - span, top)
                for body, body_rest in inner:
                    item = (f"[{a}:{n} " + " ".join(t for t, _ in body) + "]", span)
                    for tail, tail_rest in tails:
                        out.append(((item,) + tail, body_rest or tail_rest))
        return out

    def measure(self):
        return self.sequences(0, self.M, True)


def matches_template(items, tokens, ticks_per_whole):
    """Does a top-level item list match a template token list?

    Tokens: concrete notation text, "?", "*", "+", or "*=a/b".
    """
    from fractions import Fraction

    expanded = []
    for tok in tokens:
        if tok == "+":
            expanded += ["?", "*"]
        else:
            expanded.append(tok)

    @lru_cache(maxsize=None)
    def go(i, k):
        if k == len(expanded):
            return i == len(items)
        tok = expanded[k]
        if tok == "*":
            return go(i, k + 1) or (i < len(items) and go(i + 1, k))
        if tok == "?":
            return i < len(items) and go(i + 1, k + 1)
        if tok.startswith("*="):
            need = Fraction(tok[2:]) * ticks_per_whole
            total, j = 0, i
            while j < len(items) and total < need:
                total += items[j][1]
                j += 1
                if total == need and go(j, k + 1):
                    return True
            return False
        return i < len(items) and items[i][0] == tok and go(i + 1, k + 1)

    return go(0, 0)

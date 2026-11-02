"""Train and test the vision <-> music-engine dialogue on synthetic measures.

  python3 training/run.py [--train=300] [--test=300] [--scale=1.0] [--seed=1] [--out=training/out]

Train: the vision reads training measures; every feature it reports is checked against the truth and
the confidences are calibrated (Calibration), then dialogues on the same measures calibrate the relooks.
Test: new measures (another seed), never seen in training. For each one: the raw vision reading, and the
reading after the dialogue, both through pitch_context_engine. Reported:
  measures with the exact rhythm, notes identical (duration and pitch, LCS per measure), what the
  dialogue cost (relooks, searches), by resolution (shortest value 1/4 .. 1/64).
Every test dialogue is written to <out>/transcripts.jsonl; the first ones also as text in <out>/dialogues.txt.
"""
from __future__ import annotations

import json
import random
import sys
import time
from collections import Counter, defaultdict
from fractions import Fraction
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from dialogue import Calibration, Dialogue, duration       # noqa: E402
from engines import Engines                                 # noqa: E402
from synth import make_measure                              # noqa: E402
from vision_sim import VisionSim                            # noqa: E402


def arg(k, d):
    return type(d)(next((a.split("=", 1)[1] for a in sys.argv if a.startswith(f"--{k}=")), d))


def info(m):
    return {"meter": m.meter, "capacity": m.capacity, "key": m.key,
            "x_start": m.symbols[0].x - 12.0, "x_end": m.symbols[-1].x + 12.0 + 120.0 * float(m.symbols[-1].duration)}


def truth_items(m):
    return [(s.rest, s.duration, None if s.rest else (*_lo(s.step), s.alter)) for s in m.symbols]


def _lo(step):
    from synth import _letter_octave
    return _letter_octave(step)


def read_items(obs, pitches):
    return [(o["kind"] == "rest", duration(o), None if o["kind"] == "rest" else pitches.get(o["sid"])) for o in obs]


def lcs(a, b):
    t = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a)):
        for j in range(len(b)):
            t[i + 1][j + 1] = t[i][j] + 1 if a[i] == b[j] else max(t[i][j + 1], t[i + 1][j])
    return t[-1][-1]


def score(truth, read):
    notes_t = [(d, p) for r, d, p in truth if not r]
    notes_r = [(d, p) for r, d, p in read if not r]
    return {"rhythm_exact": [(r, d) for r, d, _ in truth] == [(r, d) for r, d, _ in read],
            "notes": len(notes_t), "identical": lcs(notes_t, notes_r),
            "dur_ok": lcs([d for d, _ in notes_t], [d for d, _ in notes_r])}


def train(E, n, seed, scale):
    rng = random.Random(seed)
    recs, missing = [], 0
    ms = [make_measure(E, rng) for _ in range(n)]
    for m in ms:                                   # pass 1: first looks
        v = VisionSim(m, random.Random(rng.random()), scale=scale)
        obs = v.observe()
        missing += len({o["sid"] for o in obs}) < len(m.symbols)
        for o in obs:
            tf = v.true_features(o["sid"])
            for f, (val, c) in o["features"].items():
                if f in tf:
                    recs.append((f, c, val == tf[f], False))
    cal = Calibration.fit(recs, p_missing=max(missing / n, 0.01))
    for m in ms:                                   # pass 2: relooks inside dialogues
        v = VisionSim(m, random.Random(rng.random()), scale=scale)

        def rec(o, f, c, val, v=v):
            tf = v.true_features(o["sid"])
            if f in tf:
                recs.append((f, c, val == tf[f], True))
        d = Dialogue(E, cal, record=rec)
        obs, _ = d.run(info(m), v)
        d.pitches(info(m), obs, mid="t", vision=v)
    return Calibration.fit(recs, p_missing=max(missing / n, 0.01))


def describe(log):
    lines = []
    for msg in log:
        body = {k: v for k, v in msg.items() if k not in ("round", "from", "to", "type")}
        if msg["type"] == "observation":
            body = {"symbols": [" ".join(f"{k}={v[0] if isinstance(v, list) else v}" for k, v in s.items()
                                         if k not in ("exists",)) for s in body["symbols"]]}
        lines.append(f"  [{msg['round']}] {msg['from']} -> {msg['to']}: {msg['type']} {json.dumps(body, ensure_ascii=False)}")
    return "\n".join(lines)


def main():
    import dialogue as D
    for kv in [a[6:] for a in sys.argv if a.startswith("--set=")]:
        for item in kv.split(","):
            k, v = item.split("=")
            cur = getattr(D, k)
            setattr(D, k, v in ("1", "true", "True") if isinstance(cur, bool) else type(cur)(v))
    n_train, n_test, scale, seed = arg("train", 300), arg("test", 300), arg("scale", 1.0), arg("seed", 1)
    out = Path(arg("out", str(HERE / "out")))
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    with Engines() as E:
        cal = train(E, n_train, seed, scale)
        (out / "calibration.json").write_text(cal.to_json())
        rng = random.Random(seed + 1000)
        tot = defaultdict(Counter)
        status = Counter()
        shown = 0
        with open(out / "transcripts.jsonl", "w") as tj, open(out / "dialogues.txt", "w") as tx:
            for k in range(n_test):
                m = make_measure(E, rng)
                v = VisionSim(m, random.Random(rng.random()), scale=scale)
                d = Dialogue(E, cal)
                obs, st = d.run(info(m), v)
                status[st] += 1
                raw = [o for o in d.initial if o["features"]["exists"]["v"]]
                for o in raw:
                    o["alone_in"] = m.capacity if len(raw) == 1 else None
                raw_p = Dialogue(E, cal, log=[]).pitches(info(m), raw, mid=f"r{k}")
                fin_p = d.pitches(info(m), obs, mid=str(k), vision=v)
                t = truth_items(m)
                for tag, sc in (("vision", score(t, read_items(raw, raw_p))), ("dialogue", score(t, read_items(obs, fin_p)))):
                    for lvl in ("all", f"1/{m.resolution}"):
                        c = tot[(tag, lvl)]
                        c["measures"] += 1
                        c["rhythm_exact"] += sc["rhythm_exact"]
                        c["notes"] += sc["notes"]
                        c["identical"] += sc["identical"]
                        c["dur_ok"] += sc["dur_ok"]
                tot[("cost", "all")]["relooks"] += v.relooks
                tot[("cost", "all")]["searches"] += v.searches
                tj.write(json.dumps({"measure": k, "meter": m.meter, "resolution": m.resolution, "status": st,
                                     "truth": [[r, str(dd), p] for r, dd, p in t], "log": d.log},
                                    ensure_ascii=False, default=str) + "\n")
                if shown < 25 and (v.relooks or v.searches):
                    shown += 1
                    tx.write(f"=== measure {k}: {m.meter[0]}/{m.meter[1]}, shortest 1/{m.resolution}, status {st}\n")
                    tx.write("truth: " + " ".join(("r" if r else "n") + str(dd) for r, dd, _ in t) + "\n")
                    tx.write(describe(d.log) + "\n\n")
    res = {"train": n_train, "test": n_test, "scale": scale, "seconds": round(time.time() - t0, 1),
           "status": dict(status), "cost_per_measure": {k: round(v / n_test, 2) for k, v in tot[("cost", "all")].items()}}
    for (tag, lvl), c in sorted(tot.items()):
        if tag == "cost":
            continue
        res.setdefault(lvl, {})[tag] = {"rhythm_exact": f"{c['rhythm_exact']}/{c['measures']}",
                                        "rhythm_exact_pct": round(100 * c["rhythm_exact"] / c["measures"], 1),
                                        "durations_ok_pct": round(100 * c["dur_ok"] / max(c["notes"], 1), 1),
                                        "notes_identical_pct": round(100 * c["identical"] / max(c["notes"], 1), 1)}
    (out / "results.json").write_text(json.dumps(res, indent=1))
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()

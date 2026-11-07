"""Evaluate the Vision <-> Search dialogue on synthetic measures.

  python3 omr_dialogue/run.py [--n=300] [--scale=1.0] [--seed=7] [--out=omr_dialogue/out]

For every measure: the synthetic Vision observes it (with deliberate errors), the dialogue runs, and
the result is compared with the truth. Reported, overall and by resolution (shortest value):
  resolved     one interpretation left;  of these, how many are right (precision) and how many wrong
  ambiguous    several left and Vision cannot separate them; whether the truth is among them
  contradiction / unresolved
  notes identical   duration and pitch right, counted only for resolved measures
  one pass     the same adapter and search without any question to Vision (the baseline)
  cost         re-examination requests, properties re-read, regions re-examined
Every dialogue goes to <out>/transcripts.jsonl; a few readable ones to <out>/dialogues.txt.
"""
from __future__ import annotations

import json
import random
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import adapter as A                                          # noqa: E402
from dialogue import Dialogue, durations_of                 # noqa: E402
from engines import Engines                                 # noqa: E402
from synth import METERS, _letter_octave, make_measure              # noqa: E402
from vision_sim import VisionSim                            # noqa: E402

WEIGHTS = {4: 0.2, 8: 0.3, 16: 0.25, 32: 0.15, 64: 0.1}


def arg(k, d):
    return type(d)(next((a.split("=", 1)[1] for a in sys.argv if a.startswith(f"--{k}=")), d))


def truth(vision):
    g = vision.g
    out = {}
    for x in g.symbols:
        s = g.m.symbols[x["index"]]
        out[x["id"]] = (s.duration if s.measure is None else None, None if s.rest else (*_letter_octave(s.step), s.alter))
    return out


def rhythm_right(variant, t):
    d = durations_of(variant)
    return set(d) == set(t) and all(d[k] == t[k][0] for k in t)


def one_pass(E, meter, vision_obs):
    """The baseline: every reported value taken as certain, no question asked."""
    keep = A.CERTAIN
    A.CERTAIN = 0.0
    try:
        d = Dialogue(E)
        syms = A.parse(json.loads(json.dumps(vision_obs))["measures"][0])
        vs, more = d.interpretations(meter, syms)
    finally:
        A.CERTAIN = keep
    return vs[0] if len(vs) == 1 and not more else None


def describe(log):
    lines = []
    for m in log:
        body = {k: v for k, v in m.items() if k not in ("from", "to", "type")}
        if m["type"] == "observation":
            ms = body["measure"]
            body = {"symbols": len(ms["symbols"]), "beam_groups": len(ms["beam_groups"]), "tuplets": len(ms["tuplets"])}
        lines.append(f"  {m['from']} -> {m['to']}: {m['type']} {json.dumps(body, ensure_ascii=False, default=str)[:400]}")
    return "\n".join(lines)


def main():
    n, scale, seed = arg("n", 300), arg("scale", 1.0), arg("seed", 7)
    out = Path(arg("out", str(HERE / "out")))
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    t0 = time.time()
    tot = defaultdict(Counter)
    with Engines() as E, open(out / "transcripts.jsonl", "w") as tj, open(out / "dialogues.txt", "w") as tx:
        shown = 0
        for k in range(n):
            meter = rng.choice(METERS)
            ok_res = [r for r in WEIGHTS if r >= meter[1]]
            res = rng.choices(ok_res, weights=[WEIGHTS[r] for r in ok_res])[0]
            m = make_measure(E, rng, meter=meter, resolution=res)
            v = VisionSim(m, random.Random(rng.random()), scale=scale)
            t = truth(v)
            first = v.observe()
            v_dialog = v                                   # the same vision answers the dialogue's requests
            v_dialog.observe = lambda first=first: json.loads(json.dumps(first))
            base = one_pass(E, m.meter, first)
            d = Dialogue(E)
            r = d.run(m.meter, v_dialog)
            notes = sum(1 for x in t.values() if x[1] is not None)
            identical = 0
            ok = r["status"] == "resolved" and rhythm_right(r["variants"][0], t)
            if r["status"] == "resolved":
                dur = durations_of(r["variants"][0])
                p = d.pitches(m.key, r, v_dialog)
                identical = sum(1 for sid, (dd, pp) in t.items() if pp is not None and dur.get(sid) == dd and p.get(sid) == pp)
            in_variants = r["status"] == "ambiguous" and any(rhythm_right(x, t) for x in r["variants"])
            for lvl in ("all", f"1/{m.resolution}"):
                c = tot[lvl]
                c["measures"] += 1
                c[r["status"]] += 1
                c["resolved_right"] += ok
                c["resolved_wrong"] += r["status"] == "resolved" and not ok
                c["ambiguous_truth_inside"] += in_variants
                c["one_pass_right"] += base is not None and rhythm_right(base, t)
                c["one_pass_wrong"] += base is not None and not rhythm_right(base, t)
                c["notes"] += notes
                c["identical"] += identical
                c["requests"] += v_dialog.requests
                c["properties_reread"] += v_dialog.relooked
                c["regions"] += v_dialog.regions
            tj.write(json.dumps({"measure": k, "meter": m.meter, "resolution": m.resolution, "status": r["status"],
                                 "right": ok, "log": d.log}, default=str) + "\n")
            if shown < 20 and v_dialog.requests and len(m.symbols) <= 8:
                shown += 1
                tx.write(f"=== measure {k}: {m.meter[0]}/{m.meter[1]}, shortest 1/{m.resolution}, {r['status']}"
                         f"{' (right)' if ok else ''}\n")
                tx.write("truth: " + " ".join(f"{sid}={x[0]}" for sid, x in t.items()) + "\n")
                tx.write(describe(d.log) + "\n\n")
    report = {"n": n, "scale": scale, "seconds": round(time.time() - t0, 1)}
    for lvl, c in sorted(tot.items(), key=lambda kv: (kv[0] != "all", len(kv[0]), kv[0])):
        mm = c["measures"]
        report[lvl] = {
            "measures": mm,
            "resolved_right_pct": round(100 * c["resolved_right"] / mm, 1),
            "resolved_wrong": c["resolved_wrong"],
            "ambiguous": c["ambiguous"], "ambiguous_truth_inside": c["ambiguous_truth_inside"],
            "contradiction": c["contradiction"], "unresolved": c["unresolved"],
            "one_pass_right_pct": round(100 * c["one_pass_right"] / mm, 1), "one_pass_wrong": c["one_pass_wrong"],
            "notes_identical_pct": round(100 * c["identical"] / max(c["notes"], 1), 1),
            "requests_per_measure": round(c["requests"] / mm, 2),
            "properties_reread_per_measure": round(c["properties_reread"] / mm, 2),
            "regions_per_measure": round(c["regions"] / mm, 2),
        }
    (out / "results.json").write_text(json.dumps(report, indent=1))
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()

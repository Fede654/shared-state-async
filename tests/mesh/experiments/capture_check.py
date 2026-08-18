"""Capture-on/off timing equivalence check (Phase 1, pre-committed gate).

Design, fixed by the plan BEFORE measurement: 3 paired runs of the same
probed Cell-A-parameter cell; each pair is one capture-on and one
capture-off run with within-pair order randomized from a recorded seed.

Metrics — sources available in BOTH arms (plan amendment: the original
"exchange-completion time" is not derivable in the capture-off arm,
because the per-exchange RS_DBG3 lines are compiled out at the binary's
debug level 2; the timestamped per-merge line at sharedstate.cc:910 IS
compiled in and fires once per handled gossip slice):
  m1  per-node inter-gossip-merge interval, median  (from daemon logs)
  m2  per-node inter-gossip-merge interval, p95
  m3  probe response latency, median                (from probe timings)
  m4  probe response latency, p95

Equivalence margins (fail-closed): medians within ±10% of the paired
capture-off value, p95 within ±20%, on every metric in every pair. A
failure means capture is treated as invasive and the capture
configuration is investigated — the margin is never loosened.

Usage: python3 capture_check.py [--seed 20260818] [--window 480]
Writes results/post-expiry-v2/capture-check-<stamp>.json.
"""

import json
import os
import random
import statistics
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "results", "post-expiry-v2")

PAIRS = 3
MEDIAN_MARGIN = 0.10
P95_MARGIN = 0.20


def p95(xs):
    xs = sorted(xs)
    return xs[int(0.95 * (len(xs) - 1))] if xs else None


def run_one(on, window, run_id):
    argv = [sys.executable, os.path.join(HERE, "post_expiry_v2.py"),
            "--arm", "both" if on else "probes",
            "--window", str(window), "--tag", "capcheck",
            "--run-id", run_id]
    if not on:
        argv.append("--no-capture")
    r = subprocess.run(argv, capture_output=True, text=True)
    path = os.path.join(RESULTS, run_id + ".json")
    if not os.path.exists(path):
        raise RuntimeError(f"{run_id}: no record written\n{r.stdout[-2000:]}"
                           f"\n{r.stderr[-2000:]}")
    with open(path) as f:
        return json.load(f)


def metrics(rec):
    intervals = []
    for node, m in rec["merge_events"].items():
        ts = m["gossip_merge_ts_unix"]
        intervals += [round(b - a, 3) for a, b in zip(ts, ts[1:])]
    lat = []
    for row in rec["series"]:
        for obs in row["obs"].values():
            if obs["ok"]:
                lat.append(round(obs["done"] - obs["start"], 3))
    return {"merge_interval_median": (round(statistics.median(intervals), 3)
                                      if intervals else None),
            "merge_interval_p95": p95(intervals),
            "probe_latency_median": (round(statistics.median(lat), 3)
                                     if lat else None),
            "probe_latency_p95": p95(lat),
            "n_intervals": len(intervals), "n_probes": len(lat)}


def within(on, off, margin):
    if on is None or off is None or off == 0:
        return False
    return abs(on - off) / off <= margin


if __name__ == "__main__":
    def opt(name, default, cast=int):
        return cast(sys.argv[sys.argv.index(name) + 1]) \
            if name in sys.argv else default

    seed = opt("--seed", 20260818)
    window = opt("--window", 480)
    rng = random.Random(seed)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())

    pairs = []
    for i in range(PAIRS):
        order = ["on", "off"]
        rng.shuffle(order)
        pairs.append(order)
    print(f"capture-check: seed={seed} window={window}s pair orders={pairs}")

    results = []
    for i, order in enumerate(pairs):
        pair = {"order": order}
        for which in order:
            on = which == "on"
            run_id = f"capcheck-p{i+1}-{'on' if on else 'off'}-{stamp}"
            print(f"  pair {i+1}: running capture-{which} ({run_id}) ...")
            rec = run_one(on, window, run_id)
            pair[which] = {"run_id": run_id,
                           "record_valid_acquisition":
                               rec["record_valid_acquisition"],
                           "metrics": metrics(rec)}
        results.append(pair)

    verdicts = []
    for i, pair in enumerate(results):
        mon, moff = pair["on"]["metrics"], pair["off"]["metrics"]
        v = {
            "pair": i + 1,
            "merge_interval_median_ok": within(
                mon["merge_interval_median"], moff["merge_interval_median"],
                MEDIAN_MARGIN),
            "merge_interval_p95_ok": within(
                mon["merge_interval_p95"], moff["merge_interval_p95"],
                P95_MARGIN),
            "probe_latency_median_ok": within(
                mon["probe_latency_median"], moff["probe_latency_median"],
                MEDIAN_MARGIN),
            "probe_latency_p95_ok": within(
                mon["probe_latency_p95"], moff["probe_latency_p95"],
                P95_MARGIN),
            "acquisition_valid": (pair["on"]["record_valid_acquisition"]
                                  and pair["off"]
                                  ["record_valid_acquisition"]),
        }
        v["ok"] = all(v[k] for k in v if k.endswith("_ok")) \
            and v["acquisition_valid"]
        verdicts.append(v)

    report = {
        "seed": seed, "window_s": window, "pairs": PAIRS,
        "median_margin": MEDIAN_MARGIN, "p95_margin": P95_MARGIN,
        "pair_orders": pairs, "results": results, "verdicts": verdicts,
        "equivalence_ok": all(v["ok"] for v in verdicts),
    }
    dest = os.path.join(RESULTS, f"capture-check-{stamp}.json")
    with open(dest, "w") as f:
        json.dump(report, f, indent=1, sort_keys=True)
    print(json.dumps(verdicts, indent=1))
    print(f"{dest}\nEQUIVALENCE OK: {report['equivalence_ok']}")
    sys.exit(0 if report["equivalence_ok"] else 1)

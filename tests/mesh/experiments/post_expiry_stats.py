#!/usr/bin/env python3
"""Aggregate statistics over every post-expiry record.

Pure JSON-in, text-out: no namespace, no binary, no re-measurement.
Records are grouped by the cell that produced them — (configured insert
TTL, window, arm) — so batches with different parameters are never
pooled. Only `record_valid` records are counted; invalid ones are
listed, not silently dropped.

Every proportion carries a Wilson 95% interval. With n of 3-8 these are
wide, and that width IS the finding — printing a bare "5/5" invites a
certainty the sample does not hold.
"""

import glob
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "results", "post-expiry")


def wilson(k, n, z=1.96):
    """95% score interval for a binomial proportion."""
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def fmt_ci(k, n):
    ci = wilson(k, n)
    if ci is None:
        return "n=0"
    return f"{k}/{n} = {k / n:.0%}  (95% CI {ci[0]:.0%}-{ci[1]:.0%})"


def med(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else None


def load():
    groups = {}
    invalid = []
    for path in sorted(glob.glob(os.path.join(RESULTS, "*.json"))):
        with open(path) as f:
            rec = json.load(f)
        name = os.path.basename(path)
        if not rec.get("record_valid"):
            invalid.append(name)
            continue
        key = (rec["configured_insert_ttl"], rec["window_s"],
               "control" if rec.get("control_light_sampling") else "treatment")
        groups.setdefault(key, []).append((name, rec))
    return groups, invalid


def main():
    groups, invalid = load()
    print("# Post-expiry sweep — aggregate statistics\n")
    if invalid:
        print(f"EXCLUDED (record_valid false): {len(invalid)}")
        for name in invalid:
            print(f"  - {name}")
        print()

    for (ttl, window, arm), recs in sorted(groups.items()):
        n = len(recs)
        print(f"## insert TTL {ttl}s, window {window}s, {arm}  (n={n})\n")
        ev = [r["analysis"]["resurrection_evidence_count"] for _, r in recs]
        resets = [r["analysis"]["ttl_reset_count"] for _, r in recs]
        sampled = [r["analysis"]["sampled_absence_returns_at_author"]
                   for _, r in recs]
        deltas = [e["delta"] for _, r in recs
                  for e in r["analysis"]["ttl_reset_events"]]
        quiet = [r["analysis"]["no_later_sampled_presence_from_t"]
                 for _, r in recs
                 if r["analysis"]["no_later_sampled_presence_from_t"]
                 is not None]
        absent = sum(1 for _, r in recs if r["analysis"]["absent_at_end"])
        resurrected = sum(1 for e in ev if e > 0)

        if arm == "treatment":
            print(f"- runs with >=1 resurrection evidence: "
                  f"{fmt_ci(resurrected, n)}")
            print(f"- evidence lower bound per run: {ev} "
                  f"(sum {sum(ev)}, median {med(ev)})")
            print(f"- TTL reset events per run: {resets}; "
                  f"sampled returns: {sampled}")
            if deltas:
                print(f"- reset deltas: n={len(deltas)}, "
                      f"median +{med(deltas)}, range "
                      f"+{min(deltas)}..+{max(deltas)}")
            if quiet:
                lifetimes = [round(q / ttl, 2) for q in quiet]
                print(f"- quiet from t (s): {sorted(quiet)} "
                      f"= {sorted(lifetimes)} lifetimes "
                      f"(median {med(lifetimes)})")
        else:
            print("- (3-sample arm: endpoints only, no resurrection "
                  "visibility)")
        print(f"- absent at window end: {fmt_ci(absent, n)}")
        gaps = [r["analysis"]["node_sample_gap_s_median"] for _, r in recs
                if r["analysis"]["node_sample_gap_s_median"] is not None]
        if arm == "treatment" and gaps:
            print(f"- node sample gap median across runs: {med(gaps)}s")
        for name, r in recs:
            a = r["analysis"]
            print(f"  - {name}: ev>={a['resurrection_evidence_count']} "
                  f"resets={a['ttl_reset_count']} "
                  f"quiet_from={a['no_later_sampled_presence_from_t']} "
                  f"end_absent={a['absent_at_end']}")
        print()

    print("Read the CIs before the point estimates: n per cell is small "
          "by design (serial real-time runs), and cells with different "
          "TTL/window are deliberately never pooled.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

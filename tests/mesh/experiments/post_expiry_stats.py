#!/usr/bin/env python3
"""Stage 2: extracted tables -> STATS.md + summary.json.

Consumes ONLY the tables written by `post_expiry_extract.py` (raw
records are never opened here), and answers exactly the predeclared
questions of `post-expiry-prereg.md` as amended by
`post-expiry-prereg-amendment-1.md`:

  Q1  repeatability   — v4 Cell A primary; v3 as historical replication
  Q2  ratio transfer  — v4 A vs B at the common 3-lifetime horizon,
                        Wilson intervals + Newcombe hybrid-score 95% RD,
                        labelled exploratory (A->B->C order confound)
  Q3  late recurrence — Cell C witnesses after 2 and 5 lifetimes,
                        interval-censored with an explicit ambiguous bin

No hypothesis tests, per amendment §3. The run is the unit throughout.
Grouping is by the FULL causal cell — every configuration column — so
config-divergent runs can never silently pool; batch is a stratum.

Usage: python3 post_expiry_stats.py <analysis-dir>
"""

import csv
import json
import math
import os
import sys
from statistics import median

CELL_COLS = ["configured_ttl", "window_s", "nodes", "interval", "delay_ms",
             "rate_kbit", "entries", "directed", "topology", "author",
             "clock_offsets", "sample_gap_s", "propagation_check_t",
             "acq_script_sha8", "binary_sha8", "arm"]


def wilson(k, n, z=1.96):
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def newcombe_rd(k1, n1, k2, n2):
    """95% hybrid-score interval for p1 - p2, no continuity correction."""
    p1, p2 = k1 / n1, k2 / n2
    (l1, u1), (l2, u2) = wilson(k1, n1), wilson(k2, n2)
    rd = p1 - p2
    return (rd,
            rd - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2),
            rd + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2))


def ci_str(k, n):
    lo, hi = wilson(k, n)
    return f"{k}/{n} ({k / n:.0%}; 95% CI {lo:.0%}–{hi:.0%})"


def load_runs(adir):
    with open(os.path.join(adir, "runs.csv")) as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for k in ("resets", "sampled_returns_author", "evidence",
                  "ev_within_3L", "ambiguous_3L", "late_after_2L",
                  "ambiguous_2L", "late_after_5L", "ambiguous_5L",
                  "samples_valid", "samples_invalidated"):
            r[k] = int(r[k])
        for k in ("quiet_from_lifetimes", "max_spread_corr",
                  "node_gap_med_s"):
            r[k] = float(r[k]) if r[k] not in ("", "None") else None
        r["absent_at_end"] = r["absent_at_end"] == "True"
    return rows


def select(rows, batch=None, ttl=None, window=None, arm=None):
    out = [r for r in rows
           if (batch is None or r["batch"] == batch)
           and (ttl is None or r["configured_ttl"] == str(ttl))
           and (window is None or r["window_s"] == str(window))
           and (arm is None or r["arm"] == arm)]
    cells = {tuple(r[c] for c in CELL_COLS) for r in out}
    if len(cells) > 1:
        raise SystemExit(f"selection ({batch},{ttl},{window},{arm}) mixes "
                         f"{len(cells)} distinct causal cells — refusing "
                         "to pool")
    return out


def dots(rows, col):
    vals = [r[col] for r in rows]
    return f"{vals} (median {median(vals)})" if vals else "[]"


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    adir = sys.argv[1]
    rows = load_runs(adir)
    manifest = json.load(open(os.path.join(adir, "MANIFEST.json")))

    a4 = select(rows, "v4", 96, 480, "treatment")
    a3 = select(rows, "v3", 96, 480, "treatment")
    b4 = select(rows, "v4", 406, 1230, "treatment")
    c4 = select(rows, "v4", 96, 960, "treatment")
    controls = [r for r in rows if r["arm"] == "control"]

    import hashlib
    s = {"analysis_id": manifest["analysis_id"],
         "invalid_records_excluded": manifest["invalid_records_excluded"],
         "planned_missing": manifest["planned_missing"],
         "stats_provenance": {
             "script_sha256": hashlib.sha256(
                 open(__file__, "rb").read()).hexdigest(),
             "input_tables": {n: hashlib.sha256(open(
                 os.path.join(adir, n), "rb").read()).hexdigest()[:16]
                 for n in ("runs.csv", "events.csv")}}}
    out = ["# Post-expiry v4 sweep — predeclared analyses",
           "",
           f"Analysis `{manifest['analysis_id']}`, "
           f"{len(rows)} valid runs consumed. "
           f"Invalid/excluded: {manifest['invalid_records_excluded'] or 'none'}; "
           f"planned-but-missing: {manifest['planned_missing'] or 'none'}.",
           "No hypothesis tests were run (amendment §3).",
           ""]

    # Q1 ------------------------------------------------------------
    k4 = sum(1 for r in a4 if r["evidence"] >= 1)
    k3 = sum(1 for r in a3 if r["evidence"] >= 1)
    s["Q1"] = {"v4_primary": [k4, len(a4)], "v3_historical": [k3, len(a3)],
               "v4_evidence_bounds": sorted(r["evidence"] for r in a4),
               "v3_evidence_bounds": sorted(r["evidence"] for r in a3)}
    out += ["## Q1 — repeatability (96 s cell, 480 s window)",
            "",
            f"- **Primary (v4): runs with ≥1 resurrection witness: "
            f"{ci_str(k4, len(a4))}**",
            f"- evidence lower bounds per v4 run: {dots(a4, 'evidence')}",
            f"- resets per run: {dots(a4, 'resets')}; sampled returns: "
            f"{dots(a4, 'sampled_returns_author')}",
            f"- historical replication (v3, different acquisition script): "
            f"{ci_str(k3, len(a3))}, bounds "
            f"{sorted(r['evidence'] for r in a3)}",
            "- no pooled v3+v4 estimate exists or will be produced: the "
            "acquisition diff changed the propagation gate, which fails "
            "the equivalence rule (amendment 2 §5)",
            ""]

    # Q2 ------------------------------------------------------------
    # Bounded estimands (amendment 2 §3): primary counts only definite
    # successes (a strict within-horizon witness); the sensitivity
    # upper bound also counts ambiguous-only runs as positive.
    ka = sum(1 for r in a4 if r["ev_within_3L"] >= 1)
    kb = sum(1 for r in b4 if r["ev_within_3L"] >= 1)
    ka_hi = sum(1 for r in a4 if r["ev_within_3L"] + r["ambiguous_3L"] >= 1)
    kb_hi = sum(1 for r in b4 if r["ev_within_3L"] + r["ambiguous_3L"] >= 1)
    rd, lo, hi = newcombe_rd(ka, len(a4), kb, len(b4))
    rd_h, lo_h, hi_h = newcombe_rd(ka_hi, len(a4), kb_hi, len(b4))
    amb_runs = [r["run_id"] for r in a4 + b4
                if r["ambiguous_3L"] >= 1 and r["ev_within_3L"] == 0]
    s["Q2"] = {"A_within_3L": [ka, len(a4)], "B_within_3L": [kb, len(b4)],
               "A_within_3L_sensitivity": [ka_hi, len(a4)],
               "B_within_3L_sensitivity": [kb_hi, len(b4)],
               "risk_difference": [round(rd, 3), round(lo, 3), round(hi, 3)],
               "risk_difference_sensitivity": [round(rd_h, 3), round(lo_h, 3),
                                              round(hi_h, 3)],
               "ambiguous_only_runs": amb_runs}
    out += ["## Q2 — opportunity-ratio transfer (EXPLORATORY: confounded "
            "twice over)",
            "",
            "Cell-specific common horizon (amendment 2 §1): witness upper "
            "bound / TTL ≤ 3 lifetimes — H_A = 288 s, H_B = 1218 s.",
            "Confounds: A→B execution order, AND observer dose per "
            "lifetime — absolute cadence over a 4.2× longer lifetime gives "
            "B ≈4.2× more probes per lifetime (amendment 2 §4). "
            "A difference, or its absence, is not attributable to gossip "
            "opportunities alone.",
            "",
            f"- Cell A (96 s, ~19 opportunities/lifetime): "
            f"{ci_str(ka, len(a4))}",
            f"- Cell B (406 s, ~81 ≈ production's 80): "
            f"{ci_str(kb, len(b4))}",
            f"- risk difference A−B (primary): {rd:+.2f} "
            f"(95% Newcombe {lo:+.2f} to {hi:+.2f})",
            f"- sensitivity (ambiguous counted positive): "
            f"A {ka_hi}/{len(a4)}, B {kb_hi}/{len(b4)}, RD {rd_h:+.2f} "
            f"({lo_h:+.2f} to {hi_h:+.2f})",
            f"- ambiguous-only runs: {amb_runs or 'none'}",
            f"- B evidence bounds per run: {dots(b4, 'evidence')} "
            f"(within 3L: {[r['ev_within_3L'] for r in b4]})",
            f"- B quiet-from (lifetimes): "
            f"{[r['quiet_from_lifetimes'] for r in b4]}",
            ""]

    # Q3 ------------------------------------------------------------
    l2 = sum(1 for r in c4 if r["late_after_2L"] >= 1)
    l5 = sum(1 for r in c4 if r["late_after_5L"] >= 1)
    l2_hi = sum(1 for r in c4 if r["late_after_2L"] + r["ambiguous_2L"] >= 1)
    l5_hi = sum(1 for r in c4 if r["late_after_5L"] + r["ambiguous_5L"] >= 1)
    s["Q3"] = {"late_after_2L": [l2, len(c4)], "late_after_5L": [l5, len(c4)],
               "late_after_2L_sensitivity": [l2_hi, len(c4)],
               "late_after_5L_sensitivity": [l5_hi, len(c4)],
               "per_run": {r["run_id"]: [r["late_after_2L"],
                                         r["ambiguous_2L"],
                                         r["late_after_5L"],
                                         r["ambiguous_5L"],
                                         r["quiet_from_lifetimes"]]
                           for r in c4}}
    out += ["## Q3 — late recurrence (96 s cell watched 10 lifetimes)",
            "",
            f"- runs with a witness definitely after 2 lifetimes: "
            f"{ci_str(l2, len(c4))} (sensitivity incl. ambiguous: "
            f"{l2_hi}/{len(c4)})",
            f"- runs with a witness definitely after 5 lifetimes: "
            f"{ci_str(l5, len(c4))} (sensitivity incl. ambiguous: "
            f"{l5_hi}/{len(c4)})",
            "- per run (late>2L, amb2L, late>5L, amb5L, quiet-from):", ]
    out += [f"  - {r['run_id']}: {r['late_after_2L']}, {r['ambiguous_2L']}, "
            f"{r['late_after_5L']}, {r['ambiguous_5L']}, "
            f"{r['quiet_from_lifetimes']}" for r in c4]
    out += [""]

    # Controls -------------------------------------------------------
    out += ["## Controls — resurrection not observable (3 samples)", ""]
    s["controls"] = {}
    for (batch, ttl, win) in sorted({(r["batch"], r["configured_ttl"],
                                      r["window_s"]) for r in controls}):
        grp = [r for r in controls if (r["batch"], r["configured_ttl"],
                                       r["window_s"]) == (batch, ttl, win)]
        k = sum(1 for r in grp if r["absent_at_end"])
        s["controls"][f"{batch}-ttl{ttl}-w{win}"] = [k, len(grp)]
        out.append(f"- {batch}, TTL {ttl}s, window {win}s — absent at end: "
                   f"{ci_str(k, len(grp))}")
    out += ["", "## Observer diagnostics (descriptive)", ""]
    for grp, label in ((a4, "A v4"), (b4, "B v4"), (c4, "C v4")):
        out.append(f"- {label}: valid samples {dots(grp, 'samples_valid')}, "
                   f"gap med {dots(grp, 'node_gap_med_s')}, "
                   f"spread/TTL "
                   f"{[round(r['max_spread_corr'] / int(r['configured_ttl']), 2) for r in grp]}")

    text = "\n".join(out) + "\n"
    with open(os.path.join(adir, "STATS.md"), "w") as f:
        f.write(text)
    with open(os.path.join(adir, "summary.json"), "w") as f:
        json.dump(s, f, indent=1, sort_keys=True)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

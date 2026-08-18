"""Decoder validation: probe-vs-wire parity for a "both" run (Phase 1.4).

Consumes a v2 run record (arm=both) and its decode output, and applies
the plan's matching rule:

- Each probe-derived witness (sampled absence-return at the author, or
  author TTL reset) is matched against the FIRST qualifying author
  outbound slice at or after the probe witness time — not "within one
  gossip interval", since impaired transfers can exceed one interval.
  If no qualifying outbound slice occurs before the window ends, that
  is a MISSING OBSERVATION OPPORTUNITY, reported separately, not a
  decoder disagreement.
- Consistency, not equality: the outbound slice is expected to show the
  key at (probe TTL − elapsed) within a tolerance of one gossip
  interval + 2 s (TTL decays 1 Hz with bleach granularity); if the
  expected decayed TTL has already reached zero, absence is consistent.
- Contradiction scan: EVERY valid probed author observation is checked
  against the first outbound slice at/after it the same way. The
  acceptance bar is zero contradictions.

Exit 0 = parity accepted (all matchable witnesses matched, zero
contradictions, completeness ok). The report is written next to the
record as <record>.parity.json.

Usage: python3 parity_check.py <run-record.json>
"""

import json
import os
import sys


def first_outbound_at_or_after(rows, t_unix):
    for r in rows:
        if r["t"] is not None and r["t"] >= t_unix:
            return r
    return None


def consistent(probe_ttl, t_probe_unix, row, tol):
    """Is an outbound slice consistent with a probe observation earlier?"""
    expected = probe_ttl - (row["t"] - t_probe_unix)
    if row["present"] and row["ttl"] is not None:
        return abs(row["ttl"] - expected) <= tol, expected
    # Absent is consistent only if the entry could have expired by then.
    return expected <= tol, expected


def check(record_path):
    with open(record_path) as f:
        rec = json.load(f)
    decode_path = record_path.replace(".json", "") + ".decode.json"
    with open(decode_path) as f:
        dec = json.load(f)

    if rec["arm"] != "both":
        raise SystemExit("parity needs an arm=both validation run")
    t0 = rec["t0_unix"]
    interval = rec["cell"]["interval"]
    tol = interval + 2
    window_end = t0 + rec["window_s"]
    author = rec["author"]
    rows = dec["author_outbound_rows"]

    ana = rec["analysis"]
    probe_witnesses = (
        [{"kind": "absence-return", "t": e["t"], "ttl": e["ttl_on_return"]}
         for e in ana["author_events"]]
        + [{"kind": "ttl-reset", "t": e["t"], "ttl": e["ttl"]}
           for e in ana["ttl_reset_events"]])

    matched, unmatched, missing_opportunity = [], [], []
    for w in probe_witnesses:
        t_unix = t0 + w["t"]
        row = first_outbound_at_or_after(rows, t_unix)
        if row is None:
            missing_opportunity.append(w)
            continue
        ok, expected = consistent(w["ttl"], t_unix, row, tol)
        (matched if ok else unmatched).append(
            {**w, "outbound_t": row["t"], "outbound_present": row["present"],
             "outbound_ttl": row["ttl"],
             "expected_ttl": round(expected, 1)})

    # Contradiction scan over every valid probed author observation.
    contradictions = []
    checked = 0
    for srow in rec["series"]:
        if not srow["valid"]:
            continue
        obs = srow["obs"][author]
        if not obs["ok"]:
            continue
        t_unix = t0 + obs["done"]
        if t_unix > window_end:
            continue
        row = first_outbound_at_or_after(rows, t_unix)
        if row is None:
            continue                     # tail: no opportunity, not a clash
        checked += 1
        if obs["present"] and obs["ttl"] is not None:
            ok, expected = consistent(obs["ttl"], t_unix, row, tol)
        else:
            # Probe saw absence: the next outbound slice may show it
            # still absent, or back with a fresh (re-adopted) TTL —
            # resurrection is the phenomenon under study, so a return
            # is not a contradiction. Only an impossibly HIGH value
            # would be (nothing on the wire exceeds the insert TTL).
            ok = (not row["present"]) or (
                row["ttl"] is not None
                and row["ttl"] <= rec["configured_insert_ttl"])
            expected = None
        if not ok:
            contradictions.append(
                {"t": obs["done"], "probe_present": obs["present"],
                 "probe_ttl": obs["ttl"], "outbound_t": row["t"],
                 "outbound_present": row["present"],
                 "outbound_ttl": row["ttl"],
                 "expected_ttl": (round(expected, 1)
                                  if expected is not None else None)})

    report = {
        "record": os.path.basename(record_path),
        "tolerance_s": tol,
        "probe_witnesses": len(probe_witnesses),
        "matched": matched,
        "unmatched": unmatched,
        "missing_observation_opportunity": missing_opportunity,
        "author_observations_checked": checked,
        "contradictions": contradictions,
        "wire_witness_count": dec["witnesses"]["witness_count"],
        "completeness_ok": dec["completeness_ok"],
        "parity_ok": (not unmatched and not contradictions
                      and dec["completeness_ok"]
                      and dec["decode_valid"]),
    }
    dest = record_path.replace(".json", "") + ".parity.json"
    tmp = dest + ".tmp"
    with open(tmp, "w") as f:
        json.dump(report, f, indent=1, sort_keys=True)
    os.replace(tmp, dest)
    return dest, report


if __name__ == "__main__":
    dest, rep = check(sys.argv[1])
    print(f"{dest}\n"
          f"  probe witnesses : {rep['probe_witnesses']} "
          f"(matched {len(rep['matched'])}, unmatched "
          f"{len(rep['unmatched'])}, missing-opportunity "
          f"{len(rep['missing_observation_opportunity'])})\n"
          f"  observations checked against wire: "
          f"{rep['author_observations_checked']}  contradictions: "
          f"{len(rep['contradictions'])}\n"
          f"  wire witnesses  : {rep['wire_witness_count']}\n"
          f"  PARITY OK       : {rep['parity_ok']}")
    sys.exit(0 if rep["parity_ok"] else 1)

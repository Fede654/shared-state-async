#!/usr/bin/env python3
"""Stage 1: post-expiry records -> tidy tables (runs / events / observations).

Everything is RE-DERIVED from each record's raw `series` by calling the
current `analyse()` on it — the in-record `analysis` block is never
read, so `--reanalyse` history cannot leak into aggregate results
(prereg amendment 1, "immutability, corrected"). The immutable portion
of each record (everything except `analysis`/`analysis_provenance`) is
hashed separately into the manifest.

Event intervals follow amendment §2: a witness at the author is the
interval (previous author observation `done`, current author
observation `done`], in per-node observation time. Horizon and lateness
classification live here, in lifetime units, so the stats stage is pure
table arithmetic. Classifying from full-series intervals is equivalent
to re-running the analyser on a `done <= H` truncated series: both
witness classes compare consecutive author observations only, and an
interval with upper bound <= H uses exactly the observations the
truncated series keeps.

Refusals: invalid records, mixed analysis schemas, and any binary other
than the REPRODUCTION-pinned one are errors. Planned-but-missing runs
are listed loudly; `--allow-partial` is required to proceed without
them.

Usage: python3 post_expiry_extract.py [--allow-partial]
"""

import csv
import glob
import hashlib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import post_expiry as pe                                  # noqa: E402

RESULTS = os.path.join(HERE, "results", "post-expiry")
REPRODUCTION = os.path.join(HERE, "results", "REPRODUCTION-b5b3de0a.json")

# Planned denominators, fixed by the committed drivers (prereg
# amendment 1): (batch, ttl, window, arm) -> n. Anything on disk beyond
# this plan is an error, not a bonus sample.
PLAN = {
    ("v3", 96, 480, "treatment"): 3, ("v3", 96, 480, "control"): 3,
    ("v4", 96, 480, "treatment"): 5, ("v4", 96, 480, "control"): 3,
    ("v4", 406, 1230, "treatment"): 3, ("v4", 406, 1230, "control"): 2,
    ("v4", 96, 960, "treatment"): 3,
}

RUN_FIELDS = [
    "run_id", "batch", "arm", "configured_ttl", "window_s", "nodes",
    "interval", "delay_ms", "rate_kbit", "entries", "directed",
    "topology", "topology_source", "author", "clock_offsets",
    "sample_gap_s", "propagation_check_t", "acq_script_sha8",
    "binary_sha8", "record_valid", "samples_valid", "samples_invalidated",
    "propagation_confirmed_at_t", "first_observed_author_ttl",
    "resets", "sampled_returns_author", "sampled_returns_any",
    "evidence", "ev_within_3L", "ambiguous_3L",
    "late_after_2L", "ambiguous_2L", "late_after_5L", "ambiguous_5L",
    "absent_at_end", "no_later_presence_from_t", "quiet_from_lifetimes",
    "max_spread_corr", "node_gap_med_s", "node_gap_max_s",
]
EVENT_FIELDS = ["run_id", "kind", "prev_done", "cur_done",
                "lower_lifetimes", "upper_lifetimes", "delta",
                "ttl_on_return"]
OBS_FIELDS = ["run_id", "row_t", "row_valid", "node", "start", "done",
              "ok", "present", "ttl", "gen", "alive"]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def immutable_sha(rec):
    """Hash of the record minus its rewritable derived fields."""
    core = {k: v for k, v in rec.items()
            if k not in ("analysis", "analysis_provenance")}
    return sha(json.dumps(core, sort_keys=True).encode())


def batch_of(name):
    m = re.search(r"-(v\d+)[A-C]?-rep", name)
    if not m:
        raise SystemExit(f"cannot read batch stratum from filename: {name}")
    return m.group(1)


def author_events(series, author):
    """Interval-censored witnesses at the author, on `done` times."""
    events = []
    prev = None
    held = False
    for row in (r for r in series if r["valid"]):
        cur = row["obs"][author]
        if prev is not None:
            both_ttl = (prev["ttl"] is not None and cur["ttl"] is not None)
            if (prev["present"] and cur["present"] and both_ttl
                    and cur["ttl"] > prev["ttl"]):
                events.append({"kind": "ttl_reset",
                               "prev_done": prev["done"],
                               "cur_done": cur["done"],
                               "delta": cur["ttl"] - prev["ttl"],
                               "ttl_on_return": cur["ttl"]})
            if held and not prev["present"] and cur["present"]:
                events.append({"kind": "sampled_return",
                               "prev_done": prev["done"],
                               "cur_done": cur["done"],
                               "delta": None,
                               "ttl_on_return": cur["ttl"]})
        if cur["present"]:
            held = True
        prev = cur
    return events


def classify(events, ttl):
    """Horizon/lateness bins in lifetimes; straddlers to `ambiguous`."""
    out = {"ev_within_3L": 0, "ambiguous_3L": 0,
           "late_after_2L": 0, "ambiguous_2L": 0,
           "late_after_5L": 0, "ambiguous_5L": 0}
    for e in events:
        lo, up = e["prev_done"] / ttl, e["cur_done"] / ttl
        e["lower_lifetimes"] = round(lo, 3)
        e["upper_lifetimes"] = round(up, 3)
        if up <= 3:
            out["ev_within_3L"] += 1
        elif lo < 3:
            out["ambiguous_3L"] += 1
        for thr, late_k, amb_k in ((2, "late_after_2L", "ambiguous_2L"),
                                   (5, "late_after_5L", "ambiguous_5L")):
            if lo > thr:
                out[late_k] += 1
            elif up > thr:
                out[amb_k] += 1
    return out


def main():
    allow_partial = "--allow-partial" in sys.argv
    expected_binary = json.load(open(REPRODUCTION))[
        "agreement"]["binary_sha256"]

    runs, events_rows, obs_rows, inputs = [], [], [], {}
    invalid = []
    seen = {}
    for path in sorted(glob.glob(os.path.join(RESULTS, "*.json"))):
        name = os.path.basename(path)
        raw = open(path, "rb").read()
        rec = json.loads(raw)
        if not rec.get("record_valid"):
            invalid.append(name)
            continue
        bsha = rec["provenance"]["binary"]["sha256"]
        if bsha != expected_binary:
            raise SystemExit(f"{name}: binary {bsha[:8]} is not the "
                             f"pinned {expected_binary[:8]} — refusing")
        schema = (rec.get("analysis_provenance") or {}).get("analysis_schema")
        if schema not in (None, pe.ANALYSIS_SCHEMA):
            raise SystemExit(f"{name}: analysis schema {schema} != "
                             f"{pe.ANALYSIS_SCHEMA} — refusing mixed schemas")

        cell = rec["cell"]
        batch = batch_of(name)
        arm = "control" if rec["control_light_sampling"] else "treatment"
        key = (batch, rec["configured_insert_ttl"], rec["window_s"], arm)
        seen[key] = seen.get(key, 0) + 1
        if key not in PLAN:
            raise SystemExit(f"{name}: cell {key} is not in the committed "
                             f"plan — refusing unplanned data")

        author = rec["author"]
        names = list(rec["series"][0]["obs"])
        # Re-derivation from the raw series — the only analysis input.
        a = pe.analyse(rec["series"], author, names)
        ev = author_events(rec["series"], author)
        # The interval detector and analyse() must agree on counts; a
        # divergence means one of them changed without the other.
        assert len([e for e in ev if e["kind"] == "ttl_reset"]) \
            == a["ttl_reset_count"], name
        assert len([e for e in ev if e["kind"] == "sampled_return"]) \
            == a["sampled_absence_returns_at_author"], name

        ttl = rec["configured_insert_ttl"]
        bins = classify(ev, ttl)
        quiet = a["no_later_sampled_presence_from_t"]
        runs.append({
            "run_id": name, "batch": batch, "arm": arm,
            "configured_ttl": ttl, "window_s": rec["window_s"],
            "nodes": cell["nodes"], "interval": cell["interval"],
            "delay_ms": cell["delay_ms"], "rate_kbit": cell["rate_kbit"],
            "entries": cell["entries"], "directed": cell["directed"],
            # Records before amendment 2 §6 carry no topology field; it
            # is derived from the single committed code path
            # (mesh.chain, directed=False) and marked as such.
            "topology": rec.get("topology", "chain-undirected"),
            "topology_source": ("recorded" if "topology" in rec
                                else "script-derived"),
            "author": author,
            "clock_offsets": json.dumps(rec["clock_offsets"],
                                        sort_keys=True),
            "sample_gap_s": rec["sample_gap_s"],
            "propagation_check_t": rec["propagation_check_t_requested"],
            "acq_script_sha8": rec["provenance"]["script"]["sha256"][:8],
            "binary_sha8": bsha[:8],
            "record_valid": rec["record_valid"],
            "samples_valid": a["samples_valid"],
            "samples_invalidated": a["samples_invalidated"],
            "propagation_confirmed_at_t": rec["propagation_confirmed_at_t"],
            "first_observed_author_ttl": rec["first_observed_author_ttl"],
            "resets": a["ttl_reset_count"],
            "sampled_returns_author": a["sampled_absence_returns_at_author"],
            "sampled_returns_any": a["sampled_absence_returns_total"],
            "evidence": a["resurrection_evidence_count"], **bins,
            "absent_at_end": a["absent_at_end"],
            "no_later_presence_from_t": quiet,
            "quiet_from_lifetimes": (round(quiet / ttl, 3)
                                     if quiet is not None else None),
            "max_spread_corr": a["max_spread"],
            "node_gap_med_s": a["node_sample_gap_s_median"],
            "node_gap_max_s": a["node_sample_gap_s_max"],
        })
        for e in ev:
            events_rows.append({"run_id": name, **{k: e.get(k)
                                for k in EVENT_FIELDS if k != "run_id"}})
        for row in rec["series"]:
            for nm, o in row["obs"].items():
                obs_rows.append({"run_id": name, "row_t": row["t"],
                                 "row_valid": row["valid"], "node": nm,
                                 "start": o["start"], "done": o["done"],
                                 "ok": o["ok"], "present": o["present"],
                                 "ttl": o["ttl"], "gen": o["gen"],
                                 "alive": o["alive"]})
        inputs[name] = {"sha256_full": sha(raw),
                        "sha256_immutable": immutable_sha(rec)}

    missing = {k: n - seen.get(k, 0) for k, n in PLAN.items()
               if seen.get(k, 0) != n}
    extra = {k: v for k, v in seen.items() if v > PLAN.get(k, 0)}
    if extra:
        raise SystemExit(f"more records than the committed plan: {extra}")
    if missing:
        msg = (f"planned runs missing (invalid or absent): {missing}; "
               f"invalid records: {invalid}")
        if not allow_partial:
            raise SystemExit(msg + "\nre-run with --allow-partial to "
                             "analyse anyway; the gap stays in the manifest")
        print(f"WARNING: {msg}", file=sys.stderr)

    with open(__file__, "rb") as f:
        self_sha = sha(f.read())
    analysis_id = sha("".join(
        f"{n}{v['sha256_immutable']}" for n, v in sorted(inputs.items())
    ).encode() + self_sha.encode())[:8]

    outdir = os.path.join(RESULTS, "analysis", analysis_id)
    os.makedirs(outdir, exist_ok=True)
    for fname, fields, rows in (("runs.csv", RUN_FIELDS, runs),
                                ("events.csv", EVENT_FIELDS, events_rows),
                                ("observations.csv", OBS_FIELDS, obs_rows)):
        with open(os.path.join(outdir, fname), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
    manifest = {
        "analysis_id": analysis_id,
        "inputs": inputs,
        "invalid_records_excluded": invalid,
        "planned_missing": {str(k): v for k, v in missing.items()},
        "extractor_sha256": self_sha,
        "analyser_module_sha256": pe.analysis_provenance()["analyzer_sha256"],
        "pinned_binary_sha256": expected_binary,
        "command": " ".join(sys.argv),
    }
    with open(os.path.join(outdir, "MANIFEST.json"), "w") as f:
        json.dump(manifest, f, indent=1, sort_keys=True)
    print(f"{outdir}: {len(runs)} runs, {len(events_rows)} events, "
          f"{len(obs_rows)} observations"
          + (f"; MISSING {missing}" if missing else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Sweep driver v2 (HRM-147) — frozen schedules, arms, resume, no reshuffle.

Successor to post_expiry_batch.sh (v1, untouched: it remains the record
of what ran). Three modes:

--make-schedule  Generate a fully MATERIALIZED run schedule from a seed:
                 an ordered list of slots (run_id × arm × topology ×
                 window × bleach_ttl × binary), randomized in
                 interleaved blocks. The schedule is written as JSON and
                 is meant to be committed (the preregistration freezes
                 it); the seed alone is NOT a schedule.

--schedule F     Execute a schedule file, in order. Atomic run IDs come
                 from the slots. RESUME = re-invoking with the same file:
                 slots whose record already exists are skipped, order is
                 never reshuffled, and an invalid run is recorded as
                 invalid and NEVER re-run or replaced by outcome — the
                 preregistration states the policy; this driver merely
                 cannot do anything else.

--shakedown      A small pilot batch across topologies (passive arm),
                 stamped and labelled pilot; excluded from confirmatory
                 analysis by construction (schedule_kind=shakedown in
                 every record's driver block... and by the prereg).

Dose equalization: probed slots carry a sample gap computed for a
constant number of samples per LIFETIME (SAMPLES_PER_LIFETIME), not per
second. The recorded dose covariate remains the measured cumulative
probe connection duration (post_expiry_v2.probe_dose).

Decoding is deferred until the whole batch has collected (--decode runs
it explicitly) so decoder CPU never coexists with a live cell.

A heartbeat line is appended to <schedule>.heartbeat.log before every
slot — the overnight-batch monitoring hook.
"""

import json
import os
import random
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "results", "post-expiry-v2")

SAMPLES_PER_LIFETIME = 12     # probed arms: constant dose in lifetime units


def sample_gap(bleach_ttl, interval=5):
    lifetime = bleach_ttl + interval + 1
    return round(lifetime / SAMPLES_PER_LIFETIME, 1)


def make_schedule(seed, blocks, cells, binary, kind="confirmatory"):
    """cells: list of dicts {arm, topology, window, bleach_ttl}.
    Each block contains every cell once, order drawn per block."""
    rng = random.Random(seed)
    slots = []
    for b in range(blocks):
        order = list(cells)
        rng.shuffle(order)
        for c in order:
            slot_id = (f"pe2-{kind[:5]}-b{b+1:02d}-{c['arm']}-"
                       f"{c['topology']}-ttl{c['bleach_ttl']}-"
                       f"s{len(slots)+1:03d}")
            slots.append(dict(c, run_id=slot_id, block=b + 1,
                              binary=binary))
    return {"schedule_kind": kind, "seed": seed, "blocks": blocks,
            "samples_per_lifetime": SAMPLES_PER_LIFETIME,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                          time.gmtime()),
            "slots": slots}


def run_slot(slot, heartbeat):
    run_id = slot["run_id"]
    record = os.path.join(RESULTS, run_id + ".json")
    if os.path.exists(record):
        return "skipped-existing"
    with open(heartbeat, "a") as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} "
                f"start {run_id}\n")
    argv = [sys.executable, os.path.join(HERE, "post_expiry_v2.py"),
            "--arm", slot["arm"], "--topology", slot["topology"],
            "--window", str(slot["window"]),
            "--bleach-ttl", str(slot["bleach_ttl"]),
            "--run-id", run_id]
    if slot.get("interval"):
        argv += ["--interval", str(slot["interval"])]
    if slot.get("binary"):
        argv += ["--bin", slot["binary"]]
    if slot["arm"] in ("probes", "both"):
        argv += ["--sample-gap", str(sample_gap(
            slot["bleach_ttl"], slot.get("interval", 5)))]
    r = subprocess.run(argv, capture_output=True, text=True)
    status = "ok" if r.returncode == 0 else "invalid"
    if not os.path.exists(record):
        status = "no-record"          # infrastructure failure, not data
        with open(os.path.join(RESULTS, run_id + ".FAILED.log"), "w") as f:
            f.write(r.stdout[-4000:] + "\n---\n" + r.stderr[-4000:])
    with open(heartbeat, "a") as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} "
                f"done {run_id} {status}\n")
    return status


def execute(schedule_path):
    with open(schedule_path) as f:
        sched = json.load(f)
    heartbeat = schedule_path + ".heartbeat.log"
    outcomes = {}
    for slot in sched["slots"]:
        outcomes[slot["run_id"]] = run_slot(slot, heartbeat)
        print(f"  {slot['run_id']}: {outcomes[slot['run_id']]}", flush=True)
    return outcomes


if __name__ == "__main__":
    def opt(name, default, cast=int):
        return cast(sys.argv[sys.argv.index(name) + 1]) \
            if name in sys.argv else default

    os.makedirs(RESULTS, exist_ok=True)
    if "--make-schedule" in sys.argv:
        seed = opt("--seed", None)
        if seed is None:
            sys.exit("--make-schedule requires an explicit --seed "
                     "(it goes into the preregistration)")
        blocks = opt("--blocks", 3)
        window = opt("--window", 480)
        ttl = opt("--bleach-ttl", 90)
        binary = opt("--bin", "", str) or None
        kind = opt("--kind", "confirmatory", str)
        cells = [{"arm": "passive", "topology": t, "window": window,
                  "bleach_ttl": ttl} for t in ("chain", "ring", "chord")]
        sched = make_schedule(seed, blocks, cells, binary, kind=kind)
        dest = opt("--out", os.path.join(
            RESULTS, f"schedule-{kind}-seed{seed}.json"), str)
        with open(dest, "w") as f:
            json.dump(sched, f, indent=1, sort_keys=True)
        print(f"{dest}: {len(sched['slots'])} slots")
    elif "--schedule" in sys.argv:
        path = opt("--schedule", None, str)
        outcomes = execute(path)
        bad = {k: v for k, v in outcomes.items()
               if v not in ("ok", "skipped-existing")}
        print(json.dumps(outcomes, indent=1))
        sys.exit(1 if bad else 0)
    elif "--shakedown" in sys.argv:
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        window = opt("--window", 480)
        heartbeat = os.path.join(RESULTS, f"shakedown-{stamp}.heartbeat.log")
        outcomes = {}
        for topo in ("chain", "ring", "chord"):
            slot = {"run_id": f"pe2-shake-{topo}-{stamp}", "arm": "passive",
                    "topology": topo, "window": window, "bleach_ttl": 90,
                    "binary": None}
            outcomes[slot["run_id"]] = run_slot(slot, heartbeat)
            print(f"  {slot['run_id']}: {outcomes[slot['run_id']]}",
                  flush=True)
        bad = {k: v for k, v in outcomes.items() if v != "ok"}
        sys.exit(1 if bad else 0)
    else:
        print(__doc__)

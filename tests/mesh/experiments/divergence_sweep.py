#!/usr/bin/env python3
"""Parameter sweep: what conditions produce field-scale TTL divergence?

SUPERSEDED IN PART — read this first (2026-08-13)
=================================================
Every `spread` number below, and in `results/SUMMARY.md`, is a **maximum
over an observation window that was never recorded**. TTL spread
accumulated approximately linearly **throughout the five-minute window
that was actually observed** (see `divergence_dynamics.py`), so a spread
is `rate x window` and two spreads measured over different windows are
not comparable. "For as long as you watch" was the earlier wording here
and it is withdrawn: nothing beyond five minutes was measured. The
follow-up claim — that the author's copy bleaching at ~2431 s makes
unbounded growth "implausible" — is **also withdrawn** (2026-08-15):
T24 shows expiry is not terminal, since a missing key is inserted at
`sharedstate.cc:866` before the own-authorship guard can see it, so the
author adopts a neighbour's inflated echo instead of staying empty.
Post-expiry behaviour is under measurement (`post_expiry.py`); an
interim claim that the entry "goes extinct mesh-wide in 5 of 5 runs" was
withdrawn after review found the sampler counting failed probes as
absence and the observer displacing the gossip that would prevent
extinction. Organic resurrection has NOT been reproduced (0 in 3 valid runs);
the mechanism is established by T24, which injects the echo. The 3 s / 112 s
contrast this file is known for compares two different windows as much
as two configurations.

Two conclusions stated below are consequently WITHDRAWN:

  - "propagation delay cancels exactly" — it rested on `chain-5x30` vs
    `directed-5x30`, which also changes topology directionality.
    Controlled, 10 -> 40 ms does leave spread unchanged, but 400 ms
    raises the divergence *rate* by half again.
  - "transfer duration is the driver" — right about the mechanism,
    wrong about the units. Transfer duration sets the divergence
    RATE (~36 s per 100 s at the reference configuration), not the
    divergence.

This sweep remains useful for exploring the parameter space and for the
non-spread columns (propagation time, ill-warning counts). For anything
about divergence magnitude, use `single_factor.py` (one knob at a time)
and `divergence_dynamics.py` (fixed window, timestamped samples, slope).

The original text follows unedited, because how these errors were made
is part of the record.


T22 reproduces the MonteNet *signature* (author holds the lowest TTL for
its own key, "is remote peer ill?" in the logs) but only ~5 s of spread
against the field's 22-27 s. This sweep maps the parameter space to find
which conditions close that gap, and records every run so results
accumulate instead of scrolling past.

  python3 experiments/divergence_sweep.py                # default matrix
  python3 experiments/divergence_sweep.py --quick        # 3 fast configs
  python3 experiments/divergence_sweep.py --only directed-5x30
  python3 experiments/divergence_sweep.py --bin /path/to/build/shared-state-async

Results land in experiments/results/ as one timestamped JSON per run
plus a summary table in experiments/results/SUMMARY.md rebuilt from
every record on disk. Re-running a configuration adds a data point
rather than replacing one — single runs are single runs, and nothing
here reports repetitions or confidence intervals yet.

WHAT THE SWEEP FOUND (2026-08-11)

The first hypothesis was wrong, and the sweep is what disproved it. It
assumed spread came from propagation delay — an entry reaching a distant
node late, so that node bleaches from a "younger" value. It does not:
the TTL travels *already decayed*, because a sender serializes its
current value, so propagation delay cancels out exactly. Making
propagation twice as slow (`directed-5x30`, 81 s vs 42 s) left spread
unchanged at 3 s.

What does not cancel is **transfer duration**. A receiver starts
bleaching when it finishes *reading* an entry, while the sender's copy
has been decaying since it serialized. Every hop therefore adds roughly
one transfer duration of divergence — and a sync carries the *entire*
state for the type (critique 1.1), so transfer duration grows with the
size of the network.

  bulk-5x30-256kbit   250 entries over a 256 kbit link -> **112 s** spread
  chain-5x30          1 entry over a lab bridge        ->     3 s spread

That is the mechanism behind MonteNet's 22-27 s: a large
`wifi_links_info` over distance=1000 radio. It also means the defect is
**self-amplifying with network size** — the bigger the mesh, the longer
each full-state transfer takes, the further TTLs diverge, and TTL is the
only signal the merge rule has to decide which copy is newer.

STRENGTH OF THESE CLAIMS — read before citing them

What is well supported: large, slow full-state transfers can amplify
divergence by more than an order of magnitude.

What was previously called a "clean single-factor comparison" is not
one: `directed-5x30` against `chain-5x30` changes topology
directionality, and the 42 s/81 s figures are end-to-end dissemination
times rather than independently controlled link latency. "Propagation
delay cancels exactly" is therefore a *reading* of that pair, not a
controlled result — the mechanism argument (a sender transmits its
already-decayed TTL) is what carries it, and it deserves a direct
experiment.

What is NOT yet supported: the exact ranking of knobs, and the claim
that this *is* the MonteNet mechanism rather than *a* mechanism
sufficient to produce MonteNet-scale numbers. The 112 s result changed
entry count, link rate and latency simultaneously, so it does not
isolate a cause. Every configuration here has **one run**, with no
repetitions and no confidence intervals.

To settle it, vary one factor at a time — state bytes, then rate, then
latency, then interval, then hops — with repetitions, and record actual
measured transfer time alongside the spread.
"""

import argparse
import datetime
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import ensure_inner, Mesh, DEFAULT_BIN  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import provenance  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "results")
ILL = re.compile(r"is remote peer ill")
NAMES = ["jime", "balcon", "tronco", "e-bob", "nodo-suri",
         "n6", "n7", "n8", "n9"]

# name -> knobs. Field reference: 5 nodes, 30 s interval, 2400 s TTL,
# long-distance radio (distance=1000), measured spread 22-27 s.
MATRIX = [
    dict(name="baseline-3x5", nodes=3, interval=5, directed=False,
         stagger=True, delay_ms=40),
    dict(name="chain-5x30", nodes=5, interval=30, directed=False,
         stagger=True, delay_ms=40),
    dict(name="chain-5x30-nostagger", nodes=5, interval=30, directed=False,
         stagger=False, delay_ms=40),
    dict(name="directed-5x30", nodes=5, interval=30, directed=True,
         stagger=True, delay_ms=40),
    dict(name="directed-5x30-slow", nodes=5, interval=30, directed=True,
         stagger=True, delay_ms=400, loss_pct=10),
    dict(name="directed-7x30", nodes=7, interval=30, directed=True,
         stagger=True, delay_ms=40),
    dict(name="directed-5x15", nodes=5, interval=15, directed=True,
         stagger=True, delay_ms=40),
    # transfer-duration configs: large state over a slow link, which is
    # what MonteNet actually is (big wifi_links_info, distance=1000 radio)
    dict(name="bulk-5x30-256kbit", nodes=5, interval=30, directed=False,
         stagger=True, delay_ms=100, entries=250, rate_kbit=256),
    dict(name="bulk-5x30-64kbit", nodes=5, interval=30, directed=False,
         stagger=True, delay_ms=200, entries=250, rate_kbit=64),
]
QUICK = {"baseline-3x5", "chain-5x30", "directed-5x30"}

TYPE = "wifi_links_info"
BLEACH_TTL = 2400
SAMPLES = 5
SAMPLE_EVERY = 8


def run_config(cfg, binary, rundir):
    names = NAMES[:cfg["nodes"]]
    interval = cfg["interval"]
    budget = max(120, interval * cfg["nodes"] * 2)
    # a rate-limited link makes every probe slow too
    probe_to = 90 if cfg.get("rate_kbit") else 15
    with Mesh(names, rundir, binary=binary) as mesh:
        offsets = mesh.stagger_clocks(interval) if cfg["stagger"] else {}
        nodes = mesh.bootstrap(TYPE, update_interval=interval,
                               bleach_ttl=BLEACH_TTL, full_mesh=False)
        mesh.chain(directed=cfg["directed"])
        for n in nodes:
            mesh.impair(n, delay_ms=cfg.get("delay_ms"),
                        loss_pct=cfg.get("loss_pct"),
                        rate_kbit=cfg.get("rate_kbit"))

        author = nodes[0]
        key = author.name
        # Optional bulk state: the field's wifi_links_info is large, and
        # transfer duration is what actually drives TTL divergence.
        bulk = cfg.get("entries", 0)
        if bulk:
            payload = json.dumps({
                f"pad-{i}": {"gen": 1, "src": author.name,
                             "blob": "x" * 160} for i in range(bulk)})
            author.cli(f"insert {TYPE}", stdin=payload, timeout=180)
        t_pub = time.time()
        author.publish(TYPE, key, gen=1)

        # how long until every node has heard of it?
        reached_at = {}
        deadline = time.time() + budget
        while time.time() < deadline and len(reached_at) < len(nodes):
            for n in nodes:
                if n.name in reached_at:
                    continue
                try:
                    if n.probe(TYPE, timeout=probe_to).get(key) is not None:
                        reached_at[n.name] = time.time() - t_pub
                except Exception:
                    pass
            if len(reached_at) < len(nodes):
                time.sleep(3)

        complete = len(reached_at) == len(nodes)
        worst, worst_row, author_lowest, samples = 0, None, 0, 0
        if complete:
            for _ in range(SAMPLES):
                row = {}
                for n in nodes:
                    try:
                        e = n.probe(TYPE, timeout=probe_to).get(key)
                    except Exception:
                        e = None
                    if e:
                        row[n.name] = e["ttl"]
                if len(row) == len(nodes):
                    samples += 1
                    spread = max(row.values()) - min(row.values())
                    if spread > worst:
                        worst, worst_row = spread, dict(row)
                    if row[author.name] == min(row.values()) and spread > 0:
                        author_lowest += 1
                time.sleep(SAMPLE_EVERY)

        ill = sum(len(ILL.findall(n.read_log())) for n in nodes)
        return {
            **cfg,
            "clock_offsets": offsets,
            "propagation_complete": complete,
            "propagation_s": (round(max(reached_at.values()), 1)
                              if complete else None),
            "reached": {k: round(v, 1) for k, v in reached_at.items()},
            "max_ttl_spread_s": worst,
            "spread_row": worst_row,
            "author_lowest_fraction": (f"{author_lowest}/{samples}"
                                       if samples else "n/a"),
            "ill_warnings": ill,
        }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bin", default=DEFAULT_BIN)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--only", nargs="*", default=None)
    args = ap.parse_args()
    if not os.path.exists(args.bin):
        print(f"binary not found: {args.bin}")
        return 2

    ensure_inner()
    os.makedirs(RESULTS, exist_ok=True)

    configs = MATRIX
    if args.quick:
        configs = [c for c in MATRIX if c["name"] in QUICK]
    if args.only:
        configs = [c for c in MATRIX if c["name"] in args.only]

    rows = []
    for cfg in configs:
        print(f"[sweep] {cfg['name']} ...", flush=True)
        rundir = os.path.join("/tmp/ss-sweep", cfg["name"])
        try:
            res = run_config(cfg, args.bin, rundir)
        except Exception as e:
            res = {**cfg, "error": f"{type(e).__name__}: {e}"}
        res["provenance"] = provenance.collect(args.bin, __file__)
        res["binary"] = args.bin
        rows.append(res)
        # timestamped: re-running a config must add a data point, not
        # silently replace the previous one
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y%m%dT%H%M%SZ")
        res["timestamp"] = stamp
        with open(os.path.join(RESULTS,
                               f"{cfg['name']}-{stamp}.json"), "w") as f:
            json.dump(res, f, indent=1, sort_keys=True)
        print(f"[sweep] {cfg['name']}: spread="
              f"{res.get('max_ttl_spread_s')}s "
              f"prop={res.get('propagation_s')}s "
              f"ill={res.get('ill_warnings')}", flush=True)

    _write_summary(rows)
    print("\n" + _table(rows))
    print(f"\nresults: {RESULTS}")
    return 0


def _table(rows):
    head = ("| config | nodes | interval | directed | stagger | link | "
            "spread | propagation | author-lowest | ill |\n"
            "|---|---|---|---|---|---|---|---|---|---|")
    out = [head]
    for r in rows:
        if r.get("error"):
            out.append(f"| {r['name']} | | | | | | ERROR: {r['error'][:40]} | | | |")
            continue
        link = f"{r.get('delay_ms', 0)}ms"
        if r.get("loss_pct"):
            link += f"/{r['loss_pct']}%"
        out.append(
            f"| {r['name']} | {r['nodes']} | {r['interval']}s | "
            f"{'yes' if r['directed'] else 'no'} | "
            f"{'yes' if r['stagger'] else 'no'} | {link} | "
            f"**{r['max_ttl_spread_s']}s** | "
            f"{r['propagation_s'] if r['propagation_complete'] else 'INCOMPLETE'}"
            f"{'s' if r['propagation_complete'] else ''} | "
            f"{r['author_lowest_fraction']} | {r['ill_warnings']} |")
    return "\n".join(out)


def _all_results():
    """Every run ever recorded, so the table accumulates."""
    rows = []
    for fn in sorted(os.listdir(RESULTS)):
        if fn.endswith(".json") and fn != "measurements.json":
            with open(os.path.join(RESULTS, fn)) as f:
                rows.append(json.load(f))
    return rows


def _write_summary(rows):
    path = os.path.join(RESULTS, "SUMMARY.md")
    header = ("# TTL divergence sweep\n\n"
              "Field reference (MonteNet, G10h4ck): 5 nodes, 30 s interval,\n"
              "2400 s TTL, long-distance radio — **22-27 s spread**, author\n"
              "holding the lowest TTL for its own key.\n\n"
              "`spread` is the largest TTL disagreement observed between\n"
              "nodes for the same entry. `author-lowest` counts samples in\n"
              "which the author held the minimum TTL for its own key.\n"
              "`ill` counts \"is remote peer ill?\" warnings.\n\n")
    body = _table(_all_results())
    with open(path, "w") as f:
        f.write(header + body + "\n")


if __name__ == "__main__":
    sys.exit(main())

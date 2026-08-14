#!/usr/bin/env python3
"""Single-factor experiment: which knob actually drives TTL divergence?

ITS OWN METRIC WAS CONFOUNDED — read this first (2026-08-13)
============================================================
This design fixed the sweep's attribution problem and then repeated its
measurement problem. `max_ttl_spread_s` is a maximum over a sampling
window whose LENGTH VARIED BY CELL: sampling probes every node, probing
a loaded node is slow, and this script never recorded how long its
window ran. `divergence_dynamics.py` measured those windows at 80 s to
488 s across the same cells.

Since TTL spread accumulates linearly (it is a rate, not a value), a
maximum over a longer window is mechanically larger. So the spread
column below is roughly `divergence rate x window length`, and the
cells with the biggest numbers are partly just the cells that were
watched longest.

What survives:
  - the ARM STRUCTURE, which is what this file was for — one knob at a
    time, pivot shared, repetitions outermost
  - the DIRECTION of every arm: slower configurations do diverge faster
  - the measured `sync` column, which is a direct timing and not
    windowed
  - the observation that spread is not a function of transfer duration
    alone, which is what prompted the follow-up

What does NOT survive: the magnitudes, and the conclusion drawn from
them that bandwidth scarcity amplifies divergence beyond what transfer
duration predicts. On a fixed window the slopes are 54.9 vs 58.3 per
100 s for +latency vs -bandwidth — nearly equal, against the 62 s vs
177 s reported here. That 3x was window length (236 s vs 488 s).

Anything comparing divergence magnitude across cells belongs in
`divergence_dynamics.py`, which fixes the window and reports a slope.
The design and the original reasoning are kept intact below, because
the failure mode — a confounded metric producing plausible numbers —
is the useful part.

WHY THIS EXISTS

`divergence_sweep.py` produced the headline result of this fork — 250
entries over a 256 kbit link diverged **112 s**, against 3 s for one
entry on a lab bridge — and then, in its own docstring, admitted the
result does not isolate a cause:

    "The 112 s result changed entry count, link rate and latency
     simultaneously, so it does not isolate a cause. Every configuration
     here has one run, with no repetitions and no confidence intervals."

It also withdrew the claim that `chain-5x30` vs `directed-5x30` was a
clean latency comparison: that pair changes topology directionality too,
and 42 s/81 s are end-to-end dissemination times, not link latency. So
"propagation delay cancels exactly" currently rests on a mechanism
argument (a sender serializes its *already decayed* TTL, so time spent
in flight is charged to both copies alike) with no controlled experiment
behind it. This is that experiment.

DESIGN

One pivot configuration; each arm moves exactly one knob away from it.
Everything else — node count, topology, directionality, update
interval, clock stagger, bleach TTL — is identical in every cell.

    pivot   5 nodes, bidirectional chain, 30 s interval, 2400 s TTL,
            40 ms delay, 512 kbit, 250 entries

    latency   delay_ms  in  10 / 40* / 400      (transfer size fixed)
    rate      rate_kbit in  2048 / 512* / 128   (transfer size fixed)
    size      entries   in  25 / 250* / 1000    (link fixed)

    (* = the pivot cell, shared by all three arms, so its repetitions
    count once and anchor all three)

Seven distinct cells. `--reps N` repeats the whole set N times, and
repetitions are the OUTER loop: stopping early leaves a *balanced*
dataset rather than three reps of one cell and none of another.

WHAT IS MEASURED, AND WHAT THAT BUYS

The sweep recorded only spread, so a rising spread could never be
attributed. Each run here also measures transfer duration **directly**:
a cold observer node, present in the topology but linked to nobody and
never started, runs the binary's own one-shot `sync` against a populated
node and is timed. That is a real client transfer of a real full state
over the same shaped link — not bytes divided by nominal rate. Process
startup is measured separately and reported alongside, so it can be
subtracted; merge time is deliberately *not* subtracted, because the
receiver's clock keeps running while it merges and that is part of the
divergence the mechanism describes.

That upgrades the question from "does spread rise?" to the falsifiable
one: **does spread track measured transfer duration regardless of which
knob produced it?** If it does, one relationship explains all three
arms. If spread rises with latency at constant transfer duration, the
mechanism argument is wrong and the fork's central claim needs redoing.

A CONFOUND, STATED UP FRONT

Latency is not perfectly orthogonal to transfer duration: TCP slow start
means a 400 ms link finishes a transfer slower than a 10 ms one at the
same nominal rate. So the latency arm cannot hold transfer duration
exactly fixed. This is precisely why transfer duration is measured
rather than assumed — the arm is read against measured transfer time,
not against the delay knob. If spread in the latency arm sits on the
same spread-vs-transfer relationship as the other two arms, latency has
no effect of its own; if it sits above it, latency contributes
independently.

WHAT THIS STILL WILL NOT SETTLE

That this is *the* MonteNet mechanism rather than *a* mechanism
sufficient to produce MonteNet-scale numbers. Field confirmation needs
field data.

USAGE

    python3 experiments/single_factor.py                  # 3 reps, all cells
    python3 experiments/single_factor.py --reps 1         # one pass, ~30 min
    python3 experiments/single_factor.py --only latency
    python3 experiments/single_factor.py --bin /path/to/shared-state-async

Every run is written to experiments/results/single-factor/ as its own
timestamped JSON the moment it finishes, so an interrupted session keeps
everything it already paid for. ANALYSIS.md is rebuilt from every record
on disk on each invocation.
"""

import argparse
import datetime
import json
import os
import re
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import ensure_inner, Mesh, DEFAULT_BIN  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import provenance  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "results", "single-factor")
ILL = re.compile(r"is remote peer ill")

TYPE = "wifi_links_info"
NAMES = ["jime", "balcon", "tronco", "e-bob", "nodo-suri"]
OBSERVER = "obs"

# Held constant in every cell. Chosen to match the field reference the
# sweep is calibrated against: MonteNet is 5 nodes, 30 s interval,
# 2400 s TTL, and reports 22-27 s of spread.
PIVOT = dict(nodes=5, interval=30, bleach_ttl=2400, stagger=True,
             directed=False, delay_ms=40, rate_kbit=512, entries=250)

ARMS = {
    "latency": ("delay_ms", [10, 40, 400]),
    "rate": ("rate_kbit", [2048, 512, 128]),
    "size": ("entries", [25, 250, 1000]),
}

ENTRY_PAD = 160          # bytes of filler per entry, as in the sweep
SAMPLES = 4              # TTL observations per run
SAMPLE_EVERY = 6         # seconds between them
SYNC_MEASUREMENTS = 3    # timed observer syncs per run


def cells():
    """Every distinct configuration, tagged with the arms it belongs to.

    The pivot appears in all three arms but is one physical
    configuration, so it is run once per repetition and read three
    times. Running it per-arm would triple its cost and, worse, give it
    three times the repetitions of every other cell.
    """
    out = {}
    for arm, (knob, values) in ARMS.items():
        for v in values:
            cfg = dict(PIVOT)
            cfg[knob] = v
            name = "pivot" if cfg == PIVOT else f"{knob}={v}"
            if name in out:
                out[name]["arms"].append(arm)
                continue
            out[name] = {"name": name, "cfg": cfg, "arms": [arm],
                         "knob": knob, "value": v}
    return list(out.values())


def _bulk_payload(author, n):
    return json.dumps({
        f"pad-{i}": {"gen": 1, "src": author, "blob": "x" * ENTRY_PAD}
        for i in range(n)})


MERGED = re.compile(r"got (\d+) significative changes")
RECEIVED = re.compile(r"Total received bytes: (\d+)")


def _measure_sync(obs, target, cfg):
    """Time the binary's own one-shot client sync over the shaped link.

    The observer is wiped and re-registered before each measurement:
    after one sync it holds the full state, and a second sync would
    upload it too, measuring a different transfer than the one the
    mechanism is about (a node receiving state it does not have).

    Exit status is NOT the success signal here. `SharedStateCli::sync`
    unconditionally prepends and appends the *local instance* address
    (app/shared_state_cli.cc:284,310 — "first to get local instance
    state", "last to sync collected changes back"), so on a node with no
    daemon both localhost legs fail with ECONNREFUSED and the process
    exits 111 even though the leg we are timing succeeded. Refused
    connections on loopback return immediately and are not shaped by the
    veth qdisc, so they cost microseconds inside the timed window.
    Success is read from the merge the binary reports instead, which
    doubles as a check that the entries really crossed.
    """
    samples = []
    # Wrap-and-exec overhead, so it can be subtracted from the sync
    # times. Running the binary with no arguments prints usage and
    # exits, which is process startup and nothing else.
    t0 = time.time()
    obs.cli("", timeout=60)
    overhead = time.time() - t0

    for _ in range(SYNC_MEASUREMENTS):
        obs.clean_state()
        obs.seed_config()
        obs.cli(f"register {TYPE} community {cfg['interval']} "
                f"{cfg['bleach_ttl']}", timeout=60)
        t0 = time.time()
        res = obs.cli(f"sync {TYPE} {target.ip}", timeout=900)
        elapsed = time.time() - t0
        log = (res.stderr or "") + (res.stdout or "")
        merged = [int(m) for m in MERGED.findall(log)]
        if not merged or max(merged) == 0:
            continue
        got = RECEIVED.findall(log)
        samples.append({"s": round(elapsed, 2),
                        "entries_merged": max(merged),
                        "bytes": max(int(b) for b in got) if got else None})
    return samples, overhead


def run_cell(cell, binary, rundir, rep):
    cfg = cell["cfg"]
    names = NAMES[:cfg["nodes"]]
    interval = cfg["interval"]
    # Slow links make everything slow, probes included. A deadline that
    # is too tight reports INCOMPLETE for a run that would have
    # converged, which reads as a finding and is not one.
    budget = max(240, interval * cfg["nodes"] * 3)
    probe_to = 120

    with Mesh(names + [OBSERVER], rundir, binary=binary) as mesh:
        offsets = mesh.stagger_clocks(interval) if cfg["stagger"] else {}
        nodes = mesh.bootstrap(TYPE, update_interval=interval,
                               bleach_ttl=cfg["bleach_ttl"],
                               nodes=[mesh.node(n) for n in names],
                               full_mesh=False)
        mesh.chain(names=names, directed=cfg["directed"])

        # The observer is configured and impaired like everyone else but
        # linked to nobody and never started, so it cannot influence the
        # run it is used to measure.
        obs = mesh.node(OBSERVER)
        obs.clean_state()
        obs.seed_config()
        obs.set_peers([])

        for n in nodes + [obs]:
            mesh.impair(n, delay_ms=cfg.get("delay_ms"),
                        rate_kbit=cfg.get("rate_kbit"))

        author = nodes[0]
        key = author.name
        if cfg["entries"]:
            author.cli(f"insert {TYPE}", stdin=_bulk_payload(author.name,
                                                             cfg["entries"]),
                       timeout=600)
        t_pub = time.time()
        author.publish(TYPE, key, gen=1, timeout=120)

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
        state_bytes = None
        if complete:
            for _ in range(SAMPLES):
                row = {}
                for n in nodes:
                    try:
                        st = n.probe(TYPE, timeout=probe_to)
                    except Exception:
                        st = None
                    if st is None:
                        continue
                    if state_bytes is None:
                        state_bytes = len(json.dumps(st))
                    e = st.get(key)
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

        sync, overhead = _measure_sync(obs, nodes[-1], cfg)
        ill = sum(len(ILL.findall(n.read_log())) for n in nodes)

    return {
        "cell": cell["name"],
        "arms": cell["arms"],
        "rep": rep,
        **cfg,
        "clock_offsets": offsets,
        "propagation_complete": complete,
        "propagation_s": (round(max(reached_at.values()), 1)
                          if complete else None),
        "reached": {k: round(v, 1) for k, v in reached_at.items()},
        "max_ttl_spread_s": worst,
        "spread_row": worst_row,
        "ttl_samples": samples,
        "author_lowest_fraction": (f"{author_lowest}/{samples}"
                                   if samples else "n/a"),
        "state_bytes": state_bytes,
        # measured over the shaped link, not derived from nominal rate
        "sync": sync,
        "sync_s_median": (round(statistics.median([s["s"] for s in sync]), 2)
                          if sync else None),
        "sync_wire_bytes": (max((s["bytes"] or 0) for s in sync) or None
                            if sync else None),
        "sync_entries_merged": (max(s["entries_merged"] for s in sync)
                                if sync else None),
        "cli_overhead_s": round(overhead, 2),
        "ill_warnings": ill,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bin", default=DEFAULT_BIN)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--only", nargs="*", default=None,
                    help="arm names (latency/rate/size) or cell names")
    ap.add_argument("--analyze-only", action="store_true",
                    help="rebuild ANALYSIS.md from records already on disk")
    args = ap.parse_args()

    os.makedirs(RESULTS, exist_ok=True)
    if args.analyze_only:
        _write_analysis()
        print(f"analysis rebuilt: {os.path.join(RESULTS, 'ANALYSIS.md')}")
        return 0

    if not os.path.exists(args.bin):
        print(f"binary not found: {args.bin}")
        return 2

    selected = cells()
    if args.only:
        want = set(args.only)
        selected = [c for c in selected
                    if c["name"] in want or want & set(c["arms"])]
    if not selected:
        print("no cells selected")
        return 2

    ensure_inner()
    print(f"[single-factor] {len(selected)} cells x {args.reps} reps "
          f"= {len(selected) * args.reps} runs", flush=True)

    # Repetitions outermost: an interrupted session leaves a balanced
    # dataset rather than a complete first cell and nothing else.
    for rep in range(1, args.reps + 1):
        for cell in selected:
            tag = f"{cell['name']} rep{rep}"
            print(f"[single-factor] {tag} ...", flush=True)
            rundir = os.path.join("/tmp/ss-single-factor",
                                  cell["name"].replace("=", "-"))
            try:
                res = run_cell(cell, args.bin, rundir, rep)
            except Exception as e:
                res = {"cell": cell["name"], "arms": cell["arms"], "rep": rep,
                       **cell["cfg"], "error": f"{type(e).__name__}: {e}"}
            res["provenance"] = provenance.collect(args.bin, __file__)
            res["binary"] = os.path.realpath(args.bin)
            stamp = datetime.datetime.now(datetime.timezone.utc).strftime(
                "%Y%m%dT%H%M%SZ")
            res["timestamp"] = stamp
            fn = f"{cell['name'].replace('=', '-')}-rep{rep}-{stamp}.json"
            with open(os.path.join(RESULTS, fn), "w") as f:
                json.dump(res, f, indent=1, sort_keys=True)
            print(f"[single-factor] {tag}: spread="
                  f"{res.get('max_ttl_spread_s')}s "
                  f"sync={res.get('sync_s_median')}s "
                  f"prop={res.get('propagation_s')}s "
                  f"{res.get('error', '')}", flush=True)
            _write_analysis()

    print(f"\nresults: {RESULTS}")
    print(_arm_tables(_records()))
    return 0


# -- analysis --------------------------------------------------------------

def _records():
    out = []
    if not os.path.isdir(RESULTS):
        return out
    for fn in sorted(os.listdir(RESULTS)):
        if fn.endswith(".json"):
            with open(os.path.join(RESULTS, fn)) as f:
                out.append(json.load(f))
    return out


def _fmt(vals):
    """median (min-max) over repetitions, or a single value verbatim."""
    vals = [v for v in vals if v is not None]
    if not vals:
        return "-"
    if len(vals) == 1:
        return f"{vals[0]:g}"
    return (f"{statistics.median(vals):g} "
            f"({min(vals):g}-{max(vals):g})")


def _arm_tables(records):
    ok = [r for r in records if not r.get("error")]
    out = []
    for arm, (knob, values) in ARMS.items():
        out.append(f"\n### {arm} arm — varying `{knob}`\n")
        # `Total received bytes` is RS_DBG3 and is not emitted at the
        # default log level, so sync_wire_bytes is normally null and this
        # column falls back to the probed state size — JSON length, close
        # to but not identical with what crossed the wire.
        out.append("| " + knob + " | n | measured sync | TTL spread | "
                   "state bytes | propagation | author-lowest | ill |")
        out.append("|---|---|---|---|---|---|---|---|")
        for v in values:
            rs = [r for r in ok
                  if arm in r.get("arms", []) and r.get(knob) == v]
            if not rs:
                out.append(f"| {v} | 0 | - | - | - | - | - | - |")
                continue
            mark = " *" if all(r["cell"] == "pivot" for r in rs) else ""
            out.append(
                f"| {v}{mark} | {len(rs)} | "
                f"{_fmt([r.get('sync_s_median') for r in rs])}s | "
                f"**{_fmt([r.get('max_ttl_spread_s') for r in rs])}s** | "
                f"{_fmt([r.get('sync_wire_bytes') or r.get('state_bytes') for r in rs])} | "
                f"{_fmt([r.get('propagation_s') for r in rs])}s | "
                f"{'/'.join(r.get('author_lowest_fraction', '-') for r in rs)} | "
                f"{_fmt([r.get('ill_warnings') for r in rs])} |")
    errs = [r for r in records if r.get("error")]
    if errs:
        out.append("\n### runs that errored\n")
        for r in errs:
            out.append(f"- `{r['cell']}` rep{r.get('rep')}: {r['error'][:160]}")
    return "\n".join(out)


def _pooled(records):
    """Every run as (measured sync, spread), pooled across all arms.

    This is the load-bearing table. The arms answer "does this knob
    move spread?"; pooling answers the question that actually
    discriminates the mechanism — whether spread is a function of
    transfer duration *whatever* produced it. Points from the latency
    arm landing off the curve traced by the other two would falsify it.
    """
    rows = [(r.get("sync_s_median"), r.get("max_ttl_spread_s"),
             r["cell"], r.get("rep"))
            for r in records
            if not r.get("error") and r.get("sync_s_median") is not None
            and r.get("max_ttl_spread_s") is not None]
    rows.sort(key=lambda t: t[0])
    out = ["\n### spread against measured transfer duration (all arms pooled)\n",
           "| measured sync | TTL spread | ratio | cell | rep |",
           "|---|---|---|---|---|"]
    for sync, spread, cell, rep in rows:
        ratio = f"{spread / sync:.1f}" if sync else "-"
        out.append(f"| {sync:g}s | {spread:g}s | {ratio} | `{cell}` | {rep} |")
    if len(rows) >= 3:
        xs = [r[0] for r in rows]
        ys = [r[1] for r in rows]
        out.append(f"\nPearson r over {len(rows)} runs: "
                   f"**{_pearson(xs, ys):.3f}**  "
                   f"(a correlation over pooled observational runs, not an "
                   f"effect size; the arms above are what isolate causes)")
    return "\n".join(out)


def _pearson(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = sum((x - mx) ** 2 for x in xs) ** 0.5
    dy = sum((y - my) ** 2 for y in ys) ** 0.5
    return num / (dx * dy) if dx and dy else float("nan")


def _write_analysis():
    records = _records()
    done = len([r for r in records if not r.get("error")])
    header = (
        "# Single-factor experiment: what drives TTL divergence\n\n"
        "Auto-generated by `single_factor.py`; rebuilt from every record\n"
        "in this directory on each run. Do not hand-edit.\n\n"
        f"Completed runs on disk: **{done}** "
        f"(plus {len(records) - done} errored).\n\n"
        "One pivot configuration — 5 nodes, bidirectional chain, 30 s\n"
        "interval, 2400 s bleach TTL, staggered clocks — with each arm\n"
        "moving exactly one knob. Cells marked `*` are the pivot itself,\n"
        "shared by all three arms.\n\n"
        "`measured sync` is wall-clock for the binary's own one-shot\n"
        "`sync` performed by a cold observer over the same shaped link:\n"
        "a real full-state transfer, not bytes over nominal rate. It\n"
        "includes merge time (the receiver's clock runs during merge, so\n"
        "that is part of the divergence) and process startup (reported\n"
        "per-run as `cli_overhead_s` so it can be subtracted).\n\n"
        "Cells show median (min-max) across repetitions.\n")
    body = _arm_tables(records) + "\n" + _pooled(records) + "\n"
    with open(os.path.join(RESULTS, "ANALYSIS.md"), "w") as f:
        f.write(header + body)


if __name__ == "__main__":
    sys.exit(main())

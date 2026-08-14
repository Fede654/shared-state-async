#!/usr/bin/env python3
"""Does TTL divergence saturate, or keep growing with observation time?

WHY THIS REPLACED THE CONTENTION SCRIPT

This started as a test of one hypothesis — that the `rate_kbit=128`
anomaly in `single_factor.py` was link contention. That hypothesis is
now dead, and the way it died matters more than the hypothesis did.

The first four runs said:

  cell        utilisation  throughput  backlog   spread   window
  pivot           0.289     148 kbit    16/40      38 s     80 s
  delay400        0.292     149 kbit    30/40     132 s    236 s
  rate128         0.485      62 kbit    29/40     372 s    488 s

Three things fell out, and two of them are about the measurement rather
than the system:

1. NOT SATURATION. I predicted mesh links at or near saturation under
   `rate128`. They sit at 48.5% with zero drops. The prediction failed.

2. BACKLOG IS NOT A CONTENTION SIGNAL HERE. `delay400` shows the same
   backlog occupancy as `rate128` (30/40 vs 29/40) with a third of the
   divergence, because netem implements `delay` by holding packets in
   its queue. Under an added delay a non-empty backlog is mechanical.
   Every cell here carries a 40 ms baseline, so the metric was never
   measuring congestion. It is dropped.

3. THE SPREAD METRIC WAS CONFOUNDED BY ITS OWN WINDOW. Spread was a
   maximum over samples, and the windows differed 6x (80 s to 488 s)
   because probing a loaded node takes longer. A maximum over a longer
   window is mechanically larger. Normalised per window-second the
   ordering survives but collapses from 38/132/372 to 0.475/0.559/0.762
   — so most of that headline was window length, not divergence.

Point 3 also puts a question mark over `single_factor.py`, whose
windows varied for the same reason and which did not record them. That
is the question this script now exists to answer, because it decides
whether those 21 runs mean what they appear to mean.

WHAT IS MEASURED NOW

A FIXED wall-clock observation window, identical across cells, with
every sample timestamped. That yields the thing no previous run
recorded: spread as a function of elapsed time.

  - if spread SATURATES quickly, window length is nearly irrelevant and
    the single-factor arms stand as measured
  - if spread GROWS without settling, then "TTL spread" is not a
    property of a configuration at all but of a configuration AND an
    observation duration, and every spread number this fork has
    published — including the sweep's 112 s and this experiment's 177 s
    — needs restating as a rate, not a value

A fixed window also makes utilisation directly comparable, and probe
latency is now recorded as a metric in its own right rather than being
allowed to silently stretch the window: a daemon that takes 60 s to
answer a probe it answers in 10 s when idle is reporting availability
loss, which is the T4/T5 failure mode showing up here uninvited.

WHAT THE FIRST RUN OF THIS SCRIPT STILL GOT WRONG (fixed 2026-08-14)

External review found the fixed window solved the window-length error
but did not make the cells intervention-equivalent. Two real defects:

1. ROWS WERE SKEWED, AND THE SKEW SCALED WITH THE CELL. Probes ran
   sequentially over five nodes and the row got ONE timestamp. A probe
   occupies the daemon's serial accept loop, blocking ~11 s under
   bandwidth scarcity, so a row spanned ~5 s at pivot but ~56 s at
   rate128. TTL decays 1 s/s, so first-vs-last node differed by the row
   duration from decay ALONE — up to ~56 s of artificial spread against
   a reported 57 s. Now probes run concurrently, every probe is
   individually timestamped, and the residual skew is corrected TO
   FIRST ORDER by normalising each TTL to the row's reference time —
   not exactly, since TTL advances in integer bleach ticks and t_done
   follows serialisation and delivery. `spread_raw`, `row_span_s` and
   per-node timestamps are retained so the correction's size is visible.

2. OBSERVATION LOAD WAS ADAPTIVE. Fast cells got many more full-state
   probe sessions than slow ones (31 rows vs 5), so the cells differed
   in how hard they were poked as well as in their configuration.
   `--control` runs the same cell sampling only at the window's two
   ends; if its endpoints match the probed run's, probing is not
   driving the result.

One sub-claim of that review does NOT hold, and it matters for reading
these numbers. netem on `v{i}h` shapes traffic TOWARD the node, so a
host-originated probe's RESPONSE is unshaped — measured, 16x less
bandwidth costs a probe 0.22 s (0.33 s -> 0.55 s) while a node-to-node
sync of the same state goes 0.98 s -> 9.28 s. Therefore probe latency
is daemon BLOCKING time, not transfer time, and the qdisc `Sent`
counters (bytes toward the node) never included probe responses, so the
throughput figures are gossip. That makes the intervention larger than
review argued, not smaller — an 11 s probe is 11 s of blocked daemon.

    python3 experiments/divergence_dynamics.py            # 3 cells x 3 reps
    python3 experiments/divergence_dynamics.py --reps 1
    python3 experiments/divergence_dynamics.py --only rate128
    python3 experiments/divergence_dynamics.py --window 300
    python3 experiments/divergence_dynamics.py --control  # no-probe control
"""

import argparse
import datetime
import json
import os
import re
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from harness import ensure_inner, Mesh, DEFAULT_BIN, sh  # noqa: E402
import single_factor as sf  # noqa: E402
import provenance  # noqa: E402

RESULTS = os.path.join(HERE, "results", "dynamics")

CELLS = {
    "pivot": dict(sf.PIVOT),
    "rate128": {**sf.PIVOT, "rate_kbit": 128},
    "delay400": {**sf.PIVOT, "delay_ms": 400},
}

SENT = re.compile(r"Sent (\d+) bytes (\d+) pkt "
                  r"\(dropped (\d+), overlimits (\d+)")

WINDOW = 300.0        # fixed observation window, seconds
MIN_GAP = 5.0         # minimum seconds between sample rounds
MARKS = (60, 120, 180, 240, 300)   # elapsed times to report spread at

CLK_TCK = os.sysconf("SC_CLK_TCK")


def _cpu_ticks(node):
    """CPU seconds for one node's daemon, via its process group.

    `Node.daemon_pids()` cannot do this: it matches
    `pgrep -f '<binary> peer'` and every node runs that same command
    line. Nodes start with setsid, so the process group isolates one
    node. Matching on the group also avoids /proc `comm`, which
    truncates at 15 characters and has already produced one wrong match
    in this suite.
    """
    if not node.proc:
        return None
    try:
        pgid = os.getpgid(node.proc.pid)
    except (ProcessLookupError, PermissionError):
        return None
    total = 0
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat") as f:
                raw = f.read()
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        close = raw.rfind(")")
        if close < 0:
            continue
        fields = raw[close + 2:].split()
        if len(fields) < 13 or fields[2] != str(pgid):
            continue
        total += int(fields[11]) + int(fields[12])
    return total / CLK_TCK


def _qdisc(dev):
    out = sh(f"tc -s qdisc show dev {dev}", check=False).stdout or ""
    m = SENT.search(out)
    if not m:
        return None
    return {"bytes": int(m.group(1)), "pkts": int(m.group(2)),
            "dropped": int(m.group(3)), "overlimits": int(m.group(4))}


def run_cell(name, cfg, binary, rundir, rep, window, control=False):
    names = sf.NAMES[:cfg["nodes"]]
    interval = cfg["interval"]
    budget = max(240, interval * cfg["nodes"] * 3)

    with Mesh(names + [sf.OBSERVER], rundir, binary=binary) as mesh:
        offsets = mesh.stagger_clocks(interval)
        nodes = mesh.bootstrap(sf.TYPE, update_interval=interval,
                               bleach_ttl=cfg["bleach_ttl"],
                               nodes=[mesh.node(n) for n in names],
                               full_mesh=False)
        mesh.chain(names=names, directed=cfg["directed"])

        obs = mesh.node(sf.OBSERVER)
        obs.clean_state()
        obs.seed_config()
        obs.set_peers([])
        for n in nodes + [obs]:
            mesh.impair(n, delay_ms=cfg.get("delay_ms"),
                        rate_kbit=cfg.get("rate_kbit"))

        author = nodes[0]
        key = author.name
        author.cli(f"insert {sf.TYPE}",
                   stdin=sf._bulk_payload(author.name, cfg["entries"]),
                   timeout=600)
        t_pub = time.time()
        author.publish(sf.TYPE, key, gen=1, timeout=120)

        reached = {}
        deadline = time.time() + budget
        while time.time() < deadline and len(reached) < len(nodes):
            for n in nodes:
                if n.name in reached:
                    continue
                try:
                    if n.probe(sf.TYPE, timeout=180).get(key) is not None:
                        reached[n.name] = time.time() - t_pub
                except Exception:
                    pass
            if len(reached) < len(nodes):
                time.sleep(3)
        complete = len(reached) == len(nodes)

        # -- fixed observation window ----------------------------------
        devs = {n.name: f"v{n.index}h" for n in nodes}
        devs[obs.name] = f"v{obs.index}h"
        q0 = {nm: _qdisc(d) for nm, d in devs.items()}
        cpu0 = {n.name: _cpu_ticks(n) for n in nodes}
        t0 = time.time()

        series = []
        while time.time() - t0 < window:
            row = _sample_row(nodes, key, t0)
            if row:
                series.append(row)
            if control:
                # No-probe control: sample once at each end of the window
                # and nothing in between, so the run carries almost none
                # of the observation load. If its endpoints match the
                # probed run's, probing is not driving the result.
                sleep_for = window - (time.time() - t0) - 1
                if sleep_for > 0:
                    time.sleep(sleep_for)
                row = _sample_row(nodes, key, t0)
                if row:
                    series.append(row)
                break
            remaining = window - (time.time() - t0)
            if remaining > MIN_GAP:
                time.sleep(MIN_GAP)

        elapsed = time.time() - t0
        q1 = {nm: _qdisc(d) for nm, d in devs.items()}
        cpu1 = {n.name: _cpu_ticks(n) for n in nodes}
        sync, overhead = sf._measure_sync(obs, nodes[-1], cfg)

    rate_bps = (cfg.get("rate_kbit") or 0) * 1000.0
    links = {}
    for nm in devs:
        a, b = q0.get(nm), q1.get(nm)
        if not a or not b:
            continue
        dbytes = b["bytes"] - a["bytes"]
        links[nm] = {
            "bytes": dbytes,
            "kbit_s": round(dbytes * 8.0 / elapsed / 1000.0, 1),
            "utilisation": (round(dbytes * 8.0 / (rate_bps * elapsed), 3)
                            if rate_bps else None),
            "dropped": b["dropped"] - a["dropped"],
        }
    mesh_links = {k: v for k, v in links.items() if k != obs.name}
    cpu = {nm: round((cpu1[nm] - cpu0[nm]) / elapsed, 4)
           for nm in cpu0 if cpu0.get(nm) is not None
           and cpu1.get(nm) is not None}

    spreads = [s["spread"] for s in series]
    return {
        "cell": name, "rep": rep, **cfg,
        "clock_offsets": offsets,
        "propagation_complete": complete,
        "propagation_s": round(max(reached.values()), 1) if complete else None,
        "window_s": round(elapsed, 1),
        "samples": len(series),
        "spread_series": series,
        "control_no_probe": control,
        "row_span_s_max": (max(s["row_span_s"] for s in series)
                           if series else None),
        "spread_raw_last": (series[-1]["spread_raw"] if series else None),
        # the point of the whole script: spread at MATCHED elapsed times,
        # so cells are comparable regardless of how slowly they sample
        "spread_at": {str(m): _at(series, m) for m in MARKS},
        "spread_first": spreads[0] if spreads else None,
        "spread_last": spreads[-1] if spreads else None,
        "spread_max": max(spreads) if spreads else None,
        "spread_growth_per_100s": _slope(series),
        "probe_s_mean": (round(statistics.mean([s["probe_s"] for s in series]), 2)
                         if series else None),
        "probe_s_max": (round(max(s["probe_max_s"] for s in series), 2)
                        if series else None),
        "links": links,
        "mesh_kbit_s": (round(statistics.median(
            [v["kbit_s"] for v in mesh_links.values()]), 1)
            if mesh_links else None),
        "mesh_utilisation": (round(statistics.median(
            [v["utilisation"] for v in mesh_links.values()
             if v["utilisation"] is not None]), 3) if rate_bps else None),
        "observer_kbit_s": links.get(obs.name, {}).get("kbit_s"),
        "mesh_drops": sum(v["dropped"] for v in mesh_links.values()),
        "cpu_fraction": cpu,
        "cpu_fraction_median": (round(statistics.median(cpu.values()), 4)
                                if cpu else None),
        "sync_s_median": (round(statistics.median([s["s"] for s in sync]), 2)
                          if sync else None),
        "cli_overhead_s": round(overhead, 2),
    }


def _sample_row(nodes, key, t0):
    """One observation of every node's TTL, probed CONCURRENTLY.

    Two errors are fixed here, both found by external review of the
    first run.

    SEQUENTIAL PROBING SKEWED THE ROW. Probes used to run one node after
    another. A probe occupies the daemon's serial accept loop, and under
    bandwidth scarcity it blocked for ~11 s, so a five-node row spanned
    ~56 s while being stamped with a single timestamp. TTL decays 1 s/s,
    so the first and last node in a row differed by the row duration from
    decay alone — up to ~56 s of purely artificial spread against a
    reported spread of 57 s. Probing concurrently cuts the span to
    roughly one probe.

    THE REMAINING SKEW IS CORRECTED TO FIRST ORDER — not exactly.
    A TTL read at t_i is normalised to the row's reference time by
    subtracting (t_ref - t_i). The sign is right and the bulk of the
    artefact goes away, but this is NOT exact: TTL advances in integer
    bleach ticks rather than continuously, and t_done is measured after
    server-side serialisation and response delivery, so a residual of
    order one tick survives. `spread` is the corrected quantity;
    `spread_raw`, `row_span_s` and the per-node timestamps are all
    retained so the correction's size stays visible, and the --control
    run is what bounds the residual empirically.

    Note the probe response is NOT shaped by the qdisc (netem on v{i}h
    shapes traffic toward the node; measured: 16x less bandwidth costs a
    probe 0.22 s), so probe latency is daemon blocking time, not
    transfer time. That is why concurrency helps at all.
    """
    import threading

    out = {}
    lock = threading.Lock()

    def one(n):
        tp = time.time()
        try:
            e = n.probe(sf.TYPE, timeout=180).get(key)
        except Exception:
            e = None
        done = time.time()
        with lock:
            out[n.name] = {"entry": e, "t_start": tp, "t_done": done}

    threads = [threading.Thread(target=one, args=(n,)) for n in nodes]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    got = {nm: v for nm, v in out.items() if v["entry"]}
    if len(got) != len(nodes):
        return None

    # Per-node timings, relative to the run's t0, so the first-order
    # correction can be recomputed — or rejected — by anyone reading the
    # record, without trusting this function. Without these, `spread` is
    # an unverifiable derived number.
    #
    # ROUNDED FIRST, ON PURPOSE. The correction below is computed from
    # these ALREADY-ROUNDED values, not from the full-precision clock
    # readings. Computing at full precision and persisting rounded ones
    # made `spread` irreproducible from the record: several rows came
    # back 0.1 s away from the stored value, which is exactly the kind
    # of small unexplained discrepancy that costs a result its
    # credibility. Now the record is self-consistent by construction and
    # `--validate` enforces it.
    obs = {nm: {"ttl": v["entry"]["ttl"],
                "start": round(v["t_start"] - t0, 2),
                "done": round(v["t_done"] - t0, 2)}
           for nm, v in got.items()}
    raw = {nm: o["ttl"] for nm, o in obs.items()}
    t_ref = max(o["done"] for o in obs.values())
    corrected = {nm: o["ttl"] - (t_ref - o["done"]) for nm, o in obs.items()}
    span = t_ref - min(o["start"] for o in obs.values())
    lat = [o["done"] - o["start"] for o in obs.values()]

    return {
        "t": round(t_ref, 1),
        "spread": round(max(corrected.values()) - min(corrected.values()), 1),
        "spread_raw": max(raw.values()) - min(raw.values()),
        "row_span_s": round(span, 1),
        "ttls": raw,
        "obs": obs,
        "t_ref": t_ref,
        "probe_s": round(statistics.mean(lat), 2),
        "probe_max_s": round(max(lat), 2),
    }


def recompute_row(row):
    """Recompute a row's spread from its persisted fields alone.

    Deliberately duplicates the arithmetic rather than sharing code with
    `_sample_row`, so it checks the RECORD rather than agreeing with the
    function that wrote it.
    """
    obs = row["obs"]
    t_ref = max(o["done"] for o in obs.values())
    corrected = [o["ttl"] - (t_ref - o["done"]) for o in obs.values()]
    return round(max(corrected) - min(corrected), 1)


def validate_records():
    """Recompute every row of every record; require exact equality."""
    bad, checked = [], 0
    for r in _records():
        for i, row in enumerate(r.get("spread_series") or []):
            if "obs" not in row:
                bad.append((r.get("cell"), r.get("rep"), i, "no obs field"))
                continue
            checked += 1
            got = recompute_row(row)
            if got != row["spread"]:
                bad.append((r.get("cell"), r.get("rep"), i,
                            f"stored {row['spread']} != recomputed {got}"))
    return checked, bad


def _at(series, mark):
    """Spread at the sample nearest `mark` seconds, or None if the
    window never reached it. Nearest-sample, not interpolated: on a slow
    cell the samples are sparse and interpolation would invent
    precision the data does not have."""
    if not series:
        return None
    best = min(series, key=lambda s: abs(s["t"] - mark))
    if abs(best["t"] - mark) > 45:
        return None
    return {"t": best["t"], "spread": best["spread"]}


def _slope(series):
    """Least-squares growth of spread per 100 s. The single number that
    decides whether spread is a value or a rate."""
    if len(series) < 3:
        return None
    xs = [s["t"] for s in series]
    ys = [s["spread"] for s in series]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    return round(sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den * 100, 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bin", default=DEFAULT_BIN)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--only", nargs="*", default=None)
    ap.add_argument("--window", type=float, default=WINDOW)
    ap.add_argument("--control", action="store_true",
                    help="no-probe control: sample only at window ends")
    ap.add_argument("--validate", action="store_true",
                    help="recompute every stored row; require exact equality")
    ap.add_argument("--analyze-only", action="store_true")
    args = ap.parse_args()

    os.makedirs(RESULTS, exist_ok=True)
    if args.validate:
        checked, bad = validate_records()
        for cell, rep, i, why in bad:
            print(f"MISMATCH {cell} rep{rep} row{i}: {why}")
        print(f"validated {checked} rows, {len(bad)} mismatches")
        return 1 if bad else 0
    if args.analyze_only:
        _write_analysis()
        print(f"rebuilt {os.path.join(RESULTS, 'DYNAMICS.md')}")
        return 0
    if not os.path.exists(args.bin):
        print(f"binary not found: {args.bin}")
        return 2

    selected = [(n, c) for n, c in CELLS.items()
                if not args.only or n in args.only]
    ensure_inner()
    print(f"[dynamics] {len(selected)} cells x {args.reps} reps, "
          f"{args.window:g}s window", flush=True)

    for rep in range(1, args.reps + 1):
        for name, cfg in selected:
            print(f"[dynamics] {name} rep{rep} ...", flush=True)
            deps_before = provenance.manifest(__file__)
            try:
                res = run_cell(name, cfg, args.bin,
                               os.path.join("/tmp/ss-dynamics", name), rep,
                               args.window, control=args.control)
            except Exception as e:
                res = {"cell": name, "rep": rep, **cfg,
                       "error": f"{type(e).__name__}: {e}"}
            deps_after = provenance.manifest(__file__)
            res["provenance"] = provenance.collect(args.bin, __file__)
            # A run whose code changed underneath it is not a
            # measurement. Recorded rather than merely asserted, so the
            # record itself carries the evidence.
            res["deps_stable"] = (deps_before == deps_after)
            if not res["deps_stable"]:
                changed = sorted(
                    k for k in set(deps_before) | set(deps_after)
                    if deps_before.get(k) != deps_after.get(k))
                res["deps_changed_during_run"] = changed
                print(f"[dynamics] WARNING code changed during this cell: "
                      f"{changed}", flush=True)
            res["binary"] = os.path.realpath(args.bin)
            stamp = datetime.datetime.now(datetime.timezone.utc).strftime(
                "%Y%m%dT%H%M%SZ")
            res["timestamp"] = stamp
            with open(os.path.join(RESULTS,
                                   f"{name}-rep{rep}-{stamp}.json"), "w") as f:
                json.dump(res, f, indent=1, sort_keys=True)
            print(f"[dynamics] {name} rep{rep}: "
                  f"spread {res.get('spread_first')}->{res.get('spread_last')} "
                  f"growth={res.get('spread_growth_per_100s')}/100s "
                  f"n={res.get('samples')} "
                  f"probe={res.get('probe_s_mean')}s "
                  f"kbit={res.get('mesh_kbit_s')} "
                  f"{res.get('error', '')}", flush=True)
            _write_analysis()

    print(f"\nresults: {RESULTS}")
    return 0


def _records():
    if not os.path.isdir(RESULTS):
        return []
    out = []
    for fn in sorted(os.listdir(RESULTS)):
        if fn.endswith(".json"):
            with open(os.path.join(RESULTS, fn)) as f:
                out.append(json.load(f))
    return out


def _endpoint_growth(r):
    """Growth per 100 s from the two endpoints only.

    The control samples just twice, so a three-point OLS slope is not
    defined for it. Endpoints are the honest comparison there, and they
    are computed the same way for treatment rows so the two columns
    mean the same thing.
    """
    series = r.get("spread_series") or []
    if len(series) < 2:
        return None
    dt = series[-1]["t"] - series[0]["t"]
    if dt <= 0:
        return None
    return round((series[-1]["spread"] - series[0]["spread"]) / dt * 100, 1)


def _write_analysis():
    records = [r for r in _records() if not r.get("error")]
    lines = [
        "# Divergence dynamics: is TTL spread a value or a rate?\n",
        "Auto-generated by `divergence_dynamics.py`. Do not hand-edit.\n",
        "Every cell observes for the SAME fixed wall-clock window, so",
        "spread is comparable across cells — which it was not in earlier",
        "runs, where the window varied 6x with link speed and a",
        "max-over-window metric grew mechanically with it.\n",
        "**Treatment and control are tabulated separately and never",
        "pooled.** A control samples only at the window's two ends, so",
        "counting it as an ordinary repetition would mix two different",
        "interventions into one median. Rows are grouped by",
        "(cell, control_no_probe).\n",
        "`spread` is a FIRST-ORDER skew correction, not an exact one:",
        "each TTL is normalised to its row's reference time, but TTL",
        "advances in integer bleach ticks and `t_done` is taken after",
        "server-side serialisation and response delivery, so a residual",
        "of order one tick remains. `raw` (uncorrected) and `span` (row",
        "duration) are retained so the correction's size stays visible,",
        "and per-node probe timestamps are kept in each record. The",
        "control is what bounds the residual empirically.\n",
        "`growth/100s` is the OLS slope over all samples; `endpoint`",
        "uses only the first and last sample, which is the only form",
        "defined for a two-sample control.\n",
    ]

    def block(title, control, note):
        out = ["", f"### {title}", "", note, "",
               "| cell | n | spread first -> last | raw last | span |"
               " growth/100s | endpoint | at 60s | at 180s | at 300s |"
               " probe | mesh kbit/s | sync |",
               "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for name in CELLS:
            rs = [r for r in records if r["cell"] == name
                  and bool(r.get("control_no_probe")) is control]
            if not rs:
                out.append(f"| `{name}` | 0 |" + " - |" * 12)
                continue

            def at(mark):
                vals = [r.get("spread_at", {}).get(mark) for r in rs]
                vals = [v["spread"] for v in vals if v]
                return sf._fmt(vals) + "s" if vals else "-"

            slope = ("-" if control else
                     f"**{sf._fmt([r.get('spread_growth_per_100s') for r in rs])}**")
            out.append(
                f"| `{name}` | {len(rs)} | "
                f"{sf._fmt([r.get('spread_first') for r in rs])} -> "
                f"{sf._fmt([r.get('spread_last') for r in rs])}s | "
                f"{sf._fmt([r.get('spread_raw_last') for r in rs])}s | "
                f"{sf._fmt([r.get('row_span_s_max') for r in rs])}s | "
                f"{slope} | "
                f"{sf._fmt([_endpoint_growth(r) for r in rs])} | "
                f"{at('60')} | {at('180')} | {at('300')} | "
                f"{sf._fmt([r.get('probe_s_mean') for r in rs])}s | "
                f"{sf._fmt([r.get('mesh_kbit_s') for r in rs])} | "
                f"{sf._fmt([r.get('sync_s_median') for r in rs])}s |")
        return out

    lines += block(
        "Treatment — probed throughout the window", False,
        "Sampled repeatedly, so these carry the full observation load.")
    lines += block(
        "Control — probed only at the window's two ends", True,
        "Almost no observation load. If `endpoint` here matches the "
        "treatment's `endpoint`, probing is not driving the result; if it "
        "is materially lower, the treatment numbers are partly self-"
        "inflicted and must be reported as an upper bound.")

    errs = [r for r in _records() if r.get("error")]
    if errs:
        lines.append("\n### errored runs\n")
        for r in errs:
            lines.append(f"- `{r['cell']}` rep{r.get('rep')}: {r['error'][:160]}")
    with open(os.path.join(RESULTS, "DYNAMICS.md"), "w") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    sys.exit(main())

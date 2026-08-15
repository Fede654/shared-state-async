#!/usr/bin/env python3
"""What happens AFTER the author's entry expires? (the T24 follow-up)

THE QUESTION THIS ANSWERS

`divergence_dynamics.py` measured TTL divergence as a rate over a fixed
five-minute window. The write-up then argued the effect was self-limiting
because the author's copy bleaches away at ~2431 s and "nothing refreshes
it". External review found that wrong in the code: `sharedstate.cc:866`
inserts a MISSING key before the own-authorship guard at `:882` can see
it, so an expired author adopts a neighbour's inflated echo instead of
staying empty. **T24** reproduces that deterministically.

T24 proves resurrection is POSSIBLE. It cannot show whether it RECURS,
because it injects a synthetic echo at a fixed TTL. That leaves the real
question open — does the entry ever actually die, or does the mesh keep
handing it back? — and "post-expiry behaviour is unmeasured" is now the
only unclosed claim in the divergence chapter.

So this runs the whole cycle organically: real nodes, real inflation
from real transfer duration, one publish and never again, watched for
many entry lifetimes.

WHY A SHORT TTL, AND WHAT THAT COSTS

The field TTL is 2400 s, so one cycle would take 40 minutes and a
ten-cycle run most of a day. This uses a short bleach TTL so a cycle
takes under a minute.

That trade is NOT free, and the limit is specific: shortening the TTL
raises the ratio of per-round inflation to entry lifetime, so **the
resurrection RATE measured here does not transfer to the field**. What
does transfer is the qualitative answer — whether the missing-key path
produces a self-sustaining loop at all, and whether an unrepublished
entry can outlive its own TTL. Read counts and the yes/no, not the
timings. (This is the same discipline the window-length error taught:
say which quantity the measurement supports.)

WHY PROBING CANNOT CAUSE THE EFFECT

`Node.probe()` sends an EMPTY slice, and `merge` only ever inserts keys
present in the incoming slice (`sharedstate.cc:864`). An observation
therefore cannot resurrect anything — the mechanism under test is
structurally unreachable from the observer. Probing does occupy the
daemon's serial accept loop, so it can shift TIMING; it cannot
manufacture a resurrection event.

Usage:
    python3 experiments/post_expiry.py                  # default cell
    python3 experiments/post_expiry.py --window 900
    python3 experiments/post_expiry.py --bleach-ttl 30
"""

import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from harness import ensure_inner, Mesh, DEFAULT_BIN       # noqa: E402
import single_factor as sf                                # noqa: E402
import provenance                                         # noqa: E402

RESULTS = os.path.join(HERE, "results", "post-expiry")

# The +latency cell from divergence_dynamics: it produced 55.9 s per
# 100 s of divergence, so neighbours run meaningfully ahead of the
# author — which is the precondition for resurrection. On a fast bridge
# every node decays in lockstep and the entry would simply die.
CELL = {"nodes": 5, "interval": 5, "delay_ms": 400, "rate_kbit": 512,
        "entries": 25, "directed": False}

SAMPLE_GAP = 3.0


def _sample(nodes, key, t0):
    """Presence AND absence of `key` at every node, probed concurrently.

    Deliberately not `divergence_dynamics._sample_row`, which returns
    None unless every node holds the key — it treats a missing entry as
    an unusable row, because it was written to measure spread among
    nodes that all have one. Here the missing entry IS the observation.
    """
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
            out[n.name] = {"ttl": (e or {}).get("ttl"),
                           "present": e is not None,
                           "gen": ((e or {}).get("data") or {}).get("gen"),
                           "start": round(tp - t0, 2),
                           "done": round(done - t0, 2)}

    threads = [threading.Thread(target=one, args=(n,)) for n in nodes]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    present = [v for v in out.values() if v["present"]]
    ttls = [v["ttl"] for v in present]
    return {
        "t": round(time.time() - t0, 1),
        "present_count": len(present),
        "ttls": {nm: v["ttl"] for nm, v in out.items()},
        "spread_raw": (max(ttls) - min(ttls)) if len(ttls) > 1 else None,
        "obs": out,
    }


def analyse(series, author_name, node_names):
    """Resurrection events and extinction, derived from the timeline."""
    events = []
    for nm in node_names:
        prev = None
        for row in series:
            now = row["obs"][nm]["present"]
            # A resurrection is an absent -> present transition. The
            # author's own transitions are the ones that matter: nothing
            # republished, so any return is an adopted echo.
            if prev is False and now is True:
                events.append({"node": nm, "t": row["t"],
                               "ttl_on_return": row["obs"][nm]["ttl"],
                               "gen": row["obs"][nm]["gen"]})
            prev = now

    author_events = [e for e in events if e["node"] == author_name]
    all_absent = [r["t"] for r in series if r["present_count"] == 0]
    final_absent = series[-1]["present_count"] == 0 if series else None

    # Mesh-wide extinction only counts if nothing came back afterwards.
    extinct_at = None
    if all_absent:
        last_present = [r["t"] for r in series if r["present_count"] > 0]
        if not last_present or all_absent[-1] > last_present[-1]:
            extinct_at = next(t for t in all_absent
                              if not [p for p in last_present if p > t])

    return {
        "resurrections_total": len(events),
        "resurrections_at_author": len(author_events),
        "author_events": author_events,
        "author_max_ttl_on_return": (max((e["ttl_on_return"] or 0)
                                         for e in author_events)
                                     if author_events else None),
        "ever_fully_absent": bool(all_absent),
        "first_full_absence_t": all_absent[0] if all_absent else None,
        "mesh_extinct_at": extinct_at,
        "absent_at_end": final_absent,
        "max_spread_raw": max((r["spread_raw"] or 0) for r in series)
        if series else None,
        "samples": len(series),
    }


def run(window, bleach_ttl, binary, rundir):
    names = sf.NAMES[:CELL["nodes"]]
    with Mesh(names, rundir, binary=binary) as mesh:
        offsets = mesh.stagger_clocks(CELL["interval"])
        nodes = mesh.bootstrap(sf.TYPE, update_interval=CELL["interval"],
                               bleach_ttl=bleach_ttl,
                               nodes=[mesh.node(n) for n in names],
                               full_mesh=False)
        mesh.chain(names=names, directed=CELL["directed"])
        for n in nodes:
            mesh.impair(n, delay_ms=CELL["delay_ms"],
                        rate_kbit=CELL["rate_kbit"])

        author = nodes[0]
        key = author.name
        # Bulk payload so a sync takes real time — transfer duration is
        # what inflates a neighbour's TTL above the author's.
        author.cli(f"insert {sf.TYPE}",
                   stdin=sf._bulk_payload(author.name, CELL["entries"]),
                   timeout=600)
        author.publish(sf.TYPE, key, gen=1, timeout=120)
        t0 = time.time()

        insert_ttl = None
        series = []
        while time.time() - t0 < window:
            row = _sample(nodes, key, t0)
            if insert_ttl is None and row["obs"][author.name]["ttl"]:
                insert_ttl = row["obs"][author.name]["ttl"]
            series.append(row)
            time.sleep(SAMPLE_GAP)

    return {
        "cell": dict(CELL, bleach_ttl=bleach_ttl),
        "window_s": window,
        "sample_gap_s": SAMPLE_GAP,
        "author": author.name,
        "clock_offsets": offsets,
        "author_insert_ttl": insert_ttl,
        "series": series,
        "analysis": analyse(series, author.name, names),
        "provenance": provenance.collect(binary, __file__),
    }


if __name__ == "__main__":
    def opt(name, default, cast=int):
        return cast(sys.argv[sys.argv.index(name) + 1]) \
            if name in sys.argv else default

    ensure_inner()   # re-exec into the unprivileged namespace

    window = opt("--window", 600)
    bleach_ttl = opt("--bleach-ttl", 20)
    # Impairment overrides: the precondition for resurrection is a
    # neighbour holding a MEANINGFULLY higher TTL when the author's copy
    # dies, so the interesting axis is how much inflation accumulates
    # within one entry lifetime.
    if "--rate-kbit" in sys.argv:
        CELL["rate_kbit"] = opt("--rate-kbit", CELL["rate_kbit"])
    if "--delay-ms" in sys.argv:
        CELL["delay_ms"] = opt("--delay-ms", CELL["delay_ms"])
    if "--entries" in sys.argv:
        CELL["entries"] = opt("--entries", CELL["entries"])
    tag = opt("--tag", "", str)
    # DEFAULT_BIN, not a hand-counted path: dirname-counting from
    # experiments/ lands on <repo>/tests, the exact off-by-one that once
    # wrote a null harness into every provenance record.
    binary = opt("--bin", DEFAULT_BIN, str)
    os.makedirs(RESULTS, exist_ok=True)

    rec = run(window, bleach_ttl, binary, "/tmp/ss-post-expiry")
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    path = os.path.join(
        RESULTS, f"post-expiry-ttl{bleach_ttl}{'-' + tag if tag else ''}"
                 f"-{stamp}.json")
    with open(path, "w") as f:
        json.dump(rec, f, indent=1, sort_keys=True)

    a = rec["analysis"]
    print(f"\n{path}")
    print(f"  author insert TTL      : {rec['author_insert_ttl']}s")
    print(f"  samples                : {a['samples']} over {window}s")
    print(f"  resurrections (author) : {a['resurrections_at_author']}")
    print(f"  resurrections (any)    : {a['resurrections_total']}")
    print(f"  max TTL on return      : {a['author_max_ttl_on_return']}")
    print(f"  ever absent mesh-wide  : {a['ever_fully_absent']}"
          f" (first at t={a['first_full_absence_t']})")
    print(f"  mesh extinct           : {a['mesh_extinct_at']}")
    print(f"  absent at window end   : {a['absent_at_end']}")

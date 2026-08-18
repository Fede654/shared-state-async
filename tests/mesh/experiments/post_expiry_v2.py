#!/usr/bin/env python3
"""Post-expiry v2 runner — passive capture, arms, topologies (Phase 1–3).

Extends post_expiry.py (v1, untouched — it remains the record of what
ran) with the amended v2 design:

ARMS (`--arm`): "treatment/control" does not exist in passive runs.
  passive  zero probes; observation is packet capture only. The primary
           production arm. Propagation, absence and witnesses come from
           the wire (wiredecode.py, offline).
  probes   v1-style dense probing. Observer-effect diagnostic arm, and
           the capture-off arm of the timing check (--no-capture).
  both     probes AND capture. Decoder validation only — the record is
           labelled validation and is never a confirmatory cell.

TOPOLOGIES (`--topology`): exact adjacencies fixed by the plan; nodes
a–e in bootstrap order.
  chain  a-b, b-c, c-d, d-e          (anchor, = v1)
  ring   chain + e-a
  chord  chain + b-d
The declared edge set is written into the record so the decoder can
verify it against observed flows (a mismatch invalidates the run).

LIVENESS: a connection-free supervisor thread polls proc.poll() for
every daemon AND capture process (never opening a connection, so it
cannot occupy the accept loop). Premature exit of either invalidates
the run. It also records capture-ready-before-daemon and
clean-stop-after-daemon ordering.

DOSE: for probed arms the record carries cumulative probe connection
duration per lifetime — probe count is not dose, service time varies.

Usage:
  python3 post_expiry_v2.py --arm passive [--topology ring] [--window 480]
  python3 post_expiry_v2.py --arm both --tag validation
  python3 post_expiry_v2.py --arm probes --no-capture --tag offpair
"""

import hashlib
import json
import os
import re
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from harness import ensure_inner, Mesh, DEFAULT_BIN       # noqa: E402
import single_factor as sf                                # noqa: E402
import post_expiry as pe                                  # noqa: E402
import provenance                                         # noqa: E402
import wirecap                                            # noqa: E402

RESULTS = os.path.join(HERE, "results", "post-expiry-v2")
PCAPS = os.path.join(RESULTS, "pcaps")           # hashed, not committed

TOPOLOGIES = {
    "chain": [(0, 1), (1, 2), (2, 3), (3, 4)],
    "ring":  [(0, 1), (1, 2), (2, 3), (3, 4), (4, 0)],
    "chord": [(0, 1), (1, 2), (2, 3), (3, 4), (1, 3)],
}

# One merge log line per handled slice, timestamped by RsDbg
# (sharedstate.cc:910, compiled in at debug level 2). Gossip merges have
# input slice size > 0; probe merges are size 0 and are filtered out.
# These timestamps are the log-side timing source available in BOTH
# capture-on and capture-off arms — which is what the capture-on/off
# equivalence check compares (capture_check.py).
_MERGE_RE = re.compile(
    r"^D (\d+\.\d+) .*? got (\d+) significative changes out of (\d+)"
    r" input slice size: (\d+) state size: (\d+)\s*$")


def harvest_merge_events(nodes):
    """Per-node timestamps of gossip merges (slice size > 0), from the
    daemon logs, plus each log's hash for provenance."""
    out = {}
    for n in nodes:
        events = []
        text = n.read_log()
        for line in text.splitlines():
            m = _MERGE_RE.match(line)
            if m and int(m.group(4)) > 0:
                events.append(round(float(m.group(1)), 3))
        out[n.name] = {
            "gossip_merge_ts_unix": events,
            "log_sha256": hashlib.sha256(text.encode()).hexdigest(),
        }
    return out


def apply_topology(mesh, names, topology):
    """Install peer sets for the declared adjacency; returns edge list
    as (name, name) tuples for the record."""
    nodes = [mesh.node(n) for n in names]
    peers = {n.name: [] for n in nodes}
    for a, b in TOPOLOGIES[topology]:
        peers[nodes[a].name].append(nodes[b])
        peers[nodes[b].name].append(nodes[a])
    for n in nodes:
        n.set_peers(peers[n.name])
    return [(nodes[a].name, nodes[b].name) for a, b in TOPOLOGIES[topology]]


class Supervisor(threading.Thread):
    """Connection-free liveness: proc.poll() only, for daemons+capture."""

    def __init__(self, nodes, capture, poll_s=2.0):
        super().__init__(daemon=True)
        self.nodes = nodes
        self.capture = capture
        self.poll_s = poll_s
        self.events = []
        self.polls = 0
        self._stop = threading.Event()

    def run(self):
        known_dead = set()
        while not self._stop.is_set():
            self.polls += 1
            now = round(time.time(), 3)
            for n in self.nodes:
                proc = getattr(n, "proc", None)
                if proc is not None and proc.poll() is not None \
                        and ("daemon", n.name) not in known_dead:
                    known_dead.add(("daemon", n.name))
                    self.events.append({"t_unix": now, "kind": "daemon-exit",
                                        "name": n.name,
                                        "code": proc.returncode})
            if self.capture is not None:
                for name, alive in self.capture.alive().items():
                    if not alive and ("capture", name) not in known_dead:
                        known_dead.add(("capture", name))
                        self.events.append({"t_unix": now,
                                            "kind": "capture-exit",
                                            "name": name})
            self._stop.wait(self.poll_s)

    def stop(self):
        self._stop.set()
        self.join(timeout=5)
        return {"polls": self.polls, "poll_interval_s": self.poll_s,
                "premature_exits": self.events,
                "ok": not self.events}


def probe_dose(series, configured_insert_ttl, window):
    """Cumulative probe connection duration per lifetime (the dose
    covariate the plan requires — probe COUNT is not dose)."""
    total = 0.0
    n_probes = 0
    for row in series:
        for obs in row["obs"].values():
            total += max(0.0, obs["done"] - obs["start"])
            n_probes += 1
    lifetimes = window / configured_insert_ttl
    return {"probe_connections": n_probes,
            "cumulative_probe_conn_s": round(total, 1),
            "window_lifetimes": round(lifetimes, 2),
            "probe_conn_s_per_lifetime": (round(total / lifetimes, 1)
                                          if lifetimes else None)}


def run(arm, topology, window, bleach_ttl, binary, rundir,
        capture_on=True, tag="", run_id=None):
    names = sf.NAMES[:pe.CELL["nodes"]]
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    # A driver executing a frozen schedule supplies the slot's atomic
    # run ID; ad-hoc runs get a self-describing stamped one.
    run_id = run_id or (f"pe2-{arm}-{topology}-ttl{bleach_ttl}"
                        f"{'-' + tag if tag else ''}-{stamp}")
    deps_before = provenance.manifest(__file__)

    capture = None
    sup = None
    with Mesh(names, rundir, binary=binary) as mesh:
        offsets = mesh.stagger_clocks(pe.CELL["interval"])
        node_ips = {n: mesh.node(n).ip for n in names}

        # Capture attaches BEFORE any daemon starts (ordering recorded).
        capture_attached = None
        if capture_on:
            capture = wirecap.Capture(mesh, PCAPS, run_id=run_id)
            capture_attached = capture.start()

        daemons_started_at = round(time.time(), 3)
        nodes = mesh.bootstrap(sf.TYPE, update_interval=pe.CELL["interval"],
                               bleach_ttl=bleach_ttl,
                               nodes=[mesh.node(n) for n in names],
                               full_mesh=False)
        declared_edges = apply_topology(mesh, names, topology)
        for n in nodes:
            mesh.impair(n, delay_ms=pe.CELL["delay_ms"],
                        rate_kbit=pe.CELL["rate_kbit"])

        sup = Supervisor(nodes, capture)
        sup.start()

        author = nodes[0]
        key = author.name
        author.cli(f"insert {sf.TYPE}",
                   stdin=sf._bulk_payload(author.name, pe.CELL["entries"]),
                   timeout=600)
        author.publish(sf.TYPE, key, gen=1, timeout=120)
        t0 = time.time()

        series = []
        insert_ttl = None
        if arm in ("probes", "both"):
            while time.time() - t0 < window:
                row = pe._sample(nodes, key, t0)
                if insert_ttl is None and row["obs"][author.name]["ttl"]:
                    insert_ttl = row["obs"][author.name]["ttl"]
                series.append(row)
                time.sleep(pe.SAMPLE_GAP)
        else:
            # Passive: no connections at all. The supervisor is the only
            # observer on the host side; everything else is on the wire.
            while time.time() - t0 < window:
                time.sleep(min(5.0, max(0.1, window - (time.time() - t0))))

        sup_report = sup.stop()
        merge_events = harvest_merge_events(nodes)
        # Ordering is load-bearing: daemons stop FIRST (so the capture
        # window covers every daemon packet), capture stops SECOND, and
        # only then does Mesh.__exit__ tear the namespaces down — the
        # capturers run under `ip netns exec`, so deleting the netns
        # first would kill them mid-file instead of flushing cleanly.
        for n in nodes:
            n.stop()
        daemons_stopped_at = round(time.time(), 3)
        cap_summary = capture.stop() if capture else None

    configured_insert_ttl = bleach_ttl + pe.CELL["interval"] + 1
    deps_after = provenance.manifest(__file__)

    probed = arm in ("probes", "both")
    propagated_probe = None
    if probed:
        confirming = next(
            (r for r in series
             if r["valid"] and r["present_count"] == len(names)
             and pe._before_expiry(r["t"], configured_insert_ttl)), None)
        propagated_probe = confirming is not None

    rec = {
        "run_id": run_id,
        "schema": "post-expiry-v2/1",
        "arm": arm,
        "observation_mode": ("both" if arm == "both" else
                             "probes" if probed and capture_on is False
                             else "probes+capture" if probed
                             else "passive"),
        "validation_only": arm == "both",
        "topology": topology,
        "declared_edges": declared_edges,
        "declared_edges_ips": [[node_ips[a], node_ips[b]]
                               for a, b in declared_edges],
        "node_ips": node_ips,
        "cell": dict(pe.CELL, bleach_ttl=bleach_ttl),
        "window_s": window,
        "sample_gap_s": pe.SAMPLE_GAP if probed else None,
        "author": names[0],
        "clock_offsets": offsets,
        "configured_insert_ttl": configured_insert_ttl,
        "first_observed_author_ttl": insert_ttl,
        "t0_unix": round(t0, 3),
        "daemons_started_at_unix": daemons_started_at,
        "daemons_stopped_at_unix": daemons_stopped_at,
        "capture": cap_summary,
        "capture_attached_before_daemons": capture_attached,
        "supervisor": sup_report,
        "merge_events": merge_events,
        "series": series,
        "analysis": (pe.analyse(series, names[0], names)
                     if probed else None),
        "analysis_provenance": (pe.analysis_provenance()
                                if probed else None),
        "probe_dose": (probe_dose(series, configured_insert_ttl, window)
                       if probed else None),
        "propagation_confirmed_probe": propagated_probe,
        "deps_stable": deps_before == deps_after,
        "deps_changed": sorted(k for k in set(deps_before) | set(deps_after)
                               if deps_before.get(k) != deps_after.get(k)),
        "observations_valid": (all(r["valid"] for r in series)
                               if probed else None),
        # Acquisition-side validity. Wire-side validity (capture
        # completeness details, propagation for passive arms, topology
        # match) is finalized by wiredecode.py into <record>.decode.json;
        # a v2 run is citable only when BOTH are true.
        "record_valid_acquisition": (
            deps_before == deps_after
            and sup_report["ok"]
            and (capture_attached is not False)
            and (cap_summary is None or cap_summary["capture_ok"])
            and (propagated_probe is not False)
            and (not probed or all(r["valid"] for r in series))),
        "provenance": provenance.collect(binary, __file__),
    }
    return rec


if __name__ == "__main__":
    def opt(name, default, cast=int):
        return cast(sys.argv[sys.argv.index(name) + 1]) \
            if name in sys.argv else default

    ensure_inner()
    arm = opt("--arm", "passive", str)
    topology = opt("--topology", "chain", str)
    window = opt("--window", 480)
    bleach_ttl = opt("--bleach-ttl", 90)
    tag = opt("--tag", "", str)
    binary = opt("--bin", DEFAULT_BIN, str)
    capture_on = "--no-capture" not in sys.argv
    if arm not in ("passive", "probes", "both"):
        sys.exit(f"unknown arm {arm}")
    if topology not in TOPOLOGIES:
        sys.exit(f"unknown topology {topology}")
    if arm == "passive" and not capture_on:
        sys.exit("a passive run without capture observes nothing")
    if "--rate-kbit" in sys.argv:
        pe.CELL["rate_kbit"] = opt("--rate-kbit", pe.CELL["rate_kbit"])
    if "--delay-ms" in sys.argv:
        pe.CELL["delay_ms"] = opt("--delay-ms", pe.CELL["delay_ms"])
    if "--sample-gap" in sys.argv:
        # Dose equalization hook: the v2 driver sets the gap so probed
        # cells get a constant number of samples per LIFETIME, not per
        # second (the recorded dose covariate stays the measured
        # connection duration, not this knob).
        pe.SAMPLE_GAP = opt("--sample-gap", pe.SAMPLE_GAP, float)
    run_id = opt("--run-id", None, str)

    os.makedirs(RESULTS, exist_ok=True)
    os.makedirs(PCAPS, exist_ok=True)
    rec = run(arm, topology, window, bleach_ttl, binary,
              "/tmp/ss-post-expiry-v2", capture_on=capture_on, tag=tag,
              run_id=run_id)
    path = os.path.join(RESULTS, rec["run_id"] + ".json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rec, f, indent=1, sort_keys=True)
    os.replace(tmp, path)

    print(f"\n{path}")
    print(f"  arm/topology            : {rec['arm']} / {rec['topology']}"
          + ("  [VALIDATION ONLY]" if rec["validation_only"] else ""))
    print(f"  capture ok              : "
          f"{(rec['capture'] or {}).get('capture_ok')}"
          f"  attached-before-daemons: "
          f"{rec['capture_attached_before_daemons']}")
    print(f"  supervisor ok           : {rec['supervisor']['ok']}"
          f"  ({rec['supervisor']['polls']} polls)")
    if rec["arm"] != "passive":
        a = rec["analysis"]
        print(f"  probe evidence >=       : "
              f"{a['resurrection_evidence_count']}"
              f"  dose: {rec['probe_dose']['probe_conn_s_per_lifetime']}"
              f" s/lifetime")
    print(f"  RECORD VALID (acquisition): {rec['record_valid_acquisition']}")
    print("  wire-side validity pending: run "
          f"wiredecode.py --record {path}")
    if not rec["record_valid_acquisition"]:
        sys.exit(1)

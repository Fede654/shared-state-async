#!/usr/bin/env python3
"""Rolling upgrade of a live mesh: T23 blindness, measured dynamically.

HRM-152(a). EXPLORATORY CHARACTERIZATION — descriptive, no
preregistration, no confirmatory statistics; labelled as such in the
record.

A 5-node chain starts all-v1, every node authoring its own key and
republishing a fresh generation every REPUBLISH_S. Nodes are then
upgraded one at a time to `merge_with_version` — stop daemon, WIPE
STATE (a real upgrade reboots and /tmp evaporates), re-register with
the new binary, restart — while passive capture watches the wire.
Nothing probes anything.

What T23 predicts (deterministically shown by t23_mixed_version_interop;
here we watch it happen to a living mesh): an upgraded node rejects
every slice from a v1 peer at deserialization, so it goes blind to
v1-authored state; v1 peers keep accepting v2 entries. Additionally,
a v1 relay drops the unknown mVersion field, so v2 entries relayed
THROUGH a v1 node arrive unversioned at the next v2 node and are
rejected there too — v2 islands separated by v1 nodes cannot see each
other. The coverage matrix over time (fleet_decode.py) makes all of
this visible from outbound slices alone.

Usage: python3 rolling_upgrade.py [--v2-bin PATH]
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
import post_expiry as pe                                  # noqa: E402
import provenance                                         # noqa: E402
import wirecap                                            # noqa: E402

RESULTS = os.path.join(HERE, "results", "fleet")
PCAPS = os.path.join(RESULTS, "pcaps")
V2_BIN_DEFAULT = os.path.expanduser("~/REPOS/ss-jbrk/build/shared-state-async")

INTERVAL = 5
BLEACH_TTL = 90
WARMUP_S = 150
UPGRADE_EVERY_S = 90
TAIL_S = 180
REPUBLISH_S = 30


class Publisher(threading.Thread):
    """Every node republishes its own key (gen++) every REPUBLISH_S.
    This is the normal job of a shared-state node (hooks re-publish
    local data); it runs over localhost inside each node's namespace
    and never appears on the veth capture. Nodes currently down
    (mid-upgrade) are skipped and the skip is logged."""

    def __init__(self, nodes, t0, down, period=None):
        super().__init__(daemon=True)
        self.nodes = nodes
        self.t0 = t0
        self.period = period or REPUBLISH_S
        self.down = down              # set of node names, guarded by lock
        self.lock = threading.Lock()
        self.log = []
        self.gens = {n.name: 1 for n in nodes}
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            for n in self.nodes:
                with self.lock:
                    skip = n.name in self.down
                if skip:
                    self.log.append({"t": round(time.time() - self.t0, 1),
                                     "node": n.name, "skipped_down": True})
                    continue
                self.gens[n.name] += 1
                g = self.gens[n.name]
                r = n.publish(sf.TYPE, n.name, gen=g, timeout=60)
                self.log.append({"t": round(time.time() - self.t0, 1),
                                 "node": n.name, "gen": g,
                                 "ok": r.returncode == 0})
            self._stop.wait(self.period)

    def stop(self):
        self._stop.set()
        self.join(timeout=10)
        return self.log


def upgrade(node, v2_bin, mesh):
    """Stop, wipe (reboot semantics), re-register on the new binary,
    restart. Returns the disruption window (unix times)."""
    t_stop = time.time()
    node.stop()
    node.clean_state()
    node.seed_config()
    node.binary = v2_bin
    node.cli(f"register {sf.TYPE} community {INTERVAL} {BLEACH_TTL}",
             timeout=60)
    node.start()
    ok = node.wait_listening(timeout=15)
    return {"node": node.name, "t_stop_unix": round(t_stop, 3),
            "t_started_unix": round(time.time(), 3), "listening": ok}


def main():
    ensure_inner()

    def opt(name, default, cast=str):
        return cast(sys.argv[sys.argv.index(name) + 1]) \
            if name in sys.argv else default

    v2_bin = opt("--v2-bin", V2_BIN_DEFAULT)
    os.makedirs(RESULTS, exist_ok=True)
    os.makedirs(PCAPS, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    run_id = f"rolling-upgrade-{stamp}"
    names = sf.NAMES[:5]
    deps_before = provenance.manifest(__file__)

    with Mesh(names, "/tmp/ss-fleet", binary=DEFAULT_BIN) as mesh:
        offsets = mesh.stagger_clocks(INTERVAL)
        node_ips = {n: mesh.node(n).ip for n in names}

        capture = wirecap.Capture(mesh, PCAPS, run_id=run_id)
        attached = capture.start()

        nodes = mesh.bootstrap(sf.TYPE, update_interval=INTERVAL,
                               bleach_ttl=BLEACH_TTL,
                               nodes=[mesh.node(n) for n in names],
                               full_mesh=False)
        mesh.chain(names=names)
        for n in nodes:
            mesh.impair(n, delay_ms=pe.CELL["delay_ms"],
                        rate_kbit=pe.CELL["rate_kbit"])

        for n in nodes:                      # everyone authors its key
            n.publish(sf.TYPE, n.name, gen=1, timeout=120)
        t0 = time.time()

        down = set()
        pub = Publisher(nodes, t0, down)
        pub.start()

        unexpected = []

        def check_alive():
            for n in nodes:
                with pub.lock:
                    if n.name in down:
                        continue
                if n.proc is not None and n.proc.poll() is not None:
                    unexpected.append({"t": round(time.time() - t0, 1),
                                       "node": n.name})

        time.sleep(WARMUP_S)
        upgrade_events = []
        # Upgrade from the far end toward the author end: e, d, c, b, a.
        for name in reversed(names):
            check_alive()
            n = mesh.node(name)
            with pub.lock:
                down.add(name)
            ev = upgrade(n, v2_bin, mesh)
            # Re-impair: tc qdisc lives on the host-side veth and
            # survives the daemon restart, but assert anyway is cheap —
            # re-apply to be safe.
            mesh.impair(n, delay_ms=pe.CELL["delay_ms"],
                        rate_kbit=pe.CELL["rate_kbit"])
            with pub.lock:
                down.discard(name)
            ev["t_stop"] = round(ev["t_stop_unix"] - t0, 1)
            ev["t_started"] = round(ev["t_started_unix"] - t0, 1)
            upgrade_events.append(ev)
            time.sleep(UPGRADE_EVERY_S)
        time.sleep(TAIL_S)
        check_alive()

        publish_log = pub.stop()
        for n in nodes:
            n.stop()
        daemons_stopped = round(time.time(), 3)
        cap = capture.stop()

    deps_after = provenance.manifest(__file__)
    rec = {
        "run_id": run_id,
        "schema": "fleet-rolling-upgrade/1",
        "exploratory": True,
        "experiment": "rolling-upgrade",
        "topology": "chain",
        "declared_edges_ips": [[node_ips[a], node_ips[b]] for a, b in
                               zip(names, names[1:])],
        "node_ips": node_ips,
        "cell": {"nodes": 5, "interval": INTERVAL,
                 "bleach_ttl": BLEACH_TTL,
                 "delay_ms": pe.CELL["delay_ms"],
                 "rate_kbit": pe.CELL["rate_kbit"]},
        "configured_insert_ttl": BLEACH_TTL + INTERVAL + 1,
        "phases": {"warmup_s": WARMUP_S, "upgrade_every_s": UPGRADE_EVERY_S,
                   "tail_s": TAIL_S, "republish_s": REPUBLISH_S},
        "upgrade_order": list(reversed(names)),
        "upgrade_events": upgrade_events,
        # Disruption windows: sessions failing inside these are
        # attributable to the intentional stop/restart, not decoder or
        # capture failure. fleet_decode consumes them.
        "expected_disruption_windows": [
            [ev["t_stop_unix"], ev["t_started_unix"]]
            for ev in upgrade_events],
        "binaries": {"v1": provenance.collect(DEFAULT_BIN, __file__),
                     "v2": provenance.collect(v2_bin, __file__)},
        "publish_log": publish_log,
        "clock_offsets": offsets,
        "t0_unix": round(t0, 3),
        "daemons_stopped_at_unix": daemons_stopped,
        "capture": cap,
        "capture_attached_before_daemons": attached,
        "unexpected_daemon_exits": unexpected,
        "deps_stable": deps_before == deps_after,
        "record_valid_acquisition": (
            deps_before == deps_after and not unexpected
            and attached is not False and cap["capture_ok"]
            and all(ev["listening"] for ev in upgrade_events)),
    }
    path = os.path.join(RESULTS, run_id + ".json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rec, f, indent=1, sort_keys=True)
    os.replace(tmp, path)
    print(f"{path}\n  acquisition valid: {rec['record_valid_acquisition']}"
          f"  upgrades: {[e['node'] for e in upgrade_events]}"
          f"\n  decode with: python3 fleet_decode.py {path}")
    sys.exit(0 if rec["record_valid_acquisition"] else 1)


if __name__ == "__main__":
    main()

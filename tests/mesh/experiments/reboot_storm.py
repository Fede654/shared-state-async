#!/usr/bin/env python3
"""Reboot storm on an all-v2 fleet: the T11 recovery leapfrog, live.

HRM-152(b). EXPLORATORY CHARACTERIZATION — descriptive, no
preregistration, no confirmatory statistics.

Five nodes, all running `merge_with_version`, chain topology. Every
node authors its own key; the TARGET (middle node) republishes a fresh
generation every REPUBLISH_S, so its version counter climbs. The
target is then rebooted repeatedly — stop, WIPE STATE (state lives in
/tmp on real routers), restart — while passive capture watches.

What T11 predicts (deterministic test: t11_reboot_no_stale_adoption):
the rebooted node relearns its own key from a neighbour's echo (some
older generation) and the recovery clause promotes it ABOVE the
highest version it was offered — stale payload, top authority — until
the target's next own publish displaces it. From the wire we read, per
reboot cycle: the first own-key slice the target sends after restart
(gen + version), whether the gen regressed below what it had published
before the reboot, and whether the regressed gen propagated to
neighbours with the elevated version before the next publish repaired
it.

Usage: python3 reboot_storm.py [--v2-bin PATH]
"""

import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from harness import ensure_inner, Mesh                    # noqa: E402
import single_factor as sf                                # noqa: E402
import post_expiry as pe                                  # noqa: E402
import provenance                                         # noqa: E402
import wirecap                                            # noqa: E402
from rolling_upgrade import Publisher, RESULTS, PCAPS, \
    V2_BIN_DEFAULT                                        # noqa: E402

INTERVAL = 5
BLEACH_TTL = 90
WARMUP_S = 120
REBOOTS = 4
REBOOT_EVERY_S = 75
TAIL_S = 120
REPUBLISH_S = 25


def main():
    ensure_inner()

    def opt(name, default, cast=str):
        return cast(sys.argv[sys.argv.index(name) + 1]) \
            if name in sys.argv else default

    v2_bin = opt("--v2-bin", V2_BIN_DEFAULT)
    os.makedirs(RESULTS, exist_ok=True)
    os.makedirs(PCAPS, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    run_id = f"reboot-storm-{stamp}"
    names = sf.NAMES[:5]
    target_name = names[2]                 # middle of the chain
    deps_before = provenance.manifest(__file__)

    with Mesh(names, "/tmp/ss-fleet", binary=v2_bin) as mesh:
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

        for n in nodes:
            n.publish(sf.TYPE, n.name, gen=1, timeout=120)
        t0 = time.time()
        target = mesh.node(target_name)

        down = set()
        pub = Publisher(nodes, t0, down, period=REPUBLISH_S)
        pub.start()

        unexpected = []
        reboot_events = []
        time.sleep(WARMUP_S)
        for cycle in range(1, REBOOTS + 1):
            for n in nodes:
                with pub.lock:
                    if n.name in down:
                        continue
                if n.proc is not None and n.proc.poll() is not None:
                    unexpected.append({"t": round(time.time() - t0, 1),
                                       "node": n.name})
            with pub.lock:
                down.add(target_name)
            gen_before = pub.gens[target_name]
            t_stop = time.time()
            target.stop()
            target.clean_state()           # /tmp wipe: reboot semantics
            target.seed_config()
            target.cli(f"register {sf.TYPE} community {INTERVAL} "
                       f"{BLEACH_TTL}", timeout=60)
            target.start()
            ok = target.wait_listening(timeout=15)
            with pub.lock:
                down.discard(target_name)
            reboot_events.append({
                "cycle": cycle, "node": target_name,
                "t_stop_unix": round(t_stop, 3),
                "t_started_unix": round(time.time(), 3),
                "t_stop": round(t_stop - t0, 1),
                "t_started": round(time.time() - t0, 1),
                "listening": ok,
                "last_published_gen_before": gen_before})
            time.sleep(REBOOT_EVERY_S)
        time.sleep(TAIL_S)

        publish_log = pub.stop()
        for n in nodes:
            n.stop()
        daemons_stopped = round(time.time(), 3)
        cap = capture.stop()

    deps_after = provenance.manifest(__file__)
    rec = {
        "run_id": run_id,
        "schema": "fleet-reboot-storm/1",
        "exploratory": True,
        "experiment": "reboot-storm",
        "topology": "chain",
        "declared_edges_ips": [[node_ips[a], node_ips[b]] for a, b in
                               zip(names, names[1:])],
        "node_ips": node_ips,
        "target": target_name,
        "cell": {"nodes": 5, "interval": INTERVAL,
                 "bleach_ttl": BLEACH_TTL,
                 "delay_ms": pe.CELL["delay_ms"],
                 "rate_kbit": pe.CELL["rate_kbit"]},
        "configured_insert_ttl": BLEACH_TTL + INTERVAL + 1,
        "phases": {"warmup_s": WARMUP_S, "reboots": REBOOTS,
                   "reboot_every_s": REBOOT_EVERY_S, "tail_s": TAIL_S,
                   "republish_s": REPUBLISH_S},
        "reboot_events": reboot_events,
        "expected_disruption_windows": [
            [ev["t_stop_unix"], ev["t_started_unix"]]
            for ev in reboot_events],
        "binaries": {"v2": provenance.collect(v2_bin, __file__)},
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
            and all(ev["listening"] for ev in reboot_events)),
    }
    path = os.path.join(RESULTS, run_id + ".json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rec, f, indent=1, sort_keys=True)
    os.replace(tmp, path)
    print(f"{path}\n  acquisition valid: {rec['record_valid_acquisition']}"
          f"  reboots: {len(reboot_events)}"
          f"\n  decode with: python3 fleet_decode.py {path}")
    sys.exit(0 if rec["record_valid_acquisition"] else 1)


if __name__ == "__main__":
    main()

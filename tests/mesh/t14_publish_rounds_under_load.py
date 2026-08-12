"""T14 — publishing must not stop because some peers are unreachable.

Strict condition
    A node keeps publishing to the peers it *can* reach at roughly the
    configured cadence, even when other discovered peers are dead.

Why it matters
    Two defects compound here. `peer()` wakes every 999 ms and publishes
    only when `steady_seconds % updateInterval == 0`, so any iteration
    that overruns that exact second skips the round entirely (audit B3).
    And the loop syncs with discovered peers **sequentially with no
    timeout**, so a single blackholed address stalls it for the kernel's
    SYN-retry time — minutes (audit B2).

    Unreachable peers are not an edge case on a mesh: discovery returns
    neighbours that just rebooted, moved, or lost their radio. The result
    is that a node stops publishing to healthy neighbours because an
    unrelated one is down, which is exactly the "state refresh is too
    slow" complaint in lime-packages#1129.

Method
    A real peer of our own (`FakePeer`) counts how often the node
    actually syncs with it. Measure the healthy cadence, then add three
    blackholed addresses to discovery and measure again.

Expected on today's code: RED.

Fix that turns it green: connect timeouts, and scheduling by elapsed
time rather than by matching an exact second.
"""

import time

from fakepeer import FakePeer

ID = "T14"
TITLE = "unreachable peers do not stop publishing to reachable ones"
EXPECT_TODAY = "RED"

TYPE = "probe"
UPDATE_INTERVAL = 5
OBSERVE = 45
BLACKHOLES = ["10.99.0.201", "10.99.0.202", "10.99.0.203"]
MIN_RATIO = 0.5          # allow half the healthy cadence before failing


def run(mesh):
    node = mesh.node("lime-a")
    node.clean_state()
    node.seed_config()
    node.cli(f"register {TYPE} community {UPDATE_INTERVAL} 300", timeout=30)

    with FakePeer() as peer:
        # healthy: the node's only peer is our counting listener
        node.set_peers([])
        _write_discover(node, ["10.99.0.1"])
        node.start()
        if not node.wait_listening():
            return False, "daemon did not start"
        node.publish(TYPE, "k", gen=1)

        t0 = time.time()
        time.sleep(OBSERVE)
        healthy = peer.count_since(t0)

        # now discovery also returns addresses with nothing behind them
        _write_discover(node, ["10.99.0.1"] + BLACKHOLES)
        time.sleep(2)
        t1 = time.time()
        time.sleep(OBSERVE)
        degraded = peer.count_since(t1)

    expected = OBSERVE / UPDATE_INTERVAL
    if healthy == 0:
        return False, (f"baseline broken: node never synced with the "
                       f"counting peer in {OBSERVE}s (expected ~{expected:.0f})")

    ratio = degraded / healthy
    detail = (f"{healthy} syncs in {OBSERVE}s healthy "
              f"(~{expected:.0f} expected), {degraded} with "
              f"{len(BLACKHOLES)} blackholed peers added")
    if ratio >= MIN_RATIO:
        return True, detail + f" — cadence held ({ratio:.0%})"
    return False, (detail + f" — publishing to a healthy peer collapsed to "
                   f"{ratio:.0%} because unrelated peers were unreachable: "
                   f"the publish loop connects to each discovered peer in "
                   f"turn with no timeout")


def _write_discover(node, addrs):
    import os
    path = os.path.join(node.bindir, "shared-state-async-discover")
    with open(path, "w") as f:
        f.write("#!/bin/sh\n")
        for a in addrs:
            f.write(f"echo {a}\n")
    os.chmod(path, 0o755)

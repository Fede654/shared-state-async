"""T11 — after a reboot, generation ordering must not depend on TTL.

Strict condition
    A node that restarts with its state wiped converges to the newest
    generation of its own key present in the mesh, whatever order peers
    reach it in and whatever TTLs their copies carry.

Why it matters
    State lives in /tmp, so every reboot starts blank and the node
    relearns its own identity from whatever its neighbours still hold.
    Under v1 the only ordering signal is TTL, and TTL is uncorrelated
    with generation once copies have travelled different paths and been
    bleached on different clocks (audit C6): a node that was briefly
    partitioned holds an *old* generation with a *high* TTL. If the
    rebooted node adopts by TTL it republishes stale data as its own,
    with full author authority, and the mesh believes it.

Method
    Deterministic: wipe and restart the node, then offer it two versions
    of its own key as neighbours would — first an old generation with a
    high TTL, then the newest generation with a lower TTL.

Expected on today's code: RED — the newer generation is rejected for
having a lower TTL.

Fix that turns it green: version-counter merge. See also the `v2r`
amendment in the spec oracle for the related reboot-recovery hazard.
"""

import time

ID = "T11"
TITLE = "rebooted node adopts newest generation, not highest TTL"
EXPECT_TODAY = "RED"

TYPE = "probe"


def run(mesh):
    node = mesh.node("lime-a")
    mesh.bootstrap(TYPE, nodes=[node], full_mesh=False)
    node.set_peers([])
    key = node.name

    node.publish(TYPE, key, gen=3)
    if node.gen_of(node.probe(TYPE), key) != 3:
        return False, "setup: author did not hold gen 3"

    # reboot: process down, /tmp state gone, daemon back up
    node.stop()
    node.clean_state()
    node.seed_config()
    node.cli(f"register {TYPE} community 5 300", timeout=30)
    node.start()
    if not node.wait_listening():
        return False, "node did not come back after reboot"
    time.sleep(1)

    if node.probe(TYPE).get(key) is not None:
        return False, "state survived the wipe; test is not testing a reboot"

    # a long-partitioned neighbour offers an OLD generation with a HIGH ttl
    node.inject(TYPE, {key: {"author": node.name, "ttl": 280,
                             "data": {"gen": 3, "src": node.name},
                             "version": 3}})
    # an up-to-date neighbour offers the NEWEST generation with a lower ttl
    node.inject(TYPE, {key: {"author": node.name, "ttl": 240,
                             "data": {"gen": 7, "src": node.name},
                             "version": 7}})

    final = node.probe(TYPE)
    gen = node.gen_of(final, key)
    version = (final.get(key) or {}).get("version", 0)
    if gen == 7:
        return True, (f"adopted newest generation (7) despite its lower TTL "
                      f"(version={version})")

    # Two different defects produce the same wrong answer; say which.
    if version > 7:
        return False, (
            f"ECHO RESURRECTION: kept gen {gen} and promoted it to version "
            f"{version}, above the newest generation it was offered. The "
            f"reboot-recovery leapfrog fired for an entry the node never "
            f"published itself — it had just adopted that entry from an "
            f"echo — so stale data now carries top authority and will "
            f"propagate mesh-wide. This is the hazard the spec oracle "
            f"predicted; the `v2r` amendment (apply recovery only to "
            f"entries locally inserted since boot) prevents it.")
    return False, (f"adopted gen {gen} (version={version}): kept the older "
                   f"generation because it carried the higher TTL "
                   f"(280 vs 240)")

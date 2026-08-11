"""T2 — an author's updates must reach the whole mesh.

Strict condition
    After an author publishes generation N of its own key, every node in
    the mesh serves generation N within a deadline, and no node ever
    moves backwards to an older generation.

Why it matters
    G10h4ck's MonteNet measurements show the opposite happening: copies
    held by distant nodes carry higher TTLs than the author's own, so
    for tens of seconds the author literally cannot update its own
    entry — "is remote peer ill?" in the logs — and nodes behind the
    inflated copy never see the update at all.

Method
    Emergent, not crafted: three real daemons over impaired links, the
    author republishing on a cadence comparable to the sync interval,
    with the whole mesh probed once a second. Reports the worst
    convergence delay and every backwards step observed.

Expected verdict is recorded from observed behaviour, not assumed —
see EXPECT_TODAY.
"""

import subprocess
import time

ID = "T2"
TITLE = "author updates reach every node without regressions"
EXPECT_TODAY = "GREEN"

TYPE = "probe"
GENERATIONS = 4
PUBLISH_EVERY = 12       # seconds between generations
CONVERGE_DEADLINE = 45   # per generation
UPDATE_INTERVAL = 5
LINK_DELAY_MS = 200
LINK_LOSS_PCT = 10


def run(mesh):
    # chain, not full mesh: the far node is two hops from the author, so
    # updates must survive relaying. A full mesh hides relay failures
    # because the author reaches every node directly.
    nodes = mesh.bootstrap(TYPE, update_interval=UPDATE_INTERVAL,
                           full_mesh=False)
    mesh.chain()
    for n in nodes:
        mesh.impair(n, delay_ms=LINK_DELAY_MS, loss_pct=LINK_LOSS_PCT)

    author = nodes[0]
    key = author.name
    highest_seen = {n.name: 0 for n in nodes}
    regressions = []
    slowest = 0.0

    for gen in range(1, GENERATIONS + 1):
        try:
            author.publish(TYPE, key, gen=gen, timeout=60)
        except subprocess.TimeoutExpired:
            return False, (f"publishing gen {gen} hung: the daemon stopped "
                           f"serving its own CLI while syncing with peers "
                           f"(serial accept loop + no I/O timeouts)")
        t0 = time.time()
        converged_at = None
        while time.time() - t0 < CONVERGE_DEADLINE:
            gens = mesh.gens(TYPE, key)
            for name, g in gens.items():
                if g is None:
                    continue
                if g < highest_seen[name]:
                    regressions.append(
                        f"{name} {highest_seen[name]}->{g}")
                highest_seen[name] = max(highest_seen[name], g)
            if all(g == gen for g in gens.values()):
                converged_at = time.time() - t0
                break
            time.sleep(1)
        if converged_at is None:
            gens = mesh.gens(TYPE, key)
            return False, (f"gen {gen} never reached all nodes within "
                           f"{CONVERGE_DEADLINE}s: {gens}"
                           + (f"; regressions: {regressions[:4]}"
                              if regressions else ""))
        slowest = max(slowest, converged_at)
        remaining = PUBLISH_EVERY - (time.time() - t0)
        if remaining > 0:
            time.sleep(remaining)

    if regressions:
        return False, (f"all generations converged (worst {slowest:.0f}s) but "
                       f"{len(regressions)} backwards steps: {regressions[:4]}")
    return True, (f"{GENERATIONS} generations reached all nodes, "
                  f"worst convergence {slowest:.0f}s, no regressions")

"""T3 — the mesh must converge once traffic stops.

Strict condition
    After every author stops publishing, all nodes hold identical state
    for every key within a quiesce window, on lossy links.

Why it matters
    This is the weakest promise an eventually-consistent store can make;
    if it does not hold, "eventually consistent" is not a description of
    the system. The oracle showed v1 settling into author-islands — a
    node stuck on data the rest of the mesh has moved past, with the
    "is remote peer ill?" guard actively rejecting the correction.

Method
    Three authors publishing concurrently over lossy links, then silence
    and repeated sampling until the states match or the window expires.

Expected verdict is recorded from observed behaviour — see EXPECT_TODAY.
"""

import subprocess
import time

ID = "T3"
TITLE = "mesh converges after publishing stops (lossy links)"
EXPECT_TODAY = "GREEN"

TYPE = "probe"
ROUNDS = 3
PUBLISH_EVERY = 8
QUIESCE = 60
UPDATE_INTERVAL = 5
LINK_DELAY_MS = 80
LINK_LOSS_PCT = 20


def _fingerprint(state):
    """Comparable view of a node's state: key -> (author, gen)."""
    out = {}
    for key, entry in (state or {}).items():
        data = entry.get("data") or {}
        gen = data.get("gen") if isinstance(data, dict) else None
        out[key] = (entry.get("author"), gen)
    return out


def run(mesh):
    nodes = mesh.bootstrap(TYPE, update_interval=UPDATE_INTERVAL)
    for n in nodes:
        mesh.impair(n, delay_ms=LINK_DELAY_MS, loss_pct=LINK_LOSS_PCT)

    for gen in range(1, ROUNDS + 1):
        for n in nodes:
            try:
                n.publish(TYPE, n.name, gen=gen, timeout=45)
            except subprocess.TimeoutExpired:
                # Not a harness failure: the daemon serves its local CLI
                # from the same serial accept loop it serves peers from,
                # so a stalled peer session blocks local publishing too
                # (audit B1/B2). Report it as the defect it is.
                return False, (f"{n.name} could not publish gen {gen}: its "
                               f"daemon stopped serving the local CLI while "
                               f"peer sessions were in flight on lossy links "
                               f"(serial accept loop + no I/O timeouts)")
        time.sleep(PUBLISH_EVERY)

    def all_agree():
        snap = mesh.snapshot(TYPE)
        prints = {name: _fingerprint(st) for name, st in snap.items()}
        distinct = {repr(p) for p in prints.values()}
        return len(distinct) == 1, prints

    ok, prints = mesh.wait_until(all_agree, timeout=QUIESCE, interval=3)
    if ok:
        keys = len(next(iter(prints.values())))
        return True, f"all {len(prints)} nodes agree on {keys} keys"

    # report only what actually differs
    diffs = []
    all_keys = sorted({k for p in prints.values() for k in p})
    for k in all_keys:
        vals = {name: p.get(k) for name, p in prints.items()}
        if len({repr(v) for v in vals.values()}) > 1:
            diffs.append(f"{k}: {vals}")
    return False, (f"no convergence in {QUIESCE}s; "
                   f"{len(diffs)} divergent key(s): {diffs[:3]}")

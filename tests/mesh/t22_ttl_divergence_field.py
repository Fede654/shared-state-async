"""T22 — TTL must not diverge across nodes.

Strict condition
    All nodes holding the same entry agree on its remaining TTL to
    within a small bound, and no node ever rejects an author's own entry
    as coming from an "ill" peer.

Why it matters
    TTL is the *only* freshness signal the merge rule has (audit C6), so
    disagreement about TTL is disagreement about which copy is newer.
    G10h4ck measured exactly this at MonteNet: for `wifi_links_info`,
    the same keys carried TTLs 22–27 seconds apart across four nodes,
    and the author consistently held the **lowest** value for its own
    key — meaning every neighbour's copy outranked the author's.

Why three nodes were not enough
    Earlier runs (T2/T3) used a 5-second update interval and saw no
    meaningful divergence, because divergence is not caused by transit
    delay — it is caused by the *gap between an entry being authored and
    a given node first receiving it*. Each node starts bleaching from
    the TTL it received, on its own clock, so a node that hears about an
    entry one sync interval late holds a TTL one interval higher,
    forever. The spread is therefore bounded by (hops x update
    interval), which at 5 s over 2 hops is invisible and at MonteNet's
    30 s over 4 hops is exactly the 22–27 s that was measured.

    So this test uses the field's shape: five nodes in a line, a 30 s
    update interval and a 2400 s bleach TTL.

Method
    One author publishes once. Wait for the entry to reach all five
    nodes, then sample every node's TTL for it repeatedly and report the
    spread. Also count "is remote peer ill?" warnings, which is the
    author actively rejecting its own entry coming back inflated — the
    exact log line from the field report.

Expected on today's code: RED — divergence on the order of the update
interval per hop.

Fix that turns it green: stop using TTL as the ordering signal
(version-counter merge). Note that the version merge does *not* remove
the divergence itself — TTL still drifts — it removes the consequence,
because ordering no longer depends on it.
"""

import re
import time

ID = "T22"
TITLE = "TTL stays consistent across a 5-node chain (MonteNet shape)"
EXPECT_TODAY = "RED"

NODES = ["jime", "balcon", "tronco", "e-bob", "nodo-suri"]

TYPE = "wifi_links_info"
UPDATE_INTERVAL = 30       # field value
BLEACH_TTL = 2400          # field value
PROPAGATION_BUDGET = 260   # 4 hops x 30 s, plus slack
SAMPLES = 6
SAMPLE_EVERY = 10
TOLERANCE = 3              # seconds of spread we would call "consistent"

ILL = re.compile(r"is remote peer ill")


def run(mesh):
    # Real nodes boot at different times, so their publish schedules are
    # not aligned. Without this the daemons share one host clock, fire on
    # the same instant, and an entry cascades the whole chain in a single
    # round — which suppresses the very divergence being measured.
    offsets = mesh.stagger_clocks(UPDATE_INTERVAL)
    nodes = mesh.bootstrap(TYPE, update_interval=UPDATE_INTERVAL,
                           bleach_ttl=BLEACH_TTL, full_mesh=False)
    mesh.chain()                      # jime - balcon - tronco - e-bob - suri
    for n in nodes:
        mesh.impair(n, delay_ms=40)

    author = nodes[0]
    key = author.name
    author.publish(TYPE, key, gen=1)

    # wait for the entry to reach the far end of the chain
    deadline = time.time() + PROPAGATION_BUDGET
    reached = {}
    while time.time() < deadline:
        reached = {n.name: (n.probe(TYPE, timeout=15).get(key) is not None)
                   for n in nodes}
        if all(reached.values()):
            break
        time.sleep(5)
    if not all(reached.values()):
        missing = [k for k, v in reached.items() if not v]
        return False, (f"entry never reached {', '.join(missing)} within "
                       f"{PROPAGATION_BUDGET}s across {len(nodes)} hops")

    # sample the TTL every node holds for the same entry
    worst = 0
    worst_row = None
    author_lowest = 0
    for _ in range(SAMPLES):
        row = {}
        for n in nodes:
            entry = n.probe(TYPE, timeout=15).get(key)
            if entry:
                row[n.name] = entry["ttl"]
        if len(row) == len(nodes):
            spread = max(row.values()) - min(row.values())
            if spread > worst:
                worst, worst_row = spread, dict(row)
            if row[author.name] == min(row.values()) and spread > 0:
                author_lowest += 1
        time.sleep(SAMPLE_EVERY)

    ill = sum(len(ILL.findall(n.read_log())) for n in nodes)

    if worst <= TOLERANCE and ill == 0:
        return True, (f"max TTL spread {worst}s across {len(nodes)} nodes "
                      f"(clock offsets {offsets}), no 'is remote peer ill?' "
                      f"warnings")

    detail = (f"max TTL spread **{worst}s** across {len(nodes)} nodes "
              f"(tolerance {TOLERANCE}s): {worst_row}")
    if author_lowest:
        detail += (f"; the author held the LOWEST TTL for its own key in "
                   f"{author_lowest}/{SAMPLES} samples — every neighbour's "
                   f"copy outranks the author's")
    if ill:
        detail += (f"; {ill} 'is remote peer ill?' warning(s) — the author "
                   f"rejecting its own entry coming back inflated, the exact "
                   f"line from the MonteNet report")
    return False, detail

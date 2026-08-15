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

What drives the spread — and what does not
    An earlier version of this docstring explained divergence as "late
    first receipt": a distant node hearing about an entry one interval
    late and so holding a TTL one interval higher, giving a spread of
    (hops x interval). **That explanation is withdrawn.** A sender
    serializes its current, already-decayed TTL, so a receiver inherits
    the decay rather than restarting it.

    The mechanism, since measured (audit C7): **a TTL in flight does not
    decay**. The sender serializes a frozen value, and
    `sharedstate.cc:896` adopts it whenever `sliceEntry.mTtl >=
    knownEntry.mTtl`, so every sync round injects up to one
    transfer-duration of artificial freshness — *every round*, which is
    why divergence accumulates instead of settling. The author alone
    cannot gain it (`sharedstate.cc:879-888` rejects own-authored
    entries arriving higher, logging the warning counted below), so it
    becomes the structural minimum.

    **Spread is therefore a rate, not a value**, and the number this
    test reports is `rate x how long it sampled`. Do not compare it
    against a spread measured over a different window, including the
    field's 22-27 s, whose observation duration is unknown. Measured on
    a fixed window (`experiments/divergence_dynamics.py`): 34 s per
    100 s at 512 kbit/40 ms, 56 at 400 ms, 71 at 128 kbit, each
    reproduced by an endpoint-only control. An earlier version of this
    docstring cited 112 s vs 3 s from the sweep as the magnitude of the
    effect; **both figures are windowed maxima over unrecorded windows
    and that comparison is withdrawn.**

    This test uses the field's *shape* — five nodes in a line, a 30 s
    interval, a 2400 s TTL — on a fast bridge where transfers take
    milliseconds, so it captures the qualitative signature at a small
    magnitude. The verdict does not depend on the window: the tolerance
    below is 3 s, divergence accumulates from the first round, and any
    sampling window long enough to observe the mesh at all exceeds it.
    Magnitude belongs to `divergence_dynamics.py`, which reports a slope.

Method
    One author publishes once. Wait for the entry to reach all five
    nodes, then sample every node's TTL for it repeatedly and report the
    spread. Also count "is remote peer ill?" warnings, which is the
    author actively rejecting its own entry coming back inflated — the
    exact log line from the field report.

Expected on today's code: RED.

What would turn it green — and what would not. This test asserts a bound
on the **drift itself**, so the version-counter merge does *not* satisfy
it: versioning removes the drift's *consequence* (ordering stops
depending on TTL) while the drift remains. Expect this test to stay red
after the merge fix, and that is correct rather than a regression. What
would actually bound the drift is reducing transfer duration — delta or
digest sync instead of full-state exchange — or making expiry
timestamp-based rather than decrement-based.

The consequence is covered separately and deterministically by T1.
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
# NOTE: observed lab spread is 3-5 s, so this threshold sits inside
# run-to-run noise and the GREEN/RED verdict is not a reliable
# discriminator between branches on a fast bridge. The stable signals in
# this test are the qualitative ones (author holding the lowest TTL, and
# the "is remote peer ill?" count); magnitude belongs to the sweep, where
# transfer duration is varied deliberately.

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

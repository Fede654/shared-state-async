"""T5 — concurrent peers must not be served strictly one after another.

Strict condition
    K peers syncing at once all finish within a small multiple of the
    time a single sync takes. Serving them strictly serially is a
    failure: on a mesh every neighbour wakes on the same interval, so
    they arrive together by design.

Why it matters
    The accept loop awaits each connection to completion before
    accepting the next (audit B1), and the listen backlog is 8. As a
    neighbourhood grows, sync latency grows with it and the far end of
    the queue gets connection refusals — matching the "Connection
    refused" storms in lime-packages#1150.

Method
    Measure one session (S) on an impaired link, then run K sessions
    concurrently and require them all inside K_DEADLINE x S. Serial
    handling needs ~K x S, so the threshold separates the two cleanly
    without depending on absolute machine speed.

Expected on today's code: RED.

Fix that turns it green: one task per accepted connection.
"""

import threading
import time

ID = "T5"
TITLE = "concurrent peers are not served serially"
EXPECT_TODAY = "RED"

TYPE = "probe"
K = 12                 # concurrent peers
LINK_DELAY_MS = 60     # makes per-session cost dominate scheduling noise
K_DEADLINE = 4         # allowed: 4 x single-session time (serial needs ~12x)


def run(mesh):
    node = mesh.node("lime-a")
    mesh.bootstrap(TYPE, nodes=[node], full_mesh=False)
    node.set_peers([])
    mesh.impair(node, delay_ms=LINK_DELAY_MS)

    node.probe(TYPE, timeout=15)                    # warm up
    t0 = time.time()
    node.probe(TYPE, timeout=15)
    single = time.time() - t0
    if single <= 0:
        single = 0.05
    deadline = max(K_DEADLINE * single, 2.0)

    results = [None] * K
    barrier = threading.Barrier(K)

    def worker(i):
        try:
            barrier.wait(timeout=30)
            t = time.time()
            node.probe(TYPE, timeout=int(deadline) + 30)
            results[i] = time.time() - t
        except Exception as e:
            results[i] = e

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(K)]
    start = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=deadline + 60)
    wall = time.time() - start

    failed = [r for r in results if isinstance(r, Exception)]
    if failed:
        return False, (f"{len(failed)}/{K} peers errored "
                       f"(first: {type(failed[0]).__name__}); "
                       f"single={single:.2f}s")
    if wall > deadline:
        return False, (f"{K} concurrent peers took {wall:.1f}s; "
                       f"single session {single:.2f}s, deadline "
                       f"{deadline:.1f}s -> served serially "
                       f"({wall / single:.1f}x single)")
    return True, (f"{K} peers in {wall:.1f}s "
                  f"({wall / single:.1f}x a single {single:.2f}s session)")

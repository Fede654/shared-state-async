"""T1 — a node must never replace its own fresh data with a stale echo.

Strict condition
    An author holding generation N for its own key, offered generation
    N-1 by a neighbour, still holds N afterwards.

Why it matters
    This is the corruption that audit C1/C6 describes: TTL is the only
    freshness signal, so when a returning echo's TTL happens to equal the
    author's, the author accepts its own outdated payload and then
    republishes it as current. On a live mesh the echo is not crafted —
    it is what every neighbour sends back, and TTLs coincide constantly
    because they are integer seconds decremented once a second.

Method
    Deterministic, no timing races: publish gen 2 through the normal CLI,
    read back the author's exact TTL with a passive probe, then speak the
    protocol as a neighbour offering gen 1 at that same TTL. Nothing here
    is outside the spec — it is one ordinary sync session.

Expected on today's code: RED. `merge()` compares `slice.mTtl >=
known.mTtl`, and the guard meant to prevent exactly this (`minUpdateTtl`,
commit db58e3d) was computed and never used.

Fix that turns it green: the version-counter merge, where an older
version loses regardless of TTL.
"""

ID = "T1"
TITLE = "own fresh data survives a stale echo at equal TTL"
EXPECT_TODAY = "RED"

TYPE = "probe"


def run(mesh):
    node = mesh.node("lime-a")
    mesh.bootstrap(TYPE, nodes=[node], full_mesh=False)
    node.set_peers([])
    key = node.name

    node.publish(TYPE, key, gen=1)
    node.publish(TYPE, key, gen=2)

    state = node.probe(TYPE)
    entry = state.get(key)
    if not entry:
        return False, "author does not hold its own key after publishing"
    if node.gen_of(state, key) != 2:
        return False, f"expected gen 2 before the echo, got {entry}"

    # a neighbour echoes the previous generation back, same TTL
    stale = {key: {"author": entry["author"], "ttl": entry["ttl"],
                   "data": {"gen": 1, "src": node.name},
                   "version": max(0, entry.get("version", 0) - 1)}}
    node.inject(TYPE, stale)

    after = node.probe(TYPE)
    gen = node.gen_of(after, key)
    if gen == 2:
        return True, "held gen 2 against the stale echo"
    return False, (f"regressed to gen {gen} after a gen-1 echo at "
                   f"ttl={entry['ttl']} (author's own data overwritten)")

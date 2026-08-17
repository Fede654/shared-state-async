"""T24 — an expired entry must not be resurrected by an echo of itself.

Strict condition
    Once an entry has expired at its author, a remote copy carrying that
    author's name does not silently bring it back. The test uses an echo
    TTL *below* the author's own insert, so it turns on the resurrection
    itself, not on any inflation.

Why it matters
    This is the claim that closed the divergence analysis, and it was
    **wrong**. `tests/mesh/README.md` and `CLAUDE.md` both argued that
    TTL divergence is self-limiting because "nothing refreshes the
    author's copy", so the author's entry bleaches away at ~40.5 min and
    "the five-node quantity being measured ceases to exist". External
    review found the hole in the code, not in the numbers.

    `sharedstate.cc:866-873`: a key that is MISSING from local state is
    `emplace`d immediately and the loop `continue`s — before
    `ownAuthorship` is computed at :875, and therefore before the
    "is remote peer ill?" guard at :882 that discards own-authored
    entries arriving with a higher TTL. The author's protection is
    unreachable for a key the author no longer holds.

    So expiry is not terminal. The author expires its copy while a
    neighbour still holds one — neighbours run *ahead* of the author, by
    construction (`experiments/divergence_dynamics.py`) — and the next
    sync round reinserts it under the author's name, at whatever TTL the
    neighbour had, without the author having published anything. The
    spec oracle reproduces exactly this: `bleach` removes the key, then
    v1 `merge` reports `insert` and restores the remote TTL.

    Same root cause as T11: both are the missing-key path skipping the
    reasoning that applies to a key you already have.

The tension this exposes — worth stating, because no fix is free
    T11 requires that a rebooted node RELEARN its own entries from
    neighbours, since state lives in `/tmp` and every reboot starts
    blank. T24 requires that it NOT relearn an entry it deliberately let
    expire. Both arrive as "a remote entry authored by me, which I do
    not currently hold", and TTL cannot tell them apart. Distinguishing
    them needs state the daemon does not keep — a tombstone for expired
    keys, or a persisted author epoch — so this is a design gap, not a
    line to patch.

Method
    Deterministic and short: register the type with a small bleach TTL,
    publish once, wait for the author's own copy to expire locally, then
    offer it back as a lagging neighbour would — same key, same author,
    and a TTL BELOW the author's own insert, so the echo is one a
    neighbour could genuinely hold. No 40-minute wait, and no dependence
    on the divergence rate.

Expected on today's code: RED — the entry returns, carrying the TTL the
echo supplied.

What would turn it green. Not the version-counter merge on its own:
`merge_with_version` changes how *conflicts* are ordered, while this is
the no-conflict path — the key is absent, so the insert happens before
any ordering rule is consulted. Measured, not inferred: this test is
RED on `merge_with_version` at `22a20aab` too (2026-08-17, the echo
carrying a version field so deserialization cannot mask the result).
It needs the missing-key path to consult authorship at all.

Consequence for the divergence work: the ~830 s spread at first expiry
is an extrapolation to a moment that is not an endpoint.

Whether it recurs without injection is a separate question, and this
test cannot answer it: it injects the echo. `experiments/post_expiry.py`
measured that — peer-generated, nothing injected; NOT "an ordinary
unobserved mesh", since the observer load is a factor — and **did
reproduce it — in 3 of 3 gated
runs, at least twice each** (author TTL increases force it even where
the absence fell between samples; direct absence-then-return was also
sampled, once at TTL 34 s, the author having published nothing). Every
run ended with the entry absent and nothing was sampled present after
about two lifetimes — an endpoint observation, not a proof of
self-limitation, since returns shorter than the ~8 s sampling gap are
invisible. So this test carries the *mechanism* — the guard is
unreachable, which is the part a fix has to address — and the
experiment carries the dynamics, which remain open beyond a short-TTL
model.
"""

import time

ID = "T24"
TITLE = "an expired entry is not resurrected by a remote echo of itself"
EXPECT_TODAY = "RED"

TYPE = "probe"
UPDATE_INTERVAL = 5
BLEACH_TTL = 8            # short on purpose: expiry in seconds, not minutes
# BELOW the author's own insert TTL (8 + 5 + 1 = 14 s), deliberately.
# The first version used 60 s, which no neighbour's protocol state could
# hold: protocol operations only copy or decrement a TTL, so nothing
# downstream of a 14 s insert ever exceeds 14 s. That made the test an
# adversarial injection dressed up as "exactly what a lagging neighbour
# would send", and the inflated number then appeared in the write-up as
# though it were natural. A value under the insert TTL is what a
# neighbour genuinely holds, and it isolates the defect being tested:
# the missing-key path accepting an own-authored entry at all.
ECHO_TTL = 6
EXPIRY_BUDGET = 90


def run(mesh):
    node = mesh.node("lime-a")
    mesh.bootstrap(TYPE, nodes=[node], full_mesh=False,
                   update_interval=UPDATE_INTERVAL, bleach_ttl=BLEACH_TTL)
    # No peers: the only thing that can reach this node is our injection,
    # so a resurrection cannot be blamed on ordinary mesh traffic.
    node.set_peers([])
    key = node.name

    node.publish(TYPE, key, gen=1)
    first = node.probe(TYPE).get(key)
    if not first:
        return False, "setup: author does not hold its own freshly published key"
    initial_ttl = first["ttl"]

    # Wait for the author's own copy to bleach away.
    deadline = time.time() + EXPIRY_BUDGET
    while time.time() < deadline:
        if node.probe(TYPE).get(key) is None:
            break
        time.sleep(2)
    else:
        return False, (f"setup: author's entry never expired within "
                       f"{EXPIRY_BUDGET}s (initial TTL {initial_ttl}s, "
                       f"bleach_ttl {BLEACH_TTL})")

    # A lagging neighbour offers the author its own key back, at a TTL
    # it could genuinely still be holding (below the author's insert).
    # The echo carries a version field: v1 master tolerates the extra
    # key, and WITHOUT it a version-counter branch would reject the
    # entry at deserialization (the T23 behaviour) before the
    # missing-key path ever ran — this test would then look green for
    # a reason that has nothing to do with expiry being terminal.
    node.inject(TYPE, {key: {"author": node.name, "ttl": ECHO_TTL,
                             "version": 1,
                             "data": {"gen": 1, "src": node.name}}})

    after = node.probe(TYPE).get(key)
    if after is None:
        return True, (f"expiry was terminal: the author's key stayed gone "
                      f"after an echo offered it back at TTL {ECHO_TTL}s")

    ill = "is remote peer ill" in node.read_log()
    return False, (
        f"RESURRECTED: the author's own key came back at TTL "
        f"**{after['ttl']}s** from an echo offering {ECHO_TTL}s — below "
        f"the author's own insert TTL of {initial_ttl}s, so this is a "
        f"copy a neighbour could genuinely be holding, not an inflated "
        f"one — without the author publishing anything. The missing-key "
        f"path at sharedstate.cc:866 inserts before the ownAuthorship "
        f"guard at :882 can see it"
        + (", and no 'is remote peer ill?' warning was logged — the guard "
           "never ran" if not ill else "") +
        f". Expiry is therefore not a bound on TTL divergence.")

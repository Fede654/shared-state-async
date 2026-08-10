"""Property tests for the merge model — pure logic, no simulator.

Each property states an expectation PER STRATEGY. For v1/v1i some
properties are *expected to fail*: those are characterization tests —
they PASS when the defect reproduces, because the suite's job is to pin
the behavior down, not to pretend it is correct.
"""

import random

import model
from model import Entry


def _rand_entry(rng, authors, ttl_pool=None, ver_pool=None):
    a = rng.choice(authors)
    return Entry(author=a,
                 ttl=ttl_pool.pop() if ttl_pool else rng.randrange(1, 500),
                 data=(rng.randrange(1, 50), f"blob{rng.randrange(1000)}"),
                 version=ver_pool.pop() if ver_pool else rng.randrange(0, 20))


def _rand_state(rng, keys, authors, **kw):
    return {k: _rand_entry(rng, authors, **kw) for k in keys
            if rng.random() < 0.8}


def _pools(rng, n):
    """Globally unique ttl/version values -> no exact ties anywhere."""
    return (list(rng.sample(range(1, 10_000), n)),
            list(rng.sample(range(1, 10_000), n)))


def prop_idempotent(strategy, seed):
    """merge(L, S); merge(L, S) == merge(L, S)  — for every strategy."""
    rng = random.Random(seed)
    keys = [f"k{i}" for i in range(6)]
    authors = ["n1", "n2", "n3"]
    L = _rand_state(rng, keys, authors)
    S = _rand_state(rng, keys, authors)
    L1 = {k: e.clone() for k, e in L.items()}
    model.merge(L1, S, remote=True, hostname="n1", strategy=strategy)
    L2 = {k: e.clone() for k, e in L1.items()}
    model.merge(L2, S, remote=True, hostname="n1", strategy=strategy)
    return _same(L1, L2), "second identical merge changed state"


def prop_commutative_third_party(strategy, seed):
    """Merge order of two slices does not matter when (a) no exact
    ttl/version ties exist and (b) no entry is authored by the merging
    node itself. Own-authored echoes ARE order-dependent in every
    strategy (v1 via discard_ill asymmetry, v2 via recovery leapfrog) —
    that is characterized by prop_own_echo_order_dependent."""
    rng = random.Random(seed)
    keys = [f"k{i}" for i in range(6)]
    authors = ["n2", "n3", "n4"]          # merging host n1 authors nothing
    tp, vp = _pools(rng, 40)
    base = _rand_state(rng, keys, authors, ttl_pool=tp, ver_pool=vp)
    A = _rand_state(rng, keys, authors, ttl_pool=tp, ver_pool=vp)
    B = _rand_state(rng, keys, authors, ttl_pool=tp, ver_pool=vp)
    L1 = {k: e.clone() for k, e in base.items()}
    model.merge(L1, A, remote=True, hostname="n1", strategy=strategy)
    model.merge(L1, B, remote=True, hostname="n1", strategy=strategy)
    L2 = {k: e.clone() for k, e in base.items()}
    model.merge(L2, B, remote=True, hostname="n1", strategy=strategy)
    model.merge(L2, A, remote=True, hostname="n1", strategy=strategy)
    return _same(L1, L2), "merge order changed final state"


def prop_own_echo_order_dependent(strategy, seed):
    """Characterization: merging two echoes of a node's OWN key is
    order-dependent in every strategy — via the post-boot insert
    bypassing the own-entry guards (v1/v1i), and via recovery-leapfrog
    version bookkeeping (v2/v2r). PASSES when the order-dependence
    reproduces; if it ever fails, the spec note must be revisited.
    Note: harmless in practice — self-heals at the author's next
    publish — but it is why merge is NOT a CRDT join."""
    del seed
    host = "n1"
    if strategy in ("v2", "v2r"):
        # recovery leapfrogs to different version totals per order
        mk = lambda: {"k": Entry(host, 100, "mine", 5, local=True)}
        e1 = {"k": Entry(host, 90, "x", 7)}
        e2 = {"k": Entry(host, 95, "y", 6)}
    else:
        # fresh-boot insert bypasses guards; guards then freeze per order
        mk = lambda: {}
        e1 = {"k": Entry(host, 500, "g1")}
        e2 = {"k": Entry(host, 480, "g2")}
    L1, L2 = mk(), mk()
    model.merge(L1, e1, remote=True, hostname=host, strategy=strategy)
    model.merge(L1, e2, remote=True, hostname=host, strategy=strategy)
    model.merge(L2, e2, remote=True, hostname=host, strategy=strategy)
    model.merge(L2, e1, remote=True, hostname=host, strategy=strategy)
    return (not _same(L1, L2)), "own-echo merges became order-independent"


def prop_version_monotone(strategy, seed):
    """v2 only: a node's stored version for a key never decreases."""
    rng = random.Random(seed)
    keys = [f"k{i}" for i in range(4)]
    authors = ["n1", "n2"]
    L = _rand_state(rng, keys, authors)
    for _ in range(30):
        S = _rand_state(rng, keys, authors)
        before = {k: e.version for k, e in L.items()}
        model.merge(L, S, remote=True, hostname="n1", strategy=strategy)
        for k, v in before.items():
            if k in L and L[k].version < v:
                return False, f"version decreased for {k}"
    return True, ""


def prop_stale_echo_rejected(strategy, seed):
    """The C6 corruption scenario in miniature (spec §6.2 note):
    author publishes g1, peer echoes g1 back at the same TTL after the
    author has already published g2 at that same TTL.
    Expected: v1 CORRUPTS (echo accepted), v1i and v2 resist."""
    del seed
    host = "A"
    L = {}
    model.author_insert(L, hostname=host, key="A", data=(1, "g1"),
                        strategy=strategy, bleach_ttl=300, update_interval=30)
    echo = {k: e.clone() for k, e in L.items()}          # peer's copy of g1
    model.author_insert(L, hostname=host, key="A", data=(2, "g2"),
                        strategy=strategy, bleach_ttl=300, update_interval=30)
    model.merge(L, echo, remote=True, hostname=host, strategy=strategy)
    corrupted = L["A"].data[0] == 1
    return (not corrupted), "stale echo overwrote newer own data"


def prop_echo_resurrection(strategy, seed):
    """SUITE-DISCOVERED v2 defect: a rebooted node that receives an
    OUTDATED echo of its own key first (plain insert, no local origin)
    and a newer echo second will 'recover' the outdated payload to the
    highest version, resurrecting stale data mesh-wide until its next
    publish. Expected: v2 resurrects (False), v2r resists (True)."""
    del seed
    host = "A"
    L = {}  # post-reboot: empty, nothing locally published yet
    old_echo = {"A": Entry(host, 500, (11, "g11"), 11)}
    new_echo = {"A": Entry(host, 480, (12, "g12"), 12)}
    model.merge(L, old_echo, remote=True, hostname=host, strategy=strategy)
    model.merge(L, new_echo, remote=True, hostname=host, strategy=strategy)
    resurrected = L["A"].data[0] == 11
    if resurrected:
        # confirm the damage propagates: peer holding g12/v12 adopts g11
        peer = {"A": Entry(host, 480, (12, "g12"), 12)}
        model.merge(peer, {k: e.clone() for k, e in L.items()},
                    remote=True, hostname="B", strategy=strategy)
        resurrected = peer["A"].data[0] == 11
    return (not resurrected), "outdated echo resurrected mesh-wide"


def prop_reboot_recovery(strategy, seed):
    """v2 family: after losing counters, the author regains supremacy in
    two merge rounds (recovery leapfrog + next publish)."""
    del seed
    host = "A"
    L = {}
    for g in range(1, 6):
        model.author_insert(L, hostname=host, key="A", data=(g, f"g{g}"),
                            strategy=strategy, bleach_ttl=300, update_interval=30)
    echo = {k: e.clone() for k, e in L.items()}          # network's copy: g5, v5
    L = {}                                               # reboot: state lost
    model.author_insert(L, hostname=host, key="A", data=(6, "g6"),
                        strategy=strategy, bleach_ttl=300, update_interval=30)
    # round 1: echo arrives -> recovery must keep g6 and leapfrog version
    model.merge(L, echo, remote=True, hostname=host, strategy=strategy)
    if L["A"].data[0] != 6:
        return False, "reboot: echo overwrote fresh post-reboot data"
    # round 2: author's slice must now win at a peer that still holds g5
    peer = {k: e.clone() for k, e in echo.items()}
    model.merge(peer, {k: e.clone() for k, e in L.items()},
                remote=True, hostname="B", strategy=strategy)
    if peer["A"].data[0] != 6:
        return False, "reboot: peer did not accept recovered entry"
    return True, ""


def prop_tie_gap_documented(strategy, seed):
    """Characterization of the spec §8 known gap: equal version + equal
    TTL + different data is order-dependent (first writer wins).
    This property PASSES when the gap reproduces under v2."""
    del seed
    base1 = {"k": Entry("X", 100, "left", 5)}
    base2 = {"k": Entry("X", 100, "left", 5)}
    other = {"k": Entry("X", 100, "right", 5)}
    model.merge(base1, other, remote=True, hostname="Y", strategy=strategy)
    kept_first = base1["k"].data == "left"
    model.merge(other, base2, remote=True, hostname="Y", strategy=strategy)
    kept_first_2 = other["k"].data == "right"
    return (kept_first and kept_first_2), "tie behavior changed: update spec §8"


def _same(a: dict, b: dict) -> bool:
    if set(a) != set(b):
        return False
    return all(a[k].author == b[k].author and a[k].ttl == b[k].ttl
               and a[k].data == b[k].data and a[k].version == b[k].version
               for k in a)


# (name, fn, {strategy: expected_bool}) — expected=False means the defect
# is EXPECTED to reproduce (characterization); the suite fails if a
# defect we rely on explaining suddenly stops reproducing.
PROPERTIES = [
    ("idempotent", prop_idempotent,
     {"v1": True, "v1i": True, "v2": True, "v2r": True}),
    ("commutative_third_party", prop_commutative_third_party,
     {"v1": True, "v1i": True, "v2": True, "v2r": True}),
    ("own_echo_order_dependent", prop_own_echo_order_dependent,
     {"v1": True, "v1i": True, "v2": True, "v2r": True}),
    ("version_monotone", prop_version_monotone,
     {"v2": True, "v2r": True}),
    ("stale_echo_rejected", prop_stale_echo_rejected,
     {"v1": False, "v1i": True, "v2": True, "v2r": True}),
    ("echo_resurrection_resisted", prop_echo_resurrection,
     {"v2": False, "v2r": True}),
    ("reboot_recovery", prop_reboot_recovery,
     {"v2": True, "v2r": True}),
    ("tie_gap_documented", prop_tie_gap_documented,
     {"v2": True, "v2r": True}),
]

N_SEEDS = 200


def run_properties():
    results = []
    for name, fn, expectations in PROPERTIES:
        for strategy, expected in expectations.items():
            failures = []
            for seed in range(N_SEEDS):
                ok, why = fn(strategy, seed)
                if ok != expected:
                    failures.append((seed, why))
                    if len(failures) >= 3:
                        break
            results.append({
                "property": name, "strategy": strategy,
                "expected": expected, "ok": not failures,
                "failures": failures,
            })
    return results

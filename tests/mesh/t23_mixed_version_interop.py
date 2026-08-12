"""T23 — a node must accept entries from peers that predate its features.

Strict condition
    An entry serialized by a node without the version-counter feature —
    i.e. every node in the field today, which emits no `mVersion` — is
    accepted by whatever binary is under test.

Why it matters
    Any rollout is a mixed fleet for as long as it takes to flash every
    router, which on a community mesh is weeks to never. If new nodes
    silently discard everything old nodes say, the upgrade does not
    degrade gracefully: it partitions the network along firmware
    versions, and it does so invisibly, because a dropped entry looks
    exactly like a neighbour with nothing to say.

    This is the specific hazard the spec previously got *wrong*. Spec §8
    asserted that entries from non-versioned nodes "read as
    `mVersion = 0`" and that a mixed fleet "degrades to roughly the old
    behaviour". Measured, that is false on `merge_with_version`: the
    entry is refused outright, presumably because `RS_SERIAL_PROCESS`
    fails on the missing member and takes the whole slice with it.

Method
    Offer the node two entries in one session — one in v1 shape (no
    `mVersion`, exactly what a deployed node sends) and one carrying a
    version — then read back which survived.

Expected today: GREEN on master, **RED on `merge_with_version`**, where
a slice containing one unversioned entry loses *every* entry in it — the
failure is at slice granularity, not per entry. This is the rare test
whose purpose is to fail on the *fix* branch rather than on upstream,
which is why it is worth having before that branch ships.

Fix that turns it green: treat a missing `mVersion` as 0 on
deserialization rather than as a malformed entry.
"""

ID = "T23"
TITLE = "entries from non-versioned peers are accepted"
EXPECT_TODAY = "GREEN"

TYPE = "probe"


def run(mesh):
    node = mesh.node("lime-a")
    mesh.bootstrap(TYPE, nodes=[node], full_mesh=False)
    node.set_peers([])

    node.inject(TYPE, {
        # what every currently deployed node puts on the wire
        "from-v1-peer": {"author": "old-node", "ttl": 250,
                         "data": {"gen": 1, "src": "v1"}},
        # what a version-counter node sends
        "from-v2-peer": {"author": "new-node", "ttl": 250,
                         "data": {"gen": 1, "src": "v2"}, "version": 3},
    })

    state = node.probe(TYPE)
    v1_ok = "from-v1-peer" in state
    v2_ok = "from-v2-peer" in state

    if v1_ok and v2_ok:
        return True, "accepted entries from both versioned and unversioned peers"
    if not v1_ok and v2_ok:
        return False, ("entries WITHOUT mVersion were silently dropped while "
                       "versioned entries were accepted — a mixed fleet "
                       "partitions along firmware versions, and a discarded "
                       "entry is indistinguishable from a quiet neighbour")
    if v1_ok and not v2_ok:
        return False, "versioned entries were dropped; unversioned accepted"
    return False, ("BOTH entries were dropped from a slice containing one "
                   "unversioned entry — deserialization fails on the missing "
                   "member and takes the entire slice with it, not just the "
                   "offending entry. In a mixed fleet a deployed node sends "
                   "its whole state unversioned, so an upgraded node discards "
                   "all of it, silently.")

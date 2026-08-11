"""T20 — an unauthenticated stranger must not be able to inject state.

Strict condition
    A host that is not a configured peer cannot write entries into a
    node's shared state, and cannot author entries under another node's
    identity.

Why it matters
    There is no authentication, no integrity check and no rate limit on
    TCP 3490 (critique 1.3). Whatever reaches that port is merged and
    gossiped mesh-wide, and hook scripts consuming that data run as root
    — so injected content flows into hosts files, bat-hosts and DNS on
    every node. The entries this test writes claim to be authored by a
    *different* node, which the protocol has no way to dispute.

Status of this test
    This documents the security posture; it is not a claim that the
    posture is wrong. A community mesh may legitimately decide to trust
    its L2 domain. What must not happen is the decision being made by
    omission — so the condition is asserted and expected to fail, and
    changing that is a project decision (see spec §9), not a bug fix.

Expected on today's code: RED.
"""

ID = "T20"
TITLE = "unauthenticated peers cannot inject state"
EXPECT_TODAY = "RED"

TYPE = "probe"


def run(mesh):
    node = mesh.node("lime-a")
    mesh.bootstrap(TYPE, nodes=[node], full_mesh=False)
    node.set_peers([])                 # this node has no configured peers

    victim = "lime-b"                  # identity we are impersonating
    node.inject(TYPE, {
        "injected-hostname": {
            "author": victim,          # authored by someone else entirely
            "ttl": 290,
            "data": {"gen": 1, "hostname": "attacker",
                     "note": "written by a host that is not a peer"},
        }})

    state = node.probe(TYPE)
    entry = state.get("injected-hostname")
    if entry is None:
        return True, "stranger's entry was refused"
    return False, (f"stranger wrote state into the node and it is now "
                   f"served to the mesh: author={entry['author']!r} "
                   f"(impersonating {victim}), ttl={entry['ttl']}, "
                   f"data={entry['data']} — no auth, and hooks consuming "
                   f"this run as root")

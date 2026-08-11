"""T10 — one bad line from the discover script must not void the peer list.

Strict condition
    When `shared-state-async-discover` emits an unparseable line
    alongside valid peer addresses, the valid peers are still used.

Why it matters
    `getCandidatesNeighbours` aborts the *entire* peer list on the first
    address it cannot parse. The script is a separate LibreMesh shell
    script whose output is trivially polluted — a warning on stdout, a
    blank line, an interface that momentarily has no address — and the
    result is a node that syncs with nobody that round, silently.
    Discovery reliability rests on a shell script never having a bad day.

Method
    Give the node a discover script that prints a blank line, a junk
    line, and a real peer's address. The node should still sync with the
    real peer.

Expected on today's code: RED.

Fix that turns it green: skip unparseable lines (and check the script's
exit status — see T17).
"""

import os
import time

ID = "T10"
TITLE = "discovery survives a malformed line"
EXPECT_TODAY = "RED"

TYPE = "probe"


def run(mesh):
    a, b = mesh.node("lime-a"), mesh.node("lime-b")
    mesh.bootstrap(TYPE, nodes=[a, b], full_mesh=False)

    # b knows a cleanly; a's discovery output is polluted but contains b
    b.set_peers([a])
    path = os.path.join(a.bindir, "shared-state-async-discover")
    with open(path, "w") as f:
        f.write("#!/bin/sh\n"
                "echo ''\n"                       # blank line
                "echo 'not-an-address'\n"         # junk
                f"echo {b.ip}\n")                 # the real peer
    os.chmod(path, 0o755)

    a.publish(TYPE, "from-a", gen=1)

    # a must reach b. b never initiates toward a for this key, so the
    # only path is a's own discovery working despite the bad lines.
    b.set_peers([])
    deadline = time.time() + 60
    while time.time() < deadline:
        if b.probe(TYPE).get("from-a") is not None:
            return True, "valid peer used despite malformed discovery output"
        time.sleep(2)

    out = a.cli(f"discover", timeout=30)
    return False, (f"peer never contacted: one bad line voided the whole "
                   f"list (CLI discover printed "
                   f"{len([l for l in out.stdout.splitlines() if l.strip()])} "
                   f"addresses, exit={out.returncode})")

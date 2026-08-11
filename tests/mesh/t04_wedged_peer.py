"""T4 — one silent peer must not take the node down.

Strict condition
    A peer that connects, completes the handshake, and then goes silent
    must not prevent other peers from syncing.

Why it matters
    This is the normal failure mode of a flaky radio link: no FIN, no
    RST, just silence. No TCP keepalive is configured and no network
    operation has a deadline, so the daemon's handler for that peer waits
    forever — and because the accept loop awaits each connection to
    completion before accepting the next (audit B1/B2), the whole node
    stops serving. One bad link disables a router until it is rebooted.

Expected on today's code: RED.

Fix that turns it green: handle each connection in its own task, and
give every network operation a timeout.
"""

import socket
import struct
import time

import wire

ID = "T4"
TITLE = "a silent peer does not block the daemon"
EXPECT_TODAY = "RED"

TYPE = "probe"
SERVE_DEADLINE = 20      # generous: a healthy node answers in << 1s


def run(mesh):
    node = mesh.node("lime-a")
    mesh.bootstrap(TYPE, nodes=[node], full_mesh=False)
    node.set_peers([])

    # baseline: the node serves a normal peer promptly
    t0 = time.time()
    try:
        node.probe(TYPE, timeout=10)
    except Exception as e:
        return False, f"baseline probe failed before any wedging: {e!r}"
    baseline = time.time() - t0

    # a peer that handshakes then falls silent, holding the connection open
    ver = struct.pack("!I", wire.WIRE_PROTO_VERSION)
    wedge = socket.create_connection((node.ip, wire.TCP_PORT), timeout=10)
    try:
        wedge.sendall(ver)
        if wedge.recv(4) != ver:
            return False, "wedging peer got no handshake reply"
        wedge.sendall(ver)
        # ...and now says nothing at all, forever.
        time.sleep(1)

        t0 = time.time()
        try:
            node.probe(TYPE, timeout=SERVE_DEADLINE)
        except Exception as e:
            return False, (f"node stopped serving while one peer was silent "
                           f"(baseline {baseline:.2f}s, then {type(e).__name__} "
                           f"after {time.time() - t0:.0f}s)")
        served = time.time() - t0
        return True, f"still served in {served:.2f}s (baseline {baseline:.2f}s)"
    finally:
        wedge.close()

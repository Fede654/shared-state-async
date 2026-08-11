"""T18 — a peer that disappears mid-message must be rejected, not believed.

Strict condition
    When a peer disconnects part-way through a frame, the node treats
    the message as failed. It must not act on a partially received
    header or payload.

Why it matters
    `AsyncSocket::recv` loops until the requested length or until the
    peer returns 0, and then returns a *short count*.
    `receiveNetworkMessage` adds that count to its running total without
    checking it equals what it asked for (audit D2), so a peer that
    vanishes mid-header leaves partially-filled length fields that are
    then used as if they were real. On a wireless mesh, connections
    dying mid-transfer is ordinary.

Method
    Cut the connection at three points — inside the type name, inside
    the 4-byte length field, and inside the payload — then confirm the
    node's state is unchanged and it still serves.

Expected on today's code: GREEN for state integrity (the JSON parse and
the length bounds catch most of this downstream) — the value of the test
is proving *which* of the three points are actually safe rather than
assuming, since the check that should catch them is missing.
"""

import socket
import struct
import time

import wire

ID = "T18"
TITLE = "truncated transfers do not corrupt state"
EXPECT_TODAY = "GREEN"

TYPE = "probe"
VER = struct.pack("!I", wire.WIRE_PROTO_VERSION)


def _cut(ip, payload):
    """Send a partial frame then hang up abruptly."""
    s = socket.create_connection((ip, wire.TCP_PORT), timeout=8)
    try:
        s.sendall(VER)
        s.recv(4)
        s.sendall(VER)
        s.sendall(payload)
        # abortive close: RST rather than a graceful FIN, like a link drop
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                     struct.pack("ii", 1, 0))
    except Exception:
        pass
    finally:
        s.close()


def run(mesh):
    node = mesh.node("lime-a")
    mesh.bootstrap(TYPE, nodes=[node], full_mesh=False)
    node.set_peers([])
    node.publish(TYPE, "canary", gen=7)

    before = node.probe(TYPE)
    payload = b'{"stateSlice":[{"key":"evil","value":{"mAuthor":"x",' \
              b'"mTtl":{"xint64":200,"xstr64":"200"},"mData":{"gen":1}}}]}'
    full = bytes([len(TYPE)]) + TYPE.encode() + struct.pack("!I", len(payload))

    cuts = {
        "mid-type-name": bytes([len(TYPE)]) + TYPE.encode()[:3],
        "mid-length-field": bytes([len(TYPE)]) + TYPE.encode()
        + struct.pack("!I", len(payload))[:2],
        "mid-payload": full + payload[:len(payload) // 2],
    }

    damaged = []
    for name, frag in cuts.items():
        _cut(node.ip, frag)
        time.sleep(0.5)
        try:
            after = node.probe(TYPE, timeout=10)
        except Exception as e:
            damaged.append(f"{name}: node stopped serving "
                           f"({type(e).__name__})")
            continue
        if "evil" in after:
            damaged.append(f"{name}: partial message was accepted as state")
        elif node.gen_of(after, "canary") != node.gen_of(before, "canary"):
            damaged.append(f"{name}: existing entry changed")

    if damaged:
        return False, "; ".join(damaged)
    return True, ("state intact and node serving after truncation at all "
                  "three points (type name, length field, payload)")

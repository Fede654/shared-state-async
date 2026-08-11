"""T21 — malformed frames must be rejected without taking the node down.

Strict condition
    For every malformed message a peer can send — bad type-name length,
    bad data length, truncated payload, wrong protocol version, a frame
    that claims far more data than it delivers — the daemon rejects the
    session and continues serving everyone else.

Why it matters
    The protocol has no authentication and the port is reachable by
    anything on the mesh (critique 1.3), so malformed input is not a
    hypothetical: a half-upgraded neighbour, a truncated transfer, or
    anyone at all can produce it. There is currently no negative test
    coverage of the wire format whatsoever.

Method
    Send each malformed case on its own connection, then verify with a
    normal probe that the node still serves. Each case is scored
    separately so a partial failure is visible.

Expected on today's code: RED — the node is expected to survive most
cases but a frame that under-delivers its declared length has no
deadline behind it (audit B2), so the handler waits forever and the
serial accept loop stops serving anyone (audit B1). That is T4's defect
reached through the parser instead of through silence.
"""

import socket
import struct

import wire

ID = "T21"
TITLE = "malformed frames rejected, node survives"
EXPECT_TODAY = "RED"

TYPE = "probe"
VER = struct.pack("!I", wire.WIRE_PROTO_VERSION)


def _send_raw(ip, payload, do_handshake=True, timeout=8):
    """Send bytes and report how the node reacted."""
    try:
        s = socket.create_connection((ip, wire.TCP_PORT), timeout=timeout)
    except Exception as e:
        return f"connect-failed:{type(e).__name__}"
    try:
        if do_handshake:
            s.sendall(VER)
            if s.recv(4) != VER:
                return "no-handshake-reply"
            s.sendall(VER)
        s.sendall(payload)
        try:
            data = s.recv(64)
        except socket.timeout:
            return "no-reply-timeout"
        return "closed" if not data else "replied"
    except Exception as e:
        return f"error:{type(e).__name__}"
    finally:
        s.close()


def run(mesh):
    node = mesh.node("lime-a")
    mesh.bootstrap(TYPE, nodes=[node], full_mesh=False)
    node.set_peers([])
    node.publish(TYPE, "canary", gen=1)

    good = wire.encode_frame(TYPE, b'{"stateSlice":[]}')
    cases = [
        ("zero-name-length", b"\x00" + b"\x00\x00\x00\x02" + b"{}"),
        ("name-length-overruns", bytes([200]) + b"short"),
        ("data-length-zero", bytes([len(TYPE)]) + TYPE.encode()
         + struct.pack("!I", 0)),
        ("data-length-over-max", bytes([len(TYPE)]) + TYPE.encode()
         + struct.pack("!I", 0xFFFFFFFF) + b"{}"),
        ("declares-more-than-sent", bytes([len(TYPE)]) + TYPE.encode()
         + struct.pack("!I", 4096) + b'{"stateSlice":[]}'),
        ("truncated-mid-header", bytes([len(TYPE)]) + TYPE.encode()[:2]),
        ("garbage-payload", wire.encode_frame(TYPE, b"not json at all")),
        ("wrong-version", None),        # handled specially below
    ]

    results = {}
    survived = {}
    for name, payload in cases:
        if name == "wrong-version":
            try:
                s = socket.create_connection((node.ip, wire.TCP_PORT),
                                             timeout=8)
                s.sendall(struct.pack("!I", 99))
                results[name] = "closed" if not s.recv(4) else "replied"
                s.close()
            except Exception as e:
                results[name] = f"error:{type(e).__name__}"
        else:
            results[name] = _send_raw(node.ip, payload)

        # the important half: is the node still serving anyone?
        try:
            state = node.probe(TYPE, timeout=8)
            survived[name] = state.get("canary") is not None
        except Exception:
            survived[name] = False

    wedged = [n for n, ok in survived.items() if not ok]
    if wedged:
        return False, (f"node stopped serving after: {', '.join(wedged)} "
                       f"(reactions: "
                       f"{ {n: results[n] for n in wedged} })")

    # Every case above disconnects after sending, and the resulting FIN is
    # what lets the daemon recover. A truncated transfer on a flaky link
    # does not send FIN — it just stops. Test that separately.
    lingering = socket.create_connection((node.ip, wire.TCP_PORT), timeout=8)
    try:
        lingering.sendall(VER)
        lingering.recv(4)
        lingering.sendall(VER)
        # announce 4096 bytes of payload, deliver 17, then hold the socket
        lingering.sendall(bytes([len(TYPE)]) + TYPE.encode()
                          + struct.pack("!I", 4096) + b'{"stateSlice":[]}')
        try:
            node.probe(TYPE, timeout=15)
        except Exception as e:
            return False, (f"all {len(cases)} disconnecting cases were "
                           f"handled, but a peer that under-delivers its "
                           f"declared length and stays connected stops the "
                           f"node serving anyone ({type(e).__name__}) — a "
                           f"truncated transfer on a flaky link does exactly "
                           f"this, and no read has a deadline")
    finally:
        lingering.close()

    return True, (f"all {len(cases)} malformed cases rejected and the "
                  f"under-delivering lingering peer did not block the node")

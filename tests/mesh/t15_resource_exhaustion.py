"""T15 — running out of file descriptors must not kill the daemon.

Strict condition
    When the node hits its descriptor limit, it fails the affected
    operation and keeps running.

Why it matters
    `ListeningSocket::accept()` never checks the descriptor returned by
    `AcceptOperation` for -1, so a failed accept feeds -1 into
    `registerFD`, whose `fcntl` then fails with no error bubble and
    terminates the process (audit B4). `accept(2)` fails transiently
    with `EMFILE`, `ENFILE` or `ECONNABORTED` on any busy machine, and
    every hook invocation and discovery run also allocates pipes and a
    pidfd. On a 128 MB router running dozens of packages, exhausting
    descriptors is a Tuesday — and here it is fatal to the whole
    shared-state daemon rather than to one connection.

Method
    Start the daemon under a deliberately small descriptor limit, then
    give it ordinary work: peers connecting and hooks firing.

Expected on today's code: GREEN — but read the result message before
believing it. The daemon could not be starved through connections at
all, because the serial accept loop (T4) only ever holds one at a time.
The fatal accept path is therefore largely *unreachable today* and
becomes reachable the moment concurrency is fixed. The defect is real
and unfixed; this test is the guard that catches it the day T5 lands.

Fix to make alongside the concurrency work: check the accepted
descriptor, bubble the error, and drop the connection not the process.
"""

import os
import socket
import time

import wire

ID = "T15"
TITLE = "descriptor exhaustion does not kill the daemon"
EXPECT_TODAY = "GREEN"

TYPE = "probe"
NOFILE = 24          # low enough to reach quickly, high enough to boot
HOLD = 40            # connections held open to consume the daemon's fds


def run(mesh):
    node = mesh.node("lime-a")
    node.clean_state()
    node.seed_config()
    node.set_peers([])
    node.add_hook(TYPE, "noop.sh", "#!/bin/sh\ncat > /dev/null\n")
    node.cli(f"register {TYPE} community 5 300", timeout=30)

    node.start(rlimit_nofile=NOFILE)
    if not node.wait_listening(timeout=15):
        return False, (f"daemon could not even start with {NOFILE} "
                       f"descriptors: {node.read_log()[:200]}")

    node.publish(TYPE, "canary", gen=1)

    # occupy descriptors: connections that complete the handshake and stay
    held = []
    ver = wire.struct.pack("!I", wire.WIRE_PROTO_VERSION)
    for _ in range(HOLD):
        try:
            s = socket.create_connection((node.ip, wire.TCP_PORT), timeout=3)
            s.sendall(ver)
            held.append(s)
        except Exception:
            break
        time.sleep(0.02)

    # connections alone cannot starve it (see the note in the result
    # message), so also drive the paths that allocate: every hook and
    # every discovery run costs two pipes plus a pidfd.
    for i in range(12):
        try:
            node.publish(TYPE, f"churn{i}", gen=1, timeout=20)
        except Exception:
            break
        if node.proc.poll() is not None:
            break

    time.sleep(2)
    alive = node.proc is not None and node.proc.poll() is None
    for s in held:
        try:
            s.close()
        except Exception:
            pass

    if not alive:
        tail = [ln for ln in node.read_log().splitlines() if ln.strip()]
        msg = next((ln for ln in reversed(tail)
                    if "F " in ln or "error" in ln.lower()), tail[-1] if tail
                   else "(no output)")
        return False, (f"daemon exited after {len(held)} connections under a "
                       f"{NOFILE}-descriptor limit — a resource failure took "
                       f"the process down instead of the connection: "
                       f"{msg[:150]}")

    time.sleep(2)
    try:
        state = node.probe(TYPE, timeout=10)
    except Exception as e:
        return False, (f"daemon alive but not serving after descriptor "
                       f"pressure ({type(e).__name__})")
    if state.get("canary") is None:
        return False, "daemon lost state under descriptor pressure"
    return True, (
        f"survived {len(held)} held connections plus hook/subprocess churn "
        f"at a {NOFILE}-descriptor limit. Note: connections cannot starve "
        f"this daemon, because the serial accept loop (T4) only ever holds "
        f"ONE at a time — the rest queue in the backlog. The fatal accept "
        f"path (audit B4) is therefore hard to reach today and becomes "
        f"reachable the moment concurrency (T5) is fixed. Fix accept error "
        f"handling in the same change.")

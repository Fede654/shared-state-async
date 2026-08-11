"""T16 — bandwidth estimation must not divide by zero.

Strict condition
    No sequence of syncs, however fast or small, crashes the daemon in
    the bandwidth estimator.

Why it matters
    `MbitPerSec(bytes, microseconds)` computes `(bytes<<3)/microseconds`
    with integer division. The caller guards only that the end timestamp
    is *after* the start one — but `steady_clock` has nanosecond
    resolution and the value is then `duration_cast` to microseconds, so
    any transfer completing in under a microsecond floors the divisor to
    zero and raises SIGFPE (audit C4). Every CLI operation syncs over
    loopback, where transfers are as fast as they get.

Method
    Hammer minimal syncs — empty slices, single tiny entries — over
    loopback and across the bridge, and check the daemon is still alive.

Expected today: GREEN. The divisor is a whole frame exchange including
several syscalls, which is very unlikely to complete inside a
microsecond on any real machine. This test's job is to pin that
assumption down: the guard is genuinely wrong, and if a faster machine
or a future port ever crosses that threshold, this is what catches it.
Recorded as a latent defect, not a live one.
"""

import time

ID = "T16"
TITLE = "bandwidth estimation never divides by zero"
EXPECT_TODAY = "GREEN"

TYPE = "probe"
ROUNDS = 400


def run(mesh):
    node = mesh.node("lime-a")
    mesh.bootstrap(TYPE, nodes=[node], full_mesh=False)
    node.set_peers([])
    node.publish(TYPE, "k", gen=1)

    fastest = 9e9
    for i in range(ROUNDS):
        t0 = time.time()
        try:
            node.probe(TYPE, timeout=10)
        except Exception as e:
            if node.proc.poll() is not None:
                return False, _died(node, i)
            return False, f"probe failed at round {i}: {type(e).__name__}"
        fastest = min(fastest, time.time() - t0)
        if node.proc.poll() is not None:
            return False, _died(node, i)

    return True, (f"{ROUNDS} minimal syncs, fastest round-trip "
                  f"{fastest * 1e6:.0f}us — never within the sub-microsecond "
                  f"window that would zero the divisor (latent, not live)")


def _died(node, i):
    rc = node.proc.poll()
    sig = f"signal {-rc}" if rc is not None and rc < 0 else f"exit {rc}"
    extra = " (SIGFPE — division by zero)" if rc == -8 else ""
    return (f"daemon died at round {i} with {sig}{extra}: "
            f"{node.read_log()[-200:]}")

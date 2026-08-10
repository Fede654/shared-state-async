"""T8 — hook children must not inherit the daemon's sockets.

Strict condition
    A hook script executed by the daemon sees only stdin/stdout/stderr.
    It must not inherit the listening socket, the epoll instance, or any
    peer connection.

Why it matters
    A hook that daemonizes (or merely lingers) keeps TCP port 3490 bound
    after the daemon exits, so the daemon cannot restart — on a router
    that is a manual reboot. Inherited peer sockets also mean a hook can
    read or write another node's sync stream.

Expected on today's code: RED. No FD is opened with CLOEXEC except the
timer and the stats file; `fork()`/`execvp()` in AsyncCommand therefore
hands the child everything.

Fix that turns it green: SOCK_CLOEXEC / EPOLL_CLOEXEC / O_CLOEXEC on
every descriptor the daemon owns.
"""

import os
import time

ID = "T8"
TITLE = "hook children do not inherit daemon sockets"
EXPECT_TODAY = "RED"

HOOK = """#!/bin/sh
cat > /dev/null
ls -l /proc/self/fd > {out} 2>&1
"""


def run(mesh):
    node = mesh.node("lime-a")
    node.clean_state()
    node.seed_config()                       # work around T7
    node.set_peers([])
    out = os.path.join(node.dir, "hook_fds.txt")
    if os.path.exists(out):
        os.remove(out)
    node.add_hook("probe", "fdprobe.sh", HOOK.format(out=out))

    node.cli("register probe community 5 300", timeout=30)
    node.start()
    if not node.wait_listening():
        return False, "daemon failed to start: " + node.read_log()[:200]

    node.cli('insert probe', stdin='{"k1":{"v":1}}', timeout=30)

    deadline = time.time() + 15
    while time.time() < deadline and not os.path.exists(out):
        time.sleep(0.3)
    if not os.path.exists(out):
        return False, "hook never ran (no state change?)"

    with open(out) as f:
        listing = f.read()

    leaked = []
    for line in listing.splitlines():
        if " -> " not in line:
            continue
        left, target = line.split(" -> ", 1)
        fd = left.split()[-1]
        if fd in ("0", "1", "2"):
            continue
        if "socket:" in target or "eventpoll" in target:
            leaked.append(f"fd{fd} -> {target}")

    if leaked:
        return False, "hook inherited: " + ", ".join(leaked)
    return True, "hook saw only stdio"

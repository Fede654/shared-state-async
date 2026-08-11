"""T9 — a momentarily unreadable config file must not kill the daemon.

Strict condition
    The daemon survives reading a config file that is briefly invalid,
    and keeps the state it already holds.

Why it matters
    `registerDataType` rewrites `/tmp/shared-state/shared-state-async.conf`
    in place with truncate and no temp-file-plus-rename, while the daemon
    re-reads that same file **twice a second** (the bleach loop and the
    peer loop each call `loadRegisteredTypes`). A reader landing mid-write
    sees a prefix of the new content.

    Following that through the code, the outcome is worse than the state
    wipe the audit predicted. A prefix of the config is almost always
    invalid JSON, and `loadRegisteredTypes` returns on
    `HasParseError()` *before* touching `mTypeConf` — so live state is
    actually safe. But it returns via `rs_error_bubble_or_exit`, and
    `bleachDataLoop` calls `loadRegisteredTypes()` with **no error
    bubble**, so the null `errbub` path terminates the process. A torn
    read does not corrupt the node; it kills it.

Method
    The earlier version of this test raced `register` against the daemon
    60 times and never caught the window — unsurprising, since it is
    microseconds wide against a 1 Hz sampler. So drive the consequence
    directly: write an invalid config, exactly as a reader mid-truncate
    would observe, and see whether the daemon is still alive.

Expected on today's code: RED.

Fix that turns it green: write to a temp file and `rename(2)` so no
reader ever sees a partial file, and never treat a config parse failure
as fatal in a long-running loop.
"""

import time

ID = "T9"
TITLE = "daemon survives a briefly invalid config file"
EXPECT_TODAY = "RED"

TYPE = "probe"


def run(mesh):
    node = mesh.node("lime-a")
    node.clean_state()
    node.seed_config()
    node.set_peers([])
    node.cli(f"register {TYPE} community 5 300", timeout=30)
    node.start()
    if not node.wait_listening():
        return False, "daemon did not start"

    node.publish(TYPE, "keep-me", gen=1)
    if node.probe(TYPE).get("keep-me") is None:
        return False, "setup: entry not stored"

    with open(node.conf_path) as f:
        good = f.read()

    # exactly what a reader sees mid-truncate: a prefix of the new file
    partial = good[:len(good) // 3]
    with open(node.conf_path, "w") as f:
        f.write(partial)

    time.sleep(3)                       # the daemon re-reads twice a second

    with open(node.conf_path, "w") as f:  # put it back regardless
        f.write(good)

    alive = node.proc is not None and node.proc.poll() is None
    if not alive:
        tail = node.read_log().strip().splitlines()
        msg = tail[-1][:160] if tail else "(no log output)"
        return False, (f"daemon exited while the config was briefly "
                       f"unreadable — a torn read from a concurrent "
                       f"`register` kills the node. Last log: {msg}")

    try:
        state = node.probe(TYPE, timeout=10)
    except Exception as e:
        return False, (f"daemon process alive but no longer serving after a "
                       f"partial config read ({type(e).__name__})")
    if state.get("keep-me") is None:
        return False, "daemon survived but dropped state it already held"
    return True, "survived a partial config read with state intact"

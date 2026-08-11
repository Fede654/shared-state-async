"""T6 — expiry must notify hooks.

Strict condition
    When an entry expires, the hooks for its data type run, so
    downstream artifacts stop serving the dead entry.

Why it matters
    Hooks are how shared-state reaches the rest of the system: they
    regenerate hosts files, bat-hosts, DNS entries. `bleachDataLoop`
    discards `bleach()`'s return value and `notifyHooks` only fires on
    merge changes, so a node that leaves the mesh disappears from
    shared-state while its stale entry lives on in every generated file
    until some unrelated change happens to that data type — indefinitely
    on a quiet one.

Method
    Short bleach TTL, one entry, a hook that appends its input to a log.
    Watch the entry expire out of the daemon's state, then check whether
    a hook ran because of it.

Expected on today's code: RED.

Fix that turns it green: treat expiry as a significant change — call
notifyHooks when bleach() removes anything.
"""

import os
import time

ID = "T6"
TITLE = "entry expiry notifies hooks"
EXPECT_TODAY = "RED"

TYPE = "probe"
BLEACH_TTL = 8
HOOK = """#!/bin/sh
cat >> {out}
echo "" >> {out}
"""


def run(mesh):
    node = mesh.node("lime-a")
    node.clean_state()
    node.seed_config()
    node.set_peers([])
    out = os.path.join(node.dir, "hook_calls.txt")
    if os.path.exists(out):
        os.remove(out)
    node.add_hook(TYPE, "record.sh", HOOK.format(out=out))

    node.cli(f"register {TYPE} community 5 {BLEACH_TTL}", timeout=30)
    node.start()
    if not node.wait_listening():
        return False, "daemon did not start"

    node.publish(TYPE, "doomed", gen=1)
    if node.probe(TYPE).get("doomed") is None:
        return False, "setup: entry was never stored"

    calls_before = _calls(out)

    # wait for it to bleach away
    deadline = time.time() + BLEACH_TTL + 40
    expired_at = None
    while time.time() < deadline:
        if node.probe(TYPE).get("doomed") is None:
            expired_at = time.time()
            break
        time.sleep(1)
    if expired_at is None:
        return False, f"entry never expired within {BLEACH_TTL + 40}s"

    time.sleep(5)          # generous window for a notification to happen
    calls_after = _calls(out)

    if calls_after > calls_before:
        return True, (f"hook ran on expiry "
                      f"({calls_before} -> {calls_after} invocations)")
    return False, (f"entry expired but no hook ran ({calls_after} "
                   f"invocations before and after) — downstream files keep "
                   f"serving the dead entry")


def _calls(path):
    if not os.path.exists(path):
        return 0
    with open(path) as f:
        return len([ln for ln in f.read().splitlines() if ln.strip()])

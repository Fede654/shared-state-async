"""T7 — `register` must succeed on a clean system.

Strict condition
    On a node with no prior state (no config file), registering a data
    type succeeds and creates the config file.

Why it matters
    This is the very first command run on a freshly flashed router. If
    it cannot bootstrap itself, every install depends on something else
    having created the file first.

Expected on today's code: RED. `registerDataType()` calls
`loadRegisteredTypes()` first, which on a missing file calls
`rs_error_bubble_or_exit(..., errbub=nullptr)` and terminates the
process — before reaching the code that creates the directory and file.

Fix that turns it green: treat "config file absent" as a normal
first-run condition rather than a fatal error.
"""

import os

ID = "T7"
TITLE = "register bootstraps on a clean system"
EXPECT_TODAY = "RED"


def run(mesh):
    node = mesh.node("lime-a")
    node.clean_state()                      # no config file at all
    assert not os.path.exists(node.conf_path)

    res = node.cli("register probe community 5 300", timeout=30)

    if res.returncode != 0:
        first = (res.stderr or res.stdout or "").strip().splitlines()
        return False, (f"exit={res.returncode}; "
                       f"{first[0][:150] if first else 'no output'}")
    if not os.path.exists(node.conf_path):
        return False, "exit=0 but no config file created"
    return True, "config created on first run"

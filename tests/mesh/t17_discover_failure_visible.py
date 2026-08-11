"""T17 — a broken discover script must not look like an empty mesh.

Strict condition
    When `shared-state-async-discover` fails, that failure is visible —
    a non-zero exit, or at minimum a distinguishable error — rather than
    presenting as "this node has no neighbours".

Why it matters
    `AsyncCommand::waitTermination` discards the child's exit status (an
    in-code TODO). A missing script, a script erroring out after a
    firmware upgrade, or one killed for memory all produce the same
    observable result as a genuinely isolated node: silence and an empty
    peer list. The node then quietly stops syncing with anybody, and
    nothing anywhere says why. On a mesh where nodes routinely *are*
    isolated for real reasons, this failure is invisible by design.

Method
    Three flavours of broken script — non-zero exit with no output,
    output on stderr only, and the script missing entirely — each
    compared against the healthy case.

Expected on today's code: RED.

Fix that turns it green: check the child's exit status and report a
failed discovery distinctly from an empty one.
"""

import os

ID = "T17"
TITLE = "a failing discover script is distinguishable from no peers"
EXPECT_TODAY = "RED"

TYPE = "probe"


def _write(node, body):
    path = os.path.join(node.bindir, "shared-state-async-discover")
    with open(path, "w") as f:
        f.write(body)
    os.chmod(path, 0o755)
    return path


def run(mesh):
    node = mesh.node("lime-a")
    node.clean_state()
    node.seed_config()
    node.cli(f"register {TYPE} community 5 300", timeout=30)

    # healthy baseline: one peer, exit 0
    _write(node, "#!/bin/sh\necho 10.99.0.12\n")
    healthy = node.cli("discover", timeout=20)

    # genuinely empty mesh: no peers, exit 0
    _write(node, "#!/bin/sh\nexit 0\n")
    empty = node.cli("discover", timeout=20)

    broken = {}
    _write(node, "#!/bin/sh\nexit 3\n")
    broken["exit-3"] = node.cli("discover", timeout=20)
    _write(node, "#!/bin/sh\necho 'cannot read interfaces' >&2\nexit 1\n")
    broken["stderr-exit-1"] = node.cli("discover", timeout=20)
    path = os.path.join(node.bindir, "shared-state-async-discover")
    os.remove(path)
    broken["missing"] = node.cli("discover", timeout=20)

    if healthy.returncode != 0:
        return False, f"baseline discover failed: exit {healthy.returncode}"

    empty_sig = (empty.returncode, empty.stdout.strip())
    indistinguishable = [name for name, r in broken.items()
                         if (r.returncode, r.stdout.strip()) == empty_sig]

    if not indistinguishable:
        return True, ("every broken discover script is distinguishable "
                      "from an empty mesh")
    return False, (f"{len(indistinguishable)}/{len(broken)} broken scripts "
                   f"({', '.join(indistinguishable)}) are byte-identical to "
                   f"a healthy node with no neighbours: exit "
                   f"{empty_sig[0]}, empty output — the node stops syncing "
                   f"and nothing reports why")

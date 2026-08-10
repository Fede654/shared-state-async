"""T0 — harness self-check. Not a defect test.

Strict condition
    Three real daemons form a mesh, an entry inserted on one node
    reaches the others, and the near-passive probe can read it.

Why it exists
    Every other test in this suite concludes "the code is broken" from
    an observation. That inference is only worth anything if the
    plumbing underneath is known-good: distinct network namespaces,
    distinct hostnames (hence distinct mAuthor), working discovery
    stubs, and a probe that reads state without perturbing it. When this
    test goes red, no other verdict in the run should be believed.

Expected today: GREEN.
"""

import time

ID = "T0"
TITLE = "harness plumbing: 3 real daemons mesh and are observable"
EXPECT_TODAY = "GREEN"

TYPE = "probe"


def run(mesh):
    nodes = mesh.all_nodes()
    mesh.mesh_all()

    for n in nodes:
        n.clean_state()
        n.seed_config()                      # T7 workaround
        n.cli(f"register {TYPE} community 5 300", timeout=30)
        n.start()
    for n in nodes:
        if not n.wait_listening():
            return False, f"{n.name} never listened: {n.read_log()[:200]}"

    author = nodes[0]
    author.cli(f"insert {TYPE}", stdin='{"smoke":{"gen":1}}', timeout=30)

    # distinct hostnames are load-bearing: without them every node
    # believes it authored everything and merge rules collapse
    authors = set()
    missing = []
    deadline = time.time() + 60
    while time.time() < deadline:
        missing = []
        authors = set()
        for n in nodes:
            try:
                state = n.probe(TYPE)
            except Exception as e:
                missing.append(f"{n.name}:probe-failed({type(e).__name__})")
                continue
            entry = state.get("smoke")
            if not entry:
                missing.append(n.name)
            else:
                authors.add(entry["author"])
        if not missing:
            break
        time.sleep(2)

    if missing:
        return False, "entry never reached: " + ", ".join(missing)
    if authors != {author.name}:
        return False, f"author identity wrong: {authors} (expected {author.name})"
    return True, f"propagated to all {len(nodes)} nodes, author={authors.pop()}"

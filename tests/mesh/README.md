# tests/mesh — real-binary mesh test harness

Runs **actual `shared-state-async` processes** on **actual TCP links**
and asserts on observable behavior. No part of the protocol or the merge
algorithm is reimplemented here, so a failure is a failure of the
shipped code — never of a model of it.

```
cd tests/mesh
python3 run_mesh_tests.py                 # all tests
python3 run_mesh_tests.py T7              # one test
python3 run_mesh_tests.py --bin /path/to/other-build/shared-state-async
```

Requirements: Python 3 (stdlib only), `iproute2`, unprivileged user
namespaces. **No root, no containers, no QEMU.** The runner re-execs
itself inside an unprivileged user/mount/net/uts namespace.

Build the binary under test first:

```
mkdir -p build && cd build
cmake -DCMAKE_BUILD_TYPE=Release -DSS_CPPTRACE_STACKTRACE=OFF .. && make -j
```

## How isolation works

The daemon is hostile to multi-instance testing: it binds
`in6addr_any:3490` without `SO_REUSEPORT`, hardcodes `/tmp/shared-state/`,
and takes its author identity from the system hostname. Each node
therefore gets:

| Namespace | Gives the node |
|---|---|
| net | its own `:3490` and its own IP on the `br0` bridge |
| mount | its own `/tmp/shared-state` (bind-mounted from the run dir, so the harness can inspect it) and a tmpfs `/usr/share` for the hardcoded hooks path |
| uts | its own hostname — **load-bearing**: author identity is `/proc/sys/kernel/hostname`, so without this every node believes it authored everything and every author-dependent merge rule silently evaporates |

Topology is injected through the `shared-state-async-discover` stub on
each node's `PATH`, so any graph is expressible without touching the
binary. `Mesh.impair()` applies `tc netem` delay/loss per link.

## How observation works

`Node.probe()` opens a normal sync session carrying an **empty slice**
(`{"stateSlice":[]}`) using the fixture-verified codec in `wire.py`. The
daemon merges nothing — zero changes, no hooks fire — and returns its
full state. This is deliberately *not* the CLI `dump`/`get`, which sync
with the local daemon as a side effect and perturb the very thing being
measured. Residual perturbation, declared: one connection slot and one
record appended to `network_statistics.json`.

## Reading the results

Every test declares `EXPECT_TODAY`. The runner reports three verdicts:

- **GREEN** — the strict condition holds.
- **RED** — it does not. For most tests that is the *expected* state
  today; each test's docstring names the fix that turns it green.
- **ERROR** — the harness itself broke. Never counted as a verdict, and
  never allowed to "match expectation": a spurious RED that looks
  expected manufactures exactly the false confidence this suite exists
  to prevent. (This is not hypothetical — the first run of T8 reported
  a convincing RED that was really a leaked bridge from the previous
  test.)

Exit 0 means every outcome matched its documented expectation. **When a
fix lands, flip that test's `EXPECT_TODAY` to `GREEN` in the same
commit** — that is the red→green→patch loop this suite is built for.

`T0` is the harness self-check, not a defect test. If T0 is not GREEN,
disbelieve every other verdict in the run.

## Current state (H0–H2)

Both columns are full runs against real builds of the two branches.

| ID | Condition | master | `merge_with_version` |
|---|---|---|---|
| T0 | 3 daemons mesh; entry propagates; authors distinct | GREEN | GREEN |
| T1 | own fresh data survives a stale echo at equal TTL | **RED** | **GREEN** |
| T2 | author updates reach every node, no regressions (chain, 200 ms, 10% loss) | GREEN | GREEN |
| T3 | mesh converges after publishing stops (20% loss) | GREEN | GREEN |
| T4 | a silent peer does not block the daemon | **RED** | **RED** |
| T5 | concurrent peers are not served serially | **RED** | **RED** |
| T7 | `register` bootstraps on a clean system | **RED** | **RED** |
| T8 | hook children inherit no daemon sockets | **RED** | **RED** |
| T11 | rebooted node adopts newest generation, not highest TTL | **RED** | **RED** (different cause) |

### What the runs establish

- **T1 is the corruption from audit C1/C6, reproduced against the
  compiled binary** — the author's own gen-2 payload overwritten by a
  gen-1 echo at equal TTL, deterministically, in about a second. The
  version-counter merge fixes it: T1 is the first test to flip GREEN.
- **T4/T5 are the availability defect, quantified.** One silent peer
  takes the node from answering in 0.09 s to not answering at all; 12
  concurrent peers take 9.1× a single session. Neither branch addresses
  this, and it is the largest field impact.
- **T11 fails on both branches for opposite reasons.** On master the
  rebooted node keeps an older generation because it carries a higher
  TTL. On `merge_with_version` it keeps the older generation *and
  promotes it to a version above the newest one it was offered* — the
  reboot-recovery leapfrog firing for an entry the node never published,
  because it had just adopted that entry from an echo. That is the
  echo-resurrection hazard the spec oracle predicted, now confirmed on a
  real binary; the `v2r` amendment prevents it.

### Limits — what these runs do NOT show

**T2 and T3 pass on both branches**, including on master. The emergent
author-lockout and non-convergence documented at MonteNet did not
reproduce here. Do not read that as "the field reports were wrong" —
read it as the harness not yet recreating the conditions. Known gaps:
only three nodes against five in the field; a 300 s bleach TTL against
2400 s; every generation published through `insert`, which resets TTL to
the maximum and hands the author an advantage it does not have when
entries simply age; and minutes of runtime against hours. The
deterministic tests (T1, T11) show the defect exists in the merge rule;
reproducing it *emergently* is open work.

Also unaddressed here: **an author-lockout test with a topology and
timescale closer to MonteNet** (5-node chain, long TTLs, aging rather
than republishing), and everything in H3–H5.

Planned next (see `doc/mesh-test-harness-PLAN.md`): H3 robustness
(T6 expiry hooks, T9 config atomicity, T10 discovery), H4 mixed-endian
MIPS under `qemu-user`, H5 full OpenWrt image.

## Running against another branch

```
python3 run_mesh_tests.py --bin /path/to/other/build/shared-state-async
```

`EXPECT_TODAY` is calibrated for the default build. Against another
binary a MATCH of `NO` is a **behavioural difference in the binary under
test** — the reason to run it — and the runner says so rather than
failing. Only `ERROR` rows mean the harness itself broke.

## Relationship to `tests/spec-oracle/`

The oracle is the executable form of the protocol spec's merge
semantics — it defines *what should happen*. The strict conditions
asserted here are derived from it. It is not a test of this codebase and
must not be mistaken for one.

## Also here

`wire.py` (byte-exact codec, verified against the real binary) and
`capture.py` (golden-fixture capture) with fixtures under
`fixtures/captured/`. See `doc/protocol-spec-DRAFT.md` §10.

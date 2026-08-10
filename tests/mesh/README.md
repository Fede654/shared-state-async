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

## Current state (H0)

| ID | Condition | Today | Fix that turns it green |
|---|---|---|---|
| T0 | 3 daemons mesh; entry propagates; probe observes; authors distinct | GREEN | — |
| T7 | `register` succeeds on a clean system | RED | don't fatally exit when the config file is absent on first run |
| T8 | hook children inherit no daemon sockets | RED | `CLOEXEC` on epoll, listener, peer sockets, pipes |

Planned next (see `doc/mesh-test-harness-PLAN.md`): H1 availability
(T4 wedged peer, T5 concurrent peers), H2 merge correctness
(T1/T2/T3/T11 against master and against javierbrk's
`merge_with_version`), H3 robustness, H4 mixed-endian MIPS under
`qemu-user`, H5 full OpenWrt image.

## Relationship to `tests/spec-oracle/`

The oracle is the executable form of the protocol spec's merge
semantics — it defines *what should happen*. The strict conditions
asserted here are derived from it. It is not a test of this codebase and
must not be mistaken for one.

## Also here

`wire.py` (byte-exact codec, verified against the real binary) and
`capture.py` (golden-fixture capture) with fixtures under
`fixtures/captured/`. See `doc/protocol-spec-DRAFT.md` §10.

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

## Current state (H0–H3, H6)

22 tests (15 red, 7 green), all against real binaries. `master` is
current upstream code; `mwv` is javierbrk's `merge_with_version`.

| ID | Condition | master | mwv |
|---|---|---|---|
| T0 | harness: 3 daemons mesh, entry propagates, authors distinct | GREEN | GREEN |
| T1 | own fresh data survives a stale echo at equal TTL | **RED** | GREEN |
| T2 | author updates reach every node, no regressions | GREEN | GREEN |
| T3 | mesh converges after publishing stops (20% loss) | GREEN | GREEN |
| T4 | a silent peer does not block the daemon | **RED** | **RED** |
| T5 | concurrent peers are not served serially | **RED** | **RED** |
| T6 | entry expiry notifies hooks | **RED** | — |
| T7 | `register` bootstraps on a clean system | **RED** | **RED** |
| T8 | hook children inherit no daemon sockets | **RED** | **RED** |
| T9 | daemon survives a briefly invalid config file | **RED** | — |
| T10 | discovery survives a malformed line | **RED** | — |
| T11 | rebooted node adopts newest generation, not highest TTL | **RED** | **RED** (other cause) |
| T13 | `discover` never hangs (lime-packages#1198) | GREEN | — |
| T15 | descriptor exhaustion does not kill the daemon | GREEN* | — |
| T16 | bandwidth estimation never divides by zero | GREEN* | — |
| T17 | a failing discover script is distinguishable from no peers | **RED** | — |
| T18 | truncated transfers do not corrupt state | GREEN | — |
| T19 | stats file survives concurrent writers | **RED** | — |
| T20 | unauthenticated peers cannot inject state | **RED** | — |
| T21 | malformed frames rejected, node survives | **RED** | — |
| T14 | unreachable peers do not stop publishing to reachable ones | **RED** (33–44% of cadence) | — |
| T22 | TTL stays consistent across a 5-node chain (MonteNet shape) | **RED**\* | **RED**\* |
| T23 | entries from non-versioned peers are accepted | GREEN | **RED** |

`*` T15/T16 are green today but see below — the defect is real and the
test is a guard, not a clearance. T22's 3 s threshold sits inside
run-to-run noise on a lab bridge (observed 3–5 s on both branches), so
its verdict does not discriminate between them; its stable signals are
the qualitative ones, and magnitude belongs to the sweep.

### The most consequential results

- **T1** — audit C1/C6 reproduced on the compiled binary: an author's
  own gen-2 payload overwritten by a gen-1 echo at equal TTL. The
  version-counter merge fixes it; this is the first test to flip.
- **T4 / T5 / T21** — the availability defect from three directions. One
  silent peer takes the node from answering in 0.09 s to not answering
  at all. Twelve concurrent peers take 9.1× a single session. A peer
  that under-delivers its declared frame length and stays connected
  wedges the node exactly like silence does — which is what a truncated
  transfer on a flaky link looks like.
- **T9** — *worse than the audit predicted*. The audit expected a torn
  config read to wipe state. It does not: a partial file is invalid JSON
  and `loadRegisteredTypes` returns before touching state. But it
  returns through `rs_error_bubble_or_exit`, and `bleachDataLoop` calls
  it with a null error bubble — so **the daemon exits**. Registering a
  data type, which packages do at install time, can kill a running node.
  Deterministic.
- **T11** — fails on both branches for opposite reasons. On master the
  rebooted node keeps an older generation because it carries a higher
  TTL; on `merge_with_version` it keeps the older generation *and
  promotes it above the newest one it was offered*, because the
  reboot-recovery leapfrog fires for an entry the node never published.
  That is the echo-resurrection hazard the spec oracle predicted, now
  confirmed on a real binary. The `v2r` amendment prevents it.
- **T19** — the stats file genuinely tears: unparseable in 8 of ~2400
  reads under concurrent writers, with locking off by default. Its
  timestamps are also confirmed boot-relative, so records are not
  comparable across a reboot or between nodes.
- **T23** — `merge_with_version` **discards a whole slice** if any entry
  in it lacks `mVersion`. Deployed nodes send their entire state
  unversioned, so an upgraded node silently ignores un-upgraded
  neighbours: a mixed fleet partitions along firmware versions with no
  error anywhere. Found only because T20 unexpectedly passed on that
  branch and the reason turned out to be encoding rather than the
  authentication T20 claims to test.
- **T22** — the MonteNet signature, reproduced. Five nodes named after
  the field report, in the same line topology, with the field's 30 s
  update interval and 2400 s TTL: **the author holds the lowest TTL for
  its own key** in most samples, and the daemon logs **"is remote peer
  ill?"** — the exact line from G10h4ck's note, meaning the author is
  actively rejecting its own entry coming back inflated. The mechanism
  is confirmed. The *magnitude* is not: 5 s of spread here against
  22–27 s in the field (see below).
- **T20** — a host that is not a peer wrote state into a node while
  impersonating another node's identity. Documented, not a bug report:
  changing it is a project decision (spec §9).

### Green results that are not clearances

- **T13** — lime-packages#1198 did **not** reproduce in 40 runs on
  GCC 14.2. The report is against GCC 12.2 targeting znver3, and the
  underlying defect (audit A1, missing symmetric transfer) is undefined
  behaviour, so a green run here means "not reproducible on this build",
  never "not a bug". Reproducing it needs the reporter's toolchain.
- **T15** — the daemon could not be starved of descriptors *because*
  the serial accept loop only ever holds one connection at a time. The
  fatal accept path (audit B4) is largely unreachable today and becomes
  reachable the moment concurrency is fixed. Fix accept error handling
  in the same change as T5.
- **T16** — the sub-microsecond window that would zero the divisor was
  never hit; fastest observed round-trip was ~85 ms. Latent, not live.
- **T2 / T3** — pass on both branches. The emergent lockout and
  non-convergence need conditions this harness has not reached.

  Two changes were needed before even the TTL *signature* appeared, and
  both are worth knowing for any future scenario: **five nodes in a line** rather than three in
  a mesh (a full mesh hides relay effects because the author reaches
  everyone directly), and **staggered clocks** — daemons on one host
  share `CLOCK_MONOTONIC`, so they all fire on the same instant and an
  entry cascades the whole chain in a single round. `Mesh.stagger_clocks()`
  gives each node its own time namespace, which is what separate boot
  times give real routers.

  Even so T22 reaches only ~5 s of spread on a lab bridge. The sweep
  later found why, and it was **not** the propagation speed hypothesis
  stated here originally — see the sweep section above: the driver is
  transfer duration, and a fast bridge with one entry has almost none.

### Not covered

T12 (mixed-endian) still needs `qemu-user` and a cross toolchain — worth
doing, since the fleet is big-endian and handshake msg3 already showed a
byte-order quirk. Memory-versus-state-size needs larger states than
these to produce a curve. Audit A2 (stack recursion) and D4 (EINTR) have no
reliable black-box trigger; A3 (swallowed exceptions) is observable only
through symptoms already covered.

## Running against another branch

```
python3 run_mesh_tests.py --bin /path/to/other/build/shared-state-async
```

`EXPECT_TODAY` is calibrated for the default build. Against another
binary a MATCH of `NO` is a **behavioural difference in the binary under
test** — the reason to run it — and the runner says so rather than
failing. Only `ERROR` rows mean the harness itself broke.

## Recording runs

`--json` writes `results/run-<UTC>.json` and rebuilds
`results/HISTORY.md`, a matrix of every recorded run against every test:

```
python3 run_mesh_tests.py --json
```

The matrix is the point — a cell flipping R to G is a fix landing, and a
G going R is a regression. Use it when validating a fix branch with
`--bin`.

## Baseline measurements

```
python3 experiments/measurements.py
```

Quantities rather than verdicts, written to
`results/MEASUREMENTS.md`. What it establishes:

| entries | bytes/sync | bytes/entry |
|---|---|---|
| 0 | 81 | — |
| 100 | 46,575 | 465 |
| 500 | 232,975 | 466 |

**~465 bytes per entry for this payload shape** (a synthetic entry with
a 120-byte blob) — paid per sync, per neighbour, per interval, whether
or not anything changed. Real entries carry arbitrary JSON, so the
per-entry figure is not a protocol constant; **the invariant is that
every sync serializes the whole state in both directions, so cost is
linear in serialized state size.** At 500 of these entries one sync
moves 233 kB. On a shared radio that is airtime taken from user traffic,
and it is the same quantity that drives TTL divergence, because
divergence tracks transfer duration — the merge defect and the
scalability wall are one problem.

Idle CPU is 0.03% with no peers and no state. Resident memory did not
move measurably across these sizes, so that column is *not yet
measured*, not evidence of flat memory use.

## Parameter sweeps

Individual tests fix their scenario. To vary conditions and accumulate
statistics instead of watching results scroll past:

```
python3 experiments/divergence_sweep.py --quick
python3 experiments/divergence_sweep.py --only bulk-5x30-256kbit
```

Each run writes `experiments/results/<config>.json` and rebuilds
`experiments/results/SUMMARY.md` from **every** result on disk, so the
table grows as configurations are explored.

What the sweep established, and it corrected a wrong hypothesis of mine:
TTL divergence is **not** caused by propagation delay. A sender
serializes its current, already-decayed TTL, so propagation delay
cancels exactly — doubling propagation (`directed-5x30`, 81 s vs 42 s)
left spread unchanged. The driver is **transfer duration**: a receiver
starts bleaching when it finishes reading, while the sender's copy has
been decaying since it serialized.

| config | link | state | spread |
|---|---|---|---|
| `chain-5x30` | lab bridge | 1 entry | 3 s |
| `bulk-5x30-256kbit` | 256 kbit | 250 entries | **112 s** |

Because a sync ships the *entire* state for a type (critique 1.1), that
duration grows with the network — so the defect is **self-amplifying
with mesh size**, and TTL is the only signal the merge rule has. This is
the mechanism behind MonteNet's 22–27 s: a large `wifi_links_info` over
`distance=1000` radio.

## Relationship to `tests/spec-oracle/`

The oracle is the executable form of the protocol spec's merge
semantics — it defines *what should happen*. The strict conditions
asserted here are derived from it. It is not a test of this codebase and
must not be mistaken for one.

## Also here

`wire.py` (byte-exact codec, verified against the real binary) and
`capture.py` (golden-fixture capture) with fixtures under
`fixtures/captured/`. See `doc/protocol-spec-DRAFT.md` §10.

# Real-Binary Mesh Test Harness — PLAN

> **Status: H0-H3 and H6 IMPLEMENTED (2026-08-11) — see `tests/mesh/`.**
> 23 tests, 16 defects confirmed on real binaries. Results, and the
> green results that are NOT clearances, in `tests/mesh/README.md`.
> Remaining: T12/T14, H7 measurements, H4/H5 (deferred). The simulator was retired and the model kept
> as an oracle at `tests/spec-oracle/`, per §7.
> Feasibility of every mechanism below was proven experimentally on
> 2026-08-10 (evidence in §2); the design and the test catalog are
> proposals.

## 0. What changed and why

The existing `tests/spec-suite/` reimplements the merge algorithm in
Python and tests *that*. It was built to test a thesis — "is TTL-as-
freshness the root cause?" — and it answered yes, convincingly. But as
a durable artifact it has a fatal flaw, and it is precisely the flaw
this codebase already suffers from: **a model and an implementation
that can silently diverge**. Commit `db58e3d` is the cautionary tale —
the author's intent and the author's code disagreed for over a year,
unnoticed. A Python reimplementation of merge is a second place for
that to happen.

This plan replaces the model-under-test with **the real binaries under
test**. No algorithm is reimplemented. The harness starts actual
`shared-state-async` processes, puts them on actual TCP links with
actual latency and loss, and asserts on observable behavior. When a
test fails, the failure is in the shipped code, not in a model of it.

Direction: **tests first, red on today's code**, each strict failure
condition paired with the fix that turns it green — so the algorithm
work (with javierbrk) has a definition of done rather than a vibe.

## 1. Design constraints (from the code, all confirmed)

The daemon is hostile to multi-instance testing on one host:

| Constraint | Source | Consequence for the harness |
|---|---|---|
| Binds `in6addr_any:3490`, no `SO_REUSEPORT` | `async_socket.cc` | one daemon per network namespace |
| State/config paths hardcoded to `/tmp/shared-state/` | `sharedstate.hh` | one mount namespace per node |
| Author identity = `/proc/sys/kernel/hostname` | `authorPlaceOlder()` | **one UTS namespace per node** — without this every node believes it authored everything and all author-dependent merge rules are void |
| Peers come from `shared-state-async-discover` on `PATH` | `getCandidatesNeighbours` | topology is injected per node via a stub script — no code change needed |
| `register` dies if the config file is missing | spec §6.6 | pre-seed `{"mTypeConf":[]}` per node |

Note the upside: because discovery is an external script, **arbitrary
topologies are configurable without touching the binary**.

## 2. Feasibility — PROVEN, unprivileged, no root, no containers

All verified on this workstation (Debian, kernel 6.12), user `fede`, no
`sudo` available:

| Mechanism | Verified result |
|---|---|
| `unshare --user --map-root-user --mount --net --uts` | works; we are root *inside* |
| `ip netns add` + bridge + veth pairs across namespaces | cross-namespace ping OK |
| Port isolation | `:3490` free in every namespace simultaneously |
| Per-node `/tmp/shared-state` via tmpfs in own mount ns | OK |
| Per-node hostname via UTS ns | OK (`LiMe-node1`) |
| `tc netem delay/loss` on veth | OK — real latency + packet loss injection |
| **Two real unmodified daemons meshing** | **OK — an entry inserted on node1 propagated to node2 and both dumps matched** |

That last line is the important one: the approach is not theoretical.
QEMU is **not** required for the everyday harness — it earns its place
only in Tier 2/3 below.

Reproduction sketch (what the harness will automate):

```bash
unshare --user --map-root-user --mount --net --fork bash -c '
  mount -t tmpfs none /run; mkdir -p /run/netns
  ip netns add n1; ip netns add n2
  ip link add br0 type bridge; ip addr add 10.99.0.1/24 dev br0; ip link set br0 up
  # veth per node, moved into its netns, addressed 10.99.0.1N
  # each node: ip netns exec nN unshare --mount --uts --fork \
  #   sh -c "hostname LiMe-nodeN; mount -t tmpfs none /tmp/shared-state; ...; shared-state-async peer"
'
```

## 3. Architecture — three tiers

**Tier 1 — netns harness (everyday, seconds-to-minutes).**
Native x86-64 binaries, N nodes, arbitrary topology, `tc netem` per
link. This is where TDD happens and where CI runs. Proven in §2.

**Tier 2 — mixed-endian fidelity (`qemu-user`).**
The fleet is **big-endian MIPS** (LibreRouter v1 / ath79) and we already
found one byte-order landmine: handshake msg3 is echoed in *platform*
order (spec §3). A little-endian-only test suite is structurally blind
to that entire bug class. Tier 2 runs one or more nodes as
cross-compiled `mips-linux` binaries under `qemu-mips` inside the same
netns topology, giving a **mixed-endian mesh** — the exact condition of
a staged rollout. Requires `qemu-user-static` (not currently installed;
`qemu-system-mips` is present) plus an OpenWrt SDK toolchain for the
cross build.

**Tier 3 — full OpenWrt image (`qemu-system-mips`, release gate).**
Real target userspace: procd supervision, real hook scripts, the real
`shared-state-async-discover`, opkg packaging, flash/RAM limits. Slow;
run per release candidate, not per commit. This is where "does the
package actually work" is answered, and it is the natural home for the
M5 packaging validation in `rust-port-plan.md`.

## 4. Observation design (the genuinely hard part)

The model could instrument every merge. Real binaries cannot be, without
patching them. Proposal — **black-box observation, near-passive**:

- **Generation-tagged payloads.** Every value inserted carries
  `{"gen": <monotonic per author>, "src": "<node>"}`. Ground-truth
  freshness ordering is then knowable to the harness while remaining
  invisible to the protocol — exactly the trick the simulator used.
- **Probing via `wire.py`, not `dump`.** The CLI `dump`/`get` commands
  *sync with the local daemon as a side effect*, perturbing what we are
  measuring. Instead the harness opens a normal sync session carrying an
  **empty slice** (`{"stateSlice":[]}`, 17 bytes — a legal message) and
  reads the full state back. The server merges nothing, so zero changes
  and no hooks fire. This reuses the already-fixture-verified codec.
  Residual perturbation to declare: it consumes one connection slot and
  appends a record to `network_statistics.json`.
- **Derived metrics**, computed from the snapshot time series — the same
  ones the model produced, now measured on real processes: generation
  regressions (per node and at the author), author lockout windows,
  time-to-convergence, per-entry staleness/Age-of-Information, TTL
  divergence across nodes.
- **Log capture** per node as secondary evidence (the `is remote peer
  ill?` warning, merge change counts).

## 5. Test catalog — strict failure conditions

Each test states a condition, the expected verdict **on today's code**,
and the fix that would turn it green. Ordered by field impact.

> **These "Today" values were predictions made before the tests
> existed, and two were wrong: T2 and T3 are GREEN, because the emergent
> lockout and non-convergence need conditions this harness has not yet
> reached (see `tests/mesh/README.md`). The results table in that README
> supersedes this column — it is kept here as a record of what was
> predicted versus what was found.**

| # | Strict condition | Predicted | Fix that makes it pass |
|---|---|---|---|
| T1 | No node's held generation for a key ever decreases | **RED** | version-counter merge |
| T2 | After an author publishes gen N, every node serves gen N within deadline D | RED — **actually GREEN** | version-counter merge |
| T3 | After publishers stop, all nodes hold identical state within quiesce window (20% loss) | RED (intermittent) — **actually GREEN** | version-counter merge |
| T4 | A peer that completes the handshake then goes silent must not prevent other peers from syncing | **RED** | per-connection tasks + I/O timeouts (audit B1/B2) |
| T5 | K simultaneous sync requests all succeed; none refused | **RED** | same as T4 |
| T6 | Entry expiry fires hooks (downstream artifacts stop serving dead data) | **RED** | notify on bleach removal |
| T7 | `register` succeeds on a clean system with no config file | **RED** | create config instead of fatal-exit |
| T8 | Hook children do not inherit the listening socket or epoll FD | **RED** (proven) | `CLOEXEC` everywhere |
| T9 | A `register` concurrent with a running daemon never wipes live state | **RED** | temp-file + `rename(2)`; don't destroy state on parse failure |
| T10 | One malformed line from the discover script does not void the whole peer list | **RED** | skip bad lines, check exit status |
| T11 | A node that reboots (state wiped) re-establishes its own keys with its **new** data, never resurrecting pre-reboot data | **RED** | version merge **+ the v2r amendment** (recovery only for locally-inserted entries) |
| T12 | A big-endian MIPS node syncs correctly with little-endian nodes (Tier 2) | unknown | — |

T11 is the one that also tests javierbrk's branch rather than only
master, and it is where our v2r finding earns or loses its keep.

## 6. TDD sequence

- **H0 — harness skeleton + the cheap wins.** Namespace/node lifecycle,
  topology from a declarative spec, probe, metrics, JUnit-ish output.
  First tests: **T7, T8** — single-node, unambiguous, fast. Their job is
  to validate the harness itself and deliver two trivially-mergeable
  upstream fixes, proving the red→green→patch loop end to end.
- **H1 — availability.** **T4, T5.** The highest-impact field defect;
  the fix (per-connection handling + timeouts) is also a prerequisite
  for running the later tests without pathological slowness.
- **H2 — merge correctness.** **T1, T2, T3, T11** against master
  (expected red) and against `merge_with_version` (expected mostly
  green, T11 pending the v2r amendment). **This is the collaboration
  artifact for javierbrk**: a runnable definition of done for the merge
  algorithm.
- **H3 — robustness.** T6, T9, T10.
- **H6 — negative and crash surface.** T13-T21 (below). Added
  2026-08-11: the original T1-T12 catalog covered the defects judged
  highest-impact before anything was built, and finishing it would still
  leave most of `cpp-code-audit.md` unmeasured. Characterization is not
  complete until every documented defect either reproduces or is
  recorded as not reproducible.
- **H7 — quantitative baselines.** Not pass/fail: measurements
  (below). **DONE** — see `tests/mesh/experiments/measurements.py`;
  headline is 466 bytes per entry per sync. Feeds the `data(t)`/G(t) work, which needs staleness and
  propagation as functions of topology, and gives a future port
  something to claim parity against.
- **H4 — Tier 2 mixed-endian.** T12. *Deferred*: infrastructure-heavy,
  and mostly re-tests known behaviour on other hardware — that matters
  at rollout, not during characterization.
- **H5 — Tier 3 OpenWrt image.** Packaging/procd/hook realism.
  *Deferred*, same reason.

### H6 catalog — negative and crash surface

| # | Strict condition | Source |
|---|---|---|
| T13 | `discover` completes every time, never hangs | lime-packages#1198 — the one upstream report with a waiting reporter, "fixed" by a debug printout. Audit A1 offers a hypothesis; the hang is unreproduced here |
| T14 | publish rounds are not skipped when the node is busy | audit B3 (modulo-second scheduling) |
| T15 | a transient resource failure (low FD limit) does not kill the daemon | audit B4 |
| T16 | rapid minimal syncs never divide by zero in bandwidth estimation | audit C4 (SIGFPE) |
| T17 | a failing `shared-state-async-discover` is distinguishable from "no neighbours" | audit C5 |
| T18 | a peer truncating a frame mid-message is rejected cleanly, node keeps serving | audit D2 |
| T19 | concurrent CLI and daemon writes do not tear `network_statistics.json` | critique 1.2 (locking off by default) |
| T20 | an unauthenticated stranger cannot inject state for another node's key | critique 1.3 — documents the posture; failing is the *expected* state, changing it is a project decision |
| T21 | malformed frames (bad lengths, truncation, wrong version) are rejected and the node survives | spec §4 — currently zero negative coverage |

### H7 measurements

| # | Quantity | Why |
|---|---|---|
| M1 | bytes on the wire per sync vs. number of entries | critique 1.1 — full-state exchange is the scalability wall; quantify it |
| M2 | idle CPU and syscall churn | audit F4 — two config parses per second, per-op epoll churn |
| M3 | convergence latency vs. hop count | the `data(t)` requirement: staleness as a function of topology |
| M4 | memory per in-flight connection | critique 1.3 — 1 MiB frame cap, no auth, no backpressure |

Not planned as tests, with reasons: audit **A2** (native stack
recursion) and **D4** (EINTR handling) have no reliable black-box
trigger; **A3** (swallowed exceptions) is observable only as the
symptoms already covered by T13; **C2/C3/D3** are performance
characteristics captured by H7 rather than pass/fail conditions.

Nothing here is blocked on the Rust port. Everything here *serves* the
Rust port: an implementation-agnostic conformance suite is exactly what
the port needs to prove parity, and the same tests run against a Rust
binary by changing one path.

## 7. Language and placement

**Harness in Python 3 (stdlib only), at `tests/mesh/`.** Rationale, and
the honest tradeoffs:

- It orchestrates *processes, namespaces, sockets and time* — not
  algorithms. Nothing about the protocol is reimplemented, so the
  model-drift objection that sinks the current spec-suite does not
  apply here.
- Upstream precedent exists: `tests/python-testclient/` is already
  Python, and doctest is used for in-process C++ unit tests — a category
  this harness is not.
- No build step and no dependencies, so javierbrk (or CI, or a router
  with `python3` installed) can run it against any binary — including a
  future Rust one.
- The alternative — namespace and process orchestration from C++
  doctest — buys fidelity we do not need and costs a great deal of
  awkward code.

**Disposition of the existing spec-suite** (DECIDED, done):
`simulator.py` and `scenarios.py` retired; kept `model.py`/`properties.py` as the
*executable spec oracle* at `tests/spec-oracle/` (it is where the strict
conditions in §5 come from), renamed so it is never mistaken for the test
suite; `wire.py`/`capture.py` moved to `tests/mesh/`, which depends on
them.

## 8. Risks and open questions

- **Timing flakiness.** Real processes plus `netem` means wall-clock
  assertions. Mitigation: generous deadlines expressed in sync-intervals
  rather than seconds, fixed `netem` seeds, and every test reporting its
  margin so tightening is data-driven.
- **T4/T5 are slow by construction** on today's serial daemon — the
  tests that prove the bug are the tests the bug makes expensive. Cap
  runtime and assert on timeout, do not wait for success.
- **Cross-compilation for Tier 2** needs an OpenWrt SDK checkout; budget
  real time for it and treat it as a spike, not a chore.
- **Test-only source changes are forbidden in Tier 1** by design, so
  failures can never be blamed on our patch. If we later want an
  `SS_PORT`/`SS_STATE_DIR` env override for convenience, it should be
  proposed upstream *on its own merits* as a testability patch, not
  smuggled in as harness scaffolding.
- **Unverified**: `netem` behavior on the bridge vs per-veth for
  asymmetric link conditions; whether `ip netns exec` interacts badly
  with the daemon's `pidfd_open`; how the harness should handle a node
  whose daemon fatally exits mid-test (currently many errors are fatal).

## 9. What lands where

Everything develops on `Fede654/shared-state-async` until there is
solid advancement. The intended upstream shape, when ready: a
`tests/mesh/` PR with the harness plus the T1–T11 tests, offered
alongside (not instead of) javierbrk's `merge_with_version`, so the
merge work has a conformance target. The DRAFT protocol spec goes with
it as the written definition the tests encode.

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

Build the binary under test first. **All build and provenance commands
in this file run from the repository root**, while the test commands
above run from `tests/mesh` — paths are written accordingly, so check
which directory a block assumes before pasting it.

```
# from the repository root — provenance-stamping wrapper
# (forces a relink, verifies coupling):
tests/mesh/build.sh
```

It configures, deletes the binary, builds and stamps inside one process,
because that is the only way the stamp can describe a build rather than
a file: while the evidence was passed in on a command line, a copied
binary plus a publicly computable fingerprint earned a full "coupled"
stamp with no build at all. Treat the result as **workflow verification**
— it catches stale binaries, no-op incremental builds and checkouts that
moved between building and measuring — not as tamper-proof provenance,
which would need external signed attestation.

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

24 tests (17 red, 7 green), all against real binaries. `master` is
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
| T24 | an expired entry is not resurrected by an echo of itself | **RED** | — |

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
- **T23** — on `merge_with_version`, deserialization **stops at the
  first entry lacking `mVersion`**: earlier entries survive, that one and
  everything after are lost, and `toStateSlice()` discards the failure
  status so the receiver never learns of it. Deployed nodes send their
  entire state unversioned, so the first entry fails and an upgraded node
  learns *nothing* from them — total loss by truncation. The break is
  asymmetric: v1 receivers still accept v2 entries, so information flows
  v2→v1 but not v1→v2, and the upgraded side is the blind one. Found only because T20 unexpectedly passed on that
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
  proposed explanation (audit A1) is a *hypothesis* about undefined
  behaviour rather than an established diagnosis, so a green run here
  means "not reproducible on this build" and supports no attribution. Reproducing it needs the reporter's toolchain.
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

## Parameter sweeps and controlled experiments

Individual tests fix their scenario. To vary conditions and accumulate
statistics instead of watching results scroll past:

```
python3 experiments/divergence_sweep.py --quick     # exploratory matrix
python3 experiments/single_factor.py --reps 3       # one knob at a time
python3 experiments/divergence_dynamics.py          # fixed-window, timestamped
```

Each writes one JSON per run and rebuilds its summary from **every**
result on disk, so tables grow as configurations are explored.

### TTL divergence is a *rate*, not a value

**Read this before quoting any spread figure from this repo.** TTL
spread grew approximately linearly throughout the observed five-minute
window, in a staircase locked to the update interval:

```
t:      5  16  23  37  45  52  68  76  82  97 105 112 128 135 142
spread: 8  14  17  18  29  28  29  38  40  39  50  51  50  61  62
```

Flat, then a jump of ~11 s every ~30 s — the update interval — where
11 s is 4 hops × 2.6 s of transfer duration. So a "spread" number means
nothing without the observation window that produced it, and any two
spread figures measured over different windows are not comparable.

(That trace is from an early run whose rows were probed sequentially, so
its absolute values carry some skew; it is kept because the *shape* —
flat, step, flat — is the point, and it is unchanged in the corrected
runs. Current records store per-node probe timestamps and correct the
skew to first order.)

Measured over a fixed 300 s window, three reps each
(`results/dynamics/`). The records themselves are **legacy
pre-coupling** — the binary was manually stamped right after the clean
15a1926 rebuild, before `tests/mesh/build.sh` verified coupling — but
the attribution has since been confirmed a stronger way, by
**reproduction**: `src/`, `include/`, `app/` and `CMakeLists.txt` are
byte-identical between 15a1926 and **b531f15**, and two builds verified
in-process at that commit (in-tree and a fresh out-of-tree directory,
both from a clean tree) produce sha256 `b5b3de0a…`, the exact binary
those twelve records name, against the same libretroshare and rapidjson
commits *and* fingerprints, from the same compiled input `09ac2def…`.

Both build stamps and the equivalence check are committed as
`results/REPRODUCTION-b5b3de0a.json`, so this is auditable from a clone
rather than a claim about evidence that lived only on one laptop. **Cite
that record's pinned revision, not "current master"** — a phrase whose
meaning changes with every commit is the moving-checkout attribution
this whole mechanism exists to eliminate. To regenerate, build twice
into different directories and pass both stamps — **all four commands
from the repository root**:

```
tests/mesh/build.sh
tests/mesh/build.sh --build-dir /tmp/ss-repro
python3 tests/mesh/experiments/reproduction_record.py \
    --stamp build/BUILD_PROVENANCE.json \
    --stamp /tmp/ss-repro/BUILD_PROVENANCE.json \
    --equivalent 15a1926 "$(git rev-parse HEAD)" \
    --out tests/mesh/experiments/results/REPRODUCTION-b5b3de0a.json
```

`$(git rev-parse HEAD)` is correct because the two builds above are of
the current tree, so the block is executable as written. The script
checks that revision against the stamps and refuses if they disagree,
so a stale value fails loudly rather than mislabelling the record.

It refuses to write unless all of the following hold: the two stamps
come from different build directories; the binaries, build
configurations, dependency commits and dependency fingerprints all
agree; each build verified its dependencies stable across itself; the
two revisions have identical compiled sources; the stamps agree on both
their whole-tree and **compiled-input** fingerprints (a shared commit is
not shared source on a dirty tree); no compiled path is locally
modified; and both stamps record the same host.

What reproduction does *not* establish: the history of the original
build — it shows that source produces that binary, not that this is how
that file came to exist on 14 August — and nothing about other
machines, compilers or dates, since both builds ran back to back on one
host.

| cell | slope /100 s | probe latency | mesh kbit/s | isolated sync |
|---|---|---|---|---|
| pivot (512 kbit, 40 ms) | **34.1** (33.6–35.3) | 1.02 s | 157.1 | 2.62 s |
| +latency (400 ms) | **55.9** (55.3–56.9) | 5.39 s | 153.3 | 7.74 s |
| −bandwidth (128 kbit) | **71.0** (71.0–71.4) | 9.98 s | 65.5 | 9.40 s |

**The measurement does not appear to cause the effect.** Probing is not passive —
a probe is a real sync session that occupies the daemon's serial accept
loop, for ~10 s at 128 kbit — so each cell was re-run with probing only
at the window's two ends. Comparing endpoint growth, which is the only
form defined for a two-sample control:

| cell | treatment | control |
|---|---|---|
| pivot | 33.1 | 35.2 |
| +latency | 56.4 | 55.9 |
| −bandwidth | 71.2 | 72.4 |

Controls come in *slightly higher*, not lower. That rules out the
proposed **large** probe-induced inflation, and in particular says the
−bandwidth slope is not simply an artefact of a blocked daemon — the
opposite of what the control was built to catch. It does **not** measure
control variance: there is one control per cell, two samples each, and
the pivot and −bandwidth controls fall outside their treatment endpoint
ranges. Subtle observer effects remain unquantified.

**Mechanism, confirmed in code.** A TTL in flight does not decay: the
sender serializes a value, that value is frozen for the whole transfer,
and `sharedstate.cc:896` adopts it whenever `sliceEntry.mTtl >=
knownEntry.mTtl`. Every sync round injects up to one transfer-duration
of artificial freshness — *every round*, which is why divergence
accumulates instead of settling.

`4 hops × transfer / interval` matches the pivot and over-predicts both
slower cells — a heuristic that fitted one cell of three, not an
established bound:

| cell | predicted | measured |
|---|---|---|
| pivot | `4 × 2.62 / 30` = 34.9 | 34.1 |
| +latency | `4 × 7.74 / 30` = 103 | 55.9 |
| −bandwidth | `4 × 9.40 / 30` = 125 | 71.0 |

The gap is *consistent with* fewer completed sync rounds once transfers
grow long — the −bandwidth cell moves 65 kbit/s against the pivot's 157
— but **round completion was not measured**, so that is an inference
about the mechanism, not a result. Treat the expression as a heuristic
that happened to fit one configuration.

The author is structurally excluded from gaining freshness:
`sharedstate.cc:879-888` discards an own-authored entry arriving with a
higher TTL and logs `"is remote peer ill?"`. That is why the author holds
the *lowest* TTL for its own key in 4/4 samples of every run, and why
those warnings scale with whatever injects more freshness.

Because a sync ships the *entire* state for a type (critique 1.1),
transfer duration grows with the network, so the divergence **rate**
plausibly grows with mesh size — **not measured**, node count was never
varied across these runs.

**Bounds, corrected 2026-08-15 — and the correction itself was wrong
once.** Growth was approximately linear over a **five-minute** window;
that is the whole of what was observed. An authored entry starts at
`mBleachTTL + mUpdateInterval + 1s` = 2431 s
(`app/shared_state_cli.cc:66`) and `bleach()` erases at zero, so the
author reaches its **first local expiry at ~40.5 minutes**.

**That moment is not an endpoint.** The previous version of this section
said "nothing ever refreshes the author's own copy" and concluded the
measured quantity ceases to exist; both are **withdrawn**.
`sharedstate.cc:866-873` inserts a *missing* key immediately and
`continue`s — before `ownAuthorship` is computed at :875 and before the
"is remote peer ill?" guard at :882 — so once the author's copy is gone,
a neighbour's inflated echo is adopted wholesale, under the author's own
name, with no publish. **T24 reproduces it deterministically** on the
real binary in under two minutes: the author's key returns from an echo
of 6 s — below its own 14 s insert, so a value a neighbour could
genuinely hold — and no warning is logged because the guard never runs. The spec oracle shows the same sequence.

So the ~830 s obtained by projecting the 34.1 s/100 s pivot rate to
first expiry describes **an unsupported linear extrapolation to a moment
that is not a bound** — eight times past the end of the observed window.
(An earlier version printed ~880 s, which did not follow from the final
rate either.) The claims of the full 2400 s range in "roughly two hours"
and of growth "without bound" both remain **withdrawn**.

**What happens after expiry is being measured** (`experiments/post_expiry.py`).
A first attempt claimed the loop is self-limiting — "extinct mesh-wide
in 5 of 5 runs, so resurrection does not sustain" — and that claim is
**withdrawn**. External review found the instrument, not the mesh, was
producing much of it: failed probes were recorded as absence (so a
crashed daemon manufactured "extinction"), probing occupied the serial
accept loop ~64% of the time and *displaces the gossip that would
prevent* extinction, all-absent rows spanned up to 24 s and so never
showed simultaneity, and the scale-transfer argument leaned on
uncorrected spread compared against an extrapolation. Those runs are
kept, unciteable, in `results/post-expiry-superseded/` with the full
list.

Re-run at `bleach_ttl` 90 (configured insert TTL 96 s) with error-aware sampling,
an enforced propagation gate and a **light-sampling control**
(`results/post-expiry/SUMMARY.md`, 6 runs, all `record_valid: true`):

- **resurrection occurred in 3 of 3 treatment runs, at least twice
  each.** Direct absence-then-return was *sampled* in only 2 runs, but
  that metric undercounts: a node's own samples are ~8.2 s apart (max
  13.6 s), so an expiry and return can pass unseen. What cannot hide is
  the author's TTL going **up** — impossible while it holds the key,
  since `bleach` only decrements and a higher own-authored value is
  discarded at `sharedstate.cc:882`, and the author publishes once. TTL
  resets per run: **+33/+9, +37/+15, +19/+2**. The sampled case, for
  illustration: author at TTL 6 with neighbours at 52–53, absent at
  t=104 s, back at **TTL 34** at t=112 s.
- **no presence was sampled after ~2 lifetimes** — nothing after
  184.7/189.1/210.7 s against a **96 s** configured insert TTL
  (`bleach_ttl + interval + 1`), and all six runs ended absent at a valid
  final row. That is an endpoint statement, not proof of
  self-limitation.
- **repeated probing is not needed for that endpoint** — all three
  controls, 3 rows against the treatment's 59, also ended absent. It
  bounds repeated load only, and cannot show whether resurrection
  occurred in the lightly sampled arm.

The earlier "zero resurrections" came from a cell where the entry
reached all five nodes at t≈28 s with the author already at TTL 3 of 24:
propagation took as long as the entry lived, so no post-expiry window
existed in which a neighbour still held a copy. That was a property of
the cell, not of the daemon.

**"Organic" means peer-generated and not injected** — unlike T24, nothing
here hands the author an entry. It does **not** mean "happens in an
ordinary unobserved mesh": the treatment runs 59 probe rounds on a serial
accept loop, and while an empty probe cannot insert the key, it can delay
gossip past the author's expiry — the exact timing that opens the
missing-key path. Settling that needs passive capture (a merge hook or
packet capture), not more probing.

**Nothing here transfers to production**: interval/TTL is 5/96 against
production's 30/2400, about four times fewer gossip opportunities per
lifetime.

### What earlier versions of this section got wrong

Kept visible because both errors were produced by the tests here, and
both were the kind that look like findings.

- **"Propagation delay cancels exactly."** Withdrawn. It rested on
  `chain-5x30` vs `directed-5x30`, which also changes topology
  directionality. Controlled, 10 → 40 ms does leave spread unchanged,
  but 400 ms raises the divergence *rate* by half again.
- **"Bandwidth scarcity amplifies divergence by 3×."** Withdrawn. The
  apparent gap (177 s vs 62 s) was **observation-window length**, 488 s
  against 236 s.
- **"Latency and bandwidth produce nearly the same slope."** Also
  withdrawn — this was the *over-correction*, and it stood for exactly
  one round of review. It came from a run in which the −bandwidth cell
  had 5–6 sequentially-probed samples and a slope estimate ranging
  58–80; concurrent probing doubled the sample count and tightened it to
  under 1%. The truth sits between the two errors: **71.0 vs 55.9, a
  real but modest 27% difference.**
- **The old `3 s` / `112 s` table.** Both figures are windowed maxima
  over unrecorded windows and are not comparable to each other.

Bandwidth scarcity also costs *availability*, which is a separate defect
from divergence and a larger effect: 10× probe latency (9.98 s vs
1.02 s) and 2.4× lower throughput (65 vs 157 kbit/s). That is the serial
publish loop — the same structure as **T14**.

The pattern across all three: every error was a **plausible number**
produced by an unexamined property of the instrument, never an absurd
one. Window length, row skew, sample count. None would have been caught
by looking harder at the results.

This is still *a* mechanism sufficient to produce MonteNet-scale
numbers; it is not established as *the* mechanism that produced them,
and the field reports do not state their observation window either.

## Relationship to `tests/spec-oracle/`

The oracle is the executable form of the protocol spec's merge
semantics — it defines *what should happen*. The strict conditions
asserted here are derived from it. It is not a test of this codebase and
must not be mistaken for one.

## Also here

`wire.py` (byte-exact codec, verified against the real binary) and
`capture.py` (golden-fixture capture) with fixtures under
`fixtures/captured/`. See `doc/protocol-spec-DRAFT.md` §10.

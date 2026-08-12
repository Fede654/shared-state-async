# START HERE — orientation for this fork

This is `Fede654/shared-state-async`, a fork of
[`libremesh/shared-state-async`](https://github.com/libremesh/shared-state-async).
**No upstream source file has been modified.** Everything added lives in
`doc/` and `tests/`, so the fork is a characterization and specification
effort, not a divergent branch.

Read this file first, then the two documents it points at. Everything
here is verified — where something is unproven or was disproven, it says
so explicitly.

## What this work is

The LibreMesh `shared-state` daemon has never performed well in the
field, and the upstream tracker has years of reports with no root
causes attached. This fork:

1. Audited the C++ and tied each field symptom to a specific defect.
2. Wrote down the protocol, which had never been specified, and locked
   it to the real binary with captured byte-exact fixtures.
3. Built a test harness that runs **the real binaries** on real TCP
   links and reproduces the defects on demand.

The goal is that the people fixing this — especially **javierbrk**, who
carries the merge work, and **G10h4ck**, the original author — get a
runnable definition of "fixed" instead of a list of complaints.

## Read these, in order

| Document | What it gives you |
|---|---|
| `doc/protocol-spec-DRAFT.md` | the wire protocol, merge semantics, and host-side contracts. **DRAFT** — fixture-verified but not authoritative. |
| `doc/cpp-code-audit.md` | every known defect with a code reference. §D5 records two conclusions that testing later **corrected**. |
| `tests/mesh/README.md` | the test suite: what is red, what is green, and which greens are *not* clearances. |
| `doc/refactor-critique.md` | adversarial review: why the protocol is a bigger liability than the implementation. |
| `doc/mesh-test-harness-PLAN.md` | phases H0–H7, what is done and what remains. |
| `doc/rust-port-plan.md` | a possible Rust port. **Gated**: the fielded fleet is LibreRouter v1 (big-endian MIPS, Rust Tier 3), so this is not the fleet fix. |

## Run it

```bash
mkdir -p build && cd build
cmake -DCMAKE_BUILD_TYPE=Release -DSS_CPPTRACE_STACKTRACE=OFF .. && make -j
cd ../tests/mesh
python3 run_mesh_tests.py            # ~20 min, 22 tests, real daemons
python3 run_mesh_tests.py --json     # also record to results/ + HISTORY.md
python3 run_mesh_tests.py T1         # one test
python3 run_mesh_tests.py --bin /path/to/other/build/shared-state-async
python3 experiments/divergence_sweep.py --quick
python3 experiments/measurements.py
cd ../spec-oracle && python3 run_oracle.py
```

Needs Python 3 (stdlib only), `iproute2`, and unprivileged user
namespaces. **No root, no containers, no QEMU.**

## The five things worth knowing before you touch anything

1. **The suite is not pass/fail.** Each test declares `EXPECT_TODAY`;
   the runner reports whether reality matched. Exit 0 means "everything
   behaved as documented", which today includes 15 of 22 tests
   being red.
   **When a fix lands, flip that test's `EXPECT_TODAY` to `GREEN` in the
   same commit.**
2. **`ERROR` is not `RED`.** A broken harness must never be reported as
   a test verdict — a spurious red that "matches expectation" is exactly
   the false confidence this suite exists to prevent. This is not
   hypothetical; it happened on the first run.
3. **`tests/spec-oracle/` is not a test of this codebase.** It models
   merge semantics to define what *should* happen. A Python
   reimplementation of the algorithm is a second place for intent and
   code to diverge — which is the original sin here (`db58e3d` computed
   a guard and never used it, unnoticed for a year). Tests of real code
   live in `tests/mesh/`.
4. **Per-node hostnames are load-bearing.** Author identity is
   `/proc/sys/kernel/hostname`. Without a UTS namespace per node, every
   node believes it authored everything and every author-dependent merge
   rule silently evaporates while the tests still look fine.
5. **Fixes belong on branches, not `master`.** Keeping `master`
   test-only is what makes it adoptable upstream. Validate a fix from
   outside with `--bin`, exactly as we validate javierbrk's branch.

## What is established

- **The merge rule is broken, deterministically** (T1): an author's own
  data is overwritten by a stale echo at equal TTL. javierbrk's
  `merge_with_version` fixes it — T1 is green on his branch.
- **His branch drops everything from un-upgraded peers** (T23, found
  2026-08-12): a slice containing one entry without `mVersion` is
  discarded *whole*. A deployed node sends its entire state unversioned,
  so an upgraded node silently ignores its un-upgraded neighbours —
  rollout-blocking, and invisible, since a dropped slice looks like a
  quiet peer. One-line fix: default a missing `mVersion` to 0. This
  reverses what `doc/protocol-spec-DRAFT.md` §8 originally claimed about
  graceful degradation. **Tell him first.**
- **His branch has its own hazard** (T11): a rebooted node promotes a
  stale entry it adopted from an echo *above* the newest generation it
  was offered. Predicted by the oracle, then confirmed on his binary.
  One boolean fixes it (`v2r`: recovery only for locally-inserted
  entries). **Tell him.**
- **Availability is the biggest field problem** (T4/T5/T21): one silent
  peer takes a node from answering in 0.09 s to not answering at all;
  12 concurrent peers take 9.1× a single session.
- **Registering a data type can kill a running daemon** (T9): a torn
  config read is invalid JSON, and the parse-error path exits the
  process. Deterministic, and worse than the audit predicted.
- **TTL divergence is driven by transfer duration, and therefore grows
  with the network** (T22 + sweep): 3 s with one entry on a lab bridge,
  **112 s** with 250 entries over a 256 kbit link. Since a sync ships
  the entire state, the bigger the mesh, the further TTLs diverge — and
  TTL is the only signal the merge rule has.
- **Sync cost is linear in serialized state, both directions**
  (measurements): ~465 B/entry *for the synthetic 120-byte payload
  measured* — not a protocol constant, since entries carry arbitrary
  JSON — giving 233 kB per sync at 500 entries, paid to every neighbour
  every interval whether anything changed or not. Same quantity as the
  line above: the merge defect and the full-state-exchange scalability
  wall are one problem, not two.
- **Dead peers stop publishing to live ones** (T14): adding three
  unreachable addresses to discovery cut syncs with a *healthy* peer to
  a third to a half of normal cadence (33–44% across runs), because the
  publish loop connects to each discovered peer in turn with no timeout.

## What is NOT established

- **lime-packages#1198 did not reproduce** on GCC 14.2 in 40 runs. It is
  undefined behaviour reported against GCC 12.2/znver3, so this proves
  "not on this build", never "not a bug". Reproducing it needs the
  reporter's toolchain — and it is the one upstream report with a person
  waiting.
- **T2/T3 pass**, so emergent author-lockout is still unreproduced. The
  deterministic tests prove the defect is in the merge rule; catching it
  emergently is open.
- **No fix has been written.** Every defect above is characterized, none
  repaired.

## Where to pick up

- **Highest field value:** the availability fix (T4/T5). Warning from
  T15: descriptor exhaustion is currently unreachable *because* the
  daemon is serial — fixing concurrency exposes the fatal accept path
  (audit B4), so fix accept error handling in the same change.
- **Cheapest upstream wins:** T7 (treat a missing config as first-run)
  and T8 (`CLOEXEC`), roughly ten lines each.
- **Highest collaboration value:** send javierbrk T1 (his fix validated)
  and T11 (the resurrection reproduction plus the amendment).
- **Remaining characterization:** T12 mixed-endian is the only defect
  test left (needs `qemu-user` and a cross toolchain — the fleet is
  big-endian and handshake msg3 already showed a byte-order quirk), plus
  memory-versus-state-size, which needs states large enough to force new
  pages before the number means anything.

## Context you will not find in the code

`~/REPOS/ardc-2024-report/research/` (Fede's, not in this repo) frames
shared-state as the substrate for a time-varying topology graph `G(t)`
used for mesh coordination. That raises the stakes on the merge and
freshness work: a control loop reading a divergent `G(t)` schedules on
inconsistent graphs. The two-plane split matters — shared-state feeds
the slow model plane, never per-slot decisions.

**One contradiction to resolve, flagged by external review.** That
research describes the substrate as *already existing* — "shared-state
v3 Rust/`smol`", CRDT, optimized for ephemeral events. This fork
establishes the opposite: no Rust code exists, merge is explicitly not a
CRDT join (spec §8.1), and v1 does periodic full-state exchange with no
ephemeral path. The defensible claim is that shared-state is a
**candidate foundation for the slow `data(t)`/G(t) plane after a v2
redesign**, not an available substrate. The ARDC document is Fede's to
amend; this note exists so nobody reconciles the two by believing the
optimistic one.

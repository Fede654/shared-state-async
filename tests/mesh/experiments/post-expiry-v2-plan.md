# Post-expiry v2: development plan — passive observation, then a production-parameter long run

Status: development plan, committed before any v2 collection.
Lattice: HRM-146 (passive capture), HRM-147 (driver v2), HRM-148
(topology cells) → gate → HRM-149 (production long run). HRM-150
(rolling-upgrade / tombstone-epoch) is out of scope here — gated on the
javierbrk PR #1 discussion.

Origin: the 2026-08-18 external review of the v1 test bed. Its verdict —
"excellent for proving defects and explaining mechanisms; deliberately
insufficient for quantifying real-world incidence" — matches our own
SUMMARY.md limitations list. This plan attacks the limitations in
value order. The one that could *change a conclusion* (not merely
narrow an interval) is the observer effect: empty probes insert no
state but occupy the serial accept loop, the timing condition that
opens the resurrection path. Everything below is sequenced to remove
that confound first, because the payoff experiment (production
parameters, several lifetimes) is exactly where probing would matter
most.

This plan is NOT the preregistration for the v2 sweep. The
preregistration is written and committed at the end of Phase 3, before
collection starts — this time genuinely before collection, retiring
the "not preregistration" caveat that the v1 analysis carries.

---

## Phase 1 — passive capture, no daemon patch (HRM-146)

Goal: derive author-TTL trajectories and resurrection witnesses from
wire traffic alone, with the daemons untouched and unprobed.

Why no binary change: gossip runs over TCP 3490 between namespaces we
own; the framing and payload serialization are already reverse-engineered
and fixture-verified (`tests/spec-suite/capture.py`, golden fixtures,
spec draft-1 — handshake framing, `NetworkMessage`, JSON payload items
with `mAuthor`/`mTtl`/`mData`). A merge/bleach hook inside the daemon
would be cleaner but is a code change to coordinate with javierbrk;
pcap needs nobody's agreement and cannot perturb the accept loop.

Deliverables, in order:

1. `experiments/wirecap.py` — start/stop `tcpdump` (or a raw-socket
   capturer) inside each node's netns on the veth, one pcap per node
   per run, filenames carrying node + run ID. Must be start-before /
   stop-after the daemons so no exchange is missed.
2. `experiments/wiredecode.py` — offline pcap → per-exchange decoded
   state slices: TCP stream reassembly, handshake skip, message
   framing, JSON payload decode. Output: an event table
   (timestamp, src node, dst node, key, author, ttl, data-gen) in the
   same units the analyzer consumes. Offline-only: decoding never runs
   during collection.
3. Witness extraction from the event table: the same witness classes
   the v1 analysis defined (author-TTL increases as interval-censored
   author events; TTL-reset observations), derived from what peers
   *sent about* the author's key rather than from probes of the author.
4. **Decoder validation run (the gate for the phase):** one probed run
   captured simultaneously by probes AND pcap. Acceptance: every
   probe-derived witness in the v1 sense is reproduced by the
   pcap-derived table (timestamps within one gossip interval), and the
   pcap table contains no contradiction with any probe sample. Persist
   the parity report next to the run record.
5. One unprobed smoke run (Cell-A parameters) end-to-end through
   decode, to prove the passive path stands alone.

Constraint carried over from v1: no edits to any `.py` under
`tests/mesh/` mid-batch — the provenance manifest hashes them all, so
the capture/decode modules land and are committed before the sweep
starts, like everything else.

Risk to check early: whether unprivileged tcpdump works inside our
user/net namespaces (it should — we own the netns; no root was the v1
harness's standing constraint). If not, fall back to a Python
raw-socket capturer in-namespace, which needs only what the harness
already has.

## Phase 2 — sweep driver v2 (HRM-147)

Goal: make the between-cell comparison interpretable. The v1 sweep ran
cells in deterministic A→B order with per-second-constant probing, so
execution order and observer dose per lifetime (~4.2×) confounded Q2 —
amendment 2 demoted it to an exploratory diagnostic for exactly this
reason.

Successor to `post_expiry_batch.sh` (new script, the v1 driver stays
untouched as the record of what ran):

1. Randomized interleaved blocks: cell order drawn per block from a
   seeded RNG; the seed chosen and committed in the driver before
   collection, echoed into every run record.
2. Treatment/control interleaved within blocks, not segregated.
3. Sampling dose equalized in lifetime units — for probed cells,
   probes-per-lifetime constant across cells (not probes-per-second).
   For passive cells the dose question dissolves; the driver still
   records the observation mode per run.
4. Observation mode is a recorded factor: `probes` | `passive` |
   `both` (the "both" mode is what the Phase 1 validation uses, and it
   remains available as an ongoing decoder-drift check, one per sweep).
5. Same provenance regime as v1: coupled build-and-stamp, manifest
   hash of `tests/mesh/**/*.py`, run records with topology, author,
   clock offsets, and now capture metadata (pcap hashes into the
   record).

## Phase 3 — topology cells (HRM-148)

Goal: break the "one five-node chain" scope line cheaply. Same host,
same harness — `set_peers()` already expresses arbitrary graphs.

1. Ring (5 nodes) and chain-plus-one-chord (partial mesh) as
   additional cell topologies, recorded in the existing `topology`
   field.
2. Keep the chain as the anchor cell in every sweep so v2 results
   remain comparable to the v1 evidence.
3. Explicitly out of scope now: multiple physical hosts, wireless
   contention, kernel diversity. Those matter when validating a *fix*;
   replicating an accepted defect across environments is low marginal
   value.

Exit of Phase 3 = **write and commit the v2 preregistration**: cells,
block seed, endpoints, witness definitions (pcap-derived), estimands,
denominators, and the analysis pipeline pinned by hash — before any
non-smoke collection. A short shakedown sweep (a few 480 s runs across
the cells, passive mode) validates the machinery; shakedown runs are
labelled as such and excluded from confirmatory analysis by the
preregistration itself.

## Phase 4 — readiness gate for the production long run (HRM-149)

HRM-149 starts only when all of the following hold:

- [ ] Decoder validation parity report accepted (Phase 1.4).
- [ ] Unprobed run fully analyzable from pcap alone (Phase 1.5).
- [ ] Driver v2 committed with seed; dose model reviewed (Phase 2).
- [ ] Topology cells smoke-tested (Phase 3).
- [ ] v2 preregistration committed before collection.
- [ ] Wall-clock plan: production parameters are interval 30 s /
      TTL 2430 s (lifetime ≈ 40.5 min, ~80 gossip opportunities per
      lifetime, measured directly — not ratio-matched like v1's
      Cell B). Three lifetimes ≈ 2 h per run; a treatment+control
      block ≈ 4–6 h; a small randomized batch is an overnight job.
      The driver runs as a committed script (never `/tmp`) under
      `nohup` with a heartbeat log, per the compaction-resume
      conventions, and is resumable at run granularity.
- [ ] Disk budget checked (pcap at 30 s gossip intervals is small, but
      overnight × nodes × cells — estimate before launch).

What HRM-149 then answers — the review's top "not established" rows:
whether resurrection occurs at production timing *without observation*,
and how the loop behaves at production TTL/interval. Passive-only
observation, preregistered, randomized blocks; both binaries if the
wall clock allows, v1 master first otherwise.

## Sequencing and effort

Phases 1 and 2 are independent until Phase 1.4 (validation uses the
driver's "both" mode) — develop in parallel, validate together. Phase 3
is small and rides the driver. Rough shape: Phase 1 is the only real
engineering (stream reassembly + framing decode, but against known,
fixture-verified formats); Phases 2–3 are driver work of the kind v1
already contains. The long pole is wall-clock in Phase 4, which is why
it runs unattended overnight and everything before it exists to make
that one batch trustworthy the first time it runs.

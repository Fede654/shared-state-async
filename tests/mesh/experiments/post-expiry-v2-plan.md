# Post-expiry v2: development plan — passive observation, then a production-parameter long run

Status: development plan, amendment 1 (2026-08-18) applied after
external review — witness direction corrected, capture completeness
made part of record validity, arms redefined for passive runs,
production parameters corrected, preregistration status restated as
post-pilot, gates mirrored into the lattice graph.
Lattice: HRM-146 (passive capture), HRM-147 (driver v2), HRM-148
(topology cells) → HRM-151 (preregistration freeze) → HRM-149
(production long run). Tombstone/epoch implementation (HRM-150) is out
of scope here — gated on a recorded design decision with javierbrk
(HRM-153); rolling-upgrade/reboot experiments (HRM-152) are
independent and may proceed.

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

This plan is NOT a preregistration. The preregistration for the
confirmatory v2 collection is written and committed at the freeze
point (HRM-151) — after the Phase 1 pilot/validation runs and the
Phase 3 shakedown, before any confirmatory run. It will therefore be a
**post-pilot, pre-confirmatory** preregistration: pilot and shakedown
data will exist when it is written, are labelled as such, and are
excluded from confirmatory analysis by the preregistration itself.
That is an honest improvement over v1 (whose plan was specified
mid-collection with endpoint exposure), not "preregistration before
any collection".

---

## Phase 1 — passive capture, no daemon patch (HRM-146)

Goal: derive author-TTL trajectories and resurrection witnesses from
wire traffic alone, with the daemons untouched and unprobed.

Why no binary change: gossip runs over TCP 3490 between namespaces we
own; the framing and payload serialization are already reverse-engineered
and fixture-verified (`tests/mesh/capture.py`, golden fixtures,
spec draft-1 — handshake framing, `NetworkMessage`, JSON payload items
with `mAuthor`/`mTtl`/`mData`). A merge/bleach hook inside the daemon
would be cleaner but is a code change to coordinate with javierbrk;
packet capture needs nobody's agreement and **does not directly occupy
the accept loop**. It is not free of perturbation: packet copying and
capture-process CPU compete for the same host, so Phase 1 includes a
capture-on/off timing check (identical probed cell with and without
capture; compare exchange-completion timing) before capture is treated
as non-invasive.

### Witness semantics — direction matters

An echo *arriving at* the author proves an **offer**, not acceptance.
Resurrection witnesses are therefore derived **only from the author's
outbound node-to-node full-state slices** — what the author itself
serializes and sends is direct evidence of what it holds:

- **Outbound absence→presence**: the author's own key absent from one
  completed outbound slice and present in a later one, the author
  having published exactly once (before the absence).
- **Outbound TTL increase**: the author's own key carried at a higher
  TTL in a later completed outbound slice than expected from decay,
  under single-publish.

Traffic on probe/host connections is **excluded** from independent
witness extraction entirely. Probe responses are used in exactly one
place: as ground truth in the validation run (below). This closes the
circularity where the decoder "reproduces" probe witnesses by decoding
the probe traffic itself.

### Deliverables, in order

1. `experiments/wirecap.py` — start/stop capture inside each node's
   netns on the veth, one pcap per node per run, full snap length,
   capture command line and tool version recorded, kernel drop
   counters collected at stop. Start-before / stop-after the daemons.
2. `experiments/wiredecode.py` — offline pcap → decoded event table.
   TCP stream reassembly, handshake skip, message framing, JSON
   payload decode. Event schema (per payload item, per exchange):
   session identity (connection 5-tuple + start time), request/response
   role, direction relative to the owning node, peer identity, key,
   author, TTL, data generation, version field if present, frame
   start and completion timestamps, TCP ACK status, and whether the
   connection completed normally. Per-node pcaps see each exchange
   twice — deduplicate by session identity before analysis.
   Offline-only: decoding never runs during collection.
3. Witness extraction per the semantics above, in the units the
   analyzer consumes.
4. **Decoder validation run (the gate for the phase):** one probed run
   captured simultaneously by probes AND pcap ("both" mode — a
   validation artifact, never a confirmatory cell). Acceptance: every
   probe-derived witness in the v1 sense is matched by an
   outbound-derived pcap witness (timestamps within one gossip
   interval), no contradiction between the pcap table and any probe
   sample, and the capture-completeness criteria below all pass.
   Persist the parity report next to the run record. This run is a
   pilot: it precedes, and is excluded from, the confirmatory
   preregistration's dataset.
5. One unprobed smoke run (Cell-A parameters) end-to-end through
   decode, to prove the passive path stands alone. Also pilot data.
6. The capture-on/off timing check described above.

### Capture completeness is part of `record_valid`

Absence evidence is only as good as the capture. A v2 run record is
valid only if all of the following hold, and the flags are stored in
the record:

- Zero kernel capture drops on every node's pcap; no TCP reassembly
  gaps in any decoded gossip stream.
- Full snap length used; capture tool + version + exact command
  recorded.
- Duplicates across per-node pcaps deduplicated by session identity.
- All daemons alive for the whole window (existing liveness checks).
- Initial propagation confirmed (the published key seen in completed
  outbound slices of every non-author node) before author expiry.
- Sufficient completed outbound author slices near the end of the
  observation window — an absence claim at window end requires
  outbound evidence there, not just silence.
- Capture files, decoder output, and stderr/drop-counter files hashed
  into the record alongside the existing manifest.

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

### Arms and factors — passive-only runs have no probe arms

The v1 treatment/control distinction was dense-vs-light probing; it
does not exist in a passive run. Factors, explicitly:

- **Primary production arm: passive, zero probes.** The confirmatory
  estimate comes from this arm only.
- **Observer-effect diagnostic arm (optional): probed vs passive**,
  same cell parameters — quantifies the observer effect directly.
  Excluded from the passive primary estimate.
- **"both" mode: decoder validation only** — labelled `validation` in
  the record, excluded from all confirmatory cells; at most one per
  sweep as a decoder-drift check.

Probe count per lifetime is **not** observer dose: accept-loop service
time varies with bandwidth and state size. Where probed cells are
compared at all, the record must carry **cumulative probe connection
duration per lifetime** (measured from the probe side:
connect→close per probe, summed), as the dose covariate.

### Driver requirements

Successor to `post_expiry_batch.sh` (new script, the v1 driver stays
untouched as the record of what ran):

1. Randomized interleaved blocks: cell order drawn per block from a
   seeded RNG. For confirmatory collection the schedule is not "a
   seed": the **fully materialized run schedule** (ordered list of
   run IDs × cell × arm × binary) is generated, committed, and frozen
   in the preregistration (HRM-151) before collection.
2. Arms interleaved within blocks, not segregated.
3. Observation mode (`probes` | `passive` | `both`) recorded per run;
   `both` always labelled `validation`.
4. Atomic run IDs from the frozen schedule; resume rules (a resumed
   batch continues the schedule, never reshuffles or replaces);
   invalid runs (capture-completeness or infrastructure failure) are
   recorded as invalid with their reason and are **not replaced** by
   outcome — the preregistration states the policy.
5. Same provenance regime as v1: coupled build-and-stamp, manifest
   hash of `tests/mesh/**/*.py`, run records with topology, author,
   clock offsets, and now capture metadata per Phase 1.

## Phase 3 — topology cells (HRM-148)

Goal: break the "one five-node chain" scope line cheaply. Same host,
same harness — `set_peers()` already expresses arbitrary graphs.

1. Exact adjacencies, fixed here: nodes a–e.
   - chain (anchor, unchanged from v1): a–b, b–c, c–d, d–e.
   - ring: chain plus e–a.
   - chord: chain plus b–d.
   Each topology is **verified from observed flows** in the validation
   decode: the set of node-to-node sessions in the pcap must equal the
   declared edge set (both directions), else the run is invalid.
2. Keep the chain as the anchor cell in every sweep so v2 results
   remain comparable to the v1 evidence.
3. Explicitly out of scope now: multiple physical hosts, wireless
   contention, kernel diversity. Those matter when validating a *fix*;
   replicating an accepted defect across environments is low marginal
   value.

Phase 3 ends with a short shakedown (a few 480 s passive runs across
the cells) validating the machinery. Shakedown runs are pilot data:
labelled, and excluded from confirmatory analysis.

## Freeze point — the v2 preregistration (HRM-151)

Written after the pilots, before any confirmatory run (post-pilot,
pre-confirmatory). It must commit, at minimum:

- The fully materialized run schedule (not merely an RNG seed).
- Planned denominators for every endpoint; the primary binary and
  primary topology, decided in advance. In particular the
  second-binary question is decided **here** — "if wall clock allows"
  is discretionary sampling and is not acceptable in the confirmatory
  design.
- Witness definitions exactly as in Phase 1 (outbound-only), endpoints
  and estimands, with the analysis pipeline pinned by hash.
- **Run as the statistical unit.** Packets, exchanges, and individual
  resurrection events within a run are not independent replicates and
  are never counted as such.
- Invalid-run and infrastructure-failure policy: no outcome-dependent
  replacement; invalid runs reported with reasons.
- Atomic run IDs and resume rules as frozen in the driver.

## Phase 4 — readiness gate for the production long run (HRM-149)

HRM-149 starts only when all of the following hold:

- [ ] Decoder validation parity report accepted (Phase 1.4), including
      capture-completeness criteria.
- [ ] Capture-on/off timing check shows no material perturbation
      (Phase 1.6).
- [ ] Unprobed run fully analyzable from pcap alone (Phase 1.5).
- [ ] Driver v2 committed; arms and dose covariate as specified
      (Phase 2).
- [ ] Topology cells shakedown done; adjacency verified from flows
      (Phase 3).
- [ ] v2 preregistration committed (HRM-151) — schedule materialized,
      second-binary decision made.
- [ ] Wall-clock plan: production registration is **bleach TTL 2400 s,
      update interval 30 s**, so an authored entry's initial lifetime
      is **2400 + 30 + 1 = 2431 s** (`app/shared_state_cli.cc:66`) ≈
      40.5 min. "~80 gossip opportunities per lifetime" is the
      *nominal scheduled* count (2431/30); the pcap measures the
      *completed* node-to-node exchanges per lifetime, and the
      measured number is what the analysis uses. Three lifetimes ≈ 2 h
      per run; a passive block of a few runs is 4–6 h; the full frozen
      schedule is an overnight (or multi-night) job. The driver runs
      as a committed script (never `/tmp`) under `nohup` with a
      heartbeat log, per the compaction-resume conventions, resumable
      at run granularity per the frozen schedule.
- [ ] Overnight host checks: laptop sleep/suspend inhibited for the
      batch (`systemd-inhibit` around the driver), no orphan daemons
      or leftover namespaces from previous runs, baseline load checked
      and recorded before launch.
- [ ] Disk budget checked: pcap volume estimated from the shakedown
      (full snap length × exchange rate × nodes × cells × overnight),
      with headroom, before launch.

What HRM-149 then answers — the review's top "not established" rows:
whether resurrection occurs at production timing *without observation*,
and how the loop behaves at production TTL/interval. Passive-only
primary arm, preregistered, frozen schedule.

## Sequencing and effort

Phases 1 and 2 are independent until Phase 1.4 (validation uses the
driver's "both" mode) — develop in parallel, validate together. Phase 3
is small and rides the driver. Rough shape: Phase 1 is the only real
engineering (stream reassembly + framing decode, but against known,
fixture-verified formats); Phases 2–3 are driver work of the kind v1
already contains. The freeze (HRM-151) is a writing task with teeth.
The long pole is wall-clock in Phase 4, which is why it runs unattended
overnight and everything before it exists to make that one batch
trustworthy the first time it runs.

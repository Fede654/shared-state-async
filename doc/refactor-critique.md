# Refactor Red-Team — Challenging the Port Plan and the Protocol

Status: adversarial review of both the codebase (fresh pass, new findings
below) and of our own `rust-port-plan.md` / `cpp-code-audit.md`. Written
to be argued with. Where this document and the plan disagree, this
document is the challenge and the plan is the incumbent — resolve each
point explicitly rather than letting the plan win by default.

## 0. The premise is wrong: there is nothing here to "port"

Of 4,967 LOC: ~2,700 is the hand-rolled reactor (discarded by design),
~500 is the stats subsystem (dead weight, see §1.2), serialization glue
goes to serde, and the actual replicated-map + gossip + hooks logic is a
few hundred lines — **which contain the worst bugs in the codebase**
(dead merge guard, TTL-as-freshness, expiry-without-hooks, torn-config
race). A file-by-file "port" faithfully reproduces the ambiguity of code
that even its authors couldn't keep consistent.

The protocol exists nowhere except in this code and in the field. No
document says what merge is *supposed* to do — commit `db58e3d` proves
even the author's intent and the author's code diverged, unnoticed, for
over a year.

**Deliverable zero is therefore not `main.rs`; it is a 2–3 page protocol
spec** (wire format, handshake, merge semantics including the version
counter, expiry, hook contract) plus golden fixtures and property tests
extracted from the C++ binary. The Rust code then implements the spec,
not the C++ source. This inverts the plan's §3 mapping tables from
"porting guide" to "coverage checklist."

## 1. The protocol is a bigger liability than the implementation

The audit (B1–B3) blamed the implementation for slowness. Fresh eyes:
even a flawless implementation of this protocol degrades the mesh.

### 1.1 Full-state exchange, both directions, every interval

Every sync sends the client's **entire** state slice and receives the
server's **entire** merged state back — up to 1 MB each way, per peer,
per data type, per update interval, forever. No digests, no deltas, no
"nothing changed" fast path. On a wireless mesh, airtime is the scarcest
resource there is, and steady-state gossip of unchanged data competes
with user traffic on every link, scaling O(state × peers × types ×
frequency). This — not coroutine bugs — is the long-term scalability
wall. A wire-compatible Rust port changes none of it.

**Measured since (`tests/mesh/experiments/measurements.py`): ~465 bytes
per entry for a synthetic 120-byte payload — payload-shape-specific, not
a protocol constant — giving 233 kB per sync at 500 entries, paid to
every neighbour every interval whether or not anything changed. The
shape-independent claim is that cost is linear in serialized state size,
in both directions.** And this is the same quantity that sets the TTL
divergence **rate**: a TTL in flight does not decay, so every sync round
injects one transfer-duration of artificial freshness, and divergence
accumulates linearly at 36 s per 100 s at the reference configuration
(T22 + audit C7 + `divergence_dynamics.py`; earlier versions of this
paragraph cited a divergence *magnitude*, which is window-dependent and
was withdrawn). The scalability wall and the merge defect are therefore
one problem, and the coupling is tighter than a magnitude would suggest:
a bigger network means longer transfers, which means a faster rate of
divergence — though the mesh-size step is inference, not measurement:
node count was never varied. **The merge algorithm plausibly becomes
less correct as the network grows; the rate mechanism is measured, the
scaling with node count is not.**

### 1.2 The stats subsystem doesn't earn its cost

The handshake is 3 messages instead of 1 (1.5 RTT extra per sync)
*specifically to estimate RTT*; the byte-count ACK exists to estimate
bandwidth. What that buys:

- Records timestamped with `steady_clock::time_since_epoch()` — i.e.
  **boot-relative ticks** — persisted to JSON. Meaningless across
  reboots, incomparable between nodes; the 30-minute age pruning
  compares fresh boot-relative times against stale ones.
- Read-modify-write of `network_statistics.json` on every sync, with
  file locking **OFF by default** (`SS_STAT_FILE_LOCKING`, CMake) —
  daemon and concurrent CLI syncs interleave and tear the file
  (matching the in-code "Discarding corrupted or empty statistics file"
  warning path).
- Bandwidth math that divides by a duration that floors to zero
  (audit C4).

**Open question that decides real scope: does anything in lime-packages
actually consume this file today?** If not, the honest move for v2 is
deleting the subsystem — and with it the extra handshake messages, the
ACK, `collectStat`, and the flock question. (Wire compat forces keeping
the *messages*; it does not force keeping the estimator or the file.)

### 1.3 Zero authentication, root-executed consequences

Any host that can reach TCP 3490 can inject arbitrary entries into any
data type — `bat-hosts`, DNS host lists, node metadata — mesh-wide
(gossip does the distribution for free), and hook scripts run as root
consuming that data. There is also no rate limiting and no per-entry
size cap below the global 1 MB, so a hostile or broken peer can push
1 MB per connection into RAM of every 64 MB router. Open community
networks may *choose* to trust the L2 domain — but that must be a
documented decision in the spec, not an omission. A port that silently
inherits it makes the omission permanent.

### 1.4 Expired entries never notify hooks

`bleachDataLoop` discards `bleach()`'s return value; `notifyHooks` fires
only on merge changes. When entries expire, downstream artifacts
(hosts files, bat-hosts, DNS) keep serving the dead data until the next
*positive* merge change for that type — indefinitely on a quiet type.
Expiry is a semantic change and must fire hooks; the spec must say so.

## 2. New implementation findings (this pass)

- **F1 — Torn config read, fires every second. CORRECTED 2026-08-11:
  the consequence below was wrong, and the truth is worse.**
  `registerDataType` rewrites the config file in place with truncate (no
  temp+rename), and the daemon calls `loadRegisteredTypes()` **every
  second, twice** (bleach loop + peer loop). This section originally
  predicted a state wipe, on the reasoning that a parse missing a type
  erases that type's in-memory state. Testing (T9) showed that does not
  happen: a truncated file is invalid JSON, and `loadRegisteredTypes`
  returns on `HasParseError()` *before* touching `mTypeConf`. But it
  returns via `rs_error_bubble_or_exit`, and `bleachDataLoop` passes no
  error bubble — so **the daemon exits**. Registering a data type, which
  packages do at install time, can kill a running node. Fix (both
  languages): write-temp + `rename(2)`, parse only on mtime change, and
  never treat a config parse failure as fatal in a long-running loop.
- **F2 — Handshake enforcement is real** (good news): version mismatch
  cleanly refuses the connection on both sides. This is the ready-made
  upgrade lever for any v2: bump `WIRE_PROTO_VERSION`, negotiate.
- **F3 — Discovery is all-or-nothing.** One malformed line from
  `shared-state-async-discover` (blank line, log noise on stdout) makes
  `getCandidatesNeighbours` abort the *entire* peer list — one bad line
  and the node syncs with nobody this round. Combined with ignoring the
  helper's exit status (audit C5), discovery reliability rests entirely
  on a shell script always producing perfect output.
- **F4 — Idle churn.** Two JSON config parses per second at idle
  (see F1), plus stats-file read-parse-rewrite per sync — on routers
  where `/tmp` is RAM-backed tmpfs, this is pure CPU/allocator churn.

## 3. Red-teaming our own plan

### 3.1 Gate 0 is missing and may be disqualifying: MIPS

The plan's §5 "verify feed maturity" is far too soft. Rust demoted all
`mips*-unknown-linux-*` targets to **Tier 3** in 2023 (Rust 1.72): no
prebuilt std, no CI, known codegen-bug exposure, nightly `-Z build-std`
or custom toolchains required. LibreMesh's dominant deployed hardware is
MIPS: ath79 (LibreRouter v1, TL-WDR3600 — big-endian MIPS 74Kc/24Kc) and
ramips/MT76xx (mipsel). If the fleet inventory is MIPS-heavy, the Rust
port as planned is high-risk **precisely for the devices that matter**.

**Gate 0 status: RESOLVED (2026-08-05, fleet input from Fede).** All
in-service devices are LibreRouter **v1** — QCA9558 ath79, big-endian
MIPS 74Kc, 16 MB flash / 128 MB RAM. LibreRouter 2 (MediaTek-candidate
SoC) is a lab-only design with nothing fielded. Consequence: **the Rust
port cannot be the fleet fix.** 100% of production hardware sits on a
Rust Tier-3 target with a 16 MB flash budget. The port is re-scoped to:
(a) reference implementation of the protocol spec, validated on
x86/testbed and LR2-era lab hardware; (b) the substrate for the v2 /
`data(t)` direction (§5); (c) fleet deployment only if/when a Tier-3
MIPS-musl build is proven on a real LR1 — treated as an experiment, not
a milestone. Track 1 (C++ stabilization) is therefore **the** fleet
deliverable, not a stopgap.

### 3.2 The validation plan validates nothing

The plan leans on `tests/python-testclient/` as its regression suite.
Those tests **pass today** against a binary containing every bug in the
audit — serial daemon, UB, dead merge guard, torn-config crash. They are
happy-path smoke tests; a port that reproduced every bug would sail
through them. Actually required, and each is more work than the plan's
corresponding port milestone:

- Property tests on merge: convergence (all nodes reach identical state),
  idempotence, order-independence, version monotonicity — these would
  have caught C1 and C6 on day one.
- Multi-node simulation: N in-process nodes, injected packet loss,
  reordering, slow nodes, reboots (version-counter loss!), clock skew.
  The C6 corruption class is *only* visible here — it was found in the
  field because no such harness exists. G10h4ck's field note
  (`ardc-2024-report/research/shared-state-merge-strategy/`) contains
  the exact target artifact: a simultaneous 4-node TTL-divergence
  snapshot showing 22–27 s windows in which an author cannot update its
  own entries. The harness must reproduce that table synthetically —
  and then show the version-counter merge eliminates it.
- **Freshness metrics as first-class outputs**, not just final-state
  equality: per-entry staleness distributions across nodes — in Age of
  Information terms, P(staleness ≤ t) curves over the gossip layer.
  Convergence-of-value says nothing about convergence-in-time, and the
  `data(t)` use case (§5) makes time the requirement.
- Chaos/soak: hung peers mid-protocol, hooks that block/fail/daemonize,
  malformed discovery output, torn config writes.

### 3.3 "Drop-in wire compat" is a value trap

Strict compat locks in full-state exchange, the RTT-theater handshake,
no-auth, and JSON-with-C++-member-names — effectively forever, because
"protocol v2 later" becomes a migration nobody schedules. Meanwhile the
fleet already tolerates long mixed-version windows badly (#1105: x86
release builds were months stale). Since version enforcement already
exists and works (F2), the defensible scope is: **speak v1 for interop,
and structure the Rust internals so v2 (digest/delta sync, versioned
merge, size limits, optional transport auth) is a protocol module behind
the negotiated version — designed now, shipped when ready.** Plan §9's
"no protocol v2" non-goal should be reworded from "never" to "not in
v1-parity milestones."

### 3.4 Concurrency without backpressure trades one DoS for another

The plan's fix for B1 ("spawn per connection + timeouts") is necessary
but incomplete: unbounded accept-spawn × 1 MB buffers on a 64 MB router
converts the serial-availability failure into a memory-exhaustion
failure. The port needs: a max-concurrent-syncs semaphore, per-peer
connection limits, a global in-flight byte budget, and hard per-message
limits enforced *before* allocation.

### 3.5 Operational scope absent from the plan

- procd service file + respawn policy (the current daemon *exits* on
  many errors; supervision strategy is part of availability).
- Logging design: #1150 was a log-flood bug; the plan never mentions
  log levels/rate-limiting as a requirement.
- Canary rollout: package both implementations, per-node switch, revert
  path — mixed-fleet behavior asymmetry (Rust nodes faster) needs a
  documented observation period.
- Config atomicity fix (F1) must ship in the **C++ fleet too**, not
  wait for the port.

### 3.6 Sequencing: the port is not the urgent deliverable

The mesh is degraded *now*; Rust-to-parity is months. The honest
sequencing is two coordinated tracks:

- **Track 1 (days–weeks, on javierbrk's fork):** C++ stabilization —
  I/O timeouts, accept-error handling, version-counter merge
  (`merge_with_version`), config temp+rename, CLOEXEC, hostname cache,
  `max(1, µs)`. No reactor surgery (the UB fix — symmetric transfer —
  is small and contained in `task.hh`; worth attempting, but everything
  else on this list is safe without it). This de-risks the fleet
  immediately and makes the *target behavior observable in production*
  before Rust ships.
- **Track 2 (Rust):** Gate 0 → protocol spec + fixtures + simulation
  harness (new M-1) → then the plan's M0–M5, validated against the
  simulation, not just the Python scripts.

Track 1 is not wasted work for Track 2 — it is how the golden fixtures
and the merge semantics get field-validated before being spec-frozen.

## 3.7 The spec + fixtures are a collaboration deliverable, not internal docs

The protocol spec and golden fixtures (§0) should be written *for*
upstream — concretely for javierbrk, who is carrying the effort of
getting a minimum workable merge algorithm into the field. Deliverable
form: a spec PR against `libremesh/shared-state-async` `doc/`, fixture
files capturable/verifiable with a small script against any running
binary (C++ master, `merge_with_version`, or the future Rust one), and
the property-test suite runnable against all three. That makes the spec
the *shared reference* the C++ and Rust tracks both answer to, instead
of AlterMundi-fork documentation the C++ maintainers never see.

## 5. Strategic context: shared-state as the `data(t)` bus (ARDC research)

Fede's analysis in `~/REPOS/ardc-2024-report/research/` (esp.
`esp32-medium-coordination/07-modelado-del-grafo-de-conflicto.md` and
`08-shared-state-bus-data-t.md`) reframes what this daemon is *for*, and
therefore what the refactor must protect:

- **Thesis:** next-gen LibreMesh needs a time-varying topology/conflict
  graph **G(t)** for field coordination — feeding both a human/AI
  operator plane and, eventually, low-level medium-access scheduling
  (MW-CA-RF). The `data(t)` stream that estimates G(t) needs a
  distributed substrate, and shared-state *is* that substrate — "not in
  its actual state."
- **Two-plane split (the architecture that makes this sane):** a slow
  model plane (seconds–minutes: G(t) edges, telemetry — shared-state
  gossip, eventual consistency, staleness-tolerant) and a fast execution
  plane (sub-second slot decisions — local, authoritative, explicitly
  NOT shared-state). Shared-state feeds the scheduler's *model*, never
  its per-slot *decisions*.
- **Consequence for this repo:** the TTL-convergence defect (C6) is
  promoted from annoyance to **correctness prerequisite** — divergent
  G(t) between coordinators means scheduling on inconsistent graphs
  (double-assignment/collisions). The merge fix and the freshness
  guarantees stop being Data-Collection hygiene and become a dependency
  of the MW-CA-RF workstream.
- **Design directives the v2 module (§3.3) inherits from that analysis:**
  1. Optimize the ephemeral-event path for **Age of Information**, not
     send rate — "gossip as fast as possible" provably does not yield
     the freshest state.
  2. **Differentiated consistency per data type on the same bus**: lax
     eventual for telemetry; stronger *local* consistency (within the
     collision domain's neighbor set, never network-wide) for G(t)
     edges. The existing per-type config (`DataTypeConf.mScope`) is the
     natural hook for this.
  3. A CRDT gets convergence-of-value only; semantic conflicts (two
     coordinators claiming one cell) need a deterministic policy layer
     above the merge — out of scope for shared-state, but the spec must
     not preclude it.
- **What this does NOT change:** none of the v1-parity scope. It raises
  the stakes of doing the spec + test harness right, because the same
  merge semantics will eventually sit under a control loop. The ARDC
  docs describe this substrate as "shared-state v3 Rust/`smol`" — i.e.
  the port plan's runtime choice is already assumed by that roadmap;
  what must be added to it is the freshness-metric work (AoI/PBS), which
  neither the C++ code nor our plan currently contemplates.

## 4. Disposition table

| # | Challenge | Action required |
|---|---|---|
| 0 | Port the spec, not the code | Write protocol spec + fixtures as deliverable zero |
| 1.1 | Full-state gossip is the scalability wall | Design v2 delta/digest sync as a module; not in v1 scope |
| 1.2 | Stats subsystem dead weight | Verify consumers in lime-packages; if none, drop in v2 |
| 1.3 | No auth, root hooks | Document threat-model decision in spec |
| 1.4 | Expiry never fires hooks | Spec decision + fix in both tracks |
| F1 | Torn config read **kills the daemon** (not a state wipe — corrected by T9) | Fix in C++ now (temp+rename, non-fatal parse failure); same in Rust |
| F3 | Discovery all-or-nothing | Skip bad lines + check exit status, both tracks |
| 3.1 | MIPS Tier 3 may disqualify Rust | **RESOLVED: fleet is 100% LR1 ath79 MIPS → C++ track is the fleet fix; Rust = reference impl + v2 substrate** |
| 3.2 | Python tests validate nothing | Property tests + multi-node simulation harness (M-1) |
| 3.3 | Wire-compat value trap | v1 interop + v2-ready internals; reword §9 non-goal |
| 3.4 | Unbounded spawn = memory DoS | Backpressure limits in port design |
| 3.5 | No ops scope | procd, logging, canary, revert path into plan |
| 3.6 | Port is not the urgent fix | Two-track: C++ stabilization first, in coordination with javierbrk |
| 3.7 | Spec is a collaboration deliverable | Spec + fixtures as upstream PR, runnable against C++/version-branch/Rust |
| 5 | Shared-state is the future data(t)/G(t) bus | Merge fix + freshness metrics (AoI/PBS) are prerequisites for MW-CA-RF; v2 design inherits AoI + differentiated-consistency directives |

# C++ Codebase Audit — Correctness & Performance Findings

Status: code review of the current C++ implementation (master @ `ce8659b`),
done as groundwork for the Rust port (see `rust-port-plan.md`).
Motivation: field performance has never been smooth, and the upstream
tracker confirms it — this audit ties the reported symptoms to root causes
in the code.

Referenced upstream reports:

- [shared-state-async PR #1](https://github.com/libremesh/shared-state-async/pull/1)
  — "Workaround for likely memory managment bug" (a debug printout that
  makes a hang disappear)
- [lime-packages #1198](https://github.com/libremesh/lime-packages/issues/1198)
  — `discover` hangs on x86-64/GCC 12
- [lime-packages #1105](https://github.com/libremesh/lime-packages/issues/1105)
  — insert rate / silent insert failures
- [lime-packages #1129](https://github.com/libremesh/lime-packages/issues/1129)
  — state refresh too slow after mesh upgrade
- [lime-packages #1150](https://github.com/libremesh/lime-packages/issues/1150)
  — log floods incl. "Connection refused" storms and "is remote peer ill?"
- [lime-packages #1220](https://github.com/libremesh/lime-packages/issues/1220)
  — poor responsiveness of shared-state plugins

## A. The coroutine machinery

> **Downgraded 2026-08-12 after external review.** A1 was written as an
> established UB diagnosis and as the explanation for lime-packages#1198.
> It is neither. It is a **hypothesis**, and the reviewer's counter-reading
> is at least as defensible — see the box under A1. A4 below is the
> defect in this area that *is* unambiguous, and it was missed entirely
> on the first pass.

### A1. Frame destroyed while its `resume()` is still on the stack — HYPOTHESIS

`include/task.hh` implements awaiting without symmetric transfer:
`task::await_suspend` calls `child.resume()` directly, and the child's
`final_awaiter::await_suspend` calls `waiter.resume()` directly.

When an awaited coroutine completes **synchronously** (without ever
suspending — e.g. the `closeAFD`/`CloseOperation` chain, where close(2)
never suspends by design, or any error path that returns before the first
real I/O await), the sequence is:

1. Parent P: `co_await childTask` → P suspends → `task::await_suspend`
   calls `child.resume()`.
2. Child runs to completion inside that call, reaches `final_suspend`,
   and `final_awaiter::await_suspend` **resumes P while `child.resume()`
   is still on the stack**.
3. P continues executing; when it passes the end of the `co_await`
   full-expression, the `task` temporary's destructor runs, sees
   `done() == true`, and calls `mCoroutineHandle.destroy()` — **freeing
   the child's coroutine frame while the child's `resume()` invocation is
   still active below on the native stack**.
4. When P later suspends (or completes), the stack unwinds back through
   the destroyed child's coroutine machinery.

**Why this is a hypothesis and not a finding.** The counter-argument:
by the time `final_awaiter::await_suspend` runs, the child is
*suspended* at its final suspend point, and destroying a suspended
coroutine is permitted ([coroutine.handle.resumption]). The contested
part is only whether destroying it while its own `await_suspend` has not
yet returned is defined — the frame must survive until `await_suspend`
returns, and whether the post-`await_suspend` epilogue touches it is
implementation detail rather than a guaranteed violation. The published
guidance on resuming a continuation directly from `final_suspend`
identifies **stack accumulation** (A2) as the concrete hazard, not
frame use-after-free.

Empirically, T13 ran `discover` 40 times on GCC 14.2 without a hang, so
this fork has *not* reproduced #1198 at all. Attributing #1198 to this
mechanism is therefore speculation stacked on speculation.

What remains true regardless: the design resumes continuations directly
rather than by symmetric transfer, which is a real defect (A2), and the
lifetime handling is not under control — `io_context.cc` says so in a
comment ("I haven't fully understood yet why taking just a reference …
cause invalid memory read"). The standard fix is symmetric transfer (`await_suspend` returning
`std::coroutine_handle<>`), which every mature C++ coroutine library
uses. It resolves A2 outright and moots the A1 question.

### A2. Nested resumption also means unbounded native-stack recursion

The same direct-`resume()` design nests one native stack frame per level
of coroutine chaining per completion cascade. Deep chains
(`syncWithPeer` → `sendNetworkMessage` → `AsyncSocket::send` →
`SendOperation` …) unwind recursively. On desktop this is slow; on
small-stack embedded threads it is a latent stack overflow.

### A3. Exceptions are silently swallowed, results become garbage

`promise_type_base::unhandled_exception() {}` drops any exception; the
coroutine then "completes" without `return_value()` ever being called, and
`await_resume()` returns a default-initialized (for scalar types:
**indeterminate**) result to the caller. Any throwing code path inside a
coroutine produces silent wrong results instead of a crash or an error.

### A4. Declaring a new template in `namespace std` — CONFIRMED UB

`include/task.hh:31` opens `namespace std` and declares
`template <typename T> struct task;` inside it, along with a nested
`std::detail` namespace. Adding declarations to `namespace std` is
undefined behaviour unless the standard says otherwise; only explicit
specializations of existing templates for program-defined types are
permitted ([namespace.std]). `std::task` is not a specialization of
anything — it is a new template planted in the standard library's
namespace, and `std::detail` is a new namespace there.

This is not a subtle judgement call like A1: it is a flat violation
with no counter-reading, and the behaviour of the program is
**undefined**. ("Ill-formed, no diagnostic required" is a different
formal category and does not apply here.) It also invites hard-to-diagnose collisions with a
future real `std::task`, which has been repeatedly proposed.

Fix: move the type into a project namespace and adjust the ~30 use
sites. Cheap, mechanical, and it should precede any deeper coroutine
work so that later changes are not built on an ill-formed base.

**Found by external review, not by this audit's first pass** — noted
because it is a fair measure of how much a source read misses: the
dramatic-looking A1 was over-claimed while the unambiguous A4 sat four
lines above it.

## B. Critical — availability: the daemon serves one peer at a time, forever waits

### B1. Accept loop is fully serial

`shared_state_cli.cc` `acceptReqSyncConnectionsLoop()` does
`co_await handleReqSyncConnection(socket)` inline: the next `accept()`
does not happen until the previous client's entire handshake → receive →
merge → send → close cycle finishes. The listen backlog is 8
(`async_socket.hh`). Under mesh load, concurrent peers get **connection
refused** — matching the `ConnectOperation … Connection refused` storms in
lime-packages #1150 — and propagation latency serializes across the whole
neighborhood (#1129, #1220). An in-code comment says detaching the
handler task is not memory-safe in this machinery — consistent with the
lifetime concerns in A1/A2, though which of them forced the serial
design is not established.

### B2. No timeout on any network operation

Neither connect, nor recv, nor send, nor the handshake have any deadline;
`AsyncTimer` exists but is never composed with I/O. Consequences on a
lossy wireless mesh:

- An accepted client that goes silent mid-protocol (no FIN, no RST — the
  normal failure mode of a flaky radio link, and no TCP keepalive is set)
  parks `handleReqSyncConnection` **forever**. Combined with B1, one
  wedged connection permanently stops the daemon from accepting any new
  sync request. This is the highest-impact availability defect in the
  codebase.
- Outbound `connect()` to a blackholed peer stalls the serial publish
  loop in `peer()` for the kernel SYN-retry time (minutes) per dead peer,
  per data type.

### B3. Publish rounds are skipped under load

`peer()`'s scheduler wakes every 999 ms and publishes only when
`now % updateInterval == 0` (in whole seconds). If an iteration is
delayed past that exact second — e.g. by B2 stalls — the round is skipped
entirely until the next interval. Under load, updates become sparse
exactly when the mesh most needs them (#1129).

### B4. A transient `accept()` failure kills the daemon

`ListeningSocket::accept()` never checks the FD returned by
`AcceptOperation` for `-1`; failure feeds `-1` into `registerFD`, whose
`fcntl` fails and — with no error bubble passed — the error handler
**exits the process**. `accept(2)` can fail transiently with
`ECONNABORTED`, `EMFILE`, `ENFILE` on any busy router; each such event is
currently fatal to the whole shared-state daemon.

## C. High — protocol/domain logic defects

### C1. The #1105 fix was never actually wired in (dead variable)

Commit `db58e3d` ("More robust bleaching and merge") introduced in
`SharedState::merge()`:

```cpp
const auto minUpdateTtl = (isRemote && ownAuthorship) ?
            knownEntry.mTtl + std::chrono::seconds(1) : knownEntry.mTtl;

if( sliceEntry.mTtl >= knownEntry.mTtl )   // <-- minUpdateTtl unused!
```

`minUpdateTtl` is computed and never used; the guard it was meant to
implement ("avoid overwrite of own entries from remote peers when TTL is
equal") has been dead code since the day it was written. Remote peers
echoing back this node's own entries at equal TTL silently overwrite the
local (potentially fresher) data, and every equal-TTL echo counts as a
change and re-inserts the entry. **Port decision required:** implement
the *intended* semantics (`>= minUpdateTtl`), not the actual ones —
flagged in `rust-port-plan.md`.

### C2. Hostname re-read from /proc for every entry of every merge

`authorPlaceOlder()` opens and reads `/proc/sys/kernel/hostname` on each
call, and `merge()` calls it **once per received entry** to test
authorship. A sync of a few hundred entries costs hundreds of
open/read/close cycles — on every sync, on a MIPS router. Cache it once.

### C3. Map churn and deep copies on no-op merges

For every incoming entry with `mTtl >= known` — including byte-identical
gossip echoes, the common case — `merge()` does `erase` + `emplace`,
deep-copying the rapidjson DOM (`StateEntry` copy ctor does `CopyFrom`).
Steady-state gossip therefore performs continuous allocator churn
proportional to state size × peers × sync frequency.

### C4. Division by zero in bandwidth estimation

`MbitPerSec(bytes, microseconds)` computes `(bytes<<3)/microseconds`. The
guard checks `recvETP > recvBTP`, but a sub-microsecond transfer (small
message on loopback — and every CLI operation goes through loopback)
floors to **0 µs after `duration_cast`, giving integer division by zero**
(SIGFPE, instant crash). Needs `max(1, µs)`.

### C5. Child exit status ignored

`AsyncCommand::waitTermination` discards `wstatus` (in-code TODO). A
failing `shared-state-async-discover` (missing script, script error)
looks identical to "zero neighbours discovered": `discover`/`peer`
silently operate on an empty peer list instead of reporting the failure.

## C6. Protocol design flaw — TTL conflates expiry with freshness (no versioning)

Beyond the dead-variable bug in C1, the merge algorithm has a deeper
design problem: **remaining TTL is the only freshness signal**, and TTL
is not a version. There is no timestamp, counter, or any mechanism that
allows precise "which state is newest" comparison between nodes.

Concrete corruption scenario (pointed out by javierbrk from field
experience): a node measures its own local conditions and publishes them,
but is too slow processing incoming syncs (see B1–B3 — slowness is the
norm under load). Meanwhile its earlier entries echo back from neighbors
with equal-or-higher remaining TTL — because each node bleaches on its
own clock, remote copies of an entry routinely carry *higher* TTL than
the author's own copy. The `>=` merge rule then makes the author **prefer
the stale external echo over its own fresher measurement**. The node's
state is now corrupt with respect to reality it can directly observe, and
it re-propagates the stale data as if fresh. Convergence becomes
order-dependent and can oscillate rather than settle.

### javierbrk's fix: version counters (`merge_with_version` branch)

[`javierbrk/shared-state-async` branch `merge_with_version`](https://github.com/javierbrk/shared-state-async/tree/merge_with_version)
(key commit `522f59d5` "add version number instead of ttl for merge
algorithm") replaces TTL-as-freshness with a per-entry, author-incremented
monotonic counter:

- `StateEntry` gains `uint64_t mVersion`, serialized as `"mVersion"`
  in the wire payload; the author increments it on each insert
  (`insert()` pre-syncs with the local peer to learn current versions).
- `merge()` accepts an entry only if `mVersion` is strictly higher;
  at equal version, higher TTL wins (TTL demoted to expiry/tie-break).
- **Reboot recovery**: state lives in `/tmp`, so counters are lost on
  reboot. When a node receives its *own-authored* entry back with a
  higher version than it knows, it keeps its own (freshly measured) data
  and adopts `remote version + 1`, so the next sync propagates its data
  mesh-wide. This directly encodes "own measured conditions beat external
  echoes."

Design assessment: a Lamport-style counter is the right call for this
environment — mesh routers have no RTC and unreliable NTP, so wall-clock
timestamps would be *worse* than TTL, not better. Notes for adoption:

- **It is a wire-payload change** (a new `"mVersion"` key). Old nodes
  ignore the unknown key and keep comparing by TTL; new nodes see
  entries from old nodes as version 0. **Measured, and false** (T23,
  2026-08-12): deserialization stops at the first entry lacking the
  member, losing it and everything after, with the failure status
  discarded by `toStateSlice()`. A deployed node's slice is unversioned
  throughout, so the first entry fails and the receiver learns *nothing*
  from it. The break
  is asymmetric — v1 receivers still ignore the unknown field and accept
  v2 entries, so **v2 nodes go blind to v1 state while v1 nodes keep
  seeing v2 state**. Rollout-blocking until a missing `mVersion`
  defaults to 0; a `WIRE_PROTO_VERSION` bump would at least make
  mismatched peers refuse loudly instead of appearing to work.
- At equal version with *different* data (two nodes writing independently
  after reboots), the TTL tie-break silently picks one copy and does not
  count it as a significant change, so hooks don't fire on that
  transition — a small observability gap.
- The branch's history also independently confirms B1/B2: commit
  `c464cfb7` "intial sinc is useless and generates deadlok at startup" —
  the insert pre-sync deadlocked against the busy serial accept loop.

## C7. TTL freshness is manufactured in flight — MEASURED (2026-08-13)

C6 attributes routinely-higher remote TTLs to nodes bleaching *on their
own clocks*. That is not the mechanism, or at least not the one that
dominates: bleach subtracts elapsed seconds (`sharedstate.cc:1063`), so
independent clocks shift phase, not rate, and phase alone cannot make
divergence grow. Measurement (`tests/mesh/experiments/dynamics/`) shows
it grows without bound, so something must inject freshness repeatedly.

**A TTL in flight does not decay.** The sender serializes its current
value; that value is frozen for the entire transfer; and the receiver
adopts it wholesale:

```c++
// sharedstate.cc:896
if( sliceEntry.mTtl >= knownEntry.mTtl )
{ ... tState.erase(stateKey); tState.emplace(stateKey, sliceEntry); }
```

So each sync round hands the receiver a TTL that is up to one
*transfer duration* younger than real time — and it happens **every
round**, so the error accumulates rather than settling. This makes TTL
divergence a **rate**, not a value:

- spread rises in a staircase locked to the update interval, jumping
  ~11 s every ~30 s at the reference configuration
- 11 s = 4 hops × 2.7 s measured transfer duration
- predicted slope `4 × 2.7 / 30` = 36.0 s per 100 s; measured 36.3
  (33.5–38.8 over three reps)

**The author cannot participate in this.** `sharedstate.cc:879-888`
discards an own-authored entry that arrives with a higher TTL and logs
`"is remote peer ill?"`. Every other node ratchets its TTL up toward
whatever frozen value it last heard; the author alone decays
monotonically. That is why the author holds the *lowest* TTL for its own
key in 4/4 samples of every run, why the warning fires thousands of
times per run, and why C6's corruption scenario is not a rare race but
the steady state — the "stale external echo" is *systematically*
fresher-looking than the author's own measurement.

Consequence: because a sync ships the entire state for a type, transfer
duration grows with the network, so the divergence *rate* grows with
mesh size. At 36 s/100 s against a 2400 s bleach TTL, spread reaches the
full TTL range in roughly two hours — an author's entry expiring locally
while downstream copies of it are still considered fresh.

Fix direction: propagate freshness as something that survives transit
(a version counter, as in `merge_with_version`), or decay a received TTL
by the measured transfer duration before merging. The first is
javierbrk's approach and is the better one; the second only narrows the
window.

**Note for anyone re-measuring this.** Two earlier conclusions in this
fork — "propagation delay cancels exactly" and "bandwidth scarcity
amplifies divergence" — were artifacts of comparing windowed maxima over
*different* observation windows. Spread is `rate × window`. Always record
the window, or report a slope.

## D. Medium — resource handling

### D1. FDs leak into child processes (no CLOEXEC)

Only the timerfd and the stats file are opened with CLOEXEC. The epoll
FD, the listening socket, every peer socket, and both pipe ends are
inherited by every hook script and by `shared-state-async-discover`. A
hook that daemonizes or lingers holds port 3490 open (blocking daemon
restart) and holds pipe ends open (preventing EOF detection). All FDs
should be `SOCK_CLOEXEC`/`O_CLOEXEC`/`EPOLL_CLOEXEC`.

### D2. Short-read results unchecked at protocol layer

`AsyncSocket::recv` loops correctly, but returns a short count on peer
EOF; `receiveNetworkMessage` adds `recvRet` to totals without checking it
equals the requested length. A peer disconnecting mid-header yields a
partially-filled length field interpreted as valid data — caught later
only by luck (JSON parse failure or the length bounds check).

### D3. Per-operation epoll_ctl churn and O(all-FDs) scan per loop tick

Every `RecvOperation`/`SendOperation` constructor/destructor toggles
watch flags, and `IOContext::run()` re-scans **all** managed FDs after
every `epoll_wait` batch to apply `EPOLL_CTL_MOD`. Cost grows with
concurrent FDs, wasted almost always (state usually unchanged, guarded by
compare — but the scan itself is unconditional).

### D4. Fragile EINTR handling

The `epoll_wait` EINTR-retry in `io_context.cc` is compiled only because
that file happens to pin `RS_DEBUG_LEVEL` to 1 (`#if RS_DEBUG_LEVEL > 0`
around the retry). Lowering the file's debug level would silently turn
any EINTR into daemon exit. EINTR handling must be unconditional.

## D6. The upstream test suite has not built since 2023 (2026-08-12)

Turning on `SS_TESTS` — which upstream CI never does, so its `ctest`
step reports success while running nothing — reveals five independent
breakages:

1. `tests/CMakeLists.txt` sets `CMAKE_MODULE_PATH` to
   `${CMAKE_SOURCE_DIR}/cmake/`, a directory that does not exist;
   `Doctest.cmake` is at `tests/cmake/`. `include(Doctest)` fails.
2. `target_set_warnings()` is called but defined nowhere — the
   `Warnings.cmake` module was never vendored.
3. `include(CodeCoverage)` — same, never vendored.
4. `Doctest.cmake` fetches doctest only `if(ENABLE_DOCTESTS)`, and
   nothing sets that variable, so the module is a no-op and the tests
   fail to compile on a missing `doctest/doctest.h`.
5. **Fatal to any revival:** the test sources were last touched
   2023-08-17 while `src/sharedstate.cc` was last touched 2025-01-12.
   They include `shared_state_error_code.hh` and
   `piped_async_command.hh` (both deleted) and call
   `SharedState::extractCommand` and `SharedState::mergestate`, neither
   of which exists in the codebase any more.

Items 1–4 are fixed in this fork (the only upstream file it modifies).
Item 5 means the suite cannot be revived without rewriting the tests
against APIs that no longer exist — which `tests/mesh/` supersedes, so
this is recorded rather than repaired.

Related: upstream `ci.yml` also fails at "switch to gcc-10", because
current `ubuntu-latest` images no longer ship `gcc-10`. Between that and
the empty `ctest`, upstream CI currently verifies approximately nothing.

## D5. Corrections from running the tests (2026-08-11)

Reproducing these defects against real binaries (`tests/mesh/`) changed
two conclusions in this document:

- **F1 was wrong about the consequence, and understated the severity.**
  A torn config read does *not* wipe state: a partial file is invalid
  JSON and `loadRegisteredTypes` returns before touching `mTypeConf`.
  But it returns via `rs_error_bubble_or_exit`, and `bleachDataLoop`
  calls it with a null error bubble — so **the daemon exits**.
  Registering a data type, which packages do at install time, can kill a
  running node. Test T9, deterministic.
- **B4 is currently hard to reach, and fixing B1 will expose it.** The
  daemon could not be starved of descriptors through connections,
  because the serial accept loop only ever holds one at a time. The
  fatal accept path becomes reachable the moment per-connection handling
  lands, so accept error handling must be fixed in the same change.
  Test T15.

Also confirmed empirically: D1 (T8), C4 latent but not reachable at
current speeds (T16), C5 (T17), D2 safe in practice (T18), and the
stats file tearing plus boot-relative timestamps (T19).

## E. Implications for the Rust port

Ranked by what the port must do *differently* rather than translate:

1. **Concurrency model (fixes B1/B2/B3):** spawn one task per accepted
   connection; wrap every network I/O and subprocess await in a timeout
   (`async-io::Timer` + `futures_lite::future::or`); keep publish
   scheduling on elapsed-time arithmetic, not modulo-second matching.
   This is a deliberate behavior improvement, not a wire change.
2. **Merge semantics (C1 + C6):** the port should implement
   version-counter merge (javierbrk's `merge_with_version` design) rather
   than either the actual or the intended TTL-based semantics —
   coordinate with javierbrk so C++ and Rust land the same algorithm and
   wire field, and fix the mixed-fleet break first (T23: a missing
   `mVersion` must default to 0 rather than failing the slice).
3. **Free fixes by construction:** A2/A3 — and A1 whatever its status —
   vanish with a real async
   runtime; D1 via Rust std (sockets/files are CLOEXEC by default);
   C4 via a `max(1, µs)`; C2 via caching hostname at startup; C5 by
   checking `ExitStatus`.
4. **Behavior to keep bug-for-bug:** wire format, JSON key names, stats
   file shape, hook contract, CLI verbs — per `rust-port-plan.md` §9.
5. **Validation note:** because B1–B3 change externally observable
   *timing* (not format), mixed-fleet testing in M5 should specifically
   watch for convergence-rate asymmetries between Rust and C++ nodes.

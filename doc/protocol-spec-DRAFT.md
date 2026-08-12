# Shared-State Protocol Specification — **DRAFT**

> ## ⚠️ THIS IS A DRAFT — NOT AUTHORITATIVE ⚠️
>
> Status: **draft-1**, reverse-engineered from `shared-state-async`
> C++ sources (master @ `ce8659b`) and **verified against a running
> binary** (gcc 14.2 Release build, x86-64) via golden-fixture capture
> (§10, fixtures in `tests/mesh/fixtures/captured/`). Former
> ⚠️UNVERIFIED items are now stamped ✅fixture-verified; capture on a
> big-endian target is still pending (relevant only to §3's msg3 note).
> Semantics described here include known defects (kept deliberately:
> v1 describes what deployed nodes DO, not what they should do — see §7
> and `cpp-code-audit.md`). Review by upstream developers — especially
> javierbrk and G10h4ck — is the point of this draft existing.

Scope: the wire protocol, payload encoding, merge/bleach semantics, and
host-side contracts (hooks, discovery, config, stats) of the LibreMesh
shared-state system as implemented by `shared-state-async`. The v2
version-counter merge (javierbrk's `merge_with_version`) is specified as
a proposed extension in §8.

## 1. Model and roles

Shared-state is a mesh-wide eventually-consistent key-value store,
partitioned into named **data types** (e.g. `bat-hosts`,
`wifi_links_info`). Each type holds a map `StateKey (string) → StateEntry`.
Entries carry an author, a TTL, and arbitrary JSON data. Nodes gossip
full per-type state with neighbors; conflicts resolve per §6/§8; entries
expire by TTL ("bleaching", §6.3).

Two processes speak the same wire protocol:

- **peer daemon** (`shared-state-async peer`): listens on TCP 3490,
  serves sync requests, publishes periodically, bleaches, fires hooks.
- **CLI client** (`insert`, `dump`, `get`, `sync`): short-lived process
  that syncs with the local daemon over loopback and/or remote peers.

The daemon distinguishes **loopback** clients (CLI, trusted like local
input) from **remote** peers by source address only. There is no
authentication (§9).

## 2. Transport

- TCP, port **3490**, IPv6 listener with dual-stack (`IPV6_V6ONLY=0`),
  `SO_REUSEADDR`. One sync session per TCP connection, one data type per
  session. Both sides close after the exchange; no keep-alive, no reuse.
- All multi-byte wire integers are **big-endian** (network order).

## 3. Handshake

Three 4-byte messages, each a big-endian `uint32` wire-protocol version.
Current version: **1**.

```
client → server : ver(1)          # client's version
server → client : ver(1)          # server's version (after validating client's)
client → server : ver(1)          # echo (lets server estimate RTT)
```

- Either side receiving a version ≠ its own MUST fail the session
  (observed behavior: log + close; enforced on both sides — applies to
  messages 1 and 2 only).
- The third message exists only so the server can estimate RTT from
  its send→echo interval; the client estimates RTT from message 1→2.
- **✅fixture-verified surprise: msg3's byte order is platform-
  dependent.** The C++ client applies `htonl` twice to the received
  value before echoing, so on little-endian hosts msg3 goes out as
  `01 00 00 00` (captured: `client1_handshake.hex`), while msgs 1–2 are
  proper big-endian `00 00 00 01`. Harmless only because the server
  never validates msg3 — it reads 4 bytes for timing and discards them.
  Implementations MUST NOT validate msg3's content, and MUST NOT assume
  its byte order. (A port should still send big-endian for hygiene.)
- Consequence for evolution: version mismatch cleanly refuses — this is
  the negotiation-free upgrade lever (a v2 speaker cannot talk to a v1
  speaker; there is no downgrade mechanism).

## 4. Message framing

Both directions use the same frame:

```
| 1 byte        | nameLen bytes | 4 bytes BE  | dataLen bytes |
| nameLen       | typeName      | dataLen     | data          |
```

Constraints (receiver MUST enforce): `1 ≤ nameLen ≤ 128`,
`2 ≤ dataLen ≤ 1048576` (1 MiB). Violation → session failure.

**Acknowledgment**: after fully receiving a frame, the receiver sends a
4-byte BE `uint32` equal to the **total bytes received for the frame**
(1 + nameLen + 4 + dataLen). The sender MUST compare it against its own
sent-byte count and fail the session on mismatch. (Purpose: userspace
bandwidth estimation; it is load-bearing for session success.)

## 5. Sync session sequence

```
client                              server
  |------- TCP connect ------------->|
  |<====== handshake (§3) ==========>|
  |------- frame: full client state->|   server merges (§6.2)
  |<------ ack ----------------------|
  |<------ frame: full merged state -|   client merges (§6.2)
  |------- ack --------------------->|
  |        (both sides close)        |
```

- Both directions carry the **entire** known state for the type — the
  client's full map, then the server's full post-merge map. There is no
  delta, digest, or "unchanged" fast path (see critique §1.1).
- The server merge treats the client's entries per §6.2 with
  `isRemote = !isLoopback(clientAddr)`; the client's merge of the
  response likewise (server is remote unless loopback).
- After a successful server-side merge with `significantChanges > 0`,
  the **peer daemon** (only) fires hooks (§6.4).

## 6. Semantics — v1 (deployed behavior, normative for interop)

### 6.1 Entry fields

| Field | Type | Meaning |
|---|---|---|
| `mAuthor` | string | Hostname of authoring node (from `/proc/sys/kernel/hostname`) |
| `mTtl` | int64 seconds | Remaining time to live; **doubles as the freshness/conflict signal (this is the core design flaw — §7, critique §1)** |
| `mData` | any JSON | Opaque payload |

### 6.2 Merge (v1 — what deployed nodes actually do)

For each entry `S[k]` of an incoming slice against local state `L`:

```
if k not in L:            insert S[k]; significant
else:
  own    = (L[k].mAuthor == localHostname)     # hostname read per entry!
  remote = !isLoopback(peerAddr)
  if remote and own and S[k].mTtl >  L[k].mTtl:  discard, warn "is remote peer ill?"
  if                    S[k].mTtl >= L[k].mTtl:  replace L[k] := S[k]
                                                 significant iff data differs
  else:                                          keep L[k]
```

Notes (deliberately specified as-is):

- The equal-TTL case **accepts** remote overwrites of own-authored
  entries — the guard that was intended to prevent this (`minUpdateTtl`,
  commit `db58e3d`) was never wired into the comparison
  (audit C1). A same-TTL echo of an author's *previous* payload
  overwrites the author's *newer* payload: **stale-echo corruption**.
- TTL is decremented independently per node (§6.3) and copied verbatim
  on transfer, so a receiver's copy is systematically "younger" (higher
  TTL) than the author's by roughly the delivery+processing delay,
  accumulating per hop. Field-measured divergence: 22–27 s on a
  5-node network (`shared-state-merge-strategy.md`, MonteNet), during
  which the author's own updates lose merges: **author lockout**.
- `merge` returns the count of *significant* changes (data actually
  differed); insertion of a brand-new key counts as significant.

### 6.3 Bleach (expiry)

Every ~1 s the peer daemon, per type: removes entries with
`mTtl ≤ elapsed`, subtracts `elapsed` (whole seconds, computed from real
elapsed time since last bleach) from the rest. CLI inserts set
`mTtl = bleachTTL + updateInterval + 1`.

**Known gap (audit §1.4): expiry does NOT fire hooks** — downstream
artifacts learn of removals only on the next significant merge.

### 6.4 Hooks

On significant merge (peer daemon only): for each **executable** file in
`/usr/share/shared-state/hooks/<typeName>/` (any order the filesystem
yields, sequentially, awaited):

- Invoked with **no arguments** (✅fixture-verified: `hook ran with 0
  args`), not via shell (`execvp` of the path; command strings are
  whitespace-tokenized — paths with spaces break).
- **stdin** receives the *clean* aggregate state as one JSON object:
  `{ "<key>": <mData>, ... }` — no authors, no TTLs — then EOF
  (✅fixture-verified: `hook_stdin.json`; same shape as CLI `get`).
  Note it is the **whole type's state**, not a diff of what changed.
- Exit status is read but **ignored** (audit C5).
- Hook **stdout is a pipe the daemon never reads** (`dup2`'d in the
  child, parent end registered but not drained): a hook writing more
  than the pipe buffer (64 KiB default) blocks forever, and with the
  serial daemon (audit B1) that wedges the whole node. Hooks MUST
  redirect their own output.
- **✅Confirmed (audit D1)**: hook children inherit the daemon's epoll
  FD and the **listening socket on port 3490** — captured in
  `hook_inherited_fds.txt` (`fd 3 → anon_inode:[eventpoll]`,
  `fd 4 → socket:[...]`). A hook that daemonizes keeps port 3490 bound
  and blocks daemon restart.

### 6.5 Discovery

`getCandidatesNeighbours` executes `shared-state-async-discover` (found
via `PATH`; a LibreMesh shell script, out of scope here) and parses
stdout: **one IP address per line** (IPv4 or IPv6, link-local with
`%iface` scope allowed). Port 3490 is imposed. Exit status ignored.
Any unparseable line aborts the **entire** peer list (audit F3).

### 6.6 Configuration

`/tmp/shared-state/shared-state-async.conf` — object with wrapper key
`"mTypeConf"` holding a map-as-array of
`typeName → {mName, mScope, mUpdateInterval, mBleachTTL}` in the
serialization format of §6.7 (✅fixture-verified: `config_file.json`).
Written by `register` (truncate-in-place, **not atomic** — audit F1);
re-read by the daemon every second in two loops, which **erases
in-memory state for types absent from a parse**. `mScope` is carried but
uninterpreted by the daemon (candidate hook for per-type consistency
policy, critique §5).

**✅Confirmed bootstrap defect**: `register` calls `loadRegisteredTypes`
first, and on a missing config file that path calls
`rs_error_bubble_or_exit` with `errbub = nullptr` → **the process dies
before it can create the file it is meant to create**. First-run
`register` on a clean system therefore fails with
`F ... Failure opening config file for reading` unless the file already
exists (workaround used for fixture capture: pre-seed
`{"mTypeConf":[]}`). LibreMesh installs must be shipping the config file
via package or init script for this never to have surfaced.

### 6.7 Payload encoding (✅fixture-verified)

The frame `data` is UTF-8 JSON produced by libretroshare's
`RsTypeSerializer`. Conventions, all confirmed by captured fixtures
(`client1_request_spec_probe.json`, `server_response_spec_probe.json`)
and by live interop (a Python-encoded slice was accepted and re-served
by the real daemon):

- The state slice is wrapped in an object under the literal key
  **`"stateSlice"`** (both directions).
- `std::map` serializes as a JSON **array of `{"key": K, "value": V}`
  objects** (NOT a plain JSON object).
- `int64` fields (`mTtl`, intervals, `mTS`) serialize as
  **`{"xint64": <number>, "xstr64": "<number-as-string>"}`**.
  **On read, `xint64` is authoritative**: a hand-crafted entry with
  `{"xint64": 200, "xstr64": "777"}` was stored as 200 and re-emitted
  normalized as `{"xint64": 200, "xstr64": "200"}`.
- `StateEntry` object keys: `"mAuthor"`, `"mTtl"`, `"mData"`
  (C++ member names; a port MUST reproduce them byte-exactly).
- An empty slice `{"stateSlice":[]}` is a valid message (used by a
  read-only client to fetch full state; 17 bytes ≥ the 2-byte minimum).

Captured client payload (verbatim, `insert` with
bleachTTL=300/updateInterval=30 — note mTtl = 331 confirming the §6.3
insert formula):

```json
{ "stateSlice": [
    { "key": "probe-key-1",
      "value": { "mAuthor": "lenovo-i7",
                 "mData": { "hostname": "testhost", "n": 42 },
                 "mTtl": { "xint64": 331, "xstr64": "331" } } }
] }
```

The CLI `dump` output is the same map-as-array convention (without the
`stateSlice` wrapper); `get` output is a plain `{key: mData}` object —
the same shape hooks receive on stdin (§6.4).

### 6.8 Statistics file (informative, ✅fixture-verified)

`/tmp/shared-state/network_statistics.json`: object with wrapper key
`"stats"` holding a map-as-array keyed by **IPv4-mapped IPv6 peer
string** (captured: `"::ffff:127.0.0.1"`), each value an array of
`{mTS, mRttExt, mUpBwMbsExt, mDownBwMbsExt}` (max 10 records, 30 min max
age). `mTS`/`mRttExt` use the `xint64` convention; the two bandwidth
fields are **plain JSON numbers** (uint32). Captured `mTS` value
`302045708748019` confirms it is raw `steady_clock` nanosecond ticks —
**boot-relative, not comparable across reboots or nodes**.
Read-modify-written on every sync; file locking is compile-time optional
and OFF by default. Consumers in lime-packages: still unconfirmed
(if none, v2 drops the subsystem — critique §1.2).

## 7. Known-defect ledger (v1)

Interop-relevant behaviors an implementation must decide to reproduce
or fix — full analysis in `cpp-code-audit.md` / `refactor-critique.md`:

| Ref | Behavior | Reproduce in a port? |
|---|---|---|
| C1 | Equal-TTL remote overwrite of own entries | NO — fix via §8 |
| C6 | TTL doubles as freshness → divergence, lockout, stale echo | NO — fix via §8 |
| §1.4 | Expiry fires no hooks | NO — fire hooks on expiry |
| C5 | Hook/discover exit status ignored | NO — check status |
| F3 | Discovery all-or-nothing parse | NO — skip bad lines |
| §6.4 | Hook stdout never drained (blocks > 64 KiB) | NO — drain or /dev/null |
| — | Wire framing, handshake, ACK, JSON key names | YES — byte-exact |

## 8. Proposed extension: version-counter merge ("v2 semantics", informative)

From javierbrk's `merge_with_version` branch (`522f59d5`), specified
here as the intended replacement for §6.2's conflict rule. Wire change:
`StateEntry` gains `"mVersion"` (uint64), serialized with the same
**`{"xint64": n, "xstr64": "n"}` convention as every other integer —
✅verified against a running `merge_with_version` build. Sending it as a
bare JSON number makes the field deserialize as 0, which presents as a
merge bug in the receiver rather than an encoding error in the sender.
That branch also changes the insert TTL to plain `bleachTTL` (300 in
the captured sample) rather than v1's `bleachTTL + updateInterval + 1`. **⚠️ Entries from non-versioned nodes are NOT read as `mVersion = 0` —
they are rejected.** Measured 2026-08-12 (test T23): offering
`merge_with_version` a slice containing one entry without `mVersion`
causes the *entire slice* to be discarded, not just that entry. Since a
deployed v1 node sends its whole state unversioned, an upgraded node
silently discards everything its un-upgraded neighbours say.

```
if k not in L:                       insert; significant
else:
  own    = (L[k].mAuthor == localHostname)
  remote = !isLoopback(peerAddr)
  if remote and own and S[k].mVersion > L[k].mVersion:
      # reboot recovery: we lost our counter; keep OUR data,
      # leapfrog the echo so next sync propagates our data
      L[k].mVersion := S[k].mVersion + 1
  elif S[k].mVersion > L[k].mVersion:  replace; significant iff data differs
  elif S[k].mVersion == L[k].mVersion and S[k].mTtl > L[k].mTtl:
      replace                          # TTL demoted to freshness tie-break
  else: keep
```

Author behavior: `insert` sets `mVersion := current + 1` (1 if new),
`mTtl := bleachTTL`. TTL retains only the expiry role (§6.3 unchanged).

Properties this must satisfy (enforced by the spec suite, §10):
convergence, author supremacy within one sync round, zero stale-echo
corruption, monotone versions per key per node, reboot recovery within
two author sync rounds. Known gap inherited from the branch: at equal
version with differing data the tie-break picks silently and fires no
hooks — the suite documents this; resolution TBD with javierbrk.

### 8.1 Suite-verified findings on §6/§8 semantics

The executable model (`tests/spec-oracle/`) produced these results —
each is a named, reproducible check in the suite:

- **TTL inflation (v1 and v2 both)**: the accept-equal-or-higher rule
  lets same-data echoes refresh each other's TTL across nodes with
  desynchronized bleach clocks, so circulating copies decay *slower
  than real time* (bounded by the publish interval). The author's own
  copy decays honestly — which is why the author systematically holds
  the LOWEST TTL for its own key (the MonteNet table). In v2 this only
  delays expiry; in v1 it also skews conflict resolution.
- **v1 author-island, two polarities**: (a) an author's newer
  generation can fail to displace TTL-inflated stale copies;
  (b) a stale echo corrupts the author and the "is remote peer ill?"
  guard then locks the corruption in by rejecting the mesh's
  correction. Both observed in simulation within minutes of simulated
  time under echo pressure.
- **The intended db58e3d guard (`minUpdateTtl`) would NOT have fixed
  it**: wired in, it makes own-authored entries immutable from remote
  input entirely — trading corruption for author lockout and permanent
  divergence islands. TTL arithmetic cannot express freshness; only
  the version counter resolves this.
- **⚠️ Amendment to §8 (echo resurrection) — CONFIRMED ON THE REAL
  BINARY (2026-08-11).** Predicted by this model, then reproduced
  against a build of `merge_with_version` by `tests/mesh` T11: a node
  rebooted with wiped state, offered gen 3 (version 3) and then gen 7
  (version 7) of its own key, kept **gen 3 and promoted it to version
  8** — above the newest generation it had been offered. Stale data now
  carries top authority and propagates mesh-wide. As written,
  the recovery leapfrog applies even when the node's own-keyed entry
  was itself just echo-inserted after a reboot. Sequence: reboot →
  outdated echo of own key arrives first (plain insert) → newer echo
  arrives → recovery keeps the *outdated* payload and leapfrogs its
  version above everything → mesh-wide stale resurrection until the
  next publish. Fix (suite strategy `v2r`, verified): apply recovery
  **only when the local entry originates from a local insert since
  boot**; otherwise adopt the higher-versioned echo wholesale. One
  boolean of in-memory bookkeeping, no wire change.
- **Merge is not a CRDT join**: merging two echoes of one's own key is
  order-dependent in every strategy (harmless — the next authored
  publish re-anchors — but confluence must not be claimed).

## 9. Security posture (statement of fact)

No authentication, no integrity, no rate limiting, no per-entry size
bound below the 1 MiB frame cap. Any host reaching TCP 3490 can inject
state that gossips mesh-wide and feeds root-executed hooks. This draft
documents the posture; accepting or changing it is a project decision
(critique §1.3) outside this spec's scope.

## 10. Verification: golden fixtures & the spec suite

This spec is validated on two levels. **`tests/mesh/`** runs the real
binaries — 22 tests, 15 of them red for documented reasons, including
the framing, handshake and payload behaviour specified above — and
**`tests/spec-oracle/`** models the merge semantics those conditions are
derived from. Components:

- **Executable model** of §6/§8 semantics (`model.py`) with property
  tests and a multi-node discrete-event simulator that must reproduce
  the field-observed TTL-divergence table and author-lockout windows
  (G10h4ck's MonteNet note) under v1, and their elimination under v2.
- **Wire codec** (`wire.py`) implementing §3–§5 byte-exactly, used to
  capture and replay golden fixtures against real binaries.

**Fixtures captured** (2026-08-06, gcc 14.2 Release build, x86-64;
tool: `capture.py`, stored in `tests/mesh/fixtures/captured/`):

| fixture | what it pins |
|---|---|
| `client1_handshake.{hex,json}` | real CLI handshake bytes incl. the msg3 byte-order quirk (§3) |
| `client1_request_spec_probe.{hex,json}` | real `insert` frame + payload from the C++ client |
| `server_response_spec_probe.{hex,json}` | real daemon response frame (408 B) |
| `our_request_spec_probe.hex` | our encoder's frame that the daemon **accepted** |
| `cli_dump_spec_probe.json`, `cli_get_spec_probe.json` | CLI output shapes (§6.7) |
| `config_file.json`, `network_statistics.json` | on-disk contracts (§6.6, §6.8) |
| `hook_stdin.json` | exact hook stdin payload (§6.4) |
| `hook_inherited_fds.txt` | `/proc/self/fd` of a live hook child — proves audit D1 |

Interop proven in both directions: `capture.py client` ran a full
session against a live `peer` daemon and decoded its response, and an
entry encoded by `wire.py` was accepted, merged, stored and re-served by
the daemon (visible in `cli_dump`). Reproduce with:

```
cmake -DCMAKE_BUILD_TYPE=Release -DSS_CPPTRACE_STACKTRACE=OFF .. && make
echo '{"mTypeConf":[]}' > /tmp/shared-state/shared-state-async.conf   # see §6.6
./shared-state-async register spec_probe community 30 300
./shared-state-async peer &                    # needs a shared-state-async-discover in PATH
python3 capture.py client 127.0.0.1 spec_probe
```

Hook fixtures were captured by bind-mounting the hooks directory into
an unprivileged namespace (no root needed):

```
bwrap --dev-bind / / --tmpfs /usr/share \
      --bind ./hooks-dir /usr/share/shared-state \
      ./shared-state-async peer
```

Still uncaptured: any big-endian-target capture (affects only the §3
msg3 byte-order note).

**The legacy Python client (`tests/python-testclient/`) tests the
pre-async Lua echo protocol** — no handshake, no framing, fuzzy 0.9
similarity assert. It cannot validate anything in this spec and is kept
only as a historical load generator.

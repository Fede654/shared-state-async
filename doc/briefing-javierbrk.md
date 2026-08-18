# shared-state-async: four findings on `merge_with_version`, with runnable tests

Briefing for javierbrk, from the AlterMundi fork
(`altermundi/shared-state-async`, master). Everything below is backed
by a test or a measurement in this tree; every command is listed at the
end. Nothing here argues against the version-counter merge — finding 1
is that it works. The other three findings are what we believe it still
needs before and during rollout, each with a small, contained fix
direction.

## 1. Your fix works: T1 is GREEN on `merge_with_version`

The stale-echo regression that motivated the version counter is fixed
on your branch. `tests/mesh/t01_no_stale_echo_regression.py` — real
daemons, real gossip — is RED on v1 master and GREEN on
`merge_with_version`: a newer generation displaces a TTL-inflated stale
echo, which v1 cannot do. Our protocol spec draft
(`doc/protocol-spec-DRAFT.md` §8) specifies your algorithm as the
intended replacement for the v1 conflict rule, verified against a
running build of your branch, including the
`{"xint64"/"xstr64"}` serialization of `mVersion`.

## 2. Rollout blocker: an upgraded node goes blind to v1 peers (T23)

Measured 2026-08-12 (`t23_mixed_version_interop.py`): entries from
non-versioned nodes are **not** read as `mVersion = 0` — they are
rejected, deserialization **stops at the first entry it cannot read**,
and `NetworkMessage::toStateSlice()` discards the failure status, so
the receiver silently merges the surviving prefix. Since a deployed v1
node sends its whole state unversioned, its *first* entry fails and an
upgraded node learns nothing at all from it — no error anywhere.

Consequence — asymmetric, not a full partition: **upgraded nodes
cannot learn v1-originated state; v1 receivers still accept v2
entries.** The branch needs an explicit rollout decision (read missing
`mVersion` as 0, or bump `WIRE_PROTO_VERSION`) before any incremental
deployment.

## 3. Reboot recovery can promote stale data mesh-wide (T11, fix `v2r`)

Reproduced on a real build of your branch
(`t11_reboot_no_stale_adoption.py`): a node rebooted with wiped state,
offered gen 3 (version 3) of its own key and then gen 7 (version 7),
kept **gen 3 and promoted it to version 8** — above the newest version
it had been offered. The recovery leapfrog applies even when the
local own-keyed entry was itself just echo-inserted after the reboot,
so the *outdated* payload gains top authority and propagates mesh-wide
until the next publish.

Fix, verified in our executable model (`tests/spec-oracle/`, strategy
`v2r`): apply the recovery leapfrog **only when the local entry
originates from a local insert since boot**; otherwise adopt the
higher-versioned echo wholesale. One boolean of in-memory bookkeeping,
no wire change.

## 4. Shared with v1: an expired author resurrects its own entry (T24)

The missing-key path inserts before any authorship or version rule is
consulted — v1's `sharedstate.cc:866` runs before the `:882` guard, and
the branch's merge begins the same way (`if k not in L: insert`). So
when the author's own entry *expires*, a neighbour's echo of it is
re-adopted with whatever TTL and version the echo carries. This is the
no-conflict path: reordering conflicts cannot reach it.

`t24_expiry_is_terminal.py` reproduces it deterministically **on both
binaries** — RED on v1 master and RED on `merge_with_version` at
`22a20aab` (measured 2026-08-17: the author's key came back at TTL 5 s
from a versioned echo offering 6 s, below the author's own insert, with
the authorship guard never running). Beyond the injected mechanism, we
measured it happening with nothing injected — peer echoes only, the
author publishing exactly once. The audited characterization, verbatim
from `tests/mesh/experiments/results/post-expiry/SUMMARY.md`:

> On one host, in one five-node chain topology, in every treatment run
> across four separately reported lab strata, the author's TTL showed
> at least two resurrection witnesses after publishing once. The
> primary audit-stable 96-second cell reproduced in 5/5 runs (Wilson
> 95% CI 57–100%); historical v3 was 3/3 separately. A ratio-matched
> cell (~81 gossip opportunities per lifetime vs production's 80) also
> showed evidence in 3/3 runs, but its comparison is confounded — A→B
> execution order AND ~4.2× the observer dose per lifetime — and
> imprecise (risk difference 0.00, 95% Newcombe −0.43 to +0.56). In
> three ten-lifetime runs, no witness was detected after two lifetimes
> and no presence was sampled after 2.19 lifetimes; this bounds
> observed late recurrence but does not establish self-limitation as a
> protocol property.

In the laboratory, resurrection prolonged stale-state presence. Its
duration and frequency under production parameters remain unmeasured.

Fix direction (spec §8 amendment) — a design gap, not a small patch:
the two missing-key cases differ in local history the daemon does not
currently keep. "Held this key and it expired since boot"
(resurrection: refuse, or demand a version above the one held at
expiry) versus "never seen since boot" (reboot recovery: accept, which
your recovery clause depends on). Since the expired entry is deleted,
nothing remains to carry a marker — distinguishing the cases requires
per-key retained history: a tombstone set/map with explicit lifecycle
and garbage-collection semantics, or a persisted author epoch. TTL
alone cannot distinguish the cases.

## Reproducing everything

Both binaries, explicitly pinned:

```sh
# v1 master (this fork), pinned to the evidence commit
git clone https://github.com/altermundi/shared-state-async && cd shared-state-async
git checkout 4953c42278f8ec352e285e8522c966f0e95baf39
git submodule update --init --recursive
mkdir build && cd build
cmake -DCMAKE_BUILD_TYPE=Release -DSS_CPPTRACE_STACKTRACE=OFF .. && make
cd ..

# merge_with_version at the tested commit
git clone https://github.com/javierbrk/shared-state-async mwv && cd mwv
git checkout 22a20aabdf4a02b7bdc3f18508d296fa5372808b
git submodule update --init --recursive
mkdir build && cd build
cmake -DCMAKE_BUILD_TYPE=Release -DSS_CPPTRACE_STACKTRACE=OFF .. && make
cd ../..

cd tests/mesh                                        # no root needed
python3 run_mesh_tests.py T1 T11 T23 T24             # against v1 master
python3 run_mesh_tests.py --bin ../../mwv/build/shared-state-async \
        T1 T11 T23 T24                               # against the branch
python3 ../spec-oracle/run_oracle.py                 # merge model, v1/v2/v2r
```

Measured expected-result matrix (2026-08-17; the runner's
`EXPECT_TODAY` column is master-relative, so branch runs report
"mismatches" by design — compare against this table):

| test | v1 master | `merge_with_version` @ `22a20aab` |
|---|---|---|
| T1 stale echo vs fresh data | RED | **GREEN** — the fix works |
| T11 reboot adopts newest gen | RED | RED — leapfrog to version 8 |
| T23 accepts unversioned peers | GREEN | RED — both entries dropped |
| T24 expiry is terminal | RED | RED — resurrected at TTL 5 s |

Requirements: Linux with unprivileged user namespaces, Python 3, no
root. Measurement provenance: v1 binary `b5b3de0a` (independent-build
reproduction record `REPRODUCTION-b5b3de0a.json`, 20 gated checks),
per-record dependency manifests, staged analysis with compound-hash
identity (`experiments/results/post-expiry/analysis/`). The full suite
(24 tests, 17 documenting v1 defects as RED-expected) runs with
`python3 run_mesh_tests.py`.

## Proposal

Delivery (see `pr-javierbrk.md` for the prepared text): a **Draft PR**
against `merge_with_version` whose only content is the externalized
findings document (`findings-altermundi-external.md`, with pinned
absolute links back to the tests, evidence records, and spec in this
fork) — explicitly a discussion artifact that need not be merged.
Code arrives afterwards as separate focused PRs, each contingent on
that discussion: (1) T23 interoperability semantics, (2) the `v2r`
guard, (3) the T24 design + test once tombstone-vs-epoch semantics are
agreed. The intent is to help this branch land: with those three
closed, the version counter fixes the class of divergence bugs the
suite documents on v1.

# Findings from independent testing of `merge_with_version`

**This document is a discussion artifact.** It is the only file in this
Draft PR, it changes no code, and it does not need to be merged — it
exists so the findings arrive attached to the branch they concern,
with every claim linked to a runnable test or a committed measurement
in the AlterMundi fork. Follow-up PRs with actual changes are proposed
at the end, each contingent on discussion here.

All links are pinned. Evidence commit in the AlterMundi fork:
[`4953c42278f8ec352e285e8522c966f0e95baf39`](https://github.com/AlterMundi/shared-state-async/tree/4953c42278f8ec352e285e8522c966f0e95baf39).
Branch commit tested here:
[`22a20aabdf4a02b7bdc3f18508d296fa5372808b`](https://github.com/javierbrk/shared-state-async/commit/22a20aabdf4a02b7bdc3f18508d296fa5372808b).

## 1. The fix works: T1 is GREEN on this branch

[T1](https://github.com/AlterMundi/shared-state-async/blob/4953c42278f8ec352e285e8522c966f0e95baf39/tests/mesh/t01_no_stale_echo_regression.py)
(fresh own data vs a stale echo at equal TTL, real daemons over real
gossip) is RED on v1 master and **GREEN on this branch**: a newer
generation displaces a TTL-inflated stale echo, which v1 cannot do.
The AlterMundi
[protocol spec draft](https://github.com/AlterMundi/shared-state-async/blob/4953c42278f8ec352e285e8522c966f0e95baf39/doc/protocol-spec-DRAFT.md)
(§8) specifies this branch's algorithm as the intended replacement for
the v1 conflict rule, wire format verified against a running build,
including the `{"xint64"/"xstr64"}` serialization of `mVersion`.

## 2. Rollout blocker: upgraded nodes go blind to v1 peers (T23)

Measured
([T23](https://github.com/AlterMundi/shared-state-async/blob/4953c42278f8ec352e285e8522c966f0e95baf39/tests/mesh/t23_mixed_version_interop.py)):
entries from non-versioned peers are **not** read as `mVersion = 0` —
deserialization stops at the first entry it cannot read, and
`NetworkMessage::toStateSlice()` discards the failure status, so the
receiver silently merges the surviving prefix. A deployed v1 node
sends its whole state unversioned, so its *first* entry fails and an
upgraded node learns nothing at all from it — no error anywhere.

Asymmetric, not a full partition: upgraded nodes cannot learn
v1-originated state; v1 receivers still accept v2 entries. Needs an
explicit mixed-fleet decision (read missing `mVersion` as 0, or a
`WIRE_PROTO_VERSION` bump) before incremental deployment.

## 3. Reboot recovery can promote stale data mesh-wide (T11)

Reproduced on a build of this branch
([T11](https://github.com/AlterMundi/shared-state-async/blob/4953c42278f8ec352e285e8522c966f0e95baf39/tests/mesh/t11_reboot_no_stale_adoption.py)):
a node rebooted with wiped state, offered gen 3 (version 3) of its own
key and then gen 7 (version 7), kept **gen 3 and promoted it to
version 8** — above the newest version it had been offered. The
recovery leapfrog applies even when the local own-keyed entry was
itself just echo-inserted after the reboot, so the outdated payload
gains top authority until the next publish.

A contained fix is verified in the AlterMundi
[executable merge model](https://github.com/AlterMundi/shared-state-async/tree/4953c42278f8ec352e285e8522c966f0e95baf39/tests/spec-oracle)
(strategy `v2r`): apply the recovery leapfrog only when the local
entry originates from a local insert since boot; otherwise adopt the
higher-versioned echo wholesale.

## 4. Shared with v1: an expired author resurrects its own entry (T24)

The missing-key path inserts before any authorship or version rule is
consulted — v1's `sharedstate.cc:866` runs before the `:882` guard,
and this branch's merge begins the same way. When the author's own
entry *expires*, a neighbour's echo of it is re-adopted with whatever
TTL and version the echo carries. This is the no-conflict path:
reordering conflicts cannot reach it.

[T24](https://github.com/AlterMundi/shared-state-async/blob/4953c42278f8ec352e285e8522c966f0e95baf39/tests/mesh/t24_expiry_is_terminal.py)
reproduces it deterministically **on both binaries** — measured
2026-08-17, the author's key returning at TTL 5 s from a versioned
echo offering 6 s (below the author's own insert TTL, so a value a
lagging neighbour could genuinely hold), the authorship guard never
running. Beyond the injected mechanism, the AlterMundi fork measured
it happening with nothing injected — peer echoes only, the author
publishing exactly once; the audited characterization, its
uncertainty intervals, and its limits (one host, one five-node chain,
observer confounds, no production transfer) are in the
[measurement summary](https://github.com/AlterMundi/shared-state-async/blob/4953c42278f8ec352e285e8522c966f0e95baf39/tests/mesh/experiments/results/post-expiry/SUMMARY.md).
In the laboratory, resurrection prolonged stale-state presence; its
duration and frequency under production parameters remain unmeasured.

Fixing it is a design decision, not a patch: since the expired entry
is deleted, nothing remains to carry a marker, so distinguishing
"held this key and it expired since boot" (refuse the echo, or demand
a higher version) from "never seen since boot" (reboot recovery —
which this branch's recovery clause depends on) requires per-key
retained history: a tombstone set/map with lifecycle and
garbage-collection semantics, or a persisted author epoch.

## Measured result matrix

| test | v1 master | this branch @ `22a20aab` |
|---|---|---|
| T1 stale echo vs fresh data | RED | **GREEN** |
| T11 reboot adopts newest gen | RED | RED |
| T23 accepts unversioned peers | GREEN | RED |
| T24 expiry is terminal | RED | RED |

Machine-readable run records (verdicts, binary hashes, build
provenance, harness revision) for both binaries:
[`tests/mesh/results/`](https://github.com/AlterMundi/shared-state-async/tree/4953c42278f8ec352e285e8522c966f0e95baf39/tests/mesh/results).

To reproduce (Linux, unprivileged user namespaces, Python 3, no
root): build both binaries — AlterMundi fork at `4953c42278f8ec352e285e8522c966f0e95baf39`,
this branch at `22a20aab...` — then from the fork's `tests/mesh/`:

```sh
python3 run_mesh_tests.py T1 T11 T23 T24
python3 run_mesh_tests.py --bin <path-to-branch-binary> T1 T11 T23 T24
python3 ../spec-oracle/run_oracle.py
```

(The runner's `EXPECT_TODAY` column is calibrated to v1 master, so
branch runs report differences by design; compare with the matrix.)

## Proposed follow-ups (each its own PR, after discussion here)

1. **T23 interoperability** — whatever mixed-fleet semantics we agree
   on (`mVersion` absent ⇒ 0, or a wire version bump).
2. **T11 / `v2r`** — the local-insert-since-boot guard on the recovery
   leapfrog.
3. **T24 design + test** — after agreeing tombstone vs persisted-epoch
   semantics.

Everything here is offered to help this branch land: with those three
closed, the version counter fixes the divergence class the AlterMundi
suite documents on v1.

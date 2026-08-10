# spec-oracle — executable merge semantics (NOT the test suite)

```
python3 run_oracle.py [--quick]
```

Pure Python 3 stdlib, deterministic, seconds to run.

## What this is

The runnable counterpart of `doc/protocol-spec-DRAFT.md` §6/§8: a
model of the merge/bleach semantics that says **what should happen**,
across four strategies:

| strategy | meaning |
|---|---|
| `v1` | what deployed nodes actually do (TTL as freshness signal) |
| `v1i` | what commit `db58e3d` *intended* (the `minUpdateTtl` guard actually wired in) |
| `v2` | javierbrk's version-counter merge (`merge_with_version`, `522f59d5`) |
| `v2r` | `v2` plus the amendment this model surfaced — apply the reboot-recovery leapfrog only to entries locally inserted since boot |

`properties.py` holds the property tests. Polarity is
**characterization**: entries marked "defect reproduces" PASS when the
documented defect is still present in the modeled semantics. Green means
"the model still behaves exactly as the spec documents", not "the
algorithm is correct".

## What this is NOT

It is **not** a test of this codebase, and a green run says nothing
about the shipped binary. A Python reimplementation of merge is a second
place for intent and code to diverge — precisely the failure this
project already suffered when `db58e3d` computed `minUpdateTtl` and
never used it, unnoticed for over a year.

Tests of the real code live in **`tests/mesh/`**, which runs actual
binaries and reimplements nothing. The strict failure conditions
asserted there are derived from the properties here.

## What it produced

Findings folded into the spec (§8.1), each reproducible as a property:

- **TTL inflation** — the accept-equal-or-higher rule lets same-data
  echoes refresh each other's TTL between nodes with desynchronized
  bleach clocks, so circulating copies decay slower than real time while
  the author's own copy decays honestly. That is why the author holds
  the *lowest* TTL for its own key, matching G10h4ck's MonteNet field
  table.
- **The intended `db58e3d` guard would not have fixed it** — wired in,
  it makes own-authored entries immutable from remote input, trading
  corruption for author lockout and permanent divergence islands.
- **`v2` echo resurrection** — a rebooted node that hears an outdated
  echo of its own key before a newer one promotes the *stale* payload to
  the highest version and propagates it mesh-wide until its next
  publish. `v2r` fixes it with one boolean of local-origin bookkeeping
  and no wire change. Recommended amendment for javierbrk.
- **Merge is not a CRDT join** — merging two echoes of one's own key is
  order-dependent in every strategy. Harmless (the next authored publish
  re-anchors) but confluence must not be claimed.

## History

This directory was `tests/spec-suite/` and also contained a
discrete-event mesh simulator. The simulator was retired once the real
harness (`tests/mesh/`) could reproduce those scenarios against actual
binaries, which is strictly better evidence.

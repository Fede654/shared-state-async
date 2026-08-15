# Amendment 2 to the v4 analysis plan

Follows `32ab699` (plan) and `ecf0161` (amendment 1); where they
conflict, the latest governs. Unlike the earlier documents this one is
written **after** the v4 outcomes were read and the first analysis
(`3641f7ed`) was published — nothing here is outcome-blinded, and the
corrections below are assessed for whether they change published
numbers.

## 1. Q2 horizon: cell-specific, as implemented

Amendment 1 wrote "H = 288 s for Q2", which is three lifetimes **only
for Cell A** — applied to Cell B it would observe 0.71 lifetime,
before the author can ordinarily expire, making resurrection
structurally impossible. The horizon is defined in normalized time:
witness interval upper bound / `configured_insert_ttl` ≤ 3, i.e.
**H_A = 288 s, H_B = 1218 s**.

Disclosure: the published pipeline already computed it this way
(`classify()` in `post_expiry_extract.py` works in lifetime units with
each run's own TTL) — the amendment text was wrong, the computation was
not. **No published number changes.**

## 2. Witness detection: author sequence, not truncated rows

Amendment 1's "truncate per node and run the analyser unchanged" cannot
work: `analyse()` consumes complete rows and indexes every node in
every row, so ragged rows either crash it or change semantics. The
predeclared method is what the pipeline implements: derive the author's
observation sequence from `obs[author]` across valid rows (row order —
which is `done` order, since sampling rounds are strictly sequential;
`author_events()` asserts count-agreement with `analyse()` on the full
series as a cross-check), detect both witness classes on that
sequence, and classify each witness by its interval bounds in
lifetimes. No truncated structure is ever passed to the row analyser.

## 3. Ambiguous intervals: bounded estimands

"Counted in neither bin" left the run-level binary outcome undefined.
Closed:

- **Definite success**: ≥1 witness interval satisfying the strict rule
  (within-horizon: upper ≤ H; late: lower > L).
- **Primary (lower-bound) proportion**: definite successes / all valid
  planned runs. An ambiguous-only run is NOT a definite success.
- **Sensitivity (upper-bound) proportion**: definite-or-ambiguous
  successes / all valid planned runs.
- Ambiguous runs are named, with their intervals, wherever they exist.
- Q2's Newcombe risk difference is computed for both estimands
  (lower-bound proportions, and the sensitivity pair).

In analysis `3641f7ed` every ambiguous count is zero, so primary and
sensitivity coincide and **no published number changes**; the stats
stage now reports both explicitly.

## 4. Q2 gains a second named confound: observer dose per lifetime

Sampling cadence is absolute (~3 s + serial probe time) while the
lifetime is 4.2× longer in Cell B, so B received ≈4.2× more probes per
lifetime (measured: ~11.6/lifetime in A, ~49/lifetime in B). Dense
probing occupies the serial accept loop and is known to alter gossip
timing. Q2 therefore carries TWO confounds: A→B execution order, and
observer dose per lifetime. This batch can compare ratio-matched lab
cells; it cannot attribute a difference (or its absence) specifically
to gossip opportunities. Attribution requires passive observation or
TTL-scaled sampling in a future sweep.

## 5. v3/v4 pooling: prohibited — no pooled estimate exists

The acquisition diff `d1366561…` → `c899a021…` changed the propagation
gate (first-observed TTL → configured TTL), which is a gating change;
under amendment 1's equivalence rule pooling is therefore
**prohibited**. Decision: **no pooled Q1 estimate will be produced for
v3+v4, in this analysis or later write-ups.** Q1 is the v4 five-run
estimate; v3 remains a historical replication, reported separately.

## 6. Causal configuration: author, offsets, topology

- `author` and the clock-offset schedule join the extracted columns and
  the same-cell assertion (verified constant across all 22 records:
  author `jime`, one stagger schedule).
- `topology_id` becomes a stored record field for future runs;
  `post_expiry.py` records `"topology": "chain-undirected"` from the
  same code path that builds the mesh. For existing v3/v4 records the
  extract stage derives it from the committed script (single code path,
  `mesh.chain(directed=False)`) and marks the source as
  `script-derived`, not measured.

## 7. Immutable-object hash, precisely

For record R: remove top-level keys `analysis` and
`analysis_provenance`; serialize the remainder with Python
`json.dumps(core, sort_keys=True)` under default separators
(`", "`/`": "`), `ensure_ascii=True`, ints and floats in Python's
default `repr` form (records contain only values that round-trip
through `json.load`); hash the UTF-8 bytes with SHA-256. Implemented
verbatim in `post_expiry_extract.py::immutable_sha`.

## 8. Exposure record, corrected

Amendment 1 said "Cell B in progress" at commit time. The auditable
collection state: amendment 1 was committed 2026-08-15T18:54:05Z;
Cell B rep3's record file is stamped 18:53:15Z — **all three Q2
treatment records already existed on disk** when amendment 1 landed
(Cell B controls and all of Cell C did not). File timestamps cannot
prove the records went unread; the working claim remains that no
substantive outcome was read before `6ed0f42`, and Q2's design
decisions predate collection entirely (driver committed `609a79c`,
before the batch started).

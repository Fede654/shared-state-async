# Preregistration — post-expiry v4 sweep analysis

Committed while the v4 batch is still running, before any v4 record has
been read. Status at commit time: the batch (driver `post_expiry_batch.sh`,
committed `609a79c` before launch) has completed Cell A and is inside
Cell B; 10 record files exist on disk, **none opened**. Disclosure: log
tails glimpsed during health checks showed the run-summary endpoint
lines ("absent at window end: True") of at most two Cell-A/B runs and
nothing else — no resurrection counts, no series. The v3 batch
(3 treatment + 3 control at the 96 s cell) is fully known and its
results published in `results/post-expiry/SUMMARY.md`; hypotheses below
are informed by v3 and tested on v4.

## Unit of analysis

The run. Rows, observations, and individual resurrection events are
never treated as independent.

## Predeclared questions

**Q1 — repeatability (Cell A, 96 s TTL, 480 s window, 5 treatments).**
Outcome: proportion of `record_valid` treatment runs with
`resurrection_evidence_count >= 1`. Wilson 95% interval. v3 and v4
reported separately first; pooled only if cell configuration and binary
hash are identical. Hypothesis (from v3's 3/3): most runs show
evidence; the interesting outcome is any zero-evidence run.

**Q2 — opportunity-ratio transfer (Cell B, 406 s TTL ≈ 81
opportunities/lifetime vs production's 80; 3 treatments).** Analysed at
a **common 3-lifetime horizon**: Cell A and B series re-derived from
stored raw series truncated at 3 × configured TTL, same analyser
function. Outcomes: (a) proportion of runs with evidence ≥ 1 within
the horizon, risk difference A−B with a score-based interval;
(b) evidence lower bounds as raw per-run dots. **Labelled exploratory**:
batch order is A→B→C, not randomized, so cell differences are
confounded with elapsed host time. No claim about production — B is a
ratio-matched lab cell, nothing more.

**Q3 — late recurrence (Cell C, 96 s TTL, 960 s = 10 lifetimes, 3
treatments).** Outcome: per run, whether any resurrection witness
(sampled return or TTL reset) has onset time after 2 lifetimes, and
separately after 5 lifetimes. Purely descriptive; n=3 supports no
proportion worth an interval, and absence of late witnesses at an ~8 s
sampling gap bounds only *sampled* recurrence.

## Statistical contract

- Binary outcomes: Wilson intervals and risk differences lead. If any
  formal test is run it is Fisher's exact, secondary, Holm-corrected
  across the three questions. No tests will be added after outcomes are
  seen.
- Resurrection counts: descriptive lower bounds only (dots, median,
  range). No Poisson or rate models — detection is censored and
  sampling-dependent.
- Timing: events are observation *intervals* (between a node's own
  consecutive samples), not instants. No Kaplan–Meier; "no later
  sampled presence" is an observation bound, not extinction.
- Reset deltas: run-stratified dots; events are never pooled across
  runs as if independent.
- Controls are reported as "resurrection not observable (3 samples)",
  never as zero-event runs.

## Pre-committed analysis fixes (found before v4 was seen)

1. `post_expiry_stats.py` `med()` takes the upper-middle element for
   even n; will use `statistics.median` before any v4 aggregation.
2. Grouping key becomes the **full causal cell**: every field of the
   record's `cell` dict (nodes, interval, delay_ms, rate_kbit, entries,
   directed, bleach_ttl) plus window and arm — not just (TTL, window,
   arm) — so config-divergent runs can never silently pool.

## Pipeline (implemented after the batch — `manifest()` hashes every
`.py` under `tests/mesh/`, so adding analysis scripts mid-batch would
invalidate the in-flight record)

`post_expiry_extract.py` (records → runs.csv / events.csv /
observations.csv; refuses invalid records, mixed analysis schemas, and
binaries other than the REPRODUCTION-pinned `b5b3de0a`; missing planned
runs are listed loudly, with partial analysis allowed only via an
explicit flag) → `post_expiry_stats.py` (tables → summary.json +
STATS.md, batch-stratified beside pooled) → `post_expiry_figures.py`
(matplotlib SVG/PDF/PNG: per-run TTL trajectory small multiples,
presence raster at true per-node sample times, run-level outcome panel,
observer diagnostics, common-horizon comparison; every output carries a
manifest of input hashes, analyser hash, and command). Outputs live in
`results/post-expiry/analysis/<analysis-id>/`; source records stay
immutable.

## Future sweeps

Serial execution stays (timing is the measurand), but cell order will
be a committed randomized block schedule instead of A→B→C.

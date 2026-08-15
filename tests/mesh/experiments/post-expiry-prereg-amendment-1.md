# Amendment 1 to the v4 analysis plan (32ab699)

Timestamped by its own commit, while the batch is still running.
`32ab699` is left untouched; where this amendment contradicts it, the
amendment governs. External audit found the plan not yet fully
specified — several choices could still have been made after seeing
outcomes. Each is closed here.

Batch state at this amendment: Cell A complete, Cell B in progress
(records exist on disk, unopened beyond what is disclosed below).

## 1. Status reclassified — this is not preregistration

`32ab699` and its commit message called itself a preregistration made
"before outcomes exist to peek at". **That claim was false when made**:
at commit time Cell A's five treatment and three control records and
two Cell B records existed on disk, and log tails during health checks
had exposed endpoint lines ("absent at window end: True") of at most
two runs. Git verifies the timestamp; nothing can verify the files were
never opened.

Correct designation: **a mid-collection, partially outcome-blinded
prospective analysis plan**, with the endpoint exposure above. Valuable
because the analysis choices predate reading any resurrection count,
trajectory, reset delta, or late-recurrence outcome — not equivalent to
registration before collection.

Auditor transparency, carried into the record: the audit that prompted
this amendment inspected v4 schema/provenance fields and one validity
flag, not substantive outcomes.

## 2. Event intervals and horizons, exactly

Resurrection witnesses are interval-censored. For every witness at the
author:

- **Event interval** = (previous author observation `done`, current
  author observation `done`], both in per-node observation time — never
  the shared row timestamp `t`.
- **Evidence within horizon H**: interval upper bound ≤ H.
- **Definitely late (threshold L)**: interval lower bound > L.
- **Ambiguous**: interval straddles H or L — counted in neither bin,
  reported separately, never dropped.
- **Horizon truncation** (Q2's 3-lifetime reanalysis): a truncated
  series keeps, per node, exactly the observations with `done` ≤ H;
  the analyser then runs unchanged on that subset.

Lifetimes are multiples of `configured_insert_ttl` (96 s → H = 288 s
for Q2; L = 192 s and 480 s for Q3).

## 3. No formal hypothesis tests for v4

The conditional "if any formal test is run" left a post-outcome choice
open. Closed: **no formal hypothesis tests will be run on v4 data.** No
Fisher, no Holm family, no p-values. Reporting is: raw per-run
outcomes, Wilson 95% intervals on proportions, and one exploratory
effect estimate (Q2's risk difference). Anything beyond that in a
future write-up is post-hoc and must be labelled so.

## 4. Q1 primary estimate is v4-only

v3 was acquired by script `d1366561…`, v4 by `c899a021…`, with
substantial changes between them; reanalysis aligns derived fields, not
acquisition behaviour. Therefore:

- **Primary Q1 estimate: the five v4 Cell A treatment runs.**
- v3's three runs: reported separately as historical replication.
- A pooled 8-run figure is secondary at most, and only after a
  documented acquisition-path equivalence audit (a reviewed diff of
  `d1366561…` → `c899a021…` showing no change to probing, sampling
  cadence, gating, or recording of the raw series) — matching binary
  and cell dict alone is insufficient.

## 5. Remaining specifications closed

- **Q2 comparator**: v4 Cell A only (the five primary runs), truncated
  to the 3-lifetime horizon by the rule in §2. v3 is not a comparator.
- **Interval methods**: proportions get Wilson 95% intervals; the Q2
  risk difference gets a **95% Newcombe hybrid-score interval without
  continuity correction**.
- **Q3 intervals**: Wilson 95% intervals ARE reported at n=3 (0/3
  leaves an upper bound near 56%, and that width is the message).
  "No interval worth reporting" in 32ab699 is withdrawn.
- **Missingness policy**: planned denominators are fixed by the
  committed driver — treatments 5 (A) / 3 (B) / 3 (C), controls 3 (A) /
  2 (B). Every report states planned n, valid n, invalid filenames with
  their failure reasons, and best/worst-case bounds for each binary
  outcome (all-invalid-runs-positive / all-negative). **Invalid or
  timed-out runs are not re-run for v4** — selective repetition after
  seeing which runs failed is outcome-dependent sampling.
- **Cell key**: the full record `cell` dict plus window, arm, topology
  (chain; `directed` flag), `sample_gap_s`,
  `propagation_check_t_requested`, and the acquisition-script hash.
  Batch (v3/v4) is a stratum, never silently pooled.
- **Controls endpoint**: predeclared as `absent_at_end` by cell with a
  Wilson 95% interval, reported under the standing label "resurrection
  not observable (3 samples)".
- **Immutability, corrected**: "source records stay immutable"
  conflicted with `--reanalyse`, which rewrites derived fields in
  place. The immutable object is the **raw series plus acquisition
  metadata**; the extract stage hashes that portion of each record
  separately and re-derives all statistics from the series itself, so
  v4 aggregate results never depend on in-record `analysis` fields.
  `--reanalyse` remains a convenience for humans reading single
  records; it is not an input to the pipeline. No `--reanalyse` runs on
  v4 records before extraction is complete.

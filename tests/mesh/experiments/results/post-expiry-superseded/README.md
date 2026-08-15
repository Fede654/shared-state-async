# Superseded — do not cite these five runs

Kept because how the error was made is part of the record, the same
reason `contention-superseded/` is kept.

These produced the claim "the entry goes extinct mesh-wide in 5 of 5
runs, so resurrection does not sustain". External review found four
reasons that conclusion was not established:

1. **Probe failures were recorded as absence.** The sampler caught every
   exception and set the entry to `None`, which the analysis read as
   expiry. A crashed or unreachable daemon would have manufactured
   "extinction" — the headline result. No liveness check ran.
2. **The observer could suppress the phenomenon.** Probes occupied the
   daemon's serial accept loop for a median ~5.2 s every ~8.2 s: a ~64%
   duty cycle. The defence offered at the time — "an empty slice cannot
   insert a key, so probing cannot *cause* resurrection" — had the
   direction backwards. Sustained gossip is what would *prevent*
   extinction, and probing displaces gossip, so heavy observation biases
   toward the reported result. No no-probe control was run.
3. **"Mesh-wide extinction at time X" was never observed simultaneously.**
   Rows span 5–24 s — the longest exceeding the entire 20 s bleach TTL —
   so an all-absent row means each node was absent *somewhere within that
   span*, not that all were absent together.
4. **The scale-transfer argument was unsupported.** `spread/TTL` used the
   *uncorrected* `spread_raw`, reintroducing the row-skew defect these
   experiments exist to correct; the "field range 0.34–0.70" it was
   compared against is itself the five-minute extrapolation labelled
   unsupported everywhere else in this repo; the model's
   interval/TTL ratio (5/20) is twenty times production's (30/2400), and
   gossip opportunities per lifetime may be exactly what decides whether
   the loop sustains; and the "harsh" cell changed bandwidth *and* entry
   count while its spread overlapped the base cell, so its delayed
   extinction cannot be attributed to greater inflation.

Additionally the five runs were **not produced by one script**:
`post-expiry-ttl20-20260815T125547Z.json` carries script hash `71636e…`
from an exploratory version that was never committed, while the other
four carry `f714a5…`. That uncommitted run is also the only one showing
an organic author resurrection. Counting them as n=5 was wrong on
provenance grounds alone.

What survives from these runs: nothing quantitative. The successor
experiment (`../post-expiry/`) re-runs with error-aware sampling,
liveness checks, corrected spread, recorded row spans, a no-probe
control arm, and a single committed script.

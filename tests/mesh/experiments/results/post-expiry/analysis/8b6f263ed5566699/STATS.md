# Post-expiry v4 sweep — planned analyses

Plan status (do not call this preregistered): run schedule committed before collection (`609a79c`); analysis plan specified mid-collection with limited endpoint exposure (`32ab699`, amendment 1); amendment 2 and later corrections were post-outcome.

Analysis `8b6f263ed5566699`, 22 valid runs consumed. Invalid/excluded: none; planned-but-missing: none.
No hypothesis tests were run (amendment §3).

## Q1 — repeatability (96 s cell, 480 s window)

- **Primary (v4): runs with ≥1 resurrection witness: 5/5 (100%; 95% CI 57%–100%)**
- evidence lower bounds per v4 run: [3, 2, 4, 3, 3] (median 3)
- resets per run: [1, 1, 2, 3, 2] (median 2); sampled returns: [2, 1, 2, 0, 1] (median 1)
- historical replication (v3, different acquisition script): 3/3 (100%; 95% CI 44%–100%), bounds [2, 3, 3]
- no pooled v3+v4 estimate exists or will be produced: the acquisition diff changed the propagation gate, which fails the equivalence rule (amendment 2 §5)

## Q2 — opportunity-ratio transfer (EXPLORATORY: confounded twice over)

Cell-specific common horizon (amendment 2 §1): witness upper bound / TTL ≤ 3 lifetimes — H_A = 288 s, H_B = 1218 s.
Confounds: A→B execution order, AND observer dose per lifetime — absolute cadence over a 4.2× longer lifetime gives B ≈4.2× more probes per lifetime (amendment 2 §4). A difference, or its absence, is not attributable to gossip opportunities alone.

- Cell A (96 s, ~19 opportunities/lifetime): 5/5 (100%; 95% CI 57%–100%)
- Cell B (406 s, ~81 ≈ production's 80): 3/3 (100%; 95% CI 44%–100%)
- risk difference A−B (primary): +0.00 (95% Newcombe -0.43 to +0.56)
- sensitivity (ambiguous counted positive): A 5/5, B 3/3, RD +0.00 (-0.43 to +0.56)
- ambiguous-only runs: none
- B evidence bounds per run: [5, 4, 5] (median 5) (within 3L: [5, 4, 5])
- B quiet-from (lifetimes): [1.991, 1.996, 1.997]

## Q3 — late recurrence (96 s cell watched 10 lifetimes)

- runs with a witness definitely after 2 lifetimes: 0/3 (0%; 95% CI 0%–56%) (sensitivity incl. ambiguous: 0/3)
- runs with a witness definitely after 5 lifetimes: 0/3 (0%; 95% CI 0%–56%) (sensitivity incl. ambiguous: 0/3)
- per run (late>2L, amb2L, late>5L, amb5L, quiet-from):
  - post-expiry-ttl90-v4C-rep1-20260815T195034Z.json: 0, 0, 0, 0, 2.188
  - post-expiry-ttl90-v4C-rep2-20260815T200640Z.json: 0, 0, 0, 0, 1.894
  - post-expiry-ttl90-v4C-rep3-20260815T202245Z.json: 0, 0, 0, 0, 2.008

## Controls — resurrection not observable (3 samples)

- v3, TTL 96s, window 480s — absent at end: 3/3 (100%; 95% CI 44%–100%)
- v4, TTL 406s, window 1230s — absent at end: 2/2 (100%; 95% CI 34%–100%)
- v4, TTL 96s, window 480s — absent at end: 3/3 (100%; 95% CI 44%–100%)

## Observer diagnostics (descriptive)

- A v4: valid samples [59, 57, 58, 58, 58] (median 58), gap med [8.17, 8.27, 8.26, 8.17, 8.17] (median 8.17), spread/TTL [0.47, 0.48, 0.41, 0.5, 0.51]
- B v4: valid samples [148, 147, 146] (median 147), gap med [8.25, 8.26, 8.29] (median 8.26), spread/TTL [0.49, 0.5, 0.47]
- C v4: valid samples [116, 114, 117] (median 116), gap med [8.17, 8.23, 8.17] (median 8.17), spread/TTL [0.51, 0.41, 0.51]

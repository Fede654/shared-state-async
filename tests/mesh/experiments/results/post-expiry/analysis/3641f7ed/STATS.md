# Post-expiry v4 sweep — predeclared analyses

Analysis `3641f7ed`, 22 valid runs consumed. Invalid/excluded: none; planned-but-missing: none.
No hypothesis tests were run (amendment §3).

## Q1 — repeatability (96 s cell, 480 s window)

- **Primary (v4): runs with ≥1 resurrection witness: 5/5 (100%; 95% CI 57%–100%)**
- evidence lower bounds per v4 run: [3, 2, 4, 3, 3] (median 3)
- resets per run: [1, 1, 2, 3, 2] (median 2); sampled returns: [2, 1, 2, 0, 1] (median 1)
- historical replication (v3, different acquisition script): 3/3 (100%; 95% CI 44%–100%), bounds [2, 3, 3]
- pooling withheld pending the acquisition-path equivalence audit (amendment §4)

## Q2 — opportunity-ratio transfer (EXPLORATORY: order-confounded)

Common 3-lifetime horizon; witness counted iff its interval's upper bound ≤ 3 lifetimes.

- Cell A (96 s, ~19 opportunities/lifetime): 5/5 (100%; 95% CI 57%–100%)
- Cell B (406 s, ~81 ≈ production's 80): 3/3 (100%; 95% CI 44%–100%)
- risk difference A−B: +0.00 (95% Newcombe -0.43 to +0.56)
- events ambiguous at the horizon: 0
- B evidence bounds per run: [5, 4, 5] (median 5) (within 3L: [5, 4, 5])
- B quiet-from (lifetimes): [1.991, 1.996, 1.997]

## Q3 — late recurrence (96 s cell watched 10 lifetimes)

- runs with a witness definitely after 2 lifetimes: 0/3 (0%; 95% CI 0%–56%)
- runs with a witness definitely after 5 lifetimes: 0/3 (0%; 95% CI 0%–56%)
- per run (late>2L, amb2L, late>5L, amb5L, quiet-from):
  - post-expiry-ttl90-v4C-rep1-20260815T195034Z.json: 0, 0, 0, 0, 2.188
  - post-expiry-ttl90-v4C-rep2-20260815T200640Z.json: 0, 0, 0, 0, 1.894
  - post-expiry-ttl90-v4C-rep3-20260815T202245Z.json: 0, 0, 0, 0, 2.008

## Controls — resurrection not observable (3 samples)

- v3, TTL 96s, window 480s — absent at end: 3/3 (100%; 95% CI 44%–100%)
- v4, TTL 406s, window 1230s — absent at end: 2/2 (100%; 95% CI 34%–100%)
- v4, TTL 96s, window 480s — absent at end: 3/3 (100%; 95% CI 44%–100%)

## Observer diagnostics (descriptive)

- A v4: valid samples [59, 57, 58, 58, 58] (median 58), gap med [8.17, 8.28, 8.26, 8.17, 8.17] (median 8.17), spread/TTL [0.47, 0.48, 0.41, 0.5, 0.51]
- B v4: valid samples [148, 147, 146] (median 147), gap med [8.25, 8.27, 8.29] (median 8.27), spread/TTL [0.49, 0.5, 0.47]
- C v4: valid samples [116, 114, 117] (median 116), gap med [8.17, 8.23, 8.17] (median 8.17), spread/TTL [0.51, 0.41, 0.51]

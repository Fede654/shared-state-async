# HRM-149 confirmatory results — production-parameter passive collection

Governed by `post-expiry-v2-prereg.md` (4c0aef9) + amendments 1–3. Schedule seed 20260819 (`e88c6933628d4662`), 10 slots, all executed 2026-08-18/19, no slot invalid, none replaced. All runs passive (zero probes, zero host connections); witnesses are wire-derived, outbound-only, from the author's own pcap (decode schema 4). Production registration: interval 30 s, bleach TTL 2400 s, insert TTL 2431 s; window 7293 s = 3 lifetimes.

## Q1 (primary, chain): proportion of valid runs with ≥1 resurrection witness

**6/6** valid chain runs with at least one witness (planned denominator 6; 6 valid). Wilson 95% interval **61%–100%**. Interpreted per the prereg as a conditional repeated-run interval under this fixed lab process — one host, five nodes, synthetic impairment — not a field prevalence estimate.

## Q2 (secondary, descriptive): witnesses vs measured completed exchanges

| run | topology | block | witnesses (absret+ttlinc) | completed connections | connections/lifetime | author outbound slices |
|---|---|---|---|---|---|
| s001 | chain | 1 | 2 (0+2) | 1945 | 648.3 | 486 |
| s002 | chain | 1 | 2 (1+1) | 1943 | 647.7 | 486 |
| s003 | chain | 1 | 2 (0+2) | 1944 | 648.0 | 486 |
| s004 | chord | 1 | 4 (1+3) | 2429 | 809.7 | 486 |
| s005 | ring | 1 | 3 (2+1) | 2429 | 809.7 | 970 |
| s006 | chain | 2 | 2 (1+1) | 1944 | 648.0 | 486 |
| s007 | chain | 2 | 2 (0+2) | 1945 | 648.3 | 486 |
| s008 | chord | 2 | 4 (1+3) | 2430 | 810.0 | 486 |
| s009 | ring | 2 | 3 (1+2) | 2430 | 810.0 | 972 |
| s010 | chain | 2 | 2 (0+2) | 1944 | 648.0 | 486 |

Nominal scheduled opportunities: 2431/30 ≈ 81 per node-pair direction per lifetime. Measured chain rate ≈ 648 completed connections per lifetime mesh-wide (4 edges × 2 directions × ~81), i.e. the mesh completed essentially its nominal schedule; witnesses occurred at full gossip health, not under starvation.

## Q3 (secondary): late recurrence (witnesses after 2 lifetimes)

- No witness later than 2 lifetimes in any run (tail outbound coverage held in all 10). Bounds observed late recurrence in a 3-lifetime window; does not establish self-limitation.

Witness times (lifetimes after publish), per run:
- s001 (chain): [1.01, 1.2]
- s002 (chain): [1.0, 1.2]
- s003 (chain): [1.0, 1.2]
- s004 (chord): [1.0, 1.35, 1.46, 1.49]
- s005 (ring): [1.0, 1.19, 1.23]
- s006 (chain): [1.01, 1.21]
- s007 (chain): [1.01, 1.2]
- s008 (chord): [1.0, 1.35, 1.46, 1.51]
- s009 (ring): [1.0, 1.19, 1.22]
- s010 (chain): [1.0, 1.19]

## Exploratory topologies (no pooling, no confirmatory claim)

- ring: 2/2 runs with witnesses; counts [3, 3].
- chord: 2/2 runs with witnesses; counts [4, 4].

## Validity

All 10 slots: acquisition valid (supervisor clean, capture attached-before/stopped-after, zero kernel drops on every node) AND decode valid (schema 4: no reassembly gaps, no handshake failures, all anomalies teardown-attributed per endpoint copy, wire-confirmed propagation, tail coverage, topology flow-verified). No invalid runs to report; no replacements.

## Not established / scope

One host, one 5-node cell per topology, synthetic impairment (400 ms / 512 kbit), same kernel, staggered monotonic clocks. Field prevalence, wireless contention, larger meshes, and durations beyond 3 lifetimes remain unmeasured. The observer confound is closed by design (zero connections during collection); capture non-invasiveness was accepted by the pre-committed equivalence gate at lab parameters.

## Inputs (sha256/16)
```
derivation script confirmatory_results.py  f60ea82f8de56217
3baa58aaa727f232  pe2-confi-b01-passive-chain-ttl2400-s001.decode.json
7cd4392a8a1b00d2  pe2-confi-b01-passive-chain-ttl2400-s001.json
83b69f79fd6e428a  pe2-confi-b01-passive-chain-ttl2400-s002.decode.json
d1d9adcf47c965cb  pe2-confi-b01-passive-chain-ttl2400-s002.json
b449058ec15ecd78  pe2-confi-b01-passive-chain-ttl2400-s003.decode.json
ef59e32e8bcb922d  pe2-confi-b01-passive-chain-ttl2400-s003.json
b01208c29519639e  pe2-confi-b01-passive-chord-ttl2400-s004.decode.json
0cf42b65dde44a09  pe2-confi-b01-passive-chord-ttl2400-s004.json
0b79869b94f5efd5  pe2-confi-b01-passive-ring-ttl2400-s005.decode.json
1a09cb551f71d598  pe2-confi-b01-passive-ring-ttl2400-s005.json
ddff9ef2dc0209c8  pe2-confi-b02-passive-chain-ttl2400-s006.decode.json
ee522d3ce46c659a  pe2-confi-b02-passive-chain-ttl2400-s006.json
86ba2871fa2a58c2  pe2-confi-b02-passive-chain-ttl2400-s007.decode.json
d64d0618fd3a38a7  pe2-confi-b02-passive-chain-ttl2400-s007.json
08c6e4377f67f1fa  pe2-confi-b02-passive-chain-ttl2400-s010.decode.json
d05f36ba0bd5bd0a  pe2-confi-b02-passive-chain-ttl2400-s010.json
c0c24030d26dd6df  pe2-confi-b02-passive-chord-ttl2400-s008.decode.json
ce05b690d3590ec4  pe2-confi-b02-passive-chord-ttl2400-s008.json
aa5c81be7fbd5021  pe2-confi-b02-passive-ring-ttl2400-s009.decode.json
d45ab90b2e147ad7  pe2-confi-b02-passive-ring-ttl2400-s009.json
```

# Preregistration amendment 1 — decoder repair before any confirmatory run

Amends `post-expiry-v2-prereg.md` (frozen at `4c0aef9`). Timing:
**after the freeze, before any confirmatory collection** — zero
confirmatory records existed when this was written, so no outcome
could have influenced it. Trigger: an external re-audit of the frozen
pipeline found three decoder defects (repair commit `7570e70`).

## What changed and why

1. **Mesh-wide dedup was a no-op** — session identity embedded each
   pcap's capture-local first-packet timestamp, which differs between
   endpoints by the link impairment, so no two copies ever matched and
   the frame table (hence Q2's "completed exchanges per lifetime"
   denominator) was exactly doubled. Identity is now capture-invariant:
   (client ip, client port, server ip, client ISN).
2. **The complete linked entry table** (every payload item of every
   kept frame, FK'd by frame_id) is now emitted, as HRM-146's contract
   required; the frozen decoder emitted a target-key-only view drawn
   from undeduplicated per-node data.
3. **Handshake semantics were spec-violating and validity was not
   fail-closed** — msg3 (platform-byte-order RTT echo; spec §3:
   "implementations MUST NOT validate msg3") was validated, marking
   every daemon-initiated session a handshake failure, while
   `decode_valid` ignored handshake/decode/ack/payload counters
   entirely. Now: msgs 1–2 only; every decoder diagnostic feeds
   `decode_valid`; the single tolerated anomaly class is
   teardown-attributable truncation (session's last packet within one
   gossip interval of the recorded daemon stop).

Driver deltas in the same commit: `--decode` (post-collection batch
decoding) exists as documented; resume surfaces
`skipped-existing-INVALID` instead of quiet success (still never
re-runs); records carry a `driver` block (schedule kind/file/seed/
block) so confirmatory membership is machine-checkable.

## Effect on frozen quantities

- **Witness definitions, endpoints, schedule, arms, binary and
  topology decisions: UNCHANGED.** The schedule file and its hash are
  untouched (`e88c6933628d4662`).
- **Witness values in all pilots: UNCHANGED** (validation 4, smoke 5,
  shakedown 4/4/3) — witnesses were always derived from the author's
  own pcap and never passed through the broken dedup.
- **Q2 denominators: corrected** (e.g. smoke run 381 connections /
  762 frames, previously reported as 1524 frames). The audit's
  independent transport-level count is reproduced exactly.
- Parity re-passes on re-derivation: 3/3 matched, 0 contradictions,
  0 missing-opportunity.

## Updated pinned stage hashes (supersede §6 of the prereg)

```
post_expiry_v2.py     a8c3de13b9a481b6   (was 80016edafcfd34b8)
wirecap.py            80c326bce9034fe8   (unchanged)
wiredecode.py         ce6d108481141a7e   (was beb39b0267170f0a)
post_expiry_batch2.py c5af0822053e47e8   (was c6a48c1fdb268b9d)
parity_check.py       3ceb47c66b9f9d71   (unchanged)
schedule json         e88c6933628d4662   (unchanged)
```

Governing commits now additionally include `7570e70` (repair) and
this amendment's own commit. decode_schema is 2; any `.decode.json`
bearing schema 1 is superseded and must not be cited.

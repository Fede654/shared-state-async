# Preregistration amendment 2 — decoder edge cases closed before launch

Amends `post-expiry-v2-prereg.md` (frozen `4c0aef9`) and amendment 1
(`42ba660`). Timing: **post-freeze, pre-confirmatory** — zero
confirmatory records existed. Trigger: a second external re-audit of
the repaired pipeline (repair commit: `9c4ef11`).

## What changed and why

1. **Frame-less failures now reach the validity gate.** The anomaly
   scan iterated emitted frames, so a connection dying before any
   frame decoded was counted in per-node diagnostics and then never
   consumed — the gate simply omitted it. Sessions (including
   zero-frame ones) are now a first-class deduplicated table and the
   gate judges them directly: handshake failures never tolerable;
   decode/completeness anomalies only when teardown-attributable.
2. **TCP incarnations split at ingestion.** Grouping was by client
   four-tuple with the ISN stamped afterwards, so four-tuple reuse
   within a run merged two connections into one identity bearing the
   last SYN's ISN and the first incarnation's bytes — losing completed
   exchanges without tripping validity. A client SYN with a new
   sequence number now finalizes the current incarnation; identity is
   (client ip:port → server ip # ISN) from the first packet. This was
   not hypothetical: the chain shakedown recovers 2 connections and 4
   frames under the fix (author qualifying slices 109 → 111).
3. **Resume and `--decode` report combined validity** (acquisition AND
   wire): an acquisition-valid record with `decode_valid=false` now
   resumes as `skipped-existing-INVALID`, and `decode_batch`
   distinguishes `acquisition-invalid` / `decode-invalid` / valid.

`selftest_wiredecode.py` (new; runnable as `wiredecode.py selftest`)
reproduces both audited edge cases synthetically so they cannot
regress silently.

## Effect on frozen quantities

- Witness definitions, endpoints, schedule (`e88c6933628d4662`), arms,
  binary and topology decisions: **UNCHANGED**.
- Witness values in all pilots: **UNCHANGED** (validation 4, smoke 5,
  shakedown 4/4/3).
- Corrected mesh-wide counts: smoke 383 connections (2 frame-less,
  both teardown-attributed) / 762 frames — the audit's independent
  raw SYN/ISN census (383) is reproduced exactly. Chain shakedown:
  384 connections / 764 frames (2 connections recovered from
  incarnation merging).
- Parity re-passes: 3/3 matched, 0 contradictions.
- decode_schema is now **3**; schema-1 and schema-2 outputs are
  superseded and must not be cited.

## Updated pinned stage hashes (supersede amendment 1)

```
post_expiry_v2.py       a8c3de13b9a481b6   (unchanged from am.1)
wirecap.py              80c326bce9034fe8   (unchanged)
wiredecode.py           71e9c38499506a92   (was ce6d108481141a7e)
post_expiry_batch2.py   c7b3d56b7041b8d9   (was c5af0822053e47e8)
parity_check.py         3ceb47c66b9f9d71   (unchanged)
selftest_wiredecode.py  d80dff7a36dda588   (new)
schedule json           e88c6933628d4662   (unchanged)
```

Governing commits now additionally include `9c4ef11` and this
amendment's own commit.

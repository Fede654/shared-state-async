# Preregistration amendment 3 — endpoint-copy fail-open closed

Amends `post-expiry-v2-prereg.md` (`4c0aef9`), amendment 1 (`42ba660`),
amendment 2 (post-`9c4ef11`). Timing: **post-freeze, pre-confirmatory**
— zero confirmatory records existed. Trigger: third external re-audit
(repair commit `3573b89`).

## What changed and why

Session deduplication kept one endpoint copy and the validity gate
examined only that row, silently discarding the other endpoint's
diagnostics — a mid-run failure seen only by the second endpoint could
vanish behind a clean selected copy. The gate now judges **every
endpoint copy of every session and every frame**: a handshake failure
in any copy invalidates unconditionally; every copy's
decode/incomplete/ack/payload anomaly is independently
teardown-attributed against its own last packet time. Deduplication
survives only for counting (the copy with the most decoded frames),
and health disagreements between endpoint copies are surfaced in a new
`endpoint_disagreements` field. `selftest_wiredecode.py` gains the
two-endpoint disagreement regression case.

## Effect on frozen quantities

- Witness definitions, endpoints, schedule (`e88c6933628d4662`), arms,
  binary and topology decisions: **UNCHANGED**.
- All pilot counts identical to schema 3; witness values unchanged
  (validation 4, smoke 5, shakedown 4/4/3); parity 3/3, 0
  contradictions.
- The endpoint disagreements present in the pilots (validation 1,
  ring 2) are teardown-local; every copy attributes cleanly, so all
  five pilots remain decode-valid.
- decode_schema is now **4**; schema ≤3 outputs are superseded and
  must not be cited.

## Updated pinned stage hashes (supersede amendment 2)

```
post_expiry_v2.py       a8c3de13b9a481b6   (unchanged)
wirecap.py              80c326bce9034fe8   (unchanged)
wiredecode.py           0be1f355d03ef940   (was 71e9c38499506a92)
post_expiry_batch2.py   c7b3d56b7041b8d9   (unchanged)
parity_check.py         3ceb47c66b9f9d71   (unchanged)
selftest_wiredecode.py  8d4313fed8ffd259   (was d80dff7a36dda588)
schedule json           e88c6933628d4662   (unchanged)
```

Governing commits now additionally include `3573b89` and this
amendment's own commit.

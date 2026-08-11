# TTL divergence sweep

Field reference (MonteNet, G10h4ck): 5 nodes, 30 s interval,
2400 s TTL, long-distance radio — **22-27 s spread**, author
holding the lowest TTL for its own key.

`spread` is the largest TTL disagreement observed between
nodes for the same entry. `author-lowest` counts samples in
which the author held the minimum TTL for its own key.
`ill` counts "is remote peer ill?" warnings.

| config | nodes | interval | directed | stagger | link | spread | propagation | author-lowest | ill |
|---|---|---|---|---|---|---|---|---|---|
| baseline-3x5 | 3 | 5s | no | yes | 40ms | **4s** | 5.0s | 4/5 | 17 |
| bulk-5x30-256kbit | 5 | 30s | no | yes | 100ms | **112s** | 36.4s | 4/5 | 3263 |
| chain-5x30-nostagger | 5 | 30s | no | no | 40ms | **2s** | 47.0s | 4/5 | 4 |
| chain-5x30 | 5 | 30s | no | yes | 40ms | **3s** | 42.0s | 3/5 | 3 |
| directed-5x15 | 5 | 15s | yes | yes | 40ms | **5s** | 46.0s | 3/5 | 5 |
| directed-5x30 | 5 | 30s | yes | yes | 40ms | **3s** | 81.0s | 2/5 | 4 |

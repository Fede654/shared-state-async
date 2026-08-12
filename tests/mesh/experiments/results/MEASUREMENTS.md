# Baseline measurements

Binary: `/home/fede/REPOS/shared-state-async/build/shared-state-async`

Idle CPU: **0.03%** over 30s with no peers and no state (config is re-parsed twice a second — audit F4).

| entries | bytes/sync | bytes/entry | sync time | daemon RSS |
|---|---|---|---|---|
| 0 | 81 | - | 0.090s | 3896 kB |
| 25 | 11,700 | 466 | 0.042s | 3896 kB |
| 100 | 46,575 | 465 | 0.043s | 3896 kB |
| 250 | 116,475 | 466 | 0.047s | 3896 kB |
| 500 | 232,975 | 466 | 0.045s | 3896 kB |

At 500 entries a single sync moves 232,975 bytes. Every neighbour pays that, every update interval, whether or not anything changed — there is no delta or digest path (critique 1.1). On a shared radio that cost is airtime taken from user traffic, and it is also what drives TTL divergence, since divergence tracks transfer duration.

RSS caveat: resident memory did not move measurably across these state sizes, so treat the column as *not yet measured* rather than as evidence of flat memory use. A few hundred KB of state fits inside an allocator arena the daemon has already grown; showing the real curve needs state large enough to force new pages, or heap instrumentation.

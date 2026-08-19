# HRM-152 results — rolling upgrade (T23 dynamics) and reboot storm (T11 dynamics)

EXPLORATORY CHARACTERIZATION: descriptive, single run each, no
preregistration, no confirmatory statistics. Both runs passive (zero
probes); observation = per-netns packet capture, knowledge read from
each node's OUTBOUND completed slices only. Records + `.fleet.json`
analysis artifacts in this directory; both runs acquisition-valid and
decode-valid with zero unattributed anomalies (anomaly classes:
teardown / declared disruption windows / mixed-version interop).
Binaries: v1 master `b5b3de0a…`, `merge_with_version` @ `22a20aab`
(`45e19741…`). Chain jime–balcon–tronco–e-bob–nodo-suri, interval 5,
bleach TTL 90 (insert 96 s), 400 ms / 512 kbit impairment, every node
republishing its own key every 30 s (25 s in the storm).

## A. Rolling upgrade (`rolling-upgrade-20260819T160544Z`)

All-v1 warmup 150 s, then one node upgraded every 90 s from the far
end (nodo-suri → e-bob → tronco → balcon → jime), state wiped each
time (reboot semantics), 180 s all-v2 tail.

Authors carried per node (30 s buckets; upgrades at t≈150, 240, 331,
421, 511):

```
bucket_s  balcon   e-bob    jime  nodo-s  tronco
     120       5       5       5       5       5   all-v1: full coverage
     150       5       5       5       0       5   nodo-suri upgraded
     210       5       5       5       1       5    → own key only
     240       5       2       5       2       5   e-bob upgraded
     300       5       2       5       2       5    → v2 island of 2 sees 2
     330       5       3       5       3       3   tronco upgraded → island of 3
     420       4       3       5       3       3   balcon upgraded
     480       4       4       5       4       4    → island of 4 sees 4
     510       5       5       5       5       5   jime upgraded → HEALED
     750       5       5       5       5       5   all-v2 tail: stays full
```

Readings, each matching a T23 prediction:

1. **An upgraded node's knowledge collapses to its v2 island.** Every
   upgraded node carries exactly (island size) authors: 1, then 2, 3,
   4 as adjacent nodes join it. It relearns nothing authored by, or
   relayed through, v1.
2. **The asymmetry is total.** The remaining v1 nodes (jime through
   its whole tenure; balcon until t≈421) carry **5 authors
   throughout** — v1 keeps accepting v2 entries while v2 discards v1
   slices.
3. **Full coverage returns only at 100 % upgrade** (t≈511), and
   immediately — within one bucket of the last upgrade.
4. **T23 is wire-silent in steady state.** The interop anomaly class
   caught only 12 transient sessions, all AT the upgrade instants
   (nodes mid-restart). In steady mixed operation every v1→v2 session
   completes and acknowledges normally at the wire level — the drop
   happens inside deserialization, after a successful-looking
   exchange. Nothing on the network says anything is wrong; only the
   knowledge map does. This is the operational hazard: a
   half-upgraded mesh looks healthy from every dashboard that watches
   traffic.

## B. Reboot storm (`reboot-storm-20260819T161850Z`)

All-v2 fleet; the middle node (tronco) publishes a fresh generation
every 25 s and is stop/wipe/restarted four times.

Per cycle — last gen published before reboot vs the first own-key
slice tronco SENT after restart:

| cycle | reboot t | gen before | first own out (t, gen, version) | stale? |
|---|---|---|---|---|
| 1 | 120.2 | 6  | t=126.5, gen **5**, v5   | **regressed** |
| 2 | 195.5 | 8  | t=201.5, gen 8, v7       | latest |
| 3 | 270.7 | 11 | t=276.7, gen **10**, v9  | **regressed** |
| 4 | 345.9 | 13 | t=352.0, gen 13, v11     | latest |

1. **Every reboot relearns own-authored state from echoes** — within
   ~6 s the wiped node is again advertising its own key it never
   republished. The daemon cannot distinguish this from legitimate
   recovery; that is the T11/T24 tension by design.
2. **Half the cycles resurrected a stale generation** (5 for 6, 10
   for 11): whichever echo arrived first wins, and under staggered
   clocks that is a coin flip between the newest and the
   previous generation.
3. **Stale generations propagate with authority before repair**: 84
   neighbour outbound slices carried a regressed generation of
   tronco's key between a reboot and the next publish. The
   next 25 s republish repairs it — but a node that publishes rarely
   (real hostname/config data) would stay regressed for its whole
   publish period, mesh-wide.
4. The deterministic mechanism (recovery clause promoting the
   re-adopted entry above offered versions) is `t11`'s domain; this
   run shows its field shape: reboot → echo re-adoption →
   possibly-stale data at top effective authority → window until next
   own publish.

## Scope

One host, one 5-node chain per run, one run per experiment, short-TTL
cell (interval 5 / bleach 90), synthetic impairment. These runs
characterize dynamics; they estimate no rates. The v2r guard
(recovery only for locally-inserted-since-boot entries) is expected
to close the storm's regression path and half of the upgrade
discussion; measuring a fixed binary here would be the natural
follow-up once one exists.

# Verdict history

One row per recorded run. A cell flipping RED to GREEN is a
fix landing; GREEN going RED is a regression. `.` means the
test was not selected in that run.

| run (UTC) | binary | T0 | T1 | T2 | T3 | T4 | T5 | T6 | T7 | T8 | T9 | T10 | T11 | T13 | T14 | T15 | T16 | T17 | T18 | T19 | T20 | T21 | T22 | T23 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 20260812T192950Z | shared-state-async | G | R | G | G | R | R | R | R | R | R | R | R | G | R | G | G | R | G | R | R | R | R | G |
| 20260812T193917Z | ss-jbrk | G | G | G | G | R | R | R | R | R | R | R | R | G | R | G | G | R | G | R | R | R | R | R |

G = green, R = red, E = harness error.

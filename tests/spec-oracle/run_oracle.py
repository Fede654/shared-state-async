#!/usr/bin/env python3
"""Spec oracle runner — merge-semantics property tests over the model.

  python3 run_oracle.py            # full run, exit != 0 on red
  python3 run_oracle.py --quick    # fewer seeds

This is NOT the test suite for shared-state. It is the executable
counterpart of doc/protocol-spec-DRAFT.md §6/§8: the place where "what
should merge do?" is written down in runnable form. The strict failure
conditions asserted by the real-binary harness (tests/mesh/) are derived
from it. See README.md.
"""

import sys
import time

import properties


def run(quick=False):
    t0 = time.time()
    if quick:
        properties.N_SEEDS = 40

    lines = ["# Spec-oracle report", "",
             "Model-level merge properties. Characterization polarity:",
             "'defect reproduces' entries PASS when the documented defect",
             "is still present in the modeled semantics.", "",
             "| property | strategy | expectation | result |",
             "|---|---|---|---|"]
    failures = 0
    for r in properties.run_properties():
        expected = "holds" if r["expected"] else "defect reproduces"
        status = "PASS" if r["ok"] else f"FAIL {r['failures'][:2]}"
        if not r["ok"]:
            failures += 1
        lines.append(
            f"| {r['property']} | {r['strategy']} | {expected} | {status} |")

    lines += ["", f"- failures: **{failures}**",
              f"- wall time: {time.time() - t0:.1f}s",
              f"- oracle consistent: **{'YES' if failures == 0 else 'NO'}**", ""]
    report = "\n".join(lines)
    print(report)
    return failures


if __name__ == "__main__":
    sys.exit(1 if run(quick="--quick" in sys.argv) else 0)

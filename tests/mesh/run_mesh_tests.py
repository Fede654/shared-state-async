#!/usr/bin/env python3
"""Real-binary mesh test runner.

  python3 run_mesh_tests.py [--bin PATH] [T7 T8 ...]

Re-execs itself inside an unprivileged user/mount/net/uts namespace,
builds the topology, and runs each test against the real binary.

Exit status is NOT a simple pass/fail: tests carry EXPECT_TODAY, so the
runner reports both what happened and whether it matched expectation.
Exit 0 means "every test's outcome matched its documented expectation" —
i.e. the known-red tests are still red for the documented reason, and
any test marked GREEN is passing. When a fix lands, flip its
EXPECT_TODAY to GREEN in the same commit.
"""

import argparse
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness import ensure_inner, Mesh, DEFAULT_BIN  # noqa: E402

import t00_harness_smoke  # noqa: E402
import t07_register_bootstrap  # noqa: E402
import t08_hook_fd_leak  # noqa: E402

TESTS = [t00_harness_smoke, t07_register_bootstrap, t08_hook_fd_leak]
RUNDIR = "/tmp/ss-mesh-run"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bin", default=DEFAULT_BIN)
    ap.add_argument("--keep", action="store_true", help="keep run dir")
    ap.add_argument("only", nargs="*", help="test IDs, e.g. T7")
    args = ap.parse_args()

    if not os.path.exists(args.bin):
        print(f"binary not found: {args.bin}\n"
              f"build it with: cmake -DCMAKE_BUILD_TYPE=Release "
              f"-DSS_CPPTRACE_STACKTRACE=OFF .. && make")
        return 2

    ensure_inner()  # everything below runs inside the namespace

    selected = [t for t in TESTS if not args.only or t.ID in args.only]
    if not args.keep:
        shutil.rmtree(RUNDIR, ignore_errors=True)

    results = []
    for test in selected:
        # one fresh mesh per test: no cross-test contamination
        rundir = os.path.join(RUNDIR, test.ID)
        try:
            with Mesh(["lime-a", "lime-b", "lime-c"], rundir,
                      binary=args.bin) as mesh:
                passed, detail = test.run(mesh)
            verdict = "GREEN" if passed else "RED"
        except Exception as e:
            # A broken harness must NEVER be reported as a test verdict:
            # a spurious RED that "matches expectation" manufactures
            # exactly the false confidence this suite exists to prevent.
            verdict = "ERROR"
            detail = f"{type(e).__name__}: {e}".replace("\n", " | ")[:400]
        as_expected = (verdict == test.EXPECT_TODAY)
        results.append((test, verdict, as_expected, detail))

    width = max(len(t.TITLE) for t, _, _, _ in results) + 2
    print()
    print(f"{'ID':<4} {'TITLE':<{width}} {'EXPECTED':<9} {'ACTUAL':<7} MATCH")
    print("-" * (4 + width + 9 + 7 + 6))
    for test, verdict, ok, detail in results:
        print(f"{test.ID:<4} {test.TITLE:<{width}} "
              f"{test.EXPECT_TODAY:<9} {verdict:<7} {'yes' if ok else 'NO'}")
        print(f"     -> {detail}")
    print()

    errors = [t.ID for t, v, _, _ in results if v == "ERROR"]
    mismatches = [t.ID for t, v, ok, _ in results if not ok and v != "ERROR"]
    reds = [t.ID for t, v, _, _ in results if v == "RED"]
    print(f"known-red (defects confirmed present): {', '.join(reds) or 'none'}")
    if errors:
        print(f"HARNESS ERRORS (no verdict produced): {', '.join(errors)}")
    if mismatches:
        print(f"UNEXPECTED outcomes: {', '.join(mismatches)}")
    if errors or mismatches:
        return 1
    print("all outcomes matched expectations")
    return 0


if __name__ == "__main__":
    sys.exit(main())

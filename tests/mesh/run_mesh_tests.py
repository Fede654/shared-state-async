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
import datetime
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness import ensure_inner, Mesh, DEFAULT_BIN  # noqa: E402

import t00_harness_smoke  # noqa: E402
import t01_no_stale_echo_regression  # noqa: E402
import t02_author_supremacy  # noqa: E402
import t03_convergence_under_loss  # noqa: E402
import t04_wedged_peer  # noqa: E402
import t05_concurrent_peers  # noqa: E402
import t06_expiry_fires_hooks  # noqa: E402
import t07_register_bootstrap  # noqa: E402
import t08_hook_fd_leak  # noqa: E402
import t09_config_rewrite_race  # noqa: E402
import t10_discovery_bad_line  # noqa: E402
import t11_reboot_no_stale_adoption  # noqa: E402
import t13_discover_never_hangs  # noqa: E402
import t14_publish_rounds_under_load  # noqa: E402
import t15_resource_exhaustion  # noqa: E402
import t16_bandwidth_math_crash  # noqa: E402
import t17_discover_failure_visible  # noqa: E402
import t18_truncated_transfer  # noqa: E402
import t19_stats_file_tearing  # noqa: E402
import t20_unauthenticated_injection  # noqa: E402
import t21_malformed_frames  # noqa: E402
import t22_ttl_divergence_field  # noqa: E402

TESTS = [t00_harness_smoke,
         t01_no_stale_echo_regression, t02_author_supremacy,
         t03_convergence_under_loss, t04_wedged_peer,
         t05_concurrent_peers, t06_expiry_fires_hooks,
         t07_register_bootstrap, t08_hook_fd_leak,
         t09_config_rewrite_race, t10_discovery_bad_line,
         t11_reboot_no_stale_adoption, t13_discover_never_hangs,
         t14_publish_rounds_under_load, t15_resource_exhaustion,
         t16_bandwidth_math_crash,
         t17_discover_failure_visible, t18_truncated_transfer,
         t19_stats_file_tearing, t20_unauthenticated_injection,
         t21_malformed_frames, t22_ttl_divergence_field]
RUNDIR = "/tmp/ss-mesh-run"
RESULTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bin", default=DEFAULT_BIN)
    ap.add_argument("--keep", action="store_true", help="keep run dir")
    ap.add_argument("--json", action="store_true",
                    help="record this run to results/run-<UTC>.json and "
                         "rebuild results/HISTORY.md from every recorded run")
    ap.add_argument("--json-path", default=None, metavar="PATH",
                    help="write the run record here instead")
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
        # tests may ask for a bigger mesh; three is enough for most
        names = getattr(test, "NODES", ["lime-a", "lime-b", "lime-c"])
        try:
            with Mesh(names, rundir, binary=args.bin) as mesh:
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

    if args.json or args.json_path:
        _record(results, args)
    print(f"known-red (defects confirmed present): {', '.join(reds) or 'none'}")

    baseline = os.path.realpath(args.bin) == os.path.realpath(DEFAULT_BIN)
    if not baseline:
        print(f"\nNOTE: EXPECT_TODAY is calibrated for the default build "
              f"({DEFAULT_BIN}).\nYou ran a different binary, so a MATCH of "
              f"'NO' below is a behavioural DIFFERENCE in the binary under\n"
              f"test — which is the point of running it. Only ERROR rows "
              f"indicate a broken harness.")
        if mismatches:
            print(f"differences vs baseline expectations: "
                  f"{', '.join(mismatches)}")
        return 1 if errors else 0
    if errors:
        print(f"HARNESS ERRORS (no verdict produced): {', '.join(errors)}")
    if mismatches:
        print(f"UNEXPECTED outcomes: {', '.join(mismatches)}")
    if errors or mismatches:
        return 1
    print("all outcomes matched expectations")
    return 0


def _record(results, args):
    """Persist this run and rebuild the cross-run verdict matrix.

    The matrix is the point: it shows a test flipping RED to GREEN when a
    fix lands, and flags a GREEN silently going RED again."""
    os.makedirs(RESULTS, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ")
    run = {
        "timestamp": stamp,
        "binary": os.path.realpath(args.bin),
        "tests": {t.ID: {"title": t.TITLE, "expected": t.EXPECT_TODAY,
                         "actual": v, "as_expected": ok, "detail": d}
                  for t, v, ok, d in results},
    }
    path = args.json_path or os.path.join(RESULTS, f"run-{stamp}.json")
    with open(path, "w") as f:
        json.dump(run, f, indent=1, sort_keys=True)

    runs = []
    for fn in sorted(os.listdir(RESULTS)):
        if fn.startswith("run-") and fn.endswith(".json"):
            with open(os.path.join(RESULTS, fn)) as f:
                runs.append(json.load(f))
    if not runs:
        return
    ids = sorted({i for r in runs for i in r["tests"]},
                 key=lambda x: int(x[1:]))
    lines = ["# Verdict history", "",
             "One row per recorded run. A cell flipping RED to GREEN is a",
             "fix landing; GREEN going RED is a regression. `.` means the",
             "test was not selected in that run.", "",
             "| run (UTC) | binary | " + " | ".join(ids) + " |",
             "|---|---|" + "---|" * len(ids)]
    for r in runs:
        cells = []
        for i in ids:
            t = r["tests"].get(i)
            cells.append("." if not t else
                         {"GREEN": "G", "RED": "R", "ERROR": "E"}[t["actual"]])
        lines.append(f"| {r['timestamp']} | {os.path.basename(os.path.dirname(os.path.dirname(r['binary'])))} | "
                     + " | ".join(cells) + " |")
    lines += ["", "G = green, R = red, E = harness error.", ""]
    with open(os.path.join(RESULTS, "HISTORY.md"), "w") as f:
        f.write("\n".join(lines))
    print(f"recorded: {path}")


if __name__ == "__main__":
    sys.exit(main())

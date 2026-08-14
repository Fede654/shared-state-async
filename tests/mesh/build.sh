#!/bin/sh
# Build the binary under test AND stamp its provenance, in one command.
#
# Why this exists rather than a note in the README saying "remember to
# stamp": a stamp written by hand proves only that the binary has not
# changed since stamping. It does NOT establish that the source recorded
# in it produced that binary — nothing stops someone stamping a
# months-old artifact against today's checkout, which is precisely the
# false attribution the provenance module exists to prevent. Coupling
# the two into a single command is what makes the claim true, and the
# stamp records `coupled_to_build` so a reader can tell which kind of
# claim they are looking at.
#
#   tests/mesh/build.sh                 # Release, upstream defaults
#   tests/mesh/build.sh --build-type Debug
#
# Note SS_TESTS is left OFF: upstream's doctest suite does not build on
# master (audit D6 — doctest is never fetched), and it has no bearing on
# the daemon under test.
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(git -C "$HERE" rev-parse --show-toplevel)
BUILD_TYPE=Release

while [ $# -gt 0 ]; do
    case "$1" in
        --build-type) BUILD_TYPE=$2; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

echo "==> configuring ($BUILD_TYPE)"
cmake -S "$REPO" -B "$REPO/build" \
      -DCMAKE_BUILD_TYPE="$BUILD_TYPE" \
      -DSS_CPPTRACE_STACKTRACE=OFF

echo "==> building"
cmake --build "$REPO/build" -j

echo "==> stamping provenance (coupled to this build)"
python3 "$HERE/experiments/provenance.py" stamp \
        "$REPO/build/shared-state-async" --coupled

echo "==> done: $REPO/build/shared-state-async"

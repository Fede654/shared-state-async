#!/bin/sh
# Build the binary under test AND stamp its provenance, in one command.
#
# Why this exists rather than a note saying "remember to stamp": a stamp
# written by hand proves only that the binary has not changed since
# stamping. It does NOT establish that the source recorded in it produced
# that binary.
#
# Why it forces a relink: the first version of this script ran an
# incremental `cmake --build` and stamped unconditionally. On an
# already-current tree CMake did nothing, and the script promoted an
# hour-old binary to "build-coupled" while recording the CURRENT
# checkout as its source — a different commit from the one that actually
# built it. That is precisely the false attribution the provenance
# module exists to prevent, produced by the tool meant to prevent it.
#
# So the binary is deleted first, the build is bracketed by timestamps,
# and provenance.py refuses the "build-coupled" label unless the
# resulting file's mtime lands inside that window with the source tree
# unchanged across it. The claim is verified, not asserted.
#
#   tests/mesh/build.sh                 # Release, upstream defaults
#   tests/mesh/build.sh --build-type Debug
#   tests/mesh/build.sh --build-dir /tmp/ss-build
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(git -C "$HERE" rev-parse --show-toplevel)
BUILD_TYPE=Release
BUILD_DIR="$REPO/build"

while [ $# -gt 0 ]; do
    case "$1" in
        --build-type) BUILD_TYPE=$2; shift 2 ;;
        --build-dir)  BUILD_DIR=$2; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

BIN="$BUILD_DIR/shared-state-async"

# SS_TESTS is set EXPLICITLY, not left to the cache: a stale cache can
# carry a previous ON, and upstream's doctest suite does not build on
# master (audit D6 — doctest is never fetched), which would fail the
# build for reasons unrelated to the daemon under test.
echo "==> configuring ($BUILD_TYPE, SS_TESTS=OFF) in $BUILD_DIR"
cmake -S "$REPO" -B "$BUILD_DIR" \
      -DCMAKE_BUILD_TYPE="$BUILD_TYPE" \
      -DSS_CPPTRACE_STACKTRACE=OFF \
      -DSS_TESTS=OFF

# Force the link step to actually run, so "was this file produced by
# this build?" has a real answer rather than depending on whether CMake
# felt anything was out of date.
rm -f "$BIN"

STARTED=$(date +%s)
echo "==> building (forced relink)"
cmake --build "$BUILD_DIR" -j
FINISHED=$(date +%s)

[ -x "$BIN" ] || { echo "build produced no executable at $BIN" >&2; exit 1; }

echo "==> stamping provenance (coupling verified, not asserted)"
python3 "$HERE/experiments/provenance.py" stamp "$BIN" \
        --coupled --started "$STARTED" --finished "$FINISHED" \
        --require-coupled

echo "==> done: $BIN"

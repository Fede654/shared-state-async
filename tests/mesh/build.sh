#!/bin/sh
# Build the binary under test AND stamp its provenance, in one command.
#
# This script is a THIN ENTRY POINT and must stay one. All of the work —
# fingerprinting, configuring, forcing the relink, timing, stamping —
# happens inside a single Python process in experiments/provenance.py.
#
# It used to do that work here, and pass the results to a `stamp`
# subcommand across a process boundary. That boundary was the hole:
# every fact separating a real build from a copied file arrived as a
# command-line string, so external review copied the untouched binary,
# computed the genuine source fingerprint (read-only public state
# anyone can obtain), passed the copy's own mtime as the build window,
# and got coupled_to_build=True with the right commit and a clean tree.
# No build had occurred.
#
# So there is nothing left to pass. `provenance.py build` observes the
# build it describes, and no CLI path accepts precomputed coupling
# evidence any more. Read the result as workflow verification — it
# catches stale binaries, no-op incremental builds and moved checkouts —
# not as tamper-proof provenance, which would need external signed
# attestation.
#
#   tests/mesh/build.sh                 # Release, upstream defaults
#   tests/mesh/build.sh --build-type Debug
#   tests/mesh/build.sh --build-dir /tmp/ss-build
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(git -C "$HERE" rev-parse --show-toplevel)

exec python3 "$HERE/experiments/provenance.py" build --repo "$REPO" "$@"

#!/usr/bin/env bash
# Serial post-expiry sweep. Three cells, one question each:
#
#   A  standard 96 s cell        - replication power for the 3/3 result
#   B  bleach 400 -> insert 406  - interval/TTL ratio ~81 opportunities
#      per lifetime, matching production's 80 (2400/30). Tests whether
#      the opportunity ratio, not the absolute TTL, governs resurrection.
#   C  standard TTL, 960 s window (10 lifetimes) - does anything return
#      after the ~2-lifetime quiet point seen in the v3 batch?
#
# SERIAL on purpose: concurrent meshes share CPU and the loopback stack,
# and this experiment's subject is timing. One failed run does not stop
# the batch; stats only cite record_valid files.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"

run() {
    local budget=$1; shift
    echo "=== $(date -u +%FT%TZ) post_expiry $*"
    timeout "$budget" python3 "$HERE/post_expiry.py" "$@" \
        || echo "!!! $(date -u +%FT%TZ) run failed (exit $?): $*"
}

for i in 1 2 3 4 5; do run 1500 --tag v4A-rep$i; done
for i in 1 2 3;     do run 1500 --control --tag v4A-rep$i; done

for i in 1 2 3;     do run 2400 --bleach-ttl 400 --window 1230 --tag v4B-rep$i; done
for i in 1 2;       do run 2400 --control --bleach-ttl 400 --window 1230 --tag v4B-rep$i; done

for i in 1 2 3;     do run 2000 --window 960 --tag v4C-rep$i; done

echo "=== $(date -u +%FT%TZ) batch complete"

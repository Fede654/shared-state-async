#!/usr/bin/env python3
"""H7 baselines — quantities, not pass/fail verdicts.

  python3 experiments/measurements.py [--bin PATH] [--sizes 10 50 100 200]

Writes experiments/results/measurements.json and a readable table in
experiments/results/MEASUREMENTS.md.

Why these numbers matter here rather than as general curiosity:

  M1  bytes on the wire per sync, against state size. Every sync ships
      the *entire* state for a data type — no deltas, no digests
      (critique 1.1). This is the scalability wall, and it is also the
      mechanism behind the TTL divergence the sweep found: transfer
      duration is what makes TTLs drift apart, so a number that grows
      with the network makes the merge defect grow with it too.
  M2  idle CPU. The daemon re-parses its config file twice a second and
      rewrites epoll registrations per operation (audit F4, D3), on
      hardware where CPU is a real budget.
  M4  resident memory against state size, with a 1 MiB per-frame cap and
      no authentication in front of it (critique 1.3).

For the `data(t)` / G(t) direction these are the inputs to any claim
about how fresh a distributed topology view can be: staleness is
bounded below by how long a full state takes to move.
"""

import argparse
import json
import os
import socket
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import ensure_inner, Mesh, DEFAULT_BIN  # noqa: E402
import wire  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "results")
TYPE = "probe"
IDLE_SECONDS = 30


def sync_bytes(ip, type_name, timeout=60):
    """One real sync session; returns (bytes_sent, bytes_received, seconds)."""
    ver = struct.pack("!I", wire.WIRE_PROTO_VERSION)
    payload = wire.payload_encode({})
    frame = wire.encode_frame(type_name, payload)
    t0 = time.time()
    with socket.create_connection((ip, wire.TCP_PORT), timeout=timeout) as s:
        sent = 0
        recvd = 0
        s.sendall(ver); sent += 4
        s.recv(4); recvd += 4
        s.sendall(ver); sent += 4
        s.sendall(frame); sent += len(frame)
        s.recv(4); recvd += 4
        hdr = _exact(s, 1); nlen = hdr[0]
        rest = _exact(s, nlen + 4)
        (dlen,) = struct.unpack("!I", rest[nlen:])
        _exact(s, dlen)
        recvd += 1 + nlen + 4 + dlen
        s.sendall(struct.pack("!I", 1 + nlen + 4 + dlen)); sent += 4
    return sent, recvd, time.time() - t0


def _exact(s, n):
    buf = b""
    while len(buf) < n:
        c = s.recv(n - len(buf))
        if not c:
            raise ConnectionError("closed")
        buf += c
    return buf


def _proc_stats(pid):
    """(cpu_seconds, rss_kb) for a pid, or (None, None)."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            parts = f.read().split()
        ticks = int(parts[13]) + int(parts[14])
        cpu = ticks / os.sysconf("SC_CLK_TCK")
        rss = None
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss = int(line.split()[1])
                    break
        return cpu, rss
    except (OSError, IndexError, ValueError):
        return None, None


def _daemon_pid(node):
    """The daemon itself, not the shell that exec'd it: pgrep -f matches
    the wrapper too, and reading the wrapper's RSS silently reports a
    constant."""
    for pid in node.daemon_pids():
        try:
            with open(f"/proc/{pid}/comm") as f:
                # Linux truncates comm to 15 characters
                if f.read().strip().startswith("shared-state"):
                    return pid
        except OSError:
            continue          # pid may vanish between pgrep and read
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bin", default=DEFAULT_BIN)
    ap.add_argument("--sizes", nargs="*", type=int,
                    default=[0, 25, 100, 250, 500])
    args = ap.parse_args()
    if not os.path.exists(args.bin):
        print(f"binary not found: {args.bin}")
        return 2

    ensure_inner()
    os.makedirs(RESULTS, exist_ok=True)
    out = {"binary": os.path.realpath(args.bin), "rows": []}

    with Mesh(["lime-a"], "/tmp/ss-measure", binary=args.bin) as mesh:
        node = mesh.node("lime-a")
        node.clean_state()
        node.seed_config()
        node.set_peers([])
        node.cli(f"register {TYPE} community 30 2400", timeout=30)
        node.start()
        if not node.wait_listening():
            print("daemon did not start")
            return 1
        pid = _daemon_pid(node)

        # M2 — idle cost, measured before any state exists
        c0, _ = _proc_stats(pid)
        time.sleep(IDLE_SECONDS)
        c1, rss0 = _proc_stats(pid)
        idle_cpu = None
        if c0 is not None and c1 is not None:
            idle_cpu = round((c1 - c0) / IDLE_SECONDS * 100, 2)
        out["idle_cpu_percent"] = idle_cpu
        out["idle_window_s"] = IDLE_SECONDS
        out["rss_kb_empty"] = rss0

        # M1 + M4 — wire cost and memory against state size
        added = 0
        for target in sorted(args.sizes):
            while added < target:
                batch = min(50, target - added)
                payload = json.dumps({
                    f"e{added + i}": {"gen": 1, "blob": "x" * 120}
                    for i in range(batch)})
                node.cli(f"insert {TYPE}", stdin=payload, timeout=120)
                added += batch
            sent, recvd, secs = sync_bytes(node.ip, TYPE)
            _, rss = _proc_stats(pid)
            row = {
                "entries": target,
                "bytes_sent": sent,
                "bytes_received": recvd,
                "bytes_per_sync": sent + recvd,
                "seconds": round(secs, 3),
                "bytes_per_entry": (round(recvd / target) if target else None),
                "rss_kb": rss,
            }
            out["rows"].append(row)
            print(f"[measure] {target:>4} entries: "
                  f"{row['bytes_per_sync']:>8} B/sync  "
                  f"{row['seconds']:.3f}s  rss={rss}kB", flush=True)

    with open(os.path.join(RESULTS, "measurements.json"), "w") as f:
        json.dump(out, f, indent=1, sort_keys=True)
    _write_md(out)
    print("\n" + _table(out))
    return 0


def _table(out):
    lines = ["| entries | bytes/sync | bytes/entry | sync time | daemon RSS |",
             "|---|---|---|---|---|"]
    for r in out["rows"]:
        lines.append(
            f"| {r['entries']} | {r['bytes_per_sync']:,} | "
            f"{r['bytes_per_entry'] or '-'} | {r['seconds']:.3f}s | "
            f"{r['rss_kb']} kB |")
    return "\n".join(lines)


def _write_md(out):
    rows = out["rows"]
    note = ""
    if len(rows) >= 2 and rows[0]["entries"] == 0:
        big = rows[-1]
        note = (f"\nAt {big['entries']} entries a single sync moves "
                f"{big['bytes_per_sync']:,} bytes. Every neighbour pays that, "
                f"every update interval, whether or not anything changed — "
                f"there is no delta or digest path (critique 1.1). On a "
                f"shared radio that cost is airtime taken from user traffic, "
                f"and it is also what drives TTL divergence, since divergence "
                f"tracks transfer duration.\n"
                f"\nRSS caveat: resident memory did not move measurably "
                f"across these state sizes, so treat the column as *not yet "
                f"measured* rather than as evidence of flat memory use. A "
                f"few hundred KB of state fits inside an allocator arena the "
                f"daemon has already grown; showing the real curve needs "
                f"state large enough to force new pages, or heap "
                f"instrumentation.\n")
    with open(os.path.join(RESULTS, "MEASUREMENTS.md"), "w") as f:
        f.write("# Baseline measurements\n\n"
                f"Binary: `{out['binary']}`\n\n"
                f"Idle CPU: **{out['idle_cpu_percent']}%** over "
                f"{out['idle_window_s']}s with no peers and no state "
                f"(config is re-parsed twice a second — audit F4).\n\n"
                + _table(out) + "\n" + note)


if __name__ == "__main__":
    sys.exit(main())

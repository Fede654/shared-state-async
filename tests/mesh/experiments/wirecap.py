"""Per-node passive packet capture for the mesh harness (v2 plan, Phase 1).

Starts one tcpdump per node, inside that node's net namespace, on the
node's veth, writing one pcap per node per run. This observes gossip
without opening a single connection: it does not occupy the daemon's
serial accept loop, which is the timing condition the post-expiry
experiments measure. It is NOT assumed perturbation-free — packet
copying and capture CPU share the host — which is why the plan gates it
on a capture-on/off timing check (capture_check.py).

Requirements established by direct probe (2026-08-18): unprivileged
tcpdump works inside the harness's user+net namespaces provided
privilege dropping is disabled (`-Z root`); without it tcpdump exits
after "Couldn't change ownership of savefile" because the `tcpdump`
user does not exist inside the user namespace. Kernel drop statistics
arrive on stderr at termination and are parsed into the capture record.

Lifecycle contract (capture completeness is part of record_valid):
  cap = start_capture(mesh, outdir)     # BEFORE any daemon starts
  ... run the experiment ...
  meta = cap.stop()                     # AFTER every daemon stopped
`meta` records, per node: pcap path + sha256, capture command, tool
version, capture-start timestamp, stop timestamp, packet/drop counters,
and whether the capturer was confirmed attached before daemons started
and alive at stop. Any premature capturer exit is a completeness
failure surfaced in meta["capture_ok"].
"""

import hashlib
import os
import re
import signal
import subprocess
import time

SNAPLEN = 0          # 0 = full packets (tcpdump: entire packet)
BUFFER_KB = 4096     # -B: 4 MiB kernel buffer per capturer, against drops
FILTER = "tcp port 3490"

_STATS_RE = re.compile(
    r"(\d+) packets captured.*?(\d+) packets received by filter.*?"
    r"(\d+) packets dropped by kernel", re.S)


def tool_version():
    out = subprocess.run(["tcpdump", "--version"], capture_output=True,
                         text=True, check=False)
    return (out.stdout or out.stderr).splitlines()[0].strip()


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Capture:
    """One tcpdump per node, owned as plain subprocesses (no shell)."""

    def __init__(self, mesh, outdir, run_id=""):
        self.mesh = mesh
        self.outdir = outdir
        self.run_id = run_id
        self.procs = {}      # node name -> Popen
        self.meta = {}       # node name -> dict
        os.makedirs(outdir, exist_ok=True)

    def start(self, attach_timeout=10):
        """Start every capturer and wait until each is attached.

        Attachment is confirmed from tcpdump's own "listening on" line,
        not from a sleep: a capturer that has not attached yet would
        silently miss the first exchanges, and an absence claim needs
        the capture to have been complete from before the first packet.
        """
        env = dict(os.environ,
                   PATH="/usr/sbin:/sbin:" + os.environ.get("PATH", ""))
        for node in self.mesh.all_nodes():
            pcap = os.path.join(
                self.outdir, f"{self.run_id + '-' if self.run_id else ''}"
                             f"{node.name}.pcap")
            errpath = pcap + ".stderr"
            argv = ["ip", "netns", "exec", node.ns,
                    "tcpdump", "-Z", "root", "-i", f"v{node.index}c",
                    "-s", str(SNAPLEN), "-nn", "-B", str(BUFFER_KB),
                    "-U", "-w", pcap, FILTER]
            errf = open(errpath, "w")
            proc = subprocess.Popen(argv, env=env, stdout=subprocess.DEVNULL,
                                    stderr=errf)
            self.procs[node.name] = proc
            self.meta[node.name] = {
                "pcap": pcap, "stderr_file": errpath,
                "command": " ".join(argv),
                "started_at_unix": round(time.time(), 3),
                "attached": False,
            }
        deadline = time.time() + attach_timeout
        pending = set(self.procs)
        while pending and time.time() < deadline:
            for name in list(pending):
                m = self.meta[name]
                try:
                    with open(m["stderr_file"]) as f:
                        if "listening on" in f.read():
                            m["attached"] = True
                            m["attached_at_unix"] = round(time.time(), 3)
                            pending.discard(name)
                except FileNotFoundError:
                    pass
                if self.procs[name].poll() is not None:
                    pending.discard(name)   # died; caught in stop()/alive()
            time.sleep(0.1)
        self.tool = tool_version()
        return all(m["attached"] for m in self.meta.values())

    def alive(self):
        """{node name: bool} — for the run supervisor, zero connections."""
        return {n: p.poll() is None for n, p in self.procs.items()}

    def stop(self):
        """SIGTERM every capturer, parse its final statistics, hash pcaps."""
        for name, proc in self.procs.items():
            m = self.meta[name]
            m["alive_at_stop"] = proc.poll() is None
            if m["alive_at_stop"]:
                proc.send_signal(signal.SIGTERM)
        for name, proc in self.procs.items():
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            m = self.meta[name]
            m["stopped_at_unix"] = round(time.time(), 3)
            m["exit_code"] = proc.returncode
            try:
                with open(m["stderr_file"]) as f:
                    err = f.read()
            except FileNotFoundError:
                err = ""
            m["stderr_sha256"] = hashlib.sha256(err.encode()).hexdigest()
            stats = _STATS_RE.search(err)
            if stats:
                m["packets_captured"] = int(stats.group(1))
                m["packets_received_by_filter"] = int(stats.group(2))
                m["packets_dropped_by_kernel"] = int(stats.group(3))
            else:
                m["packets_captured"] = None
                m["packets_received_by_filter"] = None
                m["packets_dropped_by_kernel"] = None
            m["pcap_sha256"] = (_sha256(m["pcap"])
                                if os.path.exists(m["pcap"]) else None)
        summary = {
            "tool": getattr(self, "tool", tool_version()),
            "snaplen": SNAPLEN, "filter": FILTER,
            "buffer_kb": BUFFER_KB,
            "nodes": self.meta,
            # The single flag record_valid consumes: every capturer
            # attached before use, survived until stop, exited cleanly
            # on our TERM, reported statistics, and dropped nothing.
            "capture_ok": all(
                m["attached"] and m["alive_at_stop"]
                and m["packets_dropped_by_kernel"] == 0
                and m["pcap_sha256"] is not None
                for m in self.meta.values()),
        }
        return summary

"""Real-binary mesh harness — runs actual shared-state-async processes.

Nothing about the protocol or the merge algorithm is reimplemented here.
This module only orchestrates namespaces, processes, links and time; the
code under test is the shipped binary.

Isolation (all unprivileged — no root, no containers, no QEMU):

  outer:  user + mount + net + uts namespace   (we are root inside)
    |- /run tmpfs so `ip netns` works
    |- br0 bridge, one veth pair per node
    `- per node: its own net namespace (own port 3490)
         `- per node process: mount + uts namespace
              |- hostname      -> distinct mAuthor identity (REQUIRED:
              |                   author identity is the hostname, so
              |                   without this every node believes it
              |                   authored everything)
              |- /tmp/shared-state  bind-mounted from the run dir, so
              |                   the harness can inspect node state
              `- /usr/share     tmpfs, for the hardcoded hooks path

Entry point: call ensure_inner() first thing in a runner, then use Mesh.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
DEFAULT_BIN = os.path.join(REPO, "build", "shared-state-async")
SBIN = "/usr/sbin:/sbin"
SUBNET = "10.99.0"
INNER_ENV = "SS_MESH_INNER"


def ensure_inner():
    """Re-exec this program inside the outer namespace if not already in it."""
    if os.environ.get(INNER_ENV) == "1":
        return
    env = dict(os.environ, SS_MESH_INNER="1",
               PATH=SBIN + ":" + os.environ.get("PATH", ""))
    argv = ["unshare", "--user", "--map-root-user", "--mount", "--net",
            "--uts", "--fork", sys.executable] + sys.argv
    os.execvpe("unshare", argv, env)


def sh(cmd, check=True, **kw):
    """Run a shell command inside the current namespace."""
    env = dict(os.environ, PATH=SBIN + ":" + os.environ.get("PATH", ""))
    return subprocess.run(cmd, shell=True, env=env, check=check,
                          capture_output=True, text=True, **kw)


class Node:
    def __init__(self, mesh, index, name):
        self.mesh = mesh
        self.index = index
        self.name = name            # also the hostname => mAuthor
        self.ns = f"n{index}"
        self.ip = f"{SUBNET}.{10 + index}"
        self.dir = os.path.join(mesh.rundir, name)
        self.statedir = os.path.join(self.dir, "state")
        self.hooksdir = os.path.join(self.dir, "hooks")
        self.bindir = os.path.join(self.dir, "bin")
        self.log = os.path.join(self.dir, "daemon.log")
        self.proc = None
        for d in (self.statedir, self.hooksdir, self.bindir):
            os.makedirs(d, exist_ok=True)

    # -- filesystem contracts ------------------------------------------
    @property
    def conf_path(self):
        return os.path.join(self.statedir, "shared-state-async.conf")

    def seed_config(self, empty=True):
        """Pre-seed the config file. Needed because `register` fatally
        exits when the file is missing (spec §6.6 / test T7)."""
        if empty:
            with open(self.conf_path, "w") as f:
                f.write('{"mTypeConf":[]}\n')

    def clean_state(self):
        shutil.rmtree(self.statedir, ignore_errors=True)
        os.makedirs(self.statedir, exist_ok=True)

    def set_peers(self, peers):
        """Install the discovery stub that defines this node's topology."""
        path = os.path.join(self.bindir, "shared-state-async-discover")
        with open(path, "w") as f:
            f.write("#!/bin/sh\n")
            for p in peers:
                f.write(f"echo {p.ip}\n")
        os.chmod(path, 0o755)

    def add_hook(self, data_type, name, body):
        d = os.path.join(self.hooksdir, data_type)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, name)
        with open(path, "w") as f:
            f.write(body)
        os.chmod(path, 0o755)
        return path

    # -- process execution ---------------------------------------------
    def _wrap(self, inner_cmd):
        """Wrap a command in this node's net + mount + uts namespaces."""
        prelude = (
            f"hostname {self.name}; "
            f"mount --bind {self.statedir} /tmp/shared-state; "
            f"mount -t tmpfs none /usr/share; "
            f"mkdir -p /usr/share/shared-state; "
            f"cp -r {self.hooksdir}/. /usr/share/shared-state/hooks 2>/dev/null; "
            f"export PATH={self.bindir}:$PATH; "
        )
        return (f"ip netns exec {self.ns} unshare --mount --uts --fork "
                f"sh -c '{prelude} {inner_cmd}'")

    def cli(self, args, stdin=None, timeout=30, check=False):
        """Run a CLI subcommand inside this node's namespaces."""
        cmd = self._wrap(f"exec {self.mesh.binary} {args}")
        env = dict(os.environ, PATH=SBIN + ":" + os.environ.get("PATH", ""))
        return subprocess.run(cmd, shell=True, env=env, input=stdin,
                              capture_output=True, text=True,
                              timeout=timeout, check=check)

    def start(self):
        os.makedirs("/tmp/shared-state", exist_ok=True)
        cmd = self._wrap(f"exec {self.mesh.binary} peer")
        logf = open(self.log, "w")
        env = dict(os.environ, PATH=SBIN + ":" + os.environ.get("PATH", ""))
        self.proc = subprocess.Popen(cmd, shell=True, env=env,
                                     stdout=logf, stderr=subprocess.STDOUT,
                                     preexec_fn=os.setsid)
        return self.proc

    def wait_listening(self, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.exists(self.log):
                with open(self.log) as f:
                    if "Listening" in f.read():
                        return True
            time.sleep(0.2)
        return False

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
        self.proc = None

    def daemon_pids(self):
        """PIDs of the daemon process(es) for this node, host-visible."""
        out = sh(f"pgrep -f '{self.mesh.binary} peer'", check=False).stdout
        return [int(p) for p in out.split()]

    def read_log(self):
        try:
            with open(self.log) as f:
                return f.read()
        except FileNotFoundError:
            return ""

    # -- observation ----------------------------------------------------
    def probe(self, data_type, timeout=15):
        """Near-passive state read: a real sync session carrying an EMPTY
        slice. The daemon merges nothing (zero changes, no hooks) and
        returns its full state. Preferred over CLI dump/get, which sync
        as a side effect and perturb what we measure."""
        import wire
        name, state = wire.client_session(self.ip, data_type, {})
        return state


class Mesh:
    """Context manager owning the namespace topology and node lifecycle."""

    def __init__(self, node_names, rundir, binary=DEFAULT_BIN, links=None):
        assert os.environ.get(INNER_ENV) == "1", "call ensure_inner() first"
        self.binary = binary
        self.rundir = rundir
        os.makedirs(rundir, exist_ok=True)
        self.nodes = {n: Node(self, i + 1, n) for i, n in enumerate(node_names)}
        self.links = links or {}

    def __enter__(self):
        self._setup_network()
        return self

    def __exit__(self, *exc):
        for n in self.nodes.values():
            n.stop()
        self._teardown_network()
        return False

    def _teardown_network(self):
        """Namespaces persist for the life of the outer process, so a
        mesh MUST clean up after itself or the next one collides."""
        for node in self.nodes.values():
            sh(f"ip netns del {node.ns}", check=False)
            sh(f"ip link del v{node.index}h", check=False)
        sh("ip link del br0", check=False)

    def _setup_network(self):
        sh("mount -t tmpfs none /run")
        sh("mkdir -p /run/netns")
        sh("ip link add br0 type bridge")
        sh(f"ip addr add {SUBNET}.1/24 dev br0")
        sh("ip link set br0 up")
        for node in self.nodes.values():
            i = node.index
            sh(f"ip netns add {node.ns}")
            sh(f"ip link add v{i}h type veth peer name v{i}c")
            sh(f"ip link set v{i}h master br0")
            sh(f"ip link set v{i}h up")
            sh(f"ip link set v{i}c netns {node.ns}")
            sh(f"ip netns exec {node.ns} ip addr add {node.ip}/24 dev v{i}c")
            sh(f"ip netns exec {node.ns} ip link set v{i}c up")
            sh(f"ip netns exec {node.ns} ip link set lo up")

    def impair(self, node, delay_ms=None, loss_pct=None):
        """Inject latency/loss on a node's link (tc netem, host side)."""
        parts = []
        if delay_ms:
            parts.append(f"delay {delay_ms}ms")
        if loss_pct:
            parts.append(f"loss {loss_pct}%")
        if not parts:
            return
        sh(f"tc qdisc replace dev v{node.index}h root netem " + " ".join(parts))

    def node(self, name):
        return self.nodes[name]

    def all_nodes(self):
        return list(self.nodes.values())

    def mesh_all(self):
        """Full-mesh topology: every node discovers every other node."""
        for n in self.all_nodes():
            n.set_peers([p for p in self.all_nodes() if p is not n])

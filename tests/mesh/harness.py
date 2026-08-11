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
        self.clock_offset = 0     # seconds added to this node's CLOCK_MONOTONIC
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
        """Wrap a command in this node's net + mount + uts (+ time) namespaces."""
        clock = ""
        if self.clock_offset:
            # A time namespace gives this node its own CLOCK_MONOTONIC
            # epoch. That matters because the publish scheduler fires when
            # `steady_seconds % updateInterval == 0`: daemons sharing one
            # host share one monotonic clock, so they all sync on the same
            # instant and an entry can cascade across the whole chain
            # within a single round. Real nodes boot at different times and
            # are not phase-locked, which is what makes an entry arrive up
            # to a full interval late at each hop.
            clock = f"--time --monotonic {self.clock_offset} "
        prelude = (
            f"hostname {self.name}; "
            f"mount --bind {self.statedir} /tmp/shared-state; "
            f"mount -t tmpfs none /usr/share; "
            f"mkdir -p /usr/share/shared-state; "
            f"cp -r {self.hooksdir}/. /usr/share/shared-state/hooks 2>/dev/null; "
            f"export PATH={self.bindir}:$PATH; "
        )
        return (f"ip netns exec {self.ns} unshare --mount --uts {clock}"
                f"--fork sh -c '{prelude} {inner_cmd}'")

    def cli(self, args, stdin=None, timeout=30, check=False):
        """Run a CLI subcommand inside this node's namespaces."""
        cmd = self._wrap(f"exec {self.mesh.binary} {args}")
        env = dict(os.environ, PATH=SBIN + ":" + os.environ.get("PATH", ""))
        return subprocess.run(cmd, shell=True, env=env, input=stdin,
                              capture_output=True, text=True,
                              timeout=timeout, check=check)

    def start(self, rlimit_nofile=None):
        os.makedirs("/tmp/shared-state", exist_ok=True)
        inner = f"exec {self.mesh.binary} peer"
        if rlimit_nofile:
            inner = f"ulimit -n {rlimit_nofile}; " + inner
        cmd = self._wrap(inner)
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

    def publish(self, data_type, key, gen, timeout=30):
        """Author a new generation of `key` through the normal CLI path.
        The payload carries a generation counter so the harness knows
        ground-truth freshness ordering while the protocol cannot see
        it — the entire point of generation tagging."""
        payload = json.dumps({key: {"gen": gen, "src": self.name}})
        return self.cli(f"insert {data_type}", stdin=payload, timeout=timeout)

    # -- observation ----------------------------------------------------
    def probe(self, data_type, timeout=15):
        """Near-passive state read: a real sync session carrying an EMPTY
        slice. The daemon merges nothing (zero changes, no hooks) and
        returns its full state. Preferred over CLI dump/get, which sync
        as a side effect and perturb what we measure."""
        import wire
        name, state = wire.client_session(self.ip, data_type, {}, timeout=timeout)
        return state

    def inject(self, data_type, slice_, timeout=15):
        """Speak the protocol as a peer and offer `slice_` to this node.
        Entries: {key: {author, ttl, data[, version]}}. This is a normal,
        spec-conformant sync session — the same thing any neighbour does.
        Returns the node's post-merge state."""
        import wire
        name, state = wire.client_session(self.ip, data_type, slice_,
                                          timeout=timeout)
        return state

    def gen_of(self, state, key):
        """Generation held for `key` in a probed state, or None."""
        entry = (state or {}).get(key)
        if not entry:
            return None
        data = entry.get("data") or {}
        return data.get("gen") if isinstance(data, dict) else None


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

    def impair(self, node, delay_ms=None, loss_pct=None, rate_kbit=None):
        """Inject latency/loss/bandwidth limits on a node's link.

        `rate_kbit` matters more than it looks: a receiver starts
        bleaching an entry when it finishes *reading* it, while the
        sender's copy has been decaying since it serialized. The TTL
        therefore diverges by roughly the transfer duration, per hop —
        which is negligible on a lab bridge and tens of seconds for a
        large state over long-distance radio."""
        parts = []
        if delay_ms:
            parts.append(f"delay {delay_ms}ms")
        if loss_pct:
            parts.append(f"loss {loss_pct}%")
        if rate_kbit:
            parts.append(f"rate {rate_kbit}kbit")
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

    def chain(self, names=None, directed=False):
        """Line topology. `directed=True` gives each node only its
        downstream neighbour, so an entry can advance one hop per
        *upstream* sync tick. Bidirectionally, any adjacent node's tick
        moves it, so with staggered clocks entries cascade far faster
        than one hop per interval — which is the difference between a
        lab chain and long-distance radio links."""
        if directed:
            nodes = [self.node(n) for n in names] if names else self.all_nodes()
            for i, n in enumerate(nodes):
                n.set_peers([nodes[i + 1]] if i < len(nodes) - 1 else [])
            return nodes
        return self._chain_bidirectional(names)

    def _chain_bidirectional(self, names=None):
        """Line topology a-b-c: the ends are two hops apart, so an
        author's updates must survive being relayed. Multi-hop is where
        the field reports live (MonteNet: jime-balcon-tronco-...), and a
        full mesh structurally hides those failures because the author
        talks to everyone directly."""
        nodes = [self.node(n) for n in names] if names else self.all_nodes()
        for i, n in enumerate(nodes):
            peers = []
            if i > 0:
                peers.append(nodes[i - 1])
            if i < len(nodes) - 1:
                peers.append(nodes[i + 1])
            n.set_peers(peers)
        return nodes

    def stagger_clocks(self, interval):
        """Give each node a distinct CLOCK_MONOTONIC offset spread across
        `interval`, so their publish schedules are not phase-locked.

        Must be called before the nodes are started: the time namespace is
        created when the process is."""
        nodes = self.all_nodes()
        step = max(1, interval // len(nodes))
        for i, n in enumerate(nodes):
            n.clock_offset = i * step + (i % 3)
        return {n.name: n.clock_offset for n in nodes}

    # -- lifecycle helpers ---------------------------------------------
    def bootstrap(self, data_type, update_interval=5, bleach_ttl=300,
                  nodes=None, full_mesh=True):
        """Clean, configure, register the type on and start every node."""
        nodes = nodes if nodes is not None else self.all_nodes()
        if full_mesh:
            self.mesh_all()
        for n in nodes:
            n.clean_state()
            n.seed_config()          # works around T7 (register bootstrap)
            n.cli(f"register {data_type} community {update_interval} "
                  f"{bleach_ttl}", timeout=30)
        for n in nodes:
            n.start()
        for n in nodes:
            if not n.wait_listening():
                raise RuntimeError(
                    f"{n.name} never listened: {n.read_log()[:300]}")
        return nodes

    def snapshot(self, data_type, nodes=None):
        """{node name: probed state or None} across the mesh."""
        out = {}
        for n in (nodes if nodes is not None else self.all_nodes()):
            try:
                out[n.name] = n.probe(data_type)
            except Exception:
                out[n.name] = None
        return out

    def gens(self, data_type, key, nodes=None):
        """{node name: generation held for key} — the core H2 measurement."""
        snap = self.snapshot(data_type, nodes)
        any_node = self.all_nodes()[0]
        return {name: any_node.gen_of(state, key)
                for name, state in snap.items()}

    def wait_until(self, predicate, timeout=60, interval=2):
        """Poll predicate() until true; returns (ok, last_value)."""
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            ok, last = predicate()
            if ok:
                return True, last
            time.sleep(interval)
        return False, last

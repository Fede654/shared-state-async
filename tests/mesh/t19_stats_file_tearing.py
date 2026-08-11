"""T19 — the statistics file must not tear under concurrent writers.

Strict condition
    `/tmp/shared-state/network_statistics.json` stays parseable while
    the daemon and CLI processes are writing it concurrently.

Why it matters
    Every sync does a read-modify-write of this file, and file locking
    is behind `SS_STAT_FILE_LOCKING`, which is **OFF by default** in
    `CMakeLists.txt`. A daemon serving a peer and a CLI running a sync
    therefore interleave truncating writes on the same path. The
    in-code warning "Discarding corrupted or empty statistics file"
    exists because this was already observed. Whatever consumes these
    numbers for bandwidth-aware routing silently loses history each time.

    The same file also stores `mTS` as raw `steady_clock` ticks —
    boot-relative, so records are not comparable across a reboot or
    between nodes at all. That is checked here too, since a reader
    cannot tell a torn file from a meaningless one.

Method
    Hammer concurrent syncs while repeatedly reading and parsing the
    file.

Expected on today's code: RED — this is a race, so a green run means
"not observed", not "cannot happen"; the default-off lock is the defect
either way.
"""

import json
import os
import threading
import time

ID = "T19"
TITLE = "stats file survives concurrent writers"
EXPECT_TODAY = "RED"

TYPE = "probe"
DURATION = 25


def run(mesh):
    node = mesh.node("lime-a")
    mesh.bootstrap(TYPE, nodes=[node], full_mesh=False)
    node.set_peers([])
    node.publish(TYPE, "k", gen=1)

    stats_path = os.path.join(node.statedir, "network_statistics.json")
    stop = threading.Event()

    def hammer():
        while not stop.is_set():
            try:
                node.probe(TYPE, timeout=10)   # each sync rewrites the file
            except Exception:
                pass

    threads = [threading.Thread(target=hammer) for _ in range(6)]
    for t in threads:
        t.start()

    torn = 0
    reads = 0
    sample = None
    boot_relative = None
    deadline = time.time() + DURATION
    try:
        while time.time() < deadline:
            if not os.path.exists(stats_path):
                continue
            try:
                with open(stats_path) as f:
                    raw = f.read()
                reads += 1
                if not raw.strip():
                    torn += 1
                    continue
                doc = json.loads(raw)
                if boot_relative is None:
                    boot_relative = _check_ts(doc)
            except json.JSONDecodeError as e:
                torn += 1
                if sample is None:
                    sample = str(e)[:90]
            except OSError:
                pass
            time.sleep(0.01)
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=20)

    notes = []
    if boot_relative:
        notes.append(f"timestamps are boot-relative ({boot_relative})")
    if torn:
        return False, (f"file unparseable in {torn}/{reads} reads under "
                       f"concurrent writers (locking is compile-time "
                       f"optional and OFF by default)"
                       + (f"; e.g. {sample}" if sample else "")
                       + ("; " + "; ".join(notes) if notes else ""))
    return True, (f"{reads} reads, no tearing observed in {DURATION}s"
                  + ("; " + "; ".join(notes) if notes else ""))


def _check_ts(doc):
    """Return a description if mTS looks boot-relative rather than wall clock."""
    try:
        for item in doc.get("stats", []):
            for rec in item.get("value", []):
                ts = rec.get("mTS", {})
                val = ts.get("xint64") if isinstance(ts, dict) else ts
                if val is None:
                    continue
                # wall-clock ns since epoch would be ~1.7e18 in 2026
                if val < 1e17:
                    return f"mTS={val}, far below epoch nanoseconds"
                return None
    except Exception:
        pass
    return None

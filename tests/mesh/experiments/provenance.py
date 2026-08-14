"""Run provenance — the single implementation for this suite.

Supersedes `run_mesh_tests.py::_provenance`, which now calls in here
rather than maintaining a parallel copy.

THE ATTRIBUTION TRAP THIS EXISTS TO AVOID

The obvious implementation asks git for the current state of the source
tree and records it as "the binary's source". That is wrong whenever the
binary was not rebuilt from that state — which is the normal case, since
a checkout moves every commit while a binary changes only on rebuild.
Caught by external review: a binary built on 6 August would have been
reported as built from a commit made on 14 August, purely because that
commit was checked out when the experiment ran. Numerically auditable,
falsely attributed, and the falsehood is invisible in the record.

So the current checkout is recorded under `checkout_at_measurement` and
never called the binary's source. Actual build-time state comes from a
stamp written when the binary is built (`stamp_build`), and is trusted
only if the stamped sha256 still matches the binary on disk. With no
stamp, the source revision is reported as UNKNOWN — which is the honest
answer for any binary built before stamping existed.

Recorded per run:
  - binary: sha256, size, mtime, and `built_from` (stamped, verified)
  - build config: compiler, build type, flags, SS_* options, and the
    commits of libretroshare/rapidjson actually built against
  - checkout_at_measurement: repo state during the run, harness and
    binary-source trees tracked separately
  - deps: sha256 of every runtime .py, since a commit id says nothing
    about a dirty tree
"""

import hashlib
import json
import os
import platform
import subprocess

STAMP_NAME = "BUILD_PROVENANCE.json"


def _run(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True,
                              text=True, timeout=15).stdout.strip() or None
    except Exception:
        return None


def _git(repo, args):
    return _run(f"git -C {repo} {args}") if os.path.isdir(repo) else None


def _sha256(path):
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _toplevel(path):
    """Repo root containing `path`, asked of git.

    Counting parent directories is what broke an earlier version: three
    dirname calls from experiments/ land on <repo>/tests, which has no
    .git, so every record was written with a null harness.
    """
    if not os.path.isdir(path):
        path = os.path.dirname(path)
    return _run(f"git -C {path} rev-parse --show-toplevel")


def _repo_state(repo):
    if not repo or not os.path.isdir(os.path.join(repo, ".git")):
        return None
    dirty = _git(repo, "status --porcelain")
    return {
        "path": repo,
        "commit": _git(repo, "rev-parse HEAD"),
        "describe": _git(repo, "describe --always --dirty"),
        "dirty": bool(dirty),
        "dirty_files": len(dirty.splitlines()) if dirty else 0,
    }


def _cache(build_dir, key):
    path = os.path.join(build_dir, "CMakeCache.txt")
    try:
        with open(path) as f:
            for line in f:
                if line.startswith(key + ":"):
                    return line.split("=", 1)[1].strip()
    except OSError:
        return None
    return None


def _build_config(build_dir):
    """Build configuration, read from the build tree itself.

    Unlike a git HEAD, these genuinely describe the build: CMakeCache
    and _deps/ are written at configure/build time and do not move when
    the checkout does.
    """
    return {
        "build_dir": build_dir,
        "cmake_cxx_compiler": _cache(build_dir, "CMAKE_CXX_COMPILER"),
        "cmake_build_type": _cache(build_dir, "CMAKE_BUILD_TYPE"),
        "cmake_cxx_flags": _cache(build_dir, "CMAKE_CXX_FLAGS"),
        "cmake_cxx_flags_release": _cache(build_dir, "CMAKE_CXX_FLAGS_RELEASE"),
        "ss_options": {k: _cache(build_dir, k) for k in (
            "SS_TESTS", "SS_STAT_FILE_LOCKING", "SS_DEVELOPMENT_BUILD",
            "SS_CPPTRACE_STACKTRACE")},
        "dep_commits": {
            d: _git(os.path.join(build_dir, "_deps", f"{d}-src"),
                    "rev-parse HEAD")
            for d in ("libretroshare", "rapidjson")},
    }


def stamp_build(binary):
    """Record build-time state next to the binary. Run after building.

    This is the only moment at which "what source produced this binary"
    is knowable without guessing, so it is written down then rather than
    inferred later from whatever happens to be checked out.
    """
    binary = os.path.realpath(binary)
    build_dir = os.path.dirname(binary)
    src_repo = _toplevel(os.path.dirname(build_dir))
    stamp = {
        "binary_sha256": _sha256(binary),
        "binary_mtime": int(os.stat(binary).st_mtime),
        "source": _repo_state(src_repo),
        "build": _build_config(build_dir),
        "host": {"kernel": platform.release(),
                 "compiler": _run("gcc --version | head -1")},
        "stamped_at": _run("date -u +%Y-%m-%dT%H:%M:%SZ"),
    }
    path = os.path.join(build_dir, STAMP_NAME)
    with open(path, "w") as f:
        json.dump(stamp, f, indent=1, sort_keys=True)
    return path


def _built_from(binary, build_dir):
    """Stamped build provenance, but only if it still describes THIS
    binary. A stale stamp is worse than none, so the sha256 is checked
    and a mismatch is reported rather than silently trusted."""
    path = os.path.join(build_dir, STAMP_NAME)
    try:
        with open(path) as f:
            stamp = json.load(f)
    except (OSError, ValueError):
        return {"status": "UNKNOWN — no build stamp; this binary predates "
                          "stamped provenance, so the revision that built "
                          "it cannot be established from the record"}
    if stamp.get("binary_sha256") != _sha256(binary):
        return {"status": "STALE — a build stamp exists but its sha256 does "
                          "not match the binary on disk, so it describes a "
                          "different build and is not trusted",
                "stamp": stamp}
    return {"status": "verified", **stamp}


def manifest(script_path):
    """sha256 of every Python file the run depends on.

    A commit id says nothing on a dirty tree: it identifies neither
    harness.py (namespaces and probes) nor wire.py (decodes the TTLs
    being measured). Compared against a session-start snapshot so an
    edit BETWEEN cells is caught too — modules are imported once at
    startup, so a later edit would otherwise be hashed into the next
    cell's provenance while the process kept running the old code.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(script_path)))
    out = {}
    for base, _dirs, files in os.walk(root):
        if "__pycache__" in base or "/results" in base:
            continue
        for fn in sorted(files):
            if fn.endswith(".py"):
                full = os.path.join(base, fn)
                out[os.path.relpath(full, root)] = _sha256(full)
    return out


def collect(binary, script_path):
    """Provenance for one run. `script_path` should be __file__."""
    binary = os.path.realpath(binary)
    build_dir = os.path.dirname(binary)
    src_repo = _toplevel(os.path.dirname(build_dir))
    harness_repo = _toplevel(os.path.abspath(script_path))

    try:
        st = os.stat(binary)
        size, mtime = st.st_size, int(st.st_mtime)
    except OSError:
        size = mtime = None

    return {
        "binary": {
            "path": binary,
            "sha256": _sha256(binary),
            "size": size,
            "mtime": mtime,
            # what actually built it, or an explicit admission of not
            # knowing — never the checkout that happened to be present
            "built_from": _built_from(binary, build_dir),
        },
        "build_config": _build_config(build_dir),
        # State of the trees DURING MEASUREMENT. Deliberately not called
        # the binary's source: the two coincide only right after a
        # rebuild. Tracked separately so a run against another
        # checkout's binary is not attributed to this repo.
        "checkout_at_measurement": {
            "harness": _repo_state(harness_repo),
            "binary_source_tree": _repo_state(src_repo),
        },
        "script": {
            "path": os.path.abspath(script_path),
            "sha256": _sha256(script_path),
        },
        "deps": manifest(script_path),
        "host": {
            "kernel": platform.release(),
            "uname": _run("uname -srm"),
            "compiler_default": _run("gcc --version | head -1"),
        },
    }

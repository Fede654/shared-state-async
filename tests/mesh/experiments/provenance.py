"""Run provenance for the experiment scripts.

Why this file exists: the mesh runner records binary hashes and git
state (`run_mesh_tests.py::_provenance`), but the experiment scripts
added later recorded only an absolute path and a timestamp. A path does
not prove which binary sat there during the run, and the scripts
themselves were being edited while runs executed, so their runtime state
could not be reconstructed either. External review caught it as a
regression of a problem this suite had already fixed once.

Records, for every run:
  - the binary: sha256, size, mtime, plus git commit/dirty of the source
    tree inferred from <src>/build/<binary>
  - the harness: git commit/dirty of the repo holding these scripts,
    tracked SEPARATELY, because a run against another checkout's binary
    must not be attributed to this repo's revision
  - the experiment script itself: sha256 of the file that ran, so an
    edit mid-session is detectable rather than invisible
"""

import hashlib
import os
import subprocess


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
    """Repository root containing `path`, asked of git rather than
    counted in parent directories.

    Counting parents is what broke the first version: three `dirname`
    calls from experiments/<script>.py land on <repo>/tests, which has
    no .git, so every record was written with "harness": null. Depth is
    a property of where a file happens to sit; the repo root is not.
    """
    if not os.path.isdir(path):
        path = os.path.dirname(path)
    return _run(f"git -C {path} rev-parse --show-toplevel")


def _repo_state(repo):
    if not repo:
        return None
    if not os.path.isdir(os.path.join(repo, ".git")):
        return None
    dirty = _git(repo, "status --porcelain")
    return {
        "path": repo,
        "commit": _git(repo, "rev-parse HEAD"),
        "describe": _git(repo, "describe --always --dirty"),
        "dirty": bool(dirty),
        "dirty_files": len(dirty.splitlines()) if dirty else 0,
    }


def manifest(script_path):
    """sha256 of every Python file the run actually depends on.

    A commit id is not enough when the tree is dirty: `4a42e10-dirty`
    identifies neither `harness.py` (which drives the namespaces and the
    probes) nor `wire.py` (which decodes the TTLs being measured) nor
    `single_factor.py` (whose PIVOT and helpers this imports). Hashing
    only the entry-point script left the parts doing most of the work
    unattested. Collected before AND after each cell so a mid-run edit
    is detected rather than silently folded into the results.
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
    src_dir = os.path.dirname(build_dir)
    # Both resolved via `git rev-parse --show-toplevel`, and kept
    # SEPARATE: a run against another checkout's binary must not be
    # attributed to this repo's revision.
    src_repo = _toplevel(src_dir)
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
            "source": _repo_state(src_repo),
            "cmake_build_type": _cache(build_dir, "CMAKE_BUILD_TYPE"),
            "cmake_cxx_flags": _cache(build_dir, "CMAKE_CXX_FLAGS"),
        },
        "harness": _repo_state(harness_repo),
        "script": {
            "path": os.path.abspath(script_path),
            "sha256": _sha256(script_path),
        },
        "deps": manifest(script_path),
        "host": {
            "uname": _run("uname -srm"),
            "cc": (_run("gcc --version") or "").splitlines()[:1],
        },
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

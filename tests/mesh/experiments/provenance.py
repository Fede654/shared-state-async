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

WHAT THIS PROVES, AND WHAT IT DOES NOT

This is **workflow verification, not tamper-proof provenance.** It
guards against accidental false attribution — the stale binary, the
no-op incremental build, the checkout that moved between building and
measuring. It cannot stop a local user who wants a false stamp.

That distinction was earned. Every fact separating "a build happened"
from "a file exists" used to be a CLI argument: `--started`,
`--finished`, `--source-before`, `--source-repo`. External review copied
the untouched binary, took the genuine current source fingerprint (which
anyone can compute — it is read-only public state), passed the copy's
own mtime as the build window, and got `coupled_to_build=True` with a
clean source and the right commit. No build occurred. Four earlier
attack tests had missed it: they covered "copy without source" and "copy
without a fingerprint", never "copy with a fingerprint that is trivially
obtainable".

So coupling evidence is no longer accepted from anywhere. `build_and_stamp`
captures the fingerprint, deletes the binary, runs the build and stamps
inside ONE process, and the CLI exposes no path that takes precomputed
evidence — `stamp` can now only write a manual stamp. Coupling must be
produced by the mechanism that does the building.

Shutting the CLI door was not enough, and the second review found the
Python one still open: `stamp_build` kept its coupling parameters behind
`evidence_origin == "in-process"`, and a caller can simply type that
string. It was typed, next to a copied binary, and earned a coupled
stamp. So the public `stamp_build` now takes no coupling parameters at
all; construction moved to the private `_write_stamp`, gated on an
object-identity sentinel a caller cannot spell. The same review found
that two absent dependency fingerprints compared equal and reported
`dep_fingerprints_stable: true` — absence reading as stability — so
every expected dependency must now be identifiable at both ends.

Absolute protection against a malicious local user needs external signed
build attestation, which is out of scope here. Read `coupled_to_build`
as "this artifact came out of a build this tool ran and watched", never
as "this artifact cannot have been forged".
"""

import hashlib
import json
import os
import platform
import subprocess
import time

STAMP_NAME = "BUILD_PROVENANCE.json"
BINARY_NAME = "shared-state-async"
EXPECTED_DEPS = ("libretroshare", "rapidjson")

# The paths that actually reach the compiler. A difference anywhere else
# in the tree — docs, tests, results — cannot change the binary.
COMPILED_PATHS = ("src", "include", "app", "CMakeLists.txt")

# Object identity, deliberately not a string. `evidence_origin="in-process"`
# was the previous guard, and a string is something a caller can simply
# type: review passed that exact literal alongside a copied binary and
# earned a coupled stamp. A caller cannot type this object — it has to be
# reached for by name, which turns accidental misuse into a deliberate
# act. That is the whole boundary being defended; see the module
# docstring on why forgery by a determined local user is out of scope.
_COUPLING_OBSERVED = object()


def _run(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True,
                              text=True, timeout=15).stdout.strip() or None
    except Exception:
        return None


def _git(repo, args):
    return _run(f"git -C {repo} {args}") if os.path.isdir(repo) else None


def _git_raw(repo, args):
    """git output WITHOUT stripping.

    `_run` strips, which silently corrupted `status --porcelain`: its
    format is two status columns then a space, so an unstaged
    modification is " M path", and stripping the leading space made the
    first line one character short — producing "ests/mesh/..." in the
    record. Provenance that quietly mangles its own filenames is worse
    than provenance that omits them.
    """
    if not os.path.isdir(repo):
        return None
    try:
        return subprocess.run(f"git -C {repo} {args}", shell=True,
                              capture_output=True, text=True,
                              timeout=15).stdout
    except Exception:
        return None


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


def _is_generated(rel):
    """Output a run writes into its own tree, rather than source.

    Kept in one place because the two callers disagreed: the untracked
    scan skipped `/results/` while the tracked diff did not, so a
    MODIFIED tracked result file still changed the source fingerprint —
    the comment claimed results were excluded and only half of them
    were. That direction is safe (a spurious refusal, never a spurious
    grant), but a check that fires on the evidence a run produces is a
    check people learn to override.
    """
    return (rel.startswith("build/") or rel == "build"
            or "results/" in rel or rel.endswith("/results"))


def _repo_state(repo):
    if not repo or not os.path.isdir(os.path.join(repo, ".git")):
        return None
    dirty = _git_raw(repo, "status --porcelain")
    paths = [ln[3:] for ln in (dirty or "").splitlines() if len(ln) > 3]
    # A run writes its own results into the tree, so by its second cell
    # the checkout is "dirty" with nothing but the evidence it just
    # produced. Counting alone made that indistinguishable from an
    # edited source file, so the paths are listed and generated output
    # is separated out.
    generated = [p for p in paths if _is_generated(p)]
    source = [p for p in paths if p not in generated]
    return {
        "path": repo,
        "commit": _git(repo, "rev-parse HEAD"),
        "describe": _git(repo, "describe --always --dirty"),
        "dirty": bool(paths),
        "dirty_files": len(paths),
        "dirty_paths": sorted(paths)[:200],
        "dirty_source_files": len(source),
        "dirty_source_paths": sorted(source)[:200],
        "dirty_generated_files": len(generated),
    }


def source_fingerprint(repo):
    """Exact identity of the source that a build consumed.

    A commit id alone is not it: a dirty tree builds something the
    commit does not describe. Recording dirty *paths* is not it either —
    paths say which files changed, not what they now contain. So the
    fingerprint is the commit plus a hash of the actual tracked diff,
    plus hashes of untracked files that could enter a build. `results/`
    and `build/` are excluded because a run writes its own evidence into
    the tree and that must not read as a source change.

    Returns None when `repo` is not a git repository, which is itself
    load-bearing: an out-of-tree build directory resolves to no source,
    and a stamp that cannot identify its source must not claim coupling.
    """
    if not repo or not os.path.isdir(os.path.join(repo, ".git")):
        return None
    head = _git(repo, "rev-parse HEAD")
    # The exclusions are pathspecs, not a post-filter: a tracked results
    # file that a run MODIFIES shows up in `diff HEAD` and would move the
    # fingerprint mid-experiment. Git's default pathspec wildmatch lets
    # `*` cross `/`, so these cover results/ at any depth.
    diff = _git_raw(repo, "diff HEAD -- . ':(exclude)*results/*' "
                          "':(exclude)build/*'") or ""
    untracked = []
    listing = _git_raw(repo, "ls-files --others --exclude-standard") or ""
    for rel in sorted(listing.splitlines()):
        if not rel or _is_generated(rel):
            continue
        untracked.append((rel, _sha256(os.path.join(repo, rel))))
    h = hashlib.sha256()
    h.update((head or "").encode())
    h.update(diff.encode())
    for rel, sha in untracked:
        h.update(rel.encode())
        h.update((sha or "").encode())
    return {
        "commit": head,
        "clean": not diff and not untracked,
        "tracked_diff_sha256": _sha256_bytes(diff.encode()),
        "untracked_source_files": len(untracked),
        "fingerprint": h.hexdigest(),
    }


def _sha256_bytes(b):
    h = hashlib.sha256()
    h.update(b)
    return h.hexdigest()


def _under_compiled(rel):
    return any(rel == c or rel.startswith(c + "/") for c in COMPILED_PATHS)


def compiled_fingerprint(repo):
    """Hash of what the compiler actually reads, dirty or not.

    `source_fingerprint` identifies the whole tree, which is the right
    scope for "did the source move during the build" but the wrong one
    for "did two builds compile the same thing": it moves when a test or
    a doc changes, and — more importantly — a *commit* is not the
    compiled input at all when the tree is dirty.

    Dirty builds are supported here deliberately (a clean-tree-only rule
    makes coupling unobtainable during development), so two builds can
    share a commit and still have compiled different bytes. Review
    caught exactly that gap. This walks the working tree under
    COMPILED_PATHS and hashes contents, so it answers the question the
    commit cannot.
    """
    if not repo or not os.path.isdir(repo):
        return None
    files = []
    for rel in COMPILED_PATHS:
        full = os.path.join(repo, rel)
        if os.path.isfile(full):
            files.append(rel)
        elif os.path.isdir(full):
            for base, dirs, names in os.walk(full):
                dirs.sort()
                for n in sorted(names):
                    files.append(os.path.relpath(
                        os.path.join(base, n), repo))
    files.sort()
    h = hashlib.sha256()
    for rel in files:
        h.update(rel.encode())
        h.update((_sha256(os.path.join(repo, rel)) or "").encode())
    return {
        "paths": list(COMPILED_PATHS),
        "file_count": len(files),
        "fingerprint": h.hexdigest(),
    }


def _host_id():
    """Stable identifier for THIS machine, published as a hash.

    Matching kernel and compiler strings mean two builds recorded the
    same environment, not that they ran on the same box — review's
    point. /etc/machine-id distinguishes hosts; it is hashed rather than
    recorded so the record can be published without carrying a machine
    identifier around with it.
    """
    try:
        with open("/etc/machine-id") as f:
            return _sha256_bytes(f.read().strip().encode())[:16]
    except OSError:
        return None


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


def dep_fingerprints(build_dir):
    """Identity of the fetched dependency trees, not just their commits.

    `_build_config` records `dep_commits`, which is blind to a dirty
    _deps checkout: a local edit inside libretroshare-src changes the
    binary and moves nothing in the record. The coupling claim covers
    everything the compiler read, so it has to cover these too.

    FetchContent materializes `_deps/*-src` during CONFIGURE, so there
    is nothing to hash before that — the "pre" point is taken right
    after configure and the pair brackets the build proper. Configure
    itself is covered by the source fingerprint instead, which is taken
    before it and re-checked at the end.
    """
    return {d: source_fingerprint(
                os.path.join(build_dir, "_deps", f"{d}-src"))
            for d in EXPECTED_DEPS}


def _dep_problems(before, now):
    """Reasons the dependency trees do not support a coupling claim.

    ABSENCE IS NOT STABILITY. The first version compared `before` to
    `now` and called them stable when they matched — which they trivially
    do when both are None, so a build directory with no `_deps` at all
    reported `dep_fingerprints_stable: true`. "We looked and saw
    nothing" and "nothing moved" are not the same fact, and only one of
    them supports a claim about what the compiler read.

    So every expected dependency must be present and hashable at BOTH
    ends, and unchanged between them. Missing and drifting both refuse.
    """
    if before is None:
        return ["no pre-build dependency fingerprints were captured"]
    bad = []
    for d in EXPECTED_DEPS:
        was, fp = (before or {}).get(d), (now or {}).get(d)
        if not (was or {}).get("fingerprint"):
            bad.append(f"{d}: not identifiable before the build")
        elif not (fp or {}).get("fingerprint"):
            bad.append(f"{d}: not identifiable after the build")
        elif was["fingerprint"] != fp["fingerprint"]:
            bad.append(f"{d}: changed during the build")
    return bad


def _config_match(stamped, live):
    """Whether the stamped build config still matches the live tree."""
    if not isinstance(stamped, dict) or "build" not in stamped:
        return None          # nothing stamped to compare against
    a, b = stamped["build"], live
    keys = ("cmake_cxx_compiler", "cmake_build_type", "cmake_cxx_flags",
            "cmake_cxx_flags_release", "ss_options", "dep_commits")
    diff = [k for k in keys if a.get(k) != b.get(k)]
    return {"match": not diff, "differing_keys": diff}


def stamp_build(binary, source_repo=None):
    """Record current state next to a binary. MANUAL STAMP ONLY.

    This is the whole public stamping API, and it takes no coupling
    parameters — not a `coupled` flag, not a build window, not a
    fingerprint. There is deliberately nothing here to assert with.

    The parameters used to exist, guarded by `evidence_origin` being the
    string `"in-process"`, and a string is something a caller can simply
    type. Review typed it, alongside a copied binary and its own mtime,
    and got `coupled_to_build: true` on a file no build had touched — a
    second time, through the Python door, after the CLI door was shut.
    Removing the parameters is what actually closes it: a guard you can
    satisfy by guessing a constant is documentation, not a guard.

    What this produces attests that the binary is unchanged since
    stamping. It does NOT establish that the recorded source produced
    it. For that, use `build_and_stamp`, which is the only thing in this
    module that can grant coupling and does so only for a build it ran
    itself.
    """
    return _write_stamp(binary, source_repo=source_repo)


def _write_stamp(binary, observed=None, build_started=None,
                 build_finished=None, source_before=None, source_repo=None,
                 deps_before=None, configure_started=None):
    """Stamp writer. Coupling requires `observed is _COUPLING_OBSERVED`.

    Private, and identity-checked rather than value-checked, so the only
    way to reach a coupled stamp is `build_and_stamp` — which captures
    every argument below between the build steps it describes.

    Coupling remains a REQUEST even from there: granted only if the
    binary's mtime falls inside [build_started, build_finished] and the
    source and dependency trees are unchanged across the build. That
    verification exists because the first version trusted its caller,
    and the caller was wrong within a day: build.sh ran an incremental
    `cmake --build` that rebuilt nothing, then stamped the pre-existing
    binary "build-coupled" — recording the CURRENT checkout
    (8d0320d-dirty) as its source when it had actually been built from
    15a1926 an hour earlier.
    """
    coupled = observed is _COUPLING_OBSERVED
    binary = os.path.realpath(binary)
    build_dir = os.path.dirname(binary)
    # An out-of-tree build dir has no source above it, so inferring the
    # repo from the build path silently yields None — and an earlier
    # version still granted coupling in that state, recording
    # "source": null. The repo is therefore passed in explicitly, with
    # inference only as a fallback.
    src_repo = source_repo or _toplevel(os.path.dirname(build_dir))
    source_now = _repo_state(src_repo)
    fp_now = source_fingerprint(src_repo)
    deps_now = dep_fingerprints(build_dir)
    dep_bad = _dep_problems(deps_before, deps_now)

    granted, why = False, None
    if not coupled:
        why = "not requested — manual stamp, no build was observed"
    elif build_started is None or build_finished is None:
        why = "no build window supplied, so the binary cannot be tied to a build"
    elif fp_now is None:
        why = ("source tree could not be identified (no git repo at "
               f"{src_repo!r}), so nothing can be attributed to it")
    elif source_before is None:
        why = ("no pre-build source fingerprint supplied, so the source "
               "cannot be shown to have been stable across the build")
    elif source_before.get("fingerprint") != fp_now["fingerprint"]:
        why = ("source tree changed during the build: fingerprint "
               f"{source_before.get('fingerprint','?')[:12]} -> "
               f"{fp_now['fingerprint'][:12]}")
    elif dep_bad:
        why = ("dependency trees do not support coupling: "
               f"{'; '.join(dep_bad)}")
    else:
        # Compared at SECOND granularity on both sides. `date +%s`
        # truncates while st_mtime carries a fraction, so a file linked
        # at ...142.53 would fail a naive `<= 142.0` and the check would
        # reject every genuine build — which it did on first run.
        mtime = int(os.stat(binary).st_mtime)
        lo, hi = int(build_started), int(build_finished)
        if not (lo <= mtime <= hi):
            why = (f"binary mtime {mtime} is outside the build window "
                   f"[{lo}, {hi}] — the link step did not produce this "
                   f"file during this build")
        else:
            granted = True

    stamp = {
        "coupled_to_build": granted,
        "coupling_note": why,
        "coupling_evidence": (
            "captured in-process by provenance.build_and_stamp"
            if coupled else
            "none — manual stamp, no build was observed"),
        "coupling_caveat": (
            "workflow verification, not tamper-proof provenance: this "
            "guards against accidental false attribution, not against a "
            "local user who wants a false stamp"),
        "build_window": ([int(build_started), int(build_finished)]
                         if build_started and build_finished else None),
        # The window above covers the LINK, which is what the mtime is
        # checked against. Configure is bracketed separately by the
        # source fingerprint, because CMake inputs changing mid-configure
        # would produce build files that no recorded source describes.
        "configure_started": (int(configure_started)
                              if configure_started else None),
        "binary_sha256": _sha256(binary),
        "binary_mtime": int(os.stat(binary).st_mtime),
        "source": source_now,
        "source_fingerprint": fp_now,
        "source_clean": bool(fp_now and fp_now["clean"]),
        # What the compiler read, as opposed to what the tree says. On a
        # dirty tree the commit describes neither.
        "compiled_input": compiled_fingerprint(src_repo),
        "dirty_compiled_paths": sorted(
            p for p in ((source_now or {}).get("dirty_paths") or [])
            if _under_compiled(p)),
        "dep_fingerprints": deps_now,
        # Reported only when there was a pre-build capture to compare
        # against. `true` here means every expected dependency was
        # identifiable at both ends AND unchanged — never merely "two
        # nulls matched", which is what the first version said when
        # `_deps` was absent entirely.
        "dep_fingerprints_stable": (None if deps_before is None
                                    else not dep_bad),
        "dep_fingerprint_problems": dep_bad or None,
        "build": _build_config(build_dir),
        "host": {"kernel": platform.release(),
                 "compiler": _run("gcc --version | head -1"),
                 # Hashed /etc/machine-id: distinguishes machines without
                 # publishing the identifier. Matching kernel and
                 # compiler strings prove a matching environment, not a
                 # shared host.
                 "host_id": _host_id()},
        "stamped_at": _run("date -u +%Y-%m-%dT%H:%M:%SZ"),
    }
    path = os.path.join(build_dir, STAMP_NAME)
    with open(path, "w") as f:
        json.dump(stamp, f, indent=1, sort_keys=True)
    return path


def _echo(msg):
    # Flushed, because the build subprocesses inherit stdout and write
    # to it unbuffered: without this, every progress line appears after
    # the step it announces whenever output is piped.
    print(msg, flush=True)


def build_and_stamp(repo, build_dir, build_type="Release", cmake_args=(),
                    echo=_echo):
    """Configure, build and stamp in ONE process. The only coupled path.

    Every fact that distinguishes a build from a pre-existing file is
    captured here, between the steps it describes, and handed straight
    to `stamp_build` without ever crossing a process boundary. Nothing
    is passed in by a caller who could be describing a build that never
    happened.

    Order matters, and each step is load-bearing:

      1. fingerprint the source BEFORE configure — CMake reads the tree
         at configure time, so a mid-configure edit yields build files
         inconsistent with the source of record
      2. configure
      3. fingerprint the fetched deps, which configure has just
         materialized
      4. DELETE the binary, so "did this build produce this file?" has a
         real answer instead of depending on whether CMake felt anything
         was out of date. An incremental no-op build promoting an
         hour-old binary is not a hypothetical; it is what happened.
      5. build, bracketed by timestamps
      6. re-fingerprint source and deps, then stamp

    `--build-dir` outside the repo is supported: `repo` is explicit
    here, so an out-of-tree build still attributes to real source
    instead of recording `"source": null`.
    """
    repo = os.path.realpath(repo)
    build_dir = os.path.realpath(build_dir)
    binary = os.path.join(build_dir, BINARY_NAME)

    src_before = source_fingerprint(repo)
    if src_before is None:
        raise SystemExit(f"not a git repository, so nothing can be "
                         f"attributed to it: {repo}")

    configure_started = time.time()
    echo(f"==> configuring ({build_type}, SS_TESTS=OFF) in {build_dir}")
    # SS_TESTS is set EXPLICITLY, not left to the cache: a stale cache
    # can carry a previous ON, and upstream's doctest suite does not
    # build on master (audit D6 — doctest is never fetched), failing the
    # build for reasons unrelated to the daemon under test.
    _must(["cmake", "-S", repo, "-B", build_dir,
           f"-DCMAKE_BUILD_TYPE={build_type}",
           "-DSS_CPPTRACE_STACKTRACE=OFF", "-DSS_TESTS=OFF", *cmake_args])

    deps_before = dep_fingerprints(build_dir)

    try:
        os.unlink(binary)
    except FileNotFoundError:
        pass

    started = time.time()
    echo("==> building (forced relink)")
    _must(["cmake", "--build", build_dir, "-j"])
    finished = time.time()

    if not os.access(binary, os.X_OK):
        raise SystemExit(f"build produced no executable at {binary}")

    echo("==> stamping provenance (coupling verified, not asserted)")
    return _write_stamp(binary, observed=_COUPLING_OBSERVED,
                        build_started=started, build_finished=finished,
                        configure_started=configure_started,
                        source_before=src_before, source_repo=repo,
                        deps_before=deps_before)


def _must(cmd):
    """Run a build step, letting its output through, failing loudly."""
    r = subprocess.run(cmd)
    if r.returncode != 0:
        raise SystemExit(f"{cmd[0]} failed ({r.returncode}): "
                         f"{' '.join(cmd)}")


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
    if stamp.get("coupled_to_build"):
        return {"status": "verified stamp (build-coupled)", **stamp}
    return {"status": "verified stamp (manual) — attests the binary is "
                      "unchanged since stamping; does NOT establish that the "
                      "recorded source produced it, since stamping was not "
                      "run as part of the build", **stamp}


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

    stamped = _built_from(binary, build_dir)
    live_build = _build_config(build_dir)

    return {
        "binary": {
            "path": binary,
            "sha256": _sha256(binary),
            "size": size,
            "mtime": mtime,
            # what actually built it, or an explicit admission of not
            # knowing — never the checkout that happened to be present
            "built_from": stamped,
        },
        # Read LIVE, at measurement time — not necessarily the config
        # the binary was built with. Named so it cannot be confused with
        # `binary.built_from.build`, which is the stamped one, and
        # explicitly diffed below so a disagreement is reported rather
        # than left for a reader to notice.
        "build_tree_at_measurement": live_build,
        "build_config_matches_stamp": _config_match(stamped, live_build),
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


def _report(path):
    with open(path) as f:
        st = json.load(f)
    print(f"{path}\n  coupled_to_build={st['coupled_to_build']}"
          f"  ({st['coupling_note']})")
    return st


if __name__ == "__main__":
    import sys

    def sval(name, default=None):
        return (sys.argv[sys.argv.index(name) + 1]
                if name in sys.argv else default)

    # NOTE ON THE MISSING FLAGS. There is deliberately no way to hand
    # coupling evidence to this CLI. `--started`, `--finished` and
    # `--source-before` used to exist for build.sh to pass across a
    # process boundary, and that boundary was the vulnerability: the
    # facts arrived as strings from whoever was calling, so a copied
    # binary with a public fingerprint and its own mtime earned a full
    # coupled stamp. Coupling now comes only from `build`, which
    # observes the build it is describing. Do not re-add them.
    # Fail loudly rather than silently downgrading. A caller using the
    # old syntax would otherwise get a manual stamp and exit 0 — a
    # script that keeps "succeeding" while quietly no longer proving
    # what it used to, which is the exact failure shape this module
    # exists to prevent.
    _gone = [f for f in ("--coupled", "--started", "--finished",
                         "--source-before", "--require-coupled")
             if f in sys.argv]
    if _gone:
        print(f"ERROR: {', '.join(_gone)} removed — coupling evidence is no "
              f"longer accepted from a caller (a copied binary plus a public "
              f"fingerprint earned a coupled stamp this way).\n"
              f"Use `provenance.py build`, which observes the build it "
              f"describes.", file=sys.stderr)
        sys.exit(2)

    if len(sys.argv) >= 3 and sys.argv[1] == "fingerprint":
        # Read-only diagnostic: prints current source identity. Safe to
        # expose precisely because nothing grants coupling any more.
        fp = source_fingerprint(sys.argv[2])
        if fp is None:
            print("ERROR: not a git repository: " + sys.argv[2],
                  file=sys.stderr)
            sys.exit(1)
        print(json.dumps(fp))
    elif len(sys.argv) >= 2 and sys.argv[1] == "build":
        repo = os.path.realpath(sval("--repo", _toplevel(os.path.abspath(
            __file__)) or "."))
        path = build_and_stamp(
            repo,
            sval("--build-dir", os.path.join(repo, "build")),
            build_type=sval("--build-type", "Release"))
        st = _report(path)
        # Belongs here rather than in a wrapper: the point of a single
        # process is that a failed coupling cannot be papered over
        # downstream.
        if not st["coupled_to_build"]:
            sys.exit(1)
    elif len(sys.argv) >= 3 and sys.argv[1] == "stamp":
        # Manual only, and says so in the record: attests the binary is
        # unchanged since stamping, never that the recorded source
        # produced it.
        _report(stamp_build(sys.argv[2], source_repo=sval("--source-repo")))
    else:
        print("usage: provenance.py build [--repo R] [--build-dir D] "
              "[--build-type T]\n"
              "         configure, build and stamp in one process; the "
              "only way to obtain a coupled stamp\n"
              "       provenance.py stamp <binary> [--source-repo R]\n"
              "         manual stamp only — records no coupling claim\n"
              "       provenance.py fingerprint <repo>\n"
              "         print current source identity (diagnostic)")
        sys.exit(2)

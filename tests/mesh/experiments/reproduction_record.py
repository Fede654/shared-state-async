"""Commit the evidence behind a "this source produces this binary" claim.

WHY THIS FILE EXISTS

`tests/mesh/README.md` says the twelve `results/dynamics/` records are
legacy pre-coupling — hand-stamped before build coupling was verified —
but that their attribution is confirmed a stronger way, by reproduction:
two builds this tool watched, one in-tree and one in a fresh out-of-tree
directory, both produce the exact sha256 those records name, from
sources byte-identical to the commit they cite.

External review pointed out that the claim was unauditable. Both stamps
lived where nobody else could read them — `build/BUILD_PROVENANCE.json`
is gitignored and the out-of-tree stamp sat in a scratch directory that
no longer exists. A future clone could re-run the experiment but could
not check the historical claim, which is the difference between evidence
and an assertion about evidence.

So the record is generated here and committed. It holds both stamps
whole, the source-equivalence check between the two commits, and the
agreement tests that give the claim its force.

To regenerate, build twice into DIFFERENT directories, then pass both
stamps and the revision those builds actually came from:

    tests/mesh/build.sh
    tests/mesh/build.sh --build-dir /tmp/ss-repro
    python3 tests/mesh/experiments/reproduction_record.py \
        --stamp build/BUILD_PROVENANCE.json \
        --stamp /tmp/ss-repro/BUILD_PROVENANCE.json \
        --equivalent 15a1926 "$(git rev-parse HEAD)" \
        --out tests/mesh/experiments/results/REPRODUCTION-b5b3de0a.json

Every argument is mandatory. `--equivalent`'s second revision is checked
against the stamps and refused if it does not match: this docstring
previously carried an example naming a commit the script's own check
would reject, which is the failure it exists to catch, printed as
instructions.

WHAT IT PROVES, AND WHAT IT DOES NOT

That the recorded source produces the recorded binary, reproducibly,
across independent build trees on one machine. NOT bit-for-bit
reproducibility across environments, compilers or dates — the builds run
back to back with the same toolchain. NOT that this is how the binary
in the original experiment came to exist on 14 August: reproduction
speaks to the source→binary mapping, never to the history of a
particular file. And like everything in `provenance.py`, it is workflow
verification; it would not survive someone determined to fake it.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import provenance as p                                   # noqa: E402

# The sources that actually reach the compiler. If these are identical
# between two commits, a difference anywhere else in the tree (docs,
# tests, results) cannot change the binary. Imported rather than
# redefined so the stamp's `compiled_input` and this equivalence check
# can never drift apart into two different notions of "compiled".
COMPILED_PATHS = p.COMPILED_PATHS


def source_equivalence(repo, rev_a, rev_b):
    """Whether two commits present the compiler with the same input."""
    changed = p._git_raw(
        repo, f"diff --name-only {rev_a} {rev_b} -- "
              f"{' '.join(COMPILED_PATHS)}") or ""
    files = [f for f in changed.splitlines() if f]
    return {
        "rev_a": p._git(repo, f"rev-parse {rev_a}"),
        "rev_b": p._git(repo, f"rev-parse {rev_b}"),
        "paths_compared": list(COMPILED_PATHS),
        "differing_files": files,
        "equivalent": not files,
        "note": ("byte-identical compiled sources: any other difference "
                 "between these commits cannot affect the binary"
                 if not files else
                 "compiled sources DIFFER — the binaries are not "
                 "expected to match"),
    }


# The build configuration fields that must agree before two builds can
# be called a reproduction of each other. Compared normalized (as JSON
# with sorted keys) so field order never masks a difference.
CONFIG_KEYS = ("cmake_cxx_compiler", "cmake_build_type", "cmake_cxx_flags",
               "cmake_cxx_flags_release", "ss_options", "dep_commits")


def _norm(obj):
    return json.dumps(obj, sort_keys=True)


def build_record(repo, stamp_paths, rev_a, rev_b):
    stamps = []
    for path in stamp_paths:
        with open(path) as f:
            stamps.append(json.load(f))

    shas = {s.get("binary_sha256") for s in stamps}
    coupled = [bool(s.get("coupled_to_build")) for s in stamps]
    dep_sets = [_norm(s.get("build", {}).get("dep_commits")) for s in stamps]

    # DEP COMMITS ARE NOT DEP CONTENTS. Two builds can name the same
    # libretroshare commit with different bytes on disk, which is the
    # whole reason `provenance.dep_fingerprints` exists — so the
    # fingerprints are compared here too, not just the commits they
    # summarize.
    dep_fps = [_norm(s.get("dep_fingerprints")) for s in stamps]
    dep_stable = [s.get("dep_fingerprints_stable") is True for s in stamps]

    configs = [_norm({k: s.get("build", {}).get(k) for k in CONFIG_KEYS})
               for s in stamps]

    # A SHARED COMMIT IS NOT SHARED SOURCE. Dirty builds are supported,
    # so two stamps can name 3a59025 and still have compiled different
    # bytes; `source_equivalence` compares committed revisions and
    # cannot see it. So the fingerprints are compared directly — the
    # whole-tree one, and the compiled-input one that answers the
    # narrower question of what the compiler actually read.
    src_fps = [((s.get("source_fingerprint") or {}).get("fingerprint"))
               for s in stamps]
    comp_fps = [((s.get("compiled_input") or {}).get("fingerprint"))
                for s in stamps]
    # `dirty_compiled_paths` is None when the source could not be
    # identified at stamp time. None is not "clean" — an unlisted field
    # would otherwise satisfy `not dirty_compiled` and pass the gate.
    dirty_known = all(isinstance(s.get("dirty_compiled_paths"), list)
                      for s in stamps)
    dirty_compiled = sorted({d for s in stamps
                             for d in (s.get("dirty_compiled_paths") or [])})

    # Two stamps are not two builds. Passing the same file twice used to
    # produce "2 builds, agree=True" with one directory in the record —
    # a reproduction claim resting on a single build compared to itself.
    build_dirs = sorted({s.get("build", {}).get("build_dir") for s in stamps})

    # `rev_b` is checked against the stamps rather than trusted. Writing
    # this record the first time, I passed the commit the claim had been
    # about while the stamps carried a newer HEAD — asserting
    # equivalence for a commit that built nothing here, which is the
    # same false attribution the whole module exists to prevent, in the
    # tool meant to document its absence.
    stamped_commits = {(s.get("source") or {}).get("commit") for s in stamps}
    resolved_b = p._git(repo, f"rev-parse {rev_b}")
    consistent = stamped_commits == {resolved_b}

    equiv = source_equivalence(repo, rev_a, rev_b)

    hosts = {_norm(s.get("host")) for s in stamps}
    host_ids = {(s.get("host") or {}).get("host_id") for s in stamps}
    same_env = len(hosts) == 1
    same_machine = len(host_ids) == 1 and None not in host_ids
    stamped_at = sorted(s.get("stamped_at") for s in stamps if s.get("stamped_at"))

    return {
        "what_this_is": (
            "Evidence that the recorded source reproducibly builds the "
            "recorded binary, committed so the claim in "
            "tests/mesh/README.md can be audited from a clone rather "
            "than taken on trust."),
        "agreement": {
            "binary_sha256": (list(shas)[0] if len(shas) == 1 else None),
            "all_builds_agree": len(shas) == 1,
            "all_builds_coupled": all(coupled),
            "dep_commits_agree": len(set(dep_sets)) == 1,
            "dep_fingerprints_agree": len(set(dep_fps)) == 1,
            "dep_fingerprints_stable_each": all(dep_stable),
            "build_configs_agree": len(set(configs)) == 1,
            "source_fingerprints_present": all(src_fps),
            "source_fingerprints_agree": len(set(src_fps)) == 1,
            "compiled_inputs_present": all(comp_fps),
            "compiled_inputs_agree": len(set(comp_fps)) == 1,
            "compiled_input_fingerprint": (comp_fps[0] if len(set(comp_fps)) == 1
                                           else None),
            "dirty_compiled_known": dirty_known,
            "no_dirty_compiled_paths": dirty_known and not dirty_compiled,
            "dirty_compiled_paths": dirty_compiled,
            "source_equivalent": equiv["equivalent"],
            "build_count": len(stamps),
            "distinct_build_dirs": build_dirs,
            "independent_build_trees": len(build_dirs) >= 2,
            "stamped_source_commits": sorted(c for c in stamped_commits if c),
            "stamps_match_rev_b": consistent,
        },
        "source_equivalence": equiv,
        # Stated rather than left to be inferred from the stamps. The
        # scope of this evidence is narrow and the reader should not have
        # to reconstruct it from kernel strings and timestamps.
        # Generated from what the stamps say, never asserted. The
        # previous version computed `same_host_record` and then printed
        # "SAME MACHINE, SAME TOOLCHAIN" regardless of it — a sentence
        # that would have survived stamps from two different hosts.
        "scope": {
            "same_host_record": same_env,
            "same_machine": same_machine,
            "host_ids": sorted(h for h in host_ids if h),
            "hosts": sorted(hosts),
            "stamped_at": stamped_at,
            "note": (
                ("Builds ran on the same machine (matching hashed "
                 "/etc/machine-id) with the same recorded toolchain"
                 if same_machine else
                 "Builds recorded the same environment (kernel and "
                 "compiler strings match), but the host could not be "
                 "confirmed identical"
                 if same_env else
                 "Builds recorded DIFFERENT environments — see `hosts`")
                + ", back to back; see `stamped_at` for the actual "
                  "separation. This is reproducibility across build "
                  "trees, not across environments, compilers or time."),
        },
        "limits": [
            # Generated, for the same reason the scope note is: this
            # sentence said "Same machine" while `same_machine` was
            # computed and not required, so removing host_id from both
            # stamps produced a record reporting same_machine=false that
            # asserted the same-machine limitation anyway.
            (("Same machine (matching hashed /etc/machine-id), same "
              "toolchain" if same_machine else
              "Same recorded environment (matching kernel and compiler "
              "strings; the physical host was NOT confirmed identical)")
             + ", run back to back: this is reproducibility across "
               "independent build trees, NOT bit-for-bit reproducibility "
               "across environments, compiler versions or dates."),
            "Reproduction establishes the source->binary mapping. It does "
            "NOT establish the history of any particular file, including "
            "the binary used in the original 14 August experiment.",
            "Workflow verification, not tamper-proof provenance: this "
            "guards against accidental false attribution, not against "
            "someone determined to fake it.",
        ],
        "stamps": stamps,
    }


if __name__ == "__main__":
    def vals(flag):
        return [sys.argv[i + 1] for i, a in enumerate(sys.argv) if a == flag]

    repo = p._toplevel(os.path.abspath(__file__))
    stamps = vals("--stamp")
    out = (vals("--out") or [os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "results", "REPRODUCTION.json")])[0]
    eq = sys.argv[sys.argv.index("--equivalent") + 1:][:2] \
        if "--equivalent" in sys.argv else []

    if len(stamps) < 2 or len(eq) != 2:
        print("usage: reproduction_record.py --stamp A.json --stamp B.json "
              "--equivalent REV_A REV_B [--out PATH]\n"
              "  Two stamps from DIFFERENT build directories are required "
              "— one build is not a reproduction, and the same stamp "
              "twice is not two builds.\n"
              "  REV_B must be the commit those builds carry; it is "
              "checked against the stamps.", file=sys.stderr)
        sys.exit(2)

    rec = build_record(repo, stamps, eq[0], eq[1])
    a = rec["agreement"]
    # Conditions live in `agreement` and `scope`; checked from one merged
    # view so a required key silently reading as None (and passing as
    # falsy, or worse, being forgotten) cannot happen.
    checks = {**rec["scope"], **a}

    # EVERY condition the output claims, checked BEFORE writing. A
    # refused record that still lands on disk is a file someone reads
    # later without its exit status; a record that asserts agreement it
    # never tested is worse than no record at all.
    required = [
        ("independent_build_trees",
         "the stamps come from one build directory — two stamps are not "
         "two builds, and comparing a build to itself reproduces nothing"),
        ("stamps_match_rev_b",
         f"stamps were built from {a['stamped_source_commits']}, not "
         f"{eq[1]}, so the equivalence check would describe a commit that "
         f"built none of them"),
        ("all_builds_agree", "the builds produced different binaries"),
        ("all_builds_coupled", "at least one stamp is not build-coupled"),
        ("source_equivalent",
         "the compiled sources differ between the two revisions, so "
         "matching binaries would not be evidence of anything"),
        ("source_fingerprints_present",
         "at least one stamp has no source fingerprint, so its source "
         "cannot be compared to the other's at all"),
        ("source_fingerprints_agree",
         "the stamps share a commit but their source trees differ — a "
         "shared commit is not shared source on a dirty tree"),
        ("compiled_inputs_present",
         "at least one stamp records no compiled-input fingerprint, so "
         "what the compiler read cannot be compared"),
        ("compiled_inputs_agree",
         "the builds compiled different bytes under "
         f"{'/'.join(COMPILED_PATHS)}"),
        ("dirty_compiled_known",
         "at least one stamp does not record which compiled sources were "
         "dirty, so cleanliness cannot be checked — absence is not "
         "cleanliness"),
        ("no_dirty_compiled_paths",
         "compiled sources are locally modified, so the commit the "
         "equivalence check names does not describe what was built"),
        ("same_host_record",
         "the stamps record different host environments, which the "
         "scope note would otherwise paper over"),
        ("same_machine",
         "the builds cannot be shown to have run on the same machine "
         "(missing or differing hashed /etc/machine-id), while the "
         "record's own limits claim they did"),
        ("build_configs_agree",
         "the build configurations differ, so the binaries were not "
         "produced under comparable conditions"),
        ("dep_commits_agree", "the builds used different dependency commits"),
        ("dep_fingerprints_agree",
         "the dependency trees differ in content despite their commits"),
        ("dep_fingerprints_stable_each",
         "at least one build did not verify its dependencies as stable "
         "across the build"),
    ]
    missing = [k for k, _ in required if k not in checks]
    if missing:
        print(f"REFUSED — nothing written: acceptance conditions "
              f"{missing} are not computed by build_record; the record "
              f"would claim what was never checked.", file=sys.stderr)
        sys.exit(1)
    failed = [(k, why) for k, why in required if not checks[k]]
    if failed:
        print("REFUSED — nothing written:", file=sys.stderr)
        for k, why in failed:
            print(f"  {k}: {why}", file=sys.stderr)
        sys.exit(1)

    with open(out, "w") as f:
        json.dump(rec, f, indent=1, sort_keys=True)
    print(f"{out}\n  {a['build_count']} builds, agree={a['all_builds_agree']}, "
          f"coupled={a['all_builds_coupled']}, "
          f"deps_agree={a['dep_commits_agree']}, sha256={a['binary_sha256']}\n"
          f"  source equivalence {eq[0]}..{eq[1]}: "
          f"{rec['source_equivalence']['equivalent']}")

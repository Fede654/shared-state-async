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
agreement test that gives the claim its force. Regenerate it with:

    python3 tests/mesh/experiments/reproduction_record.py \
        --out results/REPRODUCTION-b5b3de0a.json \
        --stamp <build-dir>/BUILD_PROVENANCE.json \
        --stamp <other-build-dir>/BUILD_PROVENANCE.json \
        --equivalent 15a1926 cad60a8

WHAT IT PROVES, AND WHAT IT DOES NOT

That the recorded source produces the recorded binary, reproducibly,
across independent build trees. NOT that this is how the binary in the
original experiment came to exist on 14 August — reproduction speaks to
the source→binary mapping, never to the history of a particular file.
And like everything in `provenance.py`, it is workflow verification: it
would not survive someone determined to fake it.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import provenance as p                                   # noqa: E402

# The sources that actually reach the compiler. If these are identical
# between two commits, a difference anywhere else in the tree (docs,
# tests, results) cannot change the binary.
COMPILED_PATHS = ("src", "include", "app", "CMakeLists.txt")


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


def build_record(repo, stamp_paths, rev_a, rev_b):
    stamps = []
    for path in stamp_paths:
        with open(path) as f:
            stamps.append(json.load(f))

    shas = {s.get("binary_sha256") for s in stamps}
    coupled = [bool(s.get("coupled_to_build")) for s in stamps]
    dep_sets = [json.dumps(s.get("build", {}).get("dep_commits"),
                           sort_keys=True) for s in stamps]

    # `rev_b` is checked against the stamps rather than trusted. Writing
    # this record the first time, I passed the commit the claim had been
    # about while the stamps carried a newer HEAD — asserting
    # equivalence for a commit that built nothing here, which is the
    # same false attribution the whole module exists to prevent, in the
    # tool meant to document its absence.
    stamped_commits = {(s.get("source") or {}).get("commit") for s in stamps}
    resolved_b = p._git(repo, f"rev-parse {rev_b}")
    consistent = stamped_commits == {resolved_b}

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
            "build_count": len(stamps),
            "distinct_build_dirs": sorted(
                {s.get("build", {}).get("build_dir") for s in stamps}),
            "stamped_source_commits": sorted(c for c in stamped_commits if c),
            "stamps_match_rev_b": consistent,
        },
        "source_equivalence": source_equivalence(repo, rev_a, rev_b),
        "limits": [
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
              "  at least two independent build stamps are required — one "
              "build is not a reproduction", file=sys.stderr)
        sys.exit(2)

    rec = build_record(repo, stamps, eq[0], eq[1])
    a = rec["agreement"]

    # Checked BEFORE writing. A refused record that still lands on disk
    # is a file someone will later read without its exit status.
    if not a["stamps_match_rev_b"]:
        print(f"REFUSED: stamps were built from "
              f"{a['stamped_source_commits']}, not {eq[1]} — the "
              f"equivalence check would describe a commit that built "
              f"none of them. Nothing written.", file=sys.stderr)
        sys.exit(1)
    # A record asserting agreement that does not hold is worse than none.
    if not (a["all_builds_agree"] and a["all_builds_coupled"]
            and a["dep_commits_agree"]):
        print(f"REFUSED: builds disagree (agree={a['all_builds_agree']}, "
              f"coupled={a['all_builds_coupled']}, "
              f"deps={a['dep_commits_agree']}). Nothing written.",
              file=sys.stderr)
        sys.exit(1)

    with open(out, "w") as f:
        json.dump(rec, f, indent=1, sort_keys=True)
    print(f"{out}\n  {a['build_count']} builds, agree={a['all_builds_agree']}, "
          f"coupled={a['all_builds_coupled']}, "
          f"deps_agree={a['dep_commits_agree']}, sha256={a['binary_sha256']}\n"
          f"  source equivalence {eq[0]}..{eq[1]}: "
          f"{rec['source_equivalence']['equivalent']}")

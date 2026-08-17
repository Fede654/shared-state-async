# Prepared PR to javierbrk — NOT YET SENT

Mechanics (his fork has issues disabled, so a PR is the conversation
channel): base `javierbrk/shared-state-async:merge_with_version`, head
= a branch containing exactly one commit that adds
`doc/findings-altermundi.md` (a copy of `doc/briefing-javierbrk.md`
from this repo). No code is patched — the diff is the document, the PR
body below is the comment.

---

## Title

Findings from independent testing of merge_with_version (no code
changes — test results and rollout notes)

## Body

Hola Javier — we (AlterMundi fork) revived and extended the mesh test
suite against real daemons and ran it against both v1 master and this
branch at `22a20aab`. This PR changes no code: it adds one document
with four findings, each backed by a runnable test. Summary:

**1. Your fix works.** T1 (stale echo vs fresh own data) is RED on v1
master and GREEN here. The version counter resolves the divergence
class that motivated it, and our protocol spec draft specifies this
branch's algorithm as the intended replacement for the v1 conflict
rule.

**2. Rollout blocker (T23).** Entries from non-versioned peers are not
read as version 0 — deserialization stops at the first entry it cannot
read and `toStateSlice()` discards the failure, so an upgraded node
silently learns nothing from a v1 peer. Asymmetric: upgraded nodes
cannot learn v1-originated state, while v1 receivers still accept v2
entries. Needs an explicit mixed-fleet decision before deployment.

**3. Reboot recovery can promote stale data (T11).** A node rebooted
with wiped state, offered gen 3 (v3) then gen 7 (v7) of its own key,
kept gen 3 and promoted it to version 8 — the recovery leapfrog fires
even when the local entry was itself just echo-inserted. Our
executable model verifies a contained fix (`v2r`): apply recovery only
to entries locally inserted since boot.

**4. Shared with v1: expired authors resurrect their own entries
(T24).** The missing-key insert runs before any authorship/version
rule on both binaries — measured RED here too, with a versioned echo
so deserialization can't mask it. We also measured it happening with
nothing injected (peer echoes only, author publishing once) in every
gated lab run; details and limits are in the document. The fix is a
design decision we'd like to make together: distinguishing "expired
since boot" from "never seen" needs per-key retained history (a
tombstone set/map, or a persisted author epoch) — the deleted entry
itself can't carry a marker.

Measured matrix (our runner's expectations are master-relative; table
is the reference):

| test | v1 master | this branch @ `22a20aab` |
|---|---|---|
| T1 | RED | **GREEN** |
| T11 | RED | RED |
| T23 | GREEN | RED |
| T24 | RED | RED |

Reproduce (Linux, unprivileged userns, no root): build both binaries,
then `python3 tests/mesh/run_mesh_tests.py [--bin <path>] T1 T11 T23
T24` — full commands in the document.

Everything is offered to help this branch land, and we're happy to
follow up however is useful — splitting findings, pairing on the
rollout question, or turning the spec draft
(`doc/protocol-spec-DRAFT.md` in our fork) into a shared reference.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

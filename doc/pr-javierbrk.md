# Prepared Draft PR to javierbrk — NOT YET SENT

Mechanics (his fork has issues disabled, so a PR is the conversation
channel; he is already expecting collaboration, so this opens directly
as a **Draft PR** rather than another preliminary message):

- base: `javierbrk/shared-state-async:merge_with_version`
- head: a topic branch created from `22a20aabdf4a02b7bdc3f18508d296fa5372808b`
  containing exactly one commit that adds
  `doc/findings-altermundi.md` — a copy of this repo's
  `doc/findings-altermundi-external.md` (the externalized document:
  pinned absolute links into the AlterMundi fork, no repo-relative
  claims; the internal `briefing-javierbrk.md` is NOT copied).
- No code is patched. The PR is opened as **Draft**, explicitly a
  discussion artifact that need not be merged.
- Follow-up code PRs come separately, after agreement: (1) T23
  interoperability, (2) T11/`v2r`, (3) T24 design + test once
  tombstone-vs-epoch semantics are agreed.

---

## Title

Findings from independent testing of merge_with_version (Draft, for
discussion — no code changes)

## Body

Hola Javier — we (AlterMundi fork) revived and extended the mesh test
suite against real daemons and ran it against both v1 master and this
branch at `22a20aab`. **This is a Draft PR for discussion: it changes
no code, adds a single findings document, and does not need to be
merged.** Every claim in the document links to a runnable test or a
committed, provenance-stamped measurement in our fork. Summary:

**1. Your fix works.** T1 (stale echo vs fresh own data) is RED on v1
master and GREEN on this branch. The version counter resolves the
divergence class that motivated it, and our protocol spec draft
specifies this branch's algorithm as the intended replacement for the
v1 conflict rule.

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
rule on both binaries — measured RED on this branch too, with a
versioned echo so deserialization can't mask it. We also measured it
happening with nothing injected (peer echoes only, author publishing
once) in every gated lab run; details, uncertainty intervals, and
limits are in the document. The fix is a design decision we'd like to
make together: distinguishing "expired since boot" from "never seen"
needs per-key retained history (a tombstone set/map, or a persisted
author epoch) — the deleted entry itself can't carry a marker.

Measured matrix (our runner's expectations are master-relative; the
table is the reference):

| test | v1 master | this branch @ `22a20aab` |
|---|---|---|
| T1 | RED | **GREEN** |
| T11 | RED | RED |
| T23 | GREEN | RED |
| T24 | RED | RED |

Machine-readable run records for both binaries (verdicts, binary
hashes, build provenance) are committed in our fork; the document
links them, alongside full reproduction commands (Linux, unprivileged
userns, no root).

If this is useful, we'd follow with separate focused PRs: T23
interoperability semantics, the `v2r` guard, and the T24 design+test
once we agree on tombstone vs epoch. Happy to split, reshape, or
discuss any of it here first.

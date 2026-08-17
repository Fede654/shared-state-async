# Analysis index

**Current, citable: [`8b6f263ed5566699/`](8b6f263ed5566699/)** — the
only analysis directory kept in the tree. Its id is the first 16 hex of
a compound SHA-256 over the immutable record inputs, every pipeline
stage script, and the governing plan commits; the full digest is in its
`MANIFEST.json`, and the extractor refuses to overwrite a directory
whose manifest carries a different full digest.

Superseded analyses live in git history only, never in the tree, so a
stale STATS.md can never be quoted by accident:

- `3641f7ed` — first analysis (commit `6ed0f42`): pre-amendment-2
  columns and estimands. Numbers identical to current.
- `fe2760d3` — second (commit `ed980dc`): amendment-2 estimands, but
  figures interpolated across expiry gaps, the status line overclaimed
  "predeclared", the id was a 32-bit prefix, and SVGs were
  non-deterministic. Numbers identical to current.

Plan status for everything here (amendment 1 §1, verbatim intent): run
schedule committed before collection; analysis plan specified
mid-collection with limited endpoint exposure; amendment 2 and later
corrections were post-outcome. Not preregistration.

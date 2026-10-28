---
name: b4m-artifacts
description: How to leave results for the maintainer in a b4-maintainer session: reports, comparisons, findings, reproducers, proposed diffs, logs, recorded in out/artifacts.toml. Use whenever you produce something worth keeping, with or without a worktree.
---

# Leaving results for the maintainer (b4-maintainer sessions)

The maintainer does not read your transcript to find your results: their tools read your
**output directory** (`out/`, named in the system prompt) and its **artifact manifest**,
`out/artifacts.toml`. They show each artifact beside the series and the workbench, and
can paste its summary into a reply. Results left anywhere else are easy to lose.

## Write files under out/

- One file per result, named by what it is: `out/reports/compare-series.md`,
  `out/diffs/take-b-on-a.diff`, `out/reproducers/race/`, `out/tests/run.log`.
- Plain text (Markdown, a unified diff, a log); no binaries unless a reproducer needs one.
- Without a worktree you cannot commit: a change you propose is a unified diff file
  (`git diff` style, paths from the top of the project), recorded as a `diff`.

## The manifest: out/artifacts.toml

Record every result the maintainer should see:

```toml
[[artifact]]
kind = "analysis"
title = "Which of the two zswap fixes to take"
summary = """Both series fix the same overflow in zswap_store(). Series B also covers
the writeback path and keeps the existing API; take B, drop A's patch 2."""
path = "out/reports/compare-zswap.md"

[[artifact]]
kind = "diff"
title = "Series B patch 1, adjusted for A's renamed helper"
summary = "Applies on top of A 1/3; only the call site of zswap_pool_get() changes."
path = "out/diffs/b1-on-a.diff"
```

- `kind`: one of `finding`, `analysis`, `diff`, `results`, `reproducer`, and (in a
  worktree, on a branch; see the b4m-branches skill) `pull-ahead`, `fixup`, `test`,
  `folded`.
  - `finding`: one problem, with where it is; a `summary` alone may do.
  - `analysis`: a report: a review, a comparison of series, a root cause.
  - `diff`: a proposed change as a diff file (needs `path`).
  - `results`: what a run showed (a log, numbers).
  - `reproducer`: what demonstrates a bug, out of tree.
- Required: `kind`, `title`, `summary`. `diff` needs `path`; the commit kinds need
  `branch`; any `path` is under `out/` and exists.
- Optional: `answers` (the finding it answers, e.g. a Sashiko finding or one of yours),
  `tested` (what you ran and what it showed; "not tested" is a valid answer, a guess is
  not).
- `summary` is one paragraph the maintainer can paste into a cover letter or reply:
  plain, specific, no hedging filler.
- The file is checked strictly: an unknown key or kind, a missing branch or file, or a
  path outside `out/` makes that entry rejected. Keep it valid TOML; rewrite the whole
  file rather than appending fragments.

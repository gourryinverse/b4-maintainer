---
name: b4m-branches
description: How to leave commits and artifacts in a b4-maintainer session worktree so the maintainer's tools can find, review, and send them. Use whenever you commit in the worktree, write a fix, a fixup, or a test, or produce results worth keeping.
---

# Leaving work for the maintainer (b4-maintainer worktrees)

Your worktree is a private clone with the series under review applied. The maintainer
does not read your transcript to find your work: their tools read your **branch names**
and your **artifact manifest**. Work left anywhere else is easy to lose.

## The tree you start with

- The session branch (`b4m/session/<id>`, checked out): the series applied on its base.
  The system prompt names the branch, the base commit, and the series' last commit.
- `git log --oneline <base>..<last>` lists the series. Never rewrite, reorder, amend, or
  drop those commits: they are the posted patches.

## Where commits go

One branch per piece of work, named by what it is. Names are local: they are never sent.

| Branch | For | Start it from |
| --- | --- | --- |
| `b4m/pull-ahead/<slug>` | a bug that is already upstream (pre-existing, not caused by the series) | the **base**: `git switch -c b4m/pull-ahead/<slug> <base>` |
| `b4m/fixup/<slug>` | a bug the series introduces | the **series' last commit**: `git switch -c b4m/fixup/<slug> <last>` |
| `b4m/tests/<slug>` | a test for the series' behaviour (selftest, KUnit) | the series' last commit |

- One bug or one test per branch; `<slug>` is short and descriptive (`foo-refcount`).
- A pull-ahead must not contain the series: if reproducing the bug needs the series,
  still fix it on the base, and test it on top of the series separately (a scratch
  branch you then delete, or a cherry-pick you do not keep).
- Other branches, or commits on the session branch itself, show as "other": the
  maintainer will ask what they are.

## How to write the commits

- Kernel style: `subsystem: imperative summary` under 75 characters, a body that says
  what was wrong, why it matters, and how the change fixes it.
- Pull-aheads carry `Fixes: <12+ hex> ("<subject of the upstream commit>")`.
- Fixups name the series patch they fix in the body (by subject: the series has no
  upstream hashes yet).
- Never add `Signed-off-by`, `Reviewed-by`, `Acked-by`, `Tested-by`, or
  `Co-developed-by`, and no `Assisted-by` either: the maintainer adds attribution when
  they take the work. (A hook strips sign-offs anyway; do not rely on it.)
- Your commits carry the agent identity configured in the tree; do not change it.

## Test before you record

- Build and test each pull-ahead on the base, each fixup and test on top of the series.
- Say what you ran and what it showed in the manifest's `tested`; "not tested" is a
  valid answer, a guess is not.

## The artifact manifest: `out/artifacts.toml`

Record every piece of work the maintainer should see, commits and files alike, in the
session's output directory (`out/`, named in the system prompt):

```toml
[[artifact]]
kind = "pull-ahead"
title = "mm/foo: fix refcount leak on the error path"
summary = """foo_get() takes a reference that the -ENOMEM path never drops, so every
failed allocation leaks the folio. Drop it before returning."""
branch = "b4m/pull-ahead/foo-refcount"
answers = "sashiko:p2/1"
tested = "x86_64 defconfig build; vng boot; tools/testing/selftests/mm/foo: 12/12 pass"

[[artifact]]
kind = "results"
title = "mm selftests on the series"
summary = "All mm selftests pass except khugepaged, which also fails on the base."
path = "out/tests/mm-selftests.log"
```

- `kind`: one of `finding`, `pull-ahead`, `fixup`, `test`, `results`, `reproducer`,
  `analysis`.
- Required: `kind`, `title`, `summary`. `pull-ahead`, `fixup`, and `test` need `branch`
  (an existing branch of the tree); files need `path`, under `out/`.
- Optional: `answers` (the finding it answers, e.g. a Sashiko finding or one of yours),
  `tested`.
- `summary` is one paragraph the maintainer can paste into a cover letter or reply:
  plain, specific, no hedging filler.
- The file is checked strictly: an unknown key or kind, a missing branch, or a path
  outside `out/` makes that entry rejected. Keep it valid TOML; rewrite the whole file
  rather than appending fragments.

## Never

- Push, fetch, or run `b4` (prep, send, or anything else): the maintainer's tools do that.
- Delete or rename branches you did not create, or the session branch.
- Leave work only in the working tree or the stash: uncommitted work is not reviewed.

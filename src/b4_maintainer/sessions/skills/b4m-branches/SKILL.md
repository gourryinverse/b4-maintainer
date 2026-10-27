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
| `b4m/tests/<slug>` | an in-tree test for the series' behaviour (selftest, KUnit) | the series' last commit |

- One bug or one test per branch; `<slug>` is short and descriptive (`foo-refcount`).
- A pull-ahead must not contain the series: if reproducing the bug needs the series,
  still fix it on the base, and test it on top of the series separately (a scratch
  branch you then delete, or a cherry-pick you do not keep).
- Other branches, or commits on the session branch itself, show as "other": the
  maintainer will ask what they are.

## Which kind of test

| Kind | What it is | Where it goes | Manifest `kind` |
| --- | --- | --- | --- |
| Reproducer | demonstrates a bug and validates its fix; never sent upstream as an in-tree test | files under `out/reproducers/<slug>/`, with how to run it | `reproducer` (with `path`) |
| Selftest | an in-tree test that needs a running kernel (a full runtime environment) | `tools/testing/selftests/`, committed on `b4m/tests/<slug>` | `test` (with `branch`) |
| KUnit test | an in-tree unit test that runs without a full runtime environment, on the KUnit API | where the subsystem keeps its KUnit tests, on `b4m/tests/<slug>` | `test` (with `branch`) |

- In-tree tests follow the subsystem's existing tests, so they can be sent upstream.
- A reproducer is written to show the bug, not to kernel test conventions; run it where
  the bug is and where it is fixed, and report both.
- Run selftests in a booted kernel built from the tree; run KUnit tests with
  `tools/testing/kunit/kunit.py run`. If a test could not run, say so in `tested`.

## Building another commit (base, single patches)

To build or boot something other than what the tree has checked out (the base, the
series up to one patch, a pull-ahead on its own), add an extra checkout instead of
switching the session branch back and forth:

```sh
git worktree add --detach <out>/worktrees/<name> <commit>   # <out>: the output directory
```

- Only under `<out>/worktrees/`: the maintainer's tools find these there, count their
  disk use, and remove them with the session's tree. Anywhere else is not cleaned up.
- A kernel checkout plus its build is gigabytes: keep only the ones you still need, and
  remove each when done (`git worktree remove --force <out>/worktrees/<name>`).
- Commits made in an extra checkout belong on a branch named as above (create it there
  with `git switch -c b4m/...`); detached commits are not kept.

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

Record every commit branch and file the maintainer should see in the manifest, as the
b4m-artifacts skill describes. Commit kinds name their branch:

```toml
[[artifact]]
kind = "pull-ahead"
title = "mm/foo: fix refcount leak on the error path"
summary = """foo_get() takes a reference that the -ENOMEM path never drops, so every
failed allocation leaks the folio. Drop it before returning."""
branch = "b4m/pull-ahead/foo-refcount"
answers = "sashiko:p2/1"
tested = "x86_64 defconfig build; vng boot; tools/testing/selftests/mm/foo: 12/12 pass"
```

- `pull-ahead`, `fixup`, and `test` need `branch`: an existing branch of the tree.

## Never

- Push, fetch, or run `b4` (prep, send, or anything else): the maintainer's tools do that.
- Delete or rename branches you did not create, or the session branch.
- Leave work only in the working tree or the stash: uncommitted work is not reviewed.

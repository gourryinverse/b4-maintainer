# SPDX-License-Identifier: GPL-2.0-or-later
"""A session tree's commits by class, what removing it would lose, and removing it.

A session tree is a clone of its own (see adapters/sessiontree.py): removal deletes the
directory, its branch with it; the project is not involved. Plain git, no b4.

Commit classes follow the branch names the b4m-branches skill gives agents: the series
(base..applied), `b4m/pull-ahead/*` (fixes for upstream bugs, on the base),
`b4m/fixup/*` (fixes for the series, on top of it), `b4m/tests/*` (tests for the series,
on top of it); anything else beyond the series is "other".
"""

import os
import pathlib
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional

from .records import Worktree

PREFIXES = (('b4m/pull-ahead/', 'pull-ahead'), ('b4m/fixup/', 'fixup'), ('b4m/tests/', 'test'))
KIND_NAMES = (
    ('pull-ahead', 'pull-ahead', 'pull-aheads'),
    ('fixup', 'fixup', 'fixups'),
    ('test', 'test', 'tests'),
    ('other', 'other commit', 'other commits'),
)


def _count(n: int, one: str, many: str) -> str:
    return f'{n} {one if n == 1 else many}'


@dataclass(frozen=True)
class TreeState:
    changed: tuple[str, ...] = ()  # Uncommitted or untracked files (git status lines).
    commits: tuple[str, ...] = ()  # Commits only in this tree, beyond the series.
    error: str = ''  # Not a readable git tree.
    kinds: tuple[tuple[str, int], ...] = ()  # Commits only here by class, when classed.

    @property
    def clean(self) -> bool:
        return not (self.changed or self.commits or self.error)

    def describe(self) -> str:
        if self.error:
            return self.error
        parts = []
        if self.changed:
            parts.append(_count(len(self.changed), 'uncommitted file', 'uncommitted files'))
        counts = dict(self.kinds)
        classed = [_count(counts[k], one, many) for k, one, many in KIND_NAMES if counts.get(k)]
        if classed:
            parts.append(', '.join(classed) + ' only here')
        elif self.commits:
            parts.append(_count(len(self.commits), 'commit', 'commits') + ' only here')
        return ', '.join(parts) or 'clean'


@dataclass(frozen=True)
class TreeCommit:
    sha: str
    subject: str
    author: str  # Name <email>


@dataclass(frozen=True)
class TreeBranch:
    name: str
    kind: str  # pull-ahead, fixup, test, or other
    commits: tuple[TreeCommit, ...]  # Beyond the base and the series, oldest first.
    problem: str = ''  # Not where its class belongs (e.g. a pull-ahead on the series).


@dataclass(frozen=True)
class TreeCommits:
    series: tuple[TreeCommit, ...] = ()
    branches: tuple[TreeBranch, ...] = ()
    error: str = ''


def _git(path: pathlib.Path, *args: str) -> list[str]:
    run = subprocess.run(
        ['git', *args],
        cwd=path,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'},  # Never touch the index while looking.
    )
    if run.returncode != 0:
        raise OSError(run.stderr.strip() or f'git {args[0]} failed')
    return [line for line in run.stdout.splitlines() if line]


def _commits(path: pathlib.Path, *args: str) -> tuple[TreeCommit, ...]:
    lines = _git(path, 'log', '--reverse', '--format=%H%x00%s%x00%an <%ae>', *args)
    return tuple(TreeCommit(*line.split('\x00', 2)) for line in lines)


def _contains(path: pathlib.Path, commit: str, branch: str) -> bool:
    run = subprocess.run(
        ['git', 'merge-base', '--is-ancestor', commit, branch], cwd=path, capture_output=True
    )
    return run.returncode == 0


def _kind(name: str) -> str:
    return next((kind for prefix, kind in PREFIXES if name.startswith(prefix)), 'other')


def classify_tree(path: pathlib.Path, tree: Worktree) -> TreeCommits:
    """The series, then each branch's own commits (beyond the base, the series, and the
    refs the tree was cloned with) with its class and whether it sits where it should."""
    try:
        series = _commits(path, f'{tree.base}..{tree.applied}')
        names = _git(path, 'for-each-ref', '--format=%(refname:short)', 'refs/heads/')
        branches = []
        for name in names:
            commits = _commits(path, name, '--not', tree.base, tree.applied, '--remotes')
            kind = _kind(name)
            if name != tree.branch and kind == 'other' and not commits:
                continue
            problem = ''
            if kind == 'pull-ahead' and series and _contains(path, series[0].sha, name):
                problem = 'contains the series: pull-aheads start from the base'
            elif kind in ('fixup', 'test') and not _contains(path, tree.applied, name):
                problem = f'not on top of the series: {kind}s start from it'
            branches.append(TreeBranch(name, kind, commits, problem))
    except OSError as ex:
        return TreeCommits(error=str(ex))
    return TreeCommits(series, tuple(branches))


def inspect_tree(
    path: pathlib.Path, applied: str = '', tree: Optional[Worktree] = None
) -> TreeState:
    """Changes in the tree; commits on its branches beyond the refs it was cloned with and
    *applied* (the series' last; without it the series counts too), classed when *tree*
    (the session's record of it) is known."""
    if not (path / '.git').is_dir():
        return TreeState(error='not a git tree')
    try:
        changed = _git(path, 'status', '--porcelain')
        since = ['--not', '--remotes', *([applied] if applied else [])]
        commits = _git(path, 'log', '--format=%h %s', 'HEAD', '--branches', *since)
    except OSError as ex:
        return TreeState(error=str(ex))
    kinds: tuple[tuple[str, int], ...] = ()
    if tree is not None and commits:
        classed = classify_tree(path, tree)
        seen: dict[str, set[str]] = {}
        for branch in classed.branches:
            seen.setdefault(branch.kind, set()).update(c.sha for c in branch.commits)
        kinds = tuple((kind, len(shas)) for kind, shas in seen.items() if shas)
    return TreeState(tuple(changed), tuple(commits), kinds=kinds)


def remove_tree(path: pathlib.Path, directory: pathlib.Path) -> None:
    """Delete a session tree; only ever one directly under *directory*."""
    path, directory = path.resolve(), directory.resolve()
    if path.parent != directory or path.is_symlink():
        raise OSError(f'{path} is not a session tree under {directory}')
    shutil.rmtree(path)

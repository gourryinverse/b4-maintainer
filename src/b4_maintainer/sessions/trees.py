# SPDX-License-Identifier: GPL-2.0-or-later
"""A session tree's commits by class, what removing it would lose, and removing it.

A session tree is a clone of its own (see adapters/sessiontree.py): removal deletes the
directory, its branch with it; the project is not involved. Plain git, no b4.

Commit classes follow the branch names the b4m-branches skill gives agents: the series
(base..applied), `b4m/pull-ahead/*` (fixes for upstream bugs, on the base),
`b4m/fixup/*` (fixes for the series, on top of it), `b4m/tests/*` (tests for the series,
on top of it); anything else beyond the series is "other".
"""

import dataclasses
import os
import pathlib
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional

from .records import Worktree

PREFIXES = (
    ('b4m/pull-ahead/', 'pull-ahead'),
    ('b4m/fixup/', 'fixup'),
    ('b4m/tests/', 'test'),
    ('b4m/folded/', 'folded'),
)
KIND_NAMES = (
    ('pull-ahead', 'pull-ahead', 'pull-aheads'),
    ('fixup', 'fixup', 'fixups'),
    ('test', 'test', 'tests'),
    ('folded', 'folded commit', 'folded commits'),
    ('other', 'other commit', 'other commits'),
)


def _count(n: int, one: str, many: str) -> str:
    return f'{n} {one if n == 1 else many}'


@dataclass(frozen=True)
class ExtraCheckout:
    """A checkout the agent added to its tree (`git worktree add`, e.g. under
    out/worktrees/ to build another commit): removed with the tree."""

    path: str
    head: str
    branch: str = ''


@dataclass(frozen=True)
class TreeState:
    changed: tuple[str, ...] = ()  # Uncommitted or untracked files (git status lines).
    commits: tuple[str, ...] = ()  # Commits only in this tree, beyond the series.
    error: str = ''  # Not a readable git tree.
    kinds: tuple[tuple[str, int], ...] = ()  # Commits only here by class, when classed.
    extras: tuple[str, ...] = ()  # Its extra checkouts' paths.

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
        if self.extras:
            parts.append(_count(len(self.extras), 'extra checkout', 'extra checkouts'))
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


@dataclass(frozen=True)
class TreeItem:
    """Something applied on a workbench tree's base, in order: a posted series (its mail
    *source* at *revision*, applied by b4), or a local *branch* (its commits beyond the
    base, replayed; in *repo*, another session's tree, or else the project)."""

    label: str
    source: str = ''
    revision: int = 0  # 0: the latest b4 finds (a file from disk).
    branch: str = ''
    repo: str = ''
    apply: bool = True  # False: given to the session to read (a discussion, a file).
    file: str = ''  # A file from disk given as it is, not as mail.
    patches: tuple[int, ...] = ()  # A series: only these patches of it (by number).
    single: bool = False  # A branch item: only its tip commit (*branch* names it).


# Where a tree b4 could not apply the series in keeps b4's mbox for the agent: under .git,
# so it is readable but never part of a commit.
AGENT_MBOX = '.git/b4m/series.mbx'


def series_tip(path: pathlib.Path, tree: Worktree) -> str:
    """The series' last commit in the tree: as recorded, or (when the agent applies the
    series itself) the session branch's tip."""
    if not tree.agent_applies:
        return tree.applied
    try:
        return _git(path, 'rev-parse', '--verify', '--quiet', f'refs/heads/{tree.branch}')[0]
    except (OSError, IndexError):
        return tree.applied


def classify_tree(path: pathlib.Path, tree: Worktree) -> TreeCommits:
    """The series, then each branch's own commits (beyond the base, the series, and the
    refs the tree was cloned with) with its class and whether it sits where it should."""
    tree = dataclasses.replace(tree, applied=series_tip(path, tree))
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
            elif kind == 'folded' and series and _contains(path, series[0].sha, name):
                problem = 'contains the series as posted: a folded series starts from the base'
            branches.append(TreeBranch(name, kind, commits, problem))
    except OSError as ex:
        return TreeCommits(error=str(ex))
    return TreeCommits(series, tuple(branches))


def _run(repo: pathlib.Path, *args: str) -> str:
    run = subprocess.run(
        ['git', *args], cwd=repo, capture_output=True, text=True, stdin=subprocess.DEVNULL
    )
    if run.returncode != 0:
        raise OSError(run.stderr.strip() or f'git {args[0]} failed')
    return run.stdout.strip()


def _promisors(project: pathlib.Path) -> list[str]:
    """The project's promisor remotes (a blobless clone fetches missing blobs from them)."""
    run = subprocess.run(
        ['git', 'config', '--get-regexp', r'^remote\..*\.promisor$'],
        cwd=project,
        capture_output=True,
        text=True,
    )
    names = []
    for line in run.stdout.splitlines():
        key, _, value = line.partition(' ')
        if value.strip().lower() in {'true', 'yes', 'on', '1'}:
            names.append(key.removeprefix('remote.').removesuffix('.promisor'))
    return names


def clone_project(project: pathlib.Path, dest: pathlib.Path) -> None:
    """A session tree's start: `git clone --shared --no-checkout` of the project (objects
    borrowed, the project untouched), set up for the sandbox."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run(dest.parent, 'clone', '--quiet', '--shared', '--no-checkout', str(project), str(dest))
    # Blobs a blobless project lacks are fetched from its remotes into the clone.
    promisors = _promisors(project)
    for name in promisors:
        _run(dest, 'config', f'remote.{name}.url', _run(project, 'remote', 'get-url', name))
        _run(dest, 'config', f'remote.{name}.promisor', 'true')
        filt = _run(
            project, 'config', '--default', 'blob:none', f'remote.{name}.partialclonefilter'
        )
        _run(dest, 'config', f'remote.{name}.partialclonefilter', filt)
    if promisors:
        _run(dest, 'config', 'extensions.partialClone', promisors[0])
    settings = {'commit.gpgSign': 'false', 'tag.gpgSign': 'false', 'gc.auto': '0'}
    # Files belong to another uid inside the sandbox: without this git re-hashes the whole
    # tree on its first command there (18 s for a kernel tree).
    settings['core.checkStat'] = 'minimal'
    settings['checkout.workers'] = '0'  # Parallel: a kernel tree checks out in about 20 s.
    for key, value in settings.items():
        _run(dest, 'config', key, value)


def tree_worktrees(path: pathlib.Path) -> list[ExtraCheckout]:
    """The tree's extra checkouts: its linked worktrees, not the tree itself nor b4's
    scratch worktrees."""
    found = []
    current: dict[str, str] = {}
    for line in (*_git(path, 'worktree', 'list', '--porcelain'), ''):
        if line.startswith('worktree '):
            current = {'path': line.removeprefix('worktree ')}
        elif line.startswith('HEAD '):
            current['head'] = line.removeprefix('HEAD ')
        elif line.startswith('branch '):
            current['branch'] = line.removeprefix('branch refs/heads/')
        elif not line and current:
            where = current.get('path', '')
            own = pathlib.Path(where).resolve() == path.resolve()
            if not own and 'b4-shazam-worktree' not in where:
                found.append(
                    ExtraCheckout(where, current.get('head', ''), current.get('branch', ''))
                )
            current = {}
    return found


def _belongs(extra: pathlib.Path, tree: pathlib.Path) -> bool:
    """Whether *extra* is a checkout of *tree*'s repository: its .git file points into
    the tree's .git/worktrees/."""
    dotgit = extra / '.git'
    if extra.is_symlink() or not dotgit.is_file():
        return False
    target = dotgit.read_text().removeprefix('gitdir:').strip()
    return pathlib.Path(target).resolve().is_relative_to((tree / '.git' / 'worktrees').resolve())


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
        extras = [e for e in tree_worktrees(path) if os.path.isdir(e.path)]
        for extra in extras:
            name = pathlib.Path(extra.path).name
            changed += [
                f'{name}: {line}'
                for line in _git(pathlib.Path(extra.path), 'status', '--porcelain')
            ]
    except OSError as ex:
        return TreeState(error=str(ex))
    kinds: tuple[tuple[str, int], ...] = ()
    if tree is not None and commits:
        classed = classify_tree(path, tree)
        seen: dict[str, set[str]] = {}
        for branch in classed.branches:
            seen.setdefault(branch.kind, set()).update(c.sha for c in branch.commits)
        kinds = tuple((kind, len(shas)) for kind, shas in seen.items() if shas)
    return TreeState(
        tuple(changed), tuple(commits), kinds=kinds, extras=tuple(e.path for e in extras)
    )


def remove_tree(path: pathlib.Path, directory: pathlib.Path) -> None:
    """Delete a session tree, only ever one directly under *directory*, with its extra
    checkouts (only those whose .git points back into this tree)."""
    path, directory = path.resolve(), directory.resolve()
    if path.parent != directory or path.is_symlink():
        raise OSError(f'{path} is not a session tree under {directory}')
    try:
        extras = [pathlib.Path(e.path) for e in tree_worktrees(path)]
    except OSError:
        extras = []  # Not a readable repository: only the tree itself goes.
    for extra in extras:
        if extra.is_dir() and _belongs(extra, path):
            shutil.rmtree(extra)
    shutil.rmtree(path)

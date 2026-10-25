# SPDX-License-Identifier: GPL-2.0-or-later
"""What the Branches screen shows: per project, the maintainer bases, the session trees,
and the prep branches, each with its commits (and a tree's artifacts); what a commit or
artifact shows; rebasing a base.

Plain git and files, gathered off the UI thread. Prep branches are b4's (`b4/<name>`),
listed read-only; b4's review branches (`b4/review/*`) are not prep branches.
"""

import pathlib
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional, Sequence

from .adapters.bases import BaseStatus, RebaseResult, base_status, rebase_base
from .sessions.artifacts import Artifact, load_artifacts
from .sessions.records import SessionRecord, session_label
from .sessions.trees import TreeState, classify_tree, inspect_tree
from .workspace import Workspace

SHOW_LINES = 4000  # A commit or file is cut here.
PREP_LIMIT = 200  # Commits listed per prep branch.


@dataclass(frozen=True)
class BranchItem:
    key: str  # base:<project>:<branch>, tree:<session>, prep:<project>:<branch>
    kind: str  # base, tree, or prep
    project: str
    label: str


@dataclass(frozen=True)
class DetailRow:
    """A row of the middle pane: a heading (no key), a commit, or an artifact."""

    text: str
    key: str = ''  # A commit sha, or artifact:<n>; empty for headings.
    repo: str = ''  # Where the commit is.
    artifact: Optional[Artifact] = None


@dataclass(frozen=True)
class BranchesSnapshot:
    items: tuple[BranchItem, ...] = ()
    details: Mapping[str, tuple[DetailRow, ...]] = field(default_factory=dict)
    statuses: Mapping[str, BaseStatus] = field(default_factory=dict)
    problems: tuple[str, ...] = ()


def _git(repo: pathlib.Path, *args: str) -> str:
    run = subprocess.run(
        ['git', *args], cwd=repo, capture_output=True, text=True, stdin=subprocess.DEVNULL
    )
    if run.returncode != 0:
        raise OSError(run.stderr.strip() or f'git {args[0]} failed')
    return run.stdout


def _count(n: int, one: str) -> str:
    return f'{n} {one}' + ('' if n == 1 else 's')


def _commit_row(sha: str, subject: str, repo: pathlib.Path, note: str = '') -> DetailRow:
    return DetailRow(f'  {sha[:12]}  {subject}' + (f'  ({note})' if note else ''), sha, str(repo))


class BranchService:
    """*records*: every session record (blocking: reads the store); *out*: a session's
    directory, whose work/out/ holds its artifacts."""

    def __init__(
        self,
        root: pathlib.Path,
        records: Callable[[], Sequence[SessionRecord]],
        out: Callable[[str], pathlib.Path],
    ) -> None:
        self.root = root
        self._records = records
        self._out = out

    def snapshot(self, workspace: Workspace) -> BranchesSnapshot:
        items: list[BranchItem] = []
        details: dict[str, tuple[DetailRow, ...]] = {}
        statuses: dict[str, BaseStatus] = {}
        problems: list[str] = []
        records = [r for r in self._records() if r.worktree is not None]
        for project in workspace.projects:
            repo = self.root / project.path
            if not (repo / '.git').exists():
                continue
            for base in project.bases:
                key = f'base:{project.id}:{base.branch}'
                status = base_status(repo, base)
                statuses[key] = status
                items.append(BranchItem(key, 'base', project.id, _base_label(status)))
                details[key] = _base_rows(status, repo)
            for record in (r for r in records if r.project == project.id):
                assert record.worktree is not None
                tree = pathlib.Path(record.worktree.path)
                if not tree.is_dir():
                    continue
                key = f'tree:{record.id}'
                rows, label, found = self._tree(record, tree)
                problems += found
                items.append(BranchItem(key, 'tree', project.id, label))
                details[key] = rows
            try:
                names = _git(repo, 'for-each-ref', '--format=%(refname:short)', 'refs/heads/b4/')
            except OSError as ex:
                problems.append(f'{project.id}: {ex}')
                continue
            for name in names.split():
                if name.startswith('b4/review/'):
                    continue
                key = f'prep:{project.id}:{name}'
                rows = _prep_rows(repo, name)
                commits = sum(1 for r in rows if r.key)
                items.append(
                    BranchItem(key, 'prep', project.id, f'{name} · {_count(commits, "commit")}')
                )
                details[key] = rows
        return BranchesSnapshot(tuple(items), details, statuses, tuple(problems))

    def _tree(
        self, record: SessionRecord, tree: pathlib.Path
    ) -> tuple[tuple[DetailRow, ...], str, list[str]]:
        assert record.worktree is not None
        classed = classify_tree(tree, record.worktree)
        state: TreeState = inspect_tree(tree, record.worktree.applied, record.worktree)
        rows = [DetailRow('Series')]
        rows += [_commit_row(c.sha, c.subject, tree) for c in classed.series]
        for branch in classed.branches:
            if not branch.commits and branch.name == record.worktree.branch:
                continue
            rows.append(DetailRow(f'{branch.kind} {branch.name}'))
            if branch.problem:
                rows.append(DetailRow(f'  ! {branch.problem}'))
            rows += [_commit_row(c.sha, c.subject, tree) for c in branch.commits]
        names = {b.name for b in classed.branches}
        artifacts, problems = load_artifacts(self._out(record.id) / 'work' / 'out', names)
        if artifacts:
            rows.append(DetailRow('Artifacts'))
            rows += [
                DetailRow(f'  {a.kind}: {a.title}', f'artifact:{n}', str(tree), a)
                for n, a in enumerate(artifacts)
            ]
        rows += [DetailRow(f'  ! {p}') for p in problems]
        label = f'{session_label(record)} · {record.id} · {state.describe()}'
        if classed.error:
            problems = [*problems, f'{record.id}: {classed.error}']
        return tuple(rows), label, [f'{record.id}: {p}' for p in problems]

    def show(self, row: DetailRow) -> str:
        """The right pane for a row: a commit's message, stat, and patch; an artifact's
        description and file."""
        if row.artifact is not None:
            a = row.artifact
            lines = [f'{a.kind}: {a.title}', '', a.summary]
            lines += [f'\nBranch: {a.branch}'] if a.branch else []
            lines += [f'Answers: {a.answers}'] if a.answers else []
            lines += [f'Tested: {a.tested}'] if a.tested else []
            if a.path is not None:
                try:
                    text = a.path.read_text(errors='replace')
                except OSError as ex:
                    text = f'(cannot read: {ex})'
                lines += ['', f'--- {a.path.name}', *text.splitlines()[:SHOW_LINES]]
            return '\n'.join(lines)
        if not row.key or not row.repo:
            return ''
        try:
            text = _git(
                pathlib.Path(row.repo), 'show', '--stat', '--patch', '--format=fuller', row.key
            )
        except OSError as ex:
            return f'Cannot show {row.key[:12]}: {ex}'
        lines = text.splitlines()
        cut = f'\n… {len(lines) - SHOW_LINES} more lines' if len(lines) > SHOW_LINES else ''
        return '\n'.join(lines[:SHOW_LINES]) + cut

    def rebase(self, workspace: Workspace, key: str, expected: str) -> RebaseResult:
        """Rebase the base named by *key*, if it is still at *expected* (its tip as shown)."""
        _kind, pid, branch = key.split(':', 2)
        project = workspace.project(pid)
        base = next((b for b in project.bases if b.branch == branch), None) if project else None
        if project is None or base is None:
            return RebaseResult(False, error=f'no maintainer base {branch} in {pid}')
        return rebase_base(self.root / project.path, base, expected)


def _base_label(status: BaseStatus) -> str:
    b = status.base
    head = f'{b.branch} on {b.upstream}'
    if not status.exists:
        return f'{head} · no such branch'
    parts = [f'{status.behind} behind'] if status.behind else []
    landed = sum(c.landed for c in status.commits)
    parts.append(_count(len(status.commits), 'commit') + (f' ({landed} landed)' if landed else ''))
    if status.checked_out:
        parts.append(f'checked out in {status.checked_out}')
    if status.error:
        parts.append(status.error)
    return f'{head} · {" · ".join(parts)}'


def _base_rows(status: BaseStatus, repo: pathlib.Path) -> tuple[DetailRow, ...]:
    if not status.exists:
        return (DetailRow('No such branch.'),)
    rows = [
        DetailRow(
            f'On {status.base.upstream}' + (f', {status.behind} behind' if status.behind else '')
        )
    ]
    rows += [
        _commit_row(c.sha, c.subject, repo, 'landed' if c.landed else '') for c in status.commits
    ]
    return tuple(rows)


def _prep_rows(repo: pathlib.Path, name: str) -> tuple[DetailRow, ...]:
    try:
        out = _git(
            repo,
            'log',
            '--reverse',
            f'--max-count={PREP_LIMIT}',
            '--format=%H%x00%s',
            name,
            '--not',
            '--remotes',
        )
    except OSError as ex:
        return (DetailRow(f'! {ex}'),)
    rows = [DetailRow('Commits not upstream')]
    for line in out.splitlines():
        sha, _, subject = line.partition('\x00')
        rows.append(_commit_row(sha, subject, repo))
    return tuple(rows)

# SPDX-License-Identifier: GPL-2.0-or-later
"""What the Branches screen shows: per project, the maintainer bases, the project's
worktrees (main checkout and linked ones), the session trees, b4 prep branches, b4
review branches, and other local branches with work not on any remote, each with its
commits (and a tree's artifacts); what a commit or artifact shows; rebasing a base.

Plain git and files, gathered off the UI thread, never writing: status runs with
GIT_OPTIONAL_LOCKS=0 so looking at a checkout does not refresh its index. Prep branches
are b4's (`b4/<name>`); its review branches (`b4/review/*`) are listed apart, read-only.
"""

import os
import pathlib
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional, Sequence

from .adapters.bases import (
    BaseStatus,
    PluckResult,
    RebaseResult,
    accept_rebase,
    base_status,
    pluck,
    rebase_base,
)
from .sessions.artifacts import Artifact, load_artifacts
from .sessions.hub import SessionError, SessionHub
from .sessions.rebase import RESULT
from .sessions.records import SessionRecord, session_label
from .sessions.trees import TreeState, classify_tree, inspect_tree, tree_worktrees
from .workspace import MaintainerBase, Project, Workspace

SHOW_LINES = 4000  # A commit or file is cut here.
LIST_LIMIT = 200  # Commits listed per branch or worktree.


@dataclass(frozen=True)
class BranchItem:
    key: str  # <kind>:<project>:<branch or path>, or tree:<session>
    kind: str  # base, worktree, tree, prep, review, or branch
    project: str
    label: str


@dataclass(frozen=True)
class DetailRow:
    """A row of the middle pane: a heading (no key), a commit, or an artifact."""

    text: str
    key: str = ''  # A commit sha, or artifact:<n>; empty for headings.
    repo: str = ''  # Where the commit is.
    artifact: Optional[Artifact] = None
    git_args: tuple[str, ...] = ()  # Shown by running git with these in repo instead.


@dataclass(frozen=True)
class BranchesSnapshot:
    items: tuple[BranchItem, ...] = ()
    details: Mapping[str, tuple[DetailRow, ...]] = field(default_factory=dict)
    statuses: Mapping[str, BaseStatus] = field(default_factory=dict)
    problems: tuple[str, ...] = ()
    rebases: Mapping[str, str] = field(default_factory=dict)  # Item key -> rebase session.
    targets: Mapping[str, tuple[str, ...]] = field(default_factory=dict)  # Item -> bases.


def _git(repo: pathlib.Path, *args: str) -> str:
    run = subprocess.run(
        ['git', *args],
        cwd=repo,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'},  # Looking never writes the index.
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
        sessions: Optional['SessionHub'] = None,
    ) -> None:
        self.root = root
        self._records = records
        self._out = out
        self._sessions = sessions  # Launches rebase sessions; None: not offered.

    def snapshot(self, workspace: Workspace) -> BranchesSnapshot:
        items: list[BranchItem] = []
        details: dict[str, tuple[DetailRow, ...]] = {}
        statuses: dict[str, BaseStatus] = {}
        problems: list[str] = []
        rebases: dict[str, str] = {}
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
            checked_out = set()
            for path, branch in _worktrees(repo):
                key = f'worktree:{project.id}:{path}'
                checked_out.add(branch)
                label, rows = _worktree(pathlib.Path(path), branch)
                items.append(BranchItem(key, 'worktree', project.id, label))
                details[key] = rows
            for record in (r for r in records if r.project == project.id):
                assert record.worktree is not None
                tree = pathlib.Path(record.worktree.path)
                if not tree.is_dir():
                    continue
                key = f'tree:{record.id}'
                if record.scope == 'rebase':
                    rebases[key] = record.id
                    rows, label, found = self._rebase_tree(record, tree)
                else:
                    rows, label, found = self._tree(record, tree)
                problems += found
                items.append(BranchItem(key, 'tree', project.id, label))
                details[key] = rows
            try:
                names = _git(repo, 'for-each-ref', '--format=%(refname:short)', 'refs/heads/')
            except OSError as ex:
                problems.append(f'{project.id}: {ex}')
                continue
            names_list = names.split()
            prep = [n for n in names_list if n.startswith('b4/') and not n.startswith('b4/review/')]
            review = [n for n in names_list if n.startswith('b4/review/')]
            bases = {b.branch for b in project.bases}
            other = [
                n
                for n in names_list
                if not n.startswith('b4/') and n not in bases and n not in checked_out
            ]
            for kind, group in (('prep', prep), ('review', review), ('branch', other)):
                for name in group:
                    key = f'{kind}:{project.id}:{name}'
                    rows = _unpushed_rows(repo, name)
                    commits = sum(1 for r in rows if r.key)
                    if kind == 'branch' and not commits:
                        continue  # Nothing of ours on it: not worth a row.
                    note = (
                        f'{_count(commits, "commit")} not upstream'
                        if kind != 'prep'
                        else _count(commits, 'commit')
                    )
                    items.append(BranchItem(key, kind, project.id, f'{name} · {note}'))
                    details[key] = rows
        targets = {}
        for item in items:
            project = workspace.project(item.project)
            bases = [f'base:{item.project}:{b.branch}' for b in project.bases] if project else []
            targets[item.key] = tuple(k for k in bases if k != item.key)
        return BranchesSnapshot(tuple(items), details, statuses, tuple(problems), rebases, targets)

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
        extras = [e for e in tree_worktrees(tree) if pathlib.Path(e.path).is_dir()]
        if extras:
            rows.append(DetailRow('Extra checkouts'))
            for extra in extras:
                try:
                    subject = _git(tree, 'log', '-1', '--format=%s', extra.head).strip()
                except OSError:
                    subject = ''
                where = extra.branch or extra.head[:12]
                rows.append(DetailRow(f'  {extra.path} · {where} {subject}'.rstrip()))
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

    def _rebase_tree(
        self, record: SessionRecord, tree: pathlib.Path
    ) -> tuple[tuple[DetailRow, ...], str, list[str]]:
        """A rebase session's tree: its result on b4m/rebased (commits on the upstream,
        and a range-diff against the base as it was), and its artifacts."""
        assert record.worktree is not None
        choices = record.choices
        upstream, fork, tip = (
            choices.get('upstream', ''),
            record.worktree.base,
            record.worktree.applied,
        )
        rows = [DetailRow(f'Rebase of {choices.get("base")} onto {upstream} (from {tip[:12]})')]
        try:
            result = _git(
                tree, 'rev-parse', '--verify', '--quiet', f'refs/heads/{RESULT}^{{commit}}'
            ).strip()
        except OSError:
            result = ''
        if result:
            log = _git(tree, 'log', '--reverse', '--format=%H%x00%s', f'{upstream}..{result}')
            commits = [line.split('\x00', 1) for line in log.splitlines()]
            rows.append(
                DetailRow(f'Result {RESULT}: {_count(len(commits), "commit")} on {upstream}')
            )
            rows += [_commit_row(sha, subject, tree) for sha, subject in commits]
            rows.append(
                DetailRow(
                    f'Range diff against {choices.get("base")}',
                    'range-diff',
                    str(tree),
                    git_args=('range-diff', f'{fork}..{tip}', f'{upstream}..{result}'),
                )
            )
            state = f'result: {_count(len(commits), "commit")}'
        else:
            rows.append(DetailRow(f'No result yet (the session leaves it on {RESULT}).'))
            state = 'no result yet'
        artifacts, problems = load_artifacts(self._out(record.id) / 'work' / 'out', {RESULT})
        if artifacts:
            rows.append(DetailRow('Artifacts'))
            rows += [
                DetailRow(f'  {a.kind}: {a.title}', f'artifact:{n}', str(tree), a)
                for n, a in enumerate(artifacts)
            ]
        rows += [DetailRow(f'  ! {p}') for p in problems]
        label = f'{session_label(record)} · {record.id} · {state}'
        return tuple(rows), label, [f'{record.id}: {p}' for p in problems]

    def rebase_session(self, workspace: Workspace, key: str, failed: RebaseResult) -> SessionRecord:
        """Launch a rebase session for the base named by *key*, after *failed*."""
        if self._sessions is None:
            raise SessionError('agent sessions are not available here')
        project, base = self._base(workspace, key)
        status = base_status(self.root / project.path, base)
        hub = self._sessions
        situation = hub.situation(workspace, hub.overview(), (), project.id, bases=False)
        return hub.launch_rebase(
            project.id, base, status, failed.failed, failed.detail or failed.error, situation
        )

    def accept(self, workspace: Workspace, sid: str) -> RebaseResult:
        """Move the rebase session's base to its result (see accept_rebase's guards)."""
        record = next((r for r in self._records() if r.id == sid and r.scope == 'rebase'), None)
        if record is None or record.worktree is None:
            return RebaseResult(False, error=f'no rebase session {sid}')
        branch = record.choices.get('base', '')
        project = workspace.project(record.project)
        base = next((b for b in project.bases if b.branch == branch), None) if project else None
        if project is None or base is None:
            return RebaseResult(False, error=f'no maintainer base {branch} in {record.project}')
        return accept_rebase(
            self.root / project.path,
            base,
            pathlib.Path(record.worktree.path),
            RESULT,
            record.choices.get('expected', ''),
        )

    def targets(self, workspace: Workspace, item: str) -> list[str]:
        """Where commits shown under *item* may be plucked: its project's bases, not itself."""
        project = self._project_of(workspace, item)
        if project is None:
            return []
        keys = [f'base:{project.id}:{b.branch}' for b in project.bases]
        return [k for k in keys if k != item]

    def _project_of(self, workspace: Workspace, item: str) -> Optional[Project]:
        if item.startswith('tree:'):
            record = next((r for r in self._records() if r.id == item.removeprefix('tree:')), None)
            return workspace.project(record.project) if record else None
        parts = item.split(':', 2)
        return workspace.project(parts[1]) if len(parts) == 3 else None

    def pluck(
        self,
        workspace: Workspace,
        item: str,
        rows: Sequence[DetailRow],
        target: str,
        expected: str,
    ) -> PluckResult:
        """Copy the commits of *rows* (shown under *item*) onto the base *target*."""
        try:
            project, base = self._base(workspace, target)
        except SessionError as ex:
            return PluckResult(False, error=str(ex))
        commits = [r for r in rows if r.key and r.repo and not r.git_args and r.artifact is None]
        if not commits:
            return PluckResult(False, error='no commits marked')
        sources = {r.repo for r in commits}
        if len(sources) != 1:
            return PluckResult(False, error='mark commits from one place at a time')
        model = 'LLM'
        if item.startswith('tree:'):
            record = next((r for r in self._records() if r.id == item.removeprefix('tree:')), None)
            if record is not None:
                model = record.model or record.preset or model
        return pluck(
            self.root / project.path,
            base,
            pathlib.Path(sources.pop()),
            [r.key for r in commits],
            expected,
            workspace.agent_email,
            workspace.assisted_by.replace('{model}', model),
        )

    def _base(self, workspace: Workspace, key: str) -> tuple[Project, MaintainerBase]:
        _kind, pid, branch = key.split(':', 2)
        project = workspace.project(pid)
        base = next((b for b in project.bases if b.branch == branch), None) if project else None
        if project is None or base is None:
            raise SessionError(f'no maintainer base {branch} in {pid}')
        return project, base

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
        if row.git_args:
            try:
                return _git(pathlib.Path(row.repo), *row.git_args) or '(no differences)'
            except OSError as ex:
                return f'Cannot show it: {ex}'
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
        try:
            project, base = self._base(workspace, key)
        except SessionError as ex:
            return RebaseResult(False, error=str(ex))
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


def _worktrees(repo: pathlib.Path) -> list[tuple[str, str]]:
    """(path, branch) of the project's worktrees, the main checkout first; b4's scratch
    worktrees are left out (they come and go with b4's own runs)."""
    found: list[tuple[str, str]] = []
    path = branch = ''
    for line in (*_git(repo, 'worktree', 'list', '--porcelain').splitlines(), ''):
        if line.startswith('worktree '):
            path, branch = line.removeprefix('worktree '), ''
        elif line.startswith('branch '):
            branch = line.removeprefix('branch refs/heads/')
        elif line == 'detached':
            branch = ''
        elif not line and path:
            if 'b4-shazam-worktree' not in path:
                found.append((path, branch))
            path = ''
    return found


def _worktree(path: pathlib.Path, branch: str) -> tuple[str, tuple[DetailRow, ...]]:
    try:
        changed = [line for line in _git(path, 'status', '--porcelain').splitlines() if line]
    except OSError as ex:
        return f'{path} · {ex}', ()
    state = f'{len(changed)} changed' if changed else 'clean'
    label = f'{path} · {branch or "detached"} · {state}'
    rows = [DetailRow('Changes'), *(DetailRow(f'  {line}') for line in changed)] if changed else []
    rows += _unpushed_rows(path, 'HEAD')
    return label, tuple(rows)


def _unpushed_rows(repo: pathlib.Path, name: str) -> tuple[DetailRow, ...]:
    try:
        out = _git(
            repo,
            'log',
            '--reverse',
            f'--max-count={LIST_LIMIT}',
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

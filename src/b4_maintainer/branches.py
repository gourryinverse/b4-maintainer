# SPDX-License-Identifier: GPL-2.0-or-later
"""What the Branches screen shows: per project, the maintainer bases, the project's
worktrees (main checkout and linked ones), the session trees, b4 prep branches, b4
review branches, and other local branches with work not on any remote, each with its
commits (and a tree's artifacts); what a commit or artifact shows; rebasing a base.

Plain git and files, gathered off the UI thread, never writing: status runs with
GIT_OPTIONAL_LOCKS=0 so looking at a checkout does not refresh its index. Prep branches
are b4's (`b4/<name>`); its review branches (`b4/review/*`) are listed apart, read-only.
"""

import dataclasses
import datetime
import json
import os
import pathlib
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence

from .adapters.bases import (
    BaseStatus,
    PluckResult,
    RebaseResult,
    accept_rebase,
    base_status,
    copy_to_tree,
    pluck_onto,
    rebase_base,
)
from .adapters.prep import (
    TRACKING,
    PrepError,
    SignResult,
    cleanup_prep,
    cover_text,
    cut_prep,
    ensure_worktree,
    move_prep,
    outgoing_summary,
    prep_fork,
    prep_tracking,
    preview_send,
    remove_prep,
    run_b4,
    sign_off,
)
from .attachments import Attachment, preview
from .sessions.artifacts import Artifact, load_artifacts
from .sessions.hub import SessionError, SessionHub
from .sessions.policy import ContextItem
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
    branch: str = ''  # The session tree branch a commit is on.
    show_text: str = ''  # Shown as is (e.g. a cover letter).


@dataclass(frozen=True)
class BranchesSnapshot:
    items: tuple[BranchItem, ...] = ()
    details: Mapping[str, tuple[DetailRow, ...]] = field(default_factory=dict)
    statuses: Mapping[str, BaseStatus] = field(default_factory=dict)
    problems: tuple[str, ...] = ()
    rebases: Mapping[str, str] = field(default_factory=dict)  # Item key -> rebase session.
    targets: Mapping[str, tuple[str, ...]] = field(default_factory=dict)  # Item -> targets.
    tips: Mapping[str, str] = field(default_factory=dict)  # Pluck target -> its tip.
    identities: Mapping[str, str] = field(default_factory=dict)  # Project -> git identity.
    preps: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)  # b4's tracking.


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


@dataclass(frozen=True)
class CutResult:
    ok: bool
    branch: str = ''
    picked: int = 0
    error: str = ''


class BranchService:
    """*records*: every session record (blocking: reads the store); *out*: a session's
    directory, whose work/out/ holds its artifacts."""

    def __init__(
        self,
        root: pathlib.Path,
        records: Callable[[], Sequence[SessionRecord]],
        out: Callable[[str], pathlib.Path],
        sessions: Optional['SessionHub'] = None,
        email_dryrun: bool = False,
        no_sign: bool = False,
    ) -> None:
        self.root = root
        self._records = records
        self._out = out
        self._sessions = sessions  # Launches rebase sessions; None: not offered.
        self.email_dryrun = email_dryrun  # b4 send -d: print instead of sending.
        self.no_sign = no_sign  # b4 send --no-sign.

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
                    rows = _prep_rows(repo, name) if kind == 'prep' else _unpushed_rows(repo, name)
                    commits = sum(1 for r in rows if r.key and not r.show_text)
                    if kind == 'branch' and not commits:
                        continue  # Nothing of ours on it: not worth a row.
                    note = (
                        f'{_count(commits, "commit")} not upstream'
                        if kind != 'prep'
                        else _count(commits, 'commit')
                    )
                    items.append(BranchItem(key, kind, project.id, f'{name} · {note}'))
                    details[key] = rows
        identities, preps = {}, {}
        for project in workspace.projects:
            repo = self.root / project.path
            if (repo / '.git').exists():
                identities[project.id] = _identity(repo)
        for n, item in enumerate(items):
            if item.kind != 'prep':
                continue
            project = workspace.project(item.project)
            if project is None:
                continue
            branch = item.key.split(':', 2)[2]
            tracking = prep_tracking(self.root / project.path, branch)
            preps[item.key] = tracking
            commits = sum(1 for r in details.get(item.key, ()) if is_commit(r))
            label = f'{branch} · v{tracking["revision"]} · {_count(commits, "commit")}'
            if tracking['sent']:
                label += f' · sent {", ".join(tracking["sent"])}'
            items[n] = dataclasses.replace(item, label=label)
        targets, tips = {}, {}
        for item in items:
            project = workspace.project(item.project)
            bases = [f'base:{item.project}:{b.branch}' for b in project.bases] if project else []
            prep_keys = [i.key for i in items if i.kind == 'prep' and i.project == item.project]
            tree_keys = [
                i.key
                for i in items
                if i.kind == 'tree' and i.project == item.project and i.key not in rebases
            ]
            keys = (*bases, *prep_keys, *tree_keys)
            targets[item.key] = tuple(k for k in keys if k != item.key)
        for key, status in statuses.items():
            tips[key] = status.tip
        for record in records:
            if record.worktree is not None:
                tips[f'tree:{record.id}'] = record.worktree.applied  # Per branch, at copy time.
        for item in (i for i in items if i.kind == 'prep'):
            project = workspace.project(item.project)
            if project is not None:
                tips[item.key] = _git(
                    self.root / project.path, 'rev-parse', item.key.split(':', 2)[2]
                ).strip()
        return BranchesSnapshot(
            tuple(items),
            details,
            statuses,
            tuple(problems),
            rebases,
            targets,
            tips,
            identities,
            preps,
        )

    def _tree(
        self, record: SessionRecord, tree: pathlib.Path
    ) -> tuple[tuple[DetailRow, ...], str, list[str]]:
        assert record.worktree is not None
        classed = classify_tree(tree, record.worktree)
        state: TreeState = inspect_tree(tree, record.worktree.applied, record.worktree)
        rows = self._prompt_rows(record, tree) + [DetailRow('Series')]
        rows += [
            dataclasses.replace(_commit_row(c.sha, c.subject, tree), branch=record.worktree.branch)
            for c in classed.series
        ]
        for branch in classed.branches:
            if not branch.commits and branch.name == record.worktree.branch:
                continue
            rows.append(DetailRow(f'{branch.kind} {branch.name}'))
            if branch.problem:
                rows.append(DetailRow(f'  ! {branch.problem}'))
            rows += [
                dataclasses.replace(_commit_row(c.sha, c.subject, tree), branch=branch.name)
                for c in branch.commits
            ]
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
        parent = f' (from {record.parent})' if record.parent else ''
        label = f'{session_label(record)} · {record.id}{parent} · {state.describe()}'
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
        rows = self._prompt_rows(record, tree)
        rows.append(DetailRow(f'Rebase of {choices.get("base")} onto {upstream} (from {tip[:12]})'))
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
        repo = self.root / project.path
        before = base_status(repo, base)
        result = accept_rebase(
            repo,
            base,
            pathlib.Path(record.worktree.path),
            RESULT,
            record.choices.get('expected', ''),
        )
        return self._move_preps(repo, before, result) if result.ok else result

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

    def _model(self, item: str) -> str:
        if item.startswith('tree:'):
            record = next((r for r in self._records() if r.id == item.removeprefix('tree:')), None)
            if record is not None:
                return record.model or record.preset or 'LLM'
        return 'LLM'

    def _target(self, workspace: Workspace, target: str) -> tuple[Project, str]:
        """(project, branch) of a pluck target: a base or a prep branch."""
        kind, pid, branch = target.split(':', 2)
        project = workspace.project(pid)
        if project is None or kind not in ('base', 'prep'):
            raise SessionError(f'cannot pluck onto {target}')
        if kind == 'base' and not any(b.branch == branch for b in project.bases):
            raise SessionError(f'no maintainer base {branch} in {pid}')
        return project, branch

    def pluck(
        self,
        workspace: Workspace,
        item: str,
        rows: Sequence[DetailRow],
        target: str,
        expected: str,
    ) -> PluckResult:
        """Copy the commits of *rows* (shown under *item*) onto *target* (a base or a prep
        branch, remembering where each came from; or another session tree)."""
        if target.startswith('tree:'):
            return self._copy_to_tree(workspace, item, rows, target.removeprefix('tree:'))
        try:
            project, branch = self._target(workspace, target)
        except SessionError as ex:
            return PluckResult(False, error=str(ex))
        commits = [r for r in rows if is_commit(r)]
        if not commits:
            return PluckResult(False, error='no commits marked')
        sources = {r.repo for r in commits}
        if len(sources) != 1:
            return PluckResult(False, error='mark commits from one place at a time')
        repo = self.root / project.path
        result = pluck_onto(
            repo,
            branch,
            pathlib.Path(sources.pop()),
            [r.key for r in commits],
            expected,
            workspace.agent_email,
            workspace.assisted_by.replace('{model}', self._model(item)),
        )
        if result.ok:
            self._remember(project, item, commits, result)
        return result

    def _copy_to_tree(
        self, workspace: Workspace, item: str, rows: Sequence[DetailRow], sid: str
    ) -> PluckResult:
        """Commits onto another session tree, on branches named as their source branch
        (or b4m/pull-ahead/<subject> from elsewhere), each made where its class starts:
        pull-aheads at the tree's series base, others on top of its series. A note in
        the session's context says what arrived."""
        record = self._record(sid)
        if record is None or record.worktree is None:
            return PluckResult(False, error=f'no session tree {sid}')
        tree = pathlib.Path(record.worktree.path)
        commits = [r for r in rows if is_commit(r)]
        if not commits or len({r.repo for r in commits}) != 1:
            return PluckResult(False, error='mark commits from one place at a time')
        groups: dict[str, list[DetailRow]] = {}
        for row in commits:
            name = row.branch or 'b4m/pull-ahead/' + _slug(row.text.split('  ', 2)[-1])
            groups.setdefault(name, []).append(row)
        picked: list[tuple[str, str]] = []
        dropped: list[str] = []
        arrived: list[str] = []
        for name, group in groups.items():
            start = (
                record.worktree.base
                if name.startswith('b4m/pull-ahead/')
                else record.worktree.applied
            )
            result = copy_to_tree(
                pathlib.Path(group[0].repo), tree, [r.key for r in group], name, start
            )
            if not result.ok:
                return result
            picked += result.picked
            dropped += result.dropped
            arrived += [f'- {name}: {r.text.strip().split("  ", 1)[-1]}' for r in group]
        source = item.removeprefix('tree:') if item.startswith('tree:') else item
        inbox = self._out(sid) / 'work' / 'context' / 'inbox.md'
        inbox.parent.mkdir(parents=True, exist_ok=True)
        when = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
        with inbox.open('a') as f:
            f.write(f'\n{when}: the maintainer copied commits here from {source}:\n')
            f.write('\n'.join(arrived) + '\n')
        return PluckResult(True, '', tuple(picked), tuple(dropped))

    # Where plucked commits came from: var/branches/<project>/plucked.json, local.

    def _provenance_path(self, project: Project) -> pathlib.Path:
        return self.root / 'var' / 'branches' / project.id / 'plucked.json'

    def _provenance(self, project: Project) -> dict[str, dict[str, str]]:
        try:
            data = json.loads(self._provenance_path(project).read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _origin(self, project: Project, item: str, row: DetailRow) -> dict[str, str]:
        """What is known about a commit: the artifact describing it (through earlier
        plucks, or its session tree's manifest)."""
        known = self._provenance(project).get(row.key)
        if known:
            return known
        if item.startswith('tree:') and row.branch:
            sid = item.removeprefix('tree:')
            names = {row.branch}
            artifacts, _problems = load_artifacts(self._out(sid) / 'work' / 'out', names)
            for a in artifacts:
                if a.branch == row.branch:
                    return {
                        'session': sid,
                        'title': a.title,
                        'summary': a.summary,
                        'tested': a.tested,
                    }
        return {}

    def _remember(
        self, project: Project, item: str, rows: Sequence[DetailRow], result: PluckResult
    ) -> None:
        data = self._provenance(project)
        by_source = {r.key: r for r in rows}
        for source, new in result.picked:
            row = by_source.get(source)
            origin = self._origin(project, item, row) if row is not None else {}
            data[new] = {**origin, 'source': source}
        path = self._provenance_path(project)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f'.{path.name}.tmp')
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
        os.replace(tmp, path)

    def cover_draft(self, project: Project, item: str, rows: Sequence[DetailRow]) -> str:
        """A cover letter to edit: the subject marked EDITME (b4 then reports it as
        needing edits), each artifact's summary, the commits without one."""
        commits = [r for r in rows if is_commit(r)]
        subjects = [r.text.split('  ', 2)[-1].rsplit('  (', 1)[0] for r in commits]
        subject = subjects[0] if len(subjects) == 1 else 'cover title'
        parts, seen, bare = [], set(), []
        for row, line in zip(commits, subjects):
            origin = self._origin(project, item, row)
            title = origin.get('title', '')
            if not title:
                bare.append(f'- {line}')
                continue
            if title in seen:
                continue
            seen.add(title)
            text = f'{title}\n\n{origin.get("summary", "").strip()}'
            if origin.get('tested'):
                text += f'\n\nTested: {origin["tested"]}'
            parts.append(text)
        if bare:
            parts.append('\n'.join(bare))
        return f'EDITME: {subject}\n\n' + '\n\n'.join(parts) + '\n'

    def fork_points(self, workspace: Workspace, item: str) -> list[str]:
        """Where a new prep branch may fork: each base's upstream, each base, the
        project's refs."""
        project = self._project_of(workspace, item)
        if project is None:
            return []
        points = [b.upstream for b in project.bases] + [b.branch for b in project.bases]
        points += [r.name for r in project.refs]
        return list(dict.fromkeys(points))

    def _prep_worktree(self, project: Project, branch: str) -> pathlib.Path:
        return self.root / 'var' / 'branches' / project.id / branch.removeprefix('b4/')

    def cut_prep(
        self, workspace: Workspace, item: str, rows: Sequence[DetailRow], name: str, fork: str
    ) -> CutResult:
        """A new prep branch `b4/<name>` from *fork* with *rows*' commits plucked onto it
        and a drafted cover letter; if the commits do not apply, no prep branch is left."""
        project = self._project_of(workspace, item)
        if project is None:
            return CutResult(False, error=f'no project for {item}')
        repo = self.root / project.path
        worktree = self.root / 'var' / 'branches' / project.id / name
        try:
            branch = cut_prep(repo, worktree, name, fork, self.cover_draft(project, item, rows))
        except PrepError as ex:
            return CutResult(False, error=str(ex))
        tip = _git(repo, 'rev-parse', branch).strip()
        result = self.pluck(workspace, item, rows, f'prep:{project.id}:{branch}', tip)
        if not result.ok:
            remove_prep(repo, worktree, branch)
            why = f'{result.failed} does not apply onto {fork}' if result.failed else result.error
            return CutResult(False, error=why)
        return CutResult(True, branch, len(result.picked))

    def prep_run(self, workspace: Workspace, item: str, action: str) -> tuple[int, str]:
        """b4 on a prep branch: recipients (auto-to-cc), checks (check), or info."""
        args = PREP_ACTIONS.get(action)
        _kind, pid, branch = item.split(':', 2)
        project = workspace.project(pid)
        if args is None or project is None:
            return 1, f'cannot {action} {item}'
        repo = self.root / project.path
        if action == 'compare':
            sent = prep_tracking(repo, branch)['sent']
            if not sent:
                return 1, 'nothing sent yet to compare with'
            args = (*args, str(sent[-1]).lstrip('v'))
        worktree = self._prep_worktree(project, branch)
        try:
            ensure_worktree(repo, worktree, branch)
        except PrepError as ex:
            return 1, str(ex)
        return run_b4(worktree, branch, *args)

    def _prep(self, workspace: Workspace, item: str) -> tuple[pathlib.Path, pathlib.Path, str]:
        """(repo, worktree, branch) of a prep item; the worktree made if missing."""
        _kind, pid, branch = item.split(':', 2)
        project = workspace.project(pid)
        if project is None:
            raise PrepError(f'no project {pid}')
        repo = self.root / project.path
        worktree = self._prep_worktree(project, branch)
        ensure_worktree(repo, worktree, branch)
        return repo, worktree, branch

    def sign_off(self, workspace: Workspace, item: str, expected: str) -> SignResult:
        """The maintainer's Signed-off-by on each commit of the prep branch lacking it."""
        _kind, pid, branch = item.split(':', 2)
        project = workspace.project(pid)
        if project is None:
            return SignResult(False, error=f'no project {pid}')
        repo = self.root / project.path
        name, email = _identity_parts(repo)
        if not name or not email:
            return SignResult(False, error='no git user.name or user.email for the project')
        return sign_off(repo, branch, expected, name, email)

    def preview(self, workspace: Workspace, item: str) -> tuple[int, str]:
        """What b4 send would send (b4 send -o, nothing sent): subjects, recipients, and
        the first message; b4's own warnings after."""
        try:
            repo, worktree, branch = self._prep(workspace, item)
        except PrepError as ex:
            return 1, str(ex)
        out = worktree.with_name(f'{worktree.name}.outgoing')
        code, text, messages = preview_send(worktree, branch, out, self.no_sign)
        if code != 0 or not messages:
            return code or 1, text
        return 0, outgoing_summary(messages) + '\n\n--- b4 send -o\n' + text

    def send_args(
        self, workspace: Workspace, item: str
    ) -> tuple[pathlib.Path, str, tuple[str, ...]]:
        """(worktree, branch, b4 arguments) for sending the prep branch interactively: b4
        shows its summary and asks before anything goes out."""
        _repo, worktree, branch = self._prep(workspace, item)
        args = (
            'send',
            *(('-d',) if self.email_dryrun else ()),
            *(('--no-sign',) if self.no_sign else ()),
        )
        return worktree, branch, args

    # Reaping: removing what is no longer needed, after the maintainer saw what goes.

    def reapable(self, data: BranchesSnapshot, item: str) -> bool:
        """Prep branches, other local branches, and session trees may be removed here;
        bases, b4's review branches, and the project's worktrees may not."""
        kind = item.split(':', 1)[0]
        return kind in ('prep', 'branch', 'tree') and any(i.key == item for i in data.items)

    def losses(self, workspace: Workspace, item: str) -> str:
        """What removing *item* would lose, for the confirmation."""
        kind, _, rest = item.partition(':')
        if kind == 'tree':
            record = self._record(rest)
            if self._sessions is None or record is None or record.worktree is None:
                return f'session tree {rest}'
            state = self._sessions.worktree_losses([record.worktree.path])[record.worktree.path]
            return f'session tree {record.worktree.path}: {state.describe()}'
        pid, _, branch = rest.partition(':')
        project = workspace.project(pid)
        if project is None:
            return item
        repo = self.root / project.path
        if kind == 'prep':
            tracking = prep_tracking(repo, branch)
            rows = _prep_rows(repo, branch)
            n = sum(1 for r in rows if is_commit(r))
            sent = f', sent {", ".join(tracking["sent"])}' if tracking['sent'] else ', never sent'
            return (
                f'{branch}: {_count(n, "commit")}, v{tracking["revision"]}{sent}. b4 archives it '
                'in its data directory, then deletes the branch and its sent/ tags; its '
                'worktree goes too'
            )
        rows = _unpushed_rows(repo, branch)
        subjects = [r.text.strip().split('  ', 1)[-1] for r in rows if is_commit(r)]
        listed = '\n'.join(f'  {s}' for s in subjects[:20])
        more = f'\n  … {len(subjects) - 20} more' if len(subjects) > 20 else ''
        return f'{branch}: {_count(len(subjects), "commit")} on no remote:\n{listed}{more}'

    def reap(self, workspace: Workspace, item: str) -> tuple[bool, str]:
        """Remove *item* (the maintainer confirmed its losses)."""
        kind, _, rest = item.partition(':')
        if kind == 'tree':
            record = self._record(rest)
            if self._sessions is None or record is None or record.worktree is None:
                return False, f'no session tree {rest}'
            try:
                self._sessions.remove_worktrees([record.worktree.path])
            except SessionError as ex:
                return False, str(ex)
            return True, f'Removed the tree of session {rest}'
        pid, _, branch = rest.partition(':')
        project = workspace.project(pid)
        if project is None:
            return False, f'no project {pid}'
        repo = self.root / project.path
        if kind == 'prep':
            code, out = cleanup_prep(repo, self._prep_worktree(project, branch), branch)
            last = next((line for line in reversed(out.splitlines()) if line.strip()), '')
            return code == 0, f'Removed {branch}' if code == 0 else f'{branch} stays: {last}'
        if kind == 'branch':
            run = subprocess.run(
                ['git', 'branch', '-D', branch], cwd=repo, capture_output=True, text=True
            )
            if run.returncode != 0:
                return False, f'{branch} stays: {run.stderr.strip()}'
            return True, f'Removed {branch}'
        return False, f'{item} is not removed from here'

    # Deriving a session from another's output.

    def derive_options(
        self, workspace: Workspace, item: str, row: Optional[DetailRow]
    ) -> list[tuple[str, str, str]]:
        """Where a session derived from a tree's session may start: (start, the session
        whose tree holds it or '', label). The series base; the parent's tree at the
        selected commit, or at its session branch; each maintainer base."""
        record = self._record(item.removeprefix('tree:'))
        if record is None or record.worktree is None:
            return []
        wt, sid = record.worktree, record.id
        options = [(wt.base, '', f'the series base {wt.base[:12]} (pre-existing bugs)')]
        if row is not None and is_commit(row) and row.repo == wt.path:
            subject = row.text.strip().split('  ', 1)[-1]
            options.append((row.key, sid, f"session {sid}'s tree at {row.key[:12]} {subject}"))
        try:
            tip = _git(pathlib.Path(wt.path), 'rev-parse', wt.branch).strip()
            options.append((tip, sid, f"session {sid}'s tree ({wt.branch}, series applied)"))
        except OSError:
            pass
        project = workspace.project(record.project)
        for base in project.bases if project else ():
            options.append((base.branch, '', f'maintainer base {base.branch}'))
        return options

    def derive_context(self, item: str, row: Optional[DetailRow]) -> Optional[ContextItem]:
        """What a derived session receives from its parent: the selected artifact, else
        every artifact's summary from the parent's manifest."""
        sid = item.removeprefix('tree:')
        if row is not None and row.artifact is not None:
            text = self.show(row)
            return ContextItem('parent', f'From session {sid}: {row.artifact.title}', text=text)
        record = self._record(sid)
        names: set[str] = set()
        if record is not None and record.worktree is not None:
            try:
                refs = _git(
                    pathlib.Path(record.worktree.path),
                    'for-each-ref',
                    '--format=%(refname:short)',
                    'refs/heads/',
                )
                names = set(refs.split())
            except OSError:
                pass
        artifacts, _problems = load_artifacts(self._out(sid) / 'work' / 'out', names)
        if not artifacts:
            return None
        text = '\n\n'.join(f'{a.kind}: {a.title}\n\n{a.summary}' for a in artifacts)
        return ContextItem('parent', f'From session {sid}: its findings', text=text)

    def _prompt_rows(self, record: SessionRecord, tree: pathlib.Path) -> list[DetailRow]:
        """The session's initial prompt (and appended system prompt), as it was sent."""
        directory = self._out(record.id)
        task = directory / 'work' / 'context' / 'task.md'
        if not task.is_file():
            return []
        try:
            system = (directory / 'system.md').read_text(errors='replace')
        except OSError:
            system = ''
        prompt = Attachment('prompt', '', record.id, path=task, title='Initial prompt', text=system)
        return [DetailRow('Initial prompt', 'prompt', str(tree), show_text=preview(prompt))]

    def _record(self, sid: str) -> Optional[SessionRecord]:
        return next((r for r in self._records() if r.id == sid), None)

    def prep_worktree(self, workspace: Workspace, item: str) -> Optional[pathlib.Path]:
        """The prep branch's worktree (made if missing), for running b4 interactively."""
        _kind, pid, branch = item.split(':', 2)
        project = workspace.project(pid)
        if project is None:
            return None
        worktree = self._prep_worktree(project, branch)
        ensure_worktree(self.root / project.path, worktree, branch)
        return worktree

    def _base(self, workspace: Workspace, key: str) -> tuple[Project, MaintainerBase]:
        _kind, pid, branch = key.split(':', 2)
        project = workspace.project(pid)
        base = next((b for b in project.bases if b.branch == branch), None) if project else None
        if project is None or base is None:
            raise SessionError(f'no maintainer base {branch} in {pid}')
        return project, base

    def show(self, row: DetailRow) -> str:
        """The right pane for a row: a commit's message, stat, and patch; an artifact's
        description and file; a cover letter."""
        if row.show_text:
            return row.show_text
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
        """Rebase the base named by *key*, if it is still at *expected* (its tip as shown),
        then move the prep branches cut from it."""
        try:
            project, base = self._base(workspace, key)
        except SessionError as ex:
            return RebaseResult(False, error=str(ex))
        repo = self.root / project.path
        before = base_status(repo, base)
        result = rebase_base(repo, base, expected)
        return self._move_preps(repo, before, result) if result.ok else result

    def _move_preps(
        self, repo: pathlib.Path, before: BaseStatus, result: RebaseResult
    ) -> RebaseResult:
        """Prep branches forked from the base as it was (its tip or one of its own
        commits) moved onto its new tip; those that conflict stay and are named."""
        old = {before.tip, *(c.sha for c in before.commits)}
        names = _git(repo, 'for-each-ref', '--format=%(refname:short)', 'refs/heads/b4/').split()
        moved, stuck = [], []
        for name in (n for n in names if not n.startswith('b4/review/')):
            if prep_fork(repo, name) not in old:
                continue
            tip = _git(repo, 'rev-parse', name).strip()
            outcome = move_prep(repo, name, result.tip, tip)
            if outcome.ok:
                moved.append(name)
            else:
                why = f'{outcome.failed} does not apply' if outcome.failed else outcome.error
                stuck.append((name, why))
        return dataclasses.replace(result, moved=tuple(moved), stuck=tuple(stuck))


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


PREP_ACTIONS = {
    'auto-to-cc': ('prep', '--auto-to-cc'),
    'check': ('prep', '--check'),
    'info': ('prep', '--show-info'),
    'trailers': ('trailers', '-u'),
    'compare': ('prep', '--compare-to'),  # And the last sent revision's number.
}


def _identity_parts(repo: pathlib.Path) -> tuple[str, str]:
    def value(key: str) -> str:
        run = subprocess.run(['git', 'config', key], cwd=repo, capture_output=True, text=True)
        return run.stdout.strip()

    return value('user.name'), value('user.email')


def _identity(repo: pathlib.Path) -> str:
    name, email = _identity_parts(repo)
    return f'{name} <{email}>' if name and email else ''


def _slug(subject: str) -> str:
    words = re.sub(r'[^a-z0-9]+', '-', subject.split(':', 1)[-1].lower()).strip('-')
    return words[:40].rstrip('-') or 'commit'


def is_commit(row: DetailRow) -> bool:
    """A commit row (not a heading, an artifact, a cover letter, or a range-diff)."""
    return (
        bool(row.key and row.repo)
        and not row.git_args
        and not row.show_text
        and row.artifact is None
    )


def _prep_rows(repo: pathlib.Path, name: str) -> tuple[DetailRow, ...]:
    """A prep branch: its cover letter, then its commits after b4's cover commit."""
    cover = _git(
        repo, 'log', '--format=%H', f'--grep={TRACKING}', '--fixed-strings', '-n1', name
    ).strip()
    if not cover:
        return _unpushed_rows(repo, name)
    text = cover_text(repo, name)
    subject = text.splitlines()[0] if text else ''
    rows = [DetailRow(f'Cover letter: {subject}', 'cover', str(repo), show_text=text)]
    log = _git(repo, 'log', '--reverse', '--format=%H%x00%s', f'{cover}..{name}')
    for line in log.splitlines():
        sha, _, subject = line.partition('\x00')
        rows.append(_commit_row(sha, subject, repo))
    return tuple(rows)


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

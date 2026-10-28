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
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional, Sequence

from .adapters.bases import (
    BaseCommit,
    BaseStatus,
    PluckResult,
    RebaseResult,
    accept_onto,
    accept_rebase,
    base_status,
    copy_to_tree,
    drop_commits,
    pluck_onto,
    rebase_base,
    series_commits,
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
    prep_from_thread,
    prep_tracking,
    preview_send,
    remove_prep,
    reroll_prep,
    run_b4,
    sign_off,
)
from .adapters.scratch import base_refs as match_refs
from .adapters.scratch import tracked_branches
from .attachments import Attachment, preview
from .sessions.artifacts import Artifact, load_artifacts
from .sessions.hub import SessionError, SessionHub
from .sessions.launch import Target
from .sessions.policy import ContextItem
from .sessions.rebase import RESULT
from .sessions.records import SessionRecord, session_label
from .sessions.trees import TreeState, classify_tree, inspect_tree, series_tip, tree_worktrees
from .workspace import MaintainerBase, Project, Workspace

if TYPE_CHECKING:
    from .adapters.apply import ApplyResult, SeriesPatch
    from .model import Series

SHOW_LINES = 4000  # A commit or file is cut here.
LIST_LIMIT = 200  # Commits listed per branch or worktree.


@dataclass(frozen=True)
class BranchItem:
    key: str  # <kind>:<project>:<branch or path>, tree:<session>, or work:<tree>:<branch>
    kind: str  # base, worktree, tree, work (a session's agent branch), prep, review, branch
    project: str
    label: str  # Short: the list never wraps; the rest is in *info*.
    info: tuple[str, ...] = ()  # Its details (the pane under the list).
    flagged: bool = False  # Something needs attention (said in *info*): marked with *.


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
class ApplyChoices:
    """What applying a posted series offers (see BranchService.apply_choices)."""

    patches: tuple['SeriesPatch', ...] = ()
    targets: tuple[tuple[str, str], ...] = ()  # (key, branch), bases first.
    tips: Mapping[str, str] = field(default_factory=dict)
    problem: str = ''


@dataclass(frozen=True)
class DropChoices:
    """Where a posted series is on the project's bases (see BranchService.drop_choices)."""

    targets: tuple[tuple[str, str], ...] = ()  # (key, branch) of each base holding it.
    commits: Mapping[str, tuple[BaseCommit, ...]] = field(default_factory=dict)
    tips: Mapping[str, str] = field(default_factory=dict)
    problem: str = ''


@dataclass(frozen=True)
class FoldPlan:
    """What `f` on a session's folded series makes: its commits, after the pull-aheads
    chosen from *optional*, on its base as a prep branch: the series' own (its commits
    replaced: the next revision), else a new one from the posted thread."""

    project: str = ''
    tree: str = ''  # The session tree holding the branches.
    base: str = ''
    folded: str = ''  # Its b4m/folded/ branch.
    commits: int = 0
    needs: tuple[str, ...] = ()  # Pull-aheads it carries already.
    optional: tuple[tuple[str, str], ...] = ()  # (branch, title) of the others.
    target: str = ''  # The prep branch it replaces, or the new one's suggested name.
    replaces: bool = False
    expected: str = ''  # The replaced prep branch's tip, as shown.
    msgid: str = ''  # The posted series, for b4 prep -F.
    subject: str = ''
    revision: int = 0  # The revision the prep branch prepares.
    error: str = ''


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

    def snapshot(self, workspace: Workspace, trees: bool = True) -> BranchesSnapshot:
        """Everything Branches lists; without *trees*, session trees are left out (the
        Workbench shows them) but rebase and apply sessions' stay (they serve bases)."""
        items: list[BranchItem] = []
        details: dict[str, tuple[DetailRow, ...]] = {}
        statuses: dict[str, BaseStatus] = {}
        problems: list[str] = []
        rebases: dict[str, str] = {}
        views = [(k, v) for k, v in self._tree_views() if trees or v.scope in ('rebase', 'apply')]
        for project in workspace.projects:
            repo = self.root / project.path
            if not (repo / '.git').exists():
                continue
            for base in project.bases:
                key = f'base:{project.id}:{base.branch}'
                status = base_status(repo, base)
                statuses[key] = status
                info, flagged = _base_info(status, self.root)
                items.append(
                    BranchItem(key, 'base', project.id, _base_label(status), info, flagged)
                )
                details[key] = _base_rows(status, repo)
            for ref in base_refs(repo, project):
                key = f'baseref:{project.id}:{ref}'
                info = (
                    f'{ref}: a read-only base (base_refs)',
                    'Start trees, prep branches (P, n), and new branches (b) here',
                )
                items.append(BranchItem(key, 'baseref', project.id, ref, info))
                details[key] = _recent_rows(repo, ref)
            checked_out = set()
            for path, branch in _worktrees(repo):
                key = f'worktree:{project.id}:{path}'
                checked_out.add(branch)
                label, rows = _worktree(pathlib.Path(path), branch, self.root)
                changed = any(r.text == 'Changes' for r in rows)
                where = _short_path(pathlib.Path(path), self.root)
                info = (f'Checkout at {where}', 'Uncommitted changes' if changed else 'Clean')
                items.append(BranchItem(key, 'worktree', project.id, label, info, changed))
                details[key] = rows
            for key, record in ((k, r) for k, r in views if r.project == project.id):
                assert record.worktree is not None
                tree = pathlib.Path(record.worktree.path)
                if not tree.is_dir():
                    continue
                if record.scope == 'rebase':
                    rebases[key] = record.id
                    rows, label, found = self._rebase_tree(record, tree)
                elif record.scope == 'apply':
                    rebases[key] = record.id  # A accepts its result onto the base.
                    rows, label, found = self._tree(record, tree)
                else:
                    rows, label, found = self._tree(record, tree)
                problems += found
                items.append(BranchItem(key, 'tree', project.id, label))
                details[key] = rows
            work_views = [
                (k, r)
                for k, r in self._tree_views()
                if r.project == project.id and r.scope not in ('rebase', 'apply')
            ]
            for key, record in work_views:
                for item, rows in self._work(key.removeprefix('tree:'), record):
                    items.append(item)
                    details[item.key] = rows
            try:
                names = _git(repo, 'for-each-ref', '--format=%(refname:short)', 'refs/heads/')
            except OSError as ex:
                problems.append(f'{project.id}: {ex}')
                continue
            made_here = _pluck_branches(repo)
            tracked = tracked_branches(repo)
            beyond = _own_bases(repo, project)
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
                    if kind == 'branch' and name in tracked:
                        rows = _beyond_rows(repo, name, beyond)
                        commits = sum(1 for r in rows if r.key and not r.show_text)
                        upstream = _upstream(repo, name)
                        info = (
                            f'Tracks {upstream or "a remote branch (gone)"} (track_branches)',
                            f'{_count(commits, "commit")} beyond the bases and refs',
                        )
                        label = f'{name} · {_count(commits, "commit")}'
                        items.append(BranchItem(key, kind, project.id, label, info, not upstream))
                        details[key] = rows
                        continue
                    rows = _prep_rows(repo, name) if kind == 'prep' else _unpushed_rows(repo, name)
                    commits = sum(1 for r in rows if r.key and not r.show_text)
                    if kind == 'branch' and not commits and name not in made_here:
                        continue  # Nothing of ours on it (and not made to pluck onto).
                    # Under their own section, review and other branches need no more words.
                    note = (
                        f'{commits} not upstream' if kind == 'branch' else _count(commits, 'commit')
                    )
                    what = {
                        'prep': 'b4 prep branch',
                        'review': "b4's review branch",
                        'branch': 'Local branch: commits on no remote',
                    }[kind]
                    info = (f'{what} {name}', note)
                    items.append(BranchItem(key, kind, project.id, f'{name} · {note}', info))
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
            sent = f'Sent {", ".join(tracking["sent"])}' if tracking['sent'] else 'Never sent'
            info = (
                f'b4 prep branch {branch}',
                f'Preparing v{tracking["revision"]}: {_count(commits, "commit")}',
                sent,
            )
            items[n] = dataclasses.replace(item, label=label, info=info)
        targets, tips = {}, {}
        for item in items:
            project = workspace.project(item.project)
            bases = [f'base:{item.project}:{b.branch}' for b in project.bases] if project else []
            prep_keys = [
                i.key for i in items if i.kind in ('prep', 'branch') and i.project == item.project
            ]
            tree_keys = [
                i.key
                for i in items
                if i.kind == 'tree' and i.project == item.project and i.key not in rebases
            ]
            keys = (*bases, *prep_keys, *tree_keys)
            targets[item.key] = tuple(k for k in keys if k != item.key)
        for key, status in statuses.items():
            tips[key] = status.tip
        for key, record in views:
            assert record.worktree is not None
            tips[key] = series_tip(  # Per branch, at copy time.
                pathlib.Path(record.worktree.path), record.worktree
            )
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

    def _work(
        self, where: str, record: SessionRecord
    ) -> list[tuple[BranchItem, tuple[DetailRow, ...]]]:
        """A session tree's agent branches with commits, one item each (Session work)."""
        assert record.worktree is not None
        tree = pathlib.Path(record.worktree.path)
        if not tree.is_dir():
            return []
        found = []
        for branch in classify_tree(tree, record.worktree).branches:
            if not branch.commits:
                continue
            name = branch.name.removeprefix('b4m/')
            label = f'{record.id} {name} · {_count(len(branch.commits), "commit")}'
            info = (
                f'{branch.kind} {branch.name} of session {record.id}: {session_label(record)}',
                f'In its tree {tree}',
                *((f'! {branch.problem}',) if branch.problem else ()),
            )
            item = BranchItem(
                f'work:{where}:{branch.name}',
                'work',
                record.project,
                label,
                info,
                bool(branch.problem),
            )
            rows = [DetailRow(f'{branch.kind} {branch.name} (session {record.id})')]
            if branch.problem:
                rows.append(DetailRow(f'  ! {branch.problem}'))
            rows += [
                dataclasses.replace(_commit_row(c.sha, c.subject, tree), branch=branch.name)
                for c in branch.commits
            ]
            found.append((item, tuple(rows)))
        return found

    def branch_starts(self, workspace: Workspace, project_id: str) -> list[str]:
        """Where a new branch may start: the project's bases, their upstreams, then its
        prep branches (any other ref may be typed)."""
        project = workspace.project(project_id)
        if project is None:
            return []
        starts = [b.branch for b in project.bases] + [b.upstream for b in project.bases]
        starts += base_refs(self.root / project.path, project)
        starts += _prep_branches(self.root / project.path)
        return list(dict.fromkeys(s for s in starts if s))

    def new_branch(self, workspace: Workspace, project_id: str, name: str, start: str) -> CutResult:
        """A plain local branch *name* at *start*, to pluck onto (blocking)."""
        project = workspace.project(project_id)
        if project is None:
            return CutResult(False, error=f'no project {project_id}')
        repo = self.root / project.path
        check = subprocess.run(
            ['git', 'check-ref-format', '--branch', name], cwd=repo, capture_output=True
        )
        if check.returncode != 0 or name.startswith(('b4/', 'b4m/')):
            return CutResult(False, error=f'{name!r} is not a branch name to use here')
        try:
            _git(repo, 'rev-parse', '--verify', '--quiet', f'refs/heads/{name}')
            return CutResult(False, error=f'{name} exists')
        except OSError:
            pass
        try:
            sha = _git(repo, 'rev-parse', '--verify', '--quiet', f'{start}^{{commit}}').strip()
            _git(repo, 'branch', name, sha)
            _git(repo, 'config', f'branch.{name}.b4m-pluck', 'true')
        except OSError:
            return CutResult(False, error=f'{start} is not a commit in {project_id}')
        return CutResult(True, name)

    def _tree(
        self, record: SessionRecord, tree: pathlib.Path
    ) -> tuple[tuple[DetailRow, ...], str, list[str]]:
        assert record.worktree is not None
        classed = classify_tree(tree, record.worktree)
        state: TreeState = inspect_tree(tree, series_tip(tree, record.worktree), record.worktree)
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
        which = f' ({record.project})' if record.trees else ''  # A workbench session's tree.
        label = f'{session_label(record)}{which} · {record.id}{parent} · {state.describe()}'
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

    def apply_session(
        self,
        workspace: Workspace,
        target: str,
        series: Target,
        numbers: Sequence[int],
        failed: 'ApplyResult',
    ) -> SessionRecord:
        """An apply session for a series that did not apply to *target* (a base or a prep
        branch): b4's mbox of the picked patches, applied by an agent on a copy."""
        from .adapters.apply import apply_mbox

        if self._sessions is None:
            raise SessionError('agent sessions are not available here')
        project, branch = self._target(workspace, target)
        repo = self.root / project.path
        expected = _git(repo, 'rev-parse', f'refs/heads/{branch}').strip()
        ambytes = apply_mbox(repo, series.source, series.revision, numbers)
        hub = self._sessions
        situation = hub.situation(workspace, hub.overview(), (), project.id, bases=False)
        return hub.launch_apply(
            series, branch, expected, ambytes, numbers, failed.output or failed.error, situation
        )

    def accept(self, workspace: Workspace, sid: str) -> RebaseResult:
        """Move the session's base to its result: a rebase session's (see accept_rebase's
        guards) or an apply session's (accept_onto's)."""
        record = next(
            (r for r in self._records() if r.id == sid and r.scope in ('rebase', 'apply')), None
        )
        if record is None or record.worktree is None:
            return RebaseResult(False, error=f'no rebase or apply session {sid}')
        branch = record.choices.get('base', '')
        project = workspace.project(record.project)
        if record.scope == 'apply':
            if project is None:
                return RebaseResult(False, error=f'no project {record.project}')
            return accept_onto(
                self.root / project.path,
                branch,
                pathlib.Path(record.worktree.path),
                record.worktree.branch,
                record.choices.get('expected', ''),
            )
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

    def tree_targets(
        self, workspace: Workspace, item: str
    ) -> tuple[list[tuple[str, str]], dict[str, str]]:
        """Where commits of session tree *item* may go, with each one's tip: its project's
        bases and prep branches, and the project's other session trees (not rebases)."""
        project = self._project_of(workspace, item)
        if project is None:
            return [], {}
        repo = self.root / project.path
        names = _git(repo, 'for-each-ref', '--format=%(refname:short) %(objectname)', 'refs/heads/')
        heads = dict(line.split(' ', 1) for line in names.splitlines() if ' ' in line)
        targets: list[tuple[str, str]] = []
        tips: dict[str, str] = {}
        for base in project.bases:
            if base.branch in heads:
                key = f'base:{project.id}:{base.branch}'
                targets.append((key, f'{base.branch} (maintainer base)'))
                tips[key] = heads[base.branch]
        for name in sorted(heads):
            if name.startswith('b4/') and not name.startswith('b4/review/'):
                key = f'prep:{project.id}:{name}'
                targets.append((key, f'{name} (prep branch)'))
                tips[key] = heads[name]
        for key, view in self._tree_views():
            if key == item or view.project != project.id or view.scope == 'rebase':
                continue
            assert view.worktree is not None
            if not pathlib.Path(view.worktree.path).is_dir():
                continue
            targets.append((key, f'{session_label(view)} · {view.id} (session tree)'))
            tips[key] = series_tip(pathlib.Path(view.worktree.path), view.worktree)
        return targets, tips

    def _project_of(self, workspace: Workspace, item: str) -> Optional[Project]:
        if item.startswith('tree:'):
            record = self._record(item.removeprefix('tree:'))
            return workspace.project(record.project) if record else None
        parts = item.split(':', 2)
        return workspace.project(parts[1]) if len(parts) == 3 else None

    def _model(self, item: str) -> str:
        if item.startswith(('tree:', 'work:')):
            where = item.split(':', 2)[1] if item.startswith('work:') else item[5:]
            record = self._record(where)
            if record is not None:
                return record.model or record.preset or 'LLM'
        return 'LLM'

    def _target(self, workspace: Workspace, target: str) -> tuple[Project, str]:
        """(project, branch) of a pluck target: a base, a prep branch, or a branch."""
        kind, pid, branch = target.split(':', 2)
        project = workspace.project(pid)
        if project is None or kind not in ('base', 'prep', 'branch'):
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
                else series_tip(tree, record.worktree)
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
        inbox = self._out(record.id) / 'work' / 'context' / 'inbox.md'
        inbox.parent.mkdir(parents=True, exist_ok=True)
        when = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
        with inbox.open('a') as f:
            f.write(f'\n{when}: the maintainer copied commits here from {source}:\n')
            f.write('\n'.join(arrived) + '\n')
        return PluckResult(True, '', tuple(picked), tuple(dropped))

    # Where plucked commits came from: var/branches/<project>/plucked.json, local.

    def apply_choices(
        self, workspace: Workspace, project_id: str, source: str, revision: int
    ) -> 'ApplyChoices':
        """What applying a posted series offers: its patches, and every project's bases and
        prep branches with their tips, the series' own project (*project_id*) first (a
        series may be routed to the wrong project, or to none)."""
        from .adapters.apply import series_patches  # b4: only when applying.

        projects = sorted(
            (p for p in workspace.projects if (self.root / p.path / '.git').exists()),
            key=lambda p: p.id != project_id,
        )
        if not projects:
            return ApplyChoices(problem='no project has a checkout')
        targets: list[tuple[str, str]] = []
        tips: dict[str, str] = {}
        for project in projects:
            repo = self.root / project.path
            names = _git(
                repo, 'for-each-ref', '--format=%(refname:short) %(objectname)', 'refs/heads/'
            )
            heads = dict(line.split(' ', 1) for line in names.splitlines() if ' ' in line)
            for base in project.bases:
                if base.branch in heads:
                    key = f'base:{project.id}:{base.branch}'
                    targets.append((key, base.branch))
                    tips[key] = heads[base.branch]
            for name in sorted(heads):
                if name.startswith('b4/') and not name.startswith('b4/review/'):
                    key = f'prep:{project.id}:{name}'
                    targets.append((key, name))
                    tips[key] = heads[name]
        if not targets:
            return ApplyChoices(problem='no project has a maintainer base or prep branch')
        try:
            patches = tuple(series_patches(self.root / projects[0].path, source, revision))
        except Exception as ex:  # b4 reports through exceptions; the reason is shown.
            return ApplyChoices(problem=f'b4 cannot read the series: {ex}')
        return ApplyChoices(patches, tuple(targets), tips)

    def apply(
        self,
        workspace: Workspace,
        target: str,
        source: str,
        revision: int,
        numbers: Sequence[int],
        expected: str,
    ) -> 'ApplyResult':
        """Patches *numbers* of a posted series onto *target* (a base or a prep branch)."""
        from .adapters.apply import ApplyResult, apply_series

        try:
            project, branch = self._target(workspace, target)
        except SessionError as ex:
            return ApplyResult(False, error=str(ex))
        return apply_series(
            self.root / project.path,
            branch,
            source,
            revision,
            numbers,
            expected,
            record_take=target.startswith('base:'),
        )

    def drop_choices(
        self, workspace: Workspace, project_id: str, patches: Sequence[tuple[str, str]]
    ) -> DropChoices:
        """Every project's bases that hold commits of a posted series (*patches*:
        (message-id, subject)), the series' own project first, with those commits and the
        bases' tips."""
        targets: list[tuple[str, str]] = []
        commits: dict[str, tuple[BaseCommit, ...]] = {}
        tips: dict[str, str] = {}
        projects = sorted(workspace.projects, key=lambda p: p.id != project_id)
        for project in projects:
            repo = self.root / project.path
            if not (repo / '.git').exists():
                continue
            for base in project.bases:
                found = series_commits(repo, base, patches)
                if found:
                    key = f'base:{project.id}:{base.branch}'
                    targets.append((key, base.branch))
                    commits[key] = tuple(found)
                    tips[key] = _git(repo, 'rev-parse', f'refs/heads/{base.branch}').strip()
        if not targets:
            return DropChoices(problem='not on any maintainer base')
        return DropChoices(tuple(targets), commits, tips)

    def drop(
        self, workspace: Workspace, target: str, shas: Sequence[str], expected: str
    ) -> PluckResult:
        """*target* (a base) without the commits *shas*, the later ones replayed."""
        try:
            project, branch = self._target(workspace, target)
        except SessionError as ex:
            return PluckResult(False, error=str(ex))
        if not target.startswith('base:'):
            return PluckResult(False, error='series are dropped from maintainer bases only')
        return drop_commits(self.root / project.path, branch, shas, expected)

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
            sid = item.removeprefix('tree:').partition('/')[0]
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
        points += base_refs(self.root / project.path, project)
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

    def fold_plan(
        self, workspace: Workspace, item: str, folded: str, series: Mapping[str, 'Series']
    ) -> FoldPlan:
        """What folding *folded* (a branch of tree *item*) into a prep branch involves
        (blocking): its manifest's pull-aheads, and the prep branch it lands on. *series*:
        cached series by identity (a workbench session's is found by patch subjects)."""
        where = item.removeprefix('tree:')
        record = self._record(where)
        project = workspace.project(record.project) if record is not None else None
        if record is None or record.worktree is None or project is None:
            return FoldPlan(error=f'no session tree {where}')
        tree, base = record.worktree.path, record.worktree.base
        classed = classify_tree(pathlib.Path(tree), record.worktree)
        branches = {b.name: b for b in classed.branches}
        branch = branches.get(folded)
        if branch is None or branch.kind != 'folded':
            return FoldPlan(error=f'{folded} is not a folded series in {where}')
        if branch.problem or not branch.commits:
            return FoldPlan(error=f'{folded}: {branch.problem or "no commits"}')
        artifacts = load_artifacts(self._out(record.id) / 'work' / 'out', set(branches))[0]
        titles = {a.branch: a.title for a in artifacts if a.branch}
        needs = next((a.needs for a in artifacts if a.branch == folded), ())
        optional = tuple(
            (b.name, titles.get(b.name) or b.commits[-1].subject)
            for b in classed.branches
            if b.kind == 'pull-ahead' and b.name not in needs and b.commits and not b.problem
        )
        plan = FoldPlan(
            project.id,
            tree,
            base,
            folded,
            len(branch.commits),
            tuple(needs),
            optional,
        )
        repo = self.root / project.path
        posted = _posted_series(record.series, [c.subject for c in branch.commits], series)
        identity = posted.identity if posted is not None else record.series
        change_id = identity.removeprefix('change-id:') if identity.startswith('change-id:') else ''
        for name in _prep_branches(repo) if change_id else ():
            tracking = prep_tracking(repo, name)
            if tracking.get('change-id') == change_id:
                tip = _git(repo, 'rev-parse', name).strip()
                return dataclasses.replace(
                    plan,
                    target=name,
                    replaces=True,
                    expected=tip,
                    subject=posted.subject if posted is not None else name,
                    revision=int(tracking['revision']),
                )
        if posted is None or not posted.msgids:
            return dataclasses.replace(
                plan, error='its series is not in the cache (F5 fetches it), and has no prep branch'
            )
        return dataclasses.replace(
            plan,
            target=_branch_name(posted.subject),
            msgid=posted.msgids[0],
            subject=posted.subject,
            revision=posted.revision + 1,
        )

    def fold_to_prep(
        self, workspace: Workspace, plan: FoldPlan, extras: Sequence[str], name: str
    ) -> CutResult:
        """Make the prep branch *plan* describes (blocking): the pull-aheads *extras*
        first, then the folded series, on its base. A conflict or a moved branch changes
        nothing; a new prep branch that cannot take them is removed."""
        project = workspace.project(plan.project)
        if project is None or plan.error:
            return CutResult(False, error=plan.error or f'no project {plan.project}')
        repo = self.root / project.path
        wanted = [*(e for e in extras if e not in plan.needs), plan.folded]
        refs = [f'refs/b4m/fold/{n}' for n in range(len(wanted))]
        specs = [f'+refs/heads/{b}:{r}' for b, r in zip(wanted, refs)]
        try:
            _git(repo, 'fetch', '--quiet', '--no-tags', plan.tree, *specs)
            shas: list[str] = []
            for ref in refs:
                for sha in _git(repo, 'rev-list', '--reverse', f'{plan.base}..{ref}').split():
                    if sha not in shas:
                        shas.append(sha)
            if plan.replaces:
                result = reroll_prep(repo, plan.target, plan.base, shas, plan.expected)
                branch = plan.target
            else:
                worktree = self.root / 'var' / 'branches' / project.id / name
                try:
                    branch = prep_from_thread(repo, worktree, name, plan.base, plan.msgid)
                except PrepError as ex:
                    return CutResult(False, error=str(ex))
                tip = _git(repo, 'rev-parse', branch).strip()
                result = reroll_prep(repo, branch, plan.base, shas, tip)
                if not result.ok:
                    remove_prep(repo, worktree, branch)
        except (OSError, PrepError) as ex:
            return CutResult(False, error=str(ex))
        finally:
            for ref in refs:
                subprocess.run(['git', 'update-ref', '-d', ref], cwd=repo, capture_output=True)
        if not result.ok:
            why = f'{result.failed} does not apply' if result.failed else result.error
            return CutResult(False, error=why)
        return CutResult(True, branch, len(shas))

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
        where = item.removeprefix('tree:')  # The tree: <sid>, or <sid>/<project>.
        record = self._record(where)
        if record is None or record.worktree is None:
            return []
        wt, sid = record.worktree, where
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
        where = item.removeprefix('tree:')
        sid = where.partition('/')[0]
        if row is not None and row.artifact is not None:
            text = self.show(row)
            return ContextItem('parent', f'From session {sid}: {row.artifact.title}', text=text)
        record = self._record(where)
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
        """Session *sid*; `<sid>/<project>` names one tree of a workbench session, as a
        view of the record with that tree as its worktree and that project as its own."""
        sid, _, project = sid.partition('/')
        record = next((r for r in self._records() if r.id == sid), None)
        if record is None or not project:
            return record
        tree = next((t for t in record.trees if t.project == project), None)
        return record.replace(worktree=tree.worktree, project=project) if tree else None

    def _tree_views(self) -> list[tuple[str, SessionRecord]]:
        """(item key, record) per session tree: `tree:<sid>`, and per workbench tree
        `tree:<sid>/<project>` with a view record holding that tree."""
        views: list[tuple[str, SessionRecord]] = []
        for record in self._records():
            if record.trees:
                for t in record.trees:
                    view = record.replace(worktree=t.worktree, project=t.project)
                    views.append((f'tree:{record.id}/{t.project}', view))
            elif record.worktree is not None:
                views.append((f'tree:{record.id}', record))
        return views

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
    if not status.exists:
        return f'{b.branch} · no such branch'
    return f'{b.branch} · {_count(len(status.commits), "commit")}'


def _base_info(
    status: BaseStatus, root: Optional[pathlib.Path] = None
) -> tuple[tuple[str, ...], bool]:
    """A base's details, and whether one needs attention (missing, behind its upstream,
    checked out, or unreadable)."""
    b = status.base
    if not status.exists:
        return (f'No branch {b.branch}', f'Upstream {b.upstream}'), True
    info = [f'On {b.upstream}']
    if status.behind:
        info.append(f'{status.behind} behind: r rebases it')
    landed = sum(c.landed for c in status.commits)
    info.append(_count(len(status.commits), 'commit') + (f', {landed} landed' if landed else ''))
    if status.checked_out:
        where = _short_path(pathlib.Path(status.checked_out), root)
        info.append(f'Checked out in {where} (moved only when not)')
    if status.error:
        info.append(status.error)
    return tuple(info), bool(status.behind or status.checked_out or status.error)


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


def _worktree(
    path: pathlib.Path, branch: str, root: Optional[pathlib.Path] = None
) -> tuple[str, tuple[DetailRow, ...]]:
    """A checkout: its branch and state first, then where it is (under *root* or ~)."""
    where = _short_path(path, root)
    try:
        changed = [line for line in _git(path, 'status', '--porcelain').splitlines() if line]
    except OSError as ex:
        return f'{where} · {ex}', ()
    state = f'{len(changed)} changed' if changed else 'clean'
    label = f'{branch or "detached"} · {state}'  # Where it is: in its details.
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


def _prep_branches(repo: pathlib.Path) -> list[str]:
    """The project's b4 prep branches (b4/<name>, not b4's review branches)."""
    names = _git(repo, 'for-each-ref', '--format=%(refname:short)', 'refs/heads/b4/')
    return [n for n in names.split() if not n.startswith('b4/review/')]


def _posted_series(
    identity: str, subjects: Sequence[str], series: Mapping[str, 'Series']
) -> Optional['Series']:
    """The series a session's folded branch is of: its own, else (a workbench session)
    the cached series sharing the most patch subjects with it."""
    if identity in series:
        return series[identity]
    wanted = set(subjects)
    best = max(
        series.values(),
        key=lambda s: len(wanted & {p.subject for p in s.patches}),
        default=None,
    )
    if best is None or not wanted & {p.subject for p in best.patches}:
        return None
    return best


def _branch_name(subject: str) -> str:
    """A prep branch name from a series subject: its words after the subsystem prefix."""
    words = re.findall(r'[a-z0-9]+', subject.rsplit(': ', 1)[-1].lower())
    return '-'.join(words)[:40].strip('-') or 'series'


def _pluck_branches(repo: pathlib.Path) -> set[str]:
    """Branches made here to pluck onto (b, marked in their git configuration)."""
    run = subprocess.run(
        ['git', 'config', '--get-regexp', r'^branch\..*\.b4m-pluck$'],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    names = set()
    for line in run.stdout.splitlines():
        key = line.split(' ', 1)[0]
        names.add(key.removeprefix('branch.').removesuffix('.b4m-pluck'))
    return names


def _short_path(path: pathlib.Path, root: Optional[pathlib.Path]) -> str:
    """*path* relative to *root* when under it, else with ~ for the home directory."""
    resolved = path.resolve()
    if root is not None and resolved.is_relative_to(root.resolve()):
        return str(resolved.relative_to(root.resolve()))
    home = pathlib.Path.home()
    return f'~/{resolved.relative_to(home)}' if resolved.is_relative_to(home) else str(path)


def base_refs(repo: pathlib.Path, project: Project) -> list[str]:
    """The refs matching the project's base_refs globs (read-only bases), sorted."""
    return match_refs(repo, project.base_refs)


def _recent_rows(repo: pathlib.Path, ref: str, limit: int = 20) -> tuple[DetailRow, ...]:
    """A read-only base: its latest commits."""
    try:
        log = _git(repo, 'log', f'-{limit}', '--format=%H%x00%s', ref)
    except OSError as ex:
        return (DetailRow(f'  ! {ex}'),)
    rows = [DetailRow(f'Latest on {ref}')]
    for line in log.splitlines():
        sha, _, subject = line.partition('\x00')
        rows.append(_commit_row(sha, subject, repo))
    return tuple(rows)


def _own_bases(repo: pathlib.Path, project: Project) -> list[str]:
    """What a tracked branch's own commits are counted beyond: the bases, their
    upstreams, the base refs, and the project's refs."""
    found = [b.branch for b in project.bases] + [b.upstream for b in project.bases]
    found += [r.name for r in project.refs] + base_refs(repo, project)
    return [f for f in dict.fromkeys(found) if f]


def _beyond_rows(repo: pathlib.Path, branch: str, beyond: Sequence[str]) -> tuple[DetailRow, ...]:
    existing = []
    for ref in beyond:
        try:
            _git(repo, 'rev-parse', '--verify', '--quiet', f'{ref}^{{commit}}')
            existing.append(ref)
        except OSError:
            pass
    try:
        log = _git(repo, 'log', f'-{LIST_LIMIT}', '--format=%H%x00%s', branch, '--not', *existing)
    except OSError as ex:
        return (DetailRow(f'  ! {ex}'),)
    rows = [DetailRow('Its own commits')]
    for line in log.splitlines():
        sha, _, subject = line.partition('\x00')
        rows.append(_commit_row(sha, subject, repo))
    return tuple(rows)


def _upstream(repo: pathlib.Path, branch: str) -> str:
    try:
        return _git(repo, 'rev-parse', '--abbrev-ref', f'{branch}@{{upstream}}').strip()
    except OSError:
        return ''

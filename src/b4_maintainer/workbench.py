# SPDX-License-Identifier: GPL-2.0-or-later
"""Workbenches: named recipes for agent sessions over several projects.

A workbench holds, per project, the base its tree starts from and the items applied on it
in order: posted series (applied by b4) and local branches (their own commits replayed: a
project branch, or `tree:<session>:<branch>` from another session's tree). Each is one
TOML file in state/workbenches/ (synced; another machine's edit to a different workbench
never collides). Edits are pure functions returning the new workbench; the store reads
and writes the files. WorkbenchService gathers what the Workbench screen offers and
previews (plain git; blocking: call from a worker). Preparing trees is the sessions'.
"""

import json
import os
import pathlib
import re
import subprocess
import tomllib
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional, Sequence

from .attachments import Attachment, gather, preview
from .model import Series
from .sessions.records import SessionRecord, session_trees
from .sessions.trees import TreeItem, classify_tree
from .workspace import Workspace

if TYPE_CHECKING:
    from .sessions.hub import TreePlan

DIRECTORY = 'workbenches'  # Under state/.
_TOP = frozenset({'label', 'origin', 'entry'})
_ENTRY = frozenset({'project', 'base', 'items'})
_ITEM = frozenset({'series', 'revision', 'subject', 'branch', 'mail', 'file', 'msgid'})
_KINDS = ('series', 'branch', 'mail', 'file')  # An item is exactly one of these.


class WorkbenchError(ValueError):
    pass


@dataclass(frozen=True)
class Item:
    series: str = ''  # A posted series' identity, applied by b4 at *revision*.
    revision: int = 0
    subject: str = ''  # As shown.
    branch: str = ''  # Else a local branch: a project ref, or tree:<session>:<branch>.
    mail: str = ''  # Else a discussion's identity: read, never applied.
    file: str = ''  # Else a file from disk: applied if b4 finds patches in it, else read.
    msgid: str = ''  # A message of a mail item's thread: kept in the cache, fetched by it.

    @property
    def label(self) -> str:
        if self.series:
            return f'{self.subject} v{self.revision}'
        if self.mail:
            return f'{self.subject} (discussion)'
        if self.file:
            return f'{pathlib.Path(self.file).name} (file)'
        return self.branch


@dataclass(frozen=True)
class Entry:
    project: str
    base: str = ''  # Where the tree starts; empty: b4 chooses for the first series.
    items: tuple[Item, ...] = ()


@dataclass(frozen=True)
class Workbench:
    id: str
    label: str
    entries: tuple[Entry, ...] = ()
    origin: str = ''  # Made by F1 for one series: its identity (found again by it).

    def entry(self, project: str) -> Optional[Entry]:
        return next((e for e in self.entries if e.project == project), None)


# The file.


def _known(table: Mapping[str, Any], keys: frozenset[str], where: str) -> None:
    for key in table:
        if key not in keys:
            raise WorkbenchError(f'unknown key {key} {where}')


def _item(raw: Any, project: str) -> Item:
    if not isinstance(raw, dict):
        raise WorkbenchError(f'an item of {project} is not a table')
    _known(raw, _ITEM, f'in an item of {project}')
    if sum(1 for k in _KINDS if raw.get(k)) != 1:
        kinds = ', '.join(_KINDS)
        raise WorkbenchError(f'an item of {project} is one of {kinds}, not several or none')
    return Item(
        str(raw.get('series', '')),
        int(raw.get('revision', 0)),
        str(raw.get('subject', '')),
        str(raw.get('branch', '')),
        str(raw.get('mail', '')),
        str(raw.get('file', '')),
        str(raw.get('msgid', '')),
    )


def parse_workbench(text: str, wid: str) -> Workbench:
    """Workbench *wid* from its file; WorkbenchError says what is wrong."""
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as ex:
        raise WorkbenchError(f'not valid TOML: {ex}') from ex
    _known(raw, _TOP, '(top level)')
    entries: list[Entry] = []
    for table in raw.get('entry', []):
        _known(table, _ENTRY, 'in an [[entry]]')
        project = str(table.get('project', ''))
        if not project:
            raise WorkbenchError('an [[entry]] has no project')
        if any(e.project == project for e in entries):
            raise WorkbenchError(f'project {project} appears twice')
        items = tuple(_item(i, project) for i in table.get('items', []))
        entries.append(Entry(project, str(table.get('base', '')), items))
    return Workbench(wid, str(raw.get('label', wid)), tuple(entries), str(raw.get('origin', '')))


def _s(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)  # A TOML basic string.


def workbench_toml(bench: Workbench) -> str:
    lines = [f'label = {_s(bench.label)}']
    if bench.origin:
        lines.append(f'origin = {_s(bench.origin)}')
    for entry in bench.entries:
        lines += ['', '[[entry]]', f'project = {_s(entry.project)}', f'base = {_s(entry.base)}']
        lines.append('items = [')
        for item in entry.items:
            if item.series:
                fields = (
                    f'series = {_s(item.series)}, revision = {item.revision}, '
                    f'subject = {_s(item.subject)}'
                )
            elif item.mail:
                fields = f'mail = {_s(item.mail)}, subject = {_s(item.subject)}'
            elif item.file:
                fields = f'file = {_s(item.file)}'
            else:
                fields = f'branch = {_s(item.branch)}'
            if item.msgid:
                fields += f', msgid = {_s(item.msgid)}'
            lines.append(f'  {{ {fields} }},')
        lines.append(']')
    return '\n'.join(lines) + '\n'


class WorkbenchStore:
    """state/workbenches/<id>.toml, one per workbench."""

    def __init__(self, directory: pathlib.Path) -> None:
        self.directory = directory

    def _path(self, wid: str) -> pathlib.Path:
        return self.directory / f'{wid}.toml'

    def list(self) -> tuple[list[Workbench], list[str]]:
        """The workbenches by label, and a problem per file that does not read."""
        found: list[Workbench] = []
        problems: list[str] = []
        if not self.directory.is_dir():
            return found, problems
        for path in sorted(self.directory.glob('*.toml')):
            try:
                found.append(parse_workbench(path.read_text(), path.stem))
            except (OSError, WorkbenchError) as ex:
                problems.append(f'workbench {path.stem}: {ex}')
        return sorted(found, key=lambda w: (w.label.lower(), w.id)), problems

    def get(self, wid: str) -> Optional[Workbench]:
        path = self._path(wid)
        return parse_workbench(path.read_text(), wid) if path.is_file() else None

    def save(self, bench: Workbench) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp = self.directory / f'.{bench.id}.toml.tmp'  # Dotfiles never sync.
        tmp.write_text(workbench_toml(bench))
        os.replace(tmp, self._path(bench.id))

    def delete(self, wid: str) -> None:
        self._path(wid).unlink(missing_ok=True)

    def new_id(self, label: str) -> str:
        """A file name for a new workbench called *label*, not taken yet."""
        base = re.sub(r'[^a-z0-9]+', '-', label.lower()).strip('-')[:40].rstrip('-') or 'bench'
        wid, n = base, 1
        while self._path(wid).exists():
            n += 1
            wid = f'{base}-{n}'
        return wid


def series_bench(
    store: WorkbenchStore, project: str, series: str, revision: int, subject: str, base: str
) -> Workbench:
    """The workbench F1 files a series' sessions in: the one made for it before (by its
    origin), else a new one (named after the series, the series on *base*), saved."""
    benches, _problems = store.list()
    found = next((b for b in benches if b.origin == series), None)
    if found is not None:
        return found
    item = Item(series, revision, subject)
    bench = Workbench(store.new_id(subject), subject, (Entry(project, base, (item,)),), series)
    store.save(bench)
    return bench


# Edits.


def _with_entry(bench: Workbench, entry: Entry) -> Workbench:
    """*bench* with *entry* replacing its project's (or added last); an empty one goes."""
    entries = list(bench.entries)
    at = next((n for n, e in enumerate(entries) if e.project == entry.project), None)
    if at is None:
        entries.append(entry)
    elif entry.items:
        entries[at] = entry
    else:
        del entries[at]
    return replace(bench, entries=tuple(entries))


def _entry(bench: Workbench, project: str, base: str = '') -> Entry:
    return bench.entry(project) or Entry(project, base)


def add_series(
    bench: Workbench,
    project: str,
    series: str,
    revision: int,
    subject: str,
    base: str = '',
    msgid: str = '',
) -> Workbench:
    """The series last under *project* (an entry made at *base* if it has none)."""
    entry = _entry(bench, project, base)
    if any(i.series == series for i in entry.items):
        raise WorkbenchError(f'{subject} is already on this workbench')
    item = Item(series, revision, subject, msgid=msgid)
    return _with_entry(bench, replace(entry, items=entry.items + (item,)))


def add_mail(
    bench: Workbench, project: str, mail: str, subject: str, msgid: str = '', base: str = ''
) -> Workbench:
    """A discussion last under *project*: read by sessions, never applied."""
    entry = _entry(bench, project, base)
    if any(i.mail == mail for i in entry.items):
        raise WorkbenchError(f'{subject} is already on this workbench')
    item = Item(subject=subject, mail=mail, msgid=msgid)
    return _with_entry(bench, replace(entry, items=entry.items + (item,)))


def add_file(bench: Workbench, project: str, path: str, base: str = '') -> Workbench:
    """A file from disk last under *project*: applied if b4 finds patches in it."""
    entry = _entry(bench, project, base)
    if any(i.file == path for i in entry.items):
        raise WorkbenchError(f'{path} is already on this workbench')
    return _with_entry(bench, replace(entry, items=entry.items + (Item(file=path),)))


def add_branch(bench: Workbench, project: str, branch: str, base: str = '') -> Workbench:
    """The local branch last under *project* (an entry made at *base* if it has none)."""
    entry = _entry(bench, project, base)
    if any(i.branch == branch for i in entry.items):
        raise WorkbenchError(f'{branch} is already on this workbench')
    return _with_entry(bench, replace(entry, items=entry.items + (Item(branch=branch),)))


def remove_item(bench: Workbench, project: str, index: int) -> Workbench:
    entry = _entry(bench, project)
    items = entry.items[:index] + entry.items[index + 1 :]
    return _with_entry(bench, replace(entry, items=items))


def move_item(bench: Workbench, project: str, index: int, delta: int) -> Workbench:
    entry = _entry(bench, project)
    to = index + delta
    if not (0 <= index < len(entry.items) and 0 <= to < len(entry.items)):
        return bench
    items = list(entry.items)
    items[index], items[to] = items[to], items[index]
    return _with_entry(bench, replace(entry, items=tuple(items)))


def move_to_project(
    bench: Workbench, project: str, index: int, to: str, base: str = ''
) -> Workbench:
    """Item *index* of *project* moved last under *to* (an entry made at *base* if it has
    none); a project left empty goes."""
    entry = _entry(bench, project)
    if not 0 <= index < len(entry.items) or to == project:
        return bench
    item = entry.items[index]
    bench = remove_item(bench, project, index)
    target = _entry(bench, to, base)
    return _with_entry(bench, replace(target, items=target.items + (item,)))


def set_base(bench: Workbench, project: str, base: str) -> Workbench:
    entry = bench.entry(project)
    if entry is None:
        return bench
    return _with_entry(bench, replace(entry, base=base))


def rerolls(bench: Workbench, latest: Mapping[str, int]) -> list[tuple[str, int, int]]:
    """(project, item index, newer revision) of each series with a newer revision known."""
    return [
        (entry.project, n, latest[item.series])
        for entry in bench.entries
        for n, item in enumerate(entry.items)
        if item.series and latest.get(item.series, 0) > item.revision
    ]


def reroll(bench: Workbench, project: str, index: int, revision: int) -> Workbench:
    entry = _entry(bench, project)
    items = list(entry.items)
    items[index] = replace(items[index], revision=revision)
    return _with_entry(bench, replace(entry, items=tuple(items)))


# The screen's data.


def _has_patches(path: pathlib.Path) -> bool:
    """Whether b4 finds a patch series in a file from disk (an mbox, a .patch file)."""
    from .adapters.b4mbox import parse_mbox  # b4: only when asked.

    try:
        return any(s.patches and not s.discussion for s in parse_mbox(path))
    except Exception:
        return False


def _git(repo: pathlib.Path, *args: str) -> Optional[str]:
    run = subprocess.run(
        ['git', *args], cwd=repo, capture_output=True, text=True, stdin=subprocess.DEVNULL
    )
    return run.stdout.strip() if run.returncode == 0 else None


@dataclass(frozen=True)
class ContentRow:
    """One row of a workbench's contents (the Workbench tab's middle pane)."""

    text: str  # As listed, indented under its section.
    kind: str  # heading, entry (a project), item, mailbox, tree, commit, or artifact
    project: str = ''
    index: int = -1  # An item's place in its project's entry.
    session: str = ''
    series: str = ''  # A mailbox's series identity.
    attachment: Optional[Attachment] = None  # A commit's or artifact's, for showing.
    path: str = ''  # A tree's path.
    item: str = ''  # A tree's or commit's Branches item key (tree:<sid>[/<project>]).


class WorkbenchService:
    """Workbenches for the screen: the store, per project what may go in a tree, and
    each workbench's contents (its sessions' trees and output from *directory*)."""

    def __init__(
        self,
        root: pathlib.Path,
        store: WorkbenchStore,
        records: Callable[[], Sequence[SessionRecord]],
        directory: Optional[Callable[[str], pathlib.Path]] = None,
    ) -> None:
        self.root = root
        self.store = store
        self._records = records
        self._directory = directory

    def _repo(self, workspace: Workspace, project: str) -> Optional[pathlib.Path]:
        found = workspace.project(project)
        repo = self.root / found.path if found else None
        return repo if repo is not None and (repo / '.git').exists() else None

    def base_choices(self, workspace: Workspace, project: str) -> list[tuple[str, str]]:
        """(base, label): the project's maintainer bases, its configured refs, HEAD."""
        found = workspace.project(project)
        if found is None:
            return []
        choices = [
            (b.branch, f'{b.branch}  (maintainer base on {b.upstream})') for b in found.bases
        ]
        choices += [(r.name, f'{r.name}  ({r.authority.name.lower()})') for r in found.refs]
        choices.append(('HEAD', "HEAD  (the checkout's)"))
        return list({v: (v, label) for v, label in choices}.values())

    def branch_choices(self, workspace: Workspace, project: str) -> list[tuple[str, str]]:
        """(branch item, label): the project's local branches (not b4's review branches),
        then the branches with commits in its session trees (`tree:<tree>:<branch>`)."""
        choices: list[tuple[str, str]] = []
        repo = self._repo(workspace, project)
        if repo is not None:
            names = _git(repo, 'for-each-ref', '--format=%(refname:short)', 'refs/heads/') or ''
            for name in names.split():
                if not name.startswith(('b4/review/', 'b4m/prep-start/')):
                    choices.append((name, name))
        for record in self._records():
            for tree_project, tree in session_trees(record):
                if tree_project != project or not pathlib.Path(tree.path).is_dir():
                    continue
                where = f'{record.id}/{tree_project}' if record.trees else record.id
                for branch in classify_tree(pathlib.Path(tree.path), tree).branches:
                    if branch.commits:
                        label = f'{branch.name}  (session {record.id}, {len(branch.commits)})'
                        choices.append((f'tree:{where}:{branch.name}', label))
        return choices

    def branch_repo(self, workspace: Workspace, project: str, branch: str) -> tuple[str, str]:
        """(repository, branch) a branch item reads from: a session tree, or the project."""
        if branch.startswith('tree:'):
            where, _, name = branch.removeprefix('tree:').partition(':')
            sid, _, tree_project = where.partition('/')
            record = next((r for r in self._records() if r.id == sid), None)
            trees = session_trees(record) if record else ()
            tree = next((t for p, t in trees if not tree_project or p == tree_project), None)
            return (tree.path if tree else ''), name
        repo = self._repo(workspace, project)
        return (str(repo) if repo else ''), branch

    def plans(
        self, workspace: Workspace, bench: Workbench, series: Mapping[str, Series]
    ) -> tuple[tuple['TreePlan', ...], list[str], list[str]]:
        """*bench* as the trees a session prepares: each series from the cache (*series*,
        by identity) at the revision the workbench names, each local branch from its
        repository; what stands in the way of any launch; and what stands in the way of
        trees only (a lightweight session reads the series' mail alone)."""
        from .sessions.hub import TreePlan

        plans: list[TreePlan] = []
        problems: list[str] = []
        tree_problems: list[str] = []
        for entry in bench.entries:
            items: list[TreeItem] = []
            for item in entry.items:
                if item.mail:
                    talk = series.get(item.mail)
                    if talk is None:
                        problems.append(f'{item.label}: not in the cache (F5 fetches it)')
                    else:
                        items.append(TreeItem(item.label, talk.source, apply=False))
                    continue
                if item.file:
                    path = pathlib.Path(item.file).expanduser()
                    if not path.is_file():
                        problems.append(f'{item.label}: {path} does not exist')
                    elif _has_patches(path):
                        items.append(TreeItem(item.label, str(path)))  # b4 applies it.
                    else:
                        items.append(TreeItem(item.label, file=str(path), apply=False))
                    continue
                if item.series:
                    known = series.get(item.series)
                    if known is None:
                        problems.append(f'{item.label}: not in the cache (F5 fetches it)')
                    elif known.revision != item.revision:
                        problems.append(
                            f'{item.label}: only v{known.revision} is cached (v on F4 takes it)'
                        )
                    else:
                        items.append(TreeItem(item.label, known.source, item.revision))
                    continue
                repo, name = self.branch_repo(workspace, entry.project, item.branch)
                if not repo:
                    tree_problems.append(f'{item.branch}: its tree is gone')
                    continue
                other = repo if item.branch.startswith('tree:') else ''
                items.append(TreeItem(item.branch, branch=name, repo=other))
            plans.append(TreePlan(entry.project, entry.base, tuple(items)))
        if not bench.entries:
            problems.append(f'{bench.label} has nothing to prepare: w adds series to it')
        return tuple(plans), problems, tree_problems

    def contents(
        self,
        workspace: Workspace,
        bench: Workbench,
        series: Mapping[str, Series],
        latest: Mapping[str, int],
    ) -> list[ContentRow]:
        """*bench* in sections: Items (each project and its items, in order), Mailboxes
        (each series), Trees (each session tree and its commits by class), Artifacts
        (each session's prompt, files, and findings)."""
        rows = [ContentRow('Items', 'heading')]
        newer = {(p, n): r for p, n, r in rerolls(bench, latest)}
        for entry in bench.entries:
            where = entry.base or 'the base b4 chooses'
            rows.append(ContentRow(f'  {entry.project} on {where}', 'entry', entry.project))
            for n, item in enumerate(entry.items):
                mark = (
                    f' · v{newer[(entry.project, n)]} available'
                    if (entry.project, n) in newer
                    else ''
                )
                text = f'    {n + 1}. {item.label}{mark}'
                rows.append(ContentRow(text, 'item', entry.project, n))
        mailboxes = [
            ContentRow(
                f'  {item.label} ({entry.project})',
                'mailbox',
                entry.project,
                series=item.series or item.mail,
                path=item.file,
            )
            for entry in bench.entries
            for item in entry.items
            if item.series or item.mail or item.file
        ]
        if mailboxes:
            rows += [ContentRow('Mailboxes', 'heading'), *mailboxes]
        records = [r for r in self._records() if bench.id and r.workbench == bench.id]
        trees: list[ContentRow] = []
        artifacts: list[ContentRow] = []
        for record in sorted(records, key=lambda r: r.created, reverse=True):
            found = gather([record], record.series, self._directory) if self._directory else []
            for project, tree in session_trees(record):
                key = f'tree:{record.id}/{project}' if record.trees else f'tree:{record.id}'
                label = f'  {record.id} · {project} · {tree.branch}'
                row = ContentRow(
                    label, 'tree', project, session=record.id, path=tree.path, item=key
                )
                trees.append(row)
                for a in (x for x in found if x.kind == 'patch' and x.repo == tree.path):
                    kind = a.label.split(' · ')[1] if ' · ' in a.label else 'commit'
                    trees.append(
                        ContentRow(
                            f'    {kind}: {a.title}',
                            'commit',
                            project,
                            session=record.id,
                            attachment=a,
                            item=key,
                        )
                    )
            for a in (x for x in found if x.kind != 'patch'):
                text = f'  {record.id}: {a.label.split("  (")[0]}'
                artifacts.append(ContentRow(text, 'artifact', session=record.id, attachment=a))
        if trees:
            rows += [ContentRow('Trees', 'heading'), *trees]
        if artifacts:
            rows += [ContentRow('Artifacts', 'heading'), *artifacts]
        return rows

    def show(
        self,
        workspace: Workspace,
        bench: Workbench,
        row: Optional[ContentRow],
        series: Mapping[str, Series],
        latest: Mapping[str, int],
    ) -> str:
        """The text for a contents row: the launch preview for Items, a series' patches
        and cover letter, a tree's state and recent commits, a commit or an artifact."""
        if row is None or row.kind in ('heading', 'entry', 'item'):
            return self.preview(workspace, bench, series, latest)
        if row.attachment is not None:
            return preview(row.attachment)
        if row.kind == 'mailbox' and row.path:
            try:
                text = pathlib.Path(row.path).expanduser().read_text(errors='replace')
            except OSError as ex:
                return f'{row.path}: {ex.strerror}'
            lines = text.splitlines()
            more = f'\n… {len(lines) - 400} more lines' if len(lines) > 400 else ''
            return f'{row.path}\n\n' + '\n'.join(lines[:400]) + more
        if row.kind == 'mailbox':
            known = series.get(row.series)
            if known is None:
                return f'{row.text.strip()}: not in the cache (F5 fetches it)'
            lines = [
                f'{known.subject} v{known.revision}',
                f'From: {known.author_name} <{known.author_email}>',
                f'Lists: {", ".join(known.lists) or "-"}',
                '',
                *(f'  {p.counter}/{known.expected} {p.subject}' for p in known.patches),
            ]
            if known.preview:
                lines += ['', known.preview_label or 'Cover letter', '', known.preview]
            return '\n'.join(lines)
        if row.kind == 'tree':
            where = pathlib.Path(row.path)
            status = _git(where, 'status', '--short') if where.is_dir() else None
            log = _git(where, 'log', '--oneline', '-20', 'HEAD') if where.is_dir() else None
            if log is None:
                return f'{row.path}: the tree is gone'
            changed = f'Uncommitted:\n{status}\n\n' if status else 'No uncommitted changes.\n\n'
            return f'{row.path}\n\n{changed}Recent commits:\n{log}'
        return ''

    def preview(
        self,
        workspace: Workspace,
        bench: Workbench,
        series: Mapping[str, Series],
        latest: Mapping[str, int],
    ) -> str:
        """What launching *bench* prepares, tree by tree, and what stands in the way."""
        if not bench.entries:
            return 'Nothing yet: w adds a series from the series list or a thread.'
        lines: list[str] = []
        for entry in bench.entries:
            repo = self._repo(workspace, entry.project)
            where = f'on {entry.base}' if entry.base else 'on the base b4 chooses'
            lines.append(f'{entry.project} {where}')
            if repo is None:
                lines.append(f'  ! {entry.project} has no checkout')
                continue
            start = (
                _git(repo, 'rev-parse', '--verify', '--quiet', entry.base) if entry.base else None
            )
            if entry.base and start is None:
                lines.append(f'  ! {entry.base} is not a commit in {entry.project}')
            for n, item in enumerate(entry.items, 1):
                lines.append(
                    f'  {n}. {self._describe(workspace, entry, item, start, series, latest)}'
                )
        return '\n'.join(lines)

    def _describe(
        self,
        workspace: Workspace,
        entry: Entry,
        item: Item,
        start: Optional[str],
        series: Mapping[str, Series],
        latest: Mapping[str, int],
    ) -> str:
        if item.mail:
            if series.get(item.mail) is None:
                return f'{item.label} · not in the cache (F5 fetches it)'
            return f'{item.label} · read by the session, not applied'
        if item.file:
            path = pathlib.Path(item.file).expanduser()
            if not path.is_file():
                return f'{item.label} · {path} does not exist'
            how = 'patches: b4 applies them' if _has_patches(path) else 'read by the session'
            return f'{item.label} · {how}'
        if item.series:
            text = item.label
            known = series.get(item.series)
            if known is None:
                return f'{text} · not in the cache (F5 fetches it)'
            if known.revision == item.revision:
                text += f' · {len(known.patches)} patches'
            newer = latest.get(item.series, 0)
            return text + (f' · v{newer} available (v)' if newer > item.revision else '')
        repo, name = self.branch_repo(workspace, entry.project, item.branch)
        if not repo:
            return f'{item.branch} · its tree is gone'
        if start is None:
            return item.branch
        count = _git(pathlib.Path(repo), 'rev-list', '--count', '--no-merges', f'{start}..{name}')
        if count is None:
            return f'{item.branch} · not a branch there'
        return f'{item.branch} · {count} commit{"s" if count != "1" else ""} beyond the base'

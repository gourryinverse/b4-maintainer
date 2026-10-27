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
from typing import Any, Callable, Mapping, Optional, Sequence

from .model import Series
from .sessions.records import SessionRecord, session_trees
from .sessions.trees import classify_tree
from .workspace import Workspace

DIRECTORY = 'workbenches'  # Under state/.
_TOP = frozenset({'label', 'entry'})
_ENTRY = frozenset({'project', 'base', 'items'})
_ITEM = frozenset({'series', 'revision', 'subject', 'branch'})


class WorkbenchError(ValueError):
    pass


@dataclass(frozen=True)
class Item:
    series: str = ''  # A posted series' identity, applied by b4 at *revision*.
    revision: int = 0
    subject: str = ''  # As shown.
    branch: str = ''  # Else a local branch: a project ref, or tree:<session>:<branch>.

    @property
    def label(self) -> str:
        return f'{self.subject} v{self.revision}' if self.series else self.branch


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
    series, branch = str(raw.get('series', '')), str(raw.get('branch', ''))
    if bool(series) == bool(branch):
        raise WorkbenchError(f'an item of {project} is a series or a branch, not both or none')
    if series:
        return Item(series, int(raw.get('revision', 0)), str(raw.get('subject', '')))
    return Item(branch=branch)


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
    return Workbench(wid, str(raw.get('label', wid)), tuple(entries))


def _s(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)  # A TOML basic string.


def workbench_toml(bench: Workbench) -> str:
    lines = [f'label = {_s(bench.label)}']
    for entry in bench.entries:
        lines += ['', '[[entry]]', f'project = {_s(entry.project)}', f'base = {_s(entry.base)}']
        lines.append('items = [')
        for item in entry.items:
            if item.series:
                fields = (
                    f'series = {_s(item.series)}, revision = {item.revision}, '
                    f'subject = {_s(item.subject)}'
                )
            else:
                fields = f'branch = {_s(item.branch)}'
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
    bench: Workbench, project: str, series: str, revision: int, subject: str, base: str = ''
) -> Workbench:
    """The series last under *project* (an entry made at *base* if it has none)."""
    entry = _entry(bench, project, base)
    if any(i.series == series for i in entry.items):
        raise WorkbenchError(f'{subject} is already on this workbench')
    item = Item(series, revision, subject)
    return _with_entry(bench, replace(entry, items=entry.items + (item,)))


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


def _git(repo: pathlib.Path, *args: str) -> Optional[str]:
    run = subprocess.run(
        ['git', *args], cwd=repo, capture_output=True, text=True, stdin=subprocess.DEVNULL
    )
    return run.stdout.strip() if run.returncode == 0 else None


class WorkbenchService:
    """Workbenches for the screen: the store, and per project what may go in a tree."""

    def __init__(
        self,
        root: pathlib.Path,
        store: WorkbenchStore,
        records: Callable[[], Sequence[SessionRecord]],
    ) -> None:
        self.root = root
        self.store = store
        self._records = records

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

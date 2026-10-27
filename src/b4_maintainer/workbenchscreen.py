# SPDX-License-Identifier: GPL-2.0-or-later
"""F3: workbenches, where series and branches of several projects are stitched together
and every session's work is found.

Left, the workbenches and the selected one's sessions; middle, its contents in sections:
Items (each project with its base, and what is applied on it, in order), Mailboxes (each
series), Trees (each session tree and its commits by class), Artifacts (each session's
prompt, reports, results, files); right, the selected row's text (for Items, what a
launch would prepare). F2/F3/F4 switch tabs, keeping each tab's place.

`n` makes a workbench, `r` renames it, `x` removes what is selected (a workbench or a
project after a confirmation, an item at once); on Items, `b` picks a project's base,
`B` adds a local branch, shift+up/down move an item, `v` takes a series' newer revision;
F1 launches a session; Enter on a session, or `o` anywhere, puts its live session beside
the app. `w` in the series list or a thread adds series (see the app).
"""

from dataclasses import replace
from typing import Callable, Mapping, Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, OptionList, Static
from textual.widgets.option_list import Option

from .branchscreen import Ask, Pick
from .model import Series
from .widgets import TabBar
from .workbench import (
    ContentRow,
    Workbench,
    WorkbenchError,
    WorkbenchService,
    add_branch,
    move_item,
    remove_item,
    reroll,
    rerolls,
    set_base,
)
from .workspace import Workspace

TYPE_BASE = '\0type'  # The base picker's "type one" entry.
UNFILED = '\0unfiled'  # Sessions from before workbenches: listed, not editable.
TITLES = {
    'bench-list': 'Workbenches',
    'bench-sessions': 'Sessions',
    'bench-contents': 'Contents',
    'bench-show-pane': 'Launch preview',
}
ITEMS = ('heading', 'entry', 'item')  # Rows whose text on the right is the launch preview.


def _count(n: int, one: str) -> str:
    return f'{n} {one}' + ('' if n == 1 else 's')


class WorkbenchScreen(Screen[None]):
    DEFAULT_CSS = """
    WorkbenchScreen #bench-title { height: 1; padding: 0 1; text-style: bold; }
    WorkbenchScreen #bench-panes { height: 1fr; }
    WorkbenchScreen #bench-left { width: 3fr; }
    WorkbenchScreen #bench-list { height: 1fr; }
    WorkbenchScreen #bench-sessions { height: 1fr; }
    WorkbenchScreen #bench-contents { width: 4fr; }
    WorkbenchScreen #bench-show-pane { width: 5fr; }
    /* Each pane titled on its top edge. */
    WorkbenchScreen #bench-list, WorkbenchScreen #bench-sessions,
    WorkbenchScreen #bench-contents, WorkbenchScreen #bench-show-pane {
        border: none; border-top: solid $panel-lighten-2; border-title-color: $text-muted;
    }
    WorkbenchScreen #bench-contents, WorkbenchScreen #bench-show-pane {
        border-left: solid $panel-lighten-2;
    }
    WorkbenchScreen OptionList:focus, WorkbenchScreen VerticalScroll:focus {
        border-title-color: $accent;
    }
    WorkbenchScreen #bench-status { height: 1; padding: 0 1; }
    """
    BINDINGS = [
        Binding('escape', 'back', 'back'),
        # Less common keys stay off the footer; ? lists them all.
        Binding('tab', 'cycle(1)', 'next pane', priority=True, show=False),
        Binding('shift+tab', 'cycle(-1)', 'previous pane', priority=True, show=False),
        Binding('n', 'new', 'new workbench'),
        Binding('r', 'rename', 'rename', show=False),
        Binding('x', 'remove', 'remove'),
        Binding('b', 'base', 'base'),
        Binding('B', 'add_branch', 'add branch'),
        Binding('shift+up', 'move(-1)', 'move up', key_display='⇧↑', show=False),
        Binding('shift+down', 'move(1)', 'move down', key_display='⇧↓', show=False),
        Binding('v', 'reroll', 'newer revision'),
        Binding('f1', 'launch', 'session', key_display='F1'),
        Binding('o', 'open_live', 'open session'),
        # On a session: beside the app (before the list's own Enter).
        Binding('enter', 'open_session', 'open beside', priority=True),
    ]

    def __init__(
        self,
        service: WorkbenchService,
        workspace: Workspace,
        series: Mapping[str, Series],
        latest: Mapping[str, int],
    ) -> None:
        super().__init__()
        self.service = service
        self.workspace = workspace
        self.series = series  # Cached series by identity.
        self.latest = latest  # Each series' newest revision known.
        self.benches: list[Workbench] = []
        self._problems: list[str] = []
        self._rows: list[ContentRow] = []  # The selected workbench's contents.
        self._preview = ''  # What a launch of the selected workbench would prepare.
        self._shown = ''  # The text on the right.
        self._status_text = ''
        self.stale = False  # Set when the app switches back to this tab.
        self._sessions: list[tuple[str, str, bool]] = []  # Its sessions: (id, label, live).
        self._pending: tuple[str, str] = ('', '')  # (workbench, session) to select.
        self._unfiled = False  # Sessions without a workbench exist: list them apart.
        self._keep: tuple[str, str, int] = ('', '', -1)  # The row to keep after a reload.

    def compose(self) -> ComposeResult:
        yield TabBar('workbench')
        yield Static('Workbenches', id='bench-title')
        with Horizontal(id='bench-panes'):
            with Vertical(id='bench-left'):
                yield OptionList(id='bench-list')
                yield OptionList(id='bench-sessions')
            yield OptionList(id='bench-contents')
            with VerticalScroll(id='bench-show-pane'):
                yield Static('', id='bench-show', markup=False)
        yield Static('', id='bench-status', markup=False)
        yield Footer()

    def on_mount(self) -> None:
        for pane, title in TITLES.items():
            self.query_one(f'#{pane}').border_title = title
        self.query_one('#bench-list', OptionList).focus()
        self._reload()

    def on_screen_resume(self) -> None:
        # Switched back to (the app marks it stale), not just a prompt closing over it:
        # reread, keeping the selection.
        if self.stale:
            self.stale = False
            self._reload()

    # The workbenches.

    def _reload(self, select: str = '') -> None:
        self.benches, self._problems = self.service.store.list()
        self._fill_benches(select or self._pending[0] or self._bench_id())
        self._status('; '.join(self._problems) or ('' if self.benches else 'n makes a workbench'))
        rows = getattr(self.app, 'workbench_sessions', None)
        if rows is None:
            return

        def work() -> None:
            try:
                unfiled = bool(rows(''))
            except Exception:
                return
            self.app.call_from_thread(self._show_unfiled, unfiled)

        self.run_worker(work, thread=True, group='bench-unfiled', exclusive=True)

    def _show_unfiled(self, unfiled: bool) -> None:
        if unfiled != self._unfiled:
            self._unfiled = unfiled
            self._fill_benches(self._pending[0] or self._bench_id())

    def _bench_id(self) -> str:
        option = self.query_one('#bench-list', OptionList).highlighted_option
        return option.id or '' if option is not None else ''

    def bench(self) -> Optional[Workbench]:
        wid = self._bench_id()
        if wid == UNFILED:
            return Workbench(UNFILED, 'Unfiled sessions')
        return next((b for b in self.benches if b.id == wid), None)

    def _editable(self) -> bool:
        bench = self.bench()
        return bench is not None and bench.id != UNFILED

    def _fill_benches(self, select: str) -> None:
        lists = self.query_one('#bench-list', OptionList)
        lists.clear_options()
        for b in self.benches:
            lists.add_option(Option(f'{b.label} · {_count(len(b.entries), "project")}', id=b.id))
        if self._unfiled:
            lists.add_option(Option('Unfiled sessions', id=UNFILED))
        ids = [b.id for b in self.benches] + ([UNFILED] if self._unfiled else [])
        if ids:
            with self.prevent(OptionList.OptionHighlighted):
                lists.highlighted = ids.index(select) if select in ids else 0
        self._load_contents()
        self.refresh_sessions()

    # The contents.

    def _load_contents(self) -> None:
        bench = self.bench()
        if bench is None or bench.id == UNFILED:
            self._fill_contents('', [])
            note = (
                "Sessions from before workbenches. Each new session is filed in its series' "
                'workbench (F1 makes it the first time).'
            )
            self._set_preview(note if bench is not None else '')
            return
        workspace, series, latest = self.workspace, self.series, self.latest

        def work() -> None:
            try:
                rows = self.service.contents(workspace, bench, series, latest)
                text = self.service.preview(workspace, bench, series, latest)
            except Exception as exc:  # Shown; the lists still work.
                self.app.call_from_thread(self._status, f'Cannot read the workbench: {exc}')
                return
            self.app.call_from_thread(self._contents_loaded, bench.id, rows, text)

        self.run_worker(work, thread=True, group='bench-contents', exclusive=True)

    def _contents_loaded(self, wid: str, rows: list[ContentRow], text: str) -> None:
        self._fill_contents(wid, rows)
        self._set_preview(text)

    def _fill_contents(self, wid: str, rows: list[ContentRow]) -> None:
        bench = self.bench()
        if wid != (bench.id if bench is not None and bench.id != UNFILED else ''):
            return  # Another workbench was selected meanwhile.
        keep = self._keep if self._keep[0] else self._row_key(self.row())
        self._keep = ('', '', -1)
        self._rows = rows
        lists = self.query_one('#bench-contents', OptionList)
        with self.prevent(OptionList.OptionHighlighted):
            lists.clear_options()
            for row in rows:
                lists.add_option(Option(row.text, disabled=row.kind == 'heading'))
            keys = [self._row_key(r) for r in rows]
            if keep in keys:
                lists.highlighted = keys.index(keep)
            elif rows:
                first = next((n for n, r in enumerate(rows) if r.kind != 'heading'), None)
                lists.highlighted = first
        if self._pane() == 'bench-contents':
            self._show_row()
        self.refresh_bindings()

    @staticmethod
    def _row_key(row: Optional[ContentRow]) -> tuple[str, str, int]:
        if row is None:
            return ('', '', -1)
        return (row.kind, row.project or row.session, row.index)

    def row(self) -> Optional[ContentRow]:
        index = self.query_one('#bench-contents', OptionList).highlighted
        return self._rows[index] if index is not None and index < len(self._rows) else None

    def _show_row(self) -> None:
        bench, row = self.bench(), self.row()
        if bench is None or row is None or row.kind in ITEMS:
            self._show_text(self._preview, 'Launch preview')
            return
        workspace, series, latest = self.workspace, self.series, self.latest

        def work() -> None:
            try:
                text = self.service.show(workspace, bench, row, series, latest)
            except Exception as exc:
                text = f'Cannot show it: {exc}'
            self.app.call_from_thread(self._show_text, text, row.text.strip())

        self.run_worker(work, thread=True, group='bench-show', exclusive=True)

    def _set_preview(self, text: str) -> None:
        """The launch preview: shown unless the right pane follows a session or a
        contents row with its own text."""
        self._preview = text
        row, pane = self.row(), self._pane()
        own = pane == 'bench-contents' and row is not None and row.kind not in ITEMS
        if pane != 'bench-sessions' and not own:
            self._show_text(text, 'Launch preview')

    def _show_text(self, text: str, title: str) -> None:
        self._shown = text
        self.query_one('#bench-show', Static).update(text)
        self.query_one('#bench-show-pane').border_title = title[:60]

    def _status(self, text: str) -> None:
        self._status_text = text
        self.query_one('#bench-status', Static).update(text)

    # The workbench's sessions.

    def refresh_sessions(self) -> None:
        """Reread the selected workbench's sessions (off the UI thread)."""
        bench = self.bench()
        rows = getattr(self.app, 'workbench_sessions', None)
        if bench is None or rows is None:
            self._fill_sessions('', [])
            return
        wid = bench.id

        def work() -> None:
            try:
                found = rows('' if wid == UNFILED else wid)
            except Exception as exc:  # Shown; the rest of the tab still works.
                self.app.call_from_thread(self._status, f'Cannot list its sessions: {exc}')
                return
            self.app.call_from_thread(self._fill_sessions, wid, found)

        self.run_worker(work, thread=True, group='bench-sessions', exclusive=True)

    def _fill_sessions(self, wid: str, rows: list[tuple[str, str, bool]]) -> None:
        bench = self.bench()
        if wid != (bench.id if bench is not None else ''):
            return
        lists = self.query_one('#bench-sessions', OptionList)
        keep = self._pending[1] if self._pending[0] == wid else self.selected_session()
        self._sessions = rows
        ids = [sid for sid, _label, _live in rows]
        with self.prevent(OptionList.OptionHighlighted):
            lists.clear_options()
            for sid, label, _live in rows:
                lists.add_option(Option(label, id=sid))
            if ids:
                lists.highlighted = ids.index(keep) if keep in ids else 0
        if self._pending[0] == wid and self._pending[1] in ids:
            self._pending = ('', '')
            lists.focus()
            self._show_session()
        self.refresh_bindings()

    def selected_session(self) -> str:
        option = self.query_one('#bench-sessions', OptionList).highlighted_option
        return option.id or '' if option is not None else ''

    def _show_session(self) -> None:
        """A session's own text on the right: its initial prompt, and what it produced."""
        sid = self.selected_session()
        output = getattr(self.app, 'session_output', None)
        if not sid or output is None:
            return

        def work() -> None:
            from .attachments import preview

            try:
                found = output(sid)
            except Exception as exc:
                self.app.call_from_thread(self._show_text, f'Cannot read it: {exc}', sid)
                return
            prompt = next((a for a in found if a.kind == 'prompt'), None)
            listed = [a.label.split('  (')[0] for a in found if a.kind != 'prompt']
            text = preview(prompt) if prompt is not None else ''
            if listed:
                produced = '\n'.join(f'  {label}' for label in listed)
                text += f'\n\nProduced (Trees and Artifacts, in Contents):\n{produced}'
            self.app.call_from_thread(self._show_text, text or 'Nothing from it yet.', sid)

        self.run_worker(work, thread=True, group='bench-show', exclusive=True)

    def select(self, wid: str, sid: str) -> None:
        """Show workbench *wid* with its session *sid* selected (from the drawer)."""
        self._pending = (wid, sid)
        if self.is_mounted:
            self._reload(wid)

    async def focus_sessions(self) -> None:
        self.query_one('#bench-sessions', OptionList).focus()
        self._show_session()

    def _live_session(self) -> str:
        """The session `o` opens: the selected one in the sessions pane, else the newest
        live one of the workbench."""
        if self._pane() == 'bench-sessions' and self.selected_session():
            return self.selected_session()
        return next((sid for sid, _label, live in self._sessions if live), '')

    def action_open_live(self) -> None:
        sid = self._live_session()
        opened = getattr(self.app, 'open_bench_session', None)
        if sid and opened is not None:
            opened(sid)

    def action_open_session(self) -> None:
        sid = self.selected_session()
        opened = getattr(self.app, 'open_bench_session', None)
        if sid and opened is not None:
            opened(sid)

    # Text, for tests.

    def bench_labels(self) -> list[str]:
        lists = self.query_one('#bench-list', OptionList)
        return [str(lists.get_option_at_index(n).prompt) for n in range(lists.option_count)]

    def content_labels(self) -> list[str]:
        return [r.text.strip() for r in self._rows]

    def entry_labels(self) -> list[str]:
        return [r.text.strip() for r in self._rows if r.kind == 'entry']

    def item_labels(self, project: str = '') -> list[str]:
        """*project*'s items as listed, without their numbers (else the selected row's
        project, else the first)."""
        row = self.row()
        first = next((r.project for r in self._rows if r.kind == 'entry'), '')
        project = project or (row.project if row is not None else '') or first
        return [
            r.text.strip().split('. ', 1)[-1]
            for r in self._rows
            if r.kind == 'item' and r.project == project
        ]

    def session_labels(self) -> list[str]:
        return [label for _sid, label, _live in self._sessions]

    def output_labels(self) -> list[str]:
        """The selected session's artifacts and commits, as Contents lists them."""
        sid = self.selected_session()
        return [r.text.strip() for r in self._rows if r.session == sid and r.attachment]

    def preview_text(self) -> str:
        return self._preview

    def shown_text(self) -> str:
        return self._shown

    def status_text(self) -> str:
        return self._status_text

    def select_row(self, kind: str, project: str = '', index: int = -1) -> None:
        """Focus Contents on its first row of *kind* (in *project*, at *index*)."""
        lists = self.query_one('#bench-contents', OptionList)
        for n, row in enumerate(self._rows):
            if row.kind != kind or (project and row.project != project):
                continue
            if index < 0 or row.index == index:
                lists.focus()
                lists.highlighted = n
                return

    # Panes.

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        event.stop()  # The app's own lists handle theirs.
        if event.option_list.id == 'bench-list':
            self._load_contents()
            self.refresh_sessions()
        elif event.option_list.id == 'bench-contents':
            self._show_row()
        elif event.option_list.id == 'bench-sessions':
            self._show_session()
        self.refresh_bindings()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()

    def action_cycle(self, step: int) -> None:
        names = ('bench-list', 'bench-sessions', 'bench-contents', 'bench-show-pane')
        panes = [self.query_one(f'#{p}') for p in names]
        index = next((n for n, p in enumerate(panes) if p is self.focused), 0)
        panes[(index + step) % len(panes)].focus()
        if self._pane() == 'bench-sessions':
            self._show_session()
        elif self._pane() == 'bench-contents':
            self._show_row()
        elif self._pane() == 'bench-list':
            self._show_text(self._preview, 'Launch preview')
        self.refresh_bindings()

    def action_back(self) -> None:
        tab = getattr(self.app, 'action_tab', None)  # Back to LKML, the column with it.
        if tab is not None:
            tab('lkml')
        else:
            self.app.pop_screen()

    def _pane(self) -> str:
        return self.focused.id or '' if self.focused is not None else ''

    def _item(self) -> Optional[ContentRow]:
        row = self.row()
        if self._pane() == 'bench-contents' and row is not None and row.kind == 'item':
            return row
        return None

    def _project(self) -> str:
        row = self.row()
        if self._pane() == 'bench-contents' and row is not None and row.kind in ('entry', 'item'):
            return row.project
        return ''

    def check_action(self, action: str, parameters: tuple[object, ...]) -> Optional[bool]:
        pane, bench = self._pane(), self.bench()
        edits = ('rename', 'remove', 'base', 'add_branch', 'move', 'reroll', 'launch')
        if action in edits and not self._editable():
            return False
        if action == 'rename':
            return pane == 'bench-list' and bench is not None
        if action == 'remove':
            if pane == 'bench-contents':
                return bool(self._project())
            return pane == 'bench-list' and bench is not None
        if action in ('base', 'add_branch'):
            return bool(self._project())
        if action == 'move':
            return self._item() is not None
        if action == 'reroll':
            return self._newer() is not None
        if action == 'launch':
            launchable = hasattr(self.app, 'launch_workbench')
            return bench is not None and bool(bench.entries) and launchable
        if action == 'open_session':
            return pane == 'bench-sessions' and bool(self.selected_session())
        if action == 'open_live':
            return bool(self._live_session())
        return True

    def _newer(self) -> Optional[int]:
        bench, item = self.bench(), self._item()
        if bench is None or item is None:
            return None
        found = {(p, n): r for p, n, r in rerolls(bench, self.latest)}
        return found.get((item.project, item.index))

    # Edits.

    def _save(
        self, bench: Workbench, keep: Optional[tuple[str, str, int]] = None, note: str = ''
    ) -> None:
        try:
            self.service.store.save(bench)
        except OSError as ex:
            self._status(f'Cannot save {bench.label}: {ex}')
            return
        self.benches = [bench if b.id == bench.id else b for b in self.benches]
        if keep is not None:
            self._keep = keep
        self._fill_benches(bench.id)
        self._status(note)

    def action_new(self) -> None:
        def named(label: Optional[str]) -> None:
            if not label:
                return
            bench = Workbench(self.service.store.new_id(label), label)
            self.service.store.save(bench)
            self._reload(bench.id)
            self._status(f'Made {label}: w adds series from the series list or a thread')

        self.app.push_screen(Ask('Name the new workbench'), named)

    def action_rename(self) -> None:
        bench = self.bench()
        if bench is None:
            return

        def named(label: Optional[str]) -> None:
            if label:
                self._save(replace(bench, label=label), note=f'Renamed to {label}')

        self.app.push_screen(Ask('Rename the workbench', bench.label), named)

    def action_remove(self) -> None:
        from .app import Confirm

        bench = self.bench()
        if bench is None:
            return
        item = self._item()
        if item is not None:
            keep = ('item', item.project, max(item.index - 1, 0))
            self._save(remove_item(bench, item.project, item.index), keep)
            return
        project = self._project()
        if project:

            def drop_entry(yes: Optional[bool]) -> None:
                if yes:
                    entries = tuple(e for e in bench.entries if e.project != project)
                    self._save(replace(bench, entries=entries), note=f'Removed {project}')

            entry = bench.entry(project)
            what = _count(len(entry.items) if entry is not None else 0, 'item')
            self.app.push_screen(
                Confirm(f'Remove {project} ({what}) from {bench.label}? [y|N]'), drop_entry
            )
            return

        def drop_bench(yes: Optional[bool]) -> None:
            if yes:
                self.service.store.delete(bench.id)
                self._reload()
                self._status(f'Removed {bench.label}')

        self.app.push_screen(Confirm(f'Remove the workbench {bench.label}? [y|N]'), drop_bench)

    def action_base(self) -> None:
        bench, project = self.bench(), self._project()
        if bench is None or not project:
            return
        keep = ('entry', project, -1)
        self._choose(
            lambda: self.service.base_choices(self.workspace, project),
            f'Base for {project}',
            lambda base: self._save(set_base(bench, project, base), keep, f'{project} on {base}'),
            typed=True,
        )

    def action_add_branch(self) -> None:
        bench, project = self.bench(), self._project()
        if bench is None or not project:
            return

        def add(branch: str) -> None:
            try:
                changed = add_branch(bench, project, branch)
            except WorkbenchError as ex:
                self._status(str(ex))
                return
            entry = changed.entry(project)
            keep = ('item', project, len(entry.items) - 1 if entry else -1)
            self._save(changed, keep, f'Added {branch}')

        self._choose(
            lambda: self.service.branch_choices(self.workspace, project),
            f'Add a local branch to {project}',
            add,
        )

    def _choose(
        self,
        choices: Callable[[], list[tuple[str, str]]],
        title: str,
        chosen: Callable[[str], None],
        typed: bool = False,
    ) -> None:
        """Pick from *choices* (read off the UI thread); *typed*: or type one."""

        def work() -> None:
            try:
                found = choices()
            except Exception as exc:
                self.app.call_from_thread(self._status, f'Cannot list them: {exc}')
                return
            self.app.call_from_thread(show, found)

        def show(found: list[tuple[str, str]]) -> None:
            options = found + ([(TYPE_BASE, 'Type one…')] if typed else [])
            if not options:
                self._status('Nothing to choose from')
                return

            def picked(value: Optional[str]) -> None:
                if value == TYPE_BASE:
                    self.app.push_screen(Ask(title), lambda text: text and chosen(text))
                elif value:
                    chosen(value)

            self.app.push_screen(Pick(title, options), picked)

        self.run_worker(work, thread=True, group='bench-choices')

    def action_move(self, step: int) -> None:
        bench, item = self.bench(), self._item()
        if bench is None or item is None:
            return
        changed = move_item(bench, item.project, item.index, step)
        if changed != bench:
            self._save(changed, ('item', item.project, item.index + step))

    def action_launch(self) -> None:
        """F1: a session over the selected workbench (the app's launch dialog)."""
        bench = self.bench()
        launch = getattr(self.app, 'launch_workbench', None)
        if bench is not None and launch is not None:
            launch(bench)

    def action_reroll(self) -> None:
        bench, item, newer = self.bench(), self._item(), self._newer()
        if bench is None or item is None or newer is None:
            return
        keep = ('item', item.project, item.index)
        self._save(reroll(bench, item.project, item.index, newer), keep, f'Now at v{newer}')

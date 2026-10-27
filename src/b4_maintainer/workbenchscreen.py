# SPDX-License-Identifier: GPL-2.0-or-later
"""F4: workbenches, the recipes for sessions over several projects.

Three panes: the workbenches; the selected one's projects (each with the base its tree
starts from); that project's items in order (posted series, local branches). Below, what
launching it would prepare. `n` makes a workbench, `r` renames it, `x` removes what the
focused pane shows (a workbench or project after a confirmation, an item at once); `b`
picks a project's base; `B` adds a local branch; shift+up/down move an item; `v` takes a
series' newer revision; F1 launches a session over its trees. `w` in the series list or
a thread adds series (see the app).
"""

from typing import Callable, Mapping, Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, OptionList, Static
from textual.widgets.option_list import Option

from .branchscreen import Ask, Pick
from .model import Series
from .workbench import (
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


def _count(n: int, one: str) -> str:
    return f'{n} {one}' + ('' if n == 1 else 's')


class WorkbenchScreen(Screen[None]):
    DEFAULT_CSS = """
    WorkbenchScreen #bench-title { height: 1; padding: 0 1; text-style: bold; }
    WorkbenchScreen #bench-panes { height: 2fr; }
    WorkbenchScreen #bench-list { width: 2fr; border: none; }
    WorkbenchScreen #bench-entries { width: 2fr; border: none; border-left: solid $panel-lighten-2; }
    WorkbenchScreen #bench-items { width: 3fr; border: none; border-left: solid $panel-lighten-2; }
    WorkbenchScreen #bench-preview-pane { height: 1fr; border-top: solid $panel-lighten-2; }
    WorkbenchScreen #bench-status { height: 1; padding: 0 1; }
    """
    BINDINGS = [
        Binding('escape', 'back', 'back'),
        Binding('f4', 'back', 'series', key_display='F4'),
        Binding('tab', 'cycle(1)', 'next pane', priority=True),
        Binding('shift+tab', 'cycle(-1)', 'previous pane', priority=True, show=False),
        Binding('n', 'new', 'new workbench'),
        Binding('r', 'rename', 'rename'),
        Binding('x', 'remove', 'remove'),
        Binding('b', 'base', 'base'),
        Binding('B', 'add_branch', 'add branch'),
        Binding('shift+up', 'move(-1)', 'move up', key_display='⇧↑'),
        Binding('shift+down', 'move(1)', 'move down', key_display='⇧↓'),
        Binding('v', 'reroll', 'newer revision'),
        Binding('f1', 'launch', 'session', key_display='F1'),
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
        self.series = series  # Cached series by identity, for the preview.
        self.latest = latest  # Each series' newest revision known.
        self.benches: list[Workbench] = []
        self._problems: list[str] = []
        self._preview = ''
        self._status_text = ''

    def compose(self) -> ComposeResult:
        yield Static('Workbenches', id='bench-title')
        with Horizontal(id='bench-panes'):
            yield OptionList(id='bench-list')
            yield OptionList(id='bench-entries')
            yield OptionList(id='bench-items')
        with VerticalScroll(id='bench-preview-pane'):
            yield Static('', id='bench-preview', markup=False)
        yield Static('', id='bench-status', markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one('#bench-list', OptionList).focus()
        self._reload()

    # Loading and showing.

    def _reload(self, select: str = '') -> None:
        self.benches, self._problems = self.service.store.list()
        self._fill_benches(select or self._bench_id())
        self._status('; '.join(self._problems) or ('' if self.benches else 'n makes a workbench'))

    def _bench_id(self) -> str:
        option = self.query_one('#bench-list', OptionList).highlighted_option
        return option.id or '' if option is not None else ''

    def bench(self) -> Optional[Workbench]:
        wid = self._bench_id()
        return next((b for b in self.benches if b.id == wid), None)

    def project(self) -> str:
        option = self.query_one('#bench-entries', OptionList).highlighted_option
        return option.id or '' if option is not None else ''

    def item_index(self) -> Optional[int]:
        return self.query_one('#bench-items', OptionList).highlighted

    def _fill_benches(self, select: str) -> None:
        lists = self.query_one('#bench-list', OptionList)
        lists.clear_options()
        for b in self.benches:
            lists.add_option(Option(f'{b.label} · {_count(len(b.entries), "project")}', id=b.id))
        ids = [b.id for b in self.benches]
        if ids:
            with self.prevent(OptionList.OptionHighlighted):
                lists.highlighted = ids.index(select) if select in ids else 0
        self._fill_entries()

    def _fill_entries(self, select: str = '') -> None:
        entries = self.query_one('#bench-entries', OptionList)
        keep = select or self.project()
        entries.clear_options()
        bench = self.bench()
        projects = [e.project for e in bench.entries] if bench else []
        for entry in bench.entries if bench else ():
            where = entry.base or 'the base b4 chooses'
            entries.add_option(Option(f'{entry.project} on {where}', id=entry.project))
        if projects:
            with self.prevent(OptionList.OptionHighlighted):
                entries.highlighted = projects.index(keep) if keep in projects else 0
        self._fill_items()
        self._show_preview()

    def _fill_items(self, select: Optional[int] = None) -> None:
        items = self.query_one('#bench-items', OptionList)
        keep = self.item_index() if select is None else select
        items.clear_options()
        bench, project = self.bench(), self.project()
        entry = bench.entry(project) if bench else None
        newer = {(p, n): r for p, n, r in rerolls(bench, self.latest)} if bench else {}
        for n, item in enumerate(entry.items if entry else ()):
            mark = f' · v{newer[(project, n)]} available' if (project, n) in newer else ''
            items.add_option(Option(f'{item.label}{mark}'))
        if items.option_count:
            with self.prevent(OptionList.OptionHighlighted):
                items.highlighted = min(keep or 0, items.option_count - 1)
        self.refresh_bindings()

    def _show_preview(self) -> None:
        bench = self.bench()
        if bench is None:
            self._set_preview('')
            return
        series, latest, workspace = self.series, self.latest, self.workspace

        def work() -> None:
            try:
                text = self.service.preview(workspace, bench, series, latest)
            except Exception as exc:  # Shown; the lists still work.
                text = f'Cannot preview: {exc}'
            self.app.call_from_thread(self._set_preview, text)

        self.run_worker(work, thread=True, group='bench-preview', exclusive=True)

    def _set_preview(self, text: str) -> None:
        self._preview = text
        self.query_one('#bench-preview', Static).update(text)

    def _status(self, text: str) -> None:
        self._status_text = text
        self.query_one('#bench-status', Static).update(text)

    # Text, for tests.

    def bench_labels(self) -> list[str]:
        lists = self.query_one('#bench-list', OptionList)
        return [str(lists.get_option_at_index(n).prompt) for n in range(lists.option_count)]

    def entry_labels(self) -> list[str]:
        lists = self.query_one('#bench-entries', OptionList)
        return [str(lists.get_option_at_index(n).prompt) for n in range(lists.option_count)]

    def item_labels(self) -> list[str]:
        lists = self.query_one('#bench-items', OptionList)
        return [str(lists.get_option_at_index(n).prompt) for n in range(lists.option_count)]

    def preview_text(self) -> str:
        return self._preview

    def status_text(self) -> str:
        return self._status_text

    # Panes.

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        event.stop()  # The app's own lists handle theirs.
        if event.option_list.id == 'bench-list':
            self._fill_entries()
        elif event.option_list.id == 'bench-entries':
            self._fill_items(0)
        else:
            self.refresh_bindings()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()

    def action_cycle(self, step: int) -> None:
        panes = [self.query_one(f'#{p}') for p in ('bench-list', 'bench-entries', 'bench-items')]
        index = next((n for n, p in enumerate(panes) if p is self.focused), 0)
        panes[(index + step) % len(panes)].focus()
        self.refresh_bindings()

    def action_back(self) -> None:
        self.app.pop_screen()

    def _pane(self) -> str:
        return self.focused.id or '' if self.focused is not None else ''

    def check_action(self, action: str, parameters: tuple[object, ...]) -> Optional[bool]:
        pane, bench = self._pane(), self.bench()
        if action == 'rename':
            return pane == 'bench-list' and bench is not None
        if action == 'remove':
            if pane == 'bench-items':
                return self.item_index() is not None and bool(self.item_labels())
            if pane == 'bench-entries':
                return bool(self.project())
            return bench is not None
        if action == 'base':
            return pane == 'bench-entries' and bool(self.project())
        if action == 'add_branch':
            return pane in ('bench-entries', 'bench-items') and bool(self.project())
        if action == 'move':
            return pane == 'bench-items' and bool(self.item_labels())
        if action == 'reroll':
            return pane == 'bench-items' and self._newer() is not None
        if action == 'launch':
            return (
                bench is not None and bool(bench.entries) and hasattr(self.app, 'launch_workbench')
            )
        return True

    def _newer(self) -> Optional[int]:
        bench, index = self.bench(), self.item_index()
        if bench is None or index is None:
            return None
        found = {(p, n): r for p, n, r in rerolls(bench, self.latest)}
        return found.get((self.project(), index))

    # Edits.

    def _save(self, bench: Workbench, item: Optional[int] = None, note: str = '') -> None:
        try:
            self.service.store.save(bench)
        except OSError as ex:
            self._status(f'Cannot save {bench.label}: {ex}')
            return
        self.benches = [bench if b.id == bench.id else b for b in self.benches]
        self._fill_benches(bench.id)
        if item is not None:
            self._fill_items(item)
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
                self._save(Workbench(bench.id, label, bench.entries), note=f'Renamed to {label}')

        self.app.push_screen(Ask('Rename the workbench', bench.label), named)

    def action_remove(self) -> None:
        from .app import Confirm

        bench, pane = self.bench(), self._pane()
        if bench is None:
            return
        if pane == 'bench-items':
            index = self.item_index()
            if index is not None:
                self._save(remove_item(bench, self.project(), index), index)
            return
        if pane == 'bench-entries':
            project = self.project()

            def drop_entry(yes: Optional[bool]) -> None:
                if yes:
                    entries = tuple(e for e in bench.entries if e.project != project)
                    self._save(Workbench(bench.id, bench.label, entries), note=f'Removed {project}')

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
        bench, project = self.bench(), self.project()
        if bench is None or not project:
            return
        self._choose(
            lambda: self.service.base_choices(self.workspace, project),
            f'Base for {project}',
            lambda base: self._save(set_base(bench, project, base), note=f'{project} on {base}'),
            typed=True,
        )

    def action_add_branch(self) -> None:
        bench, project = self.bench(), self.project()
        if bench is None or not project:
            return

        def add(branch: str) -> None:
            try:
                changed = add_branch(bench, project, branch)
            except WorkbenchError as ex:
                self._status(str(ex))
                return
            entry = changed.entry(project)
            self._save(changed, len(entry.items) - 1 if entry else None, f'Added {branch}')

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
        bench, index = self.bench(), self.item_index()
        if bench is None or index is None:
            return
        changed = move_item(bench, self.project(), index, step)
        if changed != bench:
            self._save(changed, index + step)

    def action_launch(self) -> None:
        """F1: a session over the selected workbench's trees (the app's launch dialog)."""
        bench = self.bench()
        launch = getattr(self.app, 'launch_workbench', None)
        if bench is not None and launch is not None:
            launch(bench)

    def action_reroll(self) -> None:
        bench, index, newer = self.bench(), self.item_index(), self._newer()
        if bench is None or index is None or newer is None:
            return
        self._save(reroll(bench, self.project(), index, newer), index, f'Now at v{newer}')

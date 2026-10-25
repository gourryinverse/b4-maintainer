# SPDX-License-Identifier: GPL-2.0-or-later
"""The Branches screen: each project's maintainer bases, session trees, and prep
branches (left), the selected one's commits and artifacts (middle), and the selected
commit or artifact (right).

Everything is read and acted on in workers; the screen only shows results. `r` rebases a
maintainer base after a confirmation; F5 reloads; Escape returns to the series list.
"""

from typing import Optional, Protocol

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, OptionList, Static
from textual.widgets.option_list import Option

from .adapters.bases import RebaseResult
from .branches import BranchesSnapshot, DetailRow
from .workspace import Workspace


class Branches(Protocol):
    def snapshot(self, workspace: Workspace) -> BranchesSnapshot: ...

    def show(self, row: DetailRow) -> str: ...

    def rebase(self, workspace: Workspace, key: str, expected: str) -> RebaseResult: ...


class BranchesScreen(Screen[None]):
    DEFAULT_CSS = """
    BranchesScreen #branch-title { height: 1; padding: 0 1; text-style: bold; }
    BranchesScreen #branch-panes { height: 1fr; }
    BranchesScreen #branch-items { width: 2fr; border: none; }
    BranchesScreen #branch-rows { width: 3fr; border: none; border-left: solid $panel-lighten-2; }
    BranchesScreen #branch-show-pane { width: 4fr; border-left: solid $panel-lighten-2; }
    BranchesScreen #branch-status { height: 1; padding: 0 1; }
    """
    BINDINGS = [
        Binding('escape', 'back', 'back'),
        Binding('tab', 'cycle(1)', 'next pane', priority=True),
        Binding('shift+tab', 'cycle(-1)', 'previous pane', priority=True, show=False),
        Binding('r', 'rebase', 'rebase base'),
        Binding('f5', 'reload', 'reload', key_display='F5'),
    ]

    def __init__(self, service: Branches, workspace: Workspace) -> None:
        super().__init__()
        self.service = service
        self.workspace = workspace
        self.data = BranchesSnapshot()
        self._rows: tuple[DetailRow, ...] = ()
        self._shown = ''
        self._note = ''  # The last action's outcome, kept across the reload it causes.
        self._status_text = ''

    def compose(self) -> ComposeResult:
        yield Static('Branches', id='branch-title')
        with Horizontal(id='branch-panes'):
            yield OptionList(id='branch-items')
            yield OptionList(id='branch-rows')
            with VerticalScroll(id='branch-show-pane'):
                yield Static('', id='branch-show', markup=False)
        yield Static('Loading…', id='branch-status', markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one('#branch-items', OptionList).focus()
        self.action_reload()

    # Loading

    def action_reload(self) -> None:
        def work() -> None:
            try:
                data = self.service.snapshot(self.workspace)
            except Exception as exc:
                self.app.call_from_thread(self._status, f'Cannot read branches: {exc}')
                return
            self.app.call_from_thread(self._loaded, data)

        self.run_worker(work, thread=True, group='branches-load', exclusive=True)

    def _loaded(self, data: BranchesSnapshot) -> None:
        items = self.query_one('#branch-items', OptionList)
        selected = self.selected_item()
        self.data = data
        items.clear_options()
        project = None
        for item in data.items:
            if item.project != project:
                project = item.project
                items.add_option(Option(project, disabled=True))
            items.add_option(Option(f'  {item.label}', id=item.key))
        keys = [i.key for i in data.items]
        if keys:
            target = selected if selected in keys else keys[0]
            items.highlighted = next(
                n for n in range(items.option_count) if items.get_option_at_index(n).id == target
            )
        self._show_rows()
        empty = '' if keys else 'No maintainer bases, session trees, or prep branches.'
        self._status('; '.join(p for p in (self._note, *data.problems, empty) if p))
        self._note = ''

    def _status(self, text: str) -> None:
        self._status_text = text
        self.query_one('#branch-status', Static).update(text)

    def status_text(self) -> str:
        return self._status_text

    # Panes

    def selected_item(self) -> Optional[str]:
        option = self.query_one('#branch-items', OptionList).highlighted_option
        return option.id if option is not None else None

    def selected_row(self) -> Optional[DetailRow]:
        rows = self.query_one('#branch-rows', OptionList)
        index = rows.highlighted
        if index is None or index >= len(self._rows):
            return None
        return self._rows[index]

    def _show_rows(self) -> None:
        key = self.selected_item()
        self._rows = tuple(self.data.details.get(key, ())) if key else ()
        rows = self.query_one('#branch-rows', OptionList)
        rows.clear_options()
        for row in self._rows:
            rows.add_option(Option(row.text, disabled=not row.key))
        first = next((n for n, r in enumerate(self._rows) if r.key), None)
        rows.highlighted = first
        self._show(self.selected_row())

    def _show(self, row: Optional[DetailRow]) -> None:
        if row is None or not row.key:
            self._set_shown('')
            return

        def work() -> None:
            text = self.service.show(row)
            self.app.call_from_thread(self._set_shown, text)

        self.run_worker(work, thread=True, group='branches-show', exclusive=True)

    def _set_shown(self, text: str) -> None:
        self._shown = text
        self.query_one('#branch-show', Static).update(text)

    def shown_text(self) -> str:
        return self._shown

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        event.stop()  # The app's own lists handle theirs; these are this screen's.
        if event.option_list.id == 'branch-items':
            self._show_rows()
        elif event.option_list.id == 'branch-rows':
            self._show(self.selected_row())

    def action_cycle(self, step: int) -> None:
        panes = [self.query_one('#branch-items'), self.query_one('#branch-rows')]
        panes.append(self.query_one('#branch-show-pane'))
        focused = self.focused
        index = next((n for n, p in enumerate(panes) if p is focused), 0)
        panes[(index + step) % len(panes)].focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()

    def action_back(self) -> None:
        self.app.pop_screen()

    # Actions

    def check_action(self, action: str, parameters: tuple[object, ...]) -> Optional[bool]:
        if action == 'rebase':
            key = self.selected_item()
            return bool(key and key.startswith('base:'))
        return True

    def action_rebase(self) -> None:
        from .app import Confirm

        key = self.selected_item()
        status = self.data.statuses.get(key or '')
        if key is None or status is None:
            return
        base = status.base
        keep = sum(not c.landed for c in status.commits)
        drop = len(status.commits) - keep
        prompt = (
            f'Rebase {base.branch} onto {base.upstream}? {keep} commits replayed'
            + (f', {drop} landed dropped' if drop else '')
            + ' [y|N]'
        )

        def answered(confirmed: Optional[bool]) -> None:
            if confirmed:
                self._rebase(key, status.tip)

        self.app.push_screen(Confirm(prompt), answered)

    def _rebase(self, key: str, expected: str) -> None:
        self._status('Rebasing…')

        def work() -> None:
            try:
                result = self.service.rebase(self.workspace, key, expected)
            except Exception as exc:
                result = RebaseResult(False, error=str(exc))
            self.app.call_from_thread(self._rebased, key, result)

        self.run_worker(work, thread=True, group='branches-act')

    def _rebased(self, key: str, result: RebaseResult) -> None:
        branch = key.split(':', 2)[2]
        if result.ok:
            dropped = f'; dropped: {", ".join(result.dropped)}' if result.dropped else ''
            self._status(f'Rebased {branch}{dropped}')
        elif result.failed is not None:
            self._status(f'{branch} not rebased: {result.failed.subject} does not apply')
        else:
            self._status(f'{branch} not rebased: {result.error}')
        self._note = self.status_text()
        self.action_reload()

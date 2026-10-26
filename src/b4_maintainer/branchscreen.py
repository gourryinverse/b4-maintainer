# SPDX-License-Identifier: GPL-2.0-or-later
"""The Branches screen: each project's maintainer bases, worktrees, session trees, prep
branches, review branches, and other local work (left), the selected one's commits and
artifacts (middle), and the selected commit or artifact (right).

Everything is read and acted on in workers; the screen only shows results. `r` rebases a
maintainer base after a confirmation; when that stops at a conflict, `a` launches a
rebase session on a copy (after a confirmation), and `A` on that session's tree moves the
base to its result. F5 reloads; F3 or Escape returns to the series list.
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
from .sessions.records import SessionRecord
from .workspace import Workspace


class Branches(Protocol):
    def snapshot(self, workspace: Workspace) -> BranchesSnapshot: ...

    def show(self, row: DetailRow) -> str: ...

    def rebase(self, workspace: Workspace, key: str, expected: str) -> RebaseResult: ...

    def rebase_session(
        self, workspace: Workspace, key: str, failed: RebaseResult
    ) -> SessionRecord: ...

    def accept(self, workspace: Workspace, sid: str) -> RebaseResult: ...


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
        Binding('f3', 'back', 'series', key_display='F3'),
        Binding('tab', 'cycle(1)', 'next pane', priority=True),
        Binding('shift+tab', 'cycle(-1)', 'previous pane', priority=True, show=False),
        Binding('r', 'rebase', 'rebase base'),
        Binding('a', 'agent_rebase', 'rebase with an agent'),
        Binding('A', 'accept', 'accept rebase'),
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
        self._failed: dict[str, RebaseResult] = {}  # Base key -> its last failed rebase.

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
        empty = '' if keys else 'No projects with a checkout.'
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
        key = self.selected_item() or ''
        if action == 'rebase':
            return key.startswith('base:')
        if action == 'agent_rebase':
            return key in self._failed
        if action == 'accept':
            return key in self.data.rebases
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
        self._failed.pop(key, None)
        if result.ok:
            dropped = f'; dropped: {", ".join(result.dropped)}' if result.dropped else ''
            self._status(f'Rebased {branch}{dropped}')
        elif result.failed is not None:
            self._failed[key] = result
            self._status(
                f'{branch} not rebased: {result.failed.subject} does not apply; '
                'a rebases it with an agent'
            )
        else:
            self._status(f'{branch} not rebased: {result.error}')
        self._note = self.status_text()
        self.action_reload()

    def action_agent_rebase(self) -> None:
        """a, after a failed rebase: a rebase session on a copy of the base (approved
        here: nothing starts without this confirmation)."""
        from .app import Confirm

        key = self.selected_item()
        failed = self._failed.get(key or '')
        if key is None or failed is None:
            return
        status = self.data.statuses.get(key)
        where = f'{status.base.branch} onto {status.base.upstream}' if status else key
        prompt = (
            f"Launch a rebase session for {where} (pi's default model)? It rebases a copy; "
            'your branch changes only when you accept its result (A). [y|N]'
        )

        def answered(confirmed: Optional[bool]) -> None:
            if confirmed:
                self._launch_rebase(key, failed)

        self.app.push_screen(Confirm(prompt), answered)

    def _launch_rebase(self, key: str, failed: RebaseResult) -> None:
        self._status('Preparing the rebase session…')

        def work() -> None:
            try:
                record = self.service.rebase_session(self.workspace, key, failed)
                note = f'Launched rebase session {record.id}: its tree is listed here'
            except Exception as exc:
                note = f'Cannot launch the rebase session: {exc}'
            self.app.call_from_thread(self._acted, note)

        self.run_worker(work, thread=True, group='branches-act')

    def action_accept(self) -> None:
        """A, on a rebase session's tree: move the base to the session's result."""
        from .app import Confirm

        key = self.selected_item()
        sid = self.data.rebases.get(key or '')
        if key is None or sid is None:
            return
        label = next((i.label for i in self.data.items if i.key == key), sid)
        prompt = f"Move the base to this session's result ({label})? [y|N]"

        def answered(confirmed: Optional[bool]) -> None:
            if confirmed:
                self._accept(sid)

        self.app.push_screen(Confirm(prompt), answered)

    def _accept(self, sid: str) -> None:
        self._status('Accepting…')

        def work() -> None:
            try:
                result = self.service.accept(self.workspace, sid)
            except Exception as exc:
                result = RebaseResult(False, error=str(exc))
            note = (
                f'Accepted: the base is at {result.tip[:12]}; end session {sid} when done'
                if result.ok
                else f'Not accepted: {result.error}'
            )
            self.app.call_from_thread(self._acted, note)

        self.run_worker(work, thread=True, group='branches-act')

    def _acted(self, note: str) -> None:
        self._status(note)
        self._note = note
        self.action_reload()

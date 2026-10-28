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

import pathlib
from dataclasses import replace
from typing import Any, Callable, Mapping, Optional, Sequence, TypeVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Footer, OptionList, SelectionList, Static
from textual.widgets.option_list import Option
from textual.widgets.selection_list import Selection

from .attachdialog import AttachDialog
from .attachments import Attachment
from .branchscreen import Ask, Pick
from .model import Reply, Series, SeriesThread, WorkItem
from .reader import (
    TreeRow,
    find_post,
    message_blocks,
    post_keys,
    root_blocks,
    thread_replies,
    tree_rows,
)
from .widgets import Block, TabBar, TextPane
from .workbench import (
    ContentRow,
    Item,
    Workbench,
    WorkbenchError,
    WorkbenchService,
    add_branch,
    add_file,
    add_mail,
    add_project,
    add_series,
    move_item,
    move_to_project,
    remove_item,
    remove_project,
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
T = TypeVar('T')
ITEMS = ('heading', 'entry', 'item')  # Rows whose text on the right is the launch preview.


def _count(n: int, one: str) -> str:
    return f'{n} {one}' + ('' if n == 1 else 's')


class PickMany(ModalScreen[Optional[list[str]]]):
    """Tick any of several: space ticks, Enter takes the ticked (none is fine), Escape
    cancels."""

    DEFAULT_CSS = """
    PickMany { align: center middle; }
    PickMany > Vertical { width: auto; height: auto; border: solid $accent; background: $panel; }
    PickMany SelectionList { width: auto; height: auto; border: none; }
    """
    BINDINGS = [
        Binding('escape', 'cancel', 'cancel'),
        Binding('enter', 'confirm', 'take them', priority=True),
    ]

    def __init__(self, title: str, choices: Sequence[tuple[str, str]]) -> None:
        super().__init__()
        self.title_text = title
        self.choices = list(choices)  # (key, label)

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.title_text, markup=False)
            yield SelectionList[str](*(Selection(label, key) for key, label in self.choices))

    def on_mount(self) -> None:
        self.query_one(SelectionList).focus()

    def on_selection_list_selection_highlighted(
        self, event: SelectionList.SelectionHighlighted
    ) -> None:
        event.stop()

    def on_selection_list_selected_changed(self, event: SelectionList.SelectedChanged) -> None:
        event.stop()

    def action_confirm(self) -> None:
        chosen = set(self.query_one(SelectionList).selected)
        self.dismiss([key for key, _label in self.choices if key in chosen])

    def action_cancel(self) -> None:
        self.dismiss(None)


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
    /* A thread opened from Mailboxes takes Contents' and the text pane's place. */
    WorkbenchScreen #bench-thread { display: none; width: 9fr; }
    WorkbenchScreen.-thread #bench-contents, WorkbenchScreen.-thread #bench-show-pane {
        display: none;
    }
    WorkbenchScreen.-thread #bench-thread { display: block; }
    WorkbenchScreen #thread-tree { width: 3fr; }
    WorkbenchScreen #thread-content { width: 5fr; }
    WorkbenchScreen #thread-tree, WorkbenchScreen #thread-content {
        border: none; border-top: solid $panel-lighten-2; border-left: solid $panel-lighten-2;
    }
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
        Binding('a', 'add', 'add'),
        Binding('B', 'add_branch', 'add branch'),
        Binding('m', 'move_project', 'to another project', show=False),
        Binding('shift+up', 'move(-1)', 'move up', key_display='⇧↑', show=False),
        Binding('shift+down', 'move(1)', 'move down', key_display='⇧↓', show=False),
        Binding('v', 'reroll', 'newer revision'),
        Binding('f1', 'launch', 'session', key_display='F1'),
        Binding('o', 'open_live', 'open session'),
        # On a session: beside the app; on a mailbox: its thread (before the lists' Enter).
        Binding('enter', 'open_session', 'open beside', priority=True),
        Binding('enter', 'open_thread', 'open thread', priority=True),
        # In a thread opened here: reply to the selected message (without, or with output).
        Binding('r', 'reply', 'reply'),
        Binding('R', 'reply_attach', 'reply + session output'),
        # On Trees: what Branches did with session trees.
        Binding('space', 'mark', 'mark'),
        Binding('p', 'pluck', 'pluck'),
        Binding('P', 'new_prep', 'new prep branch'),
        Binding('f', 'fold', 'fold to prep'),
        Binding('D', 'derive', 'derive a session', show=False),
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
        self._row_texts: dict[ContentRow, str] = {}  # Rows' texts read (not trees').
        self._preview = ''  # What a launch of the selected workbench would prepare.
        self._shown = ''  # The text on the right.
        self._status_text = ''
        self.stale = False  # Set when the app switches back to this tab.
        self._sessions: list[tuple[str, str, bool]] = []  # Its sessions: (id, label, live).
        self._sessions_of = UNFILED + '?'  # The workbench they are of (none yet).
        self._pending: tuple[str, str] = ('', '')  # (workbench, session) to select.
        self._unfiled = False  # Sessions without a workbench exist: list them apart.
        self._keep: tuple[str, str, int] = ('', '', -1)  # The row to keep after a reload.
        self._thread: Optional[SeriesThread] = None  # A thread opened from Mailboxes.
        self._thread_item: Optional[WorkItem] = None
        self._thread_rows: list[TreeRow] = []
        self._blocks: list[Block] = []  # The selected message's text.
        self._marks: list[int] = []  # Marked commit rows (of one tree), in marking order.

    def compose(self) -> ComposeResult:
        yield TabBar('workbench')
        yield Static('Workbenches', id='bench-title')
        with Horizontal(id='bench-panes'):
            with Vertical(id='bench-left'):
                yield OptionList(id='bench-list')
                yield OptionList(id='bench-sessions')
            yield OptionList(id='bench-contents')
            yield TextPane(id='bench-show-pane')  # Only the lines in view are drawn.
            with Horizontal(id='bench-thread'):
                yield OptionList(id='thread-tree')
                yield TextPane(id='thread-content')
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
        self._row_texts = {}
        self._marks = []
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
        known = self._row_texts.get(row)
        if known is not None:
            self._show_text(known, row.text.strip())
            return
        workspace, series, latest = self.workspace, self.series, self.latest

        def work() -> None:
            try:
                text = self.service.show(workspace, bench, row, series, latest)
            except Exception as exc:
                text = f'Cannot show it: {exc}'
            self.app.call_from_thread(self._row_shown, row, text)

        self.run_worker(work, thread=True, group='bench-show', exclusive=True)

    def _row_shown(self, row: ContentRow, text: str) -> None:
        """A row's text, once read: kept (a tree's state changes, so not a tree's), and
        shown if the row is still the one selected."""
        if row.kind != 'tree' and row in self._rows:
            self._row_texts[row] = text
        if self.row() == row and self._pane() == 'bench-contents':
            self._show_text(text, row.text.strip())

    def _set_preview(self, text: str) -> None:
        """The launch preview: shown unless the right pane follows a session or a
        contents row with its own text."""
        self._preview = text
        row, pane = self.row(), self._pane()
        own = pane == 'bench-contents' and row is not None and row.kind not in ITEMS
        if pane != 'bench-sessions' and not own:
            self._show_text(text, 'Launch preview')

    def _show_text(self, text: str, title: str) -> None:
        pane = self.query_one('#bench-show-pane', TextPane)
        if text != self._shown:
            self._shown = text
            pane.set_text(text)
        pane.border_title = title[:60]

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
        if rows == self._sessions and wid == self._sessions_of and self._pending[0] != wid:
            return  # A poll that changed nothing.
        self._sessions, self._sessions_of = rows, wid
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

    # A thread opened from Mailboxes.

    def thread_open(self) -> bool:
        return self.has_class('-thread')

    def action_open_thread(self) -> None:
        row = self.row()
        load = getattr(self.app, 'bench_thread', None)
        if row is None or row.kind != 'mailbox' or load is None:
            return
        identity = row.series

        def work() -> None:
            try:
                item, thread = load(identity)
            except Exception as exc:
                self.app.call_from_thread(self._status, f'Cannot open the thread: {exc}')
                return
            self.app.call_from_thread(self._thread_loaded, item, thread)

        self._status('Loading the thread…')
        self.run_worker(work, thread=True, group='bench-thread', exclusive=True)

    def _thread_loaded(self, item: WorkItem, thread: SeriesThread) -> None:
        self._thread_item, self._thread = item, thread
        self._thread_rows = tree_rows(
            thread, item, set(post_keys(thread)), bots=self.workspace.bots
        )
        tree = self.query_one('#thread-tree', OptionList)
        with self.prevent(OptionList.OptionHighlighted):
            tree.clear_options()
            tree.add_options([Option(Text(r.label)) for r in self._thread_rows])
            if self._thread_rows:
                tree.highlighted = 0
        tree.border_title = f'{item.series.subject} v{item.series.revision}'[:60]
        self.add_class('-thread')
        tree.focus()
        self._show_message()
        self._status('r replies to the selected message, R with session output; escape closes')
        self.refresh_bindings()

    def _message(self) -> tuple[Optional[TreeRow], Optional[Reply]]:
        index = self.query_one('#thread-tree', OptionList).highlighted
        if self._thread is None or index is None or index >= len(self._thread_rows):
            return None, None
        row = self._thread_rows[index]
        replies = thread_replies(self._thread, row.key) if row.node else ()
        return row, (replies[row.node - 1] if row.node and row.node <= len(replies) else None)

    def _show_message(self) -> None:
        row, reply = self._message()
        content = self.query_one('#thread-content', TextPane)
        if row is None or self._thread is None or self._thread_item is None:
            content.set_text('')
            return
        if reply is None:
            self._blocks = root_blocks(self._thread, row.key, self._thread_item)
        else:
            self._blocks = message_blocks(reply, False)
        content.set_blocks(self._blocks)

    def thread_msgid(self) -> str:
        """The selected message's id: a reply's, or the posting's."""
        row, reply = self._message()
        if reply is not None:
            return reply.message.msgid
        post = find_post(self._thread, row.key) if row is not None and self._thread else None
        return post.message.msgid if post is not None else ''

    def thread_labels(self) -> list[str]:
        return [r.label for r in self._thread_rows]

    def thread_text(self) -> str:
        return '\n'.join(b.text for b in self._blocks)

    def action_close_thread(self) -> None:
        self.remove_class('-thread')
        self._thread = self._thread_item = None
        self.query_one('#bench-contents', OptionList).focus()
        self._status('')
        self.refresh_bindings()

    def action_reply(self) -> None:
        self._reply(False)

    def action_reply_attach(self) -> None:
        self._reply(True)

    def _reply(self, attach: bool) -> None:
        reply = getattr(self.app, 'reply_from_bench', None)
        msgid = self.thread_msgid()
        if reply is not None and self._thread_item is not None and msgid:
            reply(self._thread_item, msgid, attach)

    # Session trees (the Trees section).

    def marked(self) -> list[str]:
        found = (self._rows[n].attachment for n in self._marks)
        return [a.sha for a in found if a is not None]

    def _chosen(self) -> list[ContentRow]:
        """The marked commits, else the selected one."""
        if self._marks:
            return [self._rows[n] for n in self._marks]
        row = self.row()
        return [row] if row is not None and row.kind == 'commit' else []

    def action_mark(self) -> None:
        lists = self.query_one('#bench-contents', OptionList)
        index, row = lists.highlighted, self.row()
        if index is None or row is None or row.kind != 'commit':
            return
        if self._marks and self._rows[self._marks[0]].item != row.item:
            self._unmark_all()  # Marks stay within one tree.
        if index in self._marks:
            self._marks.remove(index)
            lists.replace_option_prompt_at_index(index, row.text)
        else:
            self._marks.append(index)
            lists.replace_option_prompt_at_index(index, '●' + row.text[1:])
        self.refresh_bindings()

    def _unmark_all(self) -> None:
        lists = self.query_one('#bench-contents', OptionList)
        for n in self._marks:
            lists.replace_option_prompt_at_index(n, self._rows[n].text)
        self._marks = []

    def _act(self, call: Callable[[], T], done: Callable[[T], None], doing: str) -> None:
        """Run *call* off the UI thread, then *done* with its result (or say why not)."""

        def work() -> None:
            try:
                result = call()
            except Exception as exc:
                self.app.call_from_thread(self._status, f'Not done: {exc}')
                return
            self.app.call_from_thread(done, result)

        self._status(doing)
        self.run_worker(work, thread=True, group='bench-act')

    def action_pluck(self) -> None:
        from .app import Confirm

        rows = self._chosen()
        targets_of = getattr(self.app, 'bench_tree_targets', None)
        pluck: Any = getattr(self.app, 'bench_pluck', None)
        if not rows or targets_of is None or pluck is None:
            return
        item, n = rows[0].item, len(rows)
        commits = _count(n, 'commit')

        def pick(found: tuple[list[tuple[str, str]], dict[str, str]]) -> None:
            targets, tips = found
            if not targets:
                self._status('Nowhere to pluck to: no base, prep branch, or other tree')
                return

            def picked(target: Optional[str]) -> None:
                if not target:
                    return
                label = dict(targets)[target]
                verb = 'Copy' if target.startswith('tree:') else 'Pluck'
                how = (
                    'they go on branches of the same name and keep their authors'
                    if verb == 'Copy'
                    else 'agent commits become yours, with Assisted-by; others keep their authors'
                )
                prompt = f'{verb} {commits} onto {label}? {how.capitalize()}. [y|N]'

                def answered(yes: Optional[bool]) -> None:
                    if yes:
                        self._act(
                            lambda: pluck(item, rows, target, tips.get(target, '')),
                            lambda r: self._plucked(r, label),
                            f'{verb}ing {commits}…',
                        )

                self.app.push_screen(Confirm(prompt), answered)

            self.app.push_screen(Pick(f'Pluck {commits} onto…', targets), picked)

        self._act(lambda: targets_of(item), pick, 'Listing where they can go…')

    def _plucked(self, result: object, label: str) -> None:
        ok = getattr(result, 'ok', False)
        if ok:
            n = len(getattr(result, 'picked', ()))
            dropped = getattr(result, 'dropped', ())
            extra = f'; already there: {len(dropped)}' if dropped else ''
            self._status(f'Plucked {_count(n, "commit")} onto {label}{extra}')
        else:
            failed = getattr(result, 'failed', '')
            why = f'{failed} does not apply' if failed else getattr(result, 'error', 'failed')
            self._status(f'Not plucked: {why}; nothing changed')
        self._load_contents()

    def _folded(self) -> Optional[ContentRow]:
        """The selected row, if it is a commit of a session's folded series."""
        row = self.row()
        a = row.attachment if row is not None else None
        if row is None or row.kind != 'commit' or a is None:
            return None
        return row if a.branch.startswith('b4m/folded/') else None

    def action_fold(self) -> None:
        """f: the folded series as a prep branch: the series' own (its commits replaced,
        the next revision), else a new one from the posted thread; each other
        pull-ahead leads or stays out."""
        from .app import Confirm

        row = self._folded()
        plan_of = getattr(self.app, 'bench_fold_plan', None)
        fold: Any = getattr(self.app, 'bench_fold', None)
        if row is None or row.attachment is None or plan_of is None or fold is None:
            return
        branch = row.attachment.branch

        def done(result: Any) -> None:
            if result.ok:
                self._status(
                    f'Made {result.branch} ({_count(result.picked, "commit")}): F4 shows it, '
                    'with its cover letter, checks, sign-off, and send'
                )
            else:
                self._status(f'Not folded: {result.error}')

        def planned(plan: Any) -> None:
            if plan.error:
                self._status(f'Cannot fold {branch}: {plan.error}')
                return

            def confirm(extras: list[str], name: str) -> None:
                lead = f', after {_count(len(extras), "pull-ahead")}' if extras else ''
                where = f'on {plan.base[:12]}{lead}'
                folded = f'{plan.subject} folded ({_count(plan.commits, "commit")})'
                if plan.replaces:
                    text = (
                        f'Replace the commits of {plan.target} with {folded} {where}? b4 keeps '
                        f'its cover letter and tracking: the next send is v{plan.revision}.'
                    )
                else:
                    text = (
                        f'Make b4/{name} for v{plan.revision} of {folded} {where}? b4 prep -F '
                        'reads the posted thread from lore for its cover letter.'
                    )

                def answered(yes: Optional[bool]) -> None:
                    if yes:
                        self._act(lambda: fold(plan, extras, name), done, 'Folding…')

                self.app.push_screen(Confirm(f'{text} [y|N]'), answered)

            def chosen(extras: Optional[list[str]]) -> None:
                if extras is None:
                    return
                if plan.replaces:
                    confirm(extras, '')
                    return

                def named(name: Optional[str]) -> None:
                    if name:
                        confirm(extras, name.strip())

                self.app.push_screen(
                    Ask('Name the new prep branch (b4/<name>)', plan.target), named
                )

            if not plan.optional:
                chosen([])
                return
            carried = ', '.join(plan.needs) or 'none'
            title = (
                f'It leads with: {carried}. Lead with which others? (those it conflicts '
                'without; leave independent fixes out)'
            )
            options = [(b, f'{title_} ({b})') for b, title_ in plan.optional]
            self.app.push_screen(PickMany(title, options), chosen)

        self._act(lambda: plan_of(row.item, branch), planned, 'Reading the folded series…')

    def action_new_prep(self) -> None:
        rows = self._chosen()
        forks_of = getattr(self.app, 'bench_fork_points', None)
        cut: Any = getattr(self.app, 'bench_cut_prep', None)
        if not rows or forks_of is None or cut is None:
            return
        item = rows[0].item

        def named(name: Optional[str]) -> None:
            if not name:
                return

            def pick(found: list[str]) -> None:
                def cut_at(fork: Optional[str]) -> None:
                    if fork:
                        self._act(lambda: cut(item, rows, name, fork), self._cut, 'Cutting…')

                def forked(fork: Optional[str]) -> None:
                    if fork == '\0typed':
                        self.app.push_screen(Ask(f'Fork b4/{name} from (any ref):'), cut_at)
                    else:
                        cut_at(fork)

                if not found:  # None known for it: any ref, typed.
                    self.app.push_screen(Ask(f'Fork b4/{name} from (any ref):'), cut_at)
                    return
                forks = [(f, f) for f in found] + [('\0typed', 'Another ref…')]
                self.app.push_screen(Pick(f'Fork point for b4/{name}', forks), forked)

            self._act(lambda: forks_of(item), pick, 'Listing fork points…')

        self.app.push_screen(Ask('Name the new prep branch'), named)

    def _cut(self, result: object) -> None:
        if getattr(result, 'ok', False):
            branch, n = getattr(result, 'branch', ''), getattr(result, 'picked', 0)
            self._status(f'Made {branch} from {_count(n, "commit")}: F4 shows it')
            self._unmark_all()
        else:
            self._status(f'No prep branch: {getattr(result, "error", "failed")}')

    def _remove_tree(self, row: ContentRow) -> None:
        from .app import Confirm

        losses = getattr(self.app, 'bench_losses', None)
        reap: Any = getattr(self.app, 'bench_reap', None)
        if losses is None or reap is None:
            return

        def confirm(text: str) -> None:
            def answered(yes: Optional[bool]) -> None:
                if yes:
                    self._act(lambda: reap(row.item), self._reaped, 'Removing…')

            self.app.push_screen(Confirm(f'Remove {text}? [y|N]'), answered)

        self._act(lambda: losses(row.item), confirm, 'Looking at what it would lose…')

    def _reaped(self, result: tuple[bool, str]) -> None:
        ok, note = result
        self._status(note)
        if ok:
            self._load_contents()

    def action_derive(self) -> None:
        from .launchdialog import Derived

        row = self.row()
        derive = getattr(self.app, 'bench_derive', None)
        if row is None or row.kind not in ('tree', 'commit') or derive is None:
            return
        parent = row.session

        def pick(found: tuple[list[tuple[str, str, str]], Any]) -> None:
            options, context = found
            if not options:
                self._status('That session has no tree to derive from')
                return
            by_key = {str(n): option for n, option in enumerate(options)}

            def picked(choice: Optional[str]) -> None:
                if choice is None:
                    return
                start, start_from, label = by_key[choice]
                launch = getattr(self.app, 'launch_derived', None)
                if launch is not None:
                    launch(parent, Derived(parent, start, start_from, label), context)

            choices = [(n, option[2]) for n, option in by_key.items()]
            self.app.push_screen(Pick('Start the new session from…', choices), picked)

        self._act(lambda: derive(row.item, row if row.kind == 'commit' else None), pick, '')

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
        elif event.option_list.id == 'thread-tree':
            self._show_message()
        self.refresh_bindings()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()

    def action_cycle(self, step: int) -> None:
        names = ('bench-list', 'bench-sessions', 'bench-contents', 'bench-show-pane')
        if self.thread_open():
            names = ('bench-list', 'bench-sessions', 'thread-tree', 'thread-content')
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
        if self.thread_open():
            self.action_close_thread()  # Escape closes a thread first.
            return
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
        edits = ('rename', 'remove', 'base', 'add_branch', 'move', 'reroll', 'launch', 'add')
        edits += ('move_project',)
        if action in edits and not self._editable():
            return False
        if action == 'rename':
            return pane == 'bench-list' and bench is not None and not self.thread_open()
        if action == 'remove':
            if pane == 'bench-contents':
                row = self.row()
                return bool(self._project()) or (row is not None and row.kind == 'tree')
            return pane == 'bench-list' and bench is not None
        if action == 'mark':
            row = self.row()
            return pane == 'bench-contents' and row is not None and row.kind == 'commit'
        if action in ('pluck', 'new_prep'):
            return pane == 'bench-contents' and bool(self._chosen())
        if action == 'fold':
            return pane == 'bench-contents' and self._folded() is not None
        if action == 'derive':
            row = self.row()
            kinds = ('tree', 'commit')
            return pane == 'bench-contents' and row is not None and row.kind in kinds
        if action in ('base', 'add_branch'):
            return bool(self._project())
        if action in ('move', 'move_project'):
            return self._item() is not None
        if action == 'reroll':
            return self._newer() is not None
        if action == 'launch':
            launchable = hasattr(self.app, 'launch_workbench')
            return bench is not None and bool(bench.entries) and launchable
        if action == 'open_session':
            return pane == 'bench-sessions' and bool(self.selected_session())
        if action == 'open_thread':
            row = self.row()
            mail = row is not None and row.kind == 'mailbox' and bool(row.series)
            return pane == 'bench-contents' and mail  # Files have no thread.
        if action in ('reply', 'reply_attach'):
            return self.thread_open() and hasattr(self.app, 'reply_from_bench')
        if action == 'close_thread':
            return self.thread_open()
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
        """n: a workbench, named, with the projects ticked (each at its first maintainer
        base): a session can have their trees even before anything is added."""

        def named(label: Optional[str]) -> None:
            if not label:
                return

            def ticked(projects: Optional[list[str]]) -> None:
                if projects is None:
                    return
                bench = Workbench(self.service.store.new_id(label), label)
                for project in projects:
                    bench = add_project(bench, project, self._default_base(project))
                self.service.store.save(bench)
                self._reload(bench.id)
                self._status(f'Made {label}: w adds series from the series list or a thread')

            choices = [(p.id, p.label) for p in self.workspace.projects]
            self.app.push_screen(PickMany(f'Projects in {label} (space ticks)', choices), ticked)

        self.app.push_screen(Ask('Name the new workbench'), named)

    def _default_base(self, project: str) -> str:
        found = self.workspace.project(project)
        return found.bases[0].branch if found is not None and found.bases else ''

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
        row = self.row()
        if self._pane() == 'bench-contents' and row is not None and row.kind == 'tree':
            self._remove_tree(row)
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
                    self._save(remove_project(bench, project), note=f'Removed {project}')

            entry = bench.entry(project)
            what = _count(len(entry.items) if entry is not None else 0, 'item')
            self.app.push_screen(
                Confirm(f'Remove {project} ({what}) from {bench.label}? [y|N]'), drop_entry
            )
            return

        look = getattr(self.app, 'bench_removal', None)
        if look is None:
            self._confirm_removal(bench, ([], [], {}))
            return
        self._act(
            lambda: look(bench.id),
            lambda found: self._confirm_removal(bench, found),
            'Looking at what it would lose…',
        )

    def _confirm_removal(
        self, bench: Workbench, found: tuple[list[str], list[str], Mapping[str, Any]]
    ) -> None:
        """Removing a workbench takes its sessions: live ones ended, their trees removed,
        their records and output deleted. A tree with changes asks a second time."""
        from .app import Confirm

        sids, live, losses = found
        reap: Any = getattr(self.app, 'remove_bench_sessions', None)
        if sids:
            listed = ', '.join(f'{s} (live: ended)' if s in live else s for s in sids)
            lines = [f'Remove the workbench {bench.label} and its sessions?']
            lines.append(f'  Sessions, deleted with their output: {listed}')
            lines += [f'  {path}: {state.describe()}' for path, state in losses.items()]
        else:
            lines = [f'Remove the workbench {bench.label} (no sessions)?']
        changed = [f'  {p}: {s.describe()}' for p, s in losses.items() if not s.clean]

        def removed(_result: object) -> None:
            self._reload()
            self._status(
                f'Removed {bench.label}' + (f' and {_count(len(sids), "session")}' if sids else '')
            )

        def remove() -> None:
            def work() -> None:
                if sids and reap is not None:
                    reap(sids)
                self.service.store.delete(bench.id)

            self._act(work, removed, 'Removing…')

        def again(yes: Optional[bool]) -> None:
            if yes:
                remove()

        def first(yes: Optional[bool]) -> None:
            if not yes:
                return
            if changed:
                prompt = 'This will be lost:\n' + '\n'.join(changed) + '\nRemove anyway? [y|N]'
                self.app.push_screen(Confirm(prompt), again)
            else:
                remove()

        self.app.push_screen(Confirm('\n'.join([*lines, '[y|N]'])), first)

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

    def action_add(self) -> None:
        """a: add a thread (by lore link or Message-ID, fetched into the cache) or a file
        from disk."""
        bench = self.bench()
        if bench is None or not self._editable():
            return
        ways = [
            ('link', 'A thread: lore link or Message-ID (series or discussion)'),
            ('file', 'A file from disk (patches are applied, anything else is read)'),
            ('project', 'A project (a tree at its base)'),
            ('artifact', 'Artifacts from sessions (any workbench): commits, reports, diffs'),
        ]

        def picked(way: Optional[str]) -> None:
            if way == 'project':
                self._add_project()
            elif way == 'artifact':
                self._add_artifacts()
            elif way == 'link':
                self.app.push_screen(Ask('Lore link or Message-ID'), self._add_link)
            elif way == 'file':
                self.app.push_screen(Ask('Path of the file'), self._add_file)

        self.app.push_screen(Pick(f'Add to {bench.label}', ways), picked)

    def _add_artifacts(self) -> None:
        bench = self.bench()
        offer = getattr(self.app, 'bench_artifacts', None)
        if bench is None or offer is None:
            return
        wid = bench.id

        def pick(found: list[tuple[Attachment, str, Item]]) -> None:
            if not found:
                self._status('No session has left a commit or file to add')
                return
            mapped = {id(a): (project, item) for a, project, item in found}
            heading = (
                'Add to the workbench · space ticks · / filters (↓ back) · enter adds · '
                'escape cancels (a commit brings its branch)'
            )

            def chosen(picked: Optional[list[Attachment]]) -> None:
                if picked:
                    self._add_mapped([mapped[id(a)] for a in picked])

            self.app.push_screen(AttachDialog([a for a, _p, _i in found], heading), chosen)

        self._act(lambda: offer(wid), pick, 'Gathering session output…')

    def _add_mapped(self, chosen: list[tuple[str, Item]]) -> None:
        """Commits under their tree's project; files under the project picked once."""
        start = self.bench()
        if start is None:
            return
        with_branches, added = start, 0
        for project, item in chosen:
            if project:
                try:
                    base = self._default_base(project)
                    with_branches = add_branch(with_branches, project, item.branch, base)
                    added += 1
                except WorkbenchError:
                    pass  # Its branch is on already (another commit of it).
        files = [item.file for project, item in chosen if not project]
        if not files:
            self._save(with_branches, None, f'Added {_count(added, "branch")}')
            return

        def under(project: str) -> None:
            result, count = with_branches, added
            for path in files:
                try:
                    result = add_file(result, project, path, self._default_base(project))
                    count += 1
                except WorkbenchError:
                    pass
            self._save(result, None, f'Added {_count(count, "item")}')

        self._for_project('', under)

    def _add_project(self) -> None:
        bench = self.bench()
        if bench is None:
            return
        taken = {e.project for e in bench.entries}
        choices = [(p.id, p.label) for p in self.workspace.projects if p.id not in taken]
        if not choices:
            self._status('Every project is on this workbench already')
            return

        def picked(project: Optional[str]) -> None:
            bench = self.bench()
            if project and bench is not None:
                changed = add_project(bench, project, self._default_base(project))
                self._save(changed, ('entry', project, -1), f'Added {project}')

        self.app.push_screen(Pick('Which project?', choices), picked)

    def _add_link(self, text: Optional[str]) -> None:
        fetch = getattr(self.app, 'bench_fetch', None)
        if not text or fetch is None:
            return
        self._act(lambda: fetch(text), self._add_fetched, 'Fetching the thread…')

    def _add_fetched(self, item: WorkItem) -> None:
        s = item.series
        self.series = {**self.series, item.identity: s}
        self.latest = {**self.latest, item.identity: s.revision}
        msgid = s.msgids[0] if s.msgids else ''

        def add(project: str) -> None:
            bench = self.bench()
            if bench is None:
                return
            try:
                if s.discussion:
                    changed = add_mail(bench, project, item.identity, s.subject, msgid)
                else:
                    changed = add_series(
                        bench, project, item.identity, s.revision, s.subject, msgid=msgid
                    )
            except WorkbenchError as ex:
                self._status(str(ex))
                return
            self._save(changed, None, f'Added {s.subject} ({project})')

        self._for_project(item.project or '', add)

    def _add_file(self, text: Optional[str]) -> None:
        if not text:
            return
        path = pathlib.Path(text).expanduser()
        if not path.is_absolute():
            path = pathlib.Path.cwd() / path
        if not path.is_file():
            self._status(f'{path} does not exist')
            return

        def add(project: str) -> None:
            bench = self.bench()
            if bench is None:
                return
            try:
                changed = add_file(bench, project, str(path))
            except WorkbenchError as ex:
                self._status(str(ex))
                return
            self._save(changed, None, f'Added {path.name} ({project})')

        self._for_project('', add)

    def _for_project(self, hint: str, then: Callable[[str], None]) -> None:
        """Run *then* with the project to add under: the selected project row's, else the
        one picked (*hint*, the thread's own, suggested first; then the workbench's
        projects; then the rest)."""
        if self._project():
            then(self._project())
            return
        self.app.push_screen(
            Pick('Under which project?', self._project_choices(hint)), lambda p: p and then(p)
        )

    def _project_choices(self, hint: str, besides: str = '') -> list[tuple[str, str]]:
        bench = self.bench()
        entries = [e.project for e in bench.entries] if bench is not None else []
        order = [hint] if hint else []
        order += entries + [p.id for p in self.workspace.projects]
        labels = {p.id: p.label for p in self.workspace.projects}
        choices = []
        for pid in dict.fromkeys(p for p in order if p and p != besides):
            mark = ' (suggested)' if pid == hint else ''
            choices.append((pid, f'{labels.get(pid, pid)}{mark}'))
        return choices

    def action_move_project(self) -> None:
        """m: the selected item under another project (its tree, its base)."""
        bench, item = self.bench(), self._item()
        if bench is None or item is None:
            return

        def moved(to: Optional[str]) -> None:
            if not to:
                return
            changed = move_to_project(bench, item.project, item.index, to, self._default_base(to))
            entry = changed.entry(to)
            keep = ('item', to, len(entry.items) - 1 if entry else -1)
            self._save(changed, keep, f'Moved to {to}')

        choices = self._project_choices('', besides=item.project)
        self.app.push_screen(Pick('Move it under which project?', choices), moved)

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

# SPDX-License-Identifier: GPL-2.0-or-later
"""Browse screen (Views | Series | Summary) and pre-apply reader, with a session drawer."""

import dataclasses
import pathlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Iterable, Optional, Protocol, Sequence

from b4._textwidth import pad_display
from b4.review_tui._reply import preview_and_send, reply_quote
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.geometry import Size
from textual.markup import escape
from textual.message import Message
from textual.reactive import Reactive
from textual.screen import ModalScreen, Screen
from textual.scroll_view import ScrollView
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Input, OptionList, Static, TextArea
from textual.widgets.option_list import Option
from textual.worker import Worker, WorkerState

from .adapters.b4mbox import load_thread
from .adapters.bases import PluckResult
from .adapters.drafts import DraftStore, SavedDraft
from .applydialog import ApplyDialog
from .attachdialog import AttachDialog
from .attachments import Attachment, gather, gather_all, git_identity, preview, render
from .branches import ApplyChoices, CutResult, DetailRow, DropChoices
from .branchscreen import Ask, Branches, BranchesScreen, Pick
from .drawer import drawer_line, session_label, session_rows, tabs_line, worktree_row
from .launchdialog import Derived, LaunchDialog, LaunchResult
from .layout import Breakpoints, Mode, layout_mode
from .model import Reply, Series, SeriesThread, WorkItem
from .package import series_context
from .reader import (
    SASHIKO_PREFIX,
    SEVERITY_STYLES,
    TreeRow,
    branch,
    content_title,
    find_post,
    finding_anchors,
    has_long_quote,
    initially_collapsed,
    initially_expanded,
    message_blocks,
    parent_node,
    post_keys,
    root_blocks,
    thread_replies,
    tree_rows,
    with_sashiko,
    without_clean_sashiko,
)
from .rows import DEFAULT_SYMBOLS, Segments, Symbols, heading, row_segments
from .search import compile_query, match_texts, texts
from .sessions.hub import Overview, SessionError, SessionHub, TreePlan
from .sessions.launch import LaunchChoice, Situation, Target, plan
from .sessions.pane import pane_for
from .sessions.policy import ContextItem
from .sessions.profiles import Profile
from .sessions.records import SessionRecord
from .sessions.skills import Skill
from .sessions.store import SessionView
from .sessions.trees import TreeState
from .views import ViewIndex
from .widgets import Block, KeyBar, TabBar, TextPane, printable
from .workbench import (
    ContentRow,
    Item,
    Workbench,
    WorkbenchError,
    add_mail,
    add_series,
    artifact_item,
    series_bench,
)
from .workbenchscreen import WorkbenchScreen
from .workspace import Snapshot, Workspace

if TYPE_CHECKING:
    from .adapters.apply import ApplyResult  # b4: only when applying.
    from .workbench import WorkbenchService

SESSION_ACTIONS = frozenset({'open_session', 'stop_session', 'end_session', 'session_artifacts'})
WORKTREE_ACTIONS = frozenset({'mark_worktree', 'remove_worktrees'})
DRAWER_ACTIONS = frozenset({'toggle_drawer', 'back', 'cycle_pane', 'quit', 'focus_session'})
LIVE_STATES = frozenset({'approval', 'waiting', 'working', 'starting'})
UNREAD_STYLE = Style(color='green')
CURSOR_STYLE = Style(reverse=True)
HEADING_STYLE = Style(bold=True)
# Author names by MAINTAINERS role; Sashiko findings by worst severity; missing patches.
ROLE_STYLES = {
    'maintainer': Style(color='green'),
    'reviewer': Style(color='blue'),
    'sev-critical': Style(color='red', bold=True),
    'sev-high': Style(color='red'),
    'sev-medium': Style(color='yellow'),
    'sev-low': Style(dim=True),
    'incomplete': Style(color='yellow'),
    'stable': Style(color='dark_orange'),
}


class ExternalEditor(Protocol):
    """The maintainer's editor beside the app (a tmux pane); calls block briefly."""

    def open(self, path: pathlib.Path, checkout: pathlib.Path, below: str = '') -> str: ...

    def alive(self, pane: str) -> bool: ...

    def runs(self, pane: str, path: pathlib.Path) -> bool: ...

    def focus(self, pane: str) -> None: ...

    def close(self, pane: str) -> None: ...

    def save_and_close(self, pane: str) -> bool: ...


class SourceActions(Protocol):
    """Explicit maintainer actions on a live workspace; both block, so run in workers."""

    def refresh(self, progress: Callable[[str], None]) -> str: ...

    def track(self, item: WorkItem) -> str: ...

    def untrack(self, item: WorkItem) -> str: ...

    def toggle_watched(self, item: WorkItem) -> str: ...

    def toggle_hidden(self, item: WorkItem) -> str: ...

    def mark_seen(self, msgids: Iterable[str]) -> None: ...

    def mark_read(self, item: WorkItem) -> str: ...

    def prepare_reply(self, item: WorkItem, msgid: str) -> Any: ...

    def answered(self, item: WorkItem, msgid: str) -> str: ...


class SeriesList(ScrollView, can_focus=True):
    """Virtual list: renders only visible rows; switching views swaps a tuple.

    Line 0 is a fixed heading naming the fields; item rows scroll beneath it.
    """

    BINDINGS = [
        Binding('down,j', 'move(1)', 'Down', show=False),
        Binding('up,k', 'move(-1)', 'Up', show=False),
        Binding('pagedown,space', 'page(1)', 'Page down', show=False),
        Binding('pageup', 'page(-1)', 'Page up', show=False),
        Binding('home', 'jump(0)', 'First', show=False),
        Binding('end', 'jump(-1)', 'Last', show=False),
    ]

    # Pane size comes from the layout, never its content, so a new scroll
    # extent must not relayout the whole screen.
    virtual_size: Reactive[Size] = Reactive(Size(0, 0), layout=False)

    class Highlighted(Message):
        def __init__(self, item: Optional[WorkItem]) -> None:
            self.item = item
            super().__init__()

    def __init__(self, symbols: Symbols = DEFAULT_SYMBOLS, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.symbols = symbols
        self.project_width = 0
        self.cursor = 0
        self._items: tuple[WorkItem, ...] = ()
        self._rows: dict[int, Segments] = {}
        self._heading = heading()

    @property
    def row_count(self) -> int:
        return len(self._items)

    @property
    def highlighted_item(self) -> Optional[WorkItem]:
        return self._items[self.cursor] if self._items else None

    def set_items(self, items: tuple[WorkItem, ...], project_width: int = 0) -> None:
        self._items = items
        self._rows = {}
        self.project_width = project_width
        self._heading = heading(project_width)
        self.cursor = 0
        self.virtual_size = Size(0, len(items) + 1)
        self.scroll_to(0, 0, animate=False, immediate=True)
        self.refresh()
        self.post_message(self.Highlighted(self.highlighted_item))

    def select_identity(self, identity: str, fallback: int = 0) -> None:
        """Move to the item with this identity, else to the fallback position."""
        index = next((n for n, i in enumerate(self._items) if i.identity == identity), fallback)
        self._set_cursor(index)

    def _row(self, index: int) -> Segments:
        row = self._rows.get(index)
        if row is None:
            row = row_segments(self._items[index], self.symbols, self.project_width)
            self._rows[index] = row
        return row

    @property
    def _visible_rows(self) -> int:
        return max(0, self.scrollable_content_region.height - 1)

    def render_line(self, y: int) -> Strip:
        width = self.scrollable_content_region.width
        if y == 0:
            return Strip([Segment(pad_display(self._heading, width), HEADING_STYLE)]).crop(0, width)
        index = self.scroll_offset.y + y - 1
        if index >= len(self._items):
            return Strip.blank(width)
        item = self._items[index]
        # Unread colours the row; a pending stable fix colours only its ⚠ (a row role).
        style = UNREAD_STYLE if item.state.unread else Style()
        segments = []
        for text, role in self._row(index):
            seg_style = style + ROLE_STYLES[role] if role in ROLE_STYLES else style
            if index == self.cursor:
                seg_style = seg_style + CURSOR_STYLE
            segments.append(Segment(text, seg_style))
        fill = style + CURSOR_STYLE if index == self.cursor else style
        segments.append(Segment(' ' * width, fill))
        return Strip(segments).crop(0, width)

    def _set_cursor(self, index: int) -> None:
        if not self._items:
            return
        index = max(0, min(index, len(self._items) - 1))
        if index == self.cursor:
            return
        self.cursor = index
        height = self._visible_rows
        top = self.scroll_offset.y
        if index < top:
            self.scroll_to(y=index, animate=False, immediate=True)
        elif height and index >= top + height:
            self.scroll_to(y=index - height + 1, animate=False, immediate=True)
        self.refresh()
        self.post_message(self.Highlighted(self.highlighted_item))

    def action_move(self, delta: int) -> None:
        self._set_cursor(self.cursor + delta)

    def action_page(self, direction: int) -> None:
        self._set_cursor(self.cursor + direction * max(1, self._visible_rows))

    def action_jump(self, index: int) -> None:
        self._set_cursor(len(self._items) - 1 if index < 0 else index)

    def on_click(self, event: events.Click) -> None:
        if event.y > 0:
            self._set_cursor(self.scroll_offset.y + event.y - 1)


class StatusLine(Widget):
    """Fixed-height text; updates repaint without the relayout Static.update() forces."""

    def __init__(self, text: str = '', **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._text = text

    def set_text(self, text: str) -> None:
        self._text = text
        self.refresh()

    def render(self) -> Text:
        return Text(self._text)


class Confirm(ModalScreen[bool]):
    """A [y|N] question: only y confirms; any other key cancels."""

    DEFAULT_CSS = """
    Confirm { align: center middle; }
    Confirm > Static {
        width: auto; height: auto; padding: 0 2;
        border: solid $accent; background: $panel;
    }
    """

    def __init__(self, prompt: str) -> None:
        super().__init__()
        self.prompt = prompt

    def compose(self) -> ComposeResult:
        yield Static(self.prompt, markup=False)

    def on_key(self, event: events.Key) -> None:
        event.stop()
        event.prevent_default()
        self.dismiss(event.key == 'y')


class ConfirmQuit(Confirm):
    PROMPT = 'Quit? [y|N]'

    def __init__(self, prompt: str = PROMPT) -> None:
        super().__init__(prompt)


class Notice(Confirm):
    """Text shown until any key."""


class DrawerList(OptionList):
    """A drawer tab; its keys act on the selected session (the app checks which apply)."""

    BINDINGS = [
        Binding('enter', 'app.open_session', 'switch'),
        Binding('s', 'app.stop_session', 'stop'),
        Binding('o', 'app.session_artifacts', 'artifacts'),
        Binding('x', 'app.end_session', 'end'),
    ]


class WorktreeList(DrawerList):
    """The Worktrees tab: space marks trees, x removes the marked (or the selected) one."""

    BINDINGS = [
        Binding('space', 'app.mark_worktree', 'mark'),
        Binding('x', 'app.remove_worktrees', 'remove'),
    ]


@dataclass
class Draft:
    """A reply being written beside the thread, to one message."""

    item: WorkItem
    msgid: str
    target: Any  # SourceActions.prepare_reply result: lmsg, checkout, mail flags
    quote: str
    # External editor pane writing the draft file; empty for the in-app Draft pane.
    pane: str = ''
    # Asked to save and exit (the thread was closed): its exit keeps the draft quietly.
    closing: bool = False


class MaintainerApp(App[None]):
    TITLE = 'b4-maintainer'
    CSS = """
    #status { height: 1; background: $primary-darken-2; }
    #main { height: 1fr; }
    #views, #series, #summary, #tree, #content, #draft {
        border: none; border-top: solid $panel-lighten-2;
    }
    #views:focus, #series:focus, #summary:focus, #tree:focus, #content:focus, #draft:focus {
        border: none; border-top: solid $accent;
    }
    #summary, #summary:focus { border-left: solid $panel-lighten-2; }
    #views { width: 34; }
    #series { width: 1fr; }
    #summary {
        width: 64; padding: 0 1;
        scrollbar-gutter: stable;
    }
    #reader { height: 1fr; display: none; }
    #tree { width: 56; }
    #content { width: 1fr; padding: 0 1; scrollbar-gutter: stable; }
    #draft { width: 1fr; display: none; }
    Screen.-drafting #draft { display: block; }
    #artifacts { width: 1fr; display: none; border-top: solid $panel-lighten-2; }
    #artifact-list { height: auto; max-height: 40%; border: none; }
    #artifact-show { height: 1fr; padding: 0 1; border-top: solid $panel-lighten-2; }
    Screen.-artifacts #content { display: none; }
    Screen.-artifacts #artifacts { display: block; }
    Screen.-narrow.-reading #tree, Screen.-narrow.-reading #content,
    Screen.-narrow.-reading #draft { display: none; }
    Screen.-narrow.-rpane-tree #tree { display: block; width: 1fr; }
    Screen.-narrow.-rpane-content #content { display: block; width: 1fr; }
    Screen.-narrow.-rpane-draft #draft { display: block; width: 1fr; }
    Screen.-narrow.-reading #artifacts { display: none; }
    Screen.-narrow.-artifacts.-rpane-content #artifacts { display: block; width: 1fr; }
    Screen.-reading #main { display: none; }
    Screen.-reading #reader { display: block; }
    #drawer { height: 1; background: $panel; }
    #search { dock: bottom; display: none; height: 1; border: none; padding: 0 1; }
    Screen.-searching #search { display: block; }
    #drawer-panel { height: 40%; display: none; border-top: solid $accent; }
    Screen.-drawer-open #drawer-panel { display: block; }
    #drawer-tabs { height: 1; padding: 0 1; }
    #drawer-sessions, #drawer-worktrees { height: 1fr; border: none; }
    #drawer-worktrees { display: none; }
    Screen.-drawer-worktrees #drawer-sessions { display: none; }
    Screen.-drawer-worktrees #drawer-worktrees { display: block; }
    Screen.-medium #views { display: none; }
    Screen.-medium.-show-views #views { display: block; }
    Screen.-narrow #views { display: none; }
    Screen.-narrow #summary { display: none; }
    Screen.-narrow.-show-summary #series { display: none; }
    Screen.-narrow.-show-summary #summary { display: block; width: 1fr; border-left: none; }
    """
    BINDINGS = [
        # Less common keys stay off the footer; ? lists every key of the current tab.
        Binding('v', 'toggle_views', 'views', show=False),
        # Priority so the footer shows this hint instead of Screen's hidden tab binding.
        Binding('tab', 'cycle_pane(1)', 'next pane', priority=True, show=False),
        Binding('shift+tab', 'cycle_pane(-1)', 'previous pane', priority=True, show=False),
        Binding('ctrl+s', 'send_draft', 'send'),
        Binding('ctrl+g', 'discard_draft', 'discard'),
        Binding('e', 'focus_editor', 'editor'),
        Binding('enter', 'open_reader', 'read'),
        Binding('escape', 'back', 'back'),
        Binding('left_square_bracket', 'next_post(-1)', 'prev', key_display='['),
        Binding('right_square_bracket', 'next_post(1)', 'next', key_display=']'),
        Binding('n', 'next_comment', 'next comment'),
        Binding('exclamation_mark', 'next_finding', 'next finding', key_display='!'),
        Binding('minus', 'toggle_replies', 'replies', key_display='-'),
        Binding('equals_sign', 'toggle_all', 'all', key_display='='),
        # In the thread tree only; the content pane scrolls sideways with them.
        Binding('right', 'expand_node', 'expand', show=False),
        Binding('left', 'collapse_node', 'collapse', show=False),
        Binding('z', 'toggle_quotes', 'quotes'),
        Binding('p', 'toggle_preview', 'preview', show=False),
        Binding('slash', 'search', 'search', key_display='/'),
        # $ tracks, or untracks (archives in b4) after confirmation; t also tracks.
        Binding('dollar_sign', 'track', 'track', key_display='$'),
        Binding('dollar_sign', 'untrack', 'untrack', key_display='$'),
        # On discussions $ follows the conversation instead (client state, no prompt).
        Binding('dollar_sign', 'watch', 'watch', key_display='$'),
        Binding('dollar_sign', 'unwatch', 'unwatch', key_display='$'),
        Binding('t', 'track', 'track', show=False),
        # One key; the enabled action (and footer label) follows the item's state.
        Binding('x', 'hide', 'hide', show=False),
        Binding('x', 'unhide', 'unhide', show=False),
        Binding('f5', 'refresh', 'refresh', key_display='F5'),
        Binding('r', 'reply', 'reply'),
        Binding('R', 'reply_attach', 'reply + session output'),
        Binding('a', 'apply_series', 'apply', show=False),
        Binding('d', 'drop_series', 'drop', show=False),
        Binding('M', 'mark_read', 'read all', show=False),
        Binding('w', 'add_to_workbench', 'to workbench'),
        # The tabs (shown in the tab bar): F4 in a thread shows its sessions' output instead.
        Binding('f2', "tab('lkml')", 'LKML', key_display='F2', show=False),
        Binding('f3', "tab('workbench')", 'Workbench', key_display='F3', show=False),
        Binding('f4', 'artifacts', 'Branches', key_display='F4', show=False),
        Binding('question_mark', 'keys', 'keys', key_display='?'),
        Binding('f1', 'launch_session', 'session', key_display='F1'),
        Binding('f12', 'focus_session', 'to session', key_display='F12', show=False),
        Binding('grave_accent', 'toggle_drawer', 'drawer', key_display='`', show=False),
        Binding('Q', 'quit', 'quit'),
    ]

    def __init__(
        self,
        loader: Callable[[], Snapshot],
        sessions: Optional[SessionHub] = None,
        branches: Optional[Branches] = None,
        workbenches: Optional['WorkbenchService'] = None,
        breakpoints: Optional[Breakpoints] = None,
        symbols: Symbols = DEFAULT_SYMBOLS,
        thread_loader: Callable[[Series], SeriesThread] = load_thread,
        actions: Optional[SourceActions] = None,
        drafts: Optional[DraftStore] = None,
        editor: Optional[ExternalEditor] = None,
        sync: Optional[Callable[..., str]] = None,
        sync_timeout: float = 10.0,
        app_pane: str = '',
    ) -> None:
        super().__init__()
        self._loader = loader
        self._actions = actions
        # State sync with the state repository: at startup (background) and on quit (bounded).
        self._sync = sync
        self._sync_timeout = sync_timeout
        self._synced_at_start = False
        # Without a store, drafts live only while the reader is open.
        self._drafts = drafts
        # Drafts go to the maintainer's editor when there is one and a store for its file.
        self._editor = editor if drafts is not None else None
        self._save_timer: Any = None
        self._editor_timer: Any = None
        self._drafts_changed = False
        self._notice = ''
        self._query = compile_query('')
        self._matches: Optional[frozenset[str]] = None
        # Rebuilt per snapshot so each keystroke only runs the regex and set intersections.
        self._texts: list[tuple[str, str]] = []
        self._view_members: dict[str, frozenset[str]] = {}
        self._binding_state: Optional[tuple[Optional[bool], ...]] = None
        self._facts: Optional[tuple[SeriesThread, tuple[bool, bool]]] = None
        # Replies marked seen while reading; counts refresh when the reader closes.
        self._seen_now: set[str] = set()
        self._draft: Optional[Draft] = None
        # The open thread's saved draft, left closed until r (or discarded with ctrl+g).
        self._saved_draft: Optional[tuple[WorkItem, str]] = None
        self._resume_on_open: Optional[str] = None  # A draft to resume once its thread loads.
        # r in the series list: reply to the first posting once the opened thread loads.
        self._reply_on_open = False
        self._reload_reading = False  # F5 while reading: the open thread reloads too.
        self._thread_loader = thread_loader
        # Agent sessions: polled for the drawer; only the maintainer's keys act on them.
        self._sessions = sessions
        self._branches = branches  # The Branches screen's data and actions (live only).
        self._workbenches = workbenches  # F3's workbenches (live only).
        self._installed: set[str] = set()  # Tabs made so far (kept, with their place).
        self._overview = Overview()
        self._polling = False
        self._drawer_tab = 'sessions'
        self._session_rows: list[tuple[str, str]] = []
        self._worktree_rows: list[tuple[str, str]] = []
        self._marked_trees: set[str] = set()  # Worktrees tab: marked for removal.
        # The column beside the app: the app's own tmux pane ($TMUX_PANE; empty outside
        # tmux), the session we put beside it, and the workspace session chosen there.
        self._app_pane = app_pane
        self._beside: Optional[str] = None
        self._workspace_beside: Optional[str] = None
        # The Workbench tab's session in the column (only while that tab is shown).
        self._bench_beside: Optional[str] = None
        self._aside: Optional[str] = None  # Set aside while drafting on a short terminal.
        self._breakpoints = breakpoints
        self.symbols = symbols
        self.snapshot: Optional[Snapshot] = None
        self.index: Optional[ViewIndex] = None
        self.current_view = 'all'
        self._summary = 'Loading…'
        self._status = ' b4-maintainer — loading'
        self._drawer = drawer_line(Overview())
        self._mode: Mode = 'wide'
        self._preview = False
        self._item: Optional[WorkItem] = None
        self._reading: Optional[WorkItem] = None
        self._thread: Optional[SeriesThread] = None
        self._post_key = ''
        self._node = 0
        self._expanded: set[str] = set()
        # Replies (by message-id) whose branches are folded away in the tree.
        self._collapsed: set[str] = set()
        self._rows: list[TreeRow] = []
        self._finding = -1
        self._shown: Optional[tuple[str, int]] = None
        self._expand_quotes = False
        self._reader_pane = 'tree'
        self._artifacts: list[Attachment] = []  # The thread's session output (F3).

    def compose(self) -> ComposeResult:
        yield TabBar('lkml', id='tabs')
        yield StatusLine(self._status, id='status')
        with Horizontal(id='main'):
            yield OptionList(id='views')
            yield SeriesList(self.symbols, id='series')
            yield TextPane(self._summary, id='summary')
        with Horizontal(id='reader'):
            yield OptionList(id='tree')
            yield TextPane(id='content')
            with Vertical(id='artifacts'):
                yield OptionList(id='artifact-list')
                yield TextPane(id='artifact-show')
            yield TextArea(id='draft', soft_wrap=True, show_line_numbers=False)
        with Vertical(id='drawer-panel'):
            yield Static('', id='drawer-tabs', markup=False)
            yield DrawerList(id='drawer-sessions')
            yield WorktreeList(id='drawer-worktrees')
        yield Static(self._drawer, id='drawer', markup=False)
        yield Input(id='search', placeholder='/ regex over subject and author')
        yield KeyBar(id='footer')

    def on_mount(self) -> None:
        self._series_list = self.query_one(SeriesList)
        self._views = self.query_one('#views', OptionList)
        self._summary_pane = self.query_one('#summary', TextPane)
        self._views.border_title = 'Views'
        self._series_list.border_title = 'Series'
        self._summary_pane.border_title = 'Summary'
        self._tree = self.query_one('#tree', OptionList)
        self._content = self.query_one('#content', TextPane)
        self._tree.border_title = 'Thread'
        self._draft_area = self.query_one('#draft', TextArea)
        self._status_widget = self.query_one('#status', StatusLine)
        self._drawer_sessions = self.query_one('#drawer-sessions', DrawerList)
        self._drawer_worktrees = self.query_one('#drawer-worktrees', DrawerList)
        self._apply_layout(self.size.width)
        self._series_list.focus()
        self.run_worker(self._load, thread=True, name='load', exclusive=True)
        if self._sessions is not None:
            self.poll_sessions()
            self.set_interval(1.0, self.poll_sessions)
            # pi's model list, read now so F1 need not wait for pi to start (no agent runs).
            self.run_worker(self._sessions.read_catalog, thread=True, group='catalog')
            if self._app_pane:
                hub, pane = self._sessions, self._app_pane

                def settle_pane() -> None:
                    hub.announce(pane)  # Where sessions' F12 and /back return to.
                    hub.hide(pane)  # A session left beside a previous run goes back.

                self.run_worker(settle_pane, thread=True, group='session-pane')

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        # After the first load has finished, so the sync's reload cannot cancel it.
        done = event.worker.name == 'load' and event.state == WorkerState.SUCCESS
        if done and self._sync is not None and not self._synced_at_start:
            self._synced_at_start = True
            self.run_worker(self._startup_sync, thread=True, group='sync')

    def _startup_sync(self) -> None:
        """Others' changes arrive after the local view is up; a failure leaves it as is."""
        assert self._sync is not None
        try:
            result, reload = self._sync(), True
        except Exception as exc:  # Offline or refused: reported, local work goes on.
            result, reload = f'State sync failed: {exc}', False
        self.call_from_thread(self._action_done, result, reload)

    # Loading stays off the UI thread; results are applied on the event loop.
    def _load(self) -> None:
        try:
            snapshot = self._loader()
        except ValueError as exc:  # A bad workspace.toml: keep what is shown and say why.
            if self.snapshot is None:
                raise
            self.call_from_thread(self._set_notice, f'workspace.toml: {exc}')
            return
        index = ViewIndex(snapshot.workspace, snapshot.items)
        self.call_from_thread(self._apply_snapshot, snapshot, index)

    def _apply_snapshot(self, snapshot: Snapshot, index: ViewIndex) -> None:
        # A reload keeps the view and selection when they still exist.
        previous = self._series_list.highlighted_item if self.snapshot else None
        position = self._series_list.cursor
        view = self.current_view if any(v.id == self.current_view for v in index.views) else 'all'
        warned = self.snapshot.workspace.warnings if self.snapshot else ()
        self.snapshot = snapshot
        self.index = index
        if snapshot.workspace.warnings and snapshot.workspace.warnings != warned:
            self._notice = 'workspace.toml: ' + '; '.join(snapshot.workspace.warnings)
        if self._breakpoints is None:
            self._breakpoints = snapshot.workspace.breakpoints
            self._apply_layout(self.size.width)
        views = self._views
        views.clear_options()
        pool = index.items('cached')
        if any(v.id == 'hidden' for v in index.views):
            pool += index.items('hidden')
        self._texts = texts(pool)
        self._view_members = {
            v.id: frozenset(i.identity for i in index.items(v.id)) for v in index.views
        }
        self._matches = self._compute_matches()
        views.add_options(Option(self._view_prompt(v.id, v.label), id=v.id) for v in index.views)
        self.select_view(view, force=True)
        if previous is not None:
            self._series_list.select_identity(previous.identity, fallback=position)
        if self._reading is not None:
            # The open thread stays as it is; its item takes the reloaded state ($, unread).
            fresh = snapshot.item_by_identity(self._reading.identity)
            if fresh is not None:
                self._reading = fresh
            if self._reload_reading and self._draft is None:
                self._reload_thread(self._reading)  # After F5: new replies, same place.
            self._sync_bindings()
        self._reload_reading = False

    def _view_items(self, view_id: str) -> tuple[WorkItem, ...]:
        assert self.index is not None
        items = self.index.items(view_id)
        if self._matches is None:
            return items
        return tuple(i for i in items if i.identity in self._matches)

    def views_counts(self) -> dict[str, int]:
        assert self.index is not None
        return {v.id: self._view_count(v.id) for v in self.index.views}

    def _view_count(self, view_id: str) -> int:
        members = self._view_members.get(view_id, frozenset())
        return len(members) if self._matches is None else len(members & self._matches)

    def _view_prompt(self, view_id: str, label: str) -> str:
        # Titles and option prompts are markup; labels are configured text.
        return escape(f'{pad_display(label, 21)}{self._view_count(view_id):>5}')

    def select_view(self, view_id: str, force: bool = False) -> None:
        if self.index is None or (view_id == self.current_view and not force):
            return
        self.current_view = view_id
        multi = len(self.index.workspace.projects) > 1
        project_width = 5 if multi and not view_id.startswith('project:') else 0
        self._series_list.set_items(self._view_items(view_id), project_width)
        views = self._views
        position = views.get_option_index(view_id)
        if views.highlighted != position:
            views.highlighted = position
        self._update_status()

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if isinstance(event.option_list, DrawerList):
            self._sync_bindings()
        elif event.option_list.id == 'tree':
            self._show_row(event.option_index)
        elif event.option_list.id == 'artifact-list':
            self._preview_artifact(event.option_index)
        elif event.option_id:
            self.select_view(event.option_id)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        # Enter descends from the tree to the content.
        if event.option_list.id == 'tree':
            self._set_reader_pane('content')

    def on_series_list_highlighted(self, event: SeriesList.Highlighted) -> None:
        self._item = event.item
        self._show_summary()
        if not self.screen_stack:
            return  # Highlights can still arrive during shutdown.
        self._sync_bindings()

    def _thread_facts(self) -> tuple[bool, bool]:
        """(any replies, any Sashiko findings) of the open thread, computed once per thread."""
        thread, item = self._thread, self._reading
        if thread is None or item is None:
            return False, False
        cached = self._facts
        if cached is None or cached[0] is not thread:
            replies = any(thread_replies(thread, k) for k in post_keys(thread))
            cached = (thread, (replies, bool(finding_anchors(thread, item))))
            self._facts = cached
        return cached[1]

    def _sync_bindings(self) -> None:
        """Refresh the footer only when some key's enabled state changed: rebuilding is costly."""
        state = tuple(
            self.check_action(name, ()) for name in dict.fromkeys(_action_names(self.BINDINGS))
        )
        if state != self._binding_state:
            self._binding_state = state
            self.refresh_bindings()

    def _show_summary(self) -> None:
        self._summary = self._summary_for(self._item)
        # Sashiko lines are coloured by severity, as in the reader.
        styles = dict(self._sashiko_lines(self._item)) if self._item else {}
        self._summary_pane.set_blocks(
            [Block(self._summary, style=lambda line: styles.get(line) or Style())]
        )

    def on_resize(self, event: events.Resize) -> None:
        self._apply_layout(event.size.width)

    def _apply_layout(self, width: int) -> None:
        previous = self._mode
        self._mode = layout_mode(width, self._breakpoints or Breakpoints())
        for name in ('wide', 'medium', 'narrow'):
            self.screen.set_class(self._mode == name, f'-{name}')
        self._apply_preview_width()
        if self._reading is not None and self._mode != previous:
            self._set_reader_pane('tree' if self._mode == 'narrow' else self._reader_pane)
        self.refresh_bindings()

    def _apply_preview_width(self) -> None:
        """Expanded preview gets preview_width text columns; narrow mode is full width."""
        pane = getattr(self, '_summary_pane', None)
        if pane is None:
            return  # Resize can arrive before mount; on_mount reapplies the layout.
        if self._preview and self._mode != 'narrow':
            chrome = pane.styles.gutter.width + pane.styles.scrollbar_size_vertical
            width = (self._breakpoints or Breakpoints()).preview_width
            pane.styles.width = width + chrome
        else:
            pane.styles.width = None

    def check_action(self, action: str, parameters: tuple[object, ...]) -> Optional[bool]:
        """Hide keys that would do nothing in the current layout."""
        if isinstance(self.screen, LaunchDialog):
            return action == 'cycle_pane'  # Tab moves between the dialog's sections.
        if isinstance(self.screen, ModalScreen):
            return False  # A prompt or dialog owns every key while it is open.
        if action == 'keys':
            return not isinstance(self.screen, ModalScreen)
        if action in ('tab', 'artifacts') and not isinstance(self.screen, ModalScreen):
            on_lkml = len(self.screen_stack) == 1
            if action == 'artifacts' and self._reading is not None and on_lkml:
                return self._sessions is not None  # The thread's session output.
            if parameters == ('workbench',):
                return self._workbenches is not None and self.snapshot is not None
            if action == 'artifacts' or parameters == ('branches',):
                return self._branches is not None and self.snapshot is not None
            return True
        if isinstance(self.screen, WorkbenchScreen) and action == 'focus_session':
            return bool(self._app_pane) and self._beside is not None
        if isinstance(self.screen, WorkbenchScreen) and action == 'send_draft':
            return self._draft is not None  # A reply begun from a thread there.
        if isinstance(self.screen, WorkbenchScreen) and action == 'discard_draft':
            return self._draft is not None or self._saved_draft is not None
        if isinstance(self.screen, WorkbenchScreen) and action == 'focus_editor':
            return self._draft is not None and bool(self._draft.pane)
        if isinstance(self.screen, (BranchesScreen, WorkbenchScreen)):
            return action == 'quit'  # The screen has its own keys.
        if action == 'focus_session':
            return bool(self._app_pane) and self._beside is not None
        if action == 'launch_session':
            return self._sessions is not None and not self._drawer_focused()
        if action in WORKTREE_ACTIONS:
            return self._selected_worktree() is not None
        if action in SESSION_ACTIONS:
            if self.focused is self._drawer_worktrees:
                return False
            if self.selected_draft() is not None:  # Enter resumes it, x discards it.
                return action in ('open_session', 'end_session')
            view = self._selected_view()
            if view is not None and view.state == 'preparing':
                return action == 'session_artifacts'  # Nothing to act on until it starts.
            return view is not None and (action != 'open_session' or view.state != 'ended')
        if self._drawer_focused():
            return action in DRAWER_ACTIONS  # Series keys wait until focus leaves the drawer.
        narrow = self._mode == 'narrow'
        summary_open = self.screen.has_class('-show-summary')
        reading = self._reading is not None
        if action == 'toggle_views':
            return not reading and self._mode == 'medium'
        if action == 'cycle_pane':
            return not narrow or (reading and self._draft is not None)
        if action == 'send_draft':
            return self._draft is not None
        if action == 'discard_draft':
            return self._draft is not None or self._saved_draft is not None
        if action == 'focus_editor':
            return self._draft is not None and bool(self._draft.pane)
        if action == 'open_reader':
            return not reading and self._item is not None
        if action == 'back':
            searching = self.screen.has_class('-searching') or self._query.pattern is not None
            return reading or (narrow and summary_open) or searching
        if action == 'search':
            return not reading
        if action == 'toggle_preview':
            return not reading
        if action == 'refresh':
            return self._actions is not None
        if action == 'reply':
            if self._actions is None or self._draft is not None:
                return False
            # In the series list: the selected series' cover letter (or first patch).
            return self._reply_msgid() is not None if reading else self._item is not None
        if action == 'reply_attach':
            if self._actions is None or self._draft is not None or self._sessions is None:
                return False
            if self._saved_draft is not None and reading:
                return True
            return self._reply_msgid() is not None if reading else self._item is not None
        if action == 'add_to_workbench':
            item = self._reading or self._item
            return self._workbenches is not None and item is not None
        if action in ('apply_series', 'drop_series'):
            item = self._reading or self._item
            return self._branches is not None and item is not None and not item.series.discussion
        if action == 'mark_read':
            return self._actions is not None and (self._reading or self._item) is not None
        if action in ('hide', 'unhide'):
            item = self._item
            if self._actions is None or reading or item is None:
                return False
            return item.hidden == (action == 'unhide')
        if action in ('track', 'untrack', 'watch', 'unwatch'):
            item = self._reading or self._item  # The open thread while reading.
            if self._actions is None or item is None:
                return False
            if item.series.discussion != (action in ('watch', 'unwatch')):
                return False
            on = item.watched if item.series.discussion else item.state.to_review
            return on == (action in ('untrack', 'unwatch'))
        if action == 'next_finding':
            return reading and self._thread is not None and self._thread_facts()[1]
        if action in ('expand_node', 'collapse_node'):
            return reading and self._thread is not None and self.focused is self._tree
        if action == 'toggle_all':
            return reading and self._thread is not None and self._thread_facts()[0]
        if action in ('next_post', 'next_comment', 'toggle_quotes', 'toggle_replies'):
            if not reading or self._thread is None:
                return False
            if action == 'next_post':
                return len(post_keys(self._thread)) > 1
            if action == 'toggle_replies':
                return bool(self._post_key and thread_replies(self._thread, self._post_key))
            if action == 'next_comment':
                return self._thread_facts()[0]
            reply = self._current_reply()
            return reply is not None and has_long_quote(reply)
        return True

    # Text accessors for tests and status rendering.
    def summary_text(self) -> str:
        return self._summary

    def status_text(self) -> str:
        return self._status

    def drawer_text(self) -> str:
        return self._drawer

    def reader_panes(self) -> tuple[str, ...]:
        if self._reading is None:
            return ()
        return tuple(
            name
            for name in ('tree', 'content', 'artifacts', 'draft')
            if self.query_one(f'#{name}').display
        )

    def artifacts_shown(self) -> bool:
        return self.screen.has_class('-artifacts')

    def artifact_labels(self) -> list[str]:
        return [a.label for a in self._artifacts]

    def artifact_text(self) -> str:
        return self.query_one('#artifact-show', TextPane).text

    def visible_panes(self) -> tuple[str, ...]:
        if not self.query_one('#main').display:
            return ()
        return tuple(
            name for name in ('views', 'series', 'summary') if self.query_one(f'#{name}').display
        )

    def action_toggle_views(self) -> None:
        self.screen.toggle_class('-show-views')

    def action_open_summary(self) -> None:
        self.screen.add_class('-show-summary')
        self._summary_pane.focus()
        self.refresh_bindings()

    def action_search(self) -> None:
        search = self.query_one('#search', Input)
        self.screen.add_class('-searching')
        search.value = self._query.text
        search.focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == 'search':
            self._apply_query(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == 'search':
            self._close_search()

    def _compute_matches(self) -> Optional[frozenset[str]]:
        """Identities matching the search, over every item including hidden ones."""
        if self._query.pattern is None:
            return None
        return match_texts(self._texts, self._query)

    def _close_search(self) -> None:
        self.screen.remove_class('-searching')
        self._series_list.focus()
        self.refresh_bindings()

    def _apply_query(self, text: str) -> None:
        if self.index is None or text == self._query.text:
            return
        self._query = compile_query(text)
        self._matches = self._compute_matches()
        previous = self._series_list.highlighted_item
        for n, view in enumerate(self.index.views):
            self._views.replace_option_prompt_at_index(n, self._view_prompt(view.id, view.label))
        self.select_view(self.current_view, force=True)
        if previous is not None:
            self._series_list.select_identity(previous.identity)

    def action_back(self) -> None:
        if self._drawer_focused():
            self._focus_main()
            return
        if self.screen.has_class('-searching'):
            self.query_one('#search', Input).value = ''
            self._apply_query('')
            self._close_search()
            return
        if self._reading is None and self._query.pattern is not None:
            self._apply_query('')
            self.refresh_bindings()
            return
        if self._reading is not None and self.artifacts_shown():
            self._hide_artifacts()  # Back to the thread/patch content.
            return
        if self._reading is not None:
            # From the content or draft, Escape returns to the tree; from the tree, it closes.
            in_pane = self._reader_pane in ('content', 'draft') or self.focused in (
                self._content,
                self._draft_area,
            )
            if in_pane:
                self._set_reader_pane('tree')
            elif self._draft is not None and self._draft.pane:
                self._close_reader()
                self._save_editor_draft(self._draft)
            elif self._draft is not None and self._drafts is not None:
                subject = self._reading.series.subject
                self._close_draft(keep=True)
                self._close_reader()
                self._set_notice(f'Draft saved: {subject}')
            elif self._draft is not None:
                self._confirm_discard(then=self._close_reader)
            else:
                self._close_reader()
            return
        self.screen.remove_class('-show-summary')
        self._series_list.focus()
        self.refresh_bindings()

    # Reader: the thread loads in a worker; the drawer and status stay visible.
    @property
    def reading(self) -> Optional[WorkItem]:
        return self._reading

    def tree_labels(self) -> list[str]:
        return [
            Content.from_markup(str(self._tree.get_option_at_index(i).prompt)).plain
            for i in range(self._tree.option_count)
        ]

    def tree_position(self) -> tuple[str, int]:
        """(posting key, node) of the highlighted tree row; node 0 is the posting."""
        return self._post_key, self._node

    def content_text(self) -> str:
        return self._content.text

    def content_lines(self) -> list[str]:
        return self._content.lines

    def draft_msgid(self) -> Optional[str]:
        """Message ID the open draft answers, if a draft is open."""
        return self._draft.msgid if self._draft else None

    def _set_reader_pane(self, pane: str) -> None:
        self._mark_reader_pane(pane)
        content = self.query_one('#artifact-list') if self.artifacts_shown() else self._content
        {'tree': self._tree, 'content': content, 'draft': self._draft_area}[pane].focus()
        self.refresh_bindings()

    def _mark_reader_pane(self, pane: str) -> None:
        self._reader_pane = pane
        for name in ('tree', 'content', 'draft'):
            self.screen.set_class(name == pane, f'-rpane-{name}')

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        # Tab and clicks move focus too; Escape steps back from the pane actually focused.
        if self._reading is None:
            return
        panes = {self._tree: 'tree', self._content: 'content', self._draft_area: 'draft'}
        panes[self.query_one('#artifact-list')] = 'content'  # In place of the content (F3).
        pane = panes.get(event.widget)
        if pane is not None and pane != self._reader_pane:
            self._mark_reader_pane(pane)

    def action_open_reader(self) -> None:
        self._open_reader()

    def _open_reader(self, pane: bool = True) -> None:
        item = self._item
        if item is None:
            return
        self._reading, self._thread, self._post_key = item, None, ''
        self._reply_on_open = False
        self._saved_draft = None
        self.screen.add_class('-reading')
        self._tree.clear_options()
        self._rows, self._expanded, self._node = [], set(), 0
        self._collapsed = set()
        self._finding = -1
        self._shown = None
        self._content.border_title = ''
        self._content.set_text('Loading thread…')
        self._set_status(f' b4-maintainer — Reading: {item.series.subject} v{item.series.revision}')
        self._set_reader_pane('tree')
        self.run_worker(
            lambda: self._load_thread(item), thread=True, group='thread', exclusive=True
        )
        if pane:
            self._place_pane()

    def _load_thread(self, item: WorkItem) -> None:
        restore, adopt = None, False
        try:
            thread = self._with_sashiko(item, self._thread_loader(item.series))
            error = None
            if self._drafts is not None:
                restore = _drafted_in(thread, self._drafts.msgids())
            if restore is not None and self._drafts is not None and self._editor is not None:
                # Its editor is still open (e.g. from a crashed run): adopt it on entry.
                saved = self._drafts.get(restore[2])
                path = self._drafts.text_path(restore[2])
                adopt = bool(saved and saved.pane and self._editor.runs(saved.pane, path))
        except Exception as exc:  # Shown to the maintainer; mail is untrusted input.
            thread, error = None, f'{type(exc).__name__}: {exc}'
        self.call_from_thread(self._apply_thread, item, thread, error, restore, adopt)

    def _reload_thread(self, item: WorkItem) -> None:
        """The open thread loaded again (after a refresh), keeping the highlighted row and
        what was expanded or collapsed."""
        index = self._tree.highlighted
        row = self._rows[index] if index is not None and index < len(self._rows) else None
        place = (row.key, row.node) if row is not None else ('', 0)
        expanded, collapsed = set(self._expanded), set(self._collapsed)

        def work() -> None:
            try:
                thread = self._with_sashiko(item, self._thread_loader(item.series))
            except Exception as exc:  # Shown; the thread already open stays.
                self.call_from_thread(self._set_notice, f'Could not reload the thread: {exc}')
                return
            self.call_from_thread(self._replace_thread, item, thread, place, expanded, collapsed)

        self.run_worker(work, thread=True, group='thread')

    def _replace_thread(
        self,
        item: WorkItem,
        thread: SeriesThread,
        place: tuple[str, int],
        expanded: set[str],
        collapsed: set[str],
    ) -> None:
        if not self._still_reading(item) or self._draft is not None:
            return
        self._thread, self._shown = thread, None
        self._expanded = expanded | initially_expanded(thread, self._unread())
        self._collapsed = collapsed
        self._render_tree(place)

    def _apply_thread(
        self,
        item: WorkItem,
        thread: Optional[SeriesThread],
        error: Optional[str],
        restore: Optional[tuple[str, int, str]] = None,
        adopt: bool = False,
    ) -> None:
        if not self._still_reading(item):
            return  # Closed or reopened on another series while loading.
        # A draft already open or saved for this thread takes precedence: one at a time.
        reply_now, self._reply_on_open = self._reply_on_open, False
        if thread is None:
            self._content.set_text(f'Could not load thread: {error}')
            return
        self._thread, self._shown = thread, None
        self._expanded = initially_expanded(thread, self._unread())
        self._collapsed = initially_collapsed(thread, self._unread())
        keys = post_keys(thread)
        draft = self._draft
        if draft is not None and draft.pane and draft.item.identity == item.identity:
            # Its draft is still in the editor: back to the message, and to the editor.
            found = _drafted_in(thread, {draft.msgid})
            key, node = found[:2] if found else ((keys[0], 0) if keys else ('', 0))
            if node:
                self._expanded.add(key)
            self._render_tree((key, node))
            self.action_focus_editor()
            return
        resume, self._resume_on_open = self._resume_on_open, None
        if resume is not None and self._actions is not None and self._draft is None:
            self._render_tree((keys[0], 0) if keys else ('', 0))
            self._begin_reply(item, resume, None)  # Chosen in the drawer.
            return
        if restore is not None and self._actions is not None and self._draft is None:
            # A saved draft: back to the message it answers. It stays closed (it may have
            # been abandoned) until r resumes it; ctrl+g discards it.
            key, node, msgid = restore
            if node:
                self._expanded.add(key)
            self._render_tree((key, node))
            self._saved_draft = (item, msgid)
            if reply_now == 'attach':
                self.action_reply_attach()
            elif reply_now or adopt:
                self._resume_saved_draft()
            else:
                self._set_notice('Saved draft here · r resumes · ctrl+g discards')
                self._sync_bindings()
            return
        self._render_tree((keys[0], 0) if keys else ('', 0))
        if reply_now == 'attach':
            self.action_reply_attach()
        elif reply_now:
            self.action_reply()  # On the first posting: the cover letter, else the first patch.

    def _with_sashiko(self, item: WorkItem, thread: SeriesThread) -> SeriesThread:
        """The thread with Sashiko's API reviews joined, and (unless configured otherwise)
        its replies that found nothing left out."""
        merged = with_sashiko(thread, item.sashiko, self._bots())
        if self.snapshot is not None and not self.snapshot.workspace.sashiko_hide_clean:
            return merged
        return without_clean_sashiko(merged, item.sashiko, self._bots())

    def _bots(self) -> tuple[str, ...]:
        return self.snapshot.workspace.bots if self.snapshot else ()

    def _tree_rows(self) -> list[TreeRow]:
        assert self._thread is not None and self._reading is not None
        return tree_rows(
            self._thread,
            self._reading,
            self._expanded,
            self._unread(),
            self.symbols.absent,
            self._bots(),
            self._collapsed,
        )

    def _render_tree(self, target: tuple[str, int]) -> None:
        """Rebuild the tree and highlight the row for *target* (or its posting)."""
        self._rows = self._tree_rows()
        width = max(20, self._tree.scrollable_content_region.width or 54)
        self._tree.clear_options()
        self._tree.add_options(
            Option(escape(pad_display(row.label, width).rstrip())) for row in self._rows
        )
        positions = {(row.key, row.node): n for n, row in enumerate(self._rows)}
        index = positions.get(target, positions.get((target[0], 0), 0))
        if self._rows:
            self._tree.highlighted = index
            self._show_row(index)

    def _refresh_tree_labels(self) -> None:
        """Update labels in place (e.g. unread marks) without moving the cursor."""
        width = max(20, self._tree.scrollable_content_region.width or 54)
        for n, (old, new) in enumerate(zip(self._rows, self._tree_rows())):
            if old.label != new.label:
                self._tree.replace_option_prompt_at_index(
                    n, escape(pad_display(new.label, width).rstrip())
                )
                self._rows[n] = new

    def _reply_msgid(self) -> Optional[str]:
        """Message ID of what the reader shows: the selected reply, or the posting itself."""
        if self._reading is None or self._thread is None or not self._post_key:
            return None
        reply = self._current_reply()
        if reply is not None:
            return reply.message.msgid
        post = find_post(self._thread, self._post_key)
        return post.message.msgid if post is not None else None

    def action_reply(self) -> None:
        if self._saved_draft is not None and self._reading is not None:
            self._resume_saved_draft()
            return
        if self._reading is None:
            # Open the series; the reply starts on its first posting once the thread loads.
            self.action_open_reader()
            self._reply_on_open = self._reading is not None
            return
        found = self._reply_args()
        if found is not None:
            self._begin_reply(*found)

    def _reply_args(self) -> Optional[tuple[WorkItem, str, Optional[str]]]:
        """(series, message to answer, starting quote) for a reply in the reader."""
        actions, item, msgid = self._actions, self._reading, self._reply_msgid()
        if actions is None or item is None or msgid is None:
            return None
        quote = None
        if msgid.startswith(SASHIKO_PREFIX):
            # Not on the list: answer the patch, quoting Sashiko's review.
            reply = self._current_reply()
            assert reply is not None
            source = item.sashiko.url if item.sashiko and item.sashiko.url else 'sashiko.dev'
            quoted = '\n'.join(f'> {line}'.rstrip() for line in reply.message.body.splitlines())
            quote = f'Sashiko ({source}) reported:\n{quoted}\n\n'
            msgid = msgid.removeprefix(SASHIKO_PREFIX)
        return item, msgid, quote

    def action_reply_attach(self) -> None:
        """R: a reply with chosen session output (patches, results) at its end: pick from
        what this series' sessions produced, then write as usual. A saved draft gets
        them appended; an open one must be finished first."""
        hub, snapshot = self._sessions, self.snapshot
        if hub is None or snapshot is None:
            return
        if self._draft is not None:
            self._set_notice('Finish or close the open draft first (R adds to a saved one)')
            return
        if self._reading is None:
            self.action_open_reader()
            self._reply_on_open = 'attach' if self._reading is not None else False
            return
        if self._saved_draft is not None:
            item, msgid = self._saved_draft
            found: Optional[tuple[WorkItem, str, Optional[str]]] = (item, msgid, None)
        else:
            found = self._reply_args()
        if found is None:
            return
        self._reply_with_output(*found)

    def _reply_with_output(self, item: WorkItem, msgid: str, quote: Optional[str]) -> None:
        """Pick session output for a reply to *msgid*, then write it (off the UI thread)."""
        hub = self._sessions
        if hub is None:
            self._begin_reply(item, msgid, quote)
            return

        def work() -> None:
            try:
                records = hub.records()
                offered = self._offered_output(records, item)
            except Exception as exc:
                self.call_from_thread(self._set_notice, f'Cannot list session output: {exc}')
                return
            models = {r.id: r.model or r.preset or 'LLM' for r in records}
            self.call_from_thread(self._pick_attachments, item, msgid, quote, offered, models)

        self.run_worker(work, thread=True, group='session-action')

    def bench_fetch(self, target: str) -> WorkItem:
        """A thread fetched into the cache by a lore link or Message-ID, the snapshot
        reloaded with it (blocking): its work item."""
        fetch = getattr(self._actions, 'fetch_thread', None)
        if fetch is None:
            raise RuntimeError('fetching needs a live workspace')
        msgid = fetch(target)
        snapshot = self._loader()
        self.call_from_thread(
            self._apply_snapshot, snapshot, ViewIndex(snapshot.workspace, snapshot.items)
        )
        found = next(
            (i for i in snapshot.items if msgid in i.series.msgids or i.identity == msgid), None
        )
        if found is None:
            raise ValueError(f'lore has no thread for <{msgid}>')
        return found

    def bench_thread(self, identity: str) -> tuple[WorkItem, SeriesThread]:
        """A series' thread for the Workbench tab (blocking)."""
        item = self.snapshot.item_by_identity(identity) if self.snapshot else None
        if item is None:
            raise ValueError('not in the cache (F5 fetches it)')
        return item, self._with_sashiko(item, self._thread_loader(item.series))

    def reply_from_bench(self, item: WorkItem, msgid: str, attach: bool) -> None:
        """r/R in a thread opened on the Workbench tab: with the tmux editor the draft
        opens beside the app and the tab stays; otherwise the reply opens on LKML, whose
        reader holds the draft."""
        if self._actions is None:
            self._set_notice('Replies need a live workspace')
            return
        if self._draft is not None:
            self._set_notice('Finish or close the open draft first')
            return
        if self._editor is not None:
            self._start_bench_reply(item, msgid, attach)
            return
        self.action_tab('lkml')

        def on_lkml() -> None:
            self._series_list.select_identity(item.identity)
            self._item = item
            self._open_reader(pane=False)
            self._start_bench_reply(item, msgid, attach)

        self.call_after_refresh(on_lkml)

    def _start_bench_reply(self, item: WorkItem, msgid: str, attach: bool) -> None:
        if attach:
            self._reply_with_output(item, msgid, None)
        else:
            self._begin_reply(item, msgid, None)

    def bench_artifacts(self, bench_id: str) -> list[tuple[Attachment, str, Item]]:
        """What `a` on the Workbench offers from sessions (blocking): each artifact that can
        be an item, with its project ('' to ask) and the item; other workbenches' sessions
        first (labelled with theirs), then unfiled ones, then this workbench's own."""
        hub, service = self._sessions, self._workbenches
        if hub is None:
            return []
        labels = {b.id: b.label for b in service.store.list()[0]} if service is not None else {}
        newest = sorted(hub.records(), key=lambda r: r.created, reverse=True)
        ordered = [r for r in newest if r.workbench and r.workbench != bench_id]
        ordered += [r for r in newest if not r.workbench]
        ordered += [r for r in newest if r.workbench == bench_id]
        tags = {r.id: f', {labels.get(r.workbench, r.workbench) or "unfiled"}' for r in ordered}
        by_id = {r.id: r for r in ordered}
        found = []
        for a in gather_all(ordered, hub.store.directory, tags):
            mapped = artifact_item(by_id[a.session], a)
            if mapped is not None:
                found.append((a, *mapped))
        return found

    def _offered_output(self, records: list[SessionRecord], item: WorkItem) -> list[Attachment]:
        """What R offers (blocking): every session's output, the series' own first, then
        that of the workbenches holding the series (labelled with the workbench), then
        the rest; newest first within each."""
        hub, service = self._sessions, self._workbenches
        if hub is None:
            return []
        benches = service.store.list()[0] if service is not None else []
        labels = {b.id: b.label for b in benches}
        holding = {
            b.id for b in benches for e in b.entries for i in e.items if i.series == item.identity
        }
        newest = sorted(records, key=lambda r: r.created, reverse=True)
        own = [r for r in newest if r.series == item.identity]
        near = [r for r in newest if r.workbench in holding and r not in own]
        rest = [r for r in newest if r not in own and r not in near]
        tags = {r.id: f', {labels[r.workbench]}' for r in newest if r.workbench in labels}
        return gather_all([*own, *near, *rest], hub.store.directory, tags)

    def _pick_attachments(
        self,
        item: WorkItem,
        msgid: str,
        quote: Optional[str],
        offered: list[Attachment],
        models: dict[str, str],
    ) -> None:
        if not offered:
            self._set_notice('No session output yet; r replies without it')
            return
        workspace = self.snapshot.workspace if self.snapshot else Workspace()

        def chosen(picked: Optional[list[Attachment]]) -> None:
            if picked is None:
                return
            if not picked:
                self._begin_reply(item, msgid, quote)
                return
            if self._saved_draft is not None and self._saved_draft[1] == msgid:
                self._saved_draft = None

            def extra(target: Any) -> str:
                me = git_identity(pathlib.Path(target.checkout))
                assisted = {
                    sid: workspace.assisted_by.replace('{model}', model)
                    for sid, model in models.items()
                }
                return render(picked, workspace.agent_email, assisted, me)

            self._begin_reply(item, msgid, quote, extra)

        self.push_screen(AttachDialog(offered), chosen)

    def _resume_saved_draft(self) -> None:
        saved, self._saved_draft = self._saved_draft, None
        if saved is not None:
            self._begin_reply(saved[0], saved[1], None)

    def _begin_reply(
        self,
        item: WorkItem,
        msgid: str,
        quote: Optional[str],
        extra: Optional[Callable[[Any], str]] = None,
    ) -> None:
        """Load the message to answer (and any saved draft) off the UI thread; *extra*
        (given the reply target) is text appended to the draft, new or saved."""
        actions, drafts = self._actions, self._drafts
        if actions is None:
            return

        editor = self._editor
        # With a session beside the app, the editor stacks under it; on a short terminal
        # the session steps aside (still running) until the draft is done.
        hub, app_pane, beside = self._sessions, self._app_pane, self._beside
        width = self.snapshot.workspace.pane_width if self.snapshot else 90
        stack = editor is not None and hub is not None and bool(app_pane) and beside is not None
        aside = stack and self.size.height < 40
        if aside:
            self._aside, self._beside = beside, None

        def work() -> None:
            try:
                target = actions.prepare_reply(item, msgid)
                saved = drafts.get(msgid) if drafts is not None else None
                added = extra(target) if extra is not None else ''
            except Exception as exc:
                self.call_from_thread(self._set_notice, f'Cannot reply: {exc}')
                return
            if added and drafts is not None:
                start = saved.quote if saved else (quote or reply_quote(target.lmsg))
                text = saved.text if saved else start
                drafts.save(msgid, start, text.rstrip('\n') + '\n\n' + added)
                saved, added = drafts.get(msgid), ''  # Now in the saved draft.
            below = ''
            if stack and hub is not None and beside is not None:
                try:
                    if aside:
                        hub.hide(app_pane)
                    else:
                        # Beside the app first (it may still be joining, or moved by hand),
                        # so the editor can never land in the session's own window.
                        hub.show(beside, app_pane, width)
                        below = hub.pane_of(beside) or ''
                except Exception:
                    below = ''
            if editor is not None and drafts is not None:
                try:
                    pane, start = self._open_editor(
                        editor, drafts, msgid, target, quote, saved, below
                    )
                except Exception as exc:  # tmux trouble: the in-app pane still works
                    note = f'Editor unavailable ({exc}); drafting in the app'
                    self.call_from_thread(self._set_notice, note)
                    saved = drafts.get(msgid)
                else:
                    self.call_from_thread(self._editor_opened, item, msgid, target, start, pane)
                    return
            self.call_from_thread(self._compose_reply, item, msgid, target, quote, saved, added)

        self.run_worker(work, thread=True, group='action', exclusive=True)

    @staticmethod
    def _open_editor(
        editor: ExternalEditor,
        drafts: DraftStore,
        msgid: str,
        target: Any,
        quote: Optional[str],
        saved: Optional[SavedDraft],
        below: str = '',
    ) -> tuple[str, str]:
        """(pane, starting quote): focus a live pane on the draft file, else open one."""
        start = saved.quote if saved else (quote or reply_quote(target.lmsg))
        if saved is None:
            drafts.save(msgid, start, start)
        path = drafts.text_path(msgid)
        if saved is not None and saved.pane and editor.runs(saved.pane, path):
            editor.focus(saved.pane)  # Still editing, perhaps from a crashed session.
            return saved.pane, start
        pane = editor.open(path, pathlib.Path(target.checkout), below)
        drafts.set_pane(msgid, pane)
        return pane, start

    def _editor_opened(
        self, item: WorkItem, msgid: str, target: Any, quote: str, pane: str
    ) -> None:
        if self._draft is not None:
            return
        draft = Draft(item, msgid, target, quote, pane)
        self._draft = draft
        self._set_notice(
            'Drafting in your editor (tmux pane) · e editor · ctrl+s send · ctrl+g discard'
        )
        self.refresh_bindings()
        # Poll the pane about once a second; each check is a short worker.
        if self._editor_timer is not None:
            self._editor_timer.stop()
        self._editor_timer = self.set_interval(1.0, lambda: self._check_editor(draft))

    def _check_editor(self, draft: Draft) -> None:
        editor = self._editor
        if self._draft is not draft or editor is None:
            if self._editor_timer is not None:
                self._editor_timer.stop()
                self._editor_timer = None
            return

        def check() -> None:
            if not editor.alive(draft.pane):
                self.call_from_thread(self._editor_exited, draft)

        self.run_worker(check, thread=True, group='editor-watch', exclusive=True)

    def _editor_exited(self, draft: Draft) -> None:
        """The editor closed (:wq, :q, or a killed pane): preview what it wrote."""
        if self._draft is not draft or self._drafts is None:
            return
        self._draft = None
        self.refresh_bindings()
        if self._aside is not None:  # The session set aside for the draft comes back.
            sid, self._aside = self._aside, None
            self._place_pane(sid)
        saved = self._drafts.get(draft.msgid)
        self._drafts.set_pane(draft.msgid, '')
        self._drafts_changed = True
        if saved is None or saved.text == draft.quote:
            self._drafts.delete(draft.msgid)
            self._set_notice('Draft dropped: nothing written')
            return
        if draft.closing:  # Saved on closing the thread: no prompt.
            self._set_notice(f'Draft saved: {draft.item.series.subject}')
            return
        self._set_notice('Draft kept; r reopens it')
        preview_and_send(
            self,
            draft.target.lmsg,
            saved.text,
            email_dryrun=draft.target.email_dryrun,
            patatt_sign=draft.target.patatt_sign,
            on_sent=lambda _msg: self._draft_sent(draft),
            on_edit=lambda _text: self._begin_reply(draft.item, draft.msgid, None),
            on_save=lambda _text: self._set_notice('Draft saved; r resumes it'),
            on_abandon=lambda: self._draft_abandoned(draft),
        )

    def _save_editor_draft(self, draft: Draft) -> None:
        """Closing the thread saves a draft written in vim (and closes its pane); other
        editors stay open."""
        editor = self._editor
        draft.closing = True

        def work() -> None:
            try:
                saving = editor is not None and editor.save_and_close(draft.pane)
            except Exception:
                saving = False
            self.call_from_thread(self._editor_saving, draft, saving)

        self.run_worker(work, thread=True, group='editor')

    def _editor_saving(self, draft: Draft, saving: bool) -> None:
        if not saving:
            draft.closing = False
            self._set_notice('Draft still open in your editor')

    def _open_draft(self, msgid: str) -> None:
        """Enter on a draft in the drawer: resume it (its editor, if still open)."""
        draft = self._draft
        if draft is not None and draft.msgid == msgid:
            if draft.pane:
                self.action_focus_editor()
            return
        if self._reading is not None:
            self._saved_draft = None
            self._begin_reply(self._reading, msgid, None)
            return
        item = self._item
        if item is None:
            return
        self._resume_on_open = msgid
        self._open_reader()

    def _discard_draft(self, msgid: str) -> None:
        """x on a draft in the drawer: delete it after confirmation."""
        draft, drafts = self._draft, self._drafts
        if draft is not None and draft.msgid == msgid:
            self._confirm_discard(then=None)
            return
        if drafts is None:
            return

        def answered(confirmed: Optional[bool]) -> None:
            if confirmed:
                drafts.delete(msgid)
                self._drafts_changed = True
                if self._saved_draft is not None and self._saved_draft[1] == msgid:
                    self._saved_draft = None
                self._set_notice('Draft discarded')
                self._render_session_rows()

        self.push_screen(Confirm('Discard this saved draft? [y|N]'), answered)

    def _draft_abandoned(self, draft: Draft) -> None:
        """t in b4's preview: the draft is discarded for good."""
        if self._drafts is not None:
            self._drafts.delete(draft.msgid)
            self._drafts_changed = True
        self._set_notice('Draft abandoned')
        if self._reading is not None:
            self._refresh_tree_labels()

    def action_focus_editor(self) -> None:
        draft, editor = self._draft, self._editor
        if draft is None or not draft.pane or editor is None:
            return
        pane = draft.pane

        def work() -> None:
            try:
                editor.focus(pane)
            except Exception as exc:
                self.call_from_thread(self._set_notice, f'Cannot focus the editor: {exc}')

        self.run_worker(work, thread=True, group='editor')

    def _compose_reply(
        self,
        item: WorkItem,
        msgid: str,
        target: Any,
        quote: Optional[str] = None,
        saved: Optional[SavedDraft] = None,
        added: str = '',
    ) -> None:
        """Open a draft beside the thread: a saved one, else b4's quote of the message;
        *added* (session output, when no draft store holds it) goes at its end."""
        if not self._still_reading(item):
            return  # The reader closed while the message loaded.
        quote = reply_quote(target.lmsg) if quote is None else quote
        if saved is not None:
            quote = saved.quote
        self._draft = Draft(item, msgid, target, quote)
        text = saved.text if saved is not None else quote
        if added:
            text = text.rstrip('\n') + '\n\n' + added
        self._draft_area.load_text(text)
        self._draft_area.move_cursor(self._draft_area.document.end)
        author = target.lmsg.fromname or target.lmsg.fromemail
        self._draft_area.border_title = escape(f'Draft → {author} · ctrl+s send · ctrl+g discard')
        self.screen.add_class('-drafting')
        self._set_reader_pane('draft')
        self.refresh_bindings()

    def action_send_draft(self) -> None:
        """b4's preview of the real message; there Ctrl-y sends and e returns here."""
        draft = self._draft
        if draft is None:
            return
        if draft.pane and self._drafts is not None:
            saved = self._drafts.get(draft.msgid)  # As the editor last wrote it.
            text = saved.text if saved else draft.quote
        else:
            text = self._draft_area.text
        if text == draft.quote:
            self._set_notice(
                'No changes to send' + (' (write the file first)' if draft.pane else '')
            )
            return
        preview_and_send(
            self,
            draft.target.lmsg,
            text,
            email_dryrun=draft.target.email_dryrun,
            patatt_sign=draft.target.patatt_sign,
            on_sent=lambda _msg: self._draft_sent(draft),
            on_edit=lambda _text: (
                self.action_focus_editor() if draft.pane else self._set_reader_pane('draft')
            ),
        )

    def _draft_sent(self, draft: Draft) -> None:
        actions = self._actions
        if self._draft is draft:
            self._close_draft(keep=False)
        elif self._drafts is not None:
            self._drafts.delete(draft.msgid)  # Sent after its editor closed.
            self._drafts_changed = True
        if actions is None:
            return

        def work() -> None:
            try:
                result = actions.answered(draft.item, draft.msgid)
            except Exception as exc:
                result = f'Sent, but could not mark answered: {exc}'
            self.call_from_thread(self._set_notice, result)

        self.run_worker(work, thread=True, group='action')

    def action_discard_draft(self) -> None:
        if self._draft is None and self._saved_draft is not None:
            self._discard_saved_draft()
            return
        self._confirm_discard(then=None)

    def _discard_saved_draft(self) -> None:
        """ctrl+g on a thread with a saved draft (not open): delete it after confirmation."""
        saved, drafts = self._saved_draft, self._drafts
        if saved is None or drafts is None:
            return

        def answered(confirmed: Optional[bool]) -> None:
            if confirmed and self._saved_draft == saved:
                drafts.delete(saved[1])
                self._saved_draft = None
                self._drafts_changed = True
                self._set_notice('Draft discarded')
                self._refresh_tree_labels()
                self._sync_bindings()

        self.push_screen(Confirm('Discard the saved draft? [y|N]'), answered)

    def _confirm_discard(self, then: Optional[Callable[[], None]]) -> None:
        def answered(confirmed: Optional[bool]) -> None:
            if confirmed:
                self._close_draft(keep=False)
                if then is not None:
                    then()

        self.push_screen(Confirm('Discard draft? [y|N]'), answered)

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        """Save the draft shortly after typing pauses."""
        draft = self._draft
        if event.text_area is self._draft_area and draft and not draft.pane and self._drafts:
            if self._save_timer is not None:
                self._save_timer.stop()
            self._save_timer = self.set_timer(0.5, self._save_draft)

    def _save_draft(self) -> None:
        """Write the open draft; an untouched quote is no draft (one small file)."""
        if self._save_timer is not None:
            self._save_timer.stop()
            self._save_timer = None
        draft, drafts = self._draft, self._drafts
        if draft is None or drafts is None or draft.pane:
            return  # An editor's draft is the file it writes.
        text = self._draft_area.text
        if text == draft.quote:
            drafts.delete(draft.msgid)
        else:
            drafts.save(draft.msgid, draft.quote, text)
        self._drafts_changed = True

    def _close_draft(self, keep: bool) -> None:
        """Close the draft pane, keeping the saved draft or deleting it."""
        draft = self._draft
        if draft is not None and draft.pane and not keep:
            editor, pane = self._editor, draft.pane
            if editor is not None:
                self.run_worker(lambda: editor.close(pane), thread=True, group='editor')
        if self._draft is not None and self._drafts is not None:
            if keep:
                self._save_draft()
            else:
                if self._save_timer is not None:
                    self._save_timer.stop()
                    self._save_timer = None
                self._drafts.delete(self._draft.msgid)
                self._drafts_changed = True
        self._draft = None
        self._draft_area.load_text('')
        self.screen.remove_class('-drafting')
        if self._reading is not None and self._reader_pane == 'draft':
            self._set_reader_pane('tree')
        self.refresh_bindings()

    def action_cycle_pane(self, delta: int) -> None:
        """Tab/Shift-Tab: next pane; one pane at a time on narrow terminals."""
        if isinstance(self.screen, LaunchDialog):
            self.screen.focus_next() if delta > 0 else self.screen.focus_previous()
        elif self._drawer_focused():
            self._show_drawer_tab('worktrees' if self._drawer_tab == 'sessions' else 'sessions')
        elif self._mode == 'narrow' and self._reading is not None:
            in_app = self._draft is not None and not self._draft.pane
            panes = ['tree', 'content'] + (['draft'] if in_app else [])
            here = panes.index(self._reader_pane) if self._reader_pane in panes else 0
            self._set_reader_pane(panes[(here + delta) % len(panes)])
        elif delta > 0:
            self.screen.focus_next()
        else:
            self.screen.focus_previous()

    def _still_reading(self, item: WorkItem) -> bool:
        # By identity: a reload replaces the open item with its fresh state.
        return self._reading is not None and self._reading.identity == item.identity

    def _unread(self) -> frozenset[str]:
        if self._reading is None:
            return frozenset()
        return self._reading.unread_msgids - self._seen_now

    def _mark_seen(self, reply: Reply) -> None:
        msgid = reply.message.msgid
        actions = self._actions
        if actions is None or self._thread is None or msgid not in self._unread():
            return
        self._seen_now.add(msgid)
        self._refresh_tree_labels()

        def work() -> None:
            try:
                actions.mark_seen([msgid])
            except Exception as exc:  # The flag store is local; report, do not raise.
                self.call_from_thread(self._set_notice, f'Could not mark read: {exc}')

        self.run_worker(work, thread=True, group='seen')

    def _current_reply(self):
        if self._thread is None or not self._node:
            return None
        replies = thread_replies(self._thread, self._post_key)
        return replies[self._node - 1] if self._node <= len(replies) else None

    def _show_row(self, index: int) -> None:
        if self._thread is None or self._reading is None or index >= len(self._rows):
            return
        row = self._rows[index]
        self._post_key, self._node = row.key, row.node
        if self._shown != (row.key, row.node):
            self._show_current()

    def _show_current(self) -> None:
        if self._thread is None or self._reading is None:
            return
        self._shown = (self._post_key, self._node)
        reply = self._current_reply()
        total = self._reading.series.expected
        title = content_title(self._thread, self._post_key, reply, total)
        if reply is not None and has_long_quote(reply):
            title += ' · [z] quotes'
        self._content.border_title = escape(title)
        if reply is None:
            blocks = root_blocks(self._thread, self._post_key, self._reading)
        else:
            blocks = message_blocks(reply, self._expand_quotes)
            self._mark_seen(reply)
        self._content.set_blocks(blocks)
        self._sync_bindings()

    def action_next_post(self, delta: int) -> None:
        if self._thread is None:
            return
        keys = post_keys(self._thread)
        here = keys.index(self._post_key) if self._post_key in keys else 0
        target = keys[max(0, min(here + delta, len(keys) - 1))]
        self._select((target, 0))

    def action_next_comment(self) -> None:
        """Next comment in series order, expanding its posting and crossing into later ones."""
        if self._thread is None:
            return
        keys = post_keys(self._thread)
        here = keys.index(self._post_key) if self._post_key in keys else 0
        if self._node < len(thread_replies(self._thread, self._post_key)):
            self._select((self._post_key, self._node + 1))
            return
        for key in keys[here + 1 :]:
            if thread_replies(self._thread, key):
                self._select((key, 1))
                return
        self.notify('No more comments in this series.')

    def action_next_finding(self) -> None:
        """Next Sashiko finding in series order: its patch, scrolled to its mark."""
        if self._thread is None or self._reading is None:
            return
        anchors = finding_anchors(self._thread, self._reading)
        if self._finding + 1 >= len(anchors):
            self._set_notice('No more findings in this series.')
            return
        self._finding += 1
        key, anchor = anchors[self._finding]
        if (self._post_key, self._node) != (key, 0):
            self._select((key, 0))
            self._show_row(next(n for n, r in enumerate(self._rows) if (r.key, r.node) == (key, 0)))
        self._content.scroll_to_anchor(anchor)

    def action_toggle_replies(self) -> None:
        """Fold the branch under the cursor: a posting's replies, or one reply's.

        On a reply without replies of its own, fold its parent's branch instead.
        """
        key, node = self._post_key, self._node
        if node and self._thread is not None:
            replies = thread_replies(self._thread, key)
            msgid = replies[node - 1].message.msgid
            if msgid in self._collapsed:
                self._collapsed.discard(msgid)
                self._render_tree((key, node))
                return
            if branch(replies, node):
                self._collapsed.add(msgid)
                self._render_tree((key, node))
                return
            parent = parent_node(replies, node)
            if parent:
                self._collapsed.add(replies[parent - 1].message.msgid)
                self._render_tree((key, parent))
                return
        if key in self._expanded:
            self._expanded.discard(key)
            self._render_tree((key, 0))
        else:
            self._expanded.add(key)
            self._render_tree((key, self._node))

    def _select(self, target: tuple[str, int]) -> None:
        """Highlight a posting or reply, expanding its posting when a reply is hidden."""
        key, node = target
        hidden = self._unfold_ancestors(key, node) if node and self._thread else False
        if node and (key not in self._expanded or hidden):
            self._expanded.add(key)
            self._render_tree(target)
            return
        positions = {(row.key, row.node): n for n, row in enumerate(self._rows)}
        if target in positions:
            self._tree.highlighted = positions[target]

    def action_expand_node(self) -> None:
        """→: open the posting or folded branch under the cursor."""
        if self._thread is None:
            return
        key, node = self._post_key, self._node
        replies = thread_replies(self._thread, key)
        if not node:
            if replies and key not in self._expanded:
                self._expanded.add(key)
                self._render_tree((key, 0))
            return
        msgid = replies[node - 1].message.msgid
        if msgid in self._collapsed:
            self._collapsed.discard(msgid)
            self._render_tree((key, node))

    def action_collapse_node(self) -> None:
        """←: fold the open branch under the cursor, else its parent's, moving there."""
        if self._thread is None:
            return
        key, node = self._post_key, self._node
        if not node:
            if key in self._expanded:
                self._expanded.discard(key)
                self._render_tree((key, 0))
            return
        replies = thread_replies(self._thread, key)
        msgid = replies[node - 1].message.msgid
        if branch(replies, node) and msgid not in self._collapsed:
            self._collapsed.add(msgid)
            self._render_tree((key, node))
            return
        parent = parent_node(replies, node)
        if parent:
            self._collapsed.add(replies[parent - 1].message.msgid)
            self._render_tree((key, parent))
        else:
            self._expanded.discard(key)
            self._render_tree((key, 0))

    def action_toggle_all(self) -> None:
        """Expand every posting and branch; if all are open already, collapse every posting."""
        if self._thread is None:
            return
        keys = [k for k in post_keys(self._thread) if thread_replies(self._thread, k)]
        if self._collapsed or not set(keys) <= self._expanded:
            self._expanded, self._collapsed = set(keys), set()
            self._render_tree((self._post_key, self._node))
        else:
            self._expanded, self._collapsed = set(), set()
            self._render_tree((self._post_key, 0))

    def _unfold_ancestors(self, key: str, node: int) -> bool:
        """Unfold every collapsed reply above *node*; return whether any was folded."""
        assert self._thread is not None
        replies = thread_replies(self._thread, key)
        unfolded = False
        parent = parent_node(replies, node)
        while parent:
            msgid = replies[parent - 1].message.msgid
            if msgid in self._collapsed:
                self._collapsed.discard(msgid)
                unfolded = True
            parent = parent_node(replies, parent)
        return unfolded

    def action_toggle_quotes(self) -> None:
        self._expand_quotes = not self._expand_quotes
        self._show_current()

    def _close_reader(self) -> None:
        self.workers.cancel_group(self, 'thread')
        self._saved_draft = None
        self._reading, self._thread, self._post_key = None, None, ''
        self._node, self._rows, self._expanded = 0, [], set()
        self._collapsed = set()
        self.screen.remove_class('-reading')
        self.screen.remove_class('-artifacts')  # The next thread opens on its content.
        self.workers.cancel_group(self, 'artifacts')
        for name in ('tree', 'content', 'draft'):
            self.screen.remove_class(f'-rpane-{name}')
        self._series_list.focus()
        if self.index is not None:
            self._update_status()
        self.refresh_bindings()
        self._place_pane()
        if self._seen_now or self._drafts_changed:
            self._seen_now, self._drafts_changed = set(), False
            self.run_worker(self._load, thread=True, name='load', exclusive=True)

    async def action_quit(self) -> None:
        """Q and Textual's ctrl+q both ask before exiting."""
        if not isinstance(self.screen, ConfirmQuit):
            prompt = ConfirmQuit.PROMPT
            if self._draft is not None and self._draft.pane:
                prompt = 'Quit? Your editor pane closes; what it wrote is kept. [y|N]'
            self.push_screen(ConfirmQuit(prompt), self._quit_confirmed)

    def _quit_confirmed(self, confirmed: Optional[bool]) -> None:
        if not confirmed:
            return
        self._save_draft()
        self._close_editor()
        sync = self._sync
        if sync is None:
            self.exit()
            return

        def work() -> None:
            try:
                sync(self._sync_timeout)
            except Exception:  # Unpushed work stays committed in state/ for the next sync.
                pass
            self.call_from_thread(self.exit)

        self._set_notice('Syncing state…')
        # Bounded: the app exits at the timeout whether or not the sync has finished.
        self.set_timer(self._sync_timeout, self.exit)
        self.run_worker(work, thread=True, group='sync')

    def on_unmount(self) -> None:
        # However the app ends, an open draft is kept and its editor pane closed.
        self._save_draft()
        self._close_editor()
        # A session beside the app goes back to its own window and keeps running.
        if self._sessions is not None and self._app_pane and self._beside is not None:
            try:
                self._sessions.hide(self._app_pane)
            except Exception:
                pass

    def _close_editor(self) -> None:
        """Close an open editor pane, keeping what it wrote (an untouched quote is no draft)."""
        draft, editor, drafts = self._draft, self._editor, self._drafts
        if draft is None or not draft.pane or editor is None or drafts is None:
            return
        self._draft = None
        editor.close(draft.pane)
        drafts.set_pane(draft.msgid, '')
        saved = drafts.get(draft.msgid)
        if saved is not None and saved.text == draft.quote:
            drafts.delete(draft.msgid)

    def quit_prompt(self) -> Optional[str]:
        return self.screen.prompt if isinstance(self.screen, Confirm) else None

    def action_toggle_preview(self) -> None:
        self._preview = not self._preview
        self._summary_pane.border_title = 'Preview' if self._preview else 'Summary'
        self._apply_preview_width()
        if self._preview and self._mode == 'narrow':
            self.action_open_summary()
        self._show_summary()

    def action_toggle_drawer(self) -> None:
        """Open and focus the drawer; close it (focus back on the list or reader)."""
        if self.screen.has_class('-drawer-open'):
            self.screen.remove_class('-drawer-open')
            if self._drawer_focused():
                self._focus_main()
        else:
            self.screen.add_class('-drawer-open')
            self._render_session_rows()  # The selected thread's drafts may have changed.
            # Opening is the maintainer's move: start at the most urgent entry.
            for rows in (self._drawer_sessions, self._drawer_worktrees):
                if rows.option_count:
                    rows.highlighted = 0
            self._show_drawer_tab(self._drawer_tab)
            self.poll_sessions()
        self.refresh_bindings()

    # Sessions: polled off the UI thread; results change the drawer's text only.
    def poll_sessions(self) -> None:
        if self._sessions is None or self._polling:
            return
        self._polling = True
        self.run_worker(self._poll_sessions, thread=True, group='sessions')

    def _poll_sessions(self) -> None:
        assert self._sessions is not None
        try:
            overview: Optional[Overview] = self._sessions.overview()
        except Exception:  # A record being written, a vanished directory: next poll.
            overview = None
        try:
            self.call_from_thread(self._apply_overview, overview)
        except RuntimeError:  # The app has stopped.
            self._polling = False

    def _apply_overview(self, overview: Optional[Overview]) -> None:
        """A background refresh: a failure is reported in the status line, never fatal."""
        self._polling = False
        if overview is None or not self.screen_stack:
            return
        try:
            self._show_overview(overview)
        except Exception as exc:
            self.log.error(f'session drawer update failed: {exc!r}')
            self._set_notice(f'Session drawer update failed: {exc}')

    def _show_overview(self, overview: Overview) -> None:
        self._overview = overview
        if isinstance(self.screen, WorkbenchScreen):
            self.screen.refresh_sessions()  # Its sessions' states.
        line = drawer_line(overview)
        if line != self._drawer:
            self._drawer = line
            self.query_one('#drawer', Static).update(line)
        self.query_one('#drawer-tabs', Static).update(tabs_line(overview, self._drawer_tab))
        if self._beside is not None and not any(
            v.record.id == self._beside and v.state in LIVE_STATES for v in overview.sessions
        ):
            self._beside = None  # Its pane went away with it; nothing to move.
        self._render_session_rows()
        self._render_worktree_rows()
        self._sync_bindings()

    def _render_worktree_rows(self) -> None:
        """The Worktrees list; marked trees start with `*`."""
        states = {v.record.id: v.state for v in self._overview.sessions}
        paths = {w.path for w in self._overview.worktrees}
        self._marked_trees &= paths
        marks = bool(self._marked_trees)
        worktrees = [
            (
                w.path,
                ('* ' if w.path in self._marked_trees else '  ' if marks else '')
                + worktree_row(w, states),
            )
            for w in self._overview.worktrees
        ]
        if worktrees != self._worktree_rows:
            self._worktree_rows = worktrees
            _replace_rows(self._drawer_worktrees, worktrees, 'No session worktrees.')

    def _selected_worktree(self) -> Optional[str]:
        if self.focused is not self._drawer_worktrees:
            return None
        option = self._drawer_worktrees.highlighted_option
        key = option.id if option is not None else None
        return key if any(w.path == key for w in self._overview.worktrees) else None

    def action_mark_worktree(self) -> None:
        if (path := self._selected_worktree()) is not None:
            self._marked_trees ^= {path}
            self._render_worktree_rows()

    def action_remove_worktrees(self) -> None:
        """x in the Worktrees tab: the marked trees, else the selected one."""
        selected = self._selected_worktree()
        order = [w.path for w in self._overview.worktrees]
        paths = [p for p in order if p in self._marked_trees] or ([selected] if selected else [])
        if paths:
            self._remove_worktrees(paths)

    def _remove_worktrees(self, paths: list[str]) -> None:
        """Say what each tree would lose, then remove on y; changes need a second y."""
        hub = self._sessions
        if hub is None:
            return

        def look() -> None:
            try:
                losses = hub.worktree_losses(paths)
            except Exception as exc:
                self.call_from_thread(self._set_notice, str(exc))
                return
            self.call_from_thread(self._confirm_removal, paths, losses)

        self.run_worker(look, thread=True, group='session-action')

    def _confirm_removal(self, paths: list[str], losses: dict[str, TreeState]) -> None:
        hub = self._sessions
        assert hub is not None
        listed = '\n'.join(f'  {p}: {losses[p].describe()}' for p in paths)
        changed = [p for p in paths if not losses[p].clean]
        count = f'{len(paths)} worktree{"s"[: len(paths) - 1]}'

        def remove() -> str:
            hub.remove_worktrees(paths)
            self._marked_trees -= set(paths)
            return f'Removed {count}'

        def again(confirmed: Optional[bool]) -> None:
            if confirmed:
                self._session_call(remove)

        def first(confirmed: Optional[bool]) -> None:
            if not confirmed:
                return
            if not changed:
                self._session_call(remove)
                return
            lost = '\n'.join(f'  {p}: {losses[p].describe()}' for p in changed)
            prompt = f'This will be lost:\n{lost}\nRemove anyway? [y|N]'
            self.push_screen(Confirm(prompt), again)

        self.push_screen(Confirm(f'Remove {count}?\n{listed}\n[y|N]'), first)

    def _render_session_rows(self) -> None:
        """The Sessions list: the selected thread's drafts first, then its sessions."""
        reading = self._reading.identity if self._reading is not None else None
        rows = self._draft_rows() + session_rows(
            self._overview.sessions, reading=reading, beside=self._beside
        )
        if rows != self._session_rows:
            self._session_rows = rows
            _replace_rows(self._drawer_sessions, rows, 'No agent sessions or drafts.')
        self._sync_bindings()

    def _draft_rows(self) -> list[tuple[str, str]]:
        """Saved drafts of the open thread (or, in the list, the highlighted series)."""
        drafts, item = self._drafts, self._reading or self._item
        if drafts is None or item is None:
            return []
        saved = drafts.msgids()
        messages: dict[str, tuple[str, str]] = {}
        if self._reading is not None and self._thread is not None:
            for key in post_keys(self._thread):
                post = find_post(self._thread, key)
                replies = thread_replies(self._thread, key)
                for message in [post.message] if post else []:
                    messages[message.msgid] = (message.author_name, message.subject)
                for reply in replies:
                    messages[reply.message.msgid] = (
                        reply.message.author_name,
                        reply.message.subject,
                    )
        else:
            series = item.series
            for msgid in series.msgids:
                messages[msgid] = (series.author_name, series.subject)
        rows = []
        for msgid, (name, subject) in messages.items():
            if msgid not in saved:
                continue
            text = f'✎ draft · reply to {name}: {subject}'
            if self._draft is not None and self._draft.msgid == msgid and self._draft.pane:
                text += ' · in your editor'
            rows.append((f'draft:{msgid}', text))
        return rows

    def selected_draft(self) -> Optional[str]:
        """The draft highlighted in the drawer, for its keys and tests."""
        if self.focused is not self._drawer_sessions:
            return None
        option = self._drawer_sessions.highlighted_option
        key = option.id if option is not None else None
        return key.removeprefix('draft:') if key and key.startswith('draft:') else None

    def drawer_rows(self) -> list[str]:
        """Rows of the drawer tab shown, for tests."""
        rows = self._session_rows if self._drawer_tab == 'sessions' else self._worktree_rows
        return [text for _key, text in rows]

    def selected_session(self) -> Optional[str]:
        view = self._selected_view()
        return view.record.id if view is not None else None

    def _drawer_focused(self) -> bool:
        return self.focused is not None and isinstance(self.focused, DrawerList)

    def _selected_view(self) -> Optional[SessionView]:
        if self.focused is not self._drawer_sessions:
            return None
        option = self._drawer_sessions.highlighted_option
        sid = option.id if option is not None else None
        return next((v for v in self._overview.sessions if v.record.id == sid), None)

    def _show_drawer_tab(self, tab: str) -> None:
        self._drawer_tab = tab
        self.screen.set_class(tab == 'worktrees', '-drawer-worktrees')
        self.query_one('#drawer-tabs', Static).update(tabs_line(self._overview, tab))
        (self._drawer_sessions if tab == 'sessions' else self._drawer_worktrees).focus()
        self.refresh_bindings()

    def _focus_main(self) -> None:
        if self._reading is not None:
            self._set_reader_pane(self._reader_pane)
        else:
            self._series_list.focus()
        self.refresh_bindings()

    def _session_call(self, call: Callable[[], Optional[str]]) -> None:
        """Run a maintainer's session action off the UI thread, then poll."""

        def work() -> None:
            try:
                notice = call()
            except Exception as exc:
                notice = f'Session action failed: {exc}'
            self.call_from_thread(self._session_done, notice)

        self.run_worker(work, thread=True, group='session-action')

    def _session_done(self, notice: Optional[str]) -> None:
        if notice:
            self._set_notice(notice)
        self.poll_sessions()

    def action_artifacts(self) -> None:
        """F4: in a thread, its sessions' output in place of the content (F4 or Escape
        returns); elsewhere, the Branches tab."""
        if self._reading is not None and len(self.screen_stack) == 1:
            if self.artifacts_shown():
                self._hide_artifacts()
            else:
                self._show_artifacts()
            return
        self.action_tab('branches')

    def action_tab(self, tab: str) -> None:
        """F2/F3/F4: switch to a tab. Workbench and Artifacts are made once and kept, so
        each keeps its place (they reread their data on each return); LKML is the app's
        own screen, kept as it was left."""
        snapshot = self.snapshot
        if tab == 'lkml':
            if len(self.screen_stack) > 1:
                self.pop_screen()
                self.call_after_refresh(self._place_pane)  # The column follows the tab.
            return
        if snapshot is None:
            return
        if tab not in self._installed:
            if tab == 'workbench' and self._workbenches is not None:
                screen: Screen[Any] = WorkbenchScreen(self._workbenches, snapshot.workspace, {}, {})
            elif tab == 'branches' and self._branches is not None:
                screen = BranchesScreen(self._branches, snapshot.workspace)
            else:
                return
            self.install_screen(screen, tab)
            self._installed.add(tab)
        target = self.get_screen(tab)
        if isinstance(target, (WorkbenchScreen, BranchesScreen)) and self.screen is not target:
            target.stale = target.is_mounted  # Shown again: it rereads.
        if isinstance(target, WorkbenchScreen):
            target.workspace = snapshot.workspace
            target.series = {i.identity: i.series for i in snapshot.items}
            target.latest = {i.identity: i.series.revision for i in snapshot.items}
        elif isinstance(target, BranchesScreen):
            target.workspace = snapshot.workspace
        if self.screen is target:
            return
        if len(self.screen_stack) > 1:
            self.switch_screen(tab)
        else:
            self.push_screen(tab)
        self.call_after_refresh(self._place_pane)  # The column follows the tab.

    def action_keys(self) -> None:
        """?: every key of the current tab, the footer's and the rest."""
        lines, seen = [], set()
        for node, binding, enabled, _tooltip in self.screen.active_bindings.values():
            label = (binding.action, binding.description)
            ours = (binding.key, binding.action) in _own_bindings(node)  # Not Textual's own.
            if not ours or not enabled or label in seen or not binding.description:
                continue
            seen.add(label)
            lines.append(f'{self.get_key_display(binding)}  {binding.description}')
        self.push_screen(Notice('Keys here (any key closes):\n\n' + '\n'.join(lines)))

    def action_apply_series(self) -> None:
        """a: the open or selected series onto a base or a prep branch, through b4: pick the
        branch (when there are several), then the patches."""
        service, snapshot = self._branches, self.snapshot
        item = self._reading or self._item
        if service is None or snapshot is None or item is None:
            return
        workspace, series = snapshot.workspace, item.series

        def work() -> None:
            try:
                choices = service.apply_choices(
                    workspace, item.project or '', series.source, series.revision
                )
            except Exception as exc:
                self.call_from_thread(self._set_notice, f'Cannot apply: {exc}')
                return
            self.call_from_thread(self._pick_apply_target, item, choices)

        self._set_notice('Reading the series…')
        self.run_worker(work, thread=True, group='apply')

    def _pick_apply_target(self, item: WorkItem, choices: ApplyChoices) -> None:
        if choices.problem:
            self._set_notice(f'Cannot apply: {choices.problem}')
            return
        self._set_notice('')
        projects = list(dict.fromkeys(_target_project(k) for k, _branch in choices.targets))
        if len(projects) == 1:
            self._pick_apply_branch(item, choices, projects[0])
            return

        def picked(project: Optional[str]) -> None:
            if project is not None:
                self._pick_apply_branch(item, choices, project)

        options = []
        for pid in projects:
            n = sum(1 for k, _b in choices.targets if _target_project(k) == pid)
            mine = ' (this series)' if pid == item.project else ''
            options.append((pid, f'{pid}{mine} · {n} branch{"es" if n != 1 else ""}'))
        self.push_screen(Pick('Apply to which project?', options), picked)

    def _pick_apply_branch(self, item: WorkItem, choices: ApplyChoices, project: str) -> None:
        mine = [(k, b) for k, b in choices.targets if _target_project(k) == project]
        if len(mine) == 1:
            self._pick_patches(item, choices, mine[0][0])
            return

        def picked(target: Optional[str]) -> None:
            if target is not None:
                self._pick_patches(item, choices, target)

        self.push_screen(Pick(f'Apply to which {project} branch?', mine), picked)

    def _pick_patches(self, item: WorkItem, choices: ApplyChoices, target: str) -> None:
        branch = _target_label(target, choices.targets)
        subject = f'{item.series.subject} v{item.series.revision}'

        def picked(numbers: Optional[list[int]]) -> None:
            if numbers is None:
                return
            if not numbers:
                self._set_notice('Nothing applied: no patches ticked')
                return
            self._apply(item, target, branch, numbers, choices.tips[target])

        self.push_screen(ApplyDialog(subject, branch, choices.patches), picked)

    def _apply(
        self, item: WorkItem, target: str, branch: str, numbers: list[int], expected: str
    ) -> None:
        service, snapshot = self._branches, self.snapshot
        if service is None or snapshot is None:
            return
        workspace, series = snapshot.workspace, item.series

        def work() -> None:
            try:
                result = service.apply(
                    workspace, target, series.source, series.revision, numbers, expected
                )
            except Exception as exc:
                self.call_from_thread(self._set_notice, f'Cannot apply: {exc}')
                return
            if result.conflict:
                self.call_from_thread(self._set_notice, '')
                self.call_from_thread(
                    self._offer_apply_handoff, item, target, branch, numbers, result
                )
                return
            if result.ok:
                n = len(result.applied)
                notice = f'Applied {n} patch{"es" if n != 1 else ""} to {branch} · F3 shows it'
            else:
                notice = f'Not applied: {result.error}'
            self.call_from_thread(self._action_done, notice, result.ok)

        self._set_notice(f'Applying to {branch}…')
        self.run_worker(work, thread=True, group='apply')

    def _offer_apply_handoff(
        self,
        item: WorkItem,
        target: str,
        branch: str,
        numbers: list[int],
        failed: 'ApplyResult',
    ) -> None:
        """The patches do not apply to *branch*: hand them to an agent (an apply session on
        a copy; A in F3 accepts its result), or leave the branch as it is."""
        service = self._branches

        def picked(choice: Optional[str]) -> None:
            if choice != 'agent' or service is None or self.snapshot is None:
                self._set_notice(f'Not applied: {branch} is unchanged')
                return
            workspace = self.snapshot.workspace

            def work() -> None:
                try:
                    record = service.apply_session(
                        workspace, target, _target(item), numbers, failed
                    )
                except Exception as exc:
                    self.call_from_thread(self._set_notice, f'Cannot hand it off: {exc}')
                    return
                self.call_from_thread(self._after_launch, record)
                self.call_from_thread(
                    self._set_notice,
                    f'Launched apply session {record.id}: when it is done, A in F3 accepts its '
                    f'result onto {branch}',
                )

            self._set_notice(f'Preparing an apply session for {branch}…')
            self.run_worker(work, thread=True, group='apply')

        options = [
            (
                'agent',
                f'Hand it to an agent: a session applies the patches on a copy of {branch} and '
                f'resolves the conflicts; A in F3 accepts its result onto {branch}',
            ),
            ('none', f"Don't apply: leave {branch} as it is"),
        ]
        self.push_screen(Pick(_conflict_text(failed.error, failed.output), options), picked)

    def action_drop_series(self) -> None:
        """d: take the open or selected series' commits off a maintainer base (after a
        confirmation); later commits are replayed onto what remains."""
        service, snapshot = self._branches, self.snapshot
        item = self._reading or self._item
        if service is None or snapshot is None or item is None:
            return
        workspace = snapshot.workspace
        patches = tuple((p.msgid, p.subject) for p in item.series.patches)

        def work() -> None:
            try:
                choices = service.drop_choices(workspace, item.project or '', patches)
            except Exception as exc:
                self.call_from_thread(self._set_notice, f'Cannot drop: {exc}')
                return
            self.call_from_thread(self._pick_drop_target, item, choices)

        self.run_worker(work, thread=True, group='apply')

    def _pick_drop_target(self, item: WorkItem, choices: DropChoices) -> None:
        if choices.problem:
            self._set_notice(f'Cannot drop: {choices.problem}')
            return
        if len(choices.targets) == 1:
            self._confirm_drop(item, choices, choices.targets[0][0])
            return

        def picked(target: Optional[str]) -> None:
            if target is not None:
                self._confirm_drop(item, choices, target)

        labels = [(k, _target_label(k, choices.targets)) for k, _branch in choices.targets]
        self.push_screen(Pick('Drop it from which base?', labels), picked)

    def _confirm_drop(self, item: WorkItem, choices: DropChoices, target: str) -> None:
        branch = _target_label(target, choices.targets)
        commits = choices.commits[target]
        n = len(commits)
        prompt = (
            f'Drop {n} commit{"s" if n != 1 else ""} of {item.series.subject} from {branch}? '
            'Later commits are replayed onto what remains. [y|N]'
        )

        def answered(confirmed: Optional[bool]) -> None:
            if confirmed:
                self._drop(target, branch, [c.sha for c in commits], choices.tips[target])

        self.push_screen(Confirm(prompt), answered)

    def _drop(self, target: str, branch: str, shas: list[str], expected: str) -> None:
        service, snapshot = self._branches, self.snapshot
        if service is None or snapshot is None:
            return
        workspace = snapshot.workspace

        def work() -> None:
            try:
                result = service.drop(workspace, target, shas, expected)
            except Exception as exc:
                self.call_from_thread(self._set_notice, f'Cannot drop: {exc}')
                return
            if result.ok:
                n = len(result.dropped)
                notice = f'Dropped {n} commit{"s" if n != 1 else ""} from {branch}'
            elif result.failed:
                notice = (
                    f'Not dropped: {result.failed} does not replay without them; nothing changed'
                )
            else:
                notice = f'Not dropped: {result.error}'
            self.call_from_thread(self._action_done, notice, result.ok)

        self._set_notice(f'Dropping from {branch}…')
        self.run_worker(work, thread=True, group='apply')

    def action_add_to_workbench(self) -> None:
        """w: the open or selected series onto a workbench (or a new one), under its
        project; one not routed to a project asks which."""
        service, snapshot = self._workbenches, self.snapshot
        item = self._reading or self._item
        if service is None or snapshot is None or item is None:
            return
        workspace = snapshot.workspace

        def chose_project(project: Optional[str]) -> None:
            if project:
                self._pick_workbench(item, project)

        if item.project:
            self._pick_workbench(item, item.project)
        else:
            projects = [(p.id, p.label) for p in workspace.projects]
            self.push_screen(
                Pick(f'Which project is {item.series.subject} for?', projects), chose_project
            )

    def _pick_workbench(self, item: WorkItem, project: str) -> None:
        service = self._workbenches
        if service is None:
            return

        def work() -> None:
            benches, _problems = service.store.list()
            self.call_from_thread(show, benches)

        def show(benches: list[Workbench]) -> None:
            options = [(b.id, b.label) for b in benches] + [(NEW_BENCH, 'New workbench…')]

            def picked(wid: Optional[str]) -> None:
                if wid == NEW_BENCH:
                    self.push_screen(Ask('Name the new workbench'), named)
                elif wid:
                    bench = next((b for b in benches if b.id == wid), None)
                    if bench is not None:
                        self._add_to(bench, item, project)

            def named(label: Optional[str]) -> None:
                if label:
                    self._add_to(Workbench(service.store.new_id(label), label), item, project)

            self.push_screen(
                Pick(f'Add {item.series.subject} to which workbench?', options), picked
            )

        self.run_worker(work, thread=True, group='workbench')

    def _add_to(self, bench: Workbench, item: WorkItem, project: str) -> None:
        service, snapshot = self._workbenches, self.snapshot
        if service is None or snapshot is None:
            return
        found = snapshot.workspace.project(project)
        base = found.bases[0].branch if found and found.bases else ''
        s = item.series
        msgid = s.msgids[0] if s.msgids else ''
        try:
            if s.discussion:  # Read by sessions, never applied.
                changed = add_mail(bench, project, item.identity, s.subject, msgid, base=base)
            else:
                changed = add_series(
                    bench, project, item.identity, s.revision, s.subject, base=base, msgid=msgid
                )
            service.store.save(changed)
        except (WorkbenchError, OSError) as exc:
            self._set_notice(f'Not added: {exc}')
            return
        self._set_notice(
            f'Added {s.subject} v{s.revision} to {bench.label} ({project}) · F4 shows it'
        )

    def launch_workbench(self, bench: Workbench) -> None:
        """F1 on the Workbench screen: the launch dialog for a session over *bench*: its
        trees (worktree mode; each tree's base and items from the workbench), or only its
        series' mail to read and compare (lightweight)."""
        hub, snapshot, service = self._sessions, self.snapshot, self._workbenches
        if hub is None or snapshot is None or service is None:
            self._set_notice('Agent sessions are not available here')
            return
        workspace, overview = snapshot.workspace, self._overview
        series = {i.identity: i.series for i in snapshot.items}

        def work() -> None:
            try:
                plans, problems, tree_problems = service.plans(workspace, bench, series)
                if problems:
                    note = f'Cannot launch {bench.label}: ' + '; '.join(problems)
                    self.call_from_thread(self._set_notice, note)
                    return
                blocker = hub.blocker()
                if blocker is not None:
                    self.call_from_thread(self.push_screen, Notice(f'Agent sessions: {blocker}'))
                    return
                profiles, profile_problems = hub.profiles()
                skills, skill_problems = hub.skills()
                situation = _bench_situation(hub, workspace, overview, skills, plans)
            except Exception as exc:
                self.call_from_thread(self._set_notice, f'Cannot open the session dialog: {exc}')
                return
            trees = [_tree_line(p) for p in plans]
            mailboxes = [f'{i.label} ({p.project})' for p in plans for i in p.items if i.source]
            target = Target('series', '', f'workbench:{bench.id}', 0, bench.label)
            warned = [f'trees only: {w}' for w in tree_problems]
            threads = (ContextItem('threads', "each series' thread"),)
            items: dict[str, tuple[ContextItem, ...]] = {'series': threads, 'workspace': ()}
            dialog = LaunchDialog(
                target,
                profiles,
                situation,
                items,
                profile_problems + skill_problems + warned,
                trees=trees,
                mailboxes=mailboxes,
                tree_projects=[p.project for p in plans],
            )
            self.call_from_thread(
                self.push_screen,
                dialog,
                lambda r: self._launch_bench(bench, plans, r, tree_problems),
            )

        self.run_worker(work, thread=True, group='session-action')

    def _launch_bench(
        self,
        bench: Workbench,
        plans: tuple[TreePlan, ...],
        result: Optional[LaunchResult],
        tree_problems: Sequence[str] = (),
    ) -> None:
        hub, snapshot = self._sessions, self.snapshot
        if result is None or hub is None or snapshot is None:
            return
        workspace = snapshot.workspace
        uses = dict(result.choice.trees)
        if uses:  # The dialog's choice of tree per project.
            plans = tuple(dataclasses.replace(p, use=uses.get(p.project, 'none')) for p in plans)

        def launch() -> Optional[str]:
            skills = hub.skills()[0]
            situation = _bench_situation(hub, workspace, hub.overview(), skills, plans)
            mode = plan(result.profile, result.choice, situation, result.items).mode
            if mode == 'worktree' and tree_problems:
                return f'Cannot prepare the trees of {bench.label}: ' + '; '.join(tree_problems)
            if mode == 'worktree':
                self.call_from_thread(self._set_notice, f'Preparing the trees of {bench.label}…')
            try:
                record = hub.launch_workbench(
                    bench.id,
                    bench.label,
                    plans,
                    result.profile,
                    result.choice,
                    situation,
                    result.items,
                )
            except SessionError as exc:
                handed = any(p.agent_apply for p in plans if p.project == exc.project)
                if exc.base and exc.project and not handed:
                    self.call_from_thread(self._offer_bench_handoff, bench, plans, result, exc)
                    return None
                if not exc.detail:
                    raise
                self.call_from_thread(self.push_screen, Notice(f'{exc}\n\n{exc.detail.strip()}'))
                return None
            self.call_from_thread(self._bench_launched, record)
            return f'Launched session {record.id}: workbench {bench.label}'

        self._session_call(launch)

    def _bench_launched(self, record: SessionRecord) -> None:
        """A workbench session joins beside the app at once and shows in its tab."""
        if self._app_pane:
            self._bench_beside = record.id
            self._place_pane(record.id)
        if isinstance(self.screen, WorkbenchScreen):
            self.screen.refresh_sessions()

    def workbench_sessions(self, bench_id: str) -> list[tuple[str, str, bool]]:
        """(id, label, live) of workbench *bench_id*'s sessions, newest first (blocking)."""
        hub = self._sessions
        if hub is None:
            return []
        states = {v.record.id: v.state for v in self._overview.sessions}
        records = sorted(hub.records(), key=lambda r: r.created, reverse=True)
        known = {b.id for b in self._workbenches.store.list()[0]} if self._workbenches else set()
        rows = []
        for r in records:
            filed = r.workbench if r.workbench in known else ''  # Its workbench gone: unfiled.
            if filed != bench_id:
                continue
            state = states.get(r.id, r.state or 'ended')
            label = f'{r.id} · {r.mode} · {state} · {r.created[:16]}'
            rows.append((r.id, label, state in LIVE_STATES))
        return rows

    def bench_removal(self, bench_id: str) -> tuple[list[str], list[str], dict[str, TreeState]]:
        """(workbench *bench_id*'s sessions, the live ones, what removing each of their
        trees would lose) (blocking)."""
        hub = self._sessions
        if hub is None:
            return [], [], {}
        sids = [r.id for r in hub.records() if r.workbench == bench_id]
        live, losses = hub.session_losses(sids) if sids else ([], {})
        return sids, live, losses

    def remove_bench_sessions(self, sids: Sequence[str]) -> list[str]:
        """End the sessions, remove their trees, delete them (blocking; confirmed)."""
        hub = self._sessions
        if hub is None:
            return []
        removed = hub.remove_sessions(sids)
        self.call_from_thread(self.poll_sessions)
        return removed

    def session_output(self, sid: str) -> list[Attachment]:
        """What session *sid* produced: its prompt, commits, files (blocking)."""
        hub = self._sessions
        if hub is None:
            return []
        record = hub.store.record(sid)
        return gather([record], record.series, hub.store.directory)

    def open_bench_session(self, sid: str) -> None:
        """Enter on a workbench session: beside the app, focused; outside tmux, its window."""
        hub = self._sessions
        if hub is None:
            return
        if not self._app_pane:
            self._session_call(lambda: hub.open(sid))
            return
        self._bench_beside = sid
        self._place_pane(sid, focus=True)

    # The Workbench tab's tree actions: thin wrappers over the Branches service (blocking).

    def _branch_rows(self, rows: Sequence[ContentRow]) -> list[DetailRow]:
        return [
            DetailRow(r.text, r.attachment.sha, r.attachment.repo, branch=r.attachment.branch)
            for r in rows
            if r.attachment is not None and r.attachment.kind == 'patch'
        ]

    def _bench_branches(self) -> tuple[Branches, Workspace]:
        if self._branches is None or self.snapshot is None:
            raise SessionError('branches are not available here')
        return self._branches, self.snapshot.workspace

    def bench_tree_targets(self, item: str) -> tuple[list[tuple[str, str]], dict[str, str]]:
        branches, workspace = self._bench_branches()
        return branches.tree_targets(workspace, item)

    def bench_pluck(
        self, item: str, rows: Sequence[ContentRow], target: str, expected: str
    ) -> PluckResult:
        branches, workspace = self._bench_branches()
        return branches.pluck(workspace, item, self._branch_rows(rows), target, expected)

    def bench_fork_points(self, item: str) -> list[str]:
        branches, workspace = self._bench_branches()
        return branches.fork_points(workspace, item)

    def bench_cut_prep(
        self, item: str, rows: Sequence[ContentRow], name: str, fork: str
    ) -> CutResult:
        branches, workspace = self._bench_branches()
        return branches.cut_prep(workspace, item, self._branch_rows(rows), name, fork)

    def bench_losses(self, item: str) -> str:
        branches, workspace = self._bench_branches()
        return branches.losses(workspace, item)

    def bench_reap(self, item: str) -> tuple[bool, str]:
        branches, workspace = self._bench_branches()
        return branches.reap(workspace, item)

    def bench_derive(
        self, item: str, row: Optional[ContentRow]
    ) -> tuple[list[tuple[str, str, str]], Optional[ContextItem]]:
        branches, workspace = self._bench_branches()
        detail = (self._branch_rows([row]) or [None])[0] if row is not None else None
        return branches.derive_options(workspace, item, detail), branches.derive_context(
            item, detail
        )

    def _open_bench_tab(self, record: SessionRecord) -> None:
        """A workbench session from the drawer: its workbench's tab, the session selected
        and beside the app."""
        if self._workbenches is None or self.snapshot is None:
            self.open_bench_session(record.id)
            return
        self.action_tab('workbench')
        screen = self.get_screen('workbench')
        if isinstance(screen, WorkbenchScreen):
            screen.select(record.workbench, record.id)
        self.open_bench_session(record.id)

    def _offer_bench_handoff(
        self,
        bench: Workbench,
        plans: tuple[TreePlan, ...],
        result: LaunchResult,
        exc: SessionError,
    ) -> None:
        """A workbench tree whose items do not apply: hand that tree to the agent (it starts
        at that base with every item as one mbox), or launch nothing."""
        project, base = exc.project, exc.base

        def picked(choice: Optional[str]) -> None:
            if choice == 'agent':
                again = tuple(
                    dataclasses.replace(p, agent_apply=base) if p.project == project else p
                    for p in plans
                )
                self._launch_bench(bench, again, result)
            else:
                self._set_notice(f'Not launched: {project} does not apply there')

        options = [
            (
                'agent',
                f"Hand {project}'s tree to the agent: it starts at {base[:12]} without its "
                'items; the agent applies them (git am -3), resolves the conflicts, then does '
                'the task',
            ),
            ('none', "Don't apply: launch nothing"),
        ]
        self.push_screen(Pick(_conflict_text(str(exc), exc.detail), options), picked)

    def _show_artifacts(self) -> None:
        hub, item = self._sessions, self._reading
        if hub is None or item is None:
            return
        self.screen.add_class('-artifacts')
        self._set_artifacts([], 'Reading session output…')
        self._set_reader_pane('content')

        def work() -> None:
            try:
                found = gather(hub.records(), item.identity, hub.store.directory)
            except Exception as exc:
                self.call_from_thread(self._set_artifacts, [], f'Cannot list session output: {exc}')
                return
            empty = 'No session output for this series yet.'
            self.call_from_thread(self._set_artifacts, found, '' if found else empty)

        self.run_worker(work, thread=True, group='artifacts', exclusive=True)

    def _set_artifacts(self, found: list[Attachment], note: str) -> None:
        self._artifacts = found
        rows = self.query_one('#artifact-list', OptionList)
        rows.clear_options()
        rows.add_options(Option(a.label) for a in found)
        if found:
            rows.highlighted = 0
            self._preview_artifact(0)
        else:
            self.query_one('#artifact-show', TextPane).set_text(note)

    def _preview_artifact(self, index: int) -> None:
        if not 0 <= index < len(self._artifacts):
            return
        chosen = self._artifacts[index]

        def work() -> None:
            text = preview(chosen)
            self.call_from_thread(self.query_one('#artifact-show', TextPane).set_text, text)

        self.run_worker(work, thread=True, group='artifact-preview', exclusive=True)

    def _hide_artifacts(self) -> None:
        self.screen.remove_class('-artifacts')
        self._set_reader_pane('content')

    def action_launch_session(self) -> None:
        """F1: the launch dialog for the open or selected series (or the workspace)."""
        hub, snapshot = self._sessions, self.snapshot
        if hub is None or snapshot is None:
            return
        item = self._reading or self._item
        workspace, overview = snapshot.workspace, self._overview

        def work() -> None:
            try:
                blocker = hub.blocker()
                if blocker is not None:
                    self.call_from_thread(self.push_screen, Notice(f'Agent sessions: {blocker}'))
                    return
                profiles, problems = hub.profiles()
                skills, skill_problems = hub.skills()
                project = item.project or '' if item is not None else ''
                situation = _situation(hub, workspace, overview, skills, project)
                problems += skill_problems
            except Exception as exc:
                self.call_from_thread(self._set_notice, f'Cannot open the session dialog: {exc}')
                return
            self.call_from_thread(self._show_launch, item, profiles, problems, situation)

        self.run_worker(work, thread=True, group='session-action')

    def _show_launch(
        self,
        item: Optional[WorkItem],
        profiles: list[Profile],
        problems: list[str],
        situation: Situation,
    ) -> None:
        target = _target(item) if item is not None else None
        series = series_context(item) if item is not None else ()
        items: dict[str, tuple[ContextItem, ...]] = {'series': series, 'workspace': ()}
        dialog = LaunchDialog(target, profiles, situation, items, problems)
        self.push_screen(dialog, self._launch_chosen)

    def launch_derived(
        self, parent: str, derived: Derived, context: Optional[ContextItem] = None
    ) -> None:
        """The launch dialog for a session derived from *parent*'s output: its series,
        *context* (the parent's artifact) in the prompt package, its tree at the derived
        start (Branches, D)."""
        hub, snapshot = self._sessions, self.snapshot
        if hub is None or snapshot is None:
            return
        workspace, overview = snapshot.workspace, self._overview

        def work() -> None:
            try:
                blocker = hub.blocker()
                if blocker is not None:
                    self.call_from_thread(self.push_screen, Notice(f'Agent sessions: {blocker}'))
                    return
                record = hub.store.record(parent)
                profiles, problems = hub.profiles()
                skills, skill_problems = hub.skills()
                situation = _situation(hub, workspace, overview, skills, record.project)
            except Exception as exc:
                self.call_from_thread(self._set_notice, f'Cannot derive a session: {exc}')
                return
            item = snapshot.item_by_identity(record.series) if record.series else None
            target = (
                _target(item)
                if item is not None
                else Target(
                    'series', record.project, record.series, record.revision, record.subject
                )
            )
            series = series_context(item) if item is not None else ()
            items = {'series': (*series, *([context] if context else [])), 'workspace': ()}
            dialog = LaunchDialog(
                target, profiles, situation, items, problems + skill_problems, derived
            )
            self.call_from_thread(self.push_screen, dialog, self._launch_chosen)

        self.run_worker(work, thread=True, group='session-action')

    def _launch_chosen(self, result: Optional[LaunchResult]) -> None:
        hub, snapshot = self._sessions, self.snapshot
        if result is None or hub is None or snapshot is None:
            return
        workspace, label = snapshot.workspace, session_label(result.target)

        def launch() -> Optional[str]:
            # Checked again with fresh counts: the dialog may have been open a while.
            target = result.target
            choice = result.choice
            if target.scope == 'series' and not choice.workbench:
                choice = dataclasses.replace(choice, workbench=self._file_in(target, choice))
            skills = hub.skills()[0]
            situation = _situation(hub, workspace, hub.overview(), skills, target.project)
            if plan(result.profile, result.choice, situation, result.items).mode == 'worktree':
                note = f'Preparing a worktree for {label} (b4 applies the series)…'
                self.call_from_thread(self._set_notice, note)
            try:
                record = hub.launch(target, result.profile, choice, situation, result.items)
            except SessionError as exc:
                if exc.base and not choice.also and not choice.agent_apply:
                    retry = dataclasses.replace(result, choice=choice)
                    self.call_from_thread(self._offer_handoff, retry, exc)
                    return None
                if not exc.detail:
                    raise
                text = f'{exc}\n\n{exc.detail.strip()}\n\nA lightweight session needs no tree.'
                self.call_from_thread(self.push_screen, Notice(text))
                return None
            self.call_from_thread(self._after_launch, record)
            return f'Launched session {record.id}: {label}'

        self._session_call(launch)

    def _file_in(self, target: Target, choice: LaunchChoice) -> str:
        """The workbench a series session is filed in (blocking): its parent's, else the
        series' own (made once: the series on the chosen base, else the project's first
        maintainer base). Without workbenches: none."""
        hub, service, snapshot = self._sessions, self._workbenches, self.snapshot
        if service is None or snapshot is None:
            return ''
        if choice.parent and hub is not None:
            try:
                parent = hub.store.record(choice.parent)
            except (OSError, ValueError):
                parent = None
            if parent is not None and parent.workbench:
                return parent.workbench
        project = snapshot.workspace.project(target.project)
        base = choice.base or (project.bases[0].branch if project and project.bases else '')
        try:
            bench = series_bench(
                service.store, target.project, target.series, target.revision, target.subject, base
            )
        except (OSError, WorkbenchError):
            return ''  # Filed nowhere: it still launches.
        return bench.id

    def _offer_handoff(self, result: LaunchResult, exc: SessionError) -> None:
        """b4 could not apply the series for a session: hand it to the agent (its tree at
        that base, without the series), or launch nothing."""
        base = exc.base

        def picked(choice: Optional[str]) -> None:
            if choice == 'agent':
                again = dataclasses.replace(result.choice, agent_apply=base)
                self._launch_chosen(dataclasses.replace(result, choice=again))
            else:
                self._set_notice('Not launched: the series does not apply there')

        title = _conflict_text(str(exc), exc.detail)
        options = [
            (
                'agent',
                f'Hand it to the agent: its tree starts at {base[:12]} without the series; it '
                'applies the patches (git am -3), resolves the conflicts, then does the task',
            ),
            ('none', "Don't apply: launch nothing"),
        ]
        self.push_screen(Pick(title, options), picked)

    # The column beside the app: changed only by the maintainer's moves, never by polling.

    def _column_session(self) -> Optional[str]:
        """The session that belongs beside the app on this tab: on Workbench, the one
        opened there (while live); elsewhere, the open series' own, else the workspace's."""
        if isinstance(self.screen, WorkbenchScreen):
            live = {v.record.id for v in self._overview.sessions if v.state in LIVE_STATES}
            return self._bench_beside if self._bench_beside in live else None
        reading = self._reading.identity if self._reading is not None else None
        return pane_for(self._overview.sessions, reading, self._workspace_beside)

    def _place_pane(self, sid: Optional[str] = None, focus: bool = False) -> None:
        """Put *sid* (or the session that belongs there) beside the app."""
        hub, pane = self._sessions, self._app_pane
        if hub is None or not pane:
            return
        target = sid or self._column_session()
        if target == self._beside and not focus:
            return
        width = self.snapshot.workspace.pane_width if self.snapshot else 90
        self._beside = target
        # A drafts editor already beside the app: the session goes above it, one column.
        above = self._draft.pane if self._draft is not None else ''

        def work() -> None:
            try:
                if target is None:
                    hub.hide(pane)
                else:
                    hub.show(target, pane, width, focus, above)
            except Exception as exc:
                self.call_from_thread(self._set_notice, f'Session pane: {exc}')
            self.call_from_thread(self._pane_placed)

        self.run_worker(work, thread=True, group='session-pane')

    def _pane_placed(self) -> None:
        self._show_overview(self._overview)  # Re-mark the drawer.
        self._sync_bindings()

    def action_focus_session(self) -> None:
        """F12: into the session beside the app (zoomed on narrow terminals)."""
        hub, sid = self._sessions, self._beside
        if hub is None or sid is None:
            return
        zoom = self._mode == 'narrow'
        self.run_worker(lambda: hub.focus(sid, zoom), thread=True, group='session-pane')

    def _after_launch(self, record: SessionRecord) -> None:
        """A session launched while reading its series joins beside the thread."""
        if self._reading is not None and record.series == self._reading.identity:
            self._place_pane(record.id)

    def action_open_session(self) -> None:
        """Enter in the drawer: the session beside the app (with its series in the reader),
        focused; outside tmux, its window."""
        if (msgid := self.selected_draft()) is not None:
            self._open_draft(msgid)
            return
        view, hub, snapshot = self._selected_view(), self._sessions, self.snapshot
        if view is None or hub is None:
            return
        record, sid = view.record, view.record.id
        if record.scope == 'workbench':
            self._open_bench_tab(record)
            return
        item = snapshot.item_by_identity(record.series) if snapshot and record.series else None
        if not self._app_pane or (record.series and item is None and record.scope != 'workspace'):
            self._session_call(lambda: hub.open(sid))  # Its series is gone: its window.
            return
        if record.scope == 'workspace' or not record.series:
            self._workspace_beside = sid
        elif self._reading is None or self._reading.identity != record.series:
            assert item is not None
            self._series_list.select_identity(item.identity)
            self._item = item
            self._open_reader(pane=False)
        self._place_pane(sid, focus=True)

    def action_stop_session(self) -> None:
        view, hub = self._selected_view(), self._sessions
        if view is None or hub is None:
            return
        sid, label = view.record.id, session_label(view.record)

        def answered(confirmed: Optional[bool]) -> None:
            if confirmed:
                self._session_call(lambda: hub.stop(sid) or f'Stopped: {label}')

        self.push_screen(Confirm(f'Stop session {sid} ({label})? [y|N]'), answered)

    def action_end_session(self) -> None:
        if (msgid := self.selected_draft()) is not None:
            self._discard_draft(msgid)
            return
        view, hub = self._selected_view(), self._sessions
        if view is None or hub is None:
            return
        sid, label = view.record.id, session_label(view.record)
        tree = view.record.worktree
        listed = tree is not None and any(w.path == tree.path for w in self._overview.worktrees)

        def ended() -> str:
            hub.end(sid)
            if listed:
                assert tree is not None
                self.call_from_thread(self._ask_remove_tree, tree.path)
            return f'Ended: {label}'

        def answered(confirmed: Optional[bool]) -> None:
            if confirmed:
                self._session_call(ended)

        self.push_screen(Confirm(f'End session {sid} ({label})? [y|N]'), answered)

    def _ask_remove_tree(self, path: str) -> None:
        """After ending a worktree session: remove its tree, or keep it (Worktrees tab)."""

        def answered(confirmed: Optional[bool]) -> None:
            if confirmed:
                self._remove_worktrees([path])

        prompt = f'Remove its worktree {path}? [y|N] (N keeps it in the Worktrees tab)'
        self.push_screen(Confirm(prompt), answered)

    def action_session_artifacts(self) -> None:
        view, hub = self._selected_view(), self._sessions
        if view is None or hub is None:
            return
        sid, label = view.record.id, session_label(view.record)

        def work() -> None:
            try:
                files = hub.artifacts(sid)
                text = '\n'.join(f'  out/{f}' for f in files) or '  No artifacts yet.'
            except OSError as exc:
                text = f'  Cannot list artifacts: {exc}'
            self.call_from_thread(self.push_screen, Notice(f'Session {sid} ({label})\n{text}'))

        self.run_worker(work, thread=True, group='session-action')

    def action_refresh(self) -> None:
        actions = self._actions
        if actions is None:
            return

        def work() -> None:
            def progress(message: str) -> None:
                self.call_from_thread(self._set_notice, message)

            try:
                result, reload = actions.refresh(progress), True
            except Exception as exc:  # Network and b4 failures are shown, not raised.
                result, reload = f'Refresh failed: {type(exc).__name__}: {exc}', False
            self.call_from_thread(self._action_done, result, reload)

        self._reload_reading = self._reading is not None
        self._set_notice('Refreshing…')
        self.run_worker(work, thread=True, group='action', exclusive=True)

    def action_track(self) -> None:
        actions, item = self._actions, self._reading or self._item
        if actions is None or item is None:
            return

        def work() -> None:
            try:
                result, reload = actions.track(item), True
            except Exception as exc:
                result, reload = f'Track failed: {exc}', False
            self.call_from_thread(self._action_done, result, reload)

        self._set_notice(f'Tracking {item.series.subject}…')
        self.run_worker(work, thread=True, group='action', exclusive=True)

    def action_watch(self) -> None:
        self._toggle_watched()

    def action_unwatch(self) -> None:
        self._toggle_watched()

    def _toggle_watched(self) -> None:
        actions, item = self._actions, self._reading or self._item
        if actions is None or item is None:
            return

        def work() -> None:
            try:
                result, reload = actions.toggle_watched(item), True
            except Exception as exc:
                result, reload = f'Watch failed: {exc}', False
            self.call_from_thread(self._action_done, result, reload)

        self.run_worker(work, thread=True, group='action', exclusive=True)

    def action_untrack(self) -> None:
        """Archive the series in b4 review after confirmation; b4 keeps the archive."""
        actions, item = self._actions, self._reading or self._item
        if actions is None or item is None:
            return

        def work() -> None:
            try:
                result, reload = actions.untrack(item), True
            except Exception as exc:
                result, reload = f'Untrack failed: {exc}', False
            self.call_from_thread(self._action_done, result, reload)

        def answered(confirmed: Optional[bool]) -> None:
            if confirmed:
                self._set_notice(f'Archiving {item.series.subject}…')
                self.run_worker(work, thread=True, group='action', exclusive=True)

        self.push_screen(Confirm('Untrack (archive in b4)? [y|N]'), answered)

    def action_mark_read(self) -> None:
        actions, item = self._actions, self._reading or self._item
        if actions is None or item is None:
            return
        if self._reading is not None and self._thread is not None:
            # Clear the reader's marks now; counts refresh with the reload below.
            self._seen_now |= item.unread_msgids
            self._refresh_tree_labels()

        def work() -> None:
            try:
                result, reload = actions.mark_read(item), True
            except Exception as exc:
                result, reload = f'Mark read failed: {exc}', False
            self.call_from_thread(self._action_done, result, reload)

        self.run_worker(work, thread=True, group='action', exclusive=True)

    def action_hide(self) -> None:
        self._toggle_hidden()

    def action_unhide(self) -> None:
        self._toggle_hidden()

    def _toggle_hidden(self) -> None:
        actions, item = self._actions, self._item
        if actions is None or item is None:
            return

        def work() -> None:
            try:
                result, reload = actions.toggle_hidden(item), True
            except Exception as exc:
                result, reload = f'Hide failed: {exc}', False
            self.call_from_thread(self._action_done, result, reload)

        self.run_worker(work, thread=True, group='action', exclusive=True)

    def _set_notice(self, notice: str) -> None:
        self._notice = notice
        if self.index is not None:
            self._update_status()

    def _action_done(self, notice: str, reload: bool) -> None:
        self._set_notice(notice)
        if reload:
            self.run_worker(self._load, thread=True, name='load', exclusive=True)

    def _update_status(self) -> None:
        assert self.index is not None
        view = next(v for v in self.index.views if v.id == self.current_view)
        status = f' b4-maintainer — {view.label} ({self.index.count(view.id)})'
        if self._actions is None:
            status += ' · fixture data'
        if self._query.pattern is not None:
            literal = ' (literal)' if self._query.literal else ''
            status += f' · /{self._query.text}{literal}'
        if self._notice:
            status += f' · {self._notice}'
        if self.index.needs_setup:
            status += ' · No subsystem profiles configured: add [[profiles]] with lists to browse'
        self._set_status(status)

    def _set_status(self, status: str) -> None:
        self._status = status
        self._status_widget.set_text(status)

    def _summary_for(self, item: Optional[WorkItem]) -> str:
        if item is None:
            return 'No series in this view.'
        s, st = item.series, item.state
        ws = self.snapshot.workspace if self.snapshot else None
        acked, reviewed, tested = s.trailer_totals()
        if item.project:
            project = item.project
        elif item.candidates:
            project = (
                f'unassigned; candidates {", ".join(item.candidates)} (confirm before tracking)'
            )
        else:
            project = 'unassigned; no configured project matches'
        flags = [
            label
            for on, label in (
                (st.to_review, 'To Review'),
                (st.unread, f'{st.unread} unread'),
                (st.stable_pending, 'Cc stable pending'),
                (st.draft, 'draft'),
            )
            if on
        ]
        flags.append(f'progress {st.progress.value}')
        lines = [
            s.subject,
            '',
            f'v{s.revision} · {len(s.patches)}/{s.expected} patches · '
            f'{"complete" if s.complete else "incomplete"} · cover {"yes" if s.has_cover else "no"}',
            f'From:     {s.author_name} <{s.author_email}>',
            f'Sent:     {s.sent:%Y-%m-%d %H:%M %z}' if s.sent else 'Sent:     unknown',
            f'Project:  {project}',
            f'Profiles: {", ".join(sorted(item.profiles)) or "none"}',
            f'Versions: {" ".join(f"v{v}" for v in item.versions)}',
            f'State:    {" · ".join(flags)}',
            f'Trailers: A:{acked} R:{reviewed} T:{tested}'
            f' (cover A:{s.cover_trailers.acked} R:{s.cover_trailers.reviewed} T:{s.cover_trailers.tested})',
        ]
        # Why ⚠: each patch's Fixes targets, and which patches Cc stable.
        for p in s.patches:
            lines += [f'Fixes:    {p.counter}/{s.expected} {target}' for target in p.fixes]
        stable = [f'{p.counter}/{s.expected}' for p in s.patches if p.stable]
        if stable:
            lines.append(f'Stable:   Cc stable in {", ".join(stable)}')
        if s.cc_stable and item.landed:
            lines.append('          landed: no longer pending')
        lines += [text for text, _ in self._sashiko_lines(item)]
        lines += ['', 'Tree evidence:']
        if not st.tree:
            lines.append('  unchecked')
        refs = {r.label: r for p in (ws.projects if ws else ()) for r in p.refs}
        for label, authority, coverage in st.tree:
            name = refs[label].name if label in refs else label
            lines.append(f'  {label} {name} ({authority.name.lower()}): {coverage.value}')
        lines += ['', 'Patches:']
        for p in s.patches:
            t = p.trailers
            marks = ''.join(
                c if n else self.symbols.absent
                for c, n in (('A', t.acked), ('R', t.reviewed), ('T', t.tested))
            )
            tags = [label for on, label in ((p.fixes, 'Fixes'), (p.stable, 'stable')) if on]
            suffix = f' [{", ".join(tags)}]' if tags else ''
            lines.append(f'  {p.counter}/{s.expected} {marks} {p.subject}{suffix}')
        if self._preview and s.preview:
            lines += ['', f'── {s.preview_label} ──', '', s.preview]
        elif s.preview:
            lines += ['', f'[p] preview {s.preview_label.lower()}']
        return printable('\n'.join(lines))

    @staticmethod
    def _sashiko_lines(item: WorkItem) -> list[tuple[str, Optional[Style]]]:
        """Summary lines, each with its severity style (None for plain text)."""
        review = item.sashiko
        if review is None:
            return [('Sashiko:  not reviewed', None)]
        counts = ', '.join(
            f'{n} {sev}'
            for sev in ('critical', 'high', 'medium', 'low')
            if (n := getattr(review, sev))
        )
        status = f'Sashiko:  {review.status}' + (f' · {counts}' if counts else '')
        lines = [(status, SEVERITY_STYLES.get(review.worst))]
        about = ' · '.join(part for part in (review.baseline, review.model) if part)
        if about:
            lines.append((f'  {about}', None))
        if review.url:
            lines.append((f'  {review.url}', None))
        total = len(review.patches)
        for patch in review.patches:
            for f in patch.findings:
                old = ' (pre-existing)' if f.preexisting else ''
                text = f'  {patch.part}/{total} {f.severity}{old}: {f.problem}'
                lines.append((text, SEVERITY_STYLES.get(f.severity)))
        return lines


def _drafted_in(thread: SeriesThread, drafted: set[str]) -> Optional[tuple[str, int, str]]:
    """(posting key, node, message-id) of the first message in the thread with a saved draft."""
    if not drafted:
        return None
    for key in post_keys(thread):
        post = find_post(thread, key)
        if post is not None and post.message.msgid in drafted:
            return key, 0, post.message.msgid
        for node, reply in enumerate(thread_replies(thread, key), 1):
            if reply.message.msgid in drafted:
                return key, node, reply.message.msgid
    return None


def _action_names(bindings: Iterable[Any]) -> Iterable[str]:
    """Action names (without arguments) of a BINDINGS list of Binding objects or tuples."""
    for binding in bindings:
        action = binding.action if isinstance(binding, Binding) else binding[1]
        yield action.split('(', 1)[0]


def _situation(
    hub: SessionHub, workspace: Workspace, overview: Overview, skills: list[Skill], project: str
) -> Situation:
    """What a launch for *project*'s series is checked against (blocking: off the UI thread)."""
    return hub.situation(workspace, overview, skills, project)


def _target(item: WorkItem) -> Target:
    s = item.series
    return Target('series', item.project or '', item.identity, s.revision, s.subject, s.source)


def _replace_rows(rows: OptionList, entries: list[tuple[str, str]], empty: str) -> None:
    """Replace a drawer list's rows, keeping the highlight on the same entry."""
    option = rows.highlighted_option
    keep, position = (option.id if option is not None else None), rows.highlighted or 0
    rows.clear_options()
    if not entries:
        rows.add_option(Option(empty, disabled=True))
        return
    rows.add_options(Option(text, id=key) for key, text in entries)
    keys = [key for key, _text in entries]
    rows.highlighted = keys.index(keep) if keep in keys else min(position, len(keys) - 1)


def _conflict_text(message: str, detail: str, lines: int = 12, width: int = 110) -> str:
    """A conflict for a choice: the message, the end of b4's output, the question."""
    tail = [line[:width] for line in detail.strip().splitlines()[-lines:]]
    return '\n'.join([message, '', *tail, '', 'What now?'] if tail else [message, '', 'What now?'])


def _target_project(key: str) -> str:
    """The project of a branch key (`base:<project>:<branch>`, `prep:...`)."""
    return key.split(':', 2)[1]


def _target_label(key: str, targets: Sequence[tuple[str, str]]) -> str:
    """A branch as the maintainer reads it: `branch`, or `project: branch` when the
    choices span several projects."""
    branch = dict(targets)[key]
    if len({_target_project(k) for k, _b in targets}) > 1:
        return f'{_target_project(key)}: {branch}'
    return branch


NEW_BENCH = '+new'  # w's "new workbench" choice (workbench ids never hold '+').


def _bench_situation(
    hub: SessionHub,
    workspace: Workspace,
    overview: Overview,
    skills: list[Skill],
    plans: Sequence[TreePlan],
) -> Situation:
    """What a workbench launch is checked against: as for its first project, and refused
    when any of its projects cannot have a tree."""
    situation = hub.situation(workspace, overview, skills, plans[0].project, bases=False)
    reason = next((r for r in (hub.worktree_reason(p.project) for p in plans) if r), '')
    return dataclasses.replace(situation, worktree_reason=reason)


def _tree_line(plan: TreePlan) -> str:
    """A tree as the launch dialog lists it."""
    where = plan.base or 'the base b4 chooses'
    held = '; '.join(i.label for i in plan.items) or 'nothing applied'
    return f'{plan.project} on {where}: {held}'


def _own_bindings(node: object) -> set[tuple[str, str]]:
    """(key, action) of the bindings this app's own classes declare on *node*'s class."""
    found: set[tuple[str, str]] = set()
    for cls in type(node).__mro__:
        if cls.__module__.startswith('b4_maintainer'):
            for b in cls.__dict__.get('BINDINGS', ()):
                if isinstance(b, Binding):
                    found.update((key.strip(), b.action) for key in b.key.split(','))
    return found

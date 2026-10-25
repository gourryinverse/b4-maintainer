# SPDX-License-Identifier: GPL-2.0-or-later
"""Browse screen (Views | Series | Summary) and pre-apply reader, with a session drawer."""

import pathlib
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional, Protocol

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
from textual.screen import ModalScreen
from textual.scroll_view import ScrollView
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Input, OptionList, Static, TextArea
from textual.widgets.option_list import Option
from textual.worker import Worker, WorkerState

from .adapters.b4mbox import load_thread
from .adapters.drafts import DraftStore, SavedDraft
from .branchscreen import Branches, BranchesScreen
from .drawer import drawer_line, session_label, session_rows, tabs_line, worktree_row
from .launchdialog import LaunchDialog, LaunchResult
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
)
from .rows import DEFAULT_SYMBOLS, Segments, Symbols, heading, row_segments
from .search import compile_query, match_texts, texts
from .sessions.hub import Overview, SessionError, SessionHub
from .sessions.launch import Situation, Target, plan
from .sessions.pane import pane_for
from .sessions.policy import ContextItem
from .sessions.profiles import Profile
from .sessions.records import SessionRecord
from .sessions.skills import Skill
from .sessions.store import SessionView
from .sessions.trees import TreeState
from .views import ViewIndex
from .widgets import Block, KeyBar, TextPane, printable
from .workspace import Snapshot, Workspace

SESSION_ACTIONS = frozenset({'open_session', 'stop_session', 'end_session', 'session_artifacts'})
WORKTREE_ACTIONS = frozenset({'mark_worktree', 'remove_worktrees'})
DRAWER_ACTIONS = frozenset({'toggle_drawer', 'back', 'cycle_pane', 'quit', 'focus_session'})
LIVE_STATES = frozenset({'approval', 'waiting', 'working', 'starting'})
COUNTED_LIVE = LIVE_STATES | {'preparing'}  # Limits count a session from its preparation.
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
    Screen.-narrow.-reading #tree, Screen.-narrow.-reading #content,
    Screen.-narrow.-reading #draft { display: none; }
    Screen.-narrow.-rpane-tree #tree { display: block; width: 1fr; }
    Screen.-narrow.-rpane-content #content { display: block; width: 1fr; }
    Screen.-narrow.-rpane-draft #draft { display: block; width: 1fr; }
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
        Binding('v', 'toggle_views', 'views'),
        # Priority so the footer shows this hint instead of Screen's hidden tab binding.
        Binding('tab', 'cycle_pane(1)', 'next pane', priority=True),
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
        Binding('p', 'toggle_preview', 'preview'),
        Binding('slash', 'search', 'search', key_display='/'),
        # $ tracks, or untracks (archives in b4) after confirmation; t also tracks.
        Binding('dollar_sign', 'track', 'track', key_display='$'),
        Binding('dollar_sign', 'untrack', 'untrack', key_display='$'),
        # On discussions $ follows the conversation instead (client state, no prompt).
        Binding('dollar_sign', 'watch', 'watch', key_display='$'),
        Binding('dollar_sign', 'unwatch', 'unwatch', key_display='$'),
        Binding('t', 'track', 'track', show=False),
        # One key; the enabled action (and footer label) follows the item's state.
        Binding('x', 'hide', 'hide'),
        Binding('x', 'unhide', 'unhide'),
        Binding('f5', 'refresh', 'refresh', key_display='F5'),
        Binding('r', 'reply', 'reply'),
        Binding('R', 'mark_read', 'read all'),
        Binding('b', 'branches', 'branches'),
        Binding('f1', 'launch_session', 'session', key_display='F1'),
        Binding('f12', 'focus_session', 'to session', key_display='F12'),
        Binding('grave_accent', 'toggle_drawer', 'drawer', key_display='`'),
        Binding('Q', 'quit', 'quit'),
    ]

    def __init__(
        self,
        loader: Callable[[], Snapshot],
        sessions: Optional[SessionHub] = None,
        branches: Optional[Branches] = None,
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
        self._thread_loader = thread_loader
        # Agent sessions: polled for the drawer; only the maintainer's keys act on them.
        self._sessions = sessions
        self._branches = branches  # The Branches screen's data and actions (live only).
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

    def compose(self) -> ComposeResult:
        yield StatusLine(self._status, id='status')
        with Horizontal(id='main'):
            yield OptionList(id='views')
            yield SeriesList(self.symbols, id='series')
            yield TextPane(self._summary, id='summary')
        with Horizontal(id='reader'):
            yield OptionList(id='tree')
            yield TextPane(id='content')
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
        snapshot = self._loader()
        index = ViewIndex(snapshot.workspace, snapshot.items)
        self.call_from_thread(self._apply_snapshot, snapshot, index)

    def _apply_snapshot(self, snapshot: Snapshot, index: ViewIndex) -> None:
        # A reload keeps the view and selection when they still exist.
        previous = self._series_list.highlighted_item if self.snapshot else None
        position = self._series_list.cursor
        view = self.current_view if any(v.id == self.current_view for v in index.views) else 'all'
        self.snapshot = snapshot
        self.index = index
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
            self._sync_bindings()

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
        if isinstance(self.screen, BranchesScreen):
            return action == 'quit'  # The screen has its own keys.
        if action == 'branches':
            return (
                self._branches is not None
                and self._reading is None
                and not self._drawer_focused()
                and self.snapshot is not None
            )
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
            return self._actions is not None and not reading
        if action == 'reply':
            if self._actions is None or self._draft is not None:
                return False
            # In the series list: the selected series' cover letter (or first patch).
            return self._reply_msgid() is not None if reading else self._item is not None
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
            name for name in ('tree', 'content', 'draft') if self.query_one(f'#{name}').display
        )

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
        {'tree': self._tree, 'content': self._content, 'draft': self._draft_area}[pane].focus()
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
            thread = with_sashiko(self._thread_loader(item.series), item.sashiko, self._bots())
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
            if reply_now or adopt:
                self._resume_saved_draft()
            else:
                self._set_notice('Saved draft here · r resumes · ctrl+g discards')
                self._sync_bindings()
            return
        self._render_tree((keys[0], 0) if keys else ('', 0))
        if reply_now:
            self.action_reply()  # On the first posting: the cover letter, else the first patch.

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
        actions, item, msgid = self._actions, self._reading, self._reply_msgid()
        if actions is None or item is None or msgid is None:
            return
        quote = None
        if msgid.startswith(SASHIKO_PREFIX):
            # Not on the list: answer the patch, quoting Sashiko's review.
            reply = self._current_reply()
            assert reply is not None
            source = item.sashiko.url if item.sashiko and item.sashiko.url else 'sashiko.dev'
            quoted = '\n'.join(f'> {line}'.rstrip() for line in reply.message.body.splitlines())
            quote = f'Sashiko ({source}) reported:\n{quoted}\n\n'
            msgid = msgid.removeprefix(SASHIKO_PREFIX)
        self._begin_reply(item, msgid, quote)

    def _resume_saved_draft(self) -> None:
        saved, self._saved_draft = self._saved_draft, None
        if saved is not None:
            self._begin_reply(saved[0], saved[1], None)

    def _begin_reply(self, item: WorkItem, msgid: str, quote: Optional[str]) -> None:
        """Load the message to answer (and any saved draft) off the UI thread."""
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
            except Exception as exc:
                self.call_from_thread(self._set_notice, f'Cannot reply: {exc}')
                return
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
            self.call_from_thread(self._compose_reply, item, msgid, target, quote, saved)

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
    ) -> None:
        """Open a draft beside the thread: a saved one, else b4's quote of the message."""
        if not self._still_reading(item):
            return  # The reader closed while the message loaded.
        quote = reply_quote(target.lmsg) if quote is None else quote
        if saved is not None:
            quote = saved.quote
        self._draft = Draft(item, msgid, target, quote)
        self._draft_area.load_text(saved.text if saved is not None else quote)
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

    def action_branches(self) -> None:
        """b: the Branches screen (maintainer bases, session trees, prep branches)."""
        if self._branches is not None and self.snapshot is not None:
            self.push_screen(BranchesScreen(self._branches, self.snapshot.workspace))

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

    def _launch_chosen(self, result: Optional[LaunchResult]) -> None:
        hub, snapshot = self._sessions, self.snapshot
        if result is None or hub is None or snapshot is None:
            return
        workspace, label = snapshot.workspace, session_label(result.target)

        def launch() -> Optional[str]:
            # Checked again with fresh counts: the dialog may have been open a while.
            target = result.target
            skills = hub.skills()[0]
            situation = _situation(hub, workspace, hub.overview(), skills, target.project)
            if plan(result.profile, result.choice, situation, result.items).mode == 'worktree':
                note = f'Preparing a worktree for {label} (b4 applies the series)…'
                self.call_from_thread(self._set_notice, note)
            try:
                record = hub.launch(target, result.profile, result.choice, situation, result.items)
            except SessionError as exc:
                if not exc.detail:
                    raise
                text = f'{exc}\n\n{exc.detail.strip()}\n\nA lightweight session needs no tree.'
                self.call_from_thread(self.push_screen, Notice(text))
                return None
            self.call_from_thread(self._after_launch, record)
            return f'Launched session {record.id}: {label}'

        self._session_call(launch)

    # The column beside the app: changed only by the maintainer's moves, never by polling.

    def _place_pane(self, sid: Optional[str] = None, focus: bool = False) -> None:
        """Put *sid* (or the session that belongs there) beside the app."""
        hub, pane = self._sessions, self._app_pane
        if hub is None or not pane:
            return
        reading = self._reading.identity if self._reading is not None else None
        target = sid or pane_for(self._overview.sessions, reading, self._workspace_beside)
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
        item = snapshot.item_by_identity(record.series) if snapshot and record.series else None
        if not self._app_pane or (record.scope == 'series' and item is None):
            self._session_call(lambda: hub.open(sid))
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
    return Situation(
        hub.models(workspace.models),
        workspace.session_limits,
        live=sum(v.state in COUNTED_LIVE for v in overview.sessions),
        live_builds=sum(
            v.state in COUNTED_LIVE and v.record.mode == 'worktree' for v in overview.sessions
        ),
        worktrees=len(overview.worktrees),
        worktree_bytes=overview.worktree_bytes,
        worktree_reason=(reason := hub.worktree_reason(project)),
        skills=skills,
        bases=() if reason else tuple(hub.bases(project)),
    )


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

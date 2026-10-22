# SPDX-License-Identifier: GPL-2.0-or-later
"""Browse screen (Views | Series | Summary) and pre-apply reader, with a session drawer."""

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
from textual.containers import Horizontal
from textual.content import Content
from textual.geometry import Size
from textual.markup import escape
from textual.message import Message
from textual.reactive import Reactive
from textual.screen import ModalScreen
from textual.scroll_view import ScrollView
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Footer, Input, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from .adapters.b4mbox import load_thread
from .layout import Breakpoints, Mode, layout_mode
from .model import Reply, Series, SeriesThread, WorkItem
from .reader import (
    TreeRow,
    content_title,
    find_post,
    has_long_quote,
    initially_expanded,
    message_blocks,
    post_keys,
    root_blocks,
    thread_replies,
    tree_rows,
)
from .rows import DEFAULT_SYMBOLS, Segments, Symbols, heading, row_segments
from .search import compile_query, match_texts, texts
from .views import ViewIndex
from .widgets import TextPane, printable
from .workspace import Snapshot

FIXES_STYLE = Style(color='dark_orange')
UNREAD_STYLE = Style(color='green')
CURSOR_STYLE = Style(reverse=True)
HEADING_STYLE = Style(bold=True)
# Author names by MAINTAINERS role; Sashiko findings by worst severity.
ROLE_STYLES = {
    'maintainer': Style(color='green'),
    'reviewer': Style(color='blue'),
    'sev-critical': Style(color='red', bold=True),
    'sev-high': Style(color='red'),
    'sev-medium': Style(color='yellow'),
    'sev-low': Style(dim=True),
}


class AgentLauncher(Protocol):
    def launch(self, request: Any) -> None: ...


class SourceActions(Protocol):
    """Explicit maintainer actions on a live workspace; both block, so run in workers."""

    def refresh(self, progress: Callable[[str], None]) -> str: ...

    def track(self, item: WorkItem) -> str: ...

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
        style = Style()
        if item.state.fixes_pending:
            style = FIXES_STYLE
        elif item.state.unread:
            style = UNREAD_STYLE
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

    def __init__(self) -> None:
        super().__init__(self.PROMPT)


@dataclass
class Draft:
    """A reply being written beside the thread, to one message."""

    item: WorkItem
    msgid: str
    target: Any  # SourceActions.prepare_reply result: lmsg, checkout, mail flags
    quote: str


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
    #drawer-panel { height: 40%; display: none; border-top: solid $accent; padding: 0 1; }
    Screen.-drawer-open #drawer-panel { display: block; }
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
        Binding('enter', 'open_reader', 'read'),
        Binding('escape', 'back', 'back'),
        Binding('left_square_bracket', 'next_post(-1)', 'prev', key_display='['),
        Binding('right_square_bracket', 'next_post(1)', 'next', key_display=']'),
        Binding('n', 'next_comment', 'next comment'),
        Binding('minus', 'toggle_replies', 'replies', key_display='-'),
        Binding('z', 'toggle_quotes', 'quotes'),
        Binding('p', 'toggle_preview', 'preview'),
        Binding('slash', 'search', 'search', key_display='/'),
        Binding('t', 'track', 'track'),
        # One key; the enabled action (and footer label) follows the item's state.
        Binding('x', 'hide', 'hide'),
        Binding('x', 'unhide', 'unhide'),
        # r refreshes while browsing and replies while reading.
        Binding('r', 'refresh', 'refresh'),
        Binding('r', 'reply', 'reply'),
        Binding('R', 'mark_read', 'read all'),
        Binding('grave_accent', 'toggle_drawer', 'drawer', key_display='`'),
        Binding('Q', 'quit', 'quit'),
    ]

    def __init__(
        self,
        loader: Callable[[], Snapshot],
        launcher: Optional[AgentLauncher] = None,
        breakpoints: Optional[Breakpoints] = None,
        symbols: Symbols = DEFAULT_SYMBOLS,
        thread_loader: Callable[[Series], SeriesThread] = load_thread,
        actions: Optional[SourceActions] = None,
    ) -> None:
        super().__init__()
        self._loader = loader
        self._actions = actions
        self._notice = ''
        self._query = compile_query('')
        self._matches: Optional[frozenset[str]] = None
        # Rebuilt per snapshot so each keystroke only runs the regex and set intersections.
        self._texts: list[tuple[str, str]] = []
        self._view_members: dict[str, frozenset[str]] = {}
        self._item_keys: Optional[tuple[Optional[bool], Optional[bool]]] = None
        # Replies marked seen while reading; counts refresh when the reader closes.
        self._seen_now: set[str] = set()
        self._draft: Optional[Draft] = None
        self._thread_loader = thread_loader
        # Held for explicit maintainer launches; passive actions never call it.
        self.launcher = launcher
        self._breakpoints = breakpoints
        self.symbols = symbols
        self.snapshot: Optional[Snapshot] = None
        self.index: Optional[ViewIndex] = None
        self.current_view = 'all'
        self._summary = 'Loading…'
        self._status = ' b4-maintainer — loading'
        self._drawer = ''
        self._mode: Mode = 'wide'
        self._preview = False
        self._item: Optional[WorkItem] = None
        self._reading: Optional[WorkItem] = None
        self._thread: Optional[SeriesThread] = None
        self._post_key = ''
        self._node = 0
        self._expanded: set[str] = set()
        self._rows: list[TreeRow] = []
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
        yield Static('', id='drawer-panel', markup=False)
        yield Static('', id='drawer', markup=False)
        yield Input(id='search', placeholder='/ regex over subject and author')
        yield Footer()

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
        self._apply_layout(self.size.width)
        self._series_list.focus()
        self.run_worker(self._load, thread=True, name='load', exclusive=True)

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
        self._drawer = self._drawer_line()
        self.query_one('#drawer', Static).update(self._drawer)
        self.query_one('#drawer-panel', Static).update(self._drawer_panel())
        self.select_view(view, force=True)
        if previous is not None:
            self._series_list.select_identity(previous.identity, fallback=position)

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
        if event.option_list.id == 'tree':
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
        # Rebuilding the footer is costly; only do it when a per-item key's state flips.
        keys = (self.check_action('track', ()), self.check_action('hide', ()))
        if keys != self._item_keys:
            self._item_keys = keys
            self.refresh_bindings()

    def _show_summary(self) -> None:
        self._summary = self._summary_for(self._item)
        self._summary_pane.set_text(self._summary)

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
        if isinstance(self.screen, Confirm):
            return False  # The prompt owns every key while it is open.
        narrow = self._mode == 'narrow'
        summary_open = self.screen.has_class('-show-summary')
        reading = self._reading is not None
        if action == 'toggle_views':
            return not reading and self._mode == 'medium'
        if action == 'cycle_pane':
            return not narrow or (reading and self._draft is not None)
        if action in ('send_draft', 'discard_draft'):
            return self._draft is not None
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
            return (
                self._actions is not None
                and self._draft is None
                and self._reply_msgid() is not None
            )
        if action == 'mark_read':
            return self._actions is not None and (self._reading or self._item) is not None
        if action in ('hide', 'unhide'):
            item = self._item
            if self._actions is None or reading or item is None:
                return False
            return item.hidden == (action == 'unhide')
        if action == 'track':
            item = self._item
            return (
                self._actions is not None
                and not reading
                and item is not None
                and not item.state.to_review
            )
        if action in ('next_post', 'next_comment', 'toggle_quotes', 'toggle_replies'):
            if not reading or self._thread is None:
                return False
            if action == 'next_post':
                return len(post_keys(self._thread)) > 1
            if action == 'toggle_replies':
                return bool(self._post_key and thread_replies(self._thread, self._post_key))
            if action == 'next_comment':
                return any(thread_replies(self._thread, k) for k in post_keys(self._thread))
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

    def _set_reader_pane(self, pane: str) -> None:
        self._reader_pane = pane
        for name in ('tree', 'content', 'draft'):
            self.screen.set_class(name == pane, f'-rpane-{name}')
        {'tree': self._tree, 'content': self._content, 'draft': self._draft_area}[pane].focus()
        self.refresh_bindings()

    def action_open_reader(self) -> None:
        item = self._item
        if item is None:
            return
        self._reading, self._thread, self._post_key = item, None, ''
        self.screen.add_class('-reading')
        self._tree.clear_options()
        self._rows, self._expanded, self._node = [], set(), 0
        self._content.border_title = ''
        self._content.set_text('Loading thread…')
        self._set_status(f' b4-maintainer — Reading: {item.series.subject} v{item.series.revision}')
        self._set_reader_pane('tree')
        self.run_worker(
            lambda: self._load_thread(item), thread=True, group='thread', exclusive=True
        )

    def _load_thread(self, item: WorkItem) -> None:
        try:
            thread, error = self._thread_loader(item.series), None
        except Exception as exc:  # Shown to the maintainer; mail is untrusted input.
            thread, error = None, f'{type(exc).__name__}: {exc}'
        self.call_from_thread(self._apply_thread, item, thread, error)

    def _apply_thread(
        self, item: WorkItem, thread: Optional[SeriesThread], error: Optional[str]
    ) -> None:
        if self._reading is not item:
            return  # Closed or reopened on another series while loading.
        if thread is None:
            self._content.set_text(f'Could not load thread: {error}')
            return
        self._thread = thread
        self._expanded = initially_expanded(thread, self._unread())
        keys = post_keys(thread)
        self._render_tree((keys[0], 0) if keys else ('', 0))

    def _tree_rows(self) -> list[TreeRow]:
        assert self._thread is not None and self._reading is not None
        return tree_rows(
            self._thread, self._reading, self._expanded, self._unread(), self.symbols.absent
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
        actions, item, msgid = self._actions, self._reading, self._reply_msgid()
        if actions is None or item is None or msgid is None:
            return

        def work() -> None:
            try:
                target = actions.prepare_reply(item, msgid)
            except Exception as exc:
                self.call_from_thread(self._set_notice, f'Cannot reply: {exc}')
                return
            self.call_from_thread(self._compose_reply, item, msgid, target)

        self.run_worker(work, thread=True, group='action', exclusive=True)

    def _compose_reply(self, item: WorkItem, msgid: str, target: Any) -> None:
        """Open a draft beside the thread, starting from b4's quote of the message."""
        if self._reading is not item:
            return  # The reader closed while the message loaded.
        quote = reply_quote(target.lmsg)
        self._draft = Draft(item, msgid, target, quote)
        self._draft_area.load_text(quote)
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
        text = self._draft_area.text
        if text == draft.quote:
            self._set_notice('No changes to send')
            return
        preview_and_send(
            self,
            draft.target.lmsg,
            text,
            email_dryrun=draft.target.email_dryrun,
            patatt_sign=draft.target.patatt_sign,
            on_sent=lambda _msg: self._draft_sent(draft),
            on_edit=lambda _text: self._set_reader_pane('draft'),
        )

    def _draft_sent(self, draft: Draft) -> None:
        actions = self._actions
        self._close_draft()
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
        self._confirm_discard(then=None)

    def _confirm_discard(self, then: Optional[Callable[[], None]]) -> None:
        def answered(confirmed: Optional[bool]) -> None:
            if confirmed:
                self._close_draft()
                if then is not None:
                    then()

        self.push_screen(Confirm('Discard draft? [y|N]'), answered)

    def _close_draft(self) -> None:
        self._draft = None
        self._draft_area.load_text('')
        self.screen.remove_class('-drafting')
        if self._reading is not None and self._reader_pane == 'draft':
            self._set_reader_pane('tree')
        self.refresh_bindings()

    def action_cycle_pane(self, delta: int) -> None:
        """Tab/Shift-Tab: next pane; one pane at a time on narrow terminals."""
        if self._mode == 'narrow' and self._reading is not None:
            panes = ['tree', 'content'] + (['draft'] if self._draft is not None else [])
            here = panes.index(self._reader_pane) if self._reader_pane in panes else 0
            self._set_reader_pane(panes[(here + delta) % len(panes)])
        elif delta > 0:
            self.screen.focus_next()
        else:
            self.screen.focus_previous()

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
        self._show_current()

    def _show_current(self) -> None:
        if self._thread is None or self._reading is None:
            return
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
        self.refresh_bindings()

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

    def action_toggle_replies(self) -> None:
        """Expand or collapse the replies of the posting under the cursor."""
        key = self._post_key
        if key in self._expanded:
            self._expanded.discard(key)
            self._render_tree((key, 0))
        else:
            self._expanded.add(key)
            self._render_tree((key, self._node))

    def _select(self, target: tuple[str, int]) -> None:
        """Highlight a posting or reply, expanding its posting when a reply is hidden."""
        key, node = target
        if node and key not in self._expanded:
            self._expanded.add(key)
            self._render_tree(target)
            return
        positions = {(row.key, row.node): n for n, row in enumerate(self._rows)}
        if target in positions:
            self._tree.highlighted = positions[target]

    def action_toggle_quotes(self) -> None:
        self._expand_quotes = not self._expand_quotes
        self._show_current()

    def _close_reader(self) -> None:
        self.workers.cancel_group(self, 'thread')
        self._reading, self._thread, self._post_key = None, None, ''
        self._node, self._rows, self._expanded = 0, [], set()
        self.screen.remove_class('-reading')
        for name in ('tree', 'content', 'draft'):
            self.screen.remove_class(f'-rpane-{name}')
        self._series_list.focus()
        if self.index is not None:
            self._update_status()
        self.refresh_bindings()
        if self._seen_now:
            self._seen_now = set()
            self.run_worker(self._load, thread=True, name='load', exclusive=True)

    async def action_quit(self) -> None:
        """Q and Textual's ctrl+q both ask before exiting."""
        if not isinstance(self.screen, ConfirmQuit):
            self.push_screen(ConfirmQuit(), self._quit_confirmed)

    def _quit_confirmed(self, confirmed: Optional[bool]) -> None:
        if confirmed:
            self.exit()

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
        self.screen.toggle_class('-drawer-open')

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
        actions, item = self._actions, self._item
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
                (st.fixes_pending, 'Fixes pending'),
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
        lines += self._sashiko_lines(item)
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
            lines.append(f'  {p.counter}/{s.expected} {marks} {p.subject}')
        if self._preview and s.preview:
            lines += ['', f'── {s.preview_label} ──', '', s.preview]
        elif s.preview:
            lines += ['', f'[p] preview {s.preview_label.lower()}']
        return printable('\n'.join(lines))

    @staticmethod
    def _sashiko_lines(item: WorkItem) -> list[str]:
        review = item.sashiko
        if review is None:
            return ['Sashiko:  not reviewed']
        counts = ', '.join(
            f'{n} {sev}'
            for sev in ('critical', 'high', 'medium', 'low')
            if (n := getattr(review, sev))
        )
        lines = [f'Sashiko:  {review.status}' + (f' · {counts}' if counts else '')]
        about = ' · '.join(part for part in (review.baseline, review.model) if part)
        if about:
            lines.append(f'  {about}')
        if review.url:
            lines.append(f'  {review.url}')
        total = len(review.patches)
        for patch in review.patches:
            for f in patch.findings:
                old = ' (pre-existing)' if f.preexisting else ''
                lines.append(f'  {patch.part}/{total} {f.severity}{old}: {f.problem}')
        return lines

    def _drawer_line(self) -> str:
        assert self.snapshot is not None
        sessions = self.snapshot.workspace.sessions
        if not sessions:
            return ' No agent sessions'
        s = sessions[0]
        item = self.snapshot.item_by_identity(s.series)
        target = f'{item.series.subject} v{item.series.revision}' if item else s.series
        more = f' (+{len(sessions) - 1})' if len(sessions) > 1 else ''
        approvals = (
            f' · {s.approvals} approval{"s" if s.approvals != 1 else ""}' if s.approvals else ''
        )
        return f' ◆ {s.id}: {target} @ {s.project or "?"} · {s.backend} · {s.state}{approvals}{more}   [`] open'

    def _drawer_panel(self) -> str:
        assert self.snapshot is not None
        sessions = self.snapshot.workspace.sessions
        if not sessions:
            return 'No agent sessions. Sessions start only on explicit maintainer request.'
        s = sessions[0]
        return (
            f'◆ Session {s.id} · {s.backend} · {s.state}\n'
            'Fixture session: no conversation, approvals, or artifacts recorded.'
        )

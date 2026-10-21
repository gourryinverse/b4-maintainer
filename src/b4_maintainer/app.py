# SPDX-License-Identifier: GPL-2.0-or-later
"""Browse screen (Views | Series | Summary) and pre-apply reader, with a session drawer."""

from typing import Any, Callable, Optional, Protocol

from b4._textwidth import pad_display
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
from textual.widgets import Footer, OptionList, Static
from textual.widgets.option_list import Option

from .adapters.b4mbox import load_thread
from .layout import Breakpoints, Mode, layout_mode
from .model import Series, SeriesThread, WorkItem
from .reader import (
    content_title,
    has_long_quote,
    message_blocks,
    post_keys,
    post_label,
    reply_labels,
    root_blocks,
    root_label,
    thread_replies,
)
from .rows import DEFAULT_SYMBOLS, Segments, Symbols, row_segments
from .views import ViewIndex
from .widgets import TextPane, printable
from .workspace import Snapshot

FIXES_STYLE = Style(color='dark_orange')
UNREAD_STYLE = Style(color='green')
CURSOR_STYLE = Style(reverse=True)
# Author names by MAINTAINERS role.
ROLE_STYLES = {'maintainer': Style(color='green'), 'reviewer': Style(color='blue')}


class AgentLauncher(Protocol):
    def launch(self, request: Any) -> None: ...


class SourceActions(Protocol):
    """Explicit maintainer actions on a live workspace; both block, so run in workers."""

    def refresh(self, progress: Callable[[str], None]) -> str: ...

    def track(self, item: WorkItem) -> str: ...

    def toggle_hidden(self, item: WorkItem) -> str: ...


class SeriesList(ScrollView, can_focus=True):
    """Virtual list: renders only visible rows; switching views swaps a tuple."""

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
        self.cursor = 0
        self.virtual_size = Size(0, len(items))
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

    def render_line(self, y: int) -> Strip:
        width = self.scrollable_content_region.width
        index = self.scroll_offset.y + y
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
        height = self.scrollable_content_region.height
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
        self._set_cursor(self.cursor + direction * max(1, self.scrollable_content_region.height))

    def action_jump(self, index: int) -> None:
        self._set_cursor(len(self._items) - 1 if index < 0 else index)

    def on_click(self, event: events.Click) -> None:
        self._set_cursor(self.scroll_offset.y + event.y)


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


class ConfirmQuit(ModalScreen[bool]):
    """Quit? [y|N]: only y confirms; any other key cancels."""

    PROMPT = 'Quit? [y|N]'
    DEFAULT_CSS = """
    ConfirmQuit { align: center middle; }
    ConfirmQuit > Static {
        width: auto; height: auto; padding: 0 2;
        border: solid $accent; background: $panel;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static(self.PROMPT, markup=False)

    def on_key(self, event: events.Key) -> None:
        event.stop()
        event.prevent_default()
        self.dismiss(event.key == 'y')


class MaintainerApp(App[None]):
    TITLE = 'b4-maintainer'
    CSS = """
    #status { height: 1; background: $primary-darken-2; }
    #main { height: 1fr; }
    #views, #series, #summary, #posts, #thread, #content {
        border: none; border-top: solid $panel-lighten-2;
    }
    #views:focus, #series:focus, #summary:focus, #posts:focus, #thread:focus, #content:focus {
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
    #posts { width: 18; }
    #thread { width: 44; }
    #content { width: 1fr; padding: 0 1; scrollbar-gutter: stable; }
    Screen.-medium #posts { display: none; }
    Screen.-narrow.-reading #posts, Screen.-narrow.-reading #thread,
    Screen.-narrow.-reading #content { display: none; }
    Screen.-narrow.-rpane-posts #posts { display: block; width: 1fr; }
    Screen.-narrow.-rpane-thread #thread { display: block; width: 1fr; }
    Screen.-narrow.-rpane-content #content { display: block; width: 1fr; }
    Screen.-reading #main { display: none; }
    Screen.-reading #reader { display: block; }
    #drawer { height: 1; background: $panel; }
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
        Binding('tab', 'focus_next', 'next pane', priority=True),
        Binding('enter', 'open_reader', 'read'),
        Binding('escape', 'back', 'back'),
        Binding('left_square_bracket', 'next_post(-1)', 'prev', key_display='['),
        Binding('right_square_bracket', 'next_post(1)', 'next', key_display=']'),
        Binding('n', 'next_comment', 'next comment'),
        Binding('z', 'toggle_quotes', 'quotes'),
        Binding('p', 'toggle_preview', 'preview'),
        Binding('t', 'track', 'track'),
        # One key; the enabled action (and footer label) follows the item's state.
        Binding('x', 'hide', 'hide'),
        Binding('x', 'unhide', 'unhide'),
        Binding('r', 'refresh', 'refresh'),
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
        self._item_keys: Optional[tuple[Optional[bool], Optional[bool]]] = None
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
        self._pending_node: Optional[int] = None
        self._expand_quotes = False
        self._reader_pane = 'posts'

    def compose(self) -> ComposeResult:
        yield StatusLine(self._status, id='status')
        with Horizontal(id='main'):
            yield OptionList(id='views')
            yield SeriesList(self.symbols, id='series')
            yield TextPane(self._summary, id='summary')
        with Horizontal(id='reader'):
            yield OptionList(id='posts')
            yield OptionList(id='thread')
            yield TextPane(id='content')
        yield Static('', id='drawer-panel', markup=False)
        yield Static('', id='drawer', markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self._series_list = self.query_one(SeriesList)
        self._views = self.query_one('#views', OptionList)
        self._summary_pane = self.query_one('#summary', TextPane)
        self._views.border_title = 'Views'
        self._series_list.border_title = 'Series'
        self._summary_pane.border_title = 'Summary'
        self._posts = self.query_one('#posts', OptionList)
        self._thread_list = self.query_one('#thread', OptionList)
        self._content = self.query_one('#content', TextPane)
        self._posts.border_title = 'Patches'
        self._thread_list.border_title = 'Thread'
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
        views.add_options(
            # Titles and option prompts are markup; labels are configured text.
            Option(escape(f'{pad_display(v.label, 21)}{index.count(v.id):>5}'), id=v.id)
            for v in index.views
        )
        self._drawer = self._drawer_line()
        self.query_one('#drawer', Static).update(self._drawer)
        self.query_one('#drawer-panel', Static).update(self._drawer_panel())
        self.select_view(view, force=True)
        if previous is not None:
            self._series_list.select_identity(previous.identity, fallback=position)

    def select_view(self, view_id: str, force: bool = False) -> None:
        if self.index is None or (view_id == self.current_view and not force):
            return
        self.current_view = view_id
        multi = len(self.index.workspace.projects) > 1
        project_width = 5 if multi and not view_id.startswith('project:') else 0
        self._series_list.set_items(self.index.items(view_id), project_width)
        views = self._views
        position = views.get_option_index(view_id)
        if views.highlighted != position:
            views.highlighted = position
        self._update_status()

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_list.id == 'thread':
            self._show_node(event.option_index)
        elif not event.option_id:
            return
        elif event.option_list.id == 'posts':
            self._show_post(event.option_id)
        else:
            self.select_view(event.option_id)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        # Enter descends: patch list -> thread -> content.
        if event.option_list.id == 'posts':
            self._set_reader_pane('thread')
        elif event.option_list.id == 'thread':
            self._set_reader_pane('content')

    def on_series_list_highlighted(self, event: SeriesList.Highlighted) -> None:
        self._item = event.item
        self._show_summary()
        # Rebuilding the footer is costly; only do it when a per-item key's state flips.
        keys = (
            (self.check_action('track', ()), self.check_action('hide', ()))
            if self.screen_stack
            else None
        )
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
            self._set_reader_pane('posts' if self._mode == 'narrow' else self._reader_pane)
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
        if isinstance(self.screen, ConfirmQuit):
            return False  # The prompt owns every key while it is open.
        narrow = self._mode == 'narrow'
        summary_open = self.screen.has_class('-show-summary')
        reading = self._reading is not None
        if action == 'toggle_views':
            return not reading and self._mode == 'medium'
        if action == 'focus_next':
            return not narrow
        if action == 'open_reader':
            return not reading and self._item is not None
        if action == 'back':
            return reading or (narrow and summary_open)
        if action == 'toggle_preview':
            return not reading
        if action == 'refresh':
            return self._actions is not None and not reading
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
        if action in ('next_post', 'next_comment', 'toggle_quotes'):
            if not reading or self._thread is None:
                return False
            if action == 'next_post':
                return self._posts.option_count > 1
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
            name for name in ('posts', 'thread', 'content') if self.query_one(f'#{name}').display
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

    def action_back(self) -> None:
        if self._reading is not None:
            up = {'content': 'thread', 'thread': 'posts'}
            if self._mode == 'narrow' and self._reader_pane in up:
                self._set_reader_pane(up[self._reader_pane])
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

    def post_labels(self) -> list[str]:
        return [
            Content.from_markup(str(self._posts.get_option_at_index(i).prompt)).plain
            for i in range(self._posts.option_count)
        ]

    def thread_labels(self) -> list[str]:
        return [
            Content.from_markup(str(self._thread_list.get_option_at_index(i).prompt)).plain
            for i in range(self._thread_list.option_count)
        ]

    def content_text(self) -> str:
        return self._content.text

    def _set_reader_pane(self, pane: str) -> None:
        if self._mode == 'medium' and pane == 'posts':
            pane = 'thread'  # The patch list is hidden; [ and ] change patches.
        self._reader_pane = pane
        for name in ('posts', 'thread', 'content'):
            self.screen.set_class(name == pane, f'-rpane-{name}')
        widget = {'posts': self._posts, 'thread': self._thread_list, 'content': self._content}
        widget[pane].focus()
        self.refresh_bindings()

    def action_open_reader(self) -> None:
        item = self._item
        if item is None:
            return
        self._reading, self._thread, self._post_key = item, None, ''
        self.screen.add_class('-reading')
        self._posts.clear_options()
        self._thread_list.clear_options()
        self._content.border_title = ''
        self._content.set_text('Loading thread…')
        self._set_status(f' b4-maintainer — Reading: {item.series.subject} v{item.series.revision}')
        self._set_reader_pane('posts')
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
        self._posts.add_options(
            Option(escape(post_label(thread, key, item, self.symbols.absent)), id=key)
            for key in post_keys(thread)
        )
        self._posts.highlighted = 0

    def _show_post(self, key: str) -> None:
        if self._thread is None or self._reading is None:
            return
        self._post_key = key
        replies = thread_replies(self._thread, key)
        width = max(20, self._thread_list.scrollable_content_region.width or 42)
        labels = [root_label(self._thread, key)] + reply_labels(replies)
        self._thread_list.clear_options()
        self._thread_list.add_options(
            Option(escape(pad_display(label, width).rstrip())) for label in labels
        )
        self._thread_list.border_title = escape(
            f'{post_label(self._thread, key, self._reading, self.symbols.absent)} thread'
        )
        target, self._pending_node = self._pending_node or 0, None
        self._thread_list.highlighted = min(target, len(labels) - 1)

    def _current_reply(self):
        if self._thread is None or not self._node:
            return None
        replies = thread_replies(self._thread, self._post_key)
        return replies[self._node - 1] if self._node <= len(replies) else None

    def _show_node(self, index: int) -> None:
        if self._thread is None or self._reading is None:
            return
        self._node = index
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
        self._content.set_blocks(blocks)
        self.refresh_bindings()

    def action_next_post(self, delta: int) -> None:
        current = self._posts.highlighted or 0
        self._posts.highlighted = max(0, min(current + delta, self._posts.option_count - 1))

    def action_next_comment(self) -> None:
        """Next comment in series order, crossing into later patches."""
        if self._thread is None:
            return
        keys = post_keys(self._thread)
        here = keys.index(self._post_key) if self._post_key in keys else 0
        if self._node < len(thread_replies(self._thread, self._post_key)):
            self._thread_list.highlighted = self._node + 1
            return
        for position in range(here + 1, len(keys)):
            if thread_replies(self._thread, keys[position]):
                self._pending_node = 1
                self._posts.highlighted = position
                return
        self.notify('No more comments in this series.')

    def action_toggle_quotes(self) -> None:
        self._expand_quotes = not self._expand_quotes
        self._show_node(self._node)

    def _close_reader(self) -> None:
        self.workers.cancel_group(self, 'thread')
        self._reading, self._thread, self._post_key = None, None, ''
        self._node, self._pending_node = 0, None
        self.screen.remove_class('-reading')
        for name in ('posts', 'thread', 'content'):
            self.screen.remove_class(f'-rpane-{name}')
        self._series_list.focus()
        if self.index is not None:
            self._update_status()
        self.refresh_bindings()

    async def action_quit(self) -> None:
        """Q and Textual's ctrl+q both ask before exiting."""
        if not isinstance(self.screen, ConfirmQuit):
            self.push_screen(ConfirmQuit(), self._quit_confirmed)

    def _quit_confirmed(self, confirmed: Optional[bool]) -> None:
        if confirmed:
            self.exit()

    def quit_prompt(self) -> Optional[str]:
        return ConfirmQuit.PROMPT if isinstance(self.screen, ConfirmQuit) else None

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
            '',
            'Tree evidence:',
        ]
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

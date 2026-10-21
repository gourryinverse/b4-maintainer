# SPDX-License-Identifier: GPL-2.0-or-later
"""Browse screen: Views | Series list | Summary, with a collapsed session drawer."""

import re
import textwrap
from typing import Any, Callable, Optional, Protocol

from b4._textwidth import pad_display
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.geometry import Size
from textual.message import Message
from textual.reactive import Reactive
from textual.screen import ModalScreen
from textual.scroll_view import ScrollView
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Footer, OptionList, Static
from textual.widgets.option_list import Option

from .layout import Breakpoints, Mode, layout_mode
from .model import WorkItem
from .rows import DEFAULT_SYMBOLS, Symbols, render_row
from .views import ViewIndex
from .workspace import Snapshot

FIXES_STYLE = Style(color='dark_orange')
UNREAD_STYLE = Style(color='green')
CURSOR_STYLE = Style(reverse=True)

# Mail is data: drop terminal escape sequences and other control characters.
ESCAPE_RE = re.compile(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|.)')
CONTROL_RE = re.compile(r'[\x00-\x08\x0b-\x1f\x7f-\x9f]')


def printable(text: str) -> str:
    return CONTROL_RE.sub('', ESCAPE_RE.sub('', text)).expandtabs(8)


class AgentLauncher(Protocol):
    def launch(self, request: Any) -> None: ...


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
        self._rows: dict[int, str] = {}

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

    def _row(self, index: int) -> str:
        row = self._rows.get(index)
        if row is None:
            row = render_row(self._items[index], self.symbols, self.project_width)
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
        if index == self.cursor:
            style = style + CURSOR_STYLE
        return Strip([Segment(pad_display(self._row(index), width), style)], width)

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


class TextPane(ScrollView, can_focus=True):
    """Scrollable text that wraps once per width and renders only visible lines.

    Its size comes from the layout, so updates never relayout the screen,
    unlike Static.update().
    """

    virtual_size: Reactive[Size] = Reactive(Size(0, 0), layout=False)

    def __init__(self, text: str = '', **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._text = text
        self._lines: list[str] = []
        self._width = 0

    @property
    def text(self) -> str:
        return self._text

    def set_text(self, text: str) -> None:
        self._text = text
        self._width = 0
        self._wrap()
        self.scroll_to(0, 0, animate=False, immediate=True)
        self.refresh()

    def _wrap(self) -> None:
        width = self.scrollable_content_region.width
        if width <= 0 or width == self._width:
            return
        self._width = width
        self._lines = [
            wrapped
            for line in self._text.split('\n')
            for wrapped in textwrap.wrap(
                line, width, replace_whitespace=False, drop_whitespace=False
            )
            or ['']
        ]
        self.virtual_size = Size(width, len(self._lines))

    def on_resize(self, event: events.Resize) -> None:
        self._wrap()

    def render_line(self, y: int) -> Strip:
        width = self.scrollable_content_region.width
        index = self.scroll_offset.y + y
        if index >= len(self._lines):
            return Strip.blank(width)
        return Strip([Segment(pad_display(self._lines[index], width))], width)


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
    #views, #series, #summary { border: none; border-top: solid $panel-lighten-2; }
    #views:focus, #series:focus, #summary:focus { border: none; border-top: solid $accent; }
    #summary, #summary:focus { border-left: solid $panel-lighten-2; }
    #views { width: 34; }
    #series { width: 1fr; }
    #summary {
        width: 64; padding: 0 1;
        scrollbar-gutter: stable;
    }
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
        Binding('enter', 'open_summary', 'summary'),
        Binding('escape', 'close_summary', 'back'),
        Binding('p', 'toggle_preview', 'preview'),
        Binding('grave_accent', 'toggle_drawer', 'drawer', key_display='`'),
        Binding('Q', 'quit', 'quit'),
    ]

    def __init__(
        self,
        loader: Callable[[], Snapshot],
        launcher: Optional[AgentLauncher] = None,
        breakpoints: Optional[Breakpoints] = None,
        symbols: Symbols = DEFAULT_SYMBOLS,
    ) -> None:
        super().__init__()
        self._loader = loader
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

    def compose(self) -> ComposeResult:
        yield StatusLine(self._status, id='status')
        with Horizontal(id='main'):
            yield OptionList(id='views')
            yield SeriesList(self.symbols, id='series')
            yield TextPane(self._summary, id='summary')
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
        self.snapshot = snapshot
        self.index = index
        if self._breakpoints is None:
            self._breakpoints = snapshot.workspace.breakpoints
            self._apply_layout(self.size.width)
        views = self._views
        views.clear_options()
        views.add_options(
            Option(f'{pad_display(v.label, 21)}{index.count(v.id):>5}', id=v.id)
            for v in index.views
        )
        self._drawer = self._drawer_line()
        self.query_one('#drawer', Static).update(self._drawer)
        self.query_one('#drawer-panel', Static).update(self._drawer_panel())
        self.select_view('all', force=True)

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
        if event.option_id:
            self.select_view(event.option_id)

    def on_series_list_highlighted(self, event: SeriesList.Highlighted) -> None:
        self._item = event.item
        self._show_summary()

    def _show_summary(self) -> None:
        self._summary = self._summary_for(self._item)
        self._summary_pane.set_text(self._summary)

    def on_resize(self, event: events.Resize) -> None:
        self._apply_layout(event.size.width)

    def _apply_layout(self, width: int) -> None:
        self._mode = layout_mode(width, self._breakpoints or Breakpoints())
        for name in ('wide', 'medium', 'narrow'):
            self.screen.set_class(self._mode == name, f'-{name}')
        self._apply_preview_width()
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
        if action == 'toggle_views':
            return self._mode == 'medium'
        if action == 'focus_next':
            return not narrow
        if action == 'open_summary':
            return narrow and not summary_open
        if action == 'close_summary':
            return narrow and summary_open
        return True

    # Text accessors for tests and status rendering.
    def summary_text(self) -> str:
        return self._summary

    def status_text(self) -> str:
        return self._status

    def drawer_text(self) -> str:
        return self._drawer

    def visible_panes(self) -> tuple[str, ...]:
        return tuple(
            name for name in ('views', 'series', 'summary') if self.query_one(f'#{name}').display
        )

    def action_toggle_views(self) -> None:
        self.screen.toggle_class('-show-views')

    def action_open_summary(self) -> None:
        self.screen.add_class('-show-summary')
        self._summary_pane.focus()
        self.refresh_bindings()

    def action_close_summary(self) -> None:
        self.screen.remove_class('-show-summary')
        self._series_list.focus()
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

    def _update_status(self) -> None:
        assert self.index is not None
        view = next(v for v in self.index.views if v.id == self.current_view)
        status = f' b4-maintainer — {view.label} ({self.index.count(view.id)}) · fixture data'
        if self.index.needs_setup:
            status += (
                ' · No subsystem profiles configured: set up profiles to fill My Responsibilities'
            )
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

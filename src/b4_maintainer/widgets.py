# SPDX-License-Identifier: GPL-2.0-or-later
"""Shared widgets that render only visible lines."""

import dataclasses
import re
import textwrap
from dataclasses import dataclass
from typing import Any, Callable, Union

from b4._textwidth import display_width
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.geometry import Size
from textual.reactive import Reactive
from textual.scroll_view import ScrollView
from textual.selection import Selection
from textual.strip import Strip
from textual.widget import Widget

# Mail is data: drop terminal escape sequences and other control characters.
ESCAPE_RE = re.compile(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|.)')
CONTROL_RE = re.compile(r'[\x00-\x08\x0b-\x1f\x7f-\x9f]')


def printable(text: str) -> str:
    return CONTROL_RE.sub('', ESCAPE_RE.sub('', text)).expandtabs(8)


LineStyle = Union[Style, Callable[[str], Style], None]


@dataclass(frozen=True)
class Block:
    """Text rendered as a unit: prose wraps; diffs keep their columns."""

    text: str
    wrap: bool = True
    style: LineStyle = None
    indent: int = 0
    # A name scroll_to_anchor() can find, e.g. a finding's mark.
    anchor: str = ''
    # Folding (e.g. a long quote): blocks of one fold show when it is 'open' or
    # 'closed' (``when``); a click on a ``toggle`` block's lines opens or closes it.
    fold: str = ''
    when: str = ''
    toggle: bool = False


def visible_blocks(blocks: list[Block], opened: set[str]) -> list[Block]:
    """The blocks shown with the folds in *opened* open and the others closed."""
    return [
        b for b in blocks if not b.fold or not b.when or (b.when == 'open') == (b.fold in opened)
    ]


class TextPane(ScrollView, can_focus=True):
    """Scrollable blocks that wrap once per width and render only visible lines.

    Its size comes from the layout, so updates never relayout the screen,
    unlike Static.update(). Unwrapped lines scroll sideways. A mouse drag selects
    text in this pane only (ctrl+c or a right-click copies it); y copies the
    selection, or all of it. A click on a fold's marker opens or closes it.
    """

    BINDINGS = [Binding('y', 'copy_pane', 'copy', show=False)]

    virtual_size: Reactive[Size] = Reactive(Size(0, 0), layout=False)

    def __init__(self, text: str = '', **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._blocks = [Block(text)]
        self._lines: list[tuple[str, Style]] = []
        self._anchors: dict[str, int] = {}
        self._pending_anchor: tuple[str, int] = ('', 0)
        self._width = 0
        self._opened: set[str] = set()  # Open folds.
        self._toggles: dict[int, str] = {}  # Rendered line -> the fold a click toggles.

    @property
    def text(self) -> str:
        """The text shown: closed folds' hidden blocks left out."""
        return '\n'.join(b.text for b in visible_blocks(self._blocks, self._opened))

    @property
    def has_folds(self) -> bool:
        return any(b.fold for b in self._blocks)

    def set_folds(self, opened: bool) -> None:
        """Open (or close) every fold, keeping the place."""
        self._opened = {b.fold for b in self._blocks if b.fold} if opened else set()
        self._rewrap()

    def toggle_fold(self, fold: str) -> None:
        self._opened ^= {fold}
        self._rewrap()

    def click_line(self, index: int) -> bool:
        """A click on rendered line *index*: toggles the fold whose marker it is, unless
        text is selected here (the click ended a drag). True when a fold toggled."""
        fold = self._toggles.get(index)
        if not fold or self.text_selection is not None:
            return False
        self.toggle_fold(fold)
        return True

    def on_click(self, event: events.Click) -> None:
        if event.button == 1 and self.click_line(self.scroll_offset.y + event.y):
            event.stop()

    def _rewrap(self) -> None:
        self._width = 0
        self._wrap()
        self.refresh()

    @property
    def lines(self) -> list[str]:
        """Rendered lines, after wrapping."""
        return [text for text, _ in self._lines]

    @property
    def styled_lines(self) -> list[tuple[str, Style]]:
        return list(self._lines)

    def scroll_to_anchor(self, anchor: str, context: int = 3) -> None:
        """Scroll an anchored block to the top, with *context* lines above it."""
        self._pending_anchor = (anchor, context)
        self._apply_anchor()

    def _apply_anchor(self) -> None:
        anchor, context = self._pending_anchor
        if anchor in self._anchors:
            y = max(0, self._anchors[anchor] - context)
            self.scroll_to(0, y, animate=False, immediate=True)
            self._pending_anchor = ('', 0)

    def set_text(self, text: str) -> None:
        self.set_blocks([Block(text)])

    def shows(self, blocks: list[Block]) -> bool:
        """Whether *blocks* are what the pane shows (set_blocks would change nothing but
        the scroll and the opened folds)."""
        return self._blocks == [dataclasses.replace(b, text=printable(b.text)) for b in blocks]

    def set_blocks(self, blocks: list[Block], open_folds: bool = False) -> None:
        self._blocks = [dataclasses.replace(b, text=printable(b.text)) for b in blocks]
        self._opened = {b.fold for b in self._blocks if b.fold} if open_folds else set()
        self._pending_anchor = ('', 0)
        self._width = 0
        self._wrap()
        self.scroll_to(0, 0, animate=False, immediate=True)
        self.refresh()

    def _wrap(self) -> None:
        width = self.scrollable_content_region.width
        if width <= 0 or width == self._width:
            return
        self._width = width
        lines: list[tuple[str, Style]] = []
        self._anchors = {}
        self._toggles = {}
        widest = width
        for block in visible_blocks(self._blocks, self._opened):
            if block.anchor:
                self._anchors.setdefault(block.anchor, len(lines))
            prefix = ' ' * block.indent
            start = len(lines)
            for raw in block.text.split('\n'):
                style = _style(block.style, raw)
                if not block.wrap:
                    lines.append((prefix + raw, style))
                    widest = max(widest, display_width(prefix + raw))
                    continue
                room = max(1, width - block.indent)
                if len(raw) <= room and raw.isascii() and '\t' not in raw:
                    lines.append((prefix + raw, style))  # Fits: what textwrap returns.
                    continue
                wrapped = textwrap.wrap(
                    raw,
                    room,
                    replace_whitespace=False,
                    drop_whitespace=False,
                )
                lines.extend((prefix + w, style) for w in wrapped or [''])
            if block.toggle:
                self._toggles.update(dict.fromkeys(range(start, len(lines)), block.fold))
        self._lines = lines
        self.virtual_size = Size(widest, len(lines))
        self._apply_anchor()

    def on_resize(self, event: events.Resize) -> None:
        self._wrap()

    def render_line(self, y: int) -> Strip:
        width = self.scrollable_content_region.width
        index = self.scroll_offset.y + y
        if index >= len(self._lines):
            return Strip.blank(width)
        text, style = self._lines[index]
        x = self.scroll_offset.x
        segments = [Segment(text, style)]
        selection = self.text_selection
        span = selection.get_span(index) if selection is not None else None
        if span is not None:
            start, end = span
            end = len(text) if end == -1 else end
            # Only the theme's selection background: the text keeps its colour (the
            # theme's own foreground is 'transparent', which hides it on coloured text).
            shade = self.screen.get_component_rich_style('screen--selection').bgcolor
            lit = Text(text, style=style, end='')
            lit.stylize(Style(bgcolor=shade) if shade else Style(reverse=True), start, end)
            segments = list(lit.render(self.app.console))
        strip = Strip(segments).crop_extend(x, x + width, None)
        return strip.apply_offsets(x, index)  # Where a mouse selection starts and ends.

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        return selection.extract('\n'.join(self.lines)), '\n'

    def selection_updated(self, selection: Selection | None) -> None:
        self.refresh()

    def on_mouse_down(self, event: events.MouseDown) -> None:
        """A right-click copies the selection (ctrl+c does too)."""
        if event.button == 3 and (text := self.screen.get_selected_text()):
            event.stop()
            self.app.copy_to_clipboard(text)

    def action_copy_pane(self) -> None:
        """y: the selected text, or else everything in the pane, to the clipboard."""
        selection = self.text_selection
        picked = self.get_selection(selection) if selection is not None else None
        self.app.copy_to_clipboard((picked[0] if picked else '') or '\n'.join(self.lines))


def _style(style: LineStyle, line: str) -> Style:
    if callable(style):
        return style(line)
    return style or Style()


# Cells a list's rows leave blank at the column's edge: a terminal may draw a symbol (an
# ellipsis, a bullet) wider than counted, and a row reaching the edge would then wrap.
ROW_MARGIN = 2


def tree_width(tree: Widget) -> int:
    """How wide a thread tree's rows may be: ROW_MARGIN short of the pane."""
    return max(20, (tree.scrollable_content_region.width or 54) - ROW_MARGIN)


KEY_STYLE = Style(bold=True, reverse=True)
DISABLED_STYLE = Style(dim=True)


# The major views and their keys, left to right.
TABS = (
    ('session', 'F1', 'Session'),  # Not a tab: F1 starts a session where it can.
    ('lkml', 'F2', 'LKML'),
    ('workbench', 'F3', 'Workbench'),
    ('branches', 'F4', 'Branches'),
    ('refresh', 'F5', 'Refresh'),  # Not a tab: F5 runs the refresh from any of them.
    ('forge', 'F6', 'Forge'),
    ('settings', 'F7', 'Settings'),
    ('help', '?', 'Help'),  # Not a tab: the keys of whichever is open.
)
# Entries that are keys rather than tabs: a click presses the key, so the open screen
# does what it does with it (F1 on the Workbench tab launches over its workbench).
PRESSES = {'session': 'f1', 'refresh': 'f5', 'help': 'question_mark'}
TAB_STYLE = Style(bold=True, reverse=True)


class TabBar(Widget):
    """The tabs across the top: each major view with its key, the current one marked.
    A click on one switches to it."""

    DEFAULT_CSS = 'TabBar { dock: top; height: 1; background: $panel; }'

    def __init__(self, current: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.current = current
        self._spans: list[tuple[int, int, str]] = []  # (start, end, tab) for clicks.

    def text(self) -> str:
        return self.render().plain

    def render(self) -> Text:
        text = Text(no_wrap=True, overflow='ellipsis')
        self._spans = []
        for tab, key, label in TABS:
            start = len(text.plain)
            style = TAB_STYLE if tab == self.current else Style()
            text.append(f' {key} {label} ', style=style)
            self._spans.append((start, len(text.plain), tab))
        return text

    async def on_click(self, event: events.Click) -> None:
        tab = next((t for start, end, t in self._spans if start <= event.x < end), None)
        if tab in PRESSES:
            self.app.simulate_key(PRESSES[tab])
        elif tab is not None and tab != self.current:
            await self.app.run_action(f"tab('{tab}')")


class KeyBar(Widget):
    """One line of key hints for the screen's active bindings.

    Textual's Footer rebuilds a widget per key whenever bindings change, which
    costs tens of milliseconds; this redraws one line of text instead.
    """

    DEFAULT_CSS = 'KeyBar { dock: bottom; height: 1; background: $panel; }'

    def on_mount(self) -> None:
        self.screen.bindings_updated_signal.subscribe(self, self._bindings_changed)

    def on_unmount(self) -> None:
        self.screen.bindings_updated_signal.unsubscribe(self)

    def _bindings_changed(self, screen: Any) -> None:
        if screen is self.screen:
            self.refresh()

    def render(self) -> Text:
        text = Text(no_wrap=True, overflow='ellipsis')
        seen: set[str] = set()
        for _node, binding, enabled, _tooltip in self.screen.active_bindings.values():
            if not binding.show or binding.action in seen:
                continue
            seen.add(binding.action)
            key = self.app.get_key_display(binding)
            text.append(f' {key} ', style=KEY_STYLE if enabled else DISABLED_STYLE)
            text.append(f' {binding.description} ', style=Style() if enabled else DISABLED_STYLE)
        return text

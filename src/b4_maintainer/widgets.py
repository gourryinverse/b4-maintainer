# SPDX-License-Identifier: GPL-2.0-or-later
"""Shared widgets that render only visible lines."""

import re
import textwrap
from dataclasses import dataclass
from typing import Any, Callable, Union

from b4._textwidth import display_width
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual import events
from textual.geometry import Size
from textual.reactive import Reactive
from textual.scroll_view import ScrollView
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


class TextPane(ScrollView, can_focus=True):
    """Scrollable blocks that wrap once per width and render only visible lines.

    Its size comes from the layout, so updates never relayout the screen,
    unlike Static.update(). Unwrapped lines scroll sideways.
    """

    virtual_size: Reactive[Size] = Reactive(Size(0, 0), layout=False)

    def __init__(self, text: str = '', **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._blocks = [Block(text)]
        self._lines: list[tuple[str, Style]] = []
        self._anchors: dict[str, int] = {}
        self._pending_anchor: tuple[str, int] = ('', 0)
        self._width = 0

    @property
    def text(self) -> str:
        return '\n'.join(b.text for b in self._blocks)

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

    def set_blocks(self, blocks: list[Block]) -> None:
        self._blocks = [
            Block(printable(b.text), b.wrap, b.style, b.indent, b.anchor) for b in blocks
        ]
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
        widest = width
        for block in self._blocks:
            if block.anchor:
                self._anchors.setdefault(block.anchor, len(lines))
            prefix = ' ' * block.indent
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
        return Strip([Segment(text, style)]).crop_extend(x, x + width, None)


def _style(style: LineStyle, line: str) -> Style:
    if callable(style):
        return style(line)
    return style or Style()


KEY_STYLE = Style(bold=True, reverse=True)
DISABLED_STYLE = Style(dim=True)


# The major views and their keys, left to right.
TABS = (
    ('lkml', 'F2', 'LKML'),
    ('workbench', 'F3', 'Workbench'),
    ('branches', 'F4', 'Branches'),
    ('refresh', 'F5', 'Refresh'),  # Not a tab: F5 runs the refresh from any of them.
    ('settings', 'F6', 'Settings'),
)
ACTIONS = {'refresh': 'refresh'}  # Entries that run an app action rather than switch.
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
        if tab is not None and tab != self.current:
            await self.app.run_action(ACTIONS.get(tab, f"tab('{tab}')"))


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

# SPDX-License-Identifier: GPL-2.0-or-later
"""Shared widgets that render only visible lines."""

import re
import textwrap
from dataclasses import dataclass
from typing import Any, Callable, Union

from b4._textwidth import display_width
from rich.segment import Segment
from rich.style import Style
from textual import events
from textual.geometry import Size
from textual.reactive import Reactive
from textual.scroll_view import ScrollView
from textual.strip import Strip

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
        self._width = 0

    @property
    def text(self) -> str:
        return '\n'.join(b.text for b in self._blocks)

    def set_text(self, text: str) -> None:
        self.set_blocks([Block(text)])

    def set_blocks(self, blocks: list[Block]) -> None:
        self._blocks = [Block(printable(b.text), b.wrap, b.style, b.indent) for b in blocks]
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
        widest = width
        for block in self._blocks:
            prefix = ' ' * block.indent
            for raw in block.text.split('\n'):
                style = _style(block.style, raw)
                if not block.wrap:
                    lines.append((prefix + raw, style))
                    widest = max(widest, display_width(prefix + raw))
                    continue
                wrapped = textwrap.wrap(
                    raw,
                    max(1, width - block.indent),
                    replace_whitespace=False,
                    drop_whitespace=False,
                )
                lines.extend((prefix + w, style) for w in wrapped or [''])
        self._lines = lines
        self.virtual_size = Size(widest, len(lines))

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

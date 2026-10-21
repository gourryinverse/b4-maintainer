# SPDX-License-Identifier: GPL-2.0-or-later
"""Compact series-row rendering; see docs/design/interface.md."""

from dataclasses import dataclass
from typing import Optional

# Reuse b4's terminal column arithmetic rather than duplicating it.
from b4._textwidth import display_width, pad_display

from .model import Coverage, Progress, RefEvidence, WorkItem


@dataclass(frozen=True)
class Symbols:
    unread: str = '●'
    fixes: str = '⚠'
    partial: str = '◐'
    done: str = '✓'
    draft: str = '✎'
    absent: str = '·'
    complete: str = 'C'
    incomplete: str = 'I'
    acked: str = 'A'
    reviewed: str = 'R'
    tested: str = 'T'
    tree_full: str = '≡'
    tree_partial: str = '≈'
    tree_none: str = '·'
    tree_unchecked: str = '?'
    tree_stale: str = '~'
    unassigned: str = '?'


DEFAULT_SYMBOLS = Symbols()
VERSION_WIDTH = 3
AUTHOR_WIDTH = 16


def tree_field(evidence: tuple[RefEvidence, ...], sym: Symbols) -> str:
    """Two columns: highest-authority matching ref label plus coverage symbol."""
    matched = [e for e in evidence if e[2] in (Coverage.FULL, Coverage.PARTIAL)]
    if matched:
        label, _, coverage = min(matched, key=lambda e: e[1])
        mark = sym.tree_full if coverage == Coverage.FULL else sym.tree_partial
        return pad_display(label, 1) + mark
    coverages = {e[2] for e in evidence}
    # Unknown is not absent: never-checked outranks stale, which outranks none.
    if not evidence or Coverage.UNCHECKED in coverages:
        return ' ' + sym.tree_unchecked
    if Coverage.STALE in coverages:
        return ' ' + sym.tree_stale
    return ' ' + sym.tree_none


def _progress(item: WorkItem, sym: Symbols) -> str:
    if item.state.draft:
        return sym.draft
    if item.state.progress == Progress.DONE:
        return sym.done
    if item.state.progress == Progress.PARTIAL:
        return sym.partial
    return ' '


def cart_field(item: WorkItem, sym: Symbols) -> str:
    acked, reviewed, tested = item.series.trailer_totals()
    return ''.join(
        (
            sym.complete if item.series.complete else sym.incomplete,
            sym.acked if acked else sym.absent,
            sym.reviewed if reviewed else sym.absent,
            sym.tested if tested else sym.absent,
        )
    )


def fit(text: str, width: int) -> str:
    """Pad or cut text to exactly width display columns, marking cuts with …."""
    if display_width(text) <= width:
        return pad_display(text, width)
    out, used = '', 0
    for char in text:
        w = display_width(char)
        if used + w > width - 1:
            break
        out, used = out + char, used + w
    return pad_display(out + '…', width)


def author_label(item: WorkItem) -> str:
    series = item.series
    return series.author_name or series.author_email.split('@', 1)[0]


# (text, role): role is None for plain text, else the author's MAINTAINERS role.
Segments = list[tuple[str, Optional[str]]]


def row_segments(
    item: WorkItem, sym: Symbols = DEFAULT_SYMBOLS, project_width: int = 0
) -> Segments:
    state = item.state
    flags = ''.join(
        (
            pad_display(sym.unread if state.unread else '', 1),
            pad_display(sym.fixes if state.fixes_pending else '', 1),
            pad_display(_progress(item, sym), 1),
        )
    )
    head = ' '.join((flags, cart_field(item, sym), tree_field(state.tree, sym))) + ' '
    tail = [pad_display(f'v{item.series.revision}', VERSION_WIDTH)]
    if project_width:
        label = item.project_short or (sym.unassigned if item.candidates else '')
        tail.append(pad_display(label, project_width))
    tail.append(item.series.subject)
    return [
        (head, None),
        (fit(author_label(item), AUTHOR_WIDTH), item.author_role),
        (' ' + ' '.join(tail), None),
    ]


def render_row(item: WorkItem, sym: Symbols = DEFAULT_SYMBOLS, project_width: int = 0) -> str:
    return ''.join(text for text, _ in row_segments(item, sym, project_width))

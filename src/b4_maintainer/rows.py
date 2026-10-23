# SPDX-License-Identifier: GPL-2.0-or-later
"""Compact series-row rendering; see docs/design/interface.md."""

from dataclasses import dataclass
from typing import Optional

# Reuse b4's terminal column arithmetic rather than duplicating it.
from b4._textwidth import display_width, pad_display

from .model import Progress, SashikoReview, Series, WorkItem


@dataclass(frozen=True)
class Symbols:
    unread: str = '●'
    stable: str = '⚠'
    partial: str = '◐'
    done: str = '✓'
    draft: str = '✎'
    tracked: str = '$'
    absent: str = '·'
    unassigned: str = '?'
    sashiko_none: str = '·'
    sashiko_pending: str = '…'
    sashiko_clean: str = '✓'
    sashiko_findings: str = '!'
    sashiko_embargoed: str = '⊘'
    sashiko_failed: str = '✗'
    discussion: str = 'disc'
    cover: str = '+'


DEFAULT_SYMBOLS = Symbols()
VERSION_WIDTH = 3
AUTHOR_WIDTH = 16
SASHIKO_WIDTH = 3
FLAGS_WIDTH = 4
PATCHES_WIDTH = 6
SASHIKO_PENDING = frozenset({'Pending', 'Fetching', 'Applying', 'In Review', 'Incomplete'})
SASHIKO_FAILED = frozenset({'Failed', 'Failed To Apply'})


def sashiko_field(review: Optional[SashikoReview], sym: Symbols) -> tuple[str, Optional[str]]:
    """Three columns and a role: findings are coloured by their worst severity."""
    role = None
    if review is None:
        mark = sym.sashiko_none
    elif review.status == 'Reviewed' and review.findings:
        count = str(review.findings) if review.findings < 10 else '9+'
        mark, role = count + sym.sashiko_findings, f'sev-{review.worst}'
    elif review.status == 'Reviewed':
        mark = sym.sashiko_clean
    elif review.status == 'Embargoed':
        mark = sym.sashiko_embargoed
    elif review.status in SASHIKO_FAILED:
        mark = sym.sashiko_failed
    elif review.status in SASHIKO_PENDING:
        mark = sym.sashiko_pending
    else:  # Cancelled, Skipped, or a status this client does not know
        mark = sym.sashiko_none
    return pad_display(mark, SASHIKO_WIDTH), role


def patches_field(series: Series, sym: Symbols) -> tuple[str, Optional[str]]:
    """Patches received of expected, as b4 numbers them; the cover mark when there is one.

    Slashes align across rows; a series missing patches is marked incomplete.
    """
    if series.discussion:
        return pad_display(sym.discussion, PATCHES_WIDTH), None
    received = len(series.patches)
    expected = series.expected or received
    head = (sym.cover if series.has_cover else '') + str(received)
    text = pad_display(' ' * max(0, 3 - display_width(head)) + head + '/' + str(expected), 6)
    return text, 'incomplete' if received < expected else None


def _progress(item: WorkItem, sym: Symbols) -> str:
    if item.state.draft:
        return sym.draft
    if item.state.progress == Progress.DONE:
        return sym.done
    if item.state.progress == Progress.PARTIAL:
        return sym.partial
    return ' '


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


# (text, role): role is None for plain text, else the author's MAINTAINERS role, a Sashiko
# severity (sev-critical, sev-high, ...), stable (the ⚠ mark), or incomplete.
Segments = list[tuple[str, Optional[str]]]


def heading(project_width: int = 0) -> str:
    """Field names over the columns row_segments() lays out."""
    head = ' '.join(
        (
            ' ' * FLAGS_WIDTH,
            pad_display('AI', SASHIKO_WIDTH),
            fit('Author', AUTHOR_WIDTH),
        )
    )
    tail = [pad_display('Ver', VERSION_WIDTH), pad_display('Patch', PATCHES_WIDTH)]
    if project_width:
        tail.append(fit('Project', project_width))
    tail.append('Subject')
    return head + ' ' + ' '.join(tail)


def row_segments(
    item: WorkItem, sym: Symbols = DEFAULT_SYMBOLS, project_width: int = 0
) -> Segments:
    state = item.state
    unread = pad_display(sym.unread if state.unread else '', 1)
    stable = pad_display(sym.stable if state.stable_pending else '', 1)
    rest = ''.join(
        (
            pad_display(_progress(item, sym), 1),
            pad_display(sym.tracked if state.to_review or item.watched else '', 1),
        )
    )
    # No tree or trailer columns: ref views list series by the tree they reached, and the
    # summary shows completeness and trailers.
    mark, severity = sashiko_field(item.sashiko, sym)
    version = '' if item.series.discussion else f'v{item.series.revision}'
    patches, completeness = patches_field(item.series, sym)
    tail = []
    if project_width:
        label = item.project_short or (sym.unassigned if item.candidates else '')
        tail.append(pad_display(label, project_width))
    tail.append(item.series.subject)
    return [
        (unread, None),
        (stable, 'stable' if state.stable_pending else None),
        (rest + ' ', None),
        (mark, severity),
        (' ', None),
        (fit(author_label(item), AUTHOR_WIDTH), item.author_role),
        (' ' + pad_display(version, VERSION_WIDTH) + ' ', None),
        (patches, completeness),
        (' ' + ' '.join(tail), None),
    ]


def render_row(item: WorkItem, sym: Symbols = DEFAULT_SYMBOLS, project_width: int = 0) -> str:
    return ''.join(text for text, _ in row_segments(item, sym, project_width))

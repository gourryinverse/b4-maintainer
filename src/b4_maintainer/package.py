# SPDX-License-Identifier: GPL-2.0-or-later
"""The prompt package: the public sources a series session may receive.

Each source is offered with whether it is here, so the dialog can show it disabled with
the reason. Sources are public only (list mail, Sashiko); never drafts or client state.
"""

import pathlib

from .model import Finding, SashikoReview, WorkItem
from .sessions.policy import ContextItem

MBOX = 'Series mbox and thread'
SASHIKO = "Sashiko's review (sashiko.dev, including what was not mailed)"


def series_context(item: WorkItem) -> tuple[ContextItem, ...]:
    """The mailbox and Sashiko's review, each marked available or not."""
    source = item.series.source
    if source and pathlib.Path(source).exists():
        mbox = ContextItem('mbox', MBOX)
    else:
        mbox = ContextItem('mbox', MBOX, available=False, reason='the thread is not cached')
    review = item.sashiko
    if review is None:
        sashiko = ContextItem('sashiko', SASHIKO, available=False, reason='not reviewed')
    elif not review.patches:
        why = f'Sashiko: {review.status}, no review details fetched'
        sashiko = ContextItem('sashiko', SASHIKO, available=False, reason=why)
    else:
        sashiko = ContextItem('sashiko', SASHIKO, text=sashiko_text(review))
    return (mbox, sashiko)


def _finding(finding: Finding) -> list[str]:
    kind = finding.severity + (', pre-existing' if finding.preexisting else '')
    where = ', '.join(f'{path}:{line}' for path, line in finding.locations)
    lines = [f'- [{kind}] {finding.problem}' + (f' ({where})' if where else '')]
    if finding.explanation:
        lines.append(f'  {finding.explanation}')
    return lines


def sashiko_text(review: SashikoReview) -> str:
    """Sashiko's review as Markdown: status, then each patch's findings and inline review."""
    counts = ', '.join(
        f'{n} {name}'
        for n, name in (
            (review.critical, 'critical'),
            (review.high, 'high'),
            (review.medium, 'medium'),
            (review.low, 'low'),
        )
        if n
    )
    lines = [
        "# Sashiko's review",
        '',
        'Automated review from sashiko.dev; some of it may not have been mailed to the list.',
        '',
        f'- Status: {review.status}' + (f' ({counts} new findings)' if counts else ''),
    ]
    lines += [
        f'- {name}: {value}'
        for name, value in (
            ('URL', review.url),
            ('Baseline', review.baseline),
            ('Model', review.model),
        )
        if value
    ]
    for patch in sorted(review.patches, key=lambda p: p.part):
        lines += ['', f'## Patch {patch.part} ({patch.msgid})', '', f'Status: {patch.status}', '']
        for finding in patch.findings:
            lines += _finding(finding)
        if not patch.findings:
            lines.append('No findings.')
        if patch.inline_review.strip():
            lines += ['', '### Inline review', '', patch.inline_review.rstrip()]
    return '\n'.join(lines) + '\n'

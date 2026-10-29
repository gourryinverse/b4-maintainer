# SPDX-License-Identifier: GPL-2.0-or-later
"""The prompt package: the sources a series session may receive.

Each source is offered with whether it is here, so the dialog can show it disabled with
the reason. Public sources (list mail, Sashiko) start ticked; the maintainer's unsent
reply drafts on the thread are private and go only when ticked by hand. Client state
never goes.
"""

import pathlib
from typing import Sequence

from .adapters.drafts import SavedDraft
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


DRAFTS_NOTE = (
    "Not sent: the maintainer's own replies in progress on this series' thread, private "
    'until they send them. Read them to review what they say and what they may have '
    'missed. Never quote them in anything meant for the list or anyone else.'
)


def drafts_context(drafts: Sequence[tuple[SavedDraft, str]]) -> ContextItem:
    """The maintainer's unsent reply drafts on the thread, each with what it answers
    (e.g. `Re: subject (Author)`, empty if unknown): a private source."""
    if not drafts:
        return ContextItem(
            'drafts', 'Your unsent reply drafts', available=False, reason='no drafts on this thread'
        )
    n = len(drafts)
    label = f'Your unsent reply drafts on this thread ({n}, private: not sent)'
    lines = ["# The maintainer's unsent reply drafts", '', DRAFTS_NOTE]
    for draft, answers in drafts:
        lines += ['', f'## Draft reply to <{draft.msgid}>', '']
        if answers:
            lines.append(f'Answers: {answers}')
        lines += [f'Saved: {draft.saved}', '', draft.text.rstrip()]
    return ContextItem('drafts', label, text='\n'.join(lines) + '\n', private=True)


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

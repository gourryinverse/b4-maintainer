# SPDX-License-Identifier: GPL-2.0-or-later
"""Parse local mailboxes into series records through b4's LoreMailbox."""

import email.utils
import os
from typing import Iterable, Optional

import b4

from ..model import Patch, Series, Trailers

TRAILER_KINDS = {'acked-by': 'acked', 'reviewed-by': 'reviewed', 'tested-by': 'tested'}


def _list_ids(msgs: Iterable['b4.LoreMessage']) -> tuple[str, ...]:
    found: dict[str, None] = {}
    for lmsg in msgs:
        for header in lmsg.msg.get_all('List-Id', []):
            _, addr = email.utils.parseaddr(str(header))
            if addr:
                found[addr.lower()] = None
    return tuple(found)


def _recipients(msgs: Iterable['b4.LoreMessage']) -> tuple[str, ...]:
    found: dict[str, None] = {}
    for lmsg in msgs:
        for header in ('To', 'Cc'):
            for _, addr in email.utils.getaddresses(lmsg.msg.get_all(header, [])):
                if '@' in addr:
                    found[addr.lower()] = None
    return tuple(found)


def _trailers(lmsg: Optional['b4.LoreMessage']) -> Trailers:
    if lmsg is None:
        return Trailers()
    seen: set[tuple[str, str]] = set()
    counts = {'acked': 0, 'reviewed': 0, 'tested': 0}
    for trailer in lmsg.followup_trailers:
        kind = TRAILER_KINDS.get(trailer.name.lower())
        if kind is None:
            continue
        who = trailer.addr[1].lower() if trailer.addr else trailer.value.lower()
        if (kind, who) in seen:
            continue
        seen.add((kind, who))
        counts[kind] += 1
    return Trailers(**counts)


def _paths(patches: Iterable['b4.LoreMessage']) -> tuple[str, ...]:
    found: dict[str, None] = {}
    for lmsg in patches:
        for _, _, newfile, _ in sorted(b4.LoreMessage.get_indexes(lmsg.body)):
            found[newfile] = None
    return tuple(found)


def _preview(cover: Optional['b4.LoreMessage'], first: 'b4.LoreMessage', expected: int):
    """Cover letter, or the first patch's message and diffstat when there is none."""
    if cover is not None:
        return 'Cover letter', cover.body.strip('\n')
    description = first.body.split('\ndiff --git ', 1)[0]
    return f'Patch {first.counter}/{expected} (no cover letter)', description.strip('\n')


def _series(lser: 'b4.LoreSeries') -> Series:
    cover = lser.patches[0]
    patches = [p for p in lser.patches[1:] if p is not None]
    change_id = getattr(lser, 'change_id', None)
    if change_id:
        identity = f'change-id:{change_id}'
    elif cover is not None:
        identity = cover.msgid
    else:
        identity = patches[0].msgid
    present = ([cover] if cover is not None else []) + patches
    preview_label, preview = _preview(cover, patches[0], lser.expected)
    return Series(
        identity=identity,
        subject=(cover or patches[0]).lsubject.subject,
        revision=lser.revision,
        expected=lser.expected,
        complete=bool(lser.complete),
        has_cover=bool(lser.has_cover),
        author_name=lser.fromname,
        author_email=lser.fromemail,
        sent=present[0].date,
        patches=tuple(Patch(p.counter, p.msgid, p.lsubject.subject, _trailers(p)) for p in patches),
        cover_trailers=_trailers(cover),
        change_id=change_id,
        lists=_list_ids(present),
        recipients=_recipients(present),
        paths=_paths(patches),
        msgids=tuple(p.msgid for p in present),
        preview_label=preview_label,
        preview=preview,
    )


def parse_mbox(path: 'os.PathLike[str] | str') -> list[Series]:
    """Return every revision found in a mailbox or maildir, oldest first.

    Offline: lore trailer searches and reroll gap-filling stay disabled so
    completeness and trailers reflect the local mail only.
    """
    lmbx = b4.LoreMailbox()
    for msg in b4.get_msgs_from_mailbox_or_maildir(os.fspath(path)):
        lmbx.add_message(msg)
    result = []
    for revision in sorted(lmbx.series):
        lser = lmbx.get_series(revision, reroll=False, codereview_trailers=False)
        if lser is not None and any(lser.patches[1:]):
            result.append(_series(lser))
    return result

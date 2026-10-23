# SPDX-License-Identifier: GPL-2.0-or-later
"""JSON form of parsed series, so startup reads summaries instead of reparsing mail."""

import datetime
from typing import Any

from ..model import Patch, Series, Tag, Trailers

VERSION = 2  # 2: per-patch Fixes and stable tags.
# Older summaries still read (missing fields empty) until a refresh reparses them.
READABLE = frozenset({1, VERSION})


def _tags(tags: frozenset[Tag]) -> list[list[str]]:
    return sorted([kind, who] for kind, who in tags)


def _from_tags(raw: list[list[str]]) -> frozenset[Tag]:
    return frozenset((kind, who) for kind, who in raw)


def series_to_dict(s: Series) -> dict[str, Any]:
    return {
        'identity': s.identity,
        'subject': s.subject,
        'revision': s.revision,
        'expected': s.expected,
        'complete': s.complete,
        'has_cover': s.has_cover,
        'author_name': s.author_name,
        'author_email': s.author_email,
        'sent': s.sent.isoformat() if s.sent else None,
        'patches': [
            [
                p.counter,
                p.msgid,
                p.subject,
                _tags(p.replied),
                _tags(p.carried),
                list(p.fixes),
                p.stable,
            ]
            for p in s.patches
        ],
        'cover_trailers': [
            s.cover_trailers.acked,
            s.cover_trailers.reviewed,
            s.cover_trailers.tested,
        ],
        'change_id': s.change_id,
        'lists': list(s.lists),
        'recipients': list(s.recipients),
        'paths': list(s.paths),
        'msgids': list(s.msgids),
        'preview_label': s.preview_label,
        'preview': s.preview,
        'source': s.source,
    }


def series_from_dict(d: dict[str, Any]) -> Series:
    return Series(
        identity=d['identity'],
        subject=d['subject'],
        revision=d['revision'],
        expected=d['expected'],
        complete=d['complete'],
        has_cover=d['has_cover'],
        author_name=d['author_name'],
        author_email=d['author_email'],
        sent=datetime.datetime.fromisoformat(d['sent']) if d['sent'] else None,
        patches=tuple(
            Patch(
                counter,
                msgid,
                subject,
                _from_tags(replied),
                _from_tags(carried),
                tuple(rest[0]) if rest else (),
                bool(rest[1]) if len(rest) > 1 else False,
            )
            for counter, msgid, subject, replied, carried, *rest in d['patches']
        ),
        cover_trailers=Trailers(*d['cover_trailers']),
        change_id=d['change_id'],
        lists=tuple(d['lists']),
        recipients=tuple(d['recipients']),
        paths=tuple(d['paths']),
        msgids=tuple(d['msgids']),
        preview_label=d['preview_label'],
        preview=d['preview'],
        source=d['source'],
    )

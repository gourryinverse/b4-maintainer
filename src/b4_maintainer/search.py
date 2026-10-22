# SPDX-License-Identifier: GPL-2.0-or-later
"""Series-list search: case-insensitive regex over subject and author; literal on bad regex."""

import re
from dataclasses import dataclass
from typing import Iterable, Optional

from .model import WorkItem


@dataclass(frozen=True)
class Query:
    text: str
    pattern: Optional[re.Pattern[str]]
    literal: bool = False


def compile_query(text: str) -> Query:
    if not text:
        return Query(text, None)
    try:
        return Query(text, re.compile(text, re.IGNORECASE))
    except re.error:
        return Query(text, re.compile(re.escape(text), re.IGNORECASE), literal=True)


def haystack(item: WorkItem) -> str:
    s = item.series
    return f'{s.subject}\n{s.author_name}\n{s.author_email}'


def texts(items: Iterable[WorkItem]) -> list[tuple[str, str]]:
    """(identity, searchable text) pairs; build once per snapshot, search per keystroke."""
    return [(i.identity, haystack(i)) for i in items]


def match_texts(pairs: Iterable[tuple[str, str]], query: Query) -> frozenset[str]:
    if query.pattern is None:
        return frozenset(identity for identity, _ in pairs)
    search = query.pattern.search
    return frozenset(identity for identity, text in pairs if search(text))


def matching(items: Iterable[WorkItem], query: Query) -> frozenset[str]:
    """Identities of items the query matches (all of them for an empty query)."""
    return match_texts(texts(items), query)

# SPDX-License-Identifier: GPL-2.0-or-later
"""Immutable work-item records shared by adapters, views, and the UI."""

import datetime
import enum
from dataclasses import dataclass, field
from typing import Optional


class Authority(enum.IntEnum):
    """Ref authority; lower values outrank higher ones."""

    MAINLINE = 0
    ACCEPTANCE = 1
    PRELIMINARY = 2


class Coverage(enum.Enum):
    FULL = 'full'
    PARTIAL = 'partial'
    NONE = 'none'
    UNCHECKED = 'unchecked'
    STALE = 'stale'


class Progress(enum.Enum):
    NONE = 'none'
    PARTIAL = 'partial'
    DONE = 'done'


# (ref label, ref authority, coverage)
RefEvidence = tuple[str, Authority, Coverage]


@dataclass(frozen=True)
class Trailers:
    acked: int = 0
    reviewed: int = 0
    tested: int = 0

    def __add__(self, other: 'Trailers') -> 'Trailers':
        return Trailers(
            self.acked + other.acked, self.reviewed + other.reviewed, self.tested + other.tested
        )


@dataclass(frozen=True)
class Patch:
    counter: int
    msgid: str
    subject: str
    trailers: Trailers = Trailers()


@dataclass(frozen=True)
class Series:
    """One posting of a series, as parsed from mail."""

    identity: str
    subject: str
    revision: int
    expected: int
    complete: bool
    has_cover: bool
    author_name: str
    author_email: str
    sent: Optional[datetime.datetime]
    patches: tuple[Patch, ...]
    cover_trailers: Trailers = Trailers()
    change_id: Optional[str] = None
    lists: tuple[str, ...] = ()
    recipients: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    msgids: tuple[str, ...] = ()
    preview_label: str = ''
    preview: str = ''
    source: str = ''

    def list_candidates(self) -> tuple[str, ...]:
        """List IDs plus recipients in lore's name.domain list-ID form.

        Archives keep one copy of a cross-posted message, so List-Id alone
        under-reports membership; routing matches these against configured lists.
        """
        converted = tuple(addr.replace('@', '.', 1) for addr in self.recipients)
        return tuple(dict.fromkeys(self.lists + converted))

    def trailer_totals(self) -> tuple[int, int, int]:
        total = self.cover_trailers
        for patch in self.patches:
            total = total + patch.trailers
        return total.acked, total.reviewed, total.tested


@dataclass(frozen=True)
class Message:
    msgid: str
    author_name: str
    author_email: str
    date: Optional[datetime.datetime]
    subject: str
    body: str


@dataclass(frozen=True)
class Reply:
    message: Message
    depth: int


@dataclass(frozen=True)
class Post:
    """A cover letter (counter 0) or patch with its own discussion."""

    counter: int
    message: Message
    description: str
    diff: str
    replies: tuple[Reply, ...] = ()


@dataclass(frozen=True)
class SeriesThread:
    """One revision's postings and discussion, loaded on demand for reading."""

    identity: str
    revision: int
    cover: Optional[Post]
    patches: tuple[Post, ...]
    unattached: tuple[Reply, ...] = ()


@dataclass(frozen=True)
class SeriesState:
    """Per-series state owned by native stores or client state, read via adapters."""

    unread: int = 0
    fixes_pending: bool = False
    progress: Progress = Progress.NONE
    draft: bool = False
    to_review: bool = False
    tree: tuple[RefEvidence, ...] = ()

    @property
    def mainline_complete(self) -> bool:
        return any(a == Authority.MAINLINE and c == Coverage.FULL for _, a, c in self.tree)


@dataclass(frozen=True)
class WorkItem:
    """A series identity routed into the workspace, with its current state."""

    series: Series
    state: SeriesState
    project: Optional[str] = None
    project_short: str = ''
    candidates: tuple[str, ...] = ()
    profiles: frozenset[str] = field(default_factory=frozenset)
    versions: tuple[int, ...] = ()

    @property
    def identity(self) -> str:
        return self.series.identity

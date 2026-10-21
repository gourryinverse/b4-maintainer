# SPDX-License-Identifier: GPL-2.0-or-later
"""Immutable work-item records shared by adapters, views, and the UI."""

import collections
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
# (trailer kind: acked/reviewed/tested, reviewer identity)
Tag = tuple[str, str]


@dataclass(frozen=True)
class Trailers:
    acked: int = 0
    reviewed: int = 0
    tested: int = 0

    @classmethod
    def of(cls, tags: frozenset[Tag]) -> 'Trailers':
        counts = collections.Counter(kind for kind, _ in tags)
        return cls(counts['acked'], counts['reviewed'], counts['tested'])

    def __add__(self, other: 'Trailers') -> 'Trailers':
        return Trailers(
            self.acked + other.acked, self.reviewed + other.reviewed, self.tested + other.tested
        )


@dataclass(frozen=True)
class Patch:
    """A posted patch; tags carried in its commit message are kept apart from replies."""

    counter: int
    msgid: str
    subject: str
    replied: frozenset[Tag] = frozenset()
    carried: frozenset[Tag] = frozenset()

    @property
    def trailers(self) -> Trailers:
        """All evidence, deduplicated by reviewer across commit and replies."""
        return Trailers.of(self.replied | self.carried)

    @property
    def reply_trailers(self) -> Trailers:
        return Trailers.of(self.replied)

    @property
    def carried_trailers(self) -> Trailers:
        return Trailers.of(self.carried)


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
    tags: frozenset[Tag] = frozenset()


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
    # Touches files the maintainer maintains (e.g. MAINTAINERS M: entries).
    responsible: bool = False
    # The author's role in the project's MAINTAINERS: 'maintainer', 'reviewer', or ''.
    author_role: str = ''
    # Dismissed by the maintainer; shown only in the Hidden view.
    hidden: bool = False

    @property
    def identity(self) -> str:
        return self.series.identity

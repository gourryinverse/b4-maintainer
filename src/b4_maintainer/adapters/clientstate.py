# SPDX-License-Identifier: GPL-2.0-or-later
"""Client-owned state kept as text under state/client/, synced with the state repository.

Hidden series: one sorted, indented JSON entry per hide, so concurrent edits
from several machines merge as ordinary text. An entry matches by series
identity or by family (sender plus subject), so later revisions stay hidden.

Watched discussions: the same form, one entry per conversation the maintainer
follows; b4 tracks series only, so this is the client's own state.
"""

import datetime
import json
import os
import pathlib
from dataclasses import dataclass
from typing import Any, Optional

from ..model import WorkItem
from ..workspace import family_key


@dataclass(frozen=True)
class Hidden:
    identities: frozenset[str] = frozenset()
    families: frozenset[str] = frozenset()

    def matches(self, item: WorkItem) -> bool:
        return item.identity in self.identities or family_key(item.series) in self.families


class HiddenStore:
    def __init__(self, path: pathlib.Path) -> None:
        self.path = path

    def _entries(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return json.loads(self.path.read_text())

    def load(self) -> Hidden:
        entries = self._entries()
        return Hidden(
            frozenset(e['identity'] for e in entries), frozenset(e['family'] for e in entries)
        )

    def hide(self, item: WorkItem, now: Optional[datetime.datetime] = None) -> None:
        now = now or datetime.datetime.now(datetime.timezone.utc)
        entries = [e for e in self._entries() if e['identity'] != item.identity]
        entries.append(
            {
                'identity': item.identity,
                'family': family_key(item.series),
                'subject': item.series.subject,
                'author': item.series.author_email,
                'hidden_at': now.isoformat(),
            }
        )
        self._save(entries)

    def unhide(self, item: WorkItem) -> None:
        family = family_key(item.series)
        self._save(
            [e for e in self._entries() if e['identity'] != item.identity and e['family'] != family]
        )

    def _save(self, entries: list[dict[str, Any]]) -> None:
        entries.sort(key=lambda e: (e['subject'], e['identity']))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f'.{self.path.name}.tmp')
        tmp.write_text(json.dumps(entries, indent=1, ensure_ascii=False) + '\n')
        os.replace(tmp, self.path)


class WatchedStore:
    """Discussions the maintainer follows, by identity (thread:<first message-id>)."""

    def __init__(self, path: pathlib.Path) -> None:
        self.path = path

    def _entries(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return json.loads(self.path.read_text())

    def load(self) -> frozenset[str]:
        return frozenset(e['identity'] for e in self._entries())

    def msgids(self) -> frozenset[str]:
        """First message-ids of watched discussions, to fetch their threads by."""
        return frozenset(e['msgid'] for e in self._entries())

    def watch(self, item: WorkItem, now: Optional[datetime.datetime] = None) -> None:
        now = now or datetime.datetime.now(datetime.timezone.utc)
        entries = [e for e in self._entries() if e['identity'] != item.identity]
        entries.append(
            {
                'identity': item.identity,
                'msgid': item.identity.removeprefix('thread:'),
                'subject': item.series.subject,
                'watched_at': now.isoformat(),
            }
        )
        self._save(entries)

    def unwatch(self, item: WorkItem) -> None:
        self._save([e for e in self._entries() if e['identity'] != item.identity])

    def _save(self, entries: list[dict[str, Any]]) -> None:
        entries.sort(key=lambda e: (e['subject'], e['identity']))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f'.{self.path.name}.tmp')
        tmp.write_text(json.dumps(entries, indent=1, ensure_ascii=False) + '\n')
        os.replace(tmp, self.path)

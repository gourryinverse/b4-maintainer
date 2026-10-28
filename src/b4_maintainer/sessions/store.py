# SPDX-License-Identifier: GPL-2.0-or-later
"""var/sessions/: one directory per session (session.json, events.jsonl, out/).

Polling rereads a record only when its file changed and reads only new event lines, so
the drawer's once-a-second poll stays cheap with many sessions. One poll at a time.
"""

import os
import pathlib
import re
import shutil
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from .records import STATES, Event, EventLog, Progress, SessionRecord, fold_events, session_state

SESSION_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}')
RECORD = 'session.json'
EVENTS = 'events.jsonl'

# Which sessions' windows are alive, asked once per poll for all of them.
Alive = Callable[[Sequence[SessionRecord]], set[str]]


def valid_id(sid: str) -> bool:
    """A session id is also a directory name: no separators, dots, or leading dash."""
    return SESSION_ID.fullmatch(sid) is not None


@dataclass(frozen=True)
class SessionView:
    record: SessionRecord
    state: str
    approvals: int = 0
    last: str = ''  # Time of the last event.


class SessionStore:
    def __init__(self, root: pathlib.Path) -> None:
        self.root = pathlib.Path(root)
        self._records: dict[str, tuple[int, SessionRecord]] = {}
        self._logs: dict[str, EventLog] = {}
        self._progress: dict[str, Progress] = {}

    def directory(self, sid: str) -> pathlib.Path:
        if not valid_id(sid):
            raise ValueError(f'invalid session id {sid!r}')
        return self.root / sid

    def create(self, record: SessionRecord) -> None:
        path = self.directory(record.id) / RECORD
        if path.exists():
            raise FileExistsError(f'session {record.id} exists')
        record.save(path)

    def exists(self, sid: str) -> bool:
        return valid_id(sid) and (self.root / sid / RECORD).is_file()

    def record(self, sid: str) -> SessionRecord:
        return SessionRecord.load(self.directory(sid) / RECORD)

    def save(self, record: SessionRecord) -> None:
        """Replace an existing session's record (e.g. with its launched command line)."""
        record.save(self.directory(record.id) / RECORD)

    def set_state(self, sid: str, state: str) -> None:
        """Record an app state (stopped, ended, failed), or clear it with ''."""
        self.record(sid).replace(state=state).save(self.directory(sid) / RECORD)

    def delete(self, sid: str) -> None:
        """The session's directory, gone: its record, events, package, and output."""
        directory = self.directory(sid)
        if directory.is_symlink() or not directory.is_dir():
            raise OSError(f'{directory} is not a session directory')
        shutil.rmtree(directory)
        self._records.pop(sid, None)
        self._logs.pop(sid, None)
        self._progress.pop(sid, None)

    def append(self, sid: str, event: Event) -> None:
        EventLog.append(self.directory(sid) / EVENTS, event)

    def artifacts(self, sid: str) -> list[str]:
        out = self.directory(sid) / 'work' / 'out'
        return sorted(
            str(p.relative_to(out)) for p in out.rglob('*') if p.is_file() and not p.is_symlink()
        )

    def _load(self, sid: str) -> Optional[SessionRecord]:
        path = self.root / sid / RECORD
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            return None
        cached = self._records.get(sid)
        if cached is not None and cached[0] == mtime:
            return cached[1]
        try:
            record = SessionRecord.load(path)
        except (OSError, ValueError, TypeError, KeyError):
            return None  # Unreadable or being replaced: skipped this poll.
        self._records[sid] = (mtime, record)
        return record

    def poll(self, alive: Alive) -> list[SessionView]:
        """Every session with its state, most urgent first, then oldest first."""
        try:
            names = sorted(e.name for e in os.scandir(self.root) if e.is_dir())
        except OSError:
            names = []
        records = [r for r in (self._load(n) for n in names if valid_id(n)) if r is not None]
        windows = alive(records) if records else set()
        views = []
        for record in records:
            log = self._logs.setdefault(record.id, EventLog(self.root / record.id / EVENTS))
            progress = fold_events(log.read_new(), self._progress.get(record.id, Progress()))
            self._progress[record.id] = progress
            state = session_state(record, progress, record.id in windows)
            views.append(SessionView(record, state, progress.approvals, progress.last))
        views.sort(key=lambda v: (STATES.index(v.state), v.record.created, v.record.id))
        return views

# SPDX-License-Identifier: GPL-2.0-or-later
"""var/sessions/: one directory per session (session.json, events.jsonl, out/), named by
its id (a timestamp); names.toml maps each id to the session's name, which the UI shows.

Polling rereads a record only when its file changed and reads only new event lines, so
the drawer's once-a-second poll stays cheap with many sessions. One poll at a time.
"""

import fcntl
import json
import os
import pathlib
import re
import shutil
import tomllib
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Sequence

from .records import (
    STATES,
    Event,
    EventLog,
    Progress,
    SessionRecord,
    default_name,
    fold_events,
    session_state,
)

SESSION_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}')
RECORD = 'session.json'
EVENTS = 'events.jsonl'
NAMES = 'names.toml'  # id = "name", for every session.
NAME_MAX = 40

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
        self._names: tuple[int, dict[str, str]] = (-1, {})

    def names(self) -> dict[str, str]:
        """Each session's name, by id (reread only when names.toml changes)."""
        path = self.root / NAMES
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            return {}
        if mtime != self._names[0]:
            try:
                raw = tomllib.loads(path.read_text())
            except (OSError, ValueError):
                return self._names[1]  # Being replaced: the last read stands.
            self._names = (mtime, {str(k): str(v) for k, v in raw.items()})
        return self._names[1]

    def _change_names(self, change: Callable[[dict[str, str]], None]) -> dict[str, str]:
        """names.toml changed by *change*, under a lock (another instance may write too)."""
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / NAMES
        with open(self.root / '.names.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                names = {str(k): str(v) for k, v in tomllib.loads(path.read_text()).items()}
            except FileNotFoundError:
                names = {}
            change(names)
            tmp = self.root / f'.{NAMES}.tmp'
            tmp.write_text(
                ''.join(
                    f'{json.dumps(k)} = {json.dumps(v, ensure_ascii=False)}\n'
                    for k, v in sorted(names.items())
                )
            )
            os.replace(tmp, path)
        self._names = (-1, {})
        return names

    def _name_new(self, records: Iterable[SessionRecord]) -> None:
        """Default names for *records* that have none, each unique."""

        def change(names: dict[str, str]) -> None:
            taken = {n.lower() for n in names.values()}
            for record in records:
                if record.id in names:
                    continue
                base = default_name(record)[:NAME_MAX]
                name, n = base, 1
                while name.lower() in taken:
                    n += 1
                    name = f'{base[: NAME_MAX - len(str(n)) - 1]}-{n}'
                names[record.id] = name
                taken.add(name.lower())

        self._change_names(change)

    def rename(self, sid: str, name: str) -> str:
        """Session *sid* called *name* (trimmed): one line, at most 40 characters, no
        other session's name (in any case). The name kept."""
        name = name.strip()
        if not name or len(name) > NAME_MAX:
            raise ValueError(f'a name has 1 to {NAME_MAX} characters')
        if any(c < ' ' or c == '\x7f' for c in name):
            raise ValueError('a name is one line of text')
        if not self.exists(sid):
            raise ValueError(f'no session {sid}')

        def change(names: dict[str, str]) -> None:
            if any(n.lower() == name.lower() for k, n in names.items() if k != sid):
                raise ValueError(f'another session is already called {name}')
            names[sid] = name

        self._change_names(change)
        return name

    def _named(self, record: SessionRecord, names: dict[str, str]) -> SessionRecord:
        name = names.get(record.id, '')
        return record if record.name == name else record.replace(name=name)

    def directory(self, sid: str) -> pathlib.Path:
        if not valid_id(sid):
            raise ValueError(f'invalid session id {sid!r}')
        return self.root / sid

    def create(self, record: SessionRecord) -> SessionRecord:
        """Record a new session, given its default name: the record, named."""
        path = self.directory(record.id) / RECORD
        if path.exists():
            raise FileExistsError(f'session {record.id} exists')
        record.save(path)
        self._name_new([record])
        return self._named(record, self.names())

    def exists(self, sid: str) -> bool:
        return valid_id(sid) and (self.root / sid / RECORD).is_file()

    def record(self, sid: str) -> SessionRecord:
        return self._named(SessionRecord.load(self.directory(sid) / RECORD), self.names())

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
        if sid in self.names():

            def forget(names: dict[str, str]) -> None:
                names.pop(sid, None)

            self._change_names(forget)
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
        known = self.names()
        unnamed = [r for r in records if r.id not in known]
        if unnamed:  # Sessions from before names: named once, oldest first.
            try:
                self._name_new(sorted(unnamed, key=lambda r: (r.created, r.id)))
            except OSError:
                pass  # Shown by their ids until a poll can write.
            known = self.names()
        records = [self._named(r, known) for r in records]
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

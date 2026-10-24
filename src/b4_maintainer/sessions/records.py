# SPDX-License-Identifier: GPL-2.0-or-later
"""Session records (var/sessions/<id>/session.json) and their hook event logs.

A session's state comes from its events (appended by the b4-maintainer pi extension,
directly or through the hook command), whether its tmux window is alive, and the few
states the app itself records (stopped, ended, failed). Event payloads are untrusted
data: only the event name, a tool name, and a short detail are kept.

Events: start, working, tool, tool_done, approval (the extension asks the maintainer),
waiting (the agent will not continue on its own), model (detail: the new model), end.
"""

import dataclasses
import json
import os
import pathlib
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Protocol

# Most urgent first: what the collapsed drawer line names.
STATES = ('approval', 'waiting', 'failed', 'gone', 'working', 'starting', 'stopped', 'ended')
APP_STATES = frozenset({'stopped', 'ended', 'failed'})
WORKING_EVENTS = frozenset({'working', 'tool', 'tool_done'})


@dataclass(frozen=True)
class Worktree:
    path: str
    branch: str
    base: str
    applied: str  # The last series commit on the session branch.


@dataclass(frozen=True)
class SessionRecord:
    id: str
    created: str
    scope: str  # series or workspace
    project: str
    series: str  # Series identity; empty for workspace sessions.
    revision: int
    subject: str
    profile_id: str
    profile_version: int
    profile_hash: str
    choices: dict[str, Any]
    mode: str
    harness: str  # pi
    preset: str  # The dialog's model choice: default, a preset id, dry-run, or provider/id.
    command: tuple[str, ...]  # The harness command line, as launched.
    native_id: str  # The harness's session id (pi: the same as ours).
    model: str  # pi's --model; empty for pi's default.
    thinking: str
    permissions: str
    worktree: Optional[Worktree] = None
    state: str = ''  # App-recorded: stopped, ended, failed; else derived from events.
    provenance: dict[str, str] = field(default_factory=dict)
    context: tuple[str, ...] = ()  # Public context items given (ids).
    skills: tuple[str, ...] = ()

    def replace(self, **changes: Any) -> 'SessionRecord':
        return dataclasses.replace(self, **changes)

    def save(self, path: pathlib.Path) -> None:
        data = dataclasses.asdict(self)
        data['command'] = list(self.command)
        data['context'] = list(self.context)
        data['skills'] = list(self.skills)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f'.{path.name}.tmp')
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True) + '\n')
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: pathlib.Path) -> 'SessionRecord':
        data = json.loads(path.read_text())
        worktree = data.pop('worktree', None)
        data['command'] = tuple(data.get('command', ()))
        data['context'] = tuple(data.get('context', ()))
        data['skills'] = tuple(data.get('skills', ()))
        names = {f.name for f in dataclasses.fields(cls)}
        known = {k: v for k, v in data.items() if k in names}
        return cls(**known, worktree=Worktree(**worktree) if worktree else None)


class Pinned(Protocol):
    """A session record or launch target (frozen: read-only attributes)."""

    @property
    def scope(self) -> str: ...

    @property
    def project(self) -> str: ...

    @property
    def series(self) -> str: ...

    @property
    def subject(self) -> str: ...

    @property
    def revision(self) -> int: ...


def session_label(pinned: Pinned) -> str:
    """What a session is pinned to: `project: subject vN`, or `workspace`."""
    if pinned.scope == 'workspace' or not pinned.series:
        return 'workspace'
    return f'{pinned.project}: {pinned.subject} v{pinned.revision}'


@dataclass(frozen=True)
class Event:
    time: str
    event: str
    tool: str = ''
    detail: str = ''  # E.g. the model, for model events.


def event_from_payload(raw: str, now: str) -> Optional[Event]:
    """The compact event for a payload (JSON from the extension), or None."""
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    name = data.get('event') if isinstance(data, dict) else None
    if not isinstance(name, str) or not name:
        return None
    tool, detail = data.get('tool', ''), data.get('detail', '')
    return Event(
        now,
        name[:40],
        tool[:80] if isinstance(tool, str) else '',
        detail[:200] if isinstance(detail, str) else '',
    )


class EventLog:
    """events.jsonl, read incrementally; a line is read once complete."""

    def __init__(self, path: pathlib.Path) -> None:
        self.path = path
        self._offset = 0

    def read_new(self) -> list[Event]:
        try:
            with self.path.open('rb') as fh:
                fh.seek(self._offset)
                data = fh.read()
        except OSError:
            return []
        end = data.rfind(b'\n') + 1
        self._offset += end
        events = []
        for line in data[:end].splitlines():
            try:
                raw = json.loads(line)
                events.append(
                    Event(
                        str(raw['time']),
                        str(raw['event']),
                        str(raw.get('tool', '')),
                        str(raw.get('detail', '')),
                    )
                )
            except (ValueError, KeyError, TypeError):
                continue  # Malformed: skipped.
        return events

    @staticmethod
    def append(path: pathlib.Path, event: Event) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({k: v for k, v in dataclasses.asdict(event).items() if v}) + '\n'
        with path.open('a') as fh:  # One short write per line: appends stay whole.
            fh.write(line)


@dataclass(frozen=True)
class Progress:
    """What a session's events say so far: state, pending approvals, last event time."""

    state: str = 'starting'
    approvals: int = 0
    last: str = ''


def fold_events(events: Iterable[Event], progress: Progress = Progress()) -> Progress:
    """*progress* advanced by *events*; polling folds each batch of new events in."""
    state, approvals, last = progress.state, progress.approvals, progress.last
    for event in events:
        last = event.time
        if event.event in ('start', 'waiting'):
            state, approvals = 'waiting', 0
        elif event.event in WORKING_EVENTS:
            state, approvals = 'working', 0
        elif event.event == 'approval':
            state, approvals = 'approval', approvals + 1
        elif event.event == 'end':
            state, approvals = 'ended', 0
    return Progress(state, approvals, last)


def session_state(record: SessionRecord, progress: Progress, window_alive: bool) -> str:
    if record.state in APP_STATES:
        return record.state
    if not window_alive and progress.state != 'ended':
        return 'gone'
    return progress.state


def derive_state(record: SessionRecord, events: Iterable[Event], window_alive: bool) -> str:
    return session_state(record, fold_events(events), window_alive)


def most_urgent(states: Iterable[str]) -> Optional[str]:
    present = set(states)
    return next((s for s in STATES if s in present and s != 'ended'), None)

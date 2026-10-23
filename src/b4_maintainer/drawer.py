# SPDX-License-Identifier: GPL-2.0-or-later
"""Text of the session drawer: the collapsed line and the Sessions and Worktrees rows.

Each session is named by the series it was launched for, never by the selection.
"""

import datetime
from typing import Iterable, Optional, Union

from .sessions.hub import Overview, WorktreeInfo
from .sessions.launch import Target
from .sessions.records import SessionRecord
from .sessions.store import SessionView

STATE_LABELS = {
    'approval': 'needs approval',
    'waiting': 'waiting for input',
    'failed': 'failed',
    'gone': 'window gone',
    'working': 'working',
    'starting': 'starting',
    'stopped': 'stopped',
    'ended': 'ended',
}
# ● wants the maintainer; ◆ busy; ○ idle by choice.
STATE_MARKS = {'approval': '●', 'waiting': '●', 'failed': '●', 'gone': '●', 'stopped': '○'}


def format_bytes(n: int) -> str:
    for unit, size in (('GB', 10**9), ('MB', 10**6), ('kB', 10**3)):
        if n >= size:
            return f'{n / size:.1f} {unit}'
    return f'{n} B'


def session_label(record: Union[SessionRecord, Target]) -> str:
    if record.scope == 'workspace' or not record.series:
        return 'workspace'
    return f'{record.project}: {record.subject} v{record.revision}'


def state_text(view: SessionView) -> str:
    text = STATE_LABELS.get(view.state, view.state)
    return f'{text} ({view.approvals})' if view.approvals > 1 else text


def mark(state: str) -> str:
    return STATE_MARKS.get(state, '◆')


def worktrees_text(worktrees: tuple[WorktreeInfo, ...], total: int) -> str:
    return f'Worktrees {len(worktrees)} · {format_bytes(total)}'


def drawer_line(overview: Overview) -> str:
    """Most urgent session first: `Sessions 3 · ● mm: foo v3 needs approval · Worktrees …`."""
    parts = []
    if overview.sessions:
        first = overview.sessions[0]
        parts.append(f'Sessions {len(overview.sessions)}')
        parts.append(f'{mark(first.state)} {session_label(first.record)} {state_text(first)}')
    else:
        parts.append('No agent sessions')
    if overview.worktrees:
        parts.append(worktrees_text(overview.worktrees, overview.worktree_bytes))
    return f' {" · ".join(parts)}   [`] drawer'


def tabs_line(overview: Overview, tab: str) -> str:
    sessions = f'Sessions {len(overview.sessions)}'
    worktrees = worktrees_text(overview.worktrees, overview.worktree_bytes)
    left, right = (
        (f'[{sessions}]', worktrees) if tab == 'sessions' else (sessions, f'[{worktrees}]')
    )
    return f'{left}  {right}   Tab switches'


def age(created: str, now: datetime.datetime) -> str:
    try:
        then = datetime.datetime.fromisoformat(created)
        seconds = int((now - then).total_seconds())
    except (ValueError, TypeError):
        return '?'
    for unit, size in (('d', 86400), ('h', 3600), ('m', 60)):
        if seconds >= size:
            return f'{seconds // size}{unit}'
    return 'now'


def _goal(record: SessionRecord) -> str:
    goal = record.choices.get('goal')
    if isinstance(goal, list) and goal:
        return '+'.join(str(g) for g in goal)
    if isinstance(goal, str) and goal:
        return goal
    return 'custom' if record.choices.get('goal:text') else record.profile_id


def session_row(view: SessionView, now: datetime.datetime) -> str:
    r = view.record
    return (
        f'{mark(view.state)} {session_label(r)} · {state_text(view)} · {_goal(r)} · {r.mode} · '
        f'{r.backend} · {age(r.created, now)}'
    )


def worktree_row(info: WorktreeInfo, states: dict[str, str]) -> str:
    size = format_bytes(info.bytes) if info.bytes is not None else 'size unknown'
    state = states.get(info.session, 'ended')
    return f'{info.branch} · {size} · session {info.session} ({state}) · {info.path}'


def session_rows(
    sessions: Iterable[SessionView], now: Optional[datetime.datetime] = None
) -> list[tuple[str, str]]:
    """(session id, row text) in drawer order."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return [(v.record.id, session_row(v, now)) for v in sessions]

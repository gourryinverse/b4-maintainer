# SPDX-License-Identifier: GPL-2.0-or-later
"""What the app sees of agent sessions: an overview to poll, and explicit actions.

The runner owns the agent processes (tmux windows in phase 1); the store owns records;
worktrees are listed with their disk usage. Only the maintainer's actions call the
runner: polling reads state and never starts, stops, or resumes anything.
"""

import datetime
import os
import pathlib
import shutil
import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Protocol, Sequence

from .backends import BackendEntry, Which, backend_entries
from .launch import LaunchChoice, Situation, Target, new_record, plan
from .policy import ContextItem
from .profiles import BUILTIN_DIR, Profile, load_profiles
from .records import SessionRecord
from .skills import BUILTIN_DIR as BUILTIN_SKILLS
from .skills import Skill, load_skills
from .store import SessionStore, SessionView

RESUMABLE = frozenset({'stopped', 'gone'})


class SessionError(Exception):
    pass


class SessionRunner(Protocol):
    def blocker(self) -> Optional[str]:
        """Why no session can launch here (e.g. no tmux); None if they can."""
        ...

    def launch(self, record: SessionRecord, request: Any) -> None: ...

    def alive(self, records: Sequence[SessionRecord]) -> set[str]: ...

    def switch(self, record: SessionRecord) -> None: ...

    def stop(self, record: SessionRecord) -> None: ...

    def resume(self, record: SessionRecord) -> None: ...

    def end(self, record: SessionRecord) -> None: ...


@dataclass(frozen=True)
class WorktreeInfo:
    path: str
    branch: str
    session: str
    bytes: Optional[int] = None  # Not measured yet.


class Worktrees(Protocol):
    def list(self, records: Sequence[SessionRecord]) -> list[WorktreeInfo]: ...


@dataclass(frozen=True)
class Overview:
    sessions: tuple[SessionView, ...] = ()  # Not ended; most urgent first.
    worktrees: tuple[WorktreeInfo, ...] = ()

    @property
    def worktree_bytes(self) -> int:
        return sum(w.bytes or 0 for w in self.worktrees)


class IdleRunner:
    """Until an adapter exists: launches nothing, and windows are as *alive* says."""

    def __init__(self, alive: bool = False) -> None:
        self._alive = alive

    def blocker(self) -> Optional[str]:
        return 'agent sessions cannot launch yet: this build has no session runner'

    def launch(self, record: SessionRecord, request: Any) -> None:
        raise SessionError('No session runner configured')

    def alive(self, records: Sequence[SessionRecord]) -> set[str]:
        return {r.id for r in records} if self._alive else set()

    def switch(self, record: SessionRecord) -> None:
        raise SessionError(f'Session {record.id} has no window to switch to')

    def stop(self, record: SessionRecord) -> None:
        pass

    def resume(self, record: SessionRecord) -> None:
        raise SessionError(f'Session {record.id} cannot be resumed here')

    def end(self, record: SessionRecord) -> None:
        pass


def disk_usage(path: pathlib.Path) -> int:
    """Bytes allocated under *path*, not following symlinks."""
    total = 0
    for top, dirs, files in os.walk(path):
        for name in dirs + files:
            try:
                total += os.lstat(os.path.join(top, name)).st_blocks * 512
            except OSError:
                continue
    return total


class RecordWorktrees:
    """Worktrees named by session records, measured at most every *ttl* seconds."""

    def __init__(self, ttl: float = 300.0) -> None:
        self.ttl = ttl
        self._sizes: dict[str, tuple[float, int]] = {}

    def list(self, records: Sequence[SessionRecord]) -> list[WorktreeInfo]:
        infos = []
        now = time.monotonic()
        for record in records:
            wt = record.worktree
            if wt is None or not os.path.isdir(wt.path):
                continue
            cached = self._sizes.get(wt.path)
            if cached is None or now - cached[0] > self.ttl:
                cached = (now, disk_usage(pathlib.Path(wt.path)))
                self._sizes[wt.path] = cached
            infos.append(WorktreeInfo(wt.path, wt.branch, record.id, cached[1]))
        return infos


class SessionHub:
    """Blocking calls (files, tmux, disk usage): call from worker threads."""

    def __init__(
        self,
        store: SessionStore,
        runner: SessionRunner,
        worktrees: Optional[Worktrees] = None,
        profiles_dir: Optional[pathlib.Path] = None,
        skills_dir: Optional[pathlib.Path] = None,
        which: Which = shutil.which,
    ) -> None:
        self.store = store
        self.runner = runner
        self.worktrees = worktrees or RecordWorktrees()
        self.profiles_dir = profiles_dir  # The maintainer's own (state/profiles/).
        self.skills_dir = skills_dir  # The maintainer's own (state/skills/).
        self.which = which
        self._states: dict[str, str] = {}

    def overview(self) -> Overview:
        views = self.store.poll(self.runner.alive)
        self._states = {v.record.id: v.state for v in views}
        live = tuple(v for v in views if v.state != 'ended')
        worktrees = self.worktrees.list([v.record for v in views])
        return Overview(live, tuple(worktrees))

    def blocker(self) -> Optional[str]:
        return self.runner.blocker()

    def profiles(self) -> tuple[list[Profile], list[str]]:
        return load_profiles(BUILTIN_DIR, self.profiles_dir)

    def skills(self) -> tuple[list[Skill], list[str]]:
        return load_skills(BUILTIN_SKILLS, self.skills_dir)

    def backends(self, config: Sequence[Mapping[str, Any]]) -> list[BackendEntry]:
        return backend_entries(config, self.which)

    def launch(
        self,
        target: Target,
        profile: Profile,
        choice: LaunchChoice,
        situation: Situation,
        items: Sequence[ContextItem],
        now: Optional[datetime.datetime] = None,
    ) -> SessionRecord:
        """Record the session, then start it; a failed start leaves it marked failed."""
        planned = plan(profile, choice, situation, items)
        if not planned.ok:
            raise SessionError('; '.join(planned.blockers))
        now = now or datetime.datetime.now(datetime.timezone.utc)
        record = new_record(target, profile, choice, planned, now)
        self.store.create(record)
        try:
            self.runner.launch(record, planned.request)
        except Exception as exc:
            self.store.set_state(record.id, 'failed')
            raise SessionError(f'Session {record.id} failed to start: {exc}') from exc
        return record

    def open(self, sid: str) -> None:
        """Switch to the session's window, resuming it first when stopped or gone."""
        record = self.store.record(sid)
        state = self._states.get(sid, record.state)
        if state == 'ended':
            raise SessionError(f'Session {sid} has ended')
        if state in RESUMABLE:
            self.runner.resume(record)
            self.store.set_state(sid, '')
        self.runner.switch(record)

    def stop(self, sid: str) -> None:
        self.runner.stop(self.store.record(sid))
        self.store.set_state(sid, 'stopped')

    def end(self, sid: str) -> None:
        """Close the session for good; its worktree stays until removed."""
        self.runner.end(self.store.record(sid))
        self.store.set_state(sid, 'ended')

    def artifacts(self, sid: str) -> list[str]:
        return self.store.artifacts(sid)

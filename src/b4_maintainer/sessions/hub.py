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
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence

from .launch import LaunchChoice, Situation, Target, new_record, plan
from .models import ModelChoice, model_choices, parse_catalog
from .pi import LaunchSpec, prepare_session
from .policy import ContextItem
from .profiles import BUILTIN_DIR, Profile, load_profiles
from .records import SessionRecord
from .sandbox import available
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

    def launch(self, record: SessionRecord, spec: LaunchSpec) -> None: ...

    def alive(self, records: Sequence[SessionRecord]) -> set[str]: ...

    def switch(self, record: SessionRecord) -> None: ...

    def stop(self, record: SessionRecord) -> None: ...

    def resume(self, record: SessionRecord) -> None: ...

    def end(self, record: SessionRecord) -> None: ...

    # The column beside the app (tmux panes in phase 1).
    def joined(self, app_pane: str) -> Optional[str]: ...

    def join(
        self, record: SessionRecord, app_pane: str, width: int = 90, above: str = ''
    ) -> None: ...

    def release(self, app_pane: str) -> None: ...

    def focus(self, record: SessionRecord, zoom: bool = False) -> None: ...

    def focus_app(self, app_pane: str) -> None: ...

    def announce(self, app_pane: str) -> None: ...

    def pane_of(self, record: SessionRecord) -> Optional[str]: ...


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


def pi_catalog() -> Optional[str]:
    """`pi --list-models` (offline), or None when pi is missing or fails."""
    pi = shutil.which('pi')
    if pi is None:
        return None
    try:
        run = subprocess.run(
            [pi, '--offline', '--list-models'],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return run.stdout if run.returncode == 0 else None


class SessionHub:
    """Blocking calls (files, tmux, disk usage): call from worker threads."""

    def __init__(
        self,
        store: SessionStore,
        runner: SessionRunner,
        worktrees: Optional[Worktrees] = None,
        profiles_dir: Optional[pathlib.Path] = None,
        skills_dir: Optional[pathlib.Path] = None,
        catalog: Optional[Callable[[], Optional[str]]] = None,
        sandbox: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.store = store
        self.runner = runner
        self.worktrees = worktrees or RecordWorktrees()
        self.profiles_dir = profiles_dir  # The maintainer's own (state/profiles/).
        self.skills_dir = skills_dir  # The maintainer's own (state/skills/).
        self._catalog = catalog or pi_catalog
        self._sandbox = sandbox or (lambda: bool(available()))
        self._catalog_text: Optional[str] = None
        self._catalog_read = False
        self._catalog_lock = threading.Lock()
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

    def read_catalog(self) -> None:
        """Read pi's model list once (about a second: pi starts); later calls wait for it."""
        with self._catalog_lock:
            if not self._catalog_read:
                self._catalog_text, self._catalog_read = self._catalog(), True

    def models(self, presets: Sequence[Mapping[str, Any]]) -> list[ModelChoice]:
        """Model choices from pi's catalog (None if pi cannot list it)."""
        self.read_catalog()
        text = self._catalog_text
        return model_choices(parse_catalog(text) if text else None, presets)

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
        assert planned.request is not None and planned.model is not None
        installed = {s.id: s.path for s in situation.skills}
        sandboxed = self._sandbox()
        spec = LaunchSpec(
            planned.request,
            tuple(p for s in planned.skills if (p := installed.get(s)) is not None),
            planned.model.args,
            planned.model.env,
            sandboxed,
        )
        self.store.create(record)
        try:
            prepare_session(self.store, record, planned.request, target.source, sandboxed)
            self.runner.launch(record, spec)
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

    def show(
        self, sid: str, app_pane: str, width: int = 90, focus: bool = False, above: str = ''
    ) -> None:
        """Put the session beside the app, resuming it first if it was stopped or gone
        (only ever on the maintainer's request)."""
        record = self.store.record(sid)
        state = self._states.get(sid, record.state)
        if state == 'ended':
            raise SessionError(f'Session {sid} has ended')
        if state in RESUMABLE:
            self.runner.resume(record)
            self.store.set_state(sid, '')
        self.runner.join(record, app_pane, width, above)
        if focus:
            self.runner.focus(record)

    def hide(self, app_pane: str) -> None:
        """Send the session beside the app back to its own window (it keeps running)."""
        self.runner.release(app_pane)

    def focus(self, sid: str, zoom: bool = False) -> None:
        self.runner.focus(self.store.record(sid), zoom)

    def focus_app(self, app_pane: str) -> None:
        self.runner.focus_app(app_pane)

    def announce(self, app_pane: str) -> None:
        self.runner.announce(app_pane)

    def pane_of(self, sid: str) -> Optional[str]:
        return self.runner.pane_of(self.store.record(sid))

    def stop(self, sid: str) -> None:
        self.runner.stop(self.store.record(sid))
        self.store.set_state(sid, 'stopped')

    def end(self, sid: str) -> None:
        """Close the session for good; its worktree stays until removed."""
        self.runner.end(self.store.record(sid))
        self.store.set_state(sid, 'ended')

    def artifacts(self, sid: str) -> list[str]:
        return self.store.artifacts(sid)

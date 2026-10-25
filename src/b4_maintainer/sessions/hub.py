# SPDX-License-Identifier: GPL-2.0-or-later
"""What the app sees of agent sessions: an overview to poll, and explicit actions.

The runner owns the agent processes (tmux windows in phase 1); the store owns records;
worktrees are listed with their disk usage. Only the maintainer's actions call the
runner: polling reads state and never starts, stops, or resumes anything.
"""

import dataclasses
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
from .pane import LIVE
from .pi import LaunchSpec, prepare_session
from .policy import ContextItem
from .profiles import BUILTIN_DIR, Profile, load_profiles
from .records import SessionRecord, Worktree
from .sandbox import available
from .skills import BUILTIN_DIR as BUILTIN_SKILLS
from .skills import Skill, load_skills
from .store import SessionStore, SessionView
from .trees import TreeState, inspect_tree, remove_tree

RESUMABLE = frozenset({'stopped', 'gone'})


class SessionError(Exception):
    def __init__(self, message: str, detail: str = '') -> None:
        super().__init__(message)
        self.detail = detail  # Tool output behind the message (b4's, for a tree).


class TreeError(Exception):
    """A session tree could not be prepared; *output* is what b4 or git said."""

    def __init__(self, message: str, output: str = '') -> None:
        super().__init__(message)
        self.output = output


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
    session: str  # Empty: an orphan, named by no session.
    bytes: Optional[int] = None  # Not measured yet.
    state: Optional[TreeState] = None  # Not inspected yet.


class Worktrees(Protocol):
    def list(self, records: Sequence[SessionRecord]) -> list[WorktreeInfo]: ...

    def reason(self, project: str) -> str:
        """Why no tree can be prepared for the project's series; empty if one can."""
        ...

    def prepare(self, record: SessionRecord, source: str, base: str) -> Worktree:
        """The series applied in the session's own tree (*base* empty: b4 detects it)."""
        ...

    def bases(self, project: str) -> Sequence[str]:
        """Suggested bases for the project's series (b4's)."""
        ...

    def inspect(self, path: str, tree: Optional[Worktree]) -> TreeState:
        """What removing the tree would lose, looked at now (*tree*: its session's record
        of it; None for an orphan)."""
        ...

    def remove(self, path: str) -> None: ...


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


AGENT_NAME = 'b4-maintainer agent ({model})'
AGENT_EMAIL = 'agent@b4-maintainer.invalid'


class RecordWorktrees:
    """Session trees under *directory*, prepared through b4 from the project checkouts;
    listed from session records, measured at most every *ttl* seconds."""

    def __init__(
        self,
        directory: Optional[pathlib.Path] = None,
        checkouts: Optional[Mapping[str, str]] = None,
        agent: tuple[str, str] = (AGENT_NAME, AGENT_EMAIL),
        ttl: float = 300.0,
    ) -> None:
        self.directory = directory
        self.checkouts = dict(checkouts or {})
        self.agent = agent  # Commit identity; the name may use {model}.
        self.ttl = ttl
        self._sizes: dict[str, tuple[float, int, TreeState]] = {}

    def reason(self, project: str) -> str:
        if self.directory is None or not self.checkouts.get(project):
            return f'project {project or "(none)"} has no checkout'
        return ''

    def prepare(self, record: SessionRecord, source: str, base: str) -> Worktree:
        from ..adapters.sessiontree import prepare_tree  # b4: only when preparing.

        checkout, directory = self.checkouts.get(record.project), self.directory
        if not checkout or directory is None:
            raise TreeError(self.reason(record.project))
        change_id = record.series.removeprefix('change-id:')
        name = self.agent[0].format(model=record.model or record.preset)
        return prepare_tree(
            pathlib.Path(checkout),
            directory / record.id,
            f'b4m/session/{record.id}',
            source,
            record.revision,
            change_id=change_id if change_id != record.series else '',
            base=base or None,
            identity=(name, self.agent[1]),
        )

    def list(self, records: Sequence[SessionRecord]) -> list[WorktreeInfo]:
        """Trees named by sessions, then orphans: directories here that none names."""
        infos = []
        named = set()
        for record in records:
            wt = record.worktree
            if wt is None or not os.path.isdir(wt.path):
                continue
            named.add(os.path.realpath(wt.path))
            size, state = self._measure(wt.path, wt.applied, wt)
            infos.append(WorktreeInfo(wt.path, wt.branch, record.id, size, state))
        if self.directory is not None and self.directory.is_dir():
            for entry in sorted(self.directory.iterdir()):
                if not entry.is_dir() or os.path.realpath(entry) in named:
                    continue
                size, state = self._measure(str(entry), '')
                infos.append(WorktreeInfo(str(entry), _branch(entry), '', size, state))
        return infos

    def _measure(
        self, path: str, applied: str, tree: Optional[Worktree] = None
    ) -> tuple[int, TreeState]:
        now = time.monotonic()
        cached = self._sizes.get(path)
        if cached is None or now - cached[0] > self.ttl:
            where = pathlib.Path(path)
            state = inspect_tree(where, applied, tree)
            size = disk_usage(where) + sum(
                disk_usage(pathlib.Path(e))
                for e in state.extras
                if not pathlib.Path(e).resolve().is_relative_to(where.resolve())
            )
            cached = (now, size, state)
            self._sizes[path] = cached
        return cached[1], cached[2]

    def bases(self, project: str) -> Sequence[str]:
        from ..adapters.sessiontree import base_suggestions  # b4: only when asked.

        checkout = self.checkouts.get(project)
        return base_suggestions(pathlib.Path(checkout)) if checkout else []

    def inspect(self, path: str, tree: Optional[Worktree]) -> TreeState:
        return inspect_tree(pathlib.Path(path), tree.applied if tree else '', tree)

    def remove(self, path: str) -> None:
        if self.directory is None:
            raise OSError(f'{path} is not a session tree')
        remove_tree(pathlib.Path(path), self.directory)
        self._sizes.pop(path, None)


def _branch(tree: pathlib.Path) -> str:
    run = subprocess.run(
        ['git', 'symbolic-ref', '-q', '--short', 'HEAD'], cwd=tree, capture_output=True, text=True
    )
    return run.stdout.strip()


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
        sandbox: Optional[Callable[[], Any]] = None,  # Truthy where the sandbox works.
    ) -> None:
        self.store = store
        self.runner = runner
        self.worktrees = worktrees or RecordWorktrees()
        self.profiles_dir = profiles_dir  # The maintainer's own (state/profiles/).
        self.skills_dir = skills_dir  # The maintainer's own (state/skills/).
        self._catalog = catalog or pi_catalog
        self._sandbox = sandbox or available
        self._catalog_text: Optional[str] = None
        self._catalog_read = False
        self._catalog_lock = threading.Lock()
        self._states: dict[str, str] = {}
        self._preparing: set[str] = set()  # Sessions this app is preparing a tree for.

    def _poll(self) -> list[SessionView]:
        """Sessions with their states; a preparation no worker here is running (the app
        quit during it) shows as failed."""
        return [
            dataclasses.replace(v, state='failed')
            if v.state == 'preparing' and v.record.id not in self._preparing
            else v
            for v in self.store.poll(self.runner.alive)
        ]

    def overview(self) -> Overview:
        views = self._poll()
        self._states = {v.record.id: v.state for v in views}
        live = tuple(v for v in views if v.state != 'ended')
        worktrees = self.worktrees.list([v.record for v in views])
        return Overview(live, tuple(worktrees))

    def blocker(self) -> Optional[str]:
        return self.runner.blocker()

    def records(self) -> list[SessionRecord]:
        """Every session's record, ended ones included."""
        return [v.record for v in self._poll()]

    def profiles(self) -> tuple[list[Profile], list[str]]:
        return load_profiles(BUILTIN_DIR, self.profiles_dir)

    def skills(self) -> tuple[list[Skill], list[str]]:
        return load_skills(BUILTIN_SKILLS, self.skills_dir)

    def worktree_reason(self, project: str) -> str:
        """Why a worktree session for the project's series cannot launch; empty if it can.
        Blocking (checks the sandbox once)."""
        sandbox = self._sandbox()
        if not sandbox:
            why = getattr(sandbox, 'reason', '')
            return 'they build and run in a sandbox, and none works here' + (
                f' ({why})' if why else ''
            )
        return self.worktrees.reason(project)

    def bases(self, project: str) -> Sequence[str]:
        """Suggested bases for a worktree session of the project's series; none on error."""
        try:
            return self.worktrees.bases(project)
        except Exception:
            return []

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
        sandboxed = bool(self._sandbox())
        spec = LaunchSpec(
            planned.request,
            tuple(p for s in planned.skills if (p := installed.get(s)) is not None),
            planned.model.args,
            planned.model.env,
            sandboxed,
        )
        if planned.mode == 'worktree' and target.scope != 'series':
            raise SessionError('worktree sessions apply a series; this session has none')
        tree_mode = planned.mode == 'worktree'
        if tree_mode:
            self._preparing.add(record.id)
            record = record.replace(state='preparing')
        self.store.create(record)
        try:
            if tree_mode:
                tree = self.worktrees.prepare(record, target.source, choice.base)
                record = record.replace(worktree=tree, state='')
                self.store.save(record)
                self._preparing.discard(record.id)
            prepare_session(
                self.store, record, planned.request, target.source, sandboxed, planned.included
            )
            self.runner.launch(record, spec)
        except TreeError as exc:
            self._preparing.discard(record.id)
            self.store.set_state(record.id, 'failed')
            raise SessionError(f'Session {record.id}: {exc}', exc.output) from exc
        except Exception as exc:
            self._preparing.discard(record.id)
            self.store.set_state(record.id, 'failed')
            raise SessionError(f'Session {record.id} failed to start: {exc}') from exc
        return record

    def _ready(self, sid: str) -> SessionRecord:
        """The session's record; refused while its worktree is being prepared."""
        if sid in self._preparing:
            raise SessionError(f'Session {sid} is still being prepared (b4 applies the series)')
        return self.store.record(sid)

    def open(self, sid: str) -> None:
        """Switch to the session's window, resuming it first when stopped or gone."""
        record = self._ready(sid)
        state = self._states.get(sid, record.state)
        if state == 'ended':
            raise SessionError(f'Session {sid} has ended')
        if state in RESUMABLE:
            self._resume(record)
        self.runner.switch(record)

    def _resume(self, record: SessionRecord) -> None:
        tree = record.worktree
        if tree is not None and not os.path.isdir(tree.path):
            raise SessionError(f'Session {record.id} cannot resume: its worktree was removed')
        self.runner.resume(record)
        self.store.set_state(record.id, '')

    def show(
        self, sid: str, app_pane: str, width: int = 90, focus: bool = False, above: str = ''
    ) -> None:
        """Put the session beside the app, resuming it first if it was stopped or gone
        (only ever on the maintainer's request)."""
        record = self._ready(sid)
        state = self._states.get(sid, record.state)
        if state == 'ended':
            raise SessionError(f'Session {sid} has ended')
        if state in RESUMABLE:
            self._resume(record)
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
        self.runner.stop(self._ready(sid))
        self.store.set_state(sid, 'stopped')

    def end(self, sid: str) -> None:
        """Close the session for good; its worktree stays until removed."""
        self.runner.end(self._ready(sid))
        self.store.set_state(sid, 'ended')

    def _tree_owners(self, paths: Sequence[str]) -> dict[str, Optional[SessionRecord]]:
        """Each path's session (None for an orphan); refuses live sessions' trees and
        paths that are not listed trees."""
        views = {v.record.id: v for v in self._poll()}
        listed = {
            w.path: w.session for w in self.worktrees.list([v.record for v in views.values()])
        }
        owners: dict[str, Optional[SessionRecord]] = {}
        for path in paths:
            if path not in listed:
                raise SessionError(f'{path} is not a session tree')
            view = views.get(listed[path])
            if view is not None and view.state in LIVE | {'preparing'}:
                raise SessionError(
                    f'{path} belongs to a live session: stop session {view.record.id} first'
                )
            owners[path] = view.record if view is not None else None
        return owners

    def worktree_losses(self, paths: Sequence[str]) -> dict[str, TreeState]:
        """What removing each tree would lose (uncommitted files, commits only there)."""
        owners = self._tree_owners(paths)
        return {
            path: self.worktrees.inspect(path, r.worktree if r else None)
            for path, r in owners.items()
        }

    def remove_worktrees(self, paths: Sequence[str]) -> list[str]:
        """Delete the trees (the maintainer confirmed); their records stay as they were."""
        self._tree_owners(paths)
        removed = []
        for path in paths:
            try:
                self.worktrees.remove(path)
            except OSError as exc:
                raise SessionError(f'Cannot remove {path}: {exc}') from exc
            removed.append(path)
        return removed

    def artifacts(self, sid: str) -> list[str]:
        return self.store.artifacts(sid)

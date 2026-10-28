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

from ..adapters.bases import BaseCommit, BaseStatus
from ..workspace import MaintainerBase, Workspace
from .applysession import apply_record, apply_request
from .guidance import provenance
from .launch import LaunchChoice, Situation, Target, new_record, plan
from .models import ModelChoice, model_choices, parse_catalog
from .pane import LIVE
from .pi import LaunchSpec, prepare_session
from .policy import ContextItem, launch_check
from .profiles import BUILTIN_DIR, Profile, load_profiles
from .rebase import rebase_record, rebase_request
from .records import ProjectTree, SessionRecord, Worktree, session_trees
from .sandbox import available
from .skills import BUILTIN_DIR as BUILTIN_SKILLS
from .skills import Skill, load_skills
from .store import SessionStore, SessionView
from .trees import TreeItem, TreeState, inspect_tree, remove_tree, series_tip

RESUMABLE = frozenset({'stopped', 'gone'})
COUNTED_LIVE = LIVE | {'preparing'}  # Limits count a session from its preparation.


class SessionError(Exception):
    def __init__(self, message: str, detail: str = '', base: str = '', project: str = '') -> None:
        super().__init__(message)
        self.detail = detail  # Tool output behind the message (b4's, for a tree).
        self.base = base  # A series that conflicts: where it would have applied.
        self.project = project  # A workbench session's tree that conflicts: its project.


class TreeError(Exception):
    """A session tree could not be prepared; *output* is what b4 or git said; *base*, for
    a series that conflicts, the commit it would have applied on (an agent can take it
    from there)."""

    def __init__(self, message: str, output: str = '', base: str = '', project: str = '') -> None:
        super().__init__(message)
        self.output = output
        self.base = base
        self.project = project  # In a workbench session: the tree's project.


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
class TreePlan:
    """One project's tree in a workbench session: its base (empty: b4 chooses for a first
    series), then *items* (series and local branches) applied in order. *agent_apply*:
    they did not apply there; the tree starts at that commit and the agent applies them."""

    project: str
    base: str = ''
    items: tuple[TreeItem, ...] = ()
    agent_apply: str = ''
    # apply: its items applied; deliver: a tree at its base, its items for the agent to use
    # as the task directs; none: no tree, its mail only.
    use: str = 'apply'


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

    def checkout(self, project: str) -> str:
        """The project's checkout, for a session to read; empty if it has none."""
        ...

    def prepare(
        self,
        record: SessionRecord,
        source: str,
        base: str,
        also: Sequence[tuple[str, int, str]] = (),
    ) -> Worktree:
        """The series applied in the session's own tree (*base* empty: b4 detects it),
        then any further series (*also*: source, revision, subject) in order."""
        ...

    def prepare_unapplied(self, record: SessionRecord, source: str, base: str) -> Worktree:
        """A tree at *base* without the series (b4 could not apply it there): b4's mbox
        of it is left for the agent."""
        ...

    def prepare_with_mbox(self, record: SessionRecord, ambytes: bytes, base: str) -> Worktree:
        """A tree at *base* with *ambytes* (an am-ready mbox) left for the agent."""
        ...

    def prepare_entry(self, record: SessionRecord, plan: TreePlan) -> ProjectTree:
        """One project's tree of a workbench session, in the session's folder."""
        ...

    def remove_home(self, record: SessionRecord) -> None:
        """A workbench session's folder and every tree in it (a failed preparation)."""
        ...

    def prepare_rebase(self, record: SessionRecord, base: MaintainerBase) -> Worktree:
        """A rebase session's tree: a copy of *base* and its upstream."""
        ...

    def prepare_start(self, record: SessionRecord, start: str, start_from: str) -> Worktree:
        """A derived session's tree at *start* (in session *start_from*'s tree, or the
        project's), no series applied."""
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

    def checkout(self, project: str) -> str:
        return self.checkouts.get(project, '')

    def prepare(
        self,
        record: SessionRecord,
        source: str,
        base: str,
        also: Sequence[tuple[str, int, str]] = (),
    ) -> Worktree:
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
            also=[(src, rev) for src, rev, _subject in also],
        )

    def prepare_start(self, record: SessionRecord, start: str, start_from: str) -> Worktree:
        from ..adapters.sessiontree import prepare_start_tree

        checkout, directory = self.checkouts.get(record.project), self.directory
        if not checkout or directory is None:
            raise TreeError(self.reason(record.project))
        source = directory / start_from if start_from else None
        if source is not None and not (source / '.git').exists():
            raise TreeError(f'session {start_from} has no tree to start from')
        name = self.agent[0].format(model=record.model or record.preset)
        return prepare_start_tree(
            pathlib.Path(checkout),
            directory / record.id,
            f'b4m/session/{record.id}',
            start,
            (name, self.agent[1]),
            source=source,
        )

    def prepare_unapplied(self, record: SessionRecord, source: str, base: str) -> Worktree:
        from ..adapters.sessiontree import series_mbox

        checkout = self.checkouts.get(record.project)
        if not checkout:
            raise TreeError(self.reason(record.project))
        return self.prepare_with_mbox(
            record, series_mbox(pathlib.Path(checkout), source, record.revision), base
        )

    def prepare_with_mbox(self, record: SessionRecord, ambytes: bytes, base: str) -> Worktree:
        from ..adapters.sessiontree import prepare_unapplied_tree

        checkout, directory = self.checkouts.get(record.project), self.directory
        if not checkout or directory is None:
            raise TreeError(self.reason(record.project))
        name = self.agent[0].format(model=record.model or record.preset)
        return prepare_unapplied_tree(
            pathlib.Path(checkout),
            directory / record.id,
            f'b4m/session/{record.id}',
            ambytes,
            base,
            (name, self.agent[1]),
        )

    def prepare_entry(self, record: SessionRecord, plan: TreePlan) -> ProjectTree:
        from ..adapters.sessiontree import items_mbox, prepare_items_tree, prepare_unapplied_tree

        checkout, directory = self.checkouts.get(plan.project), self.directory
        if not checkout or directory is None:
            raise TreeError(self.reason(plan.project))
        home = directory / record.id
        home.mkdir(parents=True, exist_ok=True)
        name = self.agent[0].format(model=record.model or record.preset)
        identity, branch = (name, self.agent[1]), f'b4m/session/{record.id}'
        project, dest = pathlib.Path(checkout), home / plan.project
        labels = tuple(i.label for i in plan.items if i.apply)
        if plan.use == 'deliver':
            start = plan.base or 'HEAD'
            ambytes = items_mbox(project, start, plan.items)
            tree = prepare_unapplied_tree(project, dest, branch, ambytes, start, identity)
            return ProjectTree(plan.project, tree, (), labels)
        if plan.agent_apply:
            ambytes = items_mbox(project, plan.agent_apply, plan.items)
            tree = prepare_unapplied_tree(
                project, dest, branch, ambytes, plan.agent_apply, identity
            )
        else:
            tree = prepare_items_tree(project, dest, branch, plan.base, plan.items, identity)
        return ProjectTree(plan.project, tree, labels)

    def remove_home(self, record: SessionRecord) -> None:
        if self.directory is None:
            return
        home = self.directory / record.id
        if home.is_dir() and not home.is_symlink():
            for tree in sorted(home.iterdir()):
                if tree.is_dir():
                    remove_tree(tree, home)
            home.rmdir()

    def prepare_rebase(self, record: SessionRecord, base: MaintainerBase) -> Worktree:
        from ..adapters.bases import prepare_rebase_tree

        checkout, directory = self.checkouts.get(record.project), self.directory
        if not checkout or directory is None:
            raise TreeError(self.reason(record.project))
        name = self.agent[0].format(model=record.model or record.preset)
        try:
            return prepare_rebase_tree(
                pathlib.Path(checkout),
                directory / record.id,
                f'b4m/session/{record.id}',
                base,
                (name, self.agent[1]),
            )
        except OSError as ex:
            raise TreeError(f'cannot prepare the rebase: {ex}', str(ex)) from ex

    def list(self, records: Sequence[SessionRecord]) -> list[WorktreeInfo]:
        """Trees named by sessions, then orphans: directories here that none names."""
        infos = []
        named = set()
        for record in records:
            if record.home:
                named.add(os.path.realpath(record.home))  # Its trees are listed one by one.
            for _project, wt in session_trees(record):
                if not os.path.isdir(wt.path):
                    continue
                named.add(os.path.realpath(wt.path))
                size, state = self._measure(wt.path, series_tip(pathlib.Path(wt.path), wt), wt)
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
        tip = series_tip(pathlib.Path(path), tree) if tree else ''
        return inspect_tree(pathlib.Path(path), tip, tree)

    def remove(self, path: str) -> None:
        """A tree directly under the directory, or in a workbench session's folder there
        (the folder goes with its last tree)."""
        if self.directory is None:
            raise OSError(f'{path} is not a session tree')
        where, directory = pathlib.Path(path).resolve(), self.directory.resolve()
        home = where.parent
        if home != directory and home.parent == directory:
            remove_tree(where, home)
            if not any(home.iterdir()):
                home.rmdir()
        else:
            remove_tree(where, self.directory)
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
        guidance: Optional[Callable[[], tuple[list[Skill], list[str]]]] = None,
    ) -> None:
        self.store = store
        self.runner = runner
        self.worktrees = worktrees or RecordWorktrees()
        self.profiles_dir = profiles_dir  # The maintainer's own (state/profiles/).
        self.skills_dir = skills_dir  # The maintainer's own (state/skills/).
        self._catalog = catalog or pi_catalog
        self._sandbox = sandbox or available
        self._guidance = guidance  # Skills from the guidance repositories.
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

    def is_live(self, sid: str) -> bool:
        """Whether session *sid* is running or still being prepared."""
        return any(v.record.id == sid and v.state in COUNTED_LIVE for v in self._poll())

    def records(self) -> list[SessionRecord]:
        """Every session's record, ended ones included."""
        return [v.record for v in self._poll()]

    def profiles(self) -> tuple[list[Profile], list[str]]:
        return load_profiles(BUILTIN_DIR, self.profiles_dir)

    def skills(self) -> tuple[list[Skill], list[str]]:
        """Guidance repositories' skills, then built-in ones, then the maintainer's own
        (each replacing by id)."""
        found, problems = self._guidance() if self._guidance is not None else ([], [])
        builtin, more = load_skills(BUILTIN_SKILLS, self.skills_dir)
        by_id = {s.id: s for s in [*found, *builtin]}
        return list(by_id.values()), [*problems, *more]

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

    def situation(
        self,
        workspace: Workspace,
        overview: Overview,
        skills: Sequence[Skill],
        project: str,
        bases: bool = True,
    ) -> Situation:
        """What a launch for *project* is checked against: models, limits, live counts,
        worktrees, and (with *bases*) suggested bases. Blocking."""
        counted = [v for v in overview.sessions if v.state in COUNTED_LIVE]
        reason = self.worktree_reason(project)
        return Situation(
            self.models(workspace.models),
            workspace.session_limits,
            live=len(counted),
            live_builds=sum(v.record.mode == 'worktree' for v in counted),
            worktrees=len(overview.worktrees),
            worktree_bytes=overview.worktree_bytes,
            worktree_reason=reason,
            skills=tuple(skills),
            bases=() if reason or not bases else tuple(self._base_suggestions(workspace, project)),
        )

    def _base_suggestions(self, workspace: Workspace, project: str) -> list[str]:
        """b4's suggestions, then the project's base refs (read-only bases)."""
        from ..adapters.scratch import base_refs

        found = workspace.project(project)
        checkout = self.worktrees.checkout(project)
        extra = base_refs(pathlib.Path(checkout), found.base_refs) if found and checkout else []
        return list(dict.fromkeys([*self.bases(project), *extra]))

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
        installed = {s.id: s for s in situation.skills}
        chosen = [installed[s] for s in planned.skills if s in installed]
        sandboxed = bool(self._sandbox())
        spec = LaunchSpec(
            planned.request,
            tuple(s.path for s in chosen if s.path is not None),
            planned.model.args,
            planned.model.env,
            sandboxed,
            tuple(s.root for s in chosen if s.root is not None),
        )
        guidance = provenance(chosen, planned.skills)
        if guidance:
            record = record.replace(provenance={**record.provenance, **guidance})
        if planned.mode == 'worktree' and target.scope != 'series':
            raise SessionError('worktree sessions apply a series; this session has none')
        uses = dict(choice.trees) if target.scope == 'series' else {}
        own = uses.get(target.project, '')
        others = [(p, u) for p, u in uses.items() if p != target.project]
        reads = [p for p, u in others if u == 'read']
        if own == 'read' or (planned.mode == 'lightweight' and uses):
            reads.insert(0, target.project)
        read = tuple((p, c) for p in reads if (c := self.worktrees.checkout(p)))
        record = record.replace(reads=read)
        extra = [p for p, u in others if u == 'base']
        if planned.mode == 'worktree' and (extra or own == 'deliver'):
            bases = dict(choice.bases)
            plans = [TreePlan(p, bases.get(p, ''), ()) for p in extra]
            if own in ('apply', 'deliver'):
                label = f'{target.subject} v{target.revision}'
                item = TreeItem(label, target.source, target.revision)
                mine = TreePlan(target.project, choice.base, (item,), choice.agent_apply, own)
                plans.insert(0, mine)
            return self._start(
                record,
                spec,
                None,
                lambda r: (target.source, planned.included),
                self._preparer(plans),
            )

        def tree(r: SessionRecord) -> Worktree:
            if choice.agent_apply:
                return self.worktrees.prepare_unapplied(r, target.source, choice.agent_apply)
            if choice.start:
                return self.worktrees.prepare_start(r, choice.start, choice.start_from)
            if choice.also:
                return self.worktrees.prepare(r, target.source, choice.base, choice.also)
            return self.worktrees.prepare(r, target.source, choice.base)

        return self._start(
            record,
            spec,
            tree if planned.mode == 'worktree' else None,
            lambda r: (target.source, planned.included),
        )

    def _start(
        self,
        record: SessionRecord,
        spec: LaunchSpec,
        tree: Optional[Callable[[SessionRecord], Worktree]],
        package: Callable[[SessionRecord], tuple[Optional[str], Sequence[ContextItem]]],
        trees: Optional[Callable[[SessionRecord], SessionRecord]] = None,
        threads: Sequence[tuple[str, str, str]] = (),
        files: Sequence[tuple[str, str, str]] = (),
    ) -> SessionRecord:
        """Record the session (preparing, while *tree* prepares its worktree, or *trees* a
        workbench session's), write its package (*threads*: further series threads),
        start it; a failure leaves it marked failed."""
        prepare = trees or ((lambda r: r.replace(worktree=tree(r))) if tree is not None else None)
        if prepare is not None:
            self._preparing.add(record.id)
            record = record.replace(state='preparing')
        self.store.create(record)
        try:
            if prepare is not None:
                record = prepare(record).replace(state='')
                self.store.save(record)
                self._preparing.discard(record.id)
            source, items = package(record)
            prepare_session(
                self.store, record, spec.request, source, spec.sandboxed, items, threads, files
            )
            self.runner.launch(record, spec)
        except TreeError as exc:
            self._preparing.discard(record.id)
            self.store.set_state(record.id, 'failed')
            raise SessionError(
                f'Session {record.id}: {exc}', exc.output, exc.base, exc.project
            ) from exc
        except Exception as exc:
            self._preparing.discard(record.id)
            self.store.set_state(record.id, 'failed')
            raise SessionError(f'Session {record.id} failed to start: {exc}') from exc
        return record

    def launch_rebase(
        self,
        project: str,
        base: MaintainerBase,
        status: BaseStatus,
        failed: Optional[BaseCommit],
        detail: str,
        situation: Situation,
        model_id: str = 'default',
        now: Optional[datetime.datetime] = None,
    ) -> SessionRecord:
        """A rebase session for *base* after its straight rebase stopped at *failed*: it
        rebases a copy by hand; the base changes only when the maintainer accepts."""
        blockers = []
        if situation.worktree_reason:
            blockers.append(f'rebase sessions unavailable: {situation.worktree_reason}')
        limit_blockers, _warnings = launch_check(
            situation.limits,
            situation.live,
            situation.worktrees,
            situation.worktree_bytes,
            'worktree',
            situation.live_builds,
        )
        blockers += limit_blockers
        model = next((m for m in situation.models if m.id == model_id), None)
        if model is None or not model.available:
            blockers.append(f'model {model_id}: {model.reason if model else "unknown"}')
        if blockers:
            raise SessionError('; '.join(blockers))
        assert model is not None
        now = now or datetime.datetime.now(datetime.timezone.utc)
        record = rebase_record(project, base, status, failed, model, now)
        request = rebase_request(base, status, f'b4m/session/{record.id}', failed, detail)
        spec = LaunchSpec(request, (), model.args, model.env, bool(self._sandbox()))
        return self._start(
            record,
            spec,
            lambda r: self.worktrees.prepare_rebase(r, base),
            lambda r: (None, ()),
        )

    def launch_workbench(
        self,
        bench: str,
        label: str,
        plans: Sequence[TreePlan],
        profile: Profile,
        choice: LaunchChoice,
        situation: Situation,
        items: Sequence[ContextItem] = (),
        now: Optional[datetime.datetime] = None,
    ) -> SessionRecord:
        """A session over workbench *bench*: in worktree mode its trees, one per project
        (*plans*), side by side in one folder (a tree that cannot be prepared leaves none
        of them); lightweight, no trees, only each series' thread to read and compare. It
        counts once against the limits."""
        planned = plan(profile, choice, situation, items)
        if not planned.ok:
            raise SessionError('; '.join(planned.blockers))
        if not plans:
            raise SessionError(f'workbench {label} has nothing to prepare')
        assert planned.request is not None and planned.model is not None
        now = now or datetime.datetime.now(datetime.timezone.utc)
        target = Target('workbench', '', f'workbench:{bench}', 0, label)
        record = new_record(target, profile, choice, planned, now).replace(workbench=bench)
        installed = {s.id: s for s in situation.skills}
        chosen = [installed[s] for s in planned.skills if s in installed]
        spec = LaunchSpec(
            planned.request,
            tuple(s.path for s in chosen if s.path is not None),
            planned.model.args,
            planned.model.env,
            bool(self._sandbox()),
            tuple(s.root for s in chosen if s.root is not None),
        )
        guidance = provenance(chosen, planned.skills)
        if guidance:
            record = record.replace(provenance={**record.provenance, **guidance})

        trees = self._preparer(plans)
        threads = [
            (f'{entry.project}-{n}.mbx', item.source, f'{item.label} ({entry.project})')
            for entry in plans
            for n, item in enumerate((i for i in entry.items if i.source), 1)
        ]
        files = [
            (pathlib.Path(item.file).name, item.file, f'{item.label} ({entry.project})')
            for entry in plans
            for item in entry.items
            if item.file
        ]
        package = [i for i in planned.included if i.id != 'mbox']  # One thread per series.
        tree_plans = [p for p in plans if p.use != 'none']
        prepare = trees if planned.mode == 'worktree' and tree_plans else None
        return self._start(record, spec, None, lambda r: (None, package), prepare, threads, files)

    def _preparer(self, plans: Sequence[TreePlan]) -> Callable[[SessionRecord], SessionRecord]:
        """Prepares a tree per plan (a 'none' plan has none) side by side in the session's
        folder; one that cannot be prepared leaves none of them."""

        def trees(r: SessionRecord) -> SessionRecord:
            made: list[ProjectTree] = []
            for entry in (p for p in plans if p.use != 'none'):  # No tree: its mail only.
                try:
                    made.append(self.worktrees.prepare_entry(r, entry))
                except TreeError as exc:
                    self.worktrees.remove_home(r)
                    raise TreeError(
                        f'{entry.project}: {exc}', exc.output, exc.base, entry.project
                    ) from exc
            return r.replace(
                trees=tuple(made), home=str(pathlib.Path(made[0].worktree.path).parent)
            )

        return trees

    def launch_apply(
        self,
        target: Target,
        branch: str,
        expected: str,
        ambytes: bytes,
        numbers: Sequence[int],
        detail: str,
        situation: Situation,
        model_id: str = 'default',
        now: Optional[datetime.datetime] = None,
    ) -> SessionRecord:
        """An apply session for a series b4 could not apply to *branch* (a base at
        *expected*): the agent applies *ambytes* on a copy; the base changes only when the
        maintainer accepts."""
        blockers = []
        if situation.worktree_reason:
            blockers.append(f'apply sessions unavailable: {situation.worktree_reason}')
        limit_blockers, _warnings = launch_check(
            situation.limits,
            situation.live,
            situation.worktrees,
            situation.worktree_bytes,
            'worktree',
            situation.live_builds,
        )
        blockers += limit_blockers
        model = next((m for m in situation.models if m.id == model_id), None)
        if model is None or not model.available:
            blockers.append(f'model {model_id}: {model.reason if model else "unknown"}')
        if blockers:
            raise SessionError('; '.join(blockers))
        assert model is not None
        now = now or datetime.datetime.now(datetime.timezone.utc)
        record = apply_record(target, branch, expected, numbers, model, now)
        request = apply_request(target, branch, expected, numbers, detail)
        spec = LaunchSpec(request, (), model.args, model.env, bool(self._sandbox()))
        return self._start(
            record,
            spec,
            lambda r: self.worktrees.prepare_with_mbox(r, ambytes, expected),
            lambda r: (target.source, ()),
        )

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
        for _project, tree in session_trees(record):
            if not os.path.isdir(tree.path):
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
            path: self.worktrees.inspect(path, _tree_at(r, path) if r else None)
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

    def _removable(self, sids: Sequence[str]) -> list[tuple[SessionView, list[str]]]:
        """Each session's view and its listed trees; refuses one being prepared."""
        views = {v.record.id: v for v in self._poll()}
        found = []
        for sid in sids:
            view = views.get(sid)
            if view is None:
                raise SessionError(f'no session {sid}')
            if view.state == 'preparing':
                raise SessionError(f'session {sid} is being prepared')
            listed = {w.path for w in self.worktrees.list([view.record])}
            found.append(
                (view, [t.path for _p, t in session_trees(view.record) if t.path in listed])
            )
        return found

    def session_losses(self, sids: Sequence[str]) -> tuple[list[str], dict[str, TreeState]]:
        """(the live sessions among *sids*, what removing each of their trees would lose)."""
        found = self._removable(sids)
        live = [v.record.id for v, _paths in found if v.state in LIVE]
        losses = {
            path: self.worktrees.inspect(path, _tree_at(v.record, path))
            for v, paths in found
            for path in paths
        }
        return live, losses

    def remove_sessions(self, sids: Sequence[str]) -> list[str]:
        """End the live ones, remove their trees, delete their records and output (the
        maintainer confirmed what goes)."""
        removed = []
        for view, paths in self._removable(sids):
            if view.state in LIVE:
                self.runner.end(view.record)
            for path in paths:
                try:
                    self.worktrees.remove(path)
                except OSError as exc:
                    raise SessionError(f'Cannot remove {path}: {exc}') from exc
            try:
                self.store.delete(view.record.id)
            except OSError as exc:
                raise SessionError(f'Cannot delete session {view.record.id}: {exc}') from exc
            removed.append(view.record.id)
        return removed

    def artifacts(self, sid: str) -> list[str]:
        return self.store.artifacts(sid)


def _tree_at(record: SessionRecord, path: str) -> Optional[Worktree]:
    """The session's tree at *path* (one of a workbench session's, or its one)."""
    return next((tree for _project, tree in session_trees(record) if tree.path == path), None)

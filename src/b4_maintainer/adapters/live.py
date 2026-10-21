# SPDX-License-Identifier: GPL-2.0-or-later
"""The live workspace: workspace.toml, cached list mail, and each project's b4 state.

load() reads only local data. refresh() fetches list mail and track() asks b4
to track a series; both are explicit maintainer actions run off the UI thread.
"""

import dataclasses
import datetime
import pathlib
import tomllib
from typing import Callable, Optional, Sequence

from ..model import Series, SeriesState, WorkItem
from ..projectsetup import CONFIG_NAME, default_b4_command
from ..workspace import Project, Snapshot, Workspace, build_items
from . import b4state
from .b4mbox import parse_mbox
from .clientstate import HiddenStore
from .lorelist import MailCache, Search
from .maintainers import OwnerCache, author_roles


class LiveSource:
    def __init__(
        self,
        root: 'pathlib.Path | str',
        tracked_series: Callable[[pathlib.Path, str], list[b4state.Tracked]] = (
            b4state.tracked_series
        ),
        search_for: Callable[[pathlib.Path], Search] = b4state.lore_search,
        track: Callable[[pathlib.Path, str, Sequence[str]], None] = b4state.track,
        b4_command: Optional[Sequence[str]] = None,
        now: Callable[[], datetime.datetime] = lambda: datetime.datetime.now(datetime.timezone.utc),
        identity: Callable[[pathlib.Path], Optional[str]] = b4state.user_email,
    ) -> None:
        self.root = pathlib.Path(root).resolve()
        self.cache = MailCache(self.root / 'var' / 'mail')
        self.hidden = HiddenStore(self.root / 'state' / 'client' / 'hidden.json')
        self._tracked_series = tracked_series
        self._search_for = search_for
        self._track = track
        self._b4_command = b4_command
        self._now = now
        self._identity = identity

    def workspace(self) -> Workspace:
        return Workspace.from_config(tomllib.loads((self.root / CONFIG_NAME).read_text()))

    def checkout(self, project: Project) -> pathlib.Path:
        return self.root / project.path

    def owners(self, project: Project) -> OwnerCache:
        return OwnerCache(
            self.root / 'var' / 'owners' / f'{project.id}.json',
            self.checkout(project),
            command=project.get_maintainer,
            maintainers=project.maintainers,
        )

    def _with_identity(self, ws: Workspace) -> Workspace:
        """Without a configured `me`, use the address b4 resolves for each project."""
        if ws.me:
            return ws
        found = (self._identity(self.checkout(p)) for p in ws.projects if self._enrolled(p))
        me = tuple(dict.fromkeys(addr.lower() for addr in found if addr))
        return dataclasses.replace(ws, me=me)

    def _enrolled(self, project: Project) -> bool:
        return (self.checkout(project) / '.git').exists()

    def load(self) -> Snapshot:
        ws = self._with_identity(self.workspace())
        series = self.cache.series(parse_mbox)
        states: dict[str, SeriesState] = {}
        assigned: dict[str, str] = {}
        extra: list[Series] = []
        for project in ws.projects:
            if not self._enrolled(project):
                continue
            for entry in self._tracked_series(self.checkout(project), project.id):
                match = next((s for s in series if entry.msgids & set(s.msgids)), None)
                if match is None:
                    match = _from_tracking(entry)
                    extra.append(match)
                states[match.identity] = SeriesState(to_review=True)
                assigned[match.identity] = project.id
        items = self._ownership(ws, build_items(series + extra, states, ws, assigned))
        hidden = self.hidden.load()
        items = tuple(
            dataclasses.replace(i, hidden=True) if hidden.matches(i) else i for i in items
        )
        return Snapshot(ws, items)

    def _ownership(self, ws: Workspace, items: tuple[WorkItem, ...]) -> tuple[WorkItem, ...]:
        """Mark maintained series and author roles from each project's MAINTAINERS."""
        me = set(ws.me)
        per_project = {
            p.id: (self.owners(p), author_roles(self.checkout(p) / p.maintainers))
            for p in ws.projects
            if self._enrolled(p)
        }
        result = []
        for item in items:
            responsible, role = False, ''
            for pid in (item.project, *item.candidates):
                if pid not in per_project:
                    continue
                cache, roles = per_project[pid]
                owners = cache.get(item.series.paths) if item.series.paths else None
                responsible = responsible or bool(owners and owners & me)
                role = role or roles.get(item.series.author_email.lower(), '')
            if responsible or role:
                item = dataclasses.replace(item, responsible=responsible, author_role=role)
            result.append(item)
        return tuple(result)

    def browse_lists(self, ws: Workspace) -> list[tuple[str, Project]]:
        """Profile lists to fetch, each with the project whose b4 configuration fetches it.

        Project lists alone are not browsed: they can include very busy lists.
        """
        wanted: dict[str, Project] = {}
        for profile in ws.profiles:
            project = ws.project(profile.project)
            if profile.enabled and project is not None:
                for lst in profile.lists:
                    wanted.setdefault(lst, project)
        return list(wanted.items())

    def refresh(self, progress: Callable[[str], None] = lambda _: None) -> str:
        ws = self.workspace()
        lists = self.browse_lists(ws)
        if not lists:
            return f'Nothing to browse: add a [[profiles]] entry with lists to {CONFIG_NAME}'
        added = 0
        for n, (lst, project) in enumerate(lists, 1):
            progress(f'Fetching {lst} ({n}/{len(lists)})…')
            search = self._search_for(self.checkout(project))
            added += self.cache.refresh(lst, ws.window_days, search, now=self._now())
        progress('Parsing new threads…')
        self.cache.series(parse_mbox)  # So the reload that follows reads summaries only.
        checked = self._check_owners(ws, progress)
        return f'Refreshed {len(lists)} list(s): {added} new message(s){checked}'

    def _check_owners(self, ws: Workspace, progress: Callable[[str], None]) -> str:
        items = self.load().items
        notes = []
        for project in ws.projects:
            cache = self.owners(project)
            if not self._enrolled(project) or not cache.available:
                continue
            paths = [
                i.series.paths
                for i in items
                if i.series.paths and project.id in (i.project, *i.candidates)
            ]
            progress(f'Checking MAINTAINERS for {project.id} ({len(paths)} series)…')
            answered = cache.update(paths)
            notes.append(f'{answered} checked against MAINTAINERS')
            if cache.errors:
                notes.append(f'{cache.errors} get_maintainer failure(s)')
        return ''.join(f'; {n}' for n in notes)

    def toggle_hidden(self, item: WorkItem) -> str:
        if item.hidden:
            self.hidden.unhide(item)
            return f'Unhidden: {item.series.subject}'
        self.hidden.hide(item, now=self._now())
        return f'Hidden: {item.series.subject}'

    def track(self, item: WorkItem) -> str:
        ws = self.workspace()
        project = ws.project(item.project)
        if project is None:
            raise RuntimeError('Assign a project before tracking this series')
        series = item.series
        msgid = series.msgids[0] if series.msgids else ''
        if not msgid:
            raise RuntimeError('No message-id known for this series')
        command = list(self._b4_command) if self._b4_command else default_b4_command()
        self._track(self.checkout(project), msgid, command)
        return f'Tracking in {project.id}: {series.subject} v{series.revision}'


def _from_tracking(entry: b4state.Tracked) -> Series:
    """Metadata-only series for tracked work no longer in the list cache."""
    return Series(
        identity=f'change-id:{entry.change_id}',
        subject=entry.subject,
        revision=entry.revision,
        expected=entry.expected,
        complete=False,
        has_cover=False,
        author_name=entry.author_name,
        author_email=entry.author_email,
        sent=entry.sent,
        patches=(),
        change_id=entry.change_id,
        msgids=tuple(sorted(entry.msgids)),
    )

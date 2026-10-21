# SPDX-License-Identifier: GPL-2.0-or-later
"""Workspace configuration, project routing, and series identity merging."""

from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, Optional

from .layout import Breakpoints
from .model import Authority, Series, SeriesState, WorkItem


@dataclass(frozen=True)
class Ref:
    label: str
    name: str
    authority: Authority


@dataclass(frozen=True)
class Project:
    id: str
    label: str
    short: str
    lists: tuple[str, ...]
    refs: tuple[Ref, ...] = ()


@dataclass(frozen=True)
class Profile:
    id: str
    label: str
    project: str
    lists: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    enabled: bool = True

    def matches(self, series: Series) -> bool:
        if set(self.lists) & set(series.list_candidates()):
            return True
        return any(p.startswith(prefix) for p in series.paths for prefix in self.paths)


@dataclass(frozen=True)
class Session:
    """Display record for an agent session; fixtures only until a backend exists."""

    id: str
    series: str
    project: Optional[str]
    backend: str
    state: str
    approvals: int = 0


@dataclass(frozen=True)
class Workspace:
    me: tuple[str, ...] = ()
    projects: tuple[Project, ...] = ()
    profiles: tuple[Profile, ...] = ()
    sessions: tuple[Session, ...] = ()
    breakpoints: Breakpoints = Breakpoints()

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> 'Workspace':
        projects = tuple(
            Project(
                id=p['id'],
                label=p.get('label', p['id']),
                short=p.get('short', p['id'][:4]),
                lists=tuple(lst.lower() for lst in p.get('lists', [])),
                refs=tuple(
                    Ref(r['label'], r['name'], Authority[r['authority'].upper()])
                    for r in p.get('refs', [])
                ),
            )
            for p in config.get('projects', [])
        )
        profiles = tuple(
            Profile(
                id=p['id'],
                label=p.get('label', p['id']),
                project=p['project'],
                lists=tuple(lst.lower() for lst in p.get('lists', [])),
                paths=tuple(p.get('paths', [])),
                enabled=p.get('enabled', True),
            )
            for p in config.get('profiles', [])
        )
        sessions = tuple(
            Session(
                id=s['id'],
                series=s['series'],
                project=s.get('project'),
                backend=s['backend'],
                state=s['state'],
                approvals=s.get('approvals', 0),
            )
            for s in config.get('sessions', [])
        )
        return cls(
            me=tuple(m.lower() for m in config.get('me', [])),
            projects=projects,
            profiles=profiles,
            sessions=sessions,
            breakpoints=Breakpoints.from_config(config.get('layout', {})),
        )

    def project(self, project_id: Optional[str]) -> Optional[Project]:
        return next((p for p in self.projects if p.id == project_id), None)


@dataclass(frozen=True)
class Snapshot:
    workspace: Workspace
    items: tuple[WorkItem, ...]
    _by_identity: dict[str, WorkItem] = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        self._by_identity.update((i.identity, i) for i in self.items)

    def item_by_identity(self, identity: str) -> Optional[WorkItem]:
        return self._by_identity.get(identity)


def _merge_postings(a: Series, b: Series) -> Series:
    """Combine duplicate copies of one posting (e.g. delivered via two lists)."""
    return replace(
        a,
        lists=tuple(dict.fromkeys(a.lists + b.lists)),
        recipients=tuple(dict.fromkeys(a.recipients + b.recipients)),
        msgids=tuple(dict.fromkeys(a.msgids + b.msgids)),
    )


def _route(series: Series, ws: Workspace) -> tuple[Optional[str], tuple[str, ...]]:
    """Suggest a project from list membership; ambiguity needs confirmation."""
    lists = set(series.list_candidates())
    matched = tuple(p.id for p in ws.projects if lists & set(p.lists))
    if len(matched) == 1:
        return matched[0], ()
    return None, matched


def build_items(
    series: Iterable[Series], states: Mapping[str, SeriesState], ws: Workspace
) -> tuple[WorkItem, ...]:
    """One work item per series identity, showing its latest revision."""
    latest: dict[str, Series] = {}
    versions: dict[str, set[int]] = {}
    for s in series:
        versions.setdefault(s.identity, set()).add(s.revision)
        current = latest.get(s.identity)
        if current is None or s.revision > current.revision:
            latest[s.identity] = s
        elif s.revision == current.revision:
            latest[s.identity] = _merge_postings(current, s)

    items = []
    for identity, s in latest.items():
        project, candidates = _route(s, ws)
        routes = {project, *candidates}
        profiles = frozenset(
            p.id for p in ws.profiles if p.enabled and p.project in routes and p.matches(s)
        )
        proj = ws.project(project)
        items.append(
            WorkItem(
                series=s,
                state=states.get(identity, SeriesState()),
                project=project,
                project_short=proj.short if proj else '',
                candidates=candidates,
                profiles=profiles,
                versions=tuple(sorted(versions[identity])),
            )
        )
    # Newest first; undated postings last.
    items.sort(key=lambda i: -i.series.sent.timestamp() if i.series.sent else float('inf'))
    return tuple(items)

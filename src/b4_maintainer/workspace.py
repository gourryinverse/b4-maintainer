# SPDX-License-Identifier: GPL-2.0-or-later
"""Workspace configuration, project routing, and series identity merging."""

from dataclasses import dataclass, field, replace
from typing import Any, Collection, Iterable, Mapping, Optional

from .layout import Breakpoints
from .model import Authority, Series, SeriesState, WorkItem
from .sessions.policy import SessionLimits


def list_id(name: str) -> str:
    """Lore's list-ID form (linux-mm.kvack.org) of a list address or list ID."""
    return name.strip().lower().replace('@', '.', 1)


@dataclass(frozen=True)
class Ref:
    label: str
    name: str
    authority: Authority
    # Sidebar view label; series in this ref leave profile views for it.
    view: str = ''
    # A series fully in this ref has left review: it drops out of every queue.
    ends_review: bool = False


def make_ref(
    label: str, name: str, authority: Authority, view: str = '', ends_review: Optional[bool] = None
) -> Ref:
    """Mainline ends review unless configured otherwise; other refs only when configured."""
    ends = authority == Authority.MAINLINE if ends_review is None else ends_review
    return Ref(label, name, authority, view, ends)


def _ref(r: Mapping[str, Any]) -> Ref:
    try:
        authority = Authority[str(r['authority']).upper()]
    except KeyError:
        allowed = ', '.join(a.name.lower() for a in Authority)
        raise ValueError(
            f'ref {r["name"]}: unknown authority {r["authority"]!r} (one of {allowed})'
        ) from None
    return make_ref(
        r['label'],
        r['name'],
        authority,
        r.get('view', ''),
        r.get('ends_review'),
    )


def _entries(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(e, dict) and 'id' in e for e in value)


def merge_config(shared: Mapping[str, Any], local: Mapping[str, Any]) -> dict[str, Any]:
    """*local* (a machine's var/workspace.toml) over *shared* (the synced one): tables
    merge key by key, lists of entries with ids ([[projects]], [[profiles]], [[models]])
    entry by entry (new ids added), anything else is replaced."""
    merged = dict(shared)
    for key, value in local.items():
        mine = merged.get(key)
        if isinstance(mine, dict) and isinstance(value, dict):
            merged[key] = merge_config(mine, value)
        elif isinstance(mine, list) and _entries(mine) and _entries(value):
            entries = {e['id']: e for e in mine}
            for entry in value:
                entries[entry['id']] = merge_config(entries.get(entry['id'], {}), entry)
            merged[key] = list(entries.values())
        else:
            merged[key] = value
    return merged


# The keys each table of workspace.toml may have; anything else is reported, not ignored.
REF_KEYS = frozenset({'label', 'name', 'authority', 'view', 'ends_review'})
BASE_KEYS = frozenset({'branch', 'upstream'})
PROJECT_KEYS = frozenset(
    {
        'id',
        'label',
        'short',
        'lists',
        'refs',
        'path',
        'maintainers',
        'get_maintainer',
        'bases',
        'subject_tags',
    }
)
PROFILE_KEYS = frozenset({'id', 'label', 'project', 'lists', 'paths', 'enabled'})
MODEL_KEYS = frozenset({'id', 'model', 'thinking', 'args', 'env', 'enabled'})
TABLE_KEYS = {
    'layout': frozenset({'three_pane', 'single_pane', 'preview_width'}),
    'browse': frozenset({'window_days'}),
    'sashiko': frozenset({'senders', 'hide_clean'}),
    'drafts': frozenset({'editor', 'tmux_width'}),
    'sync': frozenset({'auto', 'timeout'}),
    'refs': frozenset({'fetch'}),
    'sessions': frozenset(
        {
            'max_live',
            'max_builds',
            'worktree_warn',
            'worktree_warn_gb',
            'pi',
            'pi_args',
            'pane_width',
            'agent_name',
            'agent_email',
            'sandbox_nice',
            'sandbox_ionice',
            'assisted_by',
        }
    ),
}
GUIDANCE_KEYS = frozenset({'id', 'path'})
TOP_KEYS = frozenset({'me', 'projects', 'profiles', 'models', 'guidance', *TABLE_KEYS})


def _unknown(entry: Any, known: frozenset[str], where: str) -> list[str]:
    if not isinstance(entry, Mapping):
        return []
    found = []
    for key in entry:
        if key in known:
            continue
        hint = ''
        if where.startswith('in [[profiles]]') and key in PROJECT_KEYS:
            hint = ' (it is a [[projects]] setting)'
        elif where.startswith('in [[projects]]') and key in PROFILE_KEYS:
            hint = ' (it is a [[profiles]] setting)'
        found.append(f'unknown key {key} {where}{hint}')
    return found


def config_warnings(config: Mapping[str, Any]) -> tuple[str, ...]:
    """Keys workspace.toml has that nothing reads: usually a typo or a line under the
    wrong table (a key belongs to the table header above it)."""
    warnings = [f'unknown key {k} (top level)' for k in config if k not in TOP_KEYS]
    for p in config.get('projects', []):
        name = f'[[projects]] {p.get("id", "?")}' if isinstance(p, Mapping) else '[[projects]]'
        warnings += _unknown(p, PROJECT_KEYS, f'in {name}')
        for r in p.get('refs', []) if isinstance(p, Mapping) else []:
            warnings += _unknown(r, REF_KEYS, f'in {name} refs')
        for b in p.get('bases', []) if isinstance(p, Mapping) else []:
            warnings += _unknown(b, BASE_KEYS, f'in {name} bases')
    for p in config.get('profiles', []):
        name = f'[[profiles]] {p.get("id", "?")}' if isinstance(p, Mapping) else '[[profiles]]'
        warnings += _unknown(p, PROFILE_KEYS, f'in {name}')
    for table, known in TABLE_KEYS.items():
        warnings += _unknown(config.get(table, {}), known, f'in [{table}]')
    for g in config.get('guidance', []):
        name = f'[[guidance]] {g.get("id", "?")}' if isinstance(g, Mapping) else '[[guidance]]'
        warnings += _unknown(g, GUIDANCE_KEYS, f'in {name}')
    for m in config.get('models', []):
        name = f'[[models]] {m.get("id", "?")}' if isinstance(m, Mapping) else '[[models]]'
        warnings += _unknown(m, MODEL_KEYS, f'in {name}')
    return tuple(warnings)


@dataclass(frozen=True)
class MaintainerBase:
    """The maintainer's integration branch on top of an upstream branch (e.g. a local
    branch on `mm/mm-stable`): pulled-ahead work waits there until it lands upstream."""

    branch: str
    upstream: str


@dataclass(frozen=True)
class Project:
    id: str
    label: str
    short: str
    lists: tuple[str, ...]
    refs: tuple[Ref, ...] = ()
    path: str = ''
    # Ownership: MAINTAINERS in the checkout, asked through get_maintainer
    # (default: scripts/get_maintainer.pl when present; empty disables).
    maintainers: str = 'MAINTAINERS'
    get_maintainer: Optional[tuple[str, ...]] = None
    bases: tuple[MaintainerBase, ...] = ()
    # Subject prefix tags (e.g. "ndctl" for "[ndctl PATCH]"): a series carrying one is this
    # project's whatever its lists; a project with tags takes only tagged series.
    subject_tags: tuple[str, ...] = ()


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


SASHIKO_SENDERS = ('sashiko-bot@kernel.org', 'netdev-bot+sashiko@kernel.org')


@dataclass(frozen=True)
class Workspace:
    me: tuple[str, ...] = ()
    projects: tuple[Project, ...] = ()
    profiles: tuple[Profile, ...] = ()
    breakpoints: Breakpoints = Breakpoints()
    window_days: int = 14
    # Senders of automated review mail; their replies are findings, not Responses.
    bots: tuple[str, ...] = SASHIKO_SENDERS
    # Leave Sashiko replies that found nothing out of threads ([sashiko] hide_clean).
    sashiko_hide_clean: bool = True
    # Where replies are drafted: auto (tmux when inside it), tmux, or app.
    drafts_editor: str = 'auto'
    tmux_width: int = 100
    # State sync with state/'s remote: at startup, around refresh, and on quit.
    sync_auto: bool = True
    sync_timeout: float = 10.0
    # Refresh fetches the remotes behind configured refs before scanning them.
    fetch_refs: bool = True
    # Agent sessions: limits, and hand-configured model presets (see sessions/models.py).
    session_limits: SessionLimits = SessionLimits()
    models: tuple[Mapping[str, Any], ...] = ()
    # pi and site launcher options ([sessions] pi, pi_args): site-specific, so kept in the
    # workspace's own configuration.
    pi: str = 'pi'
    pi_args: tuple[str, ...] = ()
    pane_width: int = 90  # Columns of the session pane beside the app ([sessions]).
    # Commit identity in session worktrees ([sessions] agent_name may use {model}).
    agent_name: str = 'b4-maintainer agent ({model})'
    agent_email: str = 'agent@b4-maintainer.invalid'
    # Sandboxed commands' priority ([sessions] sandbox_nice, 0 for normal; sandbox_ionice:
    # low, idle, or off): session builds and VMs yield to the app and the desktop.
    sandbox_nice: int = 10
    sandbox_ionice: str = 'low'
    # The Assisted-by trailer a plucked agent commit gets ([sessions] assisted_by; {model}
    # is the session's model).
    assisted_by: str = '{model}'
    # Guidance sources pointed at other checkouts ([[guidance]] id, path).
    guidance: tuple[tuple[str, str], ...] = ()
    warnings: tuple[str, ...] = ()  # Keys in the configuration that nothing reads.

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> 'Workspace':
        projects = tuple(
            Project(
                id=p['id'],
                label=p.get('label', p['id']),
                short=p.get('short', p['id'][:4]),
                lists=tuple(list_id(lst) for lst in p.get('lists', [])),
                refs=tuple(_ref(r) for r in p.get('refs', [])),
                path=p.get('path', f'projects/{p["id"]}'),
                maintainers=p.get('maintainers', 'MAINTAINERS'),
                get_maintainer=(
                    tuple(p['get_maintainer']) if p.get('get_maintainer') is not None else None
                ),
                bases=tuple(
                    MaintainerBase(str(b['branch']), str(b['upstream'])) for b in p.get('bases', [])
                ),
                subject_tags=tuple(str(tag).lower() for tag in p.get('subject_tags', [])),
            )
            for p in config.get('projects', [])
        )
        profiles = tuple(
            Profile(
                id=p['id'],
                label=p.get('label', p['id']),
                project=p['project'],
                lists=tuple(list_id(lst) for lst in p.get('lists', [])),
                paths=tuple(p.get('paths', [])),
                enabled=p.get('enabled', True),
            )
            for p in config.get('profiles', [])
        )
        return cls(
            me=tuple(m.lower() for m in config.get('me', [])),
            projects=projects,
            profiles=profiles,
            breakpoints=Breakpoints.from_config(config.get('layout', {})),
            window_days=int(config.get('browse', {}).get('window_days', 14)),
            bots=tuple(
                b.lower() for b in config.get('sashiko', {}).get('senders', SASHIKO_SENDERS)
            ),
            sashiko_hide_clean=bool(config.get('sashiko', {}).get('hide_clean', True)),
            drafts_editor=str(config.get('drafts', {}).get('editor', 'auto')),
            tmux_width=int(config.get('drafts', {}).get('tmux_width', 100)),
            sync_auto=bool(config.get('sync', {}).get('auto', True)),
            sync_timeout=float(config.get('sync', {}).get('timeout', 10.0)),
            fetch_refs=bool(config.get('refs', {}).get('fetch', True)),
            session_limits=SessionLimits.from_config(config.get('sessions', {})),
            models=tuple(m for m in config.get('models', []) if isinstance(m, dict)),
            pi=str(config.get('sessions', {}).get('pi', 'pi')),
            pi_args=tuple(str(a) for a in config.get('sessions', {}).get('pi_args', [])),
            pane_width=int(config.get('sessions', {}).get('pane_width', 90)),
            agent_name=str(config.get('sessions', {}).get('agent_name', cls.agent_name)),
            agent_email=str(config.get('sessions', {}).get('agent_email', cls.agent_email)),
            sandbox_nice=int(config.get('sessions', {}).get('sandbox_nice', cls.sandbox_nice)),
            sandbox_ionice=str(
                config.get('sessions', {}).get('sandbox_ionice', cls.sandbox_ionice)
            ),
            assisted_by=str(config.get('sessions', {}).get('assisted_by', cls.assisted_by)),
            guidance=tuple(
                (str(g['id']), str(g['path']))
                for g in config.get('guidance', [])
                if isinstance(g, Mapping) and 'id' in g and 'path' in g
            ),
            warnings=config_warnings(config),
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


def family_key(series: Series) -> str:
    """b4's fallback for finding a series' other revisions: sender plus subject."""
    return f'{series.author_email.lower()}\0{" ".join(series.subject.lower().split())}'


def _families(series: Iterable[Series]) -> dict[str, str]:
    """Map each posting's identity to its series identity.

    Change-ids are authoritative and never merge with each other. Postings
    without one join the family's only change-id, or, absent any, each other.
    Several change-ids for one family are ambiguous; those postings stay apart.
    """
    groups: dict[str, list[Series]] = {}
    canonical: dict[str, str] = {}
    for s in series:
        if s.discussion:
            canonical[s.identity] = s.identity  # Conversations have no revisions.
        else:
            groups.setdefault(family_key(s), []).append(s)
    for group in groups.values():
        with_id = {s.identity for s in group if s.change_id}
        loose = [s for s in group if not s.change_id]
        if len(with_id) == 1:
            target = next(iter(with_id))
        elif not with_id and loose:
            target = max(loose, key=lambda s: s.revision).identity
        else:
            target = None
        for s in group:
            canonical[s.identity] = s.identity if s.change_id or target is None else target
    return canonical


def _route(series: Series, ws: Workspace) -> tuple[Optional[str], tuple[str, ...]]:
    """Suggest a project: by a subject tag, else by list membership among the projects
    without tags; ambiguity needs confirmation."""
    words = set(series.prefixes)
    tagged = tuple(p.id for p in ws.projects if words & set(p.subject_tags))
    if len(tagged) == 1:
        return tagged[0], ()
    lists = set(series.list_candidates())
    matched = tuple(p.id for p in ws.projects if lists & set(p.lists) and not p.subject_tags)
    if len(matched) == 1:
        return matched[0], ()
    return None, matched


def build_items(
    series: Iterable[Series],
    states: Mapping[str, SeriesState],
    ws: Workspace,
    assigned: Optional[Mapping[str, str]] = None,
    responsible: Collection[str] = (),
) -> tuple[WorkItem, ...]:
    """One work item per series identity, showing its latest revision.

    *assigned* maps identities to projects already chosen, e.g. by native tracking.
    *responsible* holds identities of series the maintainer maintains.
    """
    series = list(series)
    canonical = _families(series)
    members: dict[str, list[str]] = {}
    latest: dict[str, Series] = {}
    versions: dict[str, set[int]] = {}
    sources: dict[str, dict[str, None]] = {}
    for s in series:
        identity = canonical[s.identity]
        if s.source:
            sources.setdefault(identity, {})[s.source] = None
        if s.identity not in members.setdefault(identity, []):
            members[identity].append(s.identity)
        versions.setdefault(identity, set()).add(s.revision)
        current = latest.get(identity)
        if current is None or s.revision > current.revision:
            latest[identity] = replace(s, identity=identity)
        elif s.revision == current.revision:
            latest[identity] = _merge_postings(current, s)

    def first(mapping: Mapping[str, Any], identity: str) -> Any:
        return next((mapping[m] for m in [identity, *members[identity]] if m in mapping), None)

    items = []
    for identity, s in latest.items():
        project, candidates = _route(s, ws)
        chosen = first(assigned or {}, identity)
        if chosen:
            project, candidates = chosen, ()
        routes = {project, *candidates}
        profiles = frozenset(
            p.id for p in ws.profiles if p.enabled and p.project in routes and p.matches(s)
        )
        proj = ws.project(project)
        items.append(
            WorkItem(
                series=s,
                state=first(states, identity) or SeriesState(),
                project=project,
                project_short=proj.short if proj else '',
                candidates=candidates,
                profiles=profiles,
                versions=tuple(sorted(versions[identity])),
                responsible=any(m in responsible for m in [identity, *members[identity]]),
                sources=tuple(sources.get(identity, ())),
            )
        )
    # Newest first; undated postings last.
    items.sort(key=lambda i: -i.series.sent.timestamp() if i.series.sent else float('inf'))
    return tuple(items)

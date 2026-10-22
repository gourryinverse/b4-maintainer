# SPDX-License-Identifier: GPL-2.0-or-later
"""The live workspace: workspace.toml, cached list mail, and each project's b4 state.

load() reads only local data. refresh() fetches list mail and Sashiko status, and
track() asks b4 to track a series; both are explicit maintainer actions run off
the UI thread.
"""

import dataclasses
import datetime
import pathlib
import tomllib
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from ..model import Coverage, Series, SeriesState, WorkItem
from ..projectsetup import CONFIG_NAME, default_b4_command
from ..workspace import Project, Snapshot, Workspace, build_items
from . import b4state, sashiko
from .b4mbox import parse_mbox
from .clientstate import HiddenStore
from .drafts import DraftStore
from .lorelist import MailCache, Search
from .maintainers import OwnerCache, author_roles
from .sashiko import SashikoCache
from .treematch import RefCache


@dataclass(frozen=True)
class ReplyTarget:
    lmsg: Any  # b4.LoreMessage
    checkout: pathlib.Path
    email_dryrun: bool
    patatt_sign: bool


class LiveSource:
    # b4 review's --email-dry-run and --no-sign, for replies.
    email_dryrun = False
    no_sign = False

    def __init__(
        self,
        root: 'pathlib.Path | str',
        tracked_series: Callable[[pathlib.Path, str], list[b4state.Tracked]] = (
            b4state.tracked_series
        ),
        search_for: Callable[[pathlib.Path], Search] = b4state.lore_search,
        track: Callable[[pathlib.Path, str, Sequence[str]], None] = b4state.track,
        archive: Callable[[pathlib.Path, str, str, int], tuple[bool, str]] = b4state.archive,
        b4_command: Optional[Sequence[str]] = None,
        now: Callable[[], datetime.datetime] = lambda: datetime.datetime.now(datetime.timezone.utc),
        identity: Callable[[pathlib.Path], Optional[str]] = b4state.user_email,
        seen: Callable[[pathlib.Path, Sequence[str]], set[str]] = b4state.seen,
        mark_seen: Callable[[pathlib.Path, Mapping[str, Optional[str]]], None] = (
            b4state.mark_seen
        ),
        sashiko_url: Callable[[pathlib.Path], Optional[str]] = b4state.sashiko_url,
        sashiko_get: sashiko.Get = sashiko.http_get,
        sashiko_patchset: sashiko.GetPatchset = sashiko.b4_patchset,
    ) -> None:
        self.root = pathlib.Path(root).resolve()
        self.cache = MailCache(self.root / 'var' / 'mail')
        self.hidden = HiddenStore(self.root / 'state' / 'client' / 'hidden.json')
        self.sashiko = SashikoCache(self.root / 'var' / 'sashiko')
        self.drafts = DraftStore(self.root / 'var' / 'drafts')
        self._tracked_series = tracked_series
        self._search_for = search_for
        self._track = track
        self._archive = archive
        self._b4_command = b4_command
        self._now = now
        self._identity = identity
        self._seen = seen
        self._mark_seen = mark_seen
        self._sashiko_url = sashiko_url
        self._sashiko_get = sashiko_get
        self._sashiko_patchset = sashiko_patchset

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
                if entry.status == 'archived':
                    continue  # Untracked: out of To Review, kept in b4's archive.
                match = next((s for s in series if entry.msgids & set(s.msgids)), None)
                if match is None:
                    match = _from_tracking(entry)
                    extra.append(match)
                states[match.identity] = SeriesState(to_review=True)
                assigned[match.identity] = project.id
        items = self._ownership(ws, build_items(series + extra, states, ws, assigned))
        items = self._responses(ws, items)
        items = self._tree_evidence(ws, items)
        items = self._sashiko_reviews(items)
        items = self._drafted(items)
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

    def _drafted(self, items: tuple[WorkItem, ...]) -> tuple[WorkItem, ...]:
        """Mark items with a saved reply draft to any message in their threads."""
        drafted = self.drafts.msgids()
        if not drafted:
            return items
        info = self.cache.messages()
        threads = {self.cache.thread_name(info[m].key) for m in drafted if m in info}
        result = []
        for item in items:
            names = {pathlib.Path(src).name for src in item.sources}
            if names & threads:
                item = dataclasses.replace(item, state=dataclasses.replace(item.state, draft=True))
            result.append(item)
        return tuple(result)

    def _sashiko_reviews(self, items: tuple[WorkItem, ...]) -> tuple[WorkItem, ...]:
        """Attach the cached Sashiko review of each item's posting (its newest version)."""
        reviews = self.sashiko.reviews()
        if not reviews:
            return items
        result = []
        for item in items:
            review = next((reviews[m] for m in item.series.msgids if m in reviews), None)
            result.append(dataclasses.replace(item, sashiko=review) if review else item)
        return tuple(result)

    def _fetch_sashiko(self, ws: Workspace, progress: Callable[[str], None]) -> str:
        """Status for each profile list whose project sets b4.sashiko-url, then details."""
        notes, listed = [], 0
        cached = self.load().items
        for lst, project in self.browse_lists(ws):
            url = self._sashiko_url(self.checkout(project)) if self._enrolled(project) else None
            if not url:
                continue
            # Threads still active can be older than the window: reach back to their postings.
            posted = [
                i.series.sent for i in cached if i.series.sent and lst in i.series.list_candidates()
            ]
            progress(f'Fetching Sashiko status for {lst}…')
            try:
                listed += self.sashiko.update_list(
                    lst,
                    url,
                    ws.window_days,
                    self._sashiko_get,
                    now=self._now(),
                    since=min(posted, default=None),
                )
            except Exception as ex:  # network, HTTP, or JSON: keep the last good rows
                notes.append(f'Sashiko failed for {lst}: {ex}')
        if not listed and not notes:
            return ''
        items = self.load().items
        wanted = {m for i in items if i.state.to_review or i.responsible for m in i.series.msgids}
        progress('Fetching Sashiko reviews…')
        sashiko.clear_b4_cache()
        present = {m for i in items for m in i.series.msgids}
        fetched, failed = self.sashiko.update_details(
            wanted, self._sashiko_patchset, present=present
        )
        reviewed = sum(1 for i in items if i.sashiko is not None)
        notes.insert(0, f'Sashiko: {reviewed} series, {fetched} review(s) fetched')
        if failed:
            notes.append(f'{failed} Sashiko review fetch failure(s)')
        return ''.join(f'; {n}' for n in notes)

    def refs(self, project: Project) -> RefCache:
        return RefCache(self.root / 'var' / 'refs' / f'{project.id}.json', self.checkout(project))

    def _tree_evidence(self, ws: Workspace, items: tuple[WorkItem, ...]) -> tuple[WorkItem, ...]:
        """Per-ref coverage from the last ref scan; stage is the first viewed ref holding it."""
        caches = {p.id: (p, self.refs(p)) for p in ws.projects if p.refs and self._enrolled(p)}
        result = []
        for item in items:
            entry = caches.get(item.project or '')
            if entry is not None:
                project, cache = entry
                tree = tuple(
                    (r.label, r.authority, cache.coverage(item.series, r)) for r in project.refs
                )
                held = (Coverage.FULL, Coverage.PARTIAL)
                stage = next(
                    (r.name for r, (_, _, c) in zip(project.refs, tree) if r.view and c in held), ''
                )
                landed = any(
                    r.ends_review and c == Coverage.FULL for r, (_, _, c) in zip(project.refs, tree)
                )
                state = dataclasses.replace(item.state, tree=tree)
                item = dataclasses.replace(item, state=state, stage=stage, landed=landed)
            result.append(item)
        return tuple(result)

    def _scan_refs(self, ws: Workspace, progress: Callable[[str], None]) -> str:
        # Mainline: commits since a month before the window, enough to see merges.
        since = self._now() - datetime.timedelta(days=ws.window_days + 30)
        scanned = 0
        for project in ws.projects:
            if project.refs and self._enrolled(project):
                progress(f'Checking refs for {project.id}…')
                scanned += self.refs(project).update(project.refs, since)
        return f'; {scanned} refs scanned' if scanned else ''

    def _flags_checkout(self, ws: Workspace) -> Optional[pathlib.Path]:
        """Checkout whose b4 configuration locates the shared message flags."""
        return next((self.checkout(p) for p in ws.projects if self._enrolled(p)), None)

    def _responses(self, ws: Workspace, items: tuple[WorkItem, ...]) -> tuple[WorkItem, ...]:
        """Mark unread direct replies to the maintainer's own mail, per item."""
        checkout = self._flags_checkout(ws)
        me = set(ws.me)
        if checkout is None or not me:
            return items
        info = self.cache.messages()
        mine = {m for m, e in info.items() if e.sender in me}
        others = me | set(ws.bots)
        replies = {m: e for m, e in info.items() if e.parent in mine and e.sender not in others}
        if not replies:
            return items
        unread = set(replies) - self._seen(checkout, list(replies))
        by_thread: dict[str, set[str]] = {}
        for msgid in unread:
            by_thread.setdefault(self.cache.thread_name(replies[msgid].key), set()).add(msgid)
        result = []
        for item in items:
            found = frozenset().union(
                *(by_thread.get(pathlib.Path(src).name, set()) for src in item.sources)
            )
            if found:
                state = dataclasses.replace(item.state, unread=len(found))
                item = dataclasses.replace(item, unread_msgids=found, state=state)
            result.append(item)
        return tuple(result)

    def mark_seen(self, msgids: Iterable[str]) -> None:
        ws = self.workspace()
        checkout = self._flags_checkout(ws)
        if checkout is None:
            return
        info = self.cache.messages()
        self._mark_seen(checkout, {m: info[m].date if m in info else None for m in msgids})

    def prepare_reply(self, item: WorkItem, msgid: str) -> ReplyTarget:
        """Load the message to answer and its project's b4 configuration (blocking)."""
        project = self.workspace().project(item.project)
        if project is None:
            raise RuntimeError('Assign a project before replying')
        checkout = self.checkout(project)
        lmsg = b4state.load_lore_message(item.sources, msgid)
        b4state.activate(checkout)
        sign = not self.no_sign and b4state.patatt_sign_default()
        return ReplyTarget(lmsg, checkout, self.email_dryrun, sign)

    def answered(self, item: WorkItem, msgid: str) -> str:
        project = self.workspace().project(item.project)
        if project is not None:
            info = self.cache.messages().get(msgid)
            b4state.mark_answered(self.checkout(project), msgid, info.date if info else None)
        return f'Replied: {item.series.subject}'

    def mark_read(self, item: WorkItem) -> str:
        """Mark every cached message in the item's threads (all revisions) as seen."""
        names = {pathlib.Path(src).name for src in item.sources}
        msgids = [
            m for m, e in self.cache.messages().items() if self.cache.thread_name(e.key) in names
        ]
        self.mark_seen(msgids)
        return f'Marked {len(msgids)} message(s) read: {item.series.subject}'

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
        added += self._fetch_my_mail(self._with_identity(ws), progress)
        progress('Parsing new threads…')
        self.cache.series(parse_mbox)  # So the reload that follows reads summaries only.
        checked = self._check_owners(ws, progress)
        scanned = self._scan_refs(ws, progress)
        reviews = self._fetch_sashiko(ws, progress)
        return f'Refreshed {len(lists)} list(s): {added} new message(s){checked}{scanned}{reviews}'

    def _fetch_my_mail(self, ws: Workspace, progress: Callable[[str], None]) -> int:
        """The maintainer's own mail on any list, whole threads, for Responses."""
        checkout = self._flags_checkout(ws)
        if checkout is None:
            return 0
        added = 0
        for addr in ws.me:
            progress(f'Fetching mail from {addr}…')
            added += self.cache.refresh_query(
                f'from-{addr}',
                lambda since, a=addr: f'f:{a} AND rt:{since:%Y-%m-%d}..',
                ws.window_days,
                self._search_for(checkout),
                now=self._now(),
            )
        return added

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

    def untrack(self, item: WorkItem) -> str:
        """Archive the series in b4 review: it leaves To Review; b4 keeps the archive."""
        project = self.workspace().project(item.project)
        if project is None:
            raise RuntimeError('Assign a project before untracking this series')
        checkout = self.checkout(project)
        entry = _tracked_entry(self._tracked_series(checkout, project.id), item)
        if entry is None:
            raise RuntimeError(f'{item.series.subject} is not tracked in {project.id}')
        ok, detail = self._archive(checkout, project.id, entry.change_id, entry.revision)
        if not ok:
            raise RuntimeError(f'b4 could not archive the series: {detail}')
        return f'Archived in {project.id}: {item.series.subject}'


def _tracked_entry(entries: Sequence[b4state.Tracked], item: WorkItem) -> Optional[b4state.Tracked]:
    """The active tracking record for an item, matched by any posted message-id."""
    msgids = set(item.series.msgids)
    return next(
        (e for e in entries if e.status != 'archived' and e.msgids & msgids),
        None,
    )


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

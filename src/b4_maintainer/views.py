# SPDX-License-Identifier: GPL-2.0-or-later
"""Sidebar views: membership is precomputed so switching never rescans items."""

from dataclasses import dataclass
from typing import Callable, Iterable

from .model import WorkItem
from .workspace import Workspace


@dataclass(frozen=True)
class View:
    id: str
    label: str
    kind: str


class ViewIndex:
    def __init__(self, ws: Workspace, items: Iterable[WorkItem]) -> None:
        self.workspace = ws
        self.needs_setup = not ws.profiles
        items = tuple(items)
        active = tuple(i for i in items if not i.state.mainline_complete)
        me = set(ws.me)

        specs: list[tuple[View, Callable[[WorkItem], bool], tuple[WorkItem, ...]]] = []
        multi = len(ws.projects) > 1
        specs.append((View('all', 'All projects' if multi else 'All', 'all'), _always, active))
        for p in ws.projects:
            specs.append(
                (
                    View(f'project:{p.id}', p.label, 'project'),
                    lambda i, pid=p.id: i.project == pid or pid in i.candidates,
                    active,
                )
            )
        for prof in ws.profiles:
            if prof.enabled:
                specs.append(
                    (
                        View(f'profile:{prof.id}', prof.label, 'profile'),
                        lambda i, pid=prof.id: pid in i.profiles,
                        active,
                    )
                )
        specs += [
            (View('responsibilities', 'My Responsibilities', 'builtin'), _has_profile, active),
            (
                View('unmerged', 'My Unmerged Submissions', 'builtin'),
                lambda i: i.series.author_email.lower() in me,
                active,
            ),
            (View('review', 'To Review', 'builtin'), lambda i: i.state.to_review, active),
            (View('cached', 'Cached', 'builtin'), _always, items),
        ]

        self.views: tuple[View, ...] = tuple(v for v, _, _ in specs)
        self._members: dict[str, tuple[WorkItem, ...]] = {
            v.id: tuple(i for i in pool if pred(i)) for v, pred, pool in specs
        }

    def items(self, view_id: str) -> tuple[WorkItem, ...]:
        return self._members[view_id]

    def count(self, view_id: str) -> int:
        return len(self._members[view_id])


def _always(item: WorkItem) -> bool:
    return True


def _has_profile(item: WorkItem) -> bool:
    return bool(item.profiles)

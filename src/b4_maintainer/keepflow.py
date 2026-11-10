# SPDX-License-Identifier: GPL-2.0-or-later
"""Keeping session output (var/kept/), as Branches and the Workbench ask it: which
artifacts (all ticked), then where (a commit, a shelf, a new shelf by name)."""

from typing import TYPE_CHECKING, Callable, Optional, Sequence

from textual.screen import Screen

from .branchscreen import Ask, Pick

if TYPE_CHECKING:
    from .sessions.artifacts import Artifact

NEW_SHELF = 'shelf:'


def choose_target(
    screen: Screen, targets: Sequence[tuple[str, str]], done: Callable[[str], None], title: str
) -> None:
    """Pick where to keep (a new shelf is named next), then *done* with it."""

    def named(name: Optional[str]) -> None:
        if name:
            done(f'shelf:{name.strip()}')

    def picked(target: Optional[str]) -> None:
        if target == NEW_SHELF:
            screen.app.push_screen(Ask('Name the shelf'), named)
        elif target:
            done(target)

    screen.app.push_screen(Pick(title, targets), picked)


def offer_after_pluck(
    screen: Screen,
    artifacts: Sequence['Artifact'],
    commits: Sequence[tuple[str, str]],
    keep: Callable[[list['Artifact'], str], None],
) -> None:
    """After a pluck: keep the session's artifacts with the plucked commit (asking which
    when there were several)? Escape keeps none."""
    from .workbenchscreen import PickMany

    if not artifacts or not commits:
        return
    choices = [(str(n), f'{a.kind}: {a.title}') for n, a in enumerate(artifacts)]
    what = commits[0][1] if len(commits) == 1 else f'{len(commits)} plucked commits'
    title = (
        f"Keep the session's output with {what}? It stays when the workbench goes "
        '(Workbench tab: Kept artifacts). Space unticks; Escape keeps none.'
    )

    def ticked(keys: Optional[list[str]]) -> None:
        chosen = [artifacts[int(k)] for k in keys or ()]
        if not chosen:
            return
        if len(commits) == 1:
            keep(chosen, commits[0][0])
            return
        choose_target(screen, commits, lambda target: keep(chosen, target), 'Keep them with…')

    screen.app.push_screen(PickMany(title, choices, ticked=[k for k, _ in choices]), ticked)

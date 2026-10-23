# SPDX-License-Identifier: GPL-2.0-or-later
"""What context a backend may receive, and when a launch must wait."""

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .backends import BackendEntry


@dataclass(frozen=True)
class ContextItem:
    id: str
    label: str
    private: bool  # Unsent drafts, synced client state, internal trees.


def disclosure(
    items: Iterable[ContextItem], entry: BackendEntry, ticked: set[str]
) -> tuple[list[ContextItem], list[str]]:
    """Public items always; private ones only when ticked and the entry is private_ok.

    Returns the included items and why any ticked item was left out.
    """
    included, refused = [], []
    for item in items:
        if not item.private:
            included.append(item)
        elif item.id in ticked:
            if entry.private_ok:
                included.append(item)
            else:
                refused.append(f'{item.label}: not sent to {entry.id} (not private_ok)')
    return included, refused


@dataclass(frozen=True)
class SessionLimits:
    max_live: int = 4
    max_builds: int = 2
    worktree_warn: int = 5
    worktree_warn_gb: float = 20.0

    @classmethod
    def from_config(cls, raw: Mapping[str, Any]) -> 'SessionLimits':
        default = cls()
        return cls(
            int(raw.get('max_live', default.max_live)),
            int(raw.get('max_builds', default.max_builds)),
            int(raw.get('worktree_warn', default.worktree_warn)),
            float(raw.get('worktree_warn_gb', default.worktree_warn_gb)),
        )


def launch_check(
    limits: SessionLimits, live: int, worktrees: int, worktree_bytes: int, mode: str
) -> tuple[list[str], list[str]]:
    """(blockers, warnings) for one more launch: blockers stop it, warnings are shown."""
    blockers, warnings = [], []
    if live >= limits.max_live:
        blockers.append(f'{live} sessions are live (max_live = {limits.max_live}); stop one first')
    if mode == 'worktree':
        if worktrees >= limits.worktree_warn:
            warnings.append(f'{worktrees} worktrees exist (worktree_warn = {limits.worktree_warn})')
        gb = worktree_bytes / 2**30
        if gb >= limits.worktree_warn_gb:
            warnings.append(
                f'worktrees use {gb:.1f} GB (worktree_warn_gb = {limits.worktree_warn_gb:g})'
            )
    return blockers, warnings

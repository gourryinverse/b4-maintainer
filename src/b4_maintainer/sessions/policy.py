# SPDX-License-Identifier: GPL-2.0-or-later
"""What a session receives, and when a launch must wait.

Sessions receive public data only (list mail, public trees, Sashiko): never unsent drafts,
client state, or internal trees.
"""

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class ContextItem:
    """Public material a session may receive (the dialog's Prompt package), e.g. the
    series mbox and thread, or Sashiko's review."""

    id: str
    label: str
    text: str = ''  # Written as context/<id>.md; the mailbox is copied from the source.
    available: bool = True
    reason: str = ''  # Why it is not available.


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
    limits: SessionLimits,
    live: int,
    worktrees: int,
    worktree_bytes: int,
    mode: str,
    live_builds: int = 0,
) -> tuple[list[str], list[str]]:
    """(blockers, warnings) for one more launch: blockers stop it, warnings are shown.
    *live_builds*: live worktree sessions, which build and boot kernels."""
    blockers, warnings = [], []
    if live >= limits.max_live:
        blockers.append(
            f'{live} sessions are live (max_live = {limits.max_live}); stop one first, or '
            'raise [sessions] max_live (var/workspace.toml: this machine)'
        )
    if mode == 'worktree' and live_builds >= limits.max_builds:
        many = 'sessions are' if live_builds != 1 else 'session is'
        blockers.append(
            f'{live_builds} worktree {many} live (max_builds = {limits.max_builds}); stop one '
            'first, or raise [sessions] max_builds (var/workspace.toml: this machine)'
        )
    if mode == 'worktree':
        if worktrees >= limits.worktree_warn:
            warnings.append(f'{worktrees} worktrees exist (worktree_warn = {limits.worktree_warn})')
        gb = worktree_bytes / 2**30
        if gb >= limits.worktree_warn_gb:
            warnings.append(
                f'worktrees use {gb:.1f} GB (worktree_warn_gb = {limits.worktree_warn_gb:g})'
            )
    return blockers, warnings

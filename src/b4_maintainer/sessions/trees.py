# SPDX-License-Identifier: GPL-2.0-or-later
"""What removing a session tree would lose, and removing it.

A session tree is a clone of its own (see adapters/sessiontree.py): removal deletes the
directory, its branch with it; the project is not involved. Plain git, no b4.
"""

import os
import pathlib
import shutil
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class TreeState:
    changed: tuple[str, ...] = ()  # Uncommitted or untracked files (git status lines).
    commits: tuple[str, ...] = ()  # Commits only in this tree, beyond the series.
    error: str = ''  # Not a readable git tree.

    @property
    def clean(self) -> bool:
        return not (self.changed or self.commits or self.error)

    def describe(self) -> str:
        if self.error:
            return self.error
        parts = []
        if self.changed:
            parts.append(f'{len(self.changed)} uncommitted file{"s"[: len(self.changed) - 1]}')
        if self.commits:
            parts.append(f'{len(self.commits)} commit{"s"[: len(self.commits) - 1]} only here')
        return ', '.join(parts) or 'clean'


def _git(path: pathlib.Path, *args: str) -> list[str]:
    run = subprocess.run(
        ['git', *args],
        cwd=path,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'},  # Never touch the index while looking.
    )
    if run.returncode != 0:
        raise OSError(run.stderr.strip() or f'git {args[0]} failed')
    return [line for line in run.stdout.splitlines() if line]


def inspect_tree(path: pathlib.Path, applied: str = '') -> TreeState:
    """Changes in the tree; commits on its branches beyond the refs it was cloned with and
    *applied* (the series' last; without it the series counts too)."""
    if not (path / '.git').is_dir():
        return TreeState(error='not a git tree')
    try:
        changed = _git(path, 'status', '--porcelain')
        since = ['--not', '--remotes', *([applied] if applied else [])]
        commits = _git(path, 'log', '--format=%h %s', 'HEAD', '--branches', *since)
    except OSError as ex:
        return TreeState(error=str(ex))
    return TreeState(tuple(changed), tuple(commits))


def remove_tree(path: pathlib.Path, directory: pathlib.Path) -> None:
    """Delete a session tree; only ever one directly under *directory*."""
    path, directory = path.resolve(), directory.resolve()
    if path.parent != directory or path.is_symlink():
        raise OSError(f'{path} is not a session tree under {directory}')
    shutil.rmtree(path)

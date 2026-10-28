# SPDX-License-Identifier: GPL-2.0-or-later
"""Refs a project names by glob: its base_refs (read-only bases, e.g. a remote's release
branches), and local branches for the remote branches matching its track_branches (e.g.
origin/scratch/<user>/*): made with their upstream set and marked as tracked, then
fast-forwarded as the remote moves. A checked-out or diverged branch is left as it is
and said. Only refs change; nothing is ever pushed.
"""

import fnmatch
import pathlib
import subprocess
from typing import Sequence

MARK = 'b4m-track'  # branch.<name>.b4m-track = true: made and moved here.
# b4m-untracked.<name>.at = <remote commit>: removed here; back once the remote moves.
FORGOTTEN = 'b4m-untracked'


def _git(repo: pathlib.Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ['git', *args], cwd=repo, capture_output=True, text=True, stdin=subprocess.DEVNULL
    )


def tracked_branches(repo: pathlib.Path) -> set[str]:
    """The local branches made here for remote ones."""
    run = _git(repo, 'config', '--get-regexp', rf'^branch\..*\.{MARK}$')
    return {
        line.split(' ', 1)[0].removeprefix('branch.').removesuffix(f'.{MARK}')
        for line in run.stdout.splitlines()
    }


def track_branches(repo: pathlib.Path, patterns: Sequence[str]) -> tuple[list[str], list[str]]:
    """(local branches made or moved, problems) for the remote-tracking refs matching
    *patterns* (remote/branch globs)."""
    if not patterns:
        return [], []
    refs = _git(repo, 'for-each-ref', '--format=%(refname:short) %(objectname)', 'refs/remotes/')
    checked_out = {
        line.removeprefix('branch refs/heads/')
        for line in _git(repo, 'worktree', 'list', '--porcelain').stdout.splitlines()
        if line.startswith('branch ')
    }
    moved, problems = [], []
    for line in refs.stdout.splitlines():
        ref, _, sha = line.partition(' ')
        if not any(fnmatch.fnmatchcase(ref, p) for p in patterns) or '/' not in ref:
            continue
        remote, name = ref.split('/', 1)
        local = _git(repo, 'rev-parse', '--verify', '--quiet', f'refs/heads/{name}').stdout.strip()
        if local == sha:
            continue
        removed_at = _git(repo, 'config', '--get', f'{FORGOTTEN}.{name}.at').stdout.strip()
        if not local and removed_at == sha:
            continue  # Removed here, and the remote has not moved since.
        if removed_at:
            _git(repo, 'config', '--remove-section', f'{FORGOTTEN}.{name}')
        if not local:
            made = _git(repo, 'branch', '--track', name, ref)
            if made.returncode != 0:
                problems.append(f'{name}: {made.stderr.strip()}')
                continue
            _git(repo, 'config', f'branch.{name}.{MARK}', 'true')
            moved.append(name)
            continue
        if name in checked_out:
            problems.append(f'{name} is checked out: not moved to {ref}')
            continue
        if _git(repo, 'merge-base', '--is-ancestor', local, sha).returncode != 0:
            problems.append(f'{name} has diverged from {ref}: left as it is')
            continue
        updated = _git(repo, 'update-ref', f'refs/heads/{name}', sha, local)
        if updated.returncode != 0:
            problems.append(f'{name}: {updated.stderr.strip()}')
            continue
        moved.append(name)
    return moved, problems


def base_refs(repo: pathlib.Path, globs: Sequence[str]) -> list[str]:
    """The refs (remote-tracking or local) matching *globs*, sorted."""
    if not globs:
        return []
    run = _git(repo, 'for-each-ref', '--format=%(refname:short)', 'refs/remotes/', 'refs/heads/')
    if run.returncode != 0:
        return []
    return sorted(n for n in run.stdout.split() if any(fnmatch.fnmatchcase(n, g) for g in globs))


def forget_tracked(repo: pathlib.Path, name: str) -> None:
    """Remember that *name* was removed here at its remote's current commit, so tracking
    brings it back only when the remote branch moves (call before deleting it)."""
    upstream = _git(repo, 'rev-parse', '--verify', '--quiet', f'{name}@{{upstream}}')
    sha = upstream.stdout.strip()
    if sha:
        _git(repo, 'config', f'{FORGOTTEN}.{name}.at', sha)

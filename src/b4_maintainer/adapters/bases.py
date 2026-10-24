# SPDX-License-Identifier: GPL-2.0-or-later
"""Maintainer bases: how far behind its upstream a base is, what of it has landed there,
and moving it onto the new upstream.

Plain git on the project repository, never its checkout: the base is read with rev-list
and log, and rebased with `git replay` (no worktree, no index), one commit at a time onto
the upstream tip, then moved with a single `update-ref` that fails if the branch moved
meanwhile. Landed commits are found as tree evidence finds patches (commit messages
only: a Link:/Message-ID: naming the same message, else an identical subject), so a
blobless checkout fetches nothing to answer. Blocking: call from a worker.
"""

import pathlib
import subprocess
from dataclasses import dataclass
from typing import Optional

from ..workspace import MaintainerBase
from .treematch import message_ids, subject_key

SCRATCH_REF = 'refs/b4m/rebase'  # Named in replay's output only; never written.


@dataclass(frozen=True)
class BaseCommit:
    sha: str
    subject: str
    author: str  # Name <email>
    landed: bool = False  # Its change is in the upstream branch already.


@dataclass(frozen=True)
class BaseStatus:
    base: MaintainerBase
    exists: bool = False
    tip: str = ''
    fork: str = ''  # Where the base leaves its upstream.
    behind: int = 0  # Upstream commits the base does not have.
    commits: tuple[BaseCommit, ...] = ()  # The base's own commits, oldest first.
    checked_out: str = ''  # A worktree that has the branch checked out.
    error: str = ''


@dataclass(frozen=True)
class RebaseResult:
    ok: bool
    tip: str = ''  # The base's new tip.
    dropped: tuple[str, ...] = ()  # Subjects: landed upstream, or empty once replayed.
    failed: Optional[BaseCommit] = None  # The commit that did not replay.
    error: str = ''


def _git(repo: pathlib.Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    run = subprocess.run(
        ['git', *args], cwd=repo, capture_output=True, text=True, stdin=subprocess.DEVNULL
    )
    if check and run.returncode != 0:
        raise OSError(run.stderr.strip() or f'git {args[0]} failed')
    return run


def _commit(repo: pathlib.Path, rev: str) -> Optional[str]:
    run = _git(repo, 'rev-parse', '--verify', '--quiet', f'{rev}^{{commit}}', check=False)
    return run.stdout.strip() if run.returncode == 0 else None


def _checked_out(repo: pathlib.Path, branch: str) -> str:
    path = ''
    for line in _git(repo, 'worktree', 'list', '--porcelain').stdout.splitlines():
        if line.startswith('worktree '):
            path = line.removeprefix('worktree ')
        elif line == f'branch refs/heads/{branch}':
            return path
    return ''


def _fork(repo: pathlib.Path, upstream: str, branch: str) -> str:
    """Where the branch left its upstream; the upstream's reflog finds it even after the
    upstream was rewritten."""
    run = _git(repo, 'merge-base', '--fork-point', upstream, branch, check=False)
    if run.returncode == 0 and run.stdout.strip():
        return run.stdout.strip()
    return _git(repo, 'merge-base', upstream, branch).stdout.strip()


def _log(repo: pathlib.Path, *args: str) -> list[tuple[str, str, str, str]]:
    """(sha, subject, author, body) per commit, oldest first."""
    out = _git(repo, 'log', '--reverse', '--format=%x1e%H%x00%s%x00%an <%ae>%x00%B', *args)
    records = []
    for record in out.stdout.split('\x1e')[1:]:
        sha, subject, author, body = record.split('\x00', 3)
        records.append((sha, subject, author, body))
    return records


def base_status(repo: pathlib.Path, base: MaintainerBase) -> BaseStatus:
    tip = _commit(repo, f'refs/heads/{base.branch}')
    if tip is None:
        return BaseStatus(base)
    upstream = _commit(repo, base.upstream)
    if upstream is None:
        return BaseStatus(base, True, tip, error=f'upstream {base.upstream} not found')
    try:
        fork = _fork(repo, base.upstream, base.branch)
        behind = int(_git(repo, 'rev-list', '--count', f'{fork}..{upstream}').stdout)
        merges = _git(repo, 'rev-list', '--merges', f'{fork}..{tip}').stdout.split()
        upstream_log = _log(repo, '--no-merges', f'{fork}..{upstream}')
        own = _log(repo, '--no-merges', f'{fork}..{tip}')
    except (OSError, ValueError) as ex:
        return BaseStatus(base, True, tip, error=str(ex))
    landed_ids: set[str] = set()
    landed_subjects = set()
    for _sha, subject, _author, body in upstream_log:
        landed_subjects.add(subject_key(subject))
        landed_ids |= message_ids(body)
    commits = tuple(
        BaseCommit(
            sha,
            subject,
            author,
            subject_key(subject) in landed_subjects or bool(message_ids(body) & landed_ids),
        )
        for sha, subject, author, body in own
    )
    error = f'{len(merges)} merge commits: rebase it by hand' if merges else ''
    return BaseStatus(
        base, True, tip, fork, behind, commits, _checked_out(repo, base.branch), error
    )


def rebase_base(
    repo: pathlib.Path, base: MaintainerBase, expected: Optional[str] = None
) -> RebaseResult:
    """Move the base onto its upstream's tip: landed commits dropped, the rest replayed in
    order. A conflict, a checked-out branch, or a branch that moved since *expected* (its
    tip when the maintainer looked) changes nothing."""
    status = base_status(repo, base)
    if not status.exists:
        return RebaseResult(False, error=f'{base.branch} does not exist')
    if status.error:
        return RebaseResult(False, error=status.error)
    if status.checked_out:
        return RebaseResult(
            False, error=f'{base.branch} is checked out in {status.checked_out}: rebase it there'
        )
    if expected is not None and expected != status.tip:
        return RebaseResult(False, error=f'{base.branch} moved since it was shown')
    tip = _commit(repo, base.upstream)
    assert tip is not None
    dropped = [c.subject for c in status.commits if c.landed]
    for commit in (c for c in status.commits if not c.landed):
        run = _git(
            repo,
            'replay',
            '--ref-action=print',
            '--ref',
            SCRATCH_REF,
            '--onto',
            tip,
            f'{commit.sha}^..{commit.sha}',
            check=False,
        )
        fields = run.stdout.split()
        if run.returncode != 0 or len(fields) < 3 or fields[0] != 'update':
            return RebaseResult(False, failed=commit, error=run.stderr.strip())
        new = fields[2]
        if (
            _git(repo, 'rev-parse', f'{new}^{{tree}}').stdout
            == _git(repo, 'rev-parse', f'{tip}^{{tree}}').stdout
        ):
            dropped.append(commit.subject)  # Its change is upstream already.
            continue
        tip = new
    moved = _git(
        repo,
        'update-ref',
        '-m',
        f'b4-maintainer: rebase onto {base.upstream}',
        f'refs/heads/{base.branch}',
        tip,
        status.tip,
        check=False,
    )
    if moved.returncode != 0:
        return RebaseResult(False, error=f'{base.branch} moved meanwhile: {moved.stderr.strip()}')
    return RebaseResult(True, tip, tuple(dropped))

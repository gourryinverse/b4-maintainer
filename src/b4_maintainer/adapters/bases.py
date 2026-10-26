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

import os
import pathlib
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional

from ..sessions.records import Worktree
from ..sessions.trees import clone_project
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
    detail: str = ''  # Why it did not: git merge-tree's conflict messages.


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


def _conflicts(repo: pathlib.Path, onto: str, commit: BaseCommit) -> str:
    """git merge-tree's account of replaying *commit* onto *onto* (no worktree, no index):
    the conflicted files and its CONFLICT messages."""
    run = _git(
        repo,
        'merge-tree',
        '--write-tree',
        '--name-only',
        f'--merge-base={commit.sha}^',
        onto,
        commit.sha,
        check=False,
    )
    lines = run.stdout.splitlines()[1:]  # After the tree it would have written.
    return '\n'.join(line for line in lines if line.strip()) or run.stderr.strip()


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
            return RebaseResult(
                False, failed=commit, error=run.stderr.strip(), detail=_conflicts(repo, tip, commit)
            )
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


INCOMING_REF = 'refs/b4m/incoming'  # A rebase result on its way into the project.


def prepare_rebase_tree(
    repo: pathlib.Path,
    dest: pathlib.Path,
    branch: str,
    base: MaintainerBase,
    identity: tuple[str, str],
) -> Worktree:
    """A session tree for rebasing *base* by hand: a clone with the base checked out on
    *branch* and its upstream under the same name. Commits made there carry *identity*
    as committer (authors are kept by a rebase); no hook touches their trailers, which a
    rebase must keep."""
    status = base_status(repo, base)
    if not status.exists or status.error:
        raise OSError(status.error or f'{base.branch} does not exist')
    upstream = _git(repo, 'rev-parse', '--symbolic-full-name', base.upstream).stdout.strip()
    if not upstream:
        raise OSError(f'{base.upstream} is not a ref')
    if os.path.lexists(dest):
        raise OSError(f'{dest} exists')
    try:
        clone_project(repo, dest)
        _git(dest, 'fetch', '--quiet', '--no-tags', str(repo), f'+{upstream}:{upstream}')
        _git(dest, 'checkout', '--quiet', '-b', branch, status.tip)
        _git(dest, 'config', 'user.name', identity[0])
        _git(dest, 'config', 'user.email', identity[1])
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return Worktree(str(dest), branch, status.fork, status.tip)


def accept_rebase(
    repo: pathlib.Path, base: MaintainerBase, tree: pathlib.Path, result: str, expected: str
) -> RebaseResult:
    """Move *base* to the *result* branch of a rebase session's *tree*: only while the base
    is still at *expected* (its tip when the session started), the result contains the
    upstream's current tip, and the base is not checked out. The result's commits come in
    through a scratch ref (never FETCH_HEAD); the project's checkout is not touched."""
    status = base_status(repo, base)
    if not status.exists:
        return RebaseResult(False, error=f'{base.branch} does not exist')
    if status.checked_out:
        return RebaseResult(False, error=f'{base.branch} is checked out in {status.checked_out}')
    if status.tip != expected:
        return RebaseResult(False, error=f'{base.branch} moved since the session started')
    new = _commit(tree, f'refs/heads/{result}')
    if new is None:
        return RebaseResult(False, error=f'the session left no {result} branch')
    upstream = _commit(repo, base.upstream)
    if upstream is None:
        return RebaseResult(False, error=f'upstream {base.upstream} not found')
    incoming = f'{INCOMING_REF}/{base.branch}'
    pushed = _git(
        tree, 'push', '--quiet', '--no-verify', str(repo), f'+{new}:{incoming}', check=False
    )
    if pushed.returncode != 0:
        return RebaseResult(False, error=f'cannot bring the result in: {pushed.stderr.strip()}')
    try:
        on_upstream = _git(repo, 'merge-base', '--is-ancestor', upstream, new, check=False)
        if on_upstream.returncode != 0:
            return RebaseResult(
                False, error=f'the result is not on the current {base.upstream}: rebase again'
            )
        moved = _git(
            repo,
            'update-ref',
            '-m',
            f'b4-maintainer: accept rebase onto {base.upstream}',
            f'refs/heads/{base.branch}',
            new,
            expected,
            check=False,
        )
        if moved.returncode != 0:
            return RebaseResult(
                False, error=f'{base.branch} moved meanwhile: {moved.stderr.strip()}'
            )
    finally:
        _git(repo, 'update-ref', '-d', incoming, check=False)
    return RebaseResult(True, new)

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
from typing import Optional, Sequence

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
    moved: tuple[str, ...] = ()  # Prep branches moved along with the base.
    stuck: tuple[tuple[str, str], ...] = ()  # (prep branch, why it did not move).


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


@dataclass(frozen=True)
class PluckResult:
    ok: bool
    tip: str = ''  # The base's new tip.
    picked: tuple[tuple[str, str], ...] = ()  # (source commit, new commit on the base).
    dropped: tuple[str, ...] = ()  # Subjects whose change the base already has.
    failed: str = ''  # The subject of the commit that did not apply.
    detail: str = ''  # Why: git merge-tree's conflict messages.
    error: str = ''
    checked_out: str = ''  # Refused: the worktree the branch is checked out in.


def _env(**values: str) -> dict[str, str]:
    return {**os.environ, **values}


def _agent_message(message: str, assisted: str, repo: pathlib.Path) -> str:
    """An agent's commit message as the maintainer takes it: its sign-offs dropped (an
    agent never signs off), an Assisted-by trailer added."""
    kept = [line for line in message.splitlines() if not line.lower().startswith('signed-off-by:')]
    run = subprocess.run(
        ['git', 'interpret-trailers', '--trailer', f'Assisted-by: {assisted}'],
        cwd=repo,
        input='\n'.join(kept).rstrip() + '\n',
        capture_output=True,
        text=True,
    )
    if run.returncode != 0:
        raise OSError(run.stderr.strip() or 'git interpret-trailers failed')
    return run.stdout


def _replay(
    repo: pathlib.Path,
    tip: str,
    shas: Sequence[str],
    agent_email: Optional[str] = None,
    assisted: str = '',
) -> PluckResult:
    """Each commit's change merged onto *tip* in turn (git merge-tree, no worktree) and
    committed (git commit-tree): commits by *agent_email* become the maintainer's with
    Assisted-by: *assisted*; others keep author, date, and message. Changes *tip* already
    has are dropped; a conflict stops it all. Moves no ref."""
    picked: list[tuple[str, str]] = []
    dropped: list[str] = []
    for sha in shas:
        fields = _git(repo, 'log', '-1', '--date=raw', '--format=%s%x00%an%x00%ae%x00%ad', sha)
        subject, name, email, date = fields.stdout.rstrip('\n').split('\x00')
        merged = _git(
            repo,
            'merge-tree',
            '--write-tree',
            '--name-only',
            f'--merge-base={sha}^',
            tip,
            sha,
            check=False,
        )
        if merged.returncode != 0:
            detail = '\n'.join(line for line in merged.stdout.splitlines()[1:] if line.strip())
            return PluckResult(False, failed=subject, detail=detail or merged.stderr.strip())
        tree = merged.stdout.splitlines()[0]
        if tree == _git(repo, 'rev-parse', f'{tip}^{{tree}}').stdout.strip():
            dropped.append(subject)
            continue
        message = _git(repo, 'log', '-1', '--format=%B', sha).stdout
        if agent_email and email.lower() == agent_email.lower():
            message, env = _agent_message(message, assisted, repo), dict(os.environ)
            for key in ('GIT_AUTHOR_NAME', 'GIT_AUTHOR_EMAIL', 'GIT_AUTHOR_DATE'):
                env.pop(key, None)  # The maintainer, now.
        else:
            env = _env(GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email, GIT_AUTHOR_DATE=date)
        made = subprocess.run(
            ['git', 'commit-tree', tree, '-p', tip],
            cwd=repo,
            input=message,
            capture_output=True,
            text=True,
            env=env,
        )
        if made.returncode != 0:
            return PluckResult(False, error=f'cannot commit {subject}: {made.stderr.strip()}')
        tip = made.stdout.strip()
        picked.append((sha, tip))
    return PluckResult(True, tip, tuple(picked), tuple(dropped))


def pluck(
    repo: pathlib.Path,
    base: MaintainerBase,
    source: pathlib.Path,
    shas: Sequence[str],
    expected: str,
    agent_email: str,
    assisted: str,
) -> PluckResult:
    """Pluck onto a maintainer base (see pluck_onto)."""
    return pluck_onto(repo, base.branch, source, shas, expected, agent_email, assisted)


def pluck_onto(
    repo: pathlib.Path,
    branch: str,
    source: pathlib.Path,
    shas: Sequence[str],
    expected: str,
    agent_email: str,
    assisted: str,
    into_checkout: bool = False,
) -> PluckResult:
    """Copy *shas* (oldest first) from *source* (a session tree, or the project itself)
    onto *branch* (a maintainer base, a prep branch), which must still be at *expected*
    and not checked out; with *into_checkout*, a checked-out branch moves by
    fast-forwarding its checkout (git merge --ff-only: it refuses rather than overwrite
    local changes, and keeps the others).

    Each commit's change is merged onto the branch's tip with git merge-tree (no
    worktree, no index) and committed with git commit-tree: commits by *agent_email*
    become the maintainer's (author from the project's git identity, Assisted-by:
    *assisted*); any other commit keeps its author, date, and message. A change the
    branch already has is dropped; a conflict stops it all and changes nothing. The
    branch moves in one guarded update-ref; source commits come in through scratch refs,
    deleted after."""
    tip0 = _commit(repo, f'refs/heads/{branch}')
    if tip0 is None:
        return PluckResult(False, error=f'{branch} does not exist')
    checked_out = _checked_out(repo, branch)
    if checked_out and not into_checkout:
        return PluckResult(
            False, error=f'{branch} is checked out in {checked_out}', checked_out=checked_out
        )
    if tip0 != expected:
        return PluckResult(False, error=f'{branch} moved since it was shown')
    incoming = f'{INCOMING_REF}/pluck'
    same = source.resolve() == repo.resolve()
    try:
        if not same and shas:
            refspecs = [f'+{s}:{incoming}/{n}' for n, s in enumerate(shas)]
            pushed = _git(
                source, 'push', '--quiet', '--no-verify', str(repo), *refspecs, check=False
            )
            if pushed.returncode != 0:
                return PluckResult(
                    False, error=f'cannot bring the commits in: {pushed.stderr.strip()}'
                )
        replayed = _replay(repo, tip0, shas, agent_email, assisted)
        if not replayed.ok:
            return replayed
        tip, picked = replayed.tip, replayed.picked
        if tip != tip0 and checked_out:
            merged = _git(
                pathlib.Path(checked_out), 'merge', '--ff-only', '--quiet', tip, check=False
            )
            if merged.returncode != 0:
                why = (merged.stderr or merged.stdout).strip().splitlines()
                return PluckResult(
                    False, error=f'{branch} stays: {" ".join(why[:2]) or "git merge failed"}'
                )
        elif tip != tip0:
            moved = _git(
                repo,
                'update-ref',
                '-m',
                f'b4-maintainer: pluck {len(picked)} commits',
                f'refs/heads/{branch}',
                tip,
                expected,
                check=False,
            )
            if moved.returncode != 0:
                return PluckResult(False, error=f'{branch} moved meanwhile: {moved.stderr.strip()}')
        return replayed
    finally:
        refs = _git(
            repo, 'for-each-ref', '--format=%(refname)', incoming, check=False
        ).stdout.split()
        for ref in refs:
            _git(repo, 'update-ref', '-d', ref, check=False)


def series_commits(
    repo: pathlib.Path, base: MaintainerBase, patches: Sequence[tuple[str, str]]
) -> list[BaseCommit]:
    """The base's own commits (oldest first) carrying any of *patches* ((message-id,
    subject) of a posted series): by a Link or Message-ID trailer, else by subject."""
    tip = _commit(repo, f'refs/heads/{base.branch}')
    if tip is None or _commit(repo, base.upstream) is None:
        return []
    msgids = {m for m, _ in patches if m}
    subjects = {subject_key(s) for _, s in patches if s}
    fork = _fork(repo, base.upstream, base.branch)
    return [
        BaseCommit(sha, subject, author)
        for sha, subject, author, body in _log(repo, '--no-merges', f'{fork}..{tip}')
        if message_ids(body) & msgids or subject_key(subject) in subjects
    ]


def drop_commits(
    repo: pathlib.Path, branch: str, shas: Sequence[str], expected: str
) -> PluckResult:
    """*branch* without *shas* (its commits, oldest first): the commits after the first of
    them are replayed onto its parent (as plucking replays, authors kept), and the branch
    moves in one guarded update-ref. It must still be at *expected* and not checked out;
    a later commit that does not replay without them stops it all and changes nothing.
    *dropped* in the result: the subjects taken out."""
    tip0 = _commit(repo, f'refs/heads/{branch}')
    if tip0 is None:
        return PluckResult(False, error=f'{branch} does not exist')
    checked_out = _checked_out(repo, branch)
    if checked_out:
        return PluckResult(False, error=f'{branch} is checked out in {checked_out}')
    if tip0 != expected:
        return PluckResult(False, error=f'{branch} moved since it was shown')
    if not shas:
        return PluckResult(False, error='nothing to drop')
    start = _commit(repo, f'{shas[0]}^')
    if start is None:
        return PluckResult(False, error=f'{shas[0][:12]} has no parent to replay onto')
    span = _git(repo, 'rev-list', '--reverse', f'{start}..{tip0}').stdout.split()
    if not set(shas) <= set(span):
        return PluckResult(False, error=f'not all of those commits are on {branch}')
    if _git(repo, 'rev-list', '--merges', f'{start}..{tip0}').stdout.strip():
        return PluckResult(False, error=f'{branch} has merge commits there: drop by hand')
    gone = set(shas)
    subjects = tuple(_git(repo, 'log', '-1', '--format=%s', sha).stdout.strip() for sha in shas)
    replayed = _replay(repo, start, [c for c in span if c not in gone])
    if not replayed.ok:
        return replayed
    moved = _git(
        repo,
        'update-ref',
        '-m',
        f'b4-maintainer: drop {len(shas)} commits',
        f'refs/heads/{branch}',
        replayed.tip,
        expected,
        check=False,
    )
    if moved.returncode != 0:
        return PluckResult(False, error=f'{branch} moved meanwhile: {moved.stderr.strip()}')
    return PluckResult(True, replayed.tip, replayed.picked, subjects)


def accept_onto(
    repo: pathlib.Path, branch: str, tree: pathlib.Path, ref: str, expected: str
) -> RebaseResult:
    """Move *branch* to *ref* of a session's *tree* (an apply session's result): only while
    the branch is still at *expected* (its tip when the session started), *ref* builds on
    it, and the branch is not checked out. The commits come in through a scratch ref."""
    tip = _commit(repo, f'refs/heads/{branch}')
    if tip is None:
        return RebaseResult(False, error=f'{branch} does not exist')
    checked_out = _checked_out(repo, branch)
    if checked_out:
        return RebaseResult(False, error=f'{branch} is checked out in {checked_out}')
    if tip != expected:
        return RebaseResult(False, error=f'{branch} moved since the session started')
    new = _commit(tree, f'refs/heads/{ref}')
    if new is None:
        return RebaseResult(False, error=f'the session has no {ref} branch')
    if new == expected:
        return RebaseResult(False, error='the session has applied nothing yet')
    incoming = f'{INCOMING_REF}/{branch}'
    pushed = _git(
        tree, 'push', '--quiet', '--no-verify', str(repo), f'+{new}:{incoming}', check=False
    )
    if pushed.returncode != 0:
        return RebaseResult(False, error=f'cannot bring the result in: {pushed.stderr.strip()}')
    try:
        if _git(repo, 'merge-base', '--is-ancestor', expected, new, check=False).returncode:
            return RebaseResult(False, error=f'the result does not build on {branch}')
        moved = _git(
            repo,
            'update-ref',
            '-m',
            'b4-maintainer: accept an apply session',
            f'refs/heads/{branch}',
            new,
            expected,
            check=False,
        )
        if moved.returncode != 0:
            return RebaseResult(False, error=f'{branch} moved meanwhile: {moved.stderr.strip()}')
    finally:
        _git(repo, 'update-ref', '-d', incoming, check=False)
    return RebaseResult(True, new)


ZERO = '0' * 40


def copy_to_tree(
    source: pathlib.Path, tree: pathlib.Path, shas: Sequence[str], branch: str, start: str
) -> PluckResult:
    """Copy *shas* (oldest first) from *source* onto *branch* in a session *tree* (made at
    *start* if missing), as they are: no attribution rewrite, they stay session work. A
    branch checked out in the tree (or one of its checkouts) is refused; a conflict
    changes nothing. Source commits come in through scratch refs, deleted after."""
    listed = _git(tree, 'worktree', 'list', '--porcelain').stdout
    if f'branch refs/heads/{branch}\n' in listed + '\n':
        return PluckResult(False, error=f'{branch} is checked out in that tree')
    tip0 = _commit(tree, f'refs/heads/{branch}')
    incoming = f'{INCOMING_REF}/copy'
    try:
        if source.resolve() != tree.resolve() and shas:
            refspecs = [f'+{s}:{incoming}/{n}' for n, s in enumerate(shas)]
            pushed = _git(
                source, 'push', '--quiet', '--no-verify', str(tree), *refspecs, check=False
            )
            if pushed.returncode != 0:
                return PluckResult(
                    False, error=f'cannot bring the commits in: {pushed.stderr.strip()}'
                )
        replayed = _replay(tree, tip0 or start, shas)
        if not replayed.ok:
            return replayed
        if replayed.tip != (tip0 or start) or tip0 is None:
            moved = _git(
                tree,
                'update-ref',
                '-m',
                'b4-maintainer: copied from another tree',
                f'refs/heads/{branch}',
                replayed.tip,
                tip0 or ZERO,
                check=False,
            )
            if moved.returncode != 0:
                return PluckResult(False, error=f'{branch} moved meanwhile: {moved.stderr.strip()}')
        return replayed
    finally:
        refs = _git(
            tree, 'for-each-ref', '--format=%(refname)', incoming, check=False
        ).stdout.split()
        for ref in refs:
            _git(tree, 'update-ref', '-d', ref, check=False)

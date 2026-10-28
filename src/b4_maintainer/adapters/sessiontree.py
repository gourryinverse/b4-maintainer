# SPDX-License-Identifier: GPL-2.0-or-later
"""A worktree session's tree: the project shared into its own clone, the series applied.

`git clone --shared` borrows the project's objects (no copy) and keeps everything else
apart: the project's HEAD, index, refs, FETCH_HEAD, and worktree list never change, and
the session's sandbox only needs its own clone writable. The series comes from b4:

- a series under b4 review (`b4/review/<change-id>`, same revision) starts from the review
  branch's last series commit, without b4's tracking commit;
- otherwise b4 builds the am-ready mbox and applies it at the base (the caller's, or
  `detect_apply_base()`'s), 3-way, into the clone's FETCH_HEAD.

Commits the agent makes carry the configured agent identity, unsigned, and a commit-msg
hook strips Signed-off-by and review trailers. Blocking: call from a worker.
"""

import contextlib
import dataclasses
import io
import logging
import os
import pathlib
import shutil
import subprocess
from typing import Generator, Optional, Sequence

import b4
import b4.review

from ..sessions.hub import TreeError
from ..sessions.records import Worktree
from ..sessions.trees import AGENT_MBOX, TreeItem, clone_project
from .b4state import b4_repo

TRAILERS = ('Signed-off-by', 'Reviewed-by', 'Acked-by', 'Tested-by', 'Co-developed-by')
COMMIT_MSG = (
    '#!/bin/sh\n'
    '# b4-maintainer: agent commits carry no sign-off or review trailers.\n'
    f'sed -i -E \'/^({"|".join(TRAILERS)}):/Id\' "$1"\n'
)


class PrepareError(TreeError):
    pass


def _git(repo: pathlib.Path, *args: str) -> str:
    run = subprocess.run(
        ['git', *args], cwd=repo, capture_output=True, text=True, stdin=subprocess.DEVNULL
    )
    if run.returncode != 0:
        raise PrepareError(f'git {args[0]} failed: {run.stderr.strip()}', run.stderr)
    return run.stdout.strip()


def _commit(repo: pathlib.Path, rev: str) -> Optional[str]:
    run = subprocess.run(
        ['git', 'rev-parse', '--verify', '--quiet', f'{rev}^{{commit}}'],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    return run.stdout.strip() if run.returncode == 0 else None


@contextlib.contextmanager
def _captured() -> Generator[io.StringIO, None, None]:
    """b4's log output while preparing, for reporting a failure."""
    out = io.StringIO()
    handler = logging.StreamHandler(out)
    handler.setFormatter(logging.Formatter('%(message)s'))
    b4.logger.addHandler(handler)
    try:
        yield out
    finally:
        b4.logger.removeHandler(handler)


def _clone(project: pathlib.Path, dest: pathlib.Path) -> None:
    try:
        clone_project(project, dest)
    except OSError as ex:
        raise PrepareError(f'cannot clone {project.name}: {ex}', str(ex)) from ex


def _from_review(project: pathlib.Path, change_id: str, revision: int) -> Optional[tuple[str, str]]:
    """(base, last series commit) of the series' b4 review branch, if it has this revision."""
    if not change_id:
        return None
    branch = f'{b4.review.REVIEW_BRANCH_PREFIX}{change_id}'
    if _commit(project, f'refs/heads/{branch}') is None:
        return None
    try:
        _cover, tracking = b4.review.load_tracking(str(project), branch)
    except SystemExit:
        return None
    series = tracking.get('series', {})
    if int(series.get('revision', 0) or 0) != revision:
        return None
    first = _commit(project, str(series.get('first-patch-commit', '')))
    base = _commit(project, f'{first}~1') if first else None
    applied = _commit(project, f'refs/heads/{branch}~1')
    if base is None or applied is None:
        return None
    return base, applied


def _am_bytes(
    source: str, revision: int, patches: Sequence[int] = ()
) -> tuple['b4.LoreSeries', bytes]:
    lmbx = b4.LoreMailbox()
    for msg in b4.get_msgs_from_mailbox_or_maildir(source):
        lmbx.add_message(msg)
    lser = lmbx.get_series(revision or None, reroll=False, codereview_trailers=False)
    if lser is None or not any(lser.patches[1:]):
        raise PrepareError(f'no v{revision} series in {source}')
    b4.MAIN_CONFIG['attestation-policy'] = 'off'  # Offline; the session reviews the code.
    msgs = lser.get_am_ready(
        noaddtrailers=True, allowbadchars=True, cherrypick=list(patches) or None
    )
    if not msgs:
        raise PrepareError(f'v{revision}: no patches ready to apply')
    out = io.BytesIO()
    b4.save_git_am_mbox(msgs, out)
    return lser, out.getvalue()


def _apply(
    project: pathlib.Path,
    dest: pathlib.Path,
    source: str,
    revision: int,
    base: Optional[str],
    patches: Sequence[int] = (),
) -> tuple[str, str]:
    """(base, last series commit), applying the series (only *patches*, if any) from its
    mail."""
    sha: Optional[str] = None
    with b4_repo(project), _captured() as log:
        try:
            lser, ambytes = _am_bytes(source, revision, patches)
            if base is None:
                base, _hint = b4.review.detect_apply_base(lser, str(project))
            sha = _commit(project, base)
            if sha is None:
                raise PrepareError(f'base {base!r} is not a commit in {project.name}')
            b4.git_fetch_am_into_repo(
                str(dest), ambytes, at_base=sha, am_flags=['-3'], resolve=True
            )
        except b4.AmConflictError as ex:
            raise PrepareError(
                f'v{revision} does not apply at {base}', ex.output or log.getvalue(), sha or ''
            ) from ex
        except PrepareError:
            raise
        except Exception as ex:
            raise PrepareError(f'b4 could not apply v{revision}: {ex}', log.getvalue()) from ex
    return sha, _git(dest, 'rev-parse', 'FETCH_HEAD')


def _apply_more(
    project: pathlib.Path,
    dest: pathlib.Path,
    source: str,
    revision: int,
    tip: str,
    patches: Sequence[int] = (),
) -> str:
    """A further series applied on top of *tip* (a commit in the clone): the new tip."""
    with b4_repo(project), _captured() as log:
        subject = f'v{revision}'
        try:
            lser, ambytes = _am_bytes(source, revision, patches)
            subject = f'{lser.subject} v{revision}'
            b4.git_fetch_am_into_repo(
                str(dest), ambytes, at_base=tip, am_flags=['-3'], resolve=True
            )
        except b4.AmConflictError as ex:
            raise PrepareError(
                f'{subject} does not apply on top of the series before it',
                ex.output or log.getvalue(),
            ) from ex
        except PrepareError:
            raise
        except Exception as ex:
            raise PrepareError(f'b4 could not apply {subject}: {ex}', log.getvalue()) from ex
    return _git(dest, 'rev-parse', 'FETCH_HEAD')


def _agent(dest: pathlib.Path, identity: tuple[str, str]) -> None:
    _git(dest, 'config', 'user.name', identity[0])
    _git(dest, 'config', 'user.email', identity[1])
    hook = pathlib.Path(_git(dest, 'rev-parse', '--git-path', 'hooks')) / 'commit-msg'
    hook = hook if hook.is_absolute() else dest / hook
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(COMMIT_MSG)
    hook.chmod(0o755)


def series_mbox(
    project: pathlib.Path, source: str, revision: int, patches: Sequence[int] = ()
) -> bytes:
    """b4's am-ready mbox of the series (only *patches*, if any), as a session tree
    applies it."""
    with b4_repo(project), _captured() as log:
        try:
            return _am_bytes(source, revision, patches)[1]
        except PrepareError:
            raise
        except Exception as ex:
            raise PrepareError(f'b4 could not read v{revision}: {ex}', log.getvalue()) from ex


def base_suggestions(project: pathlib.Path) -> list[str]:
    """b4's apply-base suggestions for the project: HEAD, the configured apply base and
    target branches, recently used take branches."""
    with b4_repo(project):
        return b4.review.apply_base_suggestions(str(project))


def prepare_tree(
    project: pathlib.Path,
    dest: pathlib.Path,
    branch: str,
    source: str,
    revision: int,
    change_id: str = '',
    base: Optional[str] = None,
    identity: tuple[str, str] = ('b4-maintainer agent', 'agent@b4-maintainer.invalid'),
    also: Sequence[tuple[str, int]] = (),
) -> Worktree:
    """The session's tree at *dest*, on *branch*; *also*, further series (source,
    revision) applied in order on top (a combined tree, all of it counted as series).
    On failure nothing is left behind."""
    if os.path.lexists(dest):
        raise PrepareError(f'{dest} exists')
    try:
        _clone(project, dest)
        reviewed = _from_review(project, change_id, revision) if base is None else None
        if reviewed is not None:
            start, applied = reviewed
        else:
            start, applied = _apply(project, dest, source, revision, base)
        for more, more_revision in also:
            applied = _apply_more(project, dest, more, more_revision, applied)
        _git(dest, 'checkout', '--quiet', '-b', branch, applied)
        for other in _git(dest, 'branch', '--format=%(refname:short)').splitlines():
            if other != branch:  # The clone's default branch: the project's, not the session's.
                _git(dest, 'branch', '--quiet', '-D', other)
        _agent(dest, identity)
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return Worktree(str(dest), branch, start, applied)


def prepare_start_tree(
    project: pathlib.Path,
    dest: pathlib.Path,
    branch: str,
    start: str,
    identity: tuple[str, str],
    source: Optional[pathlib.Path] = None,
) -> Worktree:
    """A derived session's tree: the project shared into its own clone, on *branch* at
    *start* (a commit or ref), no series applied. *source* is another session's tree
    holding *start* (its own commits are fetched in); else the project. Agent identity
    and the sign-off hook as for any session tree; on failure nothing is left."""
    if os.path.lexists(dest):
        raise PrepareError(f'{dest} exists')
    try:
        _clone(project, dest)
        where = source or project
        sha = _commit(where, start)
        if sha is None:
            raise PrepareError(f'{start} is not a commit in {where.name}')
        if source is not None:
            _git(dest, 'fetch', '--quiet', '--no-tags', str(source), sha)
        _git(dest, 'checkout', '--quiet', '-b', branch, sha)
        for other in _git(dest, 'branch', '--format=%(refname:short)').splitlines():
            if other != branch:
                _git(dest, 'branch', '--quiet', '-D', other)
        _agent(dest, identity)
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return Worktree(str(dest), branch, sha, sha)


def prepare_unapplied_tree(
    project: pathlib.Path,
    dest: pathlib.Path,
    branch: str,
    ambytes: bytes,
    base: str,
    identity: tuple[str, str],
) -> Worktree:
    """A tree for a series b4 could not apply: on *branch* at *base*, with b4's am-ready
    mbox (*ambytes*) at .git/b4m/series.mbx for the agent to apply and resolve by hand.
    On failure nothing is left."""
    tree = prepare_start_tree(project, dest, branch, base, identity)
    try:
        mbox = dest / AGENT_MBOX
        mbox.parent.mkdir(parents=True, exist_ok=True)
        mbox.write_bytes(ambytes)
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return dataclasses.replace(tree, agent_applies=True)


def prepare_items_tree(
    project: pathlib.Path,
    dest: pathlib.Path,
    branch: str,
    base: str,
    items: Sequence[TreeItem],
    identity: tuple[str, str],
) -> Worktree:
    """A workbench session's tree for one project at *dest*, on *branch*: *base* (empty: b4
    chooses for a first series, else the project's HEAD), then *items* in order: series
    applied by b4 (only some patches, if the item says), local branches' commits beyond the
    base replayed (only the one commit of a single item; authors kept). An
    item that does not apply fails it all, saying where an agent would start; nothing is
    left behind."""
    from .bases import _replay  # The same replay as plucking.

    if os.path.lexists(dest):
        raise PrepareError(f'{dest} exists')
    try:
        _clone(project, dest)
        start = _commit(project, base) if base else None
        if base and start is None:
            raise PrepareError(f'base {base!r} is not a commit in {project.name}')
        tip = start
        for item in (i for i in items if i.apply):  # Read-only items stay out of trees.
            try:
                if item.source and tip is None:
                    start, tip = _apply(
                        project, dest, item.source, item.revision, None, item.patches
                    )
                elif item.source:
                    tip = _apply_more(
                        project, dest, item.source, item.revision, tip or '', item.patches
                    )
                else:
                    if start is None:
                        start = tip = _git(project, 'rev-parse', 'HEAD')
                    where = pathlib.Path(item.repo) if item.repo else project
                    head = _commit(where, item.branch)
                    if head is None:
                        raise PrepareError(f'{item.branch} is not a branch in {where}')
                    if item.repo:
                        _git(dest, 'fetch', '--quiet', '--no-tags', str(where), head)
                    if item.single:  # Only this commit.
                        shas = head
                    else:
                        shas = _git(
                            dest, 'rev-list', '--reverse', '--no-merges', f'{start}..{head}'
                        )
                    replayed = _replay(dest, tip or start, shas.split())
                    if not replayed.ok:
                        raise PrepareError(
                            f'{item.label} does not apply on top of the items before it',
                            replayed.detail or replayed.error,
                        )
                    tip = replayed.tip
            except PrepareError as ex:
                raise PrepareError(str(ex), ex.output, ex.base or start or '') from ex
        if start is None or tip is None:
            start = tip = _git(project, 'rev-parse', 'HEAD')
        _git(dest, 'checkout', '--quiet', '-b', branch, tip)
        for other in _git(dest, 'branch', '--format=%(refname:short)').splitlines():
            if other != branch:
                _git(dest, 'branch', '--quiet', '-D', other)
        _agent(dest, identity)
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return Worktree(str(dest), branch, start, tip)


def items_mbox(project: pathlib.Path, start: str, items: Sequence[TreeItem]) -> bytes:
    """Every item as one mbox, in order, for an agent to git am at *start*: b4's am-ready
    patches of each series, and each branch's commits beyond *start* (git format-patch)."""
    parts = []
    for item in (i for i in items if i.apply):
        if item.source:
            parts.append(series_mbox(project, item.source, item.revision, item.patches))
            continue
        where = pathlib.Path(item.repo) if item.repo else project
        commits = ['-1', item.branch] if item.single else [f'{start}..{item.branch}']
        run = subprocess.run(
            ['git', 'format-patch', '--stdout', '--no-signature', *commits],
            cwd=where,
            capture_output=True,
            stdin=subprocess.DEVNULL,
        )
        if run.returncode != 0:
            raise PrepareError(f'cannot export {item.label}', run.stderr.decode(errors='replace'))
        parts.append(run.stdout)
    return b''.join(parts)

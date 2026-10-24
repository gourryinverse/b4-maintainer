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
import io
import logging
import os
import pathlib
import shutil
import subprocess
from typing import Generator, Optional

import b4
import b4.review

from ..sessions.hub import TreeError
from ..sessions.records import Worktree
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


def _promisors(project: pathlib.Path) -> list[str]:
    """The project's promisor remotes (a blobless clone fetches missing blobs from them)."""
    run = subprocess.run(
        ['git', 'config', '--get-regexp', r'^remote\..*\.promisor$'],
        cwd=project,
        capture_output=True,
        text=True,
    )
    names = []
    for line in run.stdout.splitlines():
        key, _, value = line.partition(' ')
        if value.strip().lower() in {'true', 'yes', 'on', '1'}:
            names.append(key.removeprefix('remote.').removesuffix('.promisor'))
    return names


def _clone(project: pathlib.Path, dest: pathlib.Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    _git(dest.parent, 'clone', '--quiet', '--shared', '--no-checkout', str(project), str(dest))
    # Blobs a blobless project lacks are fetched from its remotes into the clone.
    promisors = _promisors(project)
    for name in promisors:
        _git(dest, 'config', f'remote.{name}.url', _git(project, 'remote', 'get-url', name))
        _git(dest, 'config', f'remote.{name}.promisor', 'true')
        filt = _git(
            project, 'config', '--default', 'blob:none', f'remote.{name}.partialclonefilter'
        )
        _git(dest, 'config', f'remote.{name}.partialclonefilter', filt)
    if promisors:
        _git(dest, 'config', 'extensions.partialClone', promisors[0])
    settings = {'commit.gpgSign': 'false', 'tag.gpgSign': 'false', 'gc.auto': '0'}
    # Files belong to another uid inside the sandbox: without this git re-hashes the whole
    # tree on its first command there (18 s for a kernel tree).
    settings['core.checkStat'] = 'minimal'
    settings['checkout.workers'] = '0'  # Parallel: a kernel tree checks out in about 20 s.
    for key, value in settings.items():
        _git(dest, 'config', key, value)


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


def _am_bytes(source: str, revision: int) -> tuple['b4.LoreSeries', bytes]:
    lmbx = b4.LoreMailbox()
    for msg in b4.get_msgs_from_mailbox_or_maildir(source):
        lmbx.add_message(msg)
    lser = lmbx.get_series(revision, reroll=False, codereview_trailers=False)
    if lser is None or not any(lser.patches[1:]):
        raise PrepareError(f'no v{revision} series in {source}')
    b4.MAIN_CONFIG['attestation-policy'] = 'off'  # Offline; the session reviews the code.
    msgs = lser.get_am_ready(noaddtrailers=True, allowbadchars=True)
    if not msgs:
        raise PrepareError(f'v{revision}: no patches ready to apply')
    out = io.BytesIO()
    b4.save_git_am_mbox(msgs, out)
    return lser, out.getvalue()


def _apply(
    project: pathlib.Path, dest: pathlib.Path, source: str, revision: int, base: Optional[str]
) -> tuple[str, str]:
    """(base, last series commit), applying the series from its mail."""
    with b4_repo(project), _captured() as log:
        try:
            lser, ambytes = _am_bytes(source, revision)
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
                f'v{revision} does not apply at {base}', ex.output or log.getvalue()
            ) from ex
        except PrepareError:
            raise
        except Exception as ex:
            raise PrepareError(f'b4 could not apply v{revision}: {ex}', log.getvalue()) from ex
    return sha, _git(dest, 'rev-parse', 'FETCH_HEAD')


def _agent(dest: pathlib.Path, identity: tuple[str, str]) -> None:
    _git(dest, 'config', 'user.name', identity[0])
    _git(dest, 'config', 'user.email', identity[1])
    hook = pathlib.Path(_git(dest, 'rev-parse', '--git-path', 'hooks')) / 'commit-msg'
    hook = hook if hook.is_absolute() else dest / hook
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(COMMIT_MSG)
    hook.chmod(0o755)


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
) -> Worktree:
    """The session's tree at *dest*, on *branch*; on failure nothing is left behind."""
    if os.path.lexists(dest):
        raise PrepareError(f'{dest} exists')
    try:
        _clone(project, dest)
        reviewed = _from_review(project, change_id, revision) if base is None else None
        if reviewed is not None:
            start, applied = reviewed
        else:
            start, applied = _apply(project, dest, source, revision, base)
        _git(dest, 'checkout', '--quiet', '-b', branch, applied)
        for other in _git(dest, 'branch', '--format=%(refname:short)').splitlines():
            if other != branch:  # The clone's default branch: the project's, not the session's.
                _git(dest, 'branch', '--quiet', '-D', other)
        _agent(dest, identity)
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return Worktree(str(dest), branch, start, applied)

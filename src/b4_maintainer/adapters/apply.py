# SPDX-License-Identifier: GPL-2.0-or-later
"""A posted series, or some of its patches, onto a maintainer base or a prep branch.

b4 does the work as `b4 am` / `b4 shazam` would: it builds the am-ready mbox (trailers
collected from the list's replies, a Link trailer per patch from `b4.linkmask` or
`b4.linktrailermask`, only the picked patches) and applies it 3-way in a scratch
worktree at the branch's tip. The patches keep their authors; the maintainer commits
them. The branch then moves in one guarded update-ref. b4 records the series for
`b4 ty`, and a base as a recent take branch.

b4 writes its result into the repository's FETCH_HEAD; the maintainer's is put back. A
conflict changes nothing (b4's parked worktree is removed) and says why. Blocking: call
from a worker.
"""

import io
import pathlib
import shutil
from dataclasses import dataclass
from typing import Sequence

import b4
import b4.mbox
import b4.review.tracking

from .b4state import b4_repo
from .bases import _checked_out, _commit, _git
from .sessiontree import _captured


@dataclass(frozen=True)
class SeriesPatch:
    number: int  # b4's patch number, from 1.
    subject: str
    author: str


@dataclass(frozen=True)
class ApplyResult:
    ok: bool
    tip: str = ''
    applied: tuple[str, ...] = ()  # The new commits, oldest first.
    error: str = ''
    output: str = ''  # b4's and git's output, for a failure.
    conflict: bool = False  # The patches do not apply there: an agent can take it.


def _series(source: str, revision: int, lookup: bool) -> 'b4.LoreSeries':
    """*lookup*: also search lore for review trailers given on these patches elsewhere
    (a resend, say), as `b4 am` does when online."""
    lmbx = b4.LoreMailbox()
    for msg in b4.get_msgs_from_mailbox_or_maildir(source):
        lmbx.add_message(msg)
    lser = lmbx.get_series(revision, reroll=False, codereview_trailers=lookup)
    if lser is None or not any(lser.patches[1:]):
        raise ValueError(f'no v{revision} series in {source}')
    return lser


def series_patches(project: pathlib.Path, source: str, revision: int) -> list[SeriesPatch]:
    """The series' patches, numbered as b4 numbers them."""
    with b4_repo(project):
        lser = _series(source, revision, lookup=False)
    return [
        SeriesPatch(n, p.subject, f'{p.fromname} <{p.fromemail}>')
        for n, p in enumerate(lser.patches)
        if n and p is not None
    ]


def _am_ready(lser: 'b4.LoreSeries', numbers: Sequence[int]) -> bytes:
    """b4's am-ready mbox of patches *numbers*: trailers from the list, a Link each."""
    msgs = lser.get_am_ready(addlink=True, cherrypick=list(numbers))
    if not msgs:
        return b''
    out = io.BytesIO()
    b4.save_git_am_mbox(msgs, out)
    return out.getvalue()


def apply_mbox(project: pathlib.Path, source: str, revision: int, numbers: Sequence[int]) -> bytes:
    """The mbox apply_series would apply (for an agent to apply by hand instead)."""
    with b4_repo(project), _captured() as log:
        try:
            ambytes = _am_ready(_series(source, revision, lookup=True), numbers)
        except Exception as ex:  # b4 reports through its log and exceptions alike.
            raise ValueError(f'b4 could not prepare v{revision}: {ex}\n{log.getvalue()}') from ex
    if not ambytes:
        raise ValueError(f'v{revision}: no patches ready to apply')
    return ambytes


def apply_series(
    project: pathlib.Path,
    branch: str,
    source: str,
    revision: int,
    numbers: Sequence[int],
    expected: str,
    record_take: bool = False,
) -> ApplyResult:
    """Patches *numbers* of the series onto *branch*, which must still be at *expected*
    and not checked out. *record_take*: remember *branch* as a take target for b4."""
    tip0 = _commit(project, f'refs/heads/{branch}')
    if tip0 is None:
        return ApplyResult(False, error=f'{branch} does not exist')
    checked_out = _checked_out(project, branch)
    if checked_out:
        return ApplyResult(False, error=f'{branch} is checked out in {checked_out}')
    if tip0 != expected:
        return ApplyResult(False, error=f'{branch} moved since it was shown')
    if not numbers:
        return ApplyResult(False, error='no patches picked')
    fetch_head = project / _git(project, 'rev-parse', '--git-path', 'FETCH_HEAD').stdout.strip()
    saved = fetch_head.read_bytes() if fetch_head.exists() else None
    with b4_repo(project), _captured() as log:
        subject = f'v{revision}'
        try:
            lser = _series(source, revision, lookup=True)
            subject = f'{lser.subject} v{revision}'
            ambytes = _am_ready(lser, numbers)
            if not ambytes:
                return ApplyResult(False, error=f'{subject}: no patches ready to apply')
            b4.git_fetch_am_into_repo(
                str(project), ambytes, at_base=tip0, am_flags=['-3'], resolve=True
            )
            tip = _git(project, 'rev-parse', 'FETCH_HEAD').stdout.strip()
        except b4.AmConflictError as ex:
            _git(project, 'worktree', 'remove', '--force', ex.worktree_path, check=False)
            shutil.rmtree(ex.worktree_path, ignore_errors=True)
            return ApplyResult(
                False,
                error=f'{subject} does not apply on {branch}',
                output=ex.output,
                conflict=True,
            )
        except Exception as ex:  # b4 reports through its log and exceptions alike.
            return ApplyResult(
                False, error=f'b4 could not apply {subject}: {ex}', output=log.getvalue()
            )
        finally:
            if saved is None:
                fetch_head.unlink(missing_ok=True)
            else:
                fetch_head.write_bytes(saved)
        applied = tuple(_git(project, 'rev-list', '--reverse', f'{tip0}..{tip}').stdout.split())
        moved = _git(
            project,
            'update-ref',
            '-m',
            f'b4-maintainer: apply {len(applied)} patches of {subject}',
            f'refs/heads/{branch}',
            tip,
            expected,
            check=False,
        )
        if moved.returncode != 0:
            return ApplyResult(False, error=f'{branch} moved meanwhile: {moved.stderr.strip()}')
        b4.mbox.thanks_record_am(lser, cherrypick=list(numbers))
        if record_take:
            common = _git(project, 'rev-parse', '--git-common-dir').stdout.strip()
            b4.review.tracking.record_take_branch(str(project / common), branch)
    return ApplyResult(True, tip, applied)

# SPDX-License-Identifier: GPL-2.0-or-later
"""Prep branches: b4's own (`b4/<name>`), made and worked on by running b4 prep.

b4 prep acts on the checked-out branch of the current directory's repository, so each
prep branch gets a worktree of the project (`var/branches/<project>/<name>`), sparse: the
top-level files, the files directly in each top-level directory, and all of `scripts/`
(enough for get_maintainer.pl's tree check and checkpatch; about 25 MB for the kernel).
The branch is checked out there only while b4 runs and detached otherwise, so commits
can be plucked onto it (a ref update) without a stale index. The project's main checkout
is never touched. The pinned b4 runs as a subprocess, exactly as on the command line.

Workaround: `b4 prep -n` refuses a detached HEAD ("not currently on a branch, unable to
determine cover strategy"), so the worktree starts on a throwaway `b4m/prep-start/<name>`
branch, deleted right after. Remove when b4 creates prep branches from a detached HEAD.
"""

import os
import pathlib
import re
import shutil
import subprocess
import sys
from typing import Optional

NAME = re.compile(r'^[a-z0-9][a-z0-9._-]{0,63}$')
START = 'b4m/prep-start'
TRACKING = '--- b4-submit-tracking ---'
ANSI = re.compile(r'\x1b\[[0-9;]*m')


class PrepError(Exception):
    pass


def b4_command() -> list[str]:
    """The pinned b4 (installed beside this interpreter), else b4 on PATH."""
    pinned = pathlib.Path(sys.executable).parent / 'b4'
    return [str(pinned)] if pinned.exists() else ['b4']


def _git(repo: pathlib.Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    run = subprocess.run(
        ['git', *args], cwd=repo, capture_output=True, text=True, stdin=subprocess.DEVNULL
    )
    if check and run.returncode != 0:
        raise PrepError(run.stderr.strip() or f'git {args[0]} failed')
    return run


def _b4(worktree: pathlib.Path, *args: str, stdin: Optional[str] = None) -> tuple[int, str]:
    run = subprocess.run(
        [*b4_command(), *args],
        cwd=worktree,
        input=stdin if stdin is not None else '',
        capture_output=True,
        text=True,
        env={**os.environ, 'NO_COLOR': '1'},
    )
    return run.returncode, ANSI.sub('', run.stdout + run.stderr).strip()


def sparse_patterns(repo: pathlib.Path, commit: str) -> str:
    """Top-level files, files directly in each top-level directory, all of scripts/."""
    dirs = _git(repo, 'ls-tree', '-d', '--name-only', commit).stdout.split()
    lines = ['/*', '!/*/']
    for name in dirs:
        lines += [f'/{name}/*', f'!/{name}/*/']
    lines.append('/scripts/')
    return '\n'.join(lines) + '\n'


def _sparse_worktree(repo: pathlib.Path, worktree: pathlib.Path, ref: str) -> None:
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, 'worktree', 'add', '--quiet', '--no-checkout', str(worktree), ref)
    patterns = sparse_patterns(repo, ref)
    run = subprocess.run(
        ['git', 'sparse-checkout', 'set', '--no-cone', '--stdin'],
        cwd=worktree,
        input=patterns,
        capture_output=True,
        text=True,
    )
    if run.returncode != 0:
        raise PrepError(run.stderr.strip() or 'git sparse-checkout failed')
    _git(worktree, 'checkout', '--quiet')


def cut_prep(
    repo: pathlib.Path, worktree: pathlib.Path, name: str, fork: str, cover: Optional[str]
) -> str:
    """A new prep branch `b4/<name>` from *fork*, made by `b4 prep -n` in a sparse worktree
    at *worktree*, its cover letter set from *cover* (subject line, blank line, body).
    On failure nothing is left: no worktree, branch, or branch configuration."""
    if not NAME.match(name):
        raise PrepError(f'{name!r}: a prep branch name is lower-case letters, digits, . _ -')
    branch, start = f'b4/{name}', f'{START}/{name}'
    if (
        _git(
            repo, 'rev-parse', '--verify', '--quiet', f'refs/heads/{branch}', check=False
        ).returncode
        == 0
    ):
        raise PrepError(f'{branch} exists')
    if os.path.lexists(worktree):
        raise PrepError(f'{worktree} exists')
    fork_sha = _git(repo, 'rev-parse', '--verify', '--quiet', f'{fork}^{{commit}}', check=False)
    if fork_sha.returncode != 0:
        raise PrepError(f'{fork} is not a commit')
    try:
        _git(repo, 'branch', '--quiet', start, fork_sha.stdout.strip())
        _sparse_worktree(repo, worktree, start)
        code, out = _b4(worktree, 'prep', '-n', name, '-f', fork_sha.stdout.strip())
        if code != 0:
            raise PrepError(f'b4 prep -n failed: {out}')
        _git(repo, 'branch', '--quiet', '-D', start)
        if cover:
            code, out = _b4(worktree, 'prep', '--cover-from-file', '-', stdin=cover)
            if code != 0:
                raise PrepError(f'b4 prep --cover-from-file failed: {out}')
        _git(worktree, 'switch', '--quiet', '--detach')
    except BaseException:
        remove_prep(repo, worktree, branch)
        _git(repo, 'branch', '--quiet', '-D', start, check=False)
        raise
    return branch


def remove_prep(repo: pathlib.Path, worktree: pathlib.Path, branch: str) -> None:
    """Undo a prep branch made here: its worktree, the branch, b4's branch settings."""
    _git(repo, 'worktree', 'remove', '--force', str(worktree), check=False)
    shutil.rmtree(worktree, ignore_errors=True)
    _git(repo, 'worktree', 'prune', check=False)
    _git(repo, 'branch', '--quiet', '-D', branch, check=False)
    _git(repo, 'config', '--remove-section', f'branch.{branch}', check=False)


def ensure_worktree(repo: pathlib.Path, worktree: pathlib.Path, branch: str) -> None:
    """A sparse, detached worktree for an existing prep branch (made elsewhere)."""
    if (worktree / '.git').exists():
        return
    _sparse_worktree(repo, worktree, branch)
    _git(worktree, 'switch', '--quiet', '--detach')


def run_b4(
    worktree: pathlib.Path, branch: str, *args: str, interactive: bool = False
) -> tuple[int, str]:
    """Run b4 with *args* on *branch*, checked out in *worktree* only meanwhile;
    *interactive* runs it on the terminal (an editor), output not captured."""
    switched = _git(worktree, 'switch', '--quiet', branch, check=False)
    if switched.returncode != 0:
        why = switched.stderr.strip()
        if 'already used by worktree' in why or 'checked out' in why:
            why = f'{branch} is checked out in another worktree; run b4 there ({why})'
        return switched.returncode, why
    try:
        if interactive:
            return subprocess.run([*b4_command(), *args], cwd=worktree).returncode, ''
        return _b4(worktree, *args)
    finally:
        _git(worktree, 'switch', '--quiet', '--detach', check=False)


def cover_text(repo: pathlib.Path, branch: str) -> str:
    """The prep branch's cover letter, from b4's cover commit (without its tracking)."""
    out = _git(
        repo, 'log', '--format=%x1e%B', f'--grep={TRACKING}', '--fixed-strings', '-n1', branch
    )
    text = out.stdout.lstrip('\x1e')
    return text.split(TRACKING, 1)[0].strip()

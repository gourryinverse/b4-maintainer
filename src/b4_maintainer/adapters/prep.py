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

import email
import email.policy
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Optional

from .bases import PluckResult, _replay  # The same replay as plucking.

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


@dataclass(frozen=True)
class SignResult:
    ok: bool
    tip: str = ''
    signed: int = 0  # Commits that got the sign-off.
    error: str = ''


def _cover_commit(repo: pathlib.Path, branch: str) -> str:
    out = _git(repo, 'log', '--format=%H', f'--grep={TRACKING}', '--fixed-strings', '-n1', branch)
    return out.stdout.strip()


def sign_off(repo: pathlib.Path, branch: str, expected: str, name: str, email: str) -> SignResult:
    """Add `Signed-off-by: name <email>` to each commit after b4's cover commit that lacks
    it (the maintainer certifying the DCO). Trees and authors are kept; the cover commit
    is not touched. No worktree: commits are rewritten with git commit-tree and the
    branch moves in one update-ref guarded by *expected*; refused while checked out."""
    tip = _git(repo, 'rev-parse', '--verify', '--quiet', f'refs/heads/{branch}', check=False)
    if tip.returncode != 0:
        return SignResult(False, error=f'{branch} does not exist')
    if tip.stdout.strip() != expected:
        return SignResult(False, error=f'{branch} moved since it was shown')
    listed = _git(repo, 'worktree', 'list', '--porcelain').stdout
    if f'branch refs/heads/{branch}\n' in listed + '\n':
        return SignResult(False, error=f'{branch} is checked out; finish there first')
    cover = _cover_commit(repo, branch)
    if not cover:
        return SignResult(False, error=f'{branch} has no b4 cover commit')
    line = f'Signed-off-by: {name} <{email}>'
    shas = _git(repo, 'rev-list', '--reverse', f'{cover}..{branch}').stdout.split()
    parent, signed = cover, 0
    for sha in shas:
        message = _git(repo, 'log', '-1', '--format=%B', sha).stdout
        fields = _git(repo, 'log', '-1', '--date=raw', '--format=%an%x00%ae%x00%ad%x00%P', sha)
        author, author_email, date, parents = fields.stdout.rstrip('\n').split('\x00')
        if line in message.splitlines() and parents == parent:
            parent = sha  # Signed already, on an unchanged parent: kept as it is.
            continue
        if line not in message.splitlines():
            run = subprocess.run(
                ['git', 'interpret-trailers', '--trailer', line],
                cwd=repo,
                input=message.rstrip() + '\n',
                capture_output=True,
                text=True,
            )
            if run.returncode != 0:
                return SignResult(False, error=run.stderr.strip())
            message, signed = run.stdout, signed + 1
        tree = _git(repo, 'rev-parse', f'{sha}^{{tree}}').stdout.strip()
        env = {
            **os.environ,
            'GIT_AUTHOR_NAME': author,
            'GIT_AUTHOR_EMAIL': author_email,
            'GIT_AUTHOR_DATE': date,
        }
        made = subprocess.run(
            ['git', 'commit-tree', tree, '-p', parent],
            cwd=repo,
            input=message,
            capture_output=True,
            text=True,
            env=env,
        )
        if made.returncode != 0:
            return SignResult(False, error=made.stderr.strip())
        parent = made.stdout.strip()
    if parent != expected:
        moved = _git(
            repo,
            'update-ref',
            '-m',
            'b4-maintainer: sign off',
            f'refs/heads/{branch}',
            parent,
            expected,
            check=False,
        )
        if moved.returncode != 0:
            return SignResult(False, error=f'{branch} moved meanwhile: {moved.stderr.strip()}')
    return SignResult(True, parent, signed)


def preview_send(
    worktree: pathlib.Path, branch: str, out: pathlib.Path, no_sign: bool = False
) -> tuple[int, str, list[pathlib.Path]]:
    """What b4 send would send, without sending: `b4 send -o <out>` writes each message.
    *out* is emptied first; it must not be inside the worktree."""
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    code, text = run_b4(
        worktree, branch, 'send', '-o', str(out), *(['--no-sign'] if no_sign else [])
    )
    return code, text, sorted(p for p in out.iterdir() if p.is_file())


def outgoing_summary(messages: list[pathlib.Path]) -> str:
    """Each message's subject and recipients; the first message (the cover letter) whole."""
    parts = []
    for n, path in enumerate(messages):
        msg = email.message_from_bytes(path.read_bytes(), policy=email.policy.default)
        head = [
            f'Subject: {msg["subject"]}',
            f'To: {msg.get("to", "")}',
            f'Cc: {msg.get("cc", "")}',
        ]
        if n == 0:
            body = msg.get_body(preferencelist=('plain',))
            head += ['', body.get_content() if body is not None else '']
        parts.append('\n'.join(head))
    return '\n\n---\n\n'.join(parts)


def prep_tracking(repo: pathlib.Path, branch: str) -> dict[str, Any]:
    """b4's tracking of a prep branch (read from its cover commit): the revision being
    prepared and the revisions already sent."""
    cover = _cover_commit(repo, branch)
    if not cover:
        return {'revision': 0, 'sent': ()}
    message = _git(repo, 'log', '-1', '--format=%B', cover).stdout
    data = message.split(TRACKING, 1)[1] if TRACKING in message else ''
    lines = [line for line in data.splitlines() if not line.startswith('#')]
    try:
        series = json.loads('\n'.join(lines)).get('series', {})
    except ValueError:
        return {'revision': 0, 'sent': ()}
    history = series.get('history', {}) or {}
    return {
        'revision': int(series.get('revision', 0) or 0),
        'sent': tuple(sorted(history, key=lambda v: int(str(v).lstrip('v') or 0))),
        'change-id': series.get('change-id', ''),
    }


def prep_fork(repo: pathlib.Path, branch: str) -> str:
    """Where the prep branch forks: the parent of b4's cover commit."""
    cover = _cover_commit(repo, branch)
    if not cover:
        return ''
    return _git(repo, 'rev-parse', f'{cover}^', check=False).stdout.strip()


def move_prep(repo: pathlib.Path, branch: str, onto: str, expected: str) -> PluckResult:
    """Move a prep branch onto *onto* (e.g. its rebased maintainer base): b4's cover
    commit recreated there with its message (and tracking) intact, then each commit
    replayed; a change *onto* already has is dropped. A conflict, a moved branch, or a
    checked-out one changes nothing. No worktree; one guarded update-ref."""
    tip = _git(repo, 'rev-parse', '--verify', '--quiet', f'refs/heads/{branch}', check=False)
    if tip.returncode != 0:
        return PluckResult(False, error=f'{branch} does not exist')
    if tip.stdout.strip() != expected:
        return PluckResult(False, error=f'{branch} moved since it was shown')
    if (
        f'branch refs/heads/{branch}\n'
        in _git(repo, 'worktree', 'list', '--porcelain').stdout + '\n'
    ):
        return PluckResult(False, error=f'{branch} is checked out')
    cover = _cover_commit(repo, branch)
    if not cover:
        return PluckResult(False, error=f'{branch} has no b4 cover commit')
    fields = _git(repo, 'log', '-1', '--date=raw', '--format=%an%x00%ae%x00%ad', cover).stdout
    name, author_email, date = fields.rstrip('\n').split('\x00')
    message = _git(repo, 'log', '-1', '--format=%B', cover).stdout
    tree = _git(repo, 'rev-parse', f'{onto}^{{tree}}').stdout.strip()
    made = subprocess.run(
        ['git', 'commit-tree', tree, '-p', onto],
        cwd=repo,
        input=message,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            'GIT_AUTHOR_NAME': name,
            'GIT_AUTHOR_EMAIL': author_email,
            'GIT_AUTHOR_DATE': date,
        },
    )
    if made.returncode != 0:
        return PluckResult(False, error=made.stderr.strip())
    shas = _git(repo, 'rev-list', '--reverse', f'{cover}..{branch}').stdout.split()
    replayed = _replay(repo, made.stdout.strip(), shas)
    if not replayed.ok:
        return replayed
    moved = _git(
        repo,
        'update-ref',
        '-m',
        f'b4-maintainer: move onto {onto[:12]}',
        f'refs/heads/{branch}',
        replayed.tip,
        expected,
        check=False,
    )
    if moved.returncode != 0:
        return PluckResult(False, error=f'{branch} moved meanwhile: {moved.stderr.strip()}')
    return replayed


def cleanup_prep(repo: pathlib.Path, worktree: pathlib.Path, branch: str) -> tuple[int, str]:
    """Remove a prep branch the b4 way: `b4 prep --cleanup` archives it (cover, tracking,
    patches) in b4's data directory and deletes the branch and its sent/ tags (its prompt
    answered yes: the maintainer confirmed in the app). b4 refuses a branch with only a
    cover letter; that one is deleted directly. Its worktree and outgoing preview go too."""
    try:
        ensure_worktree(repo, worktree, branch)
        code, out = _b4(worktree, 'prep', '--cleanup', branch, stdin='y\n')
    except PrepError as ex:
        code, out = 1, str(ex)
    exists = _git(repo, 'rev-parse', '--verify', '--quiet', f'refs/heads/{branch}', check=False)
    if exists.returncode == 0 and 'appears to be an empty branch' not in out:
        return code or 1, out
    remove_prep(repo, worktree, branch)
    shutil.rmtree(worktree.with_name(f'{worktree.name}.outgoing'), ignore_errors=True)
    return 0, out

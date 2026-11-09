# SPDX-License-Identifier: GPL-2.0-or-later
"""A project checkout's upstream remotes, as Settings tracks and untracks branches.

Each remote fetches only the branches tracked from it (one fetch line each), without
tags, with the checkout's partial-clone filter. Only the checkout's configuration and
remote-tracking refs change: never its HEAD, index, or worktree. Fetches and lookups
reach the network; callers run them off the UI thread.
"""

import pathlib
import subprocess

FETCH_TIMEOUT = 600


class RemoteError(RuntimeError):
    pass


def _git(checkout: pathlib.Path, *args: str, timeout: int = 60) -> str:
    try:
        run = subprocess.run(
            ['git', *args],
            cwd=checkout,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as ex:
        raise RemoteError(f'git {args[0]}: {ex}') from ex
    if run.returncode != 0:
        raise RemoteError(f'git {" ".join(args[:2])}: {run.stderr.strip() or run.returncode}')
    return run.stdout


def _config(checkout: pathlib.Path, *args: str) -> list[str]:
    try:
        return _git(checkout, 'config', *args).splitlines()
    except RemoteError:
        return []  # Unset.


def remotes(checkout: pathlib.Path) -> dict[str, str]:
    """Each remote's name and URL."""
    found = {}
    for line in _config(checkout, '--get-regexp', r'^remote\..*\.url$'):
        key, _, url = line.partition(' ')
        found[key.removeprefix('remote.').removesuffix('.url')] = url
    return found


def _refspec(remote: str, branch: str) -> str:
    return f'+refs/heads/{branch}:refs/remotes/{remote}/{branch}'


def tracked(checkout: pathlib.Path, remote: str) -> list[str]:
    """The branches *remote* fetches, in its fetch lines' order."""
    prefix, middle = '+refs/heads/', f':refs/remotes/{remote}/'
    return [
        spec[len(prefix) :].split(':', 1)[0]
        for spec in _config(checkout, '--get-all', f'remote.{remote}.fetch')
        if spec.startswith(prefix) and middle in spec
    ]


def heads(checkout: pathlib.Path, where: str) -> list[str]:
    """The branches at *where* (a remote's name or a URL), sorted."""
    out = _git(checkout, 'ls-remote', '--heads', where, timeout=FETCH_TIMEOUT)
    return sorted(
        line.split('refs/heads/', 1)[1] for line in out.splitlines() if 'refs/heads/' in line
    )


def _filter(checkout: pathlib.Path) -> str:
    """The partial-clone filter the checkout's remotes use ('' when none)."""
    for line in _config(checkout, '--get-regexp', r'^remote\..*\.partialclonefilter$'):
        return line.partition(' ')[2]
    return ''


def track(checkout: pathlib.Path, remote: str, url: str, branch: str) -> None:
    """Fetch *branch* from *remote* (added at *url* when missing) into remote/branch."""
    if remote not in remotes(checkout):
        _git(checkout, 'remote', 'add', '--no-tags', '-t', branch, remote, url)
        wanted = _filter(checkout)
        if wanted:
            _git(checkout, 'config', f'remote.{remote}.promisor', 'true')
            _git(checkout, 'config', f'remote.{remote}.partialclonefilter', wanted)
    elif branch not in tracked(checkout, remote):
        _git(checkout, 'config', '--add', f'remote.{remote}.fetch', _refspec(remote, branch))
    _git(checkout, 'fetch', '-q', remote, timeout=FETCH_TIMEOUT)


def untrack(checkout: pathlib.Path, remote: str, branch: str) -> bool:
    """Stop fetching *branch*; remove *remote* once it fetches nothing. Whether it went."""
    if remote not in remotes(checkout):
        return False
    _config(
        checkout, '--fixed-value', '--unset-all', f'remote.{remote}.fetch', _refspec(remote, branch)
    )
    try:
        _git(checkout, 'update-ref', '-d', f'refs/remotes/{remote}/{branch}')
    except RemoteError:
        pass  # Never fetched.
    if tracked(checkout, remote) or _config(checkout, '--get-all', f'remote.{remote}.fetch'):
        return False
    _git(checkout, 'remote', 'remove', remote)
    return True

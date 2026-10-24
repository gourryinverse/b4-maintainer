# SPDX-License-Identifier: GPL-2.0-or-later
"""`b4-maintainer sandbox <id>`: run one of a session's shell commands, confined.

`unshare -rn` gives the command its own user and network namespaces (no network; it
also works inside launcher jails that hold capabilities without setuid); bubblewrap gives
it a filesystem of the system directories (read-only), the session's readable
directories (read-only), and its writable ones, with a private /tmp and a cleared
environment, so nothing else on the machine, and no credential in the agent's
environment, is visible.

The rules come from the session's launch.json, written by the client where the agent
cannot write, never from the caller's environment. Kept free of UI imports: it runs for
every shell command.
"""

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Mapping, Optional

from .store import SessionStore

SYSTEM = ('/usr', '/etc', '/bin', '/sbin', '/lib', '/lib64')
PATH = '/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin'
KVM = '/dev/kvm'


class SandboxError(Exception):
    pass


@dataclass(frozen=True)
class Policy:
    readable: tuple[str, ...]
    writable: tuple[str, ...]
    cwd: str
    kvm: bool
    env: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Availability:
    ok: bool
    reason: str = ''

    def __bool__(self) -> bool:
        return self.ok


def policy_for(store: SessionStore, sid: str) -> Policy:
    """The session's rules, from its launch.json."""
    try:
        launch = json.loads((store.directory(sid) / 'launch.json').read_text())
        env = launch['env']
        cwd = str(launch['cwd'])
    except (OSError, ValueError, KeyError, TypeError) as ex:
        raise SandboxError(f'no launched session {sid!r}: {ex}') from ex
    execute = env.get('B4M_PERMISSIONS') == 'execute'
    readable = [r for r in str(env.get('B4M_READABLE', '')).split('\n') if r]
    writable = [str(env.get('B4M_OUT', ''))]
    if execute:
        writable.append(str(env.get('B4M_WORKTREE', '')))
    extra = {k: str(env[k]) for k in ('GIT_CEILING_DIRECTORIES',) if k in env}
    return Policy(tuple(readable), tuple(w for w in writable if w), cwd, execute, extra)


def bwrap_argv(policy: Policy, command: str) -> list[str]:
    argv = ['unshare', '-rn', '--', 'bwrap', '--die-with-parent', '--new-session']
    argv += ['--unshare-pid', '--clearenv']
    for path in SYSTEM:
        if os.path.exists(path):
            argv += ['--ro-bind', path, path]
    argv += ['--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp']
    if policy.kvm and os.path.exists(KVM):
        argv += ['--dev-bind', KVM, KVM]
    for path in policy.readable:
        if os.path.exists(path):
            argv += ['--ro-bind', path, path]
    for path in policy.writable:  # Later binds win: out/ is writable inside work/.
        if os.path.exists(path):
            argv += ['--bind', path, path]
    env = {'PATH': PATH, 'HOME': '/tmp', 'LANG': os.environ.get('LANG', 'C.UTF-8')}
    env |= {'TERM': os.environ.get('TERM', 'dumb'), **policy.env}
    for key, value in env.items():
        argv += ['--setenv', key, value]
    argv += ['--chdir', policy.cwd, '--', 'bash', '-c', command]
    return argv


_available: Optional[Availability] = None


def available() -> Availability:
    """Whether sandboxed commands can run on this machine (checked once)."""
    global _available
    if _available is not None:
        return _available
    missing = [tool for tool in ('unshare', 'bwrap', 'bash') if shutil.which(tool) is None]
    if missing:
        _available = Availability(False, f'{", ".join(missing)} not installed')
        return _available
    probe = ['unshare', '-rn', '--', 'bwrap', '--ro-bind', '/', '/', '--dev', '/dev']
    probe += ['--proc', '/proc', 'true']
    try:
        run = subprocess.run(probe, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as ex:
        _available = Availability(False, str(ex))
        return _available
    reason = (run.stderr.strip().splitlines() or ['failed'])[-1] if run.returncode else ''
    _available = Availability(run.returncode == 0, reason)
    return _available


def exec_sandboxed(store: SessionStore, sid: str, command: str) -> None:
    """Replace this process with the confined command (its exit status is the result)."""
    argv = bwrap_argv(policy_for(store, sid), command)
    os.execvp(argv[0], argv)

"""Contract: a session's shell commands run where they see only the session, with no network."""

import json
import os
import pathlib
import subprocess
import sys

import pytest
from sessiondata import make_record

from b4_maintainer.sessions.pi import LaunchSpec, Site, pi_command, prepare_session
from b4_maintainer.sessions.profiles import Request
from b4_maintainer.sessions.records import Worktree
from b4_maintainer.sessions.sandbox import Policy, available, bwrap_argv, policy_for
from b4_maintainer.sessions.store import SessionStore

REQUEST = Request('Review the series.', 'You assist a maintainer.', (), 'lightweight', ())
HOOK = str(pathlib.Path(sys.executable).parent / 'b4-maintainer')


def session(tmp_path, permissions='read-only', worktree=None):
    """A launched session's files (launch.json included), as the runner writes them."""
    store = SessionStore(tmp_path / 'var' / 'sessions')
    record = make_record('s1', permissions=permissions)
    if worktree is not None:
        record = record.replace(worktree=Worktree(str(worktree), 'b4m/session/s1', 'v1', 'a'))
    store.create(record)
    prepare_session(store, record, REQUEST, source='')
    checkout = tmp_path / 'checkout'
    checkout.mkdir(exist_ok=True)
    (checkout / 'README').write_text('synthetic checkout\n')
    site = Site(tmp_path, HOOK, 'pi', checkouts={'linux': str(checkout)})
    cmd = pi_command(store, record, LaunchSpec(REQUEST), site)
    launch = {'argv': cmd.argv, 'resume': cmd.argv, 'cwd': cmd.cwd, 'env': cmd.env}
    (store.directory('s1') / 'launch.json').write_text(json.dumps(launch))
    return store, cmd


def test_the_policy_comes_from_the_sessions_launch_record(tmp_path):
    store, cmd = session(tmp_path)
    policy = policy_for(store, 's1')
    work = store.directory('s1') / 'work'
    assert policy.readable == (str(work), str(tmp_path / 'checkout'))
    assert policy.writable == (str(work / 'out'),)
    assert policy.cwd == str(work) and policy.kvm is False


def test_execute_sessions_also_write_their_worktree_and_use_kvm(tmp_path):
    worktree = tmp_path / 'wt'
    worktree.mkdir()
    store, _cmd = session(tmp_path, 'execute', worktree)
    policy = policy_for(store, 's1')
    assert str(worktree) in policy.writable and policy.kvm is True


def test_the_bwrap_command_binds_only_what_the_policy_names(tmp_path):
    w = tmp_path / 'w'
    for path in ('work/out', 'linux'):
        (w / path).mkdir(parents=True)
    policy = Policy(
        readable=(f'{w}/work', f'{w}/linux', f'{w}/missing'),
        writable=(f'{w}/work/out',),
        cwd=f'{w}/work',
        kvm=False,
        env={'GIT_CEILING_DIRECTORIES': str(w)},
    )
    argv = bwrap_argv(policy, 'make -j8')
    assert argv[:3] == ['unshare', '-rn', '--']  # User and network namespaces.
    assert argv[3] == 'bwrap' and '--clearenv' in argv and '--unshare-pid' in argv
    joined = ' '.join(argv)
    assert '--ro-bind /usr /usr' in joined and f'--ro-bind {w}/linux {w}/linux' in joined
    assert f'--bind {w}/work/out {w}/work/out' in joined
    assert 'missing' not in joined  # bwrap refuses to bind what does not exist.
    assert '/dev/kvm' not in joined and '--tmpfs /tmp' in joined
    assert f'--setenv GIT_CEILING_DIRECTORIES {w}' in joined and f'--chdir {w}/work' in joined
    assert argv[-4:] == ['--', 'bash', '-c', 'make -j8']


usable = available()
real = pytest.mark.skipif(not usable, reason=f'sandbox not usable here: {usable.reason}')


def run(store, command, tmp_path, extra_env=None):
    env = {**os.environ, 'B4M_SANDBOX_COMMAND': command, **(extra_env or {})}
    return subprocess.run(
        [HOOK, 'sandbox', '-C', str(tmp_path), 's1'],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@real
def test_commands_see_the_session_and_nothing_else(tmp_path, home):
    store, _cmd = session(tmp_path)
    (home / 'secret').write_text('do not read\n')
    out = store.directory('s1') / 'work' / 'out'
    script = (
        'wc -l context/task.md; cat ../../../../checkout/README; '
        f'cat {home}/secret || echo no-home; '
        'echo made > out/notes.md && echo out-ok; '
        f'touch {store.directory("s1")}/x; '  # Only the sandbox's throwaway root sees it.
        'touch /usr/x 2>/dev/null || echo system-ro; '
        'timeout 3 bash -c "exec 3<>/dev/tcp/1.1.1.1/443" 2>/dev/null || echo no-net; '
        'echo "secret=${B4M_TEST_SECRET:-unset}"; exit 7'
    )
    result = run(store, script, tmp_path, {'B4M_TEST_SECRET': 'token'})
    assert result.returncode == 7, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0].startswith('1 ') and 'synthetic checkout' in lines
    for word in ('no-home', 'out-ok', 'system-ro', 'no-net', 'secret=unset'):
        assert word in lines, (word, result.stdout, result.stderr)
    assert (out / 'notes.md').read_text() == 'made\n'
    assert not (store.directory('s1') / 'x').exists()


@real
def test_a_missing_session_runs_nothing(tmp_path, home):
    store, _cmd = session(tmp_path)
    result = subprocess.run(
        [HOOK, 'sandbox', '-C', str(tmp_path), 'nope'],
        env={**os.environ, 'B4M_SANDBOX_COMMAND': 'echo ran'},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0 and 'ran' not in result.stdout

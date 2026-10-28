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

REQUEST = Request('Review the series.', 'You assist a maintainer.', 'lightweight', ())
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


def test_sandboxed_commands_run_at_low_cpu_and_io_priority(tmp_path):
    store, cmd = session(tmp_path)
    assert (cmd.env['B4M_NICE'], cmd.env['B4M_IONICE']) == ('10', 'low')  # Defaults.
    policy = policy_for(store, 's1')
    assert (policy.nice, policy.ionice) == (10, 'low')
    argv = bwrap_argv(policy, 'make')
    assert argv[:6] == ['nice', '-n', '10', 'ionice', '-c2', '-n7']
    assert argv[6:9] == ['unshare', '-rn', '--']
    idle = bwrap_argv(Policy((), (), '/', False, nice=5, ionice='idle'), 'make')
    assert idle[:5] == ['nice', '-n', '5', 'ionice', '-c3']
    off = bwrap_argv(Policy((), (), '/', False, nice=0, ionice='off'), 'make')
    assert off[:3] == ['unshare', '-rn', '--']


def test_a_launch_record_without_priorities_gets_the_defaults(tmp_path):
    store, _cmd = session(tmp_path)
    path = store.directory('s1') / 'launch.json'
    launch = json.loads(path.read_text())
    del launch['env']['B4M_NICE'], launch['env']['B4M_IONICE']
    path.write_text(json.dumps(launch))
    policy = policy_for(store, 's1')
    assert (policy.nice, policy.ionice) == (10, 'low')


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
    argv = argv[argv.index('unshare') :]  # After the priority prefix.
    assert argv[:3] == ['unshare', '-rn', '--']  # User and network namespaces.
    assert argv[3] == 'bwrap' and '--clearenv' in argv and '--unshare-pid' in argv
    joined = ' '.join(argv)
    assert '--ro-bind /usr /usr' in joined and f'--ro-bind {w}/linux {w}/linux' in joined
    assert f'--bind {w}/work/out {w}/work/out' in joined
    assert 'missing' not in joined  # bwrap refuses to bind what does not exist.
    assert '/dev/kvm' not in joined and '--tmpfs /tmp' in joined
    assert '--dir /run' in joined and '--dir /sys' in joined  # A vng guest mounts on these.
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
        'echo "secret=${B4M_TEST_SECRET:-unset}"; echo "nice=$(nice)"; exit 7'
    )
    result = run(store, script, tmp_path, {'B4M_TEST_SECRET': 'token'})
    assert result.returncode == 7, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0].startswith('1 ') and 'synthetic checkout' in lines
    lowered = f'nice={min(os.nice(0) + 10, 19)}'  # Ten below whatever started it.
    for word in ('no-home', 'out-ok', 'system-ro', 'no-net', 'secret=unset', lowered):
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


def test_vngs_virtiofsd_is_the_sandboxs_shim_first(tmp_path):
    from b4_maintainer.sessions.sandbox import SHIMS

    argv = bwrap_argv(Policy((), (), str(tmp_path), kvm=True), 'vng')
    joined = ' '.join(argv)
    assert f'--ro-bind {SHIMS} {SHIMS}' in joined
    path = argv[argv.index('PATH', argv.index('--setenv')) + 1]
    assert path.split(':')[0] == str(SHIMS) and (SHIMS / 'virtiofsd').is_file()


FAKE_VIRTIOFSD = """#!/bin/sh
[ "$1" = --help ] && { echo '      --translate-uid <TRANSLATE_UID>'; exit 0; }
echo "args: $*"
"""


@real
def test_the_shim_shows_root_owned_files_as_roots_in_the_guest(tmp_path):
    """In a namespace that maps one uid to root (as the sandbox's), the files root owns
    appear as the overflow ids: the shim has virtiofsd show them as root's."""
    from b4_maintainer.sessions.sandbox import SHIMS

    fake = tmp_path / 'bin'
    fake.mkdir()
    (fake / 'virtiofsd').write_text(FAKE_VIRTIOFSD)
    (fake / 'virtiofsd').chmod(0o755)
    path = f'{SHIMS}:{fake}:/usr/bin:/bin'

    def shim(*args):
        command = ['unshare', '-r', 'env', f'PATH={path}', 'virtiofsd', *args]
        return subprocess.run(command, capture_output=True, text=True, timeout=20).stdout

    overflow = pathlib.Path('/proc/sys/kernel/overflowuid').read_text().strip()
    out = shim('--shared-dir', '/', '--sandbox', 'none')
    assert out.strip() == (
        f'args: --translate-gid host:{overflow}:0:1 --translate-uid host:{overflow}:0:1 '
        '--shared-dir / --sandbox none'
    )
    assert shim('--version').strip() == 'args: --version'  # Only a daemon run.
    given = shim('--translate-uid', 'host:1:0:1', '--shared-dir', '/')
    assert given.strip() == 'args: --translate-uid host:1:0:1 --shared-dir /'

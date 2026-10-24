"""Contract: the b4-maintainer pi extension reports state and enforces the session's limits.

Runs real pi (print mode, offline) with the extension's scripted dry-run model: no model
or provider is called. Skipped where pi is not installed.
"""

import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

import pytest
from sessiondata import FakeSessionRunner, make_record

from b4_maintainer.sessions.models import DRY_RUN_MODEL
from b4_maintainer.sessions.pi import LaunchSpec, Site, pi_command, prepare_session
from b4_maintainer.sessions.profiles import Request
from b4_maintainer.sessions.store import SessionStore

PI = shutil.which('pi')
pytestmark = pytest.mark.skipif(PI is None, reason='pi is not installed')
HOOK = str(pathlib.Path(sys.executable).parent / 'b4-maintainer')
REQUEST = Request('Review the series.', 'You assist a maintainer.', (), 'lightweight', ())


def run(tmp_path, steps=None, permissions='read-only', worktree=None):
    """One print-mode pi run; returns the session's events and pi's transcript."""
    store = SessionStore(tmp_path / 'var' / 'sessions')
    record = make_record('x1', model=DRY_RUN_MODEL, permissions=permissions)
    if worktree is not None:
        from b4_maintainer.sessions.records import Worktree

        record = record.replace(worktree=Worktree(str(worktree), 'b4m/session/x1', 'v1', 'a'))
    FakeSessionRunner(store).add(record)
    prepare_session(store, record, REQUEST, source='')
    checkout = tmp_path / 'checkout'
    checkout.mkdir(exist_ok=True)
    (checkout / 'README').write_text('synthetic checkout\n')
    site = Site(tmp_path, HOOK, PI, checkouts={'linux': str(checkout)})
    cmd = pi_command(store, record, LaunchSpec(REQUEST), site)
    argv = [cmd.argv[0], '-p', '--offline', *cmd.argv[1:]]
    env = {**os.environ, **cmd.env}
    if steps is not None:
        env['B4M_DRY_RUN_STEPS'] = json.dumps(steps)
    directory = store.directory('x1')
    log = directory / 'events.jsonl'
    # pi can linger for many seconds after its session ends; the exit event is enough.
    with subprocess.Popen(
        argv,
        cwd=cmd.cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as proc:
        deadline = time.monotonic() + 120
        while proc.poll() is None and time.monotonic() < deadline:
            if log.exists() and '"exit"' in log.read_text():
                time.sleep(0.2)  # Let stdout drain.
                proc.terminate()
                break
            time.sleep(0.1)
        stdout, stderr = proc.communicate(timeout=30)
    events = [json.loads(line) for line in log.read_text().splitlines()]
    assert events and events[-1]['event'] == 'exit', stderr[-2000:]
    transcript = ''.join(p.read_text() for p in (directory / 'pi').rglob('*.jsonl'))
    return events, transcript, stdout


def names(events):
    return [e['event'] for e in events]


def test_the_default_script_reports_each_state_and_shows_a_refusal(tmp_path, home):
    events, transcript, stdout = run(tmp_path)
    seen = names(events)
    assert seen[0] == 'start' and 'working' in seen and seen[-2:] == ['waiting', 'exit']
    tools = [e.get('tool') for e in events if e['event'] == 'tool']
    assert tools[:3] == ['read', 'bash', 'bash']
    assert seen.count('tool_done') == len(tools) - 1  # The refused build never ran.
    assert 'only read-only git commands' in transcript  # make -j8 was refused.
    assert 'Dry run finished' in stdout


def test_reads_outside_the_session_are_refused(tmp_path, home):
    secret = home / 'secret.txt'
    secret.write_text('do not read\n')
    steps = [
        {'tool': 'read', 'args': {'path': str(tmp_path / 'checkout' / 'README')}},
        {'tool': 'read', 'args': {'path': str(secret)}},
        {'tool': 'ls', 'args': {'path': '/'}},
    ]
    _events, transcript, _stdout = run(tmp_path, steps)
    assert 'synthetic checkout' in transcript
    assert 'do not read' not in transcript
    assert transcript.count('outside the session') == 2


def test_proposals_write_only_to_out_and_the_worktree(tmp_path, home):
    worktree = tmp_path / 'wt'
    worktree.mkdir()
    out = tmp_path / 'var' / 'sessions' / 'x1' / 'work' / 'out'
    steps = [
        {'tool': 'write', 'args': {'path': str(out / 'notes.md'), 'content': 'ok\n'}},
        {'tool': 'write', 'args': {'path': str(worktree / 'fix.c'), 'content': 'ok\n'}},
        {'tool': 'write', 'args': {'path': str(home / 'evil'), 'content': 'no\n'}},
    ]
    _events, transcript, _stdout = run(tmp_path, steps, 'proposals', worktree)
    assert (out / 'notes.md').read_text() == 'ok\n'
    assert (worktree / 'fix.c').read_text() == 'ok\n'
    assert not (home / 'evil').exists()
    assert 'may write only' in transcript


def test_read_only_sessions_write_only_their_artifacts(tmp_path, home):
    worktree = tmp_path / 'wt'
    worktree.mkdir()
    out = tmp_path / 'var' / 'sessions' / 'x1' / 'work' / 'out'
    steps = [
        {'tool': 'write', 'args': {'path': str(out / 'quality.md'), 'content': 'ok\n'}},
        {'tool': 'write', 'args': {'path': str(worktree / 'fix.c'), 'content': 'no\n'}},
        {'tool': 'bash', 'args': {'command': 'echo test-write'}},
    ]
    _events, transcript, _stdout = run(tmp_path, steps, 'read-only', worktree)
    assert (out / 'quality.md').read_text() == 'ok\n'
    assert not (worktree / 'fix.c').exists()
    assert f'this session may write only under {out}' in transcript
    # The refusal says what the session can do instead.
    assert 'only read-only git commands' in transcript and 'write tool' in transcript


def test_read_only_git_runs_and_other_commands_are_refused(tmp_path, home):
    steps = [
        {'tool': 'bash', 'args': {'command': 'git --version'}},
        {'tool': 'bash', 'args': {'command': 'echo hi > /tmp/x'}},
    ]
    events, transcript, _stdout = run(tmp_path, steps)
    assert 'not a read-only git command' in transcript  # git --version: not on the list.
    assert 'shell control is not allowed' in transcript
    assert [e['tool'] for e in events if e['event'] == 'tool'] == ['bash', 'bash']


@pytest.mark.skipif(shutil.which('tmux') is None, reason='tmux not installed')
def test_a_tmux_session_with_real_pi_reaches_waiting_and_stops(tmp_path, home):
    import uuid

    from b4_maintainer.sessions.tmuxrunner import TmuxRunner

    socket = f'b4m-test-{uuid.uuid4().hex[:8]}'
    base = ['tmux', '-L', socket, '-f', '/dev/null']
    subprocess.run([*base, 'new-session', '-d', '-x', '200', '-y', '50'], check=True)
    try:
        store = SessionStore(tmp_path / 'var' / 'sessions')
        record = make_record('t1', model=DRY_RUN_MODEL)
        store.create(record)
        prepare_session(store, record, REQUEST, source='')
        app_pane = subprocess.run(
            [*base, 'display-message', '-p', '#{pane_id}'], capture_output=True, text=True
        ).stdout.strip()
        site = Site(tmp_path, HOOK, PI, args=('--offline',))
        runner = TmuxRunner(store, site, socket=socket, environ={'TMUX_PANE': app_pane})
        runner.launch(record, LaunchSpec(REQUEST))
        log = store.directory('t1') / 'events.jsonl'

        def state():
            return [v.state for v in store.poll(runner.alive)]

        def settled():  # The script ran to its end: a waiting event, not just start.
            return log.exists() and '"event":"waiting"' in log.read_text()

        deadline = time.monotonic() + 60
        while not settled() and time.monotonic() < deadline:
            time.sleep(0.2)
        assert settled() and state() == ['waiting'], log.read_text() if log.exists() else ''

        def active():
            return subprocess.run(
                [*base, 'display-message', '-p', '#{@b4m_session}'], capture_output=True, text=True
            ).stdout.strip()

        runner.switch(record)
        assert active() == 't1'
        window = runner._windows()['t1']
        subprocess.run([*base, 'send-keys', '-t', window, '-l', '/back'], check=True)
        subprocess.run([*base, 'send-keys', '-t', window, 'Enter'], check=True)
        deadline = time.monotonic() + 10
        while active() == 't1' and time.monotonic() < deadline:
            time.sleep(0.1)
        assert active() == ''  # Back in the app's window,
        assert state() == ['waiting']  # and the session still runs.
        runner.stop(record)
        assert runner.alive([record]) == set()
        assert '"exit"' in log.read_text()  # pi quit on /quit; the window was not killed.
    finally:
        subprocess.run([*base, 'kill-server'], capture_output=True)


def test_session_internals_are_out_of_reach_and_git_may_use_the_checkout(tmp_path, home):
    internals = tmp_path / 'var' / 'sessions' / 'x1' / 'session.json'
    checkout = tmp_path / 'checkout'
    steps = [
        {'tool': 'read', 'args': {'path': str(internals)}},
        {'tool': 'bash', 'args': {'command': f'git -C {checkout} log -1'}},
        {'tool': 'bash', 'args': {'command': 'git status'}},
    ]
    _events, transcript, _stdout = run(tmp_path, steps)
    assert f'{internals} is outside the session' in transcript
    assert 'not a read-only git command' not in transcript  # -C into the checkout is allowed.
    # The checkout is not a repository here, and git finds none around the session.
    assert transcript.count('not a git repository') == 2

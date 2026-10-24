"""Contract: sessions run in tmux windows that outlive the app; stop, resume, and switch."""

import json
import shutil
import subprocess
import time
import uuid

import pytest
from sessiondata import make_record

from b4_maintainer.sessions.pi import LaunchSpec, Site, prepare_session
from b4_maintainer.sessions.profiles import Request
from b4_maintainer.sessions.store import SessionStore
from b4_maintainer.sessions.tmuxrunner import TmuxRunner

pytestmark = pytest.mark.skipif(shutil.which('tmux') is None, reason='tmux not installed')
REQUEST = Request('Review the series.', 'You assist a maintainer.', (), 'lightweight', ())

# Stands in for pi: records its arguments and environment, reports start and waiting,
# and exits on /quit, reporting that too.
STAND_IN = """#!/bin/sh
printf '%s\\n' "$@" > "$B4M_OUT/../argv.$$"
printf '%s\\n' "$B4M_SESSION" > "$B4M_OUT/../env.$$"
echo '{"time": "2026-10-23T12:00:00+00:00", "event": "start"}' >> "$B4M_EVENTS"
echo '{"time": "2026-10-23T12:00:00+00:00", "event": "waiting"}' >> "$B4M_EVENTS"
while read -r line; do
    case "$line" in
    */quit) echo '{"time": "2026-10-23T12:00:01+00:00", "event": "exit"}' >> "$B4M_EVENTS"; exit 0 ;;
    esac
done
"""


@pytest.fixture
def server():
    socket = f'b4m-test-{uuid.uuid4().hex[:8]}'
    base = ['tmux', '-L', socket, '-f', '/dev/null']
    subprocess.run([*base, 'new-session', '-d', '-x', '200', '-y', '50'], check=True)
    yield socket, base
    subprocess.run([*base, 'kill-server'], capture_output=True)


@pytest.fixture
def store(tmp_path):
    return SessionStore(tmp_path / 'var' / 'sessions')


def make_runner(tmp_path, store, socket, pi=None):
    stand_in = tmp_path / 'pi'
    stand_in.write_text(STAND_IN)
    stand_in.chmod(0o755)
    site = Site(tmp_path, 'b4-maintainer', str(pi or stand_in), checkouts={})
    return TmuxRunner(store, site, socket=socket)


def wait_for(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def windows(base):
    out = subprocess.run(
        [*base, 'list-windows', '-a', '-F', '#{window_id} #{@b4m_session} #{window_name}'],
        capture_output=True,
        text=True,
    ).stdout
    return [line.split(' ', 2) for line in out.splitlines()]


def launched(tmp_path, store, server, sid='s1'):
    socket, base = server
    runner = make_runner(tmp_path, store, socket)
    record = make_record(sid)
    store.create(record)
    prepare_session(store, record, REQUEST, source='')
    runner.launch(record, LaunchSpec(REQUEST))
    assert wait_for(lambda: (store.directory(sid) / 'events.jsonl').exists())
    return runner, record


def argv_of(store, sid, index=0):
    files = sorted((store.directory(sid) / 'work').glob('argv.*'), key=lambda p: p.stat().st_mtime)
    return files[index].read_text().splitlines()


def test_a_launch_opens_a_window_named_after_the_series(tmp_path, store, server):
    runner, record = launched(tmp_path, store, server)
    [(_window, sid, name)] = [w for w in windows(server[1]) if w[1] == 's1']
    assert name.startswith('b4m:linux: mm: fix foo leak v3'[:20])
    argv = argv_of(store, 's1')
    assert argv[argv.index('--session-id') + 1] == 's1' and argv[-1] == REQUEST.prompt
    assert runner.alive([record]) == {'s1'}
    assert [v.state for v in store.poll(runner.alive)] == ['waiting']
    launch = json.loads((store.directory('s1') / 'launch.json').read_text())
    assert launch['argv'][-1] == REQUEST.prompt and launch['env']['B4M_SESSION'] == 's1'
    assert store.record('s1').command[-1] == REQUEST.prompt


def test_a_new_runner_finds_running_sessions(tmp_path, store, server):
    _runner, record = launched(tmp_path, store, server)
    again = make_runner(tmp_path, store, server[0])
    assert again.alive([record, make_record('other')]) == {'s1'}


def test_stop_quits_pi_and_closes_the_window(tmp_path, store, server):
    runner, record = launched(tmp_path, store, server)
    runner.stop(record)
    assert runner.alive([record]) == set()
    assert '"exit"' in (store.directory('s1') / 'events.jsonl').read_text()


def test_resume_reopens_without_resending_the_task(tmp_path, store, server):
    runner, record = launched(tmp_path, store, server)
    runner.stop(record)
    runner.resume(record)
    assert wait_for(lambda: len(list((store.directory('s1') / 'work').glob('argv.*'))) == 2)
    argv = argv_of(store, 's1', 1)
    assert argv[argv.index('--session-id') + 1] == 's1'
    assert REQUEST.prompt not in argv and '--' not in argv
    assert runner.alive([record]) == {'s1'}


def test_switch_selects_the_sessions_window(tmp_path, store, server):
    runner, record = launched(tmp_path, store, server)
    socket, base = server
    subprocess.run([*base, 'select-window', '-t', ':0'], check=True)
    runner.switch(record)
    current = subprocess.run(
        [*base, 'display-message', '-p', '#{@b4m_session}'], capture_output=True, text=True
    ).stdout.strip()
    assert current == 's1'


def test_blockers_name_what_is_missing(tmp_path, store, server):
    socket, _base = server
    assert make_runner(tmp_path, store, socket).blocker() is None
    missing = make_runner(tmp_path, store, socket, pi=tmp_path / 'no-such-pi')
    assert 'pi' in missing.blocker()
    outside = TmuxRunner(store, Site(tmp_path, 'b4-maintainer', 'pi'), socket=None, environ={})
    assert 'tmux' in outside.blocker()


def test_sessions_learn_the_apps_window_for_back(tmp_path, store, server):
    socket, base = server
    app_pane = subprocess.run(
        [*base, 'display-message', '-p', '#{pane_id}'], capture_output=True, text=True
    ).stdout.strip()
    app_window = subprocess.run(
        [*base, 'display-message', '-p', '#{window_id}'], capture_output=True, text=True
    ).stdout.strip()
    stand_in = tmp_path / 'pi'
    stand_in.write_text(STAND_IN)
    stand_in.chmod(0o755)
    site = Site(tmp_path, 'b4-maintainer', str(stand_in))
    runner = TmuxRunner(store, site, socket=socket, environ={'TMUX_PANE': app_pane})
    record = make_record('s1')
    store.create(record)
    prepare_session(store, record, REQUEST, source='')
    runner.launch(record, LaunchSpec(REQUEST))
    launch = json.loads((store.directory('s1') / 'launch.json').read_text())
    assert launch['env']['B4M_APP_WINDOW'] == app_window

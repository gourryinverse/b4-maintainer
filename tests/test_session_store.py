"""Contract: the session store lists records with their state; the hub acts on them."""

import pytest
from sessiondata import NOW, FakeSessionRunner, FakeWorktrees, make_record

from b4_maintainer.sessions.hub import SessionError, SessionHub, WorktreeInfo
from b4_maintainer.sessions.store import SessionStore, valid_id


@pytest.fixture
def store(tmp_path):
    return SessionStore(tmp_path / 'var' / 'sessions')


@pytest.fixture
def runner(store):
    return FakeSessionRunner(store)


def states(views):
    return [(v.record.id, v.state) for v in views]


@pytest.mark.parametrize('sid', ['s1', '20261023-1200-ab12', 'a_b'])
def test_valid_session_ids(sid):
    assert valid_id(sid)


@pytest.mark.parametrize('sid', ['', '.', '..', '../x', 'a/b', '-a', 'a b', 'x' * 65, 'a\n'])
def test_ids_that_could_leave_the_sessions_directory_are_refused(store, sid):
    assert not valid_id(sid)
    with pytest.raises(ValueError):
        store.directory(sid)


def test_records_are_listed_most_urgent_first(store, runner):
    runner.add(make_record('a'))
    runner.add(make_record('b'))
    runner.add(make_record('c'), alive=False)
    runner.emit('a', 'working')
    runner.emit('b', 'approval')
    assert states(store.poll(runner.alive)) == [('b', 'approval'), ('c', 'gone'), ('a', 'working')]


def test_polling_reads_only_new_events_and_keeps_state(store, runner):
    runner.add(make_record('a'))
    runner.emit('a', 'start')
    assert states(store.poll(runner.alive)) == [('a', 'waiting')]
    runner.emit('a', 'tool', tool='read')
    assert states(store.poll(runner.alive)) == [('a', 'working')]
    assert states(store.poll(runner.alive)) == [('a', 'working')]


def test_pending_approvals_count_until_the_agent_moves_on(store, runner):
    runner.add(make_record('a'))
    runner.emit('a', 'approval')
    runner.emit('a', 'approval')
    [view] = store.poll(runner.alive)
    assert (view.state, view.approvals, view.last) == ('approval', 2, NOW)
    runner.emit('a', 'tool_done', tool='bash')
    [view] = store.poll(runner.alive)
    assert (view.state, view.approvals) == ('working', 0)


def test_app_states_are_recorded_and_win(store, runner):
    runner.add(make_record('a'))
    runner.emit('a', 'working')
    store.set_state('a', 'stopped')
    assert states(store.poll(runner.alive)) == [('a', 'stopped')]
    assert store.record('a').state == 'stopped'


def test_new_and_unreadable_records_are_picked_up_and_skipped(store, runner):
    assert store.poll(runner.alive) == []
    runner.add(make_record('a'))
    (store.root / 'broken').mkdir()
    (store.root / 'broken' / 'session.json').write_text('{not json')
    (store.root / 'empty').mkdir()
    assert states(store.poll(runner.alive)) == [('a', 'starting')]


def test_creating_an_existing_session_is_refused(store, runner):
    runner.add(make_record('a'))
    with pytest.raises(FileExistsError):
        store.create(make_record('a'))


def test_artifacts_are_files_under_out(store, runner):
    runner.add(make_record('a'))
    assert store.artifacts('a') == []
    out = store.directory('a') / 'work' / 'out'
    (out / 'draft').mkdir(parents=True)
    (out / 'draft' / 'reply.txt').write_text('synthetic\n')
    (out / 'notes.md').write_text('synthetic\n')
    assert store.artifacts('a') == ['draft/reply.txt', 'notes.md']


def hub(store, runner, *worktrees):
    return SessionHub(store, runner, FakeWorktrees(*worktrees))


def test_the_overview_lists_live_sessions_and_worktrees(store, runner):
    runner.add(make_record('a'))
    runner.add(make_record('b'))
    store.set_state('b', 'ended')
    wt = WorktreeInfo('/w/a', 'b4m/session/a', 'a', 1_500_000_000)
    overview = hub(store, runner, wt).overview()
    assert states(overview.sessions) == [('a', 'starting')]
    assert overview.worktrees == (wt,)
    assert overview.worktree_bytes == 1_500_000_000


def test_open_switches_and_resumes_stopped_or_gone_sessions_first(store, runner):
    h = hub(store, runner)
    runner.add(make_record('a'))
    runner.add(make_record('b'))
    runner.add(make_record('c'), alive=False)
    store.set_state('b', 'stopped')
    h.overview()
    for sid in ('a', 'b', 'c'):
        h.open(sid)
    assert runner.resumes == ['b', 'c']
    assert runner.switches == ['a', 'b', 'c']
    assert store.record('b').state == ''


def test_stop_and_end_record_the_state(store, runner):
    h = hub(store, runner)
    runner.add(make_record('a'))
    runner.add(make_record('b'))
    h.stop('a')
    h.end('b')
    assert (runner.stops, runner.ends) == (['a'], ['b'])
    assert (store.record('a').state, store.record('b').state) == ('stopped', 'ended')


def test_an_ended_session_cannot_be_opened(store, runner):
    h = hub(store, runner)
    runner.add(make_record('a'))
    store.set_state('a', 'ended')
    h.overview()
    with pytest.raises(SessionError):
        h.open('a')
    assert runner.switches == []

"""Contract: the drawer shows sessions from hook events and acts only on maintainer keys."""

import threading

import pytest
from sessiondata import FakeSessionRunner, FakeWorktrees, make_record
from test_app_actions import FakeLive
from test_draft import DraftActions

from b4_maintainer.adapters.fixture import FixtureSource
from b4_maintainer.app import Confirm, MaintainerApp, SeriesList
from b4_maintainer.drawer import drawer_line, format_bytes, session_label, worktree_row
from b4_maintainer.sessions.hub import Overview, SessionHub, WorktreeInfo
from b4_maintainer.sessions.records import Worktree
from b4_maintainer.sessions.store import SessionStore, SessionView
from b4_maintainer.sessions.trees import TreeState

GB = 1_000_000_000


@pytest.fixture
def store(tmp_path):
    return SessionStore(tmp_path / 'var' / 'sessions')


@pytest.fixture
def runner(store):
    return FakeSessionRunner(store)


def make_app(demo_root, store, runner, *worktrees, **kwargs):
    hub = SessionHub(store, runner, FakeWorktrees(*worktrees))
    return MaintainerApp(FixtureSource(demo_root).load, sessions=hub, **kwargs)


async def settle(pilot):
    for _ in range(2):
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()


async def poll(pilot):
    pilot.app.poll_sessions()
    await settle(pilot)


def focused_id(app):
    return app.focused.id if app.focused is not None else None


# Formatting


def view(sid, state, subject='mm: fix foo leak', **changes):
    return SessionView(make_record(sid, subject=subject, **changes), state)


def test_the_line_names_the_most_urgent_session_and_counts_worktrees():
    overview = Overview(
        (view('b', 'approval', 'mm: fix bar'), view('a', 'working')),
        (
            WorktreeInfo('/w/a', 'b4m/session/a', 'a', int(1.5 * GB)),
            WorktreeInfo('/w/b', 'b4m/session/b', 'b', int(1.3 * GB)),
        ),
    )
    line = drawer_line(overview)
    assert 'Sessions 2 · ● linux: mm: fix bar v3 needs approval' in line
    assert 'Worktrees 2 · 2.8 GB' in line
    assert drawer_line(Overview()).strip().startswith('No agent sessions')
    assert 'Worktrees 1' in drawer_line(Overview((), (WorktreeInfo('/w', 'b', 'x', 0),)))


def test_labels_and_sizes():
    assert session_label(make_record()) == 'linux: mm: fix foo leak v3'
    assert session_label(make_record(scope='workspace', series='', subject='')) == 'workspace'
    assert [format_bytes(n) for n in (0, 999, 12_300_000, int(2.84 * GB))] == [
        '0 B',
        '999 B',
        '12.3 MB',
        '2.8 GB',
    ]


# App contracts


async def test_without_sessions_the_line_says_so(demo_root, store, runner):
    app = make_app(demo_root, store, runner)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        assert 'No agent sessions' in app.drawer_text()


async def test_events_update_the_line_without_moving_focus_or_selection(demo_root, store, runner):
    runner.add(make_record('a'))
    app = make_app(demo_root, store, runner)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        series_list = app.query_one(SeriesList)
        await pilot.press('down', 'down')
        selected, before = series_list.cursor, app.drawer_text()
        runner.emit('a', 'approval')
        await poll(pilot)
        assert 'needs approval' in app.drawer_text() != before
        assert app.focused is series_list and series_list.cursor == selected
        assert len(app.screen_stack) == 1
        assert not app.screen.has_class('-drawer-open')


async def test_polling_reads_sessions_off_the_ui_thread(demo_root, store, runner):
    threads = []
    hub = SessionHub(store, runner, FakeWorktrees())
    overview = hub.overview

    def spy():
        threads.append(threading.current_thread() is threading.main_thread())
        return overview()

    hub.overview = spy
    app = MaintainerApp(FixtureSource(demo_root).load, sessions=hub)
    async with app.run_test(size=(180, 40)) as pilot:
        await poll(pilot)
    assert threads and not any(threads)


async def test_backtick_opens_tab_switches_and_escape_returns(demo_root, store, runner):
    runner.add(make_record('a'))
    wt = WorktreeInfo('/w/a', 'b4m/session/a', 'a', GB)
    app = make_app(demo_root, store, runner, wt)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        await pilot.press('grave_accent')
        await settle(pilot)
        assert app.screen.has_class('-drawer-open')
        assert focused_id(app) == 'drawer-sessions'
        assert 'mm: fix foo leak' in app.drawer_rows()[0]
        await pilot.press('tab')
        assert focused_id(app) == 'drawer-worktrees'
        assert 'b4m/session/a' in app.drawer_rows()[0]
        await pilot.press('tab')
        assert focused_id(app) == 'drawer-sessions'
        await pilot.press('escape')
        assert app.focused is app.query_one(SeriesList)
        assert app.screen.has_class('-drawer-open')
        await pilot.press('grave_accent')
        assert not app.screen.has_class('-drawer-open')


async def test_events_keep_the_drawer_selection_on_its_session(demo_root, store, runner):
    runner.add(make_record('a', subject='mm: first'))
    runner.add(make_record('b', subject='mm: second'))
    app = make_app(demo_root, store, runner)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        await pilot.press('grave_accent')
        await settle(pilot)
        await pilot.press('down')
        assert app.selected_session() == 'b'
        runner.emit('b', 'working')
        runner.emit('a', 'approval')
        await poll(pilot)
        assert 'mm: first' in app.drawer_rows()[0]  # a is now the most urgent.
        assert app.selected_session() == 'b'
        assert focused_id(app) == 'drawer-sessions'


async def test_enter_switches_and_resumes_a_stopped_session(demo_root, store, runner):
    runner.add(make_record('a'))
    store.set_state('a', 'stopped')
    app = make_app(demo_root, store, runner)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        await pilot.press('grave_accent')
        await settle(pilot)
        await pilot.press('enter')
        await settle(pilot)
        assert (runner.resumes, runner.switches) == (['a'], ['a'])
        assert app.reading is None


@pytest.mark.parametrize('answer, stopped', [('n', []), ('y', ['a'])])
async def test_s_stops_after_confirmation(demo_root, store, runner, answer, stopped):
    runner.add(make_record('a'))
    app = make_app(demo_root, store, runner)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        await pilot.press('grave_accent')
        await settle(pilot)
        await pilot.press('s')
        assert isinstance(app.screen, Confirm)
        await pilot.press(answer)
        await settle(pilot)
        assert runner.stops == stopped
        assert focused_id(app) == 'drawer-sessions'
        if stopped:
            assert 'stopped' in app.drawer_rows()[0]


async def test_x_ends_after_confirmation_and_may_keep_the_worktree(demo_root, store, runner):
    wt = Worktree('/w/a', 'b4m/session/a', 'v6.18', 'deadbeef')
    runner.add(make_record('a', mode='worktree', worktree=wt))
    info = WorktreeInfo('/w/a', 'b4m/session/a', 'a', GB, TreeState())
    app = make_app(demo_root, store, runner, info)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        hidden = app.snapshot.items[0].hidden
        await pilot.press('grave_accent')
        await settle(pilot)
        await pilot.press('x')
        assert isinstance(app.screen, Confirm)
        await pilot.press('y')
        await settle(pilot)
        assert runner.ends == ['a']
        assert isinstance(app.screen, Confirm) and 'Remove its worktree' in app.screen.prompt
        await pilot.press('n')  # Kept: still in the Worktrees tab.
        await settle(pilot)
        assert app._sessions.worktrees.removed == []
        assert store.record('a').state == 'ended'
        assert app.snapshot.items[0].hidden == hidden  # x did not hide a series.
        assert 'No agent sessions' in app.drawer_text()


async def test_o_lists_the_sessions_artifacts(demo_root, store, runner):
    runner.add(make_record('a'))
    out = store.directory('a') / 'work' / 'out' / 'draft'
    out.mkdir(parents=True)
    (out / 'reply.txt').write_text('synthetic\n')
    app = make_app(demo_root, store, runner)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        await pilot.press('grave_accent')
        await settle(pilot)
        await pilot.press('o')
        await settle(pilot)
        assert 'draft/reply.txt' in app.screen.prompt
        await pilot.press('space')
        assert len(app.screen_stack) == 1
        assert focused_id(app) == 'drawer-sessions'


async def test_series_keys_are_inactive_in_the_drawer(demo_root, store, runner):
    runner.add(make_record('a'))
    app = make_app(demo_root, store, runner)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        await pilot.press('grave_accent')
        await settle(pilot)
        for action in ('open_reader', 'reply', 'track', 'hide', 'search', 'toggle_preview'):
            assert not app.check_action(action, ()), action
        for action in ('open_session', 'stop_session', 'end_session', 'session_artifacts'):
            assert app.check_action(action, ()), action
        await pilot.press('escape')
        assert not app.check_action('stop_session', ())


class Actions(FakeLive):
    prepare_reply = DraftActions.prepare_reply
    answered = DraftActions.answered

    def __init__(self, root):
        super().__init__(root)
        self.answered_calls = []

    def toggle_watched(self, item):
        return ''


async def test_passive_actions_launch_no_agents(demo_root, store, runner):
    runner.add(make_record('a'))
    actions = Actions(demo_root)
    hub = SessionHub(store, runner, FakeWorktrees())
    app = MaintainerApp(actions.load, actions=actions, sessions=hub)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        runner.emit('a', 'approval')
        await pilot.press('f5')
        await settle(pilot)
        for v in [*app.index.views, app.index.views[0]]:
            app.select_view(v.id)
            await pilot.pause()
        await pilot.press('dollar_sign')
        await settle(pilot)
        await pilot.press('enter')
        await settle(pilot)
        await pilot.press('escape', 'r')
        await settle(pilot)
        await poll(pilot)
    assert runner.launches == []
    assert (runner.resumes, runner.stops, runner.switches, runner.ends) == ([], [], [], [])


async def test_a_failed_drawer_update_is_reported_not_fatal(demo_root, store, runner, monkeypatch):
    import b4_maintainer.app as app_module

    runner.add(make_record('a'))
    app = make_app(demo_root, store, runner)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)

        def broken(overview):
            raise RuntimeError('synthetic drawer failure')

        real = app_module.drawer_line
        monkeypatch.setattr(app_module, 'drawer_line', broken)
        runner.emit('a', 'approval')
        await poll(pilot)
        assert app.is_running
        assert 'synthetic drawer failure' in app.status_text()
        monkeypatch.setattr(app_module, 'drawer_line', real)
        await poll(pilot)  # Polling continues after a failure.
        assert 'needs approval' in app.drawer_text()
    assert app.return_code in (0, None)


async def test_opening_the_drawer_selects_the_most_urgent_session(demo_root, store, runner):
    runner.add(make_record('a', subject='mm: first'))
    app = make_app(demo_root, store, runner)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        runner.add(make_record('b', subject='mm: second'))
        runner.emit('b', 'approval')
        await poll(pilot)
        await pilot.press('grave_accent')
        await settle(pilot)
        assert app.selected_session() == 'b'


def tree_app(demo_root, store, runner, *infos, states=None):
    trees = FakeWorktrees(*infos)
    trees.states = states or {}
    hub = SessionHub(store, runner, trees)
    return MaintainerApp(FixtureSource(demo_root).load, sessions=hub), trees


async def open_worktrees(pilot):
    await settle(pilot)
    await pilot.press('grave_accent')
    await settle(pilot)
    await pilot.press('tab')
    await settle(pilot)


def test_worktree_rows_show_state_and_orphans():
    changed = TreeState(changed=('?? x',), commits=('abc agent: work',))
    row = worktree_row(WorktreeInfo('/w/a', 'b4m/session/a', 'a', GB, changed), {'a': 'working'})
    assert 'session a (working)' in row and '1 uncommitted file, 1 commit only here' in row
    orphan = worktree_row(WorktreeInfo('/w/o', 'b4m/session/o', '', GB, TreeState()), {})
    assert 'orphan' in orphan and 'clean' in orphan


async def test_x_removes_a_clean_worktree_after_one_confirmation(demo_root, store, runner):
    runner.add(make_record('a', mode='worktree'), alive=False)
    store.set_state('a', 'ended')
    info = WorktreeInfo('/w/a', 'b4m/session/a', 'a', GB, TreeState())
    app, trees = tree_app(demo_root, store, runner, info)
    async with app.run_test(size=(180, 40)) as pilot:
        await open_worktrees(pilot)
        await pilot.press('x')
        await settle(pilot)
        assert isinstance(app.screen, Confirm)
        assert '/w/a' in app.screen.prompt and 'clean' in app.screen.prompt
        await pilot.press('y')
        await settle(pilot)
        assert trees.removed == ['/w/a']
        assert app.drawer_rows() == []


async def test_changes_need_a_second_confirmation(demo_root, store, runner):
    info = WorktreeInfo('/w/o', 'b4m/session/o', '', GB, TreeState())
    states = {'/w/o': TreeState(changed=('M a.c',))}
    app, trees = tree_app(demo_root, store, runner, info, states=states)
    async with app.run_test(size=(180, 40)) as pilot:
        await open_worktrees(pilot)
        await pilot.press('x')
        await settle(pilot)
        assert '1 uncommitted file' in app.screen.prompt
        await pilot.press('y')
        await settle(pilot)
        assert isinstance(app.screen, Confirm) and 'will be lost' in app.screen.prompt
        await pilot.press('n')
        await settle(pilot)
        assert trees.removed == []
        await pilot.press('x')
        await settle(pilot)
        await pilot.press('y')
        await settle(pilot)
        await pilot.press('y')
        await settle(pilot)
        assert trees.removed == ['/w/o']


async def test_space_marks_several_and_x_removes_them(demo_root, store, runner):
    infos = [WorktreeInfo(f'/w/{n}', f'b4m/session/{n}', '', GB, TreeState()) for n in 'abc']
    app, trees = tree_app(demo_root, store, runner, *infos)
    async with app.run_test(size=(180, 40)) as pilot:
        await open_worktrees(pilot)
        await pilot.press('space', 'down', 'down', 'space')
        await settle(pilot)
        assert [r.startswith('* ') for r in app.drawer_rows()] == [True, False, True]
        await pilot.press('x')
        await settle(pilot)
        assert '/w/a' in app.screen.prompt and '/w/c' in app.screen.prompt
        assert '/w/b' not in app.screen.prompt
        await pilot.press('y')
        await settle(pilot)
        assert trees.removed == ['/w/a', '/w/c']


async def test_a_live_sessions_worktree_is_refused(demo_root, store, runner):
    runner.add(make_record('a', mode='worktree'))
    info = WorktreeInfo('/w/a', 'b4m/session/a', 'a', GB, TreeState())
    app, trees = tree_app(demo_root, store, runner, info)
    async with app.run_test(size=(180, 40)) as pilot:
        await open_worktrees(pilot)
        await pilot.press('x')
        await settle(pilot)
        assert not isinstance(app.screen, Confirm)
        assert 'stop session a first' in app.status_text()
        assert trees.removed == []

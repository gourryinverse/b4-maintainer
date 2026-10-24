"""Contract: a series' session opens beside its thread; only the maintainer's moves join."""

import pytest
from sessiondata import FakeSessionRunner, FakeWorktrees, make_record

from b4_maintainer.adapters.fixture import FixtureSource
from b4_maintainer.app import MaintainerApp, SeriesList
from b4_maintainer.sessions.hub import SessionHub
from b4_maintainer.sessions.store import SessionStore

APP = '%app'


@pytest.fixture
def store(tmp_path):
    return SessionStore(tmp_path / 'var' / 'sessions')


@pytest.fixture
def runner(store):
    return FakeSessionRunner(store)


def make_app(demo_root, store, runner, app_pane=APP):
    hub = SessionHub(store, runner, FakeWorktrees(), sandbox=lambda: True)
    return MaintainerApp(FixtureSource(demo_root).load, sessions=hub, app_pane=app_pane)


async def settle(pilot):
    for _ in range(3):
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()


async def poll(pilot):
    pilot.app.poll_sessions()
    await settle(pilot)


def items(demo_root):
    return FixtureSource(demo_root).load().items


def session(runner, sid, item, state='waiting'):
    runner.add(make_record(sid, series=item.identity, subject=item.series.subject))
    if state == 'waiting':
        runner.emit(sid, 'waiting')
    elif state == 'approval':
        runner.emit(sid, 'approval')
    elif state == 'stopped':
        runner.store.set_state(sid, 'stopped')


async def open_series(pilot, item):
    pilot.app.query_one(SeriesList).select_identity(item.identity)
    await pilot.pause()
    await pilot.press('enter')
    await settle(pilot)


async def test_opening_a_series_joins_its_session_without_taking_focus(demo_root, store, runner):
    first = items(demo_root)[0]
    session(runner, 'a', first)
    app = make_app(demo_root, store, runner)
    async with app.run_test(size=(220, 50)) as pilot:
        await poll(pilot)
        await open_series(pilot, first)
        assert runner.beside == 'a' and runner.joins == [('a', 90)]
        assert runner.focused is None  # Focus stays in the reader.
        await pilot.press('escape')
        await settle(pilot)
        assert runner.beside is None and app.reading is None
        assert runner.windows == {'a'}  # Released, still running.


async def test_the_most_urgent_session_joins_and_stopped_ones_never(demo_root, store, runner):
    first, second = items(demo_root)[:2]
    session(runner, 'w', first)
    session(runner, 'p', first, 'approval')
    session(runner, 's', second, 'stopped')
    app = make_app(demo_root, store, runner)
    async with app.run_test(size=(220, 50)) as pilot:
        await poll(pilot)
        await open_series(pilot, first)
        assert runner.beside == 'p'
        await pilot.press('escape')
        await settle(pilot)
        await open_series(pilot, second)
        assert runner.beside is None and runner.resumes == []


async def test_polling_never_joins(demo_root, store, runner):
    first = items(demo_root)[0]
    app = make_app(demo_root, store, runner)
    async with app.run_test(size=(220, 50)) as pilot:
        await poll(pilot)
        await open_series(pilot, first)
        session(runner, 'late', first)
        await poll(pilot)
        assert runner.beside is None and runner.joins == []


async def test_drawer_enter_opens_the_series_with_its_session_beside(demo_root, store, runner):
    first, second = items(demo_root)[:2]
    session(runner, 'a', first)
    session(runner, 'b', second)
    app = make_app(demo_root, store, runner)
    async with app.run_test(size=(220, 50)) as pilot:
        await poll(pilot)
        await open_series(pilot, first)
        await pilot.press('grave_accent')
        await settle(pilot)
        while app.selected_session() != 'b':
            await pilot.press('down')
        await pilot.press('enter')
        await settle(pilot)
        assert app.reading.identity == second.identity
        assert runner.beside == 'b' and runner.focused == 'b'  # Enter goes to the session.


async def test_drawer_enter_swaps_sessions_of_the_open_series(demo_root, store, runner):
    first = items(demo_root)[0]
    session(runner, 'p', first, 'approval')
    session(runner, 'w', first)
    app = make_app(demo_root, store, runner)
    async with app.run_test(size=(220, 50)) as pilot:
        await poll(pilot)
        await open_series(pilot, first)
        assert runner.beside == 'p'
        await pilot.press('grave_accent')
        await settle(pilot)
        rows = app.drawer_rows()
        assert rows[0].startswith('▸') and 'p' not in rows[1][:1]  # The joined one is marked.
        await pilot.press('down', 'enter')
        await settle(pilot)
        assert runner.beside == 'w' and app.reading.identity == first.identity


async def test_the_open_series_sessions_come_first_in_the_drawer(demo_root, store, runner):
    first, second = items(demo_root)[:2]
    session(runner, 'other', second, 'approval')  # More urgent, but another series.
    session(runner, 'mine', first)
    app = make_app(demo_root, store, runner)
    async with app.run_test(size=(220, 50)) as pilot:
        await poll(pilot)
        await open_series(pilot, first)
        await pilot.press('grave_accent')
        await settle(pilot)
        assert app.selected_session() == 'mine'
        assert first.series.subject in app.drawer_rows()[0]


async def test_a_workspace_session_sits_beside_the_list_and_yields_to_series(
    demo_root, store, runner
):
    first, second = items(demo_root)[:2]
    runner.add(make_record('ws', scope='workspace', series='', subject=''))
    runner.emit('ws', 'waiting')
    session(runner, 'a', first)
    app = make_app(demo_root, store, runner)
    async with app.run_test(size=(220, 50)) as pilot:
        await poll(pilot)
        await pilot.press('grave_accent')
        await settle(pilot)
        while app.selected_session() != 'ws':
            await pilot.press('down')
        await pilot.press('enter')
        await settle(pilot)
        assert runner.beside == 'ws' and app.reading is None
        await pilot.press('escape')  # Back to the list from the drawer.
        await open_series(pilot, second)  # No session of its own: the workspace one stays.
        assert runner.beside == 'ws'
        await pilot.press('escape')
        await settle(pilot)
        await open_series(pilot, first)  # Its own session takes the column.
        assert runner.beside == 'a'
        await pilot.press('escape')
        await settle(pilot)
        assert runner.beside == 'ws'  # And the workspace session comes back.


async def test_f12_focuses_the_session_and_zooms_on_narrow_terminals(demo_root, store, runner):
    first = items(demo_root)[0]
    session(runner, 'a', first)
    app = make_app(demo_root, store, runner)
    async with app.run_test(size=(220, 50)) as pilot:
        await poll(pilot)
        assert not app.check_action('focus_session', ())  # Nothing beside yet.
        await open_series(pilot, first)
        await pilot.press('f12')
        await settle(pilot)
        assert (runner.focused, runner.zoomed) == ('a', False)
        await pilot.resize_terminal(80, 40)
        await pilot.press('f12')
        await settle(pilot)
        assert (runner.focused, runner.zoomed) == ('a', True)


async def test_f1_while_reading_joins_the_new_session(demo_root, store, runner):
    first = items(demo_root)[0]
    app = make_app(demo_root, store, runner)
    async with app.run_test(size=(220, 50)) as pilot:
        await poll(pilot)
        await open_series(pilot, first)
        await pilot.press('f1')
        await settle(pilot)
        dialog = app.screen
        dialog.pick('goal', 'review')
        await settle(pilot)
        await pilot.press('ctrl+s')
        await settle(pilot)
        [(sid, _request)] = runner.launches
        assert runner.beside == sid and runner.focused is None


async def test_without_tmux_panes_enter_switches_windows_as_before(demo_root, store, runner):
    first = items(demo_root)[0]
    session(runner, 'a', first)
    app = make_app(demo_root, store, runner, app_pane='')
    async with app.run_test(size=(220, 50)) as pilot:
        await poll(pilot)
        await open_series(pilot, first)
        assert runner.joins == []
        await pilot.press('grave_accent')
        await settle(pilot)
        await pilot.press('enter')
        await settle(pilot)
        assert runner.switches == ['a'] and runner.joins == []


async def test_quitting_puts_the_session_back_in_its_window(demo_root, store, runner):
    first = items(demo_root)[0]
    session(runner, 'a', first)
    app = make_app(demo_root, store, runner)
    async with app.run_test(size=(220, 50)) as pilot:
        await poll(pilot)
        await open_series(pilot, first)
        assert runner.beside == 'a'
    assert runner.beside is None and runner.windows == {'a'}


def drafting_app(demo_root, tmp_path, store, runner):
    from test_editor_drafts import CapturingActions, FakeEditor

    from b4_maintainer.adapters.drafts import DraftStore

    actions = CapturingActions(demo_root)
    editor = FakeEditor()
    hub = SessionHub(store, runner, FakeWorktrees(), sandbox=lambda: True)
    app = MaintainerApp(
        actions.load,
        actions=actions,
        drafts=DraftStore(tmp_path / 'drafts'),
        editor=editor,
        sessions=hub,
        app_pane=APP,
    )
    return app, editor


async def test_the_editor_stacks_under_the_session(demo_root, tmp_path, store, runner):
    first = items(demo_root)[0]
    session(runner, 'a', first)
    app, editor = drafting_app(demo_root, tmp_path, store, runner)
    async with app.run_test(size=(220, 50)) as pilot:
        await poll(pilot)
        await open_series(pilot, first)
        assert runner.beside == 'a'
        await pilot.press('r')
        await settle(pilot)
        assert editor.below == ['%a']  # Split under the session pane, not beside the app.
        assert runner.beside == 'a'


async def test_on_short_terminals_the_session_steps_aside_while_drafting(
    demo_root, tmp_path, store, runner
):
    first = items(demo_root)[0]
    session(runner, 'a', first)
    app, editor = drafting_app(demo_root, tmp_path, store, runner)
    async with app.run_test(size=(220, 30)) as pilot:
        await poll(pilot)
        await open_series(pilot, first)
        await pilot.press('r')
        await settle(pilot)
        assert editor.below == [''] and runner.beside is None  # Set aside, still running.
        assert runner.windows == {'a'}
        editor.exit()
        await pilot.pause(1.3)
        await settle(pilot)
        assert runner.beside == 'a'  # Back beside the thread once the draft is done.


async def test_the_editor_never_stacks_under_a_session_that_is_not_beside(
    demo_root, tmp_path, store, runner
):
    first = items(demo_root)[0]
    session(runner, 'a', first)
    app, editor = drafting_app(demo_root, tmp_path, store, runner)
    async with app.run_test(size=(220, 50)) as pilot:
        await poll(pilot)
        await open_series(pilot, first)
        runner.beside = None  # Moved away behind the app's back (by hand, or still joining).
        await pilot.press('r')
        await settle(pilot)
        assert runner.joins[-1][0] == 'a' and runner.beside == 'a'  # Back beside first,
        assert editor.below == ['%a']  # then the editor goes under it.


async def test_reopening_a_thread_puts_the_session_above_its_open_editor(
    demo_root, tmp_path, store, runner
):
    first = items(demo_root)[0]
    session(runner, 'a', first)
    app, editor = drafting_app(demo_root, tmp_path, store, runner)
    async with app.run_test(size=(220, 50)) as pilot:
        await poll(pilot)
        await open_series(pilot, first)
        await pilot.press('r')
        await settle(pilot)
        editor_pane = editor.opened[-1][0]
        await pilot.press('escape')  # Close the thread: the editor stays open beside the list.
        await settle(pilot)
        assert runner.beside is None and editor.alive(editor_pane)
        await pilot.press('enter')
        await settle(pilot)
        assert runner.beside == 'a' and runner.above[-1] == editor_pane

"""Contract: a workbench session lives in the Workbench tab: its workbench lists its
sessions, with what each produced; Enter (there, or on it in the drawer) puts it beside
the app; one launched from the tab joins beside at once."""

from sessiondata import FakeSessionRunner, FakeWorktrees, make_record
from test_workbench_screen import BENCH, FakeService

from b4_maintainer.adapters.fixture import FixtureSource
from b4_maintainer.app import MaintainerApp
from b4_maintainer.sessions.hub import SessionHub
from b4_maintainer.sessions.store import SessionStore
from b4_maintainer.workbench import WorkbenchStore
from b4_maintainer.workbenchscreen import WorkbenchScreen

APP = '%app'


def make(demo_root, tmp_path):
    sessions = SessionStore(tmp_path / 'var' / 'sessions')
    runner = FakeSessionRunner(sessions)
    hub = SessionHub(sessions, runner, FakeWorktrees(), sandbox=lambda: True)
    store = WorkbenchStore(tmp_path / 'state' / 'workbenches')
    store.save(BENCH)
    service = FakeService(tmp_path, store, hub.records, sessions.directory)
    app = MaintainerApp(
        FixtureSource(demo_root).load, sessions=hub, workbenches=service, app_pane=APP
    )
    return app, runner


def bench_session(runner, sid, state='waiting'):
    record = make_record(
        sid,
        scope='workbench',
        series=f'workbench:{BENCH.id}',
        subject=BENCH.label,
        workbench=BENCH.id,
        mode='lightweight',
    )
    runner.add(record)
    directory = runner.store.directory(sid)
    (directory / 'work' / 'context').mkdir(parents=True, exist_ok=True)
    (directory / 'work' / 'context' / 'task.md').write_text('Compare the series.\n')
    (directory / 'system.md').write_text('System.\n')
    if state == 'waiting':
        runner.emit(sid, 'waiting')
    return record


async def settle(pilot):
    for _ in range(3):
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()


async def test_enter_in_the_drawer_opens_a_workbench_session_in_its_tab(demo_root, tmp_path):
    app, runner = make(demo_root, tmp_path)
    bench_session(runner, 'w1')
    async with app.run_test(size=(220, 50)) as pilot:
        app.poll_sessions()
        await settle(pilot)
        await pilot.press('grave_accent')
        await settle(pilot)
        while app.selected_session() != 'w1':
            await pilot.press('down')
        await pilot.press('enter')  # Used to crash: no series to read.
        await settle(pilot)
        screen = app.screen
        assert isinstance(screen, WorkbenchScreen)
        assert screen.bench().id == BENCH.id and screen.selected_session() == 'w1'
        assert runner.beside == 'w1'


async def test_the_tab_lists_a_workbenchs_sessions_and_what_they_produced(demo_root, tmp_path):
    app, runner = make(demo_root, tmp_path)
    bench_session(runner, 'w1')
    bench_session(runner, 'w2', state='')
    async with app.run_test(size=(220, 50)) as pilot:
        app.poll_sessions()
        await settle(pilot)
        await pilot.press('f3')
        await settle(pilot)
        screen = app.screen
        labels = screen.session_labels()
        assert len(labels) == 2 and all('lightweight' in label for label in labels)
        name = runner.store.names()['w1']
        assert any(label.startswith(name) and 'waiting' in label for label in labels)
        await screen.focus_sessions()
        while screen.selected_session() != 'w1':
            await pilot.press('down')
        await settle(pilot)
        assert any('initial prompt' in label for label in screen.output_labels())
        assert 'Compare the series.' in screen.shown_text()
        await pilot.press('enter')
        await settle(pilot)
        assert runner.beside == 'w1'


async def test_a_session_launched_from_the_tab_joins_beside(demo_root, tmp_path):
    from b4_maintainer.workbench import Entry, Item, Workbench

    app, runner = make(demo_root, tmp_path)
    async with app.run_test(size=(220, 50)) as pilot:
        await settle(pilot)
        first = app.snapshot.items[0]
        bench = Workbench(
            'demo',
            'Demo',
            (
                Entry(
                    'linux',
                    'HEAD',
                    (Item(first.identity, first.series.revision, first.series.subject),),
                ),
            ),
        )
        app._workbenches.store.save(bench)
        await pilot.press('f3')
        await settle(pilot)
        screen = app.screen
        while screen.bench().id != 'demo':
            await pilot.press('down')
        await pilot.press('f1')
        await settle(pilot)
        app.screen.pick('goal', 'review')  # No tree for any project: lightweight.
        await pilot.pause()
        await pilot.press('ctrl+s')
        await settle(pilot)
        [(sid, _request)] = runner.launches
        assert runner.beside == sid
        app.poll_sessions()
        await settle(pilot)
        name = runner.store.names()[sid]
        assert any(label.startswith(name) for label in screen.session_labels())


async def test_the_column_follows_the_tab_and_o_opens_the_live_session(demo_root, tmp_path):
    app, runner = make(demo_root, tmp_path)
    bench_session(runner, 'w1')
    async with app.run_test(size=(220, 50)) as pilot:
        app.poll_sessions()
        await settle(pilot)
        await pilot.press('f3')
        await settle(pilot)
        screen = app.screen
        assert screen.focused.id == 'bench-list'
        await pilot.press('o')  # From any pane: the workbench's live session.
        await settle(pilot)
        assert runner.beside == 'w1'
        await pilot.press('f2')  # LKML: it goes back to its own window...
        await settle(pilot)
        assert runner.beside is None
        await pilot.press('f3')  # ...and returns with the Workbench tab.
        await settle(pilot)
        assert runner.beside == 'w1'
        await pilot.press('escape')  # Escape to LKML moves it too.
        await settle(pilot)
        assert runner.beside is None


async def test_the_panes_are_titled(demo_root, tmp_path):
    app, _runner = make(demo_root, tmp_path)
    async with app.run_test(size=(220, 50)) as pilot:
        await settle(pilot)
        await pilot.press('f3')
        await settle(pilot)
        panes = '#bench-list, #bench-sessions, #bench-contents, #bench-show-pane'
        titles = [str(w.border_title) for w in app.screen.query(panes)]
        assert titles == ['Workbenches', 'Sessions', 'Contents', 'Launch preview']


async def test_f1_on_a_series_launches_into_a_new_workbench_made_from_it(demo_root, tmp_path):
    from b4_maintainer.branchscreen import Pick
    from b4_maintainer.launchdialog import LaunchDialog

    app, runner = make(demo_root, tmp_path)
    async with app.run_test(size=(220, 50)) as pilot:
        await settle(pilot)
        item = app.query_one('SeriesList').highlighted_item
        held = [b for b in app._workbenches.store.list()[0] if b.id != BENCH.id]
        assert held == []
        await pilot.press('f1')  # No workbench holds it: straight to a new one's dialog.
        await settle(pilot)
        assert isinstance(app.screen, LaunchDialog)
        assert app.screen.trees and app.screen.mailboxes  # A workbench launch.
        await pilot.press('escape')  # Cancelled: no workbench is left behind.
        await settle(pilot)
        assert [b.id for b in app._workbenches.store.list()[0]] == [BENCH.id]
        await pilot.press('f1')
        await settle(pilot)
        app.screen.pick('goal', 'review')
        await pilot.pause()
        await pilot.press('ctrl+s')
        await settle(pilot)
        [(sid, _request)] = runner.launches
        record = app._sessions.store.record(sid)
        bench = app._workbenches.store.get(record.workbench)
        assert bench.origin == item.identity and bench.label == item.series.subject
        assert bench.entries[0].items[0].series == item.identity
        # Still the series' own session: beside its thread, first in its lists.
        assert (record.scope, record.series, record.project) == (
            'series',
            item.identity,
            item.project,
        )

        await pilot.press('f1')  # Now a workbench holds it: new (first) or that one.
        await settle(pilot)
        assert isinstance(app.screen, Pick)
        assert [key for key, _label in app.screen.choices] == ['\0new', bench.id]
        await pilot.press('down', 'enter')
        await settle(pilot)
        app.screen.pick('goal', 'review')
        await pilot.pause()
        await pilot.press('ctrl+s')
        await settle(pilot)
        second = app._sessions.store.record(runner.launches[-1][0])
        assert second.workbench == bench.id and len(app._workbenches.store.list()[0]) == 2


async def test_sessions_from_before_are_listed_as_unfiled(demo_root, tmp_path):
    app, runner = make(demo_root, tmp_path)
    first = FixtureSource(demo_root).load().items[0]
    runner.add(make_record('old', series=first.identity, subject=first.series.subject))
    async with app.run_test(size=(220, 50)) as pilot:
        app.poll_sessions()
        await settle(pilot)
        await pilot.press('f3')
        await settle(pilot)
        screen = app.screen
        assert screen.bench_labels()[-1] == 'Unfiled sessions'
        while (
            screen.bench_labels()[screen.query_one('#bench-list').highlighted] != 'Unfiled sessions'
        ):
            await pilot.press('down')
        await settle(pilot)
        name = runner.store.names()['old']  # Named when first polled.
        assert any(label.startswith(name) for label in screen.session_labels())
        assert not screen.check_action('rename', ()) and not screen.check_action('launch', ())


async def test_contents_keep_their_place_through_session_polls(demo_root, tmp_path):
    app, runner = make(demo_root, tmp_path)
    record = bench_session(runner, 'w1')
    out = runner.store.directory('w1') / 'work' / 'out'
    out.mkdir(parents=True, exist_ok=True)
    for name in ('a', 'b', 'c'):
        (out / f'{name}.md').write_text(f'{name}\n')
    async with app.run_test(size=(220, 50)) as pilot:
        app.poll_sessions()
        await settle(pilot)
        await pilot.press('f3')
        await settle(pilot)
        screen = app.screen
        rows = [r for r in screen._rows if r.kind == 'artifact']
        assert len(rows) >= 3 and record.id == 'w1'
        screen.select_row('artifact')
        await pilot.press('down', 'down')
        await settle(pilot)
        place = screen.row()
        for _ in range(3):  # Polls (each session event) must not move the cursor.
            app.poll_sessions()
            await settle(pilot)
        assert screen.row() == place


async def test_r_on_a_session_renames_it_and_r_on_the_workbench_still_renames_that(
    demo_root, tmp_path
):
    from b4_maintainer.branchscreen import Ask

    app, runner = make(demo_root, tmp_path)
    bench_session(runner, 'w1')
    async with app.run_test(size=(220, 50)) as pilot:
        app.poll_sessions()
        await settle(pilot)
        await pilot.press('f3')
        await settle(pilot)
        screen = app.screen
        await pilot.press('r')  # The workbench list: the workbench.
        await settle(pilot)
        assert isinstance(app.screen, Ask) and 'workbench' in app.screen.title_text
        await pilot.press('escape')
        await screen.focus_sessions()
        await settle(pilot)
        await pilot.press('r')
        await settle(pilot)
        assert isinstance(app.screen, Ask) and 'session' in app.screen.title_text
        assert app.screen.query_one('Input').value == runner.store.names()['w1']
        app.screen.query_one('Input').value = 'cxl compare'
        await pilot.press('enter')
        await settle(pilot)
        await settle(pilot)
        assert runner.store.names()['w1'] == 'cxl compare'
        assert screen.session_labels()[0].startswith('cxl compare · ')
        assert 'Renamed to cxl compare' in screen.status_text()

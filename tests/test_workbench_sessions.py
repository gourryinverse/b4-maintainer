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
    service = FakeService(tmp_path, store, hub.records)
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
        assert any(label.startswith('w1') and 'waiting' in label for label in labels)
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
        app.screen.pick('goal', 'review')
        app.screen.pick('mode', 'lightweight')
        await pilot.pause()
        await pilot.press('ctrl+s')
        await settle(pilot)
        [(sid, _request)] = runner.launches
        assert runner.beside == sid
        app.poll_sessions()
        await settle(pilot)
        assert any(label.startswith(sid) for label in screen.session_labels())

"""Contract: browsing a workbench's Contents stays quick: the right pane renders only the
lines in view (a commit can be thousands), a row shown before comes back from memory, a
row left before its text arrived never replaces the one now selected, and session polls
that change nothing leave the lists alone."""

import threading

from test_workbench_screen import FakeService

from b4_maintainer.adapters.fixture import FixtureSource
from b4_maintainer.app import MaintainerApp
from b4_maintainer.widgets import TextPane
from b4_maintainer.workbench import ContentRow, Entry, Item, Workbench, WorkbenchStore

ROWS = [ContentRow('Mailboxes', 'heading')] + [
    ContentRow(f'  s{n} (linux)', 'mailbox', 'linux', series=f's{n}') for n in range(3)
]


class SlowService(FakeService):
    """Shows a row as its series; s0 waits until released."""

    def __init__(self, *args):
        super().__init__(*args)
        self.shown = []
        self.release = threading.Event()

    def contents(self, workspace, bench, series, latest):
        return list(ROWS)

    def show(self, workspace, bench, row, series, latest):
        self.shown.append(row.series)
        if row.series == 's0':
            self.release.wait(10)
        return f'shown {row.series}\n' + 'diff line\n' * 4000


def make(demo_root, tmp_path):
    store = WorkbenchStore(tmp_path / 'state' / 'workbenches')
    store.save(Workbench('w', 'W', (Entry('linux', '', (Item(branch='me/fix'),)),)))
    service = SlowService(tmp_path, store, lambda: [])
    app = MaintainerApp(FixtureSource(demo_root).load, workbenches=service)
    return app, service


async def settle(pilot):
    for _ in range(3):
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()


async def test_a_row_left_early_never_replaces_the_one_selected(demo_root, tmp_path):
    app, service = make(demo_root, tmp_path)
    service.release.set()
    async with app.run_test(size=(200, 50)) as pilot:
        await settle(pilot)
        await pilot.press('f3')
        await settle(pilot)
        screen = app.screen
        assert isinstance(screen.query_one('#bench-show-pane'), TextPane)  # Lines in view.
        service.release.clear()
        screen.select_row('mailbox')  # s0: its text waits.
        await pilot.pause()
        await pilot.press('down')  # s1 arrives first.
        await settle(pilot)
        assert screen.shown_text().startswith('shown s1')
        service.release.set()  # Then s0's, late.
        await settle(pilot)
        assert screen.shown_text().startswith('shown s1')


async def test_a_row_shown_before_comes_back_from_memory(demo_root, tmp_path):
    app, service = make(demo_root, tmp_path)
    service.release.set()
    async with app.run_test(size=(200, 50)) as pilot:
        await settle(pilot)
        await pilot.press('f3')
        await settle(pilot)
        screen = app.screen
        screen.select_row('mailbox')
        await settle(pilot)
        for key in ('down', 'down', 'up', 'up', 'down'):
            await pilot.press(key)
            await settle(pilot)
        assert service.shown.count('s1') == 1 and service.shown.count('s0') == 1
        assert screen.shown_text().startswith('shown s1')


async def test_polls_that_change_nothing_leave_the_session_list_alone(demo_root, tmp_path):
    from sessiondata import FakeSessionRunner, FakeWorktrees, make_record

    from b4_maintainer.sessions.hub import SessionHub
    from b4_maintainer.sessions.store import SessionStore

    sessions = SessionStore(tmp_path / 'var' / 'sessions')
    runner = FakeSessionRunner(sessions)
    hub = SessionHub(sessions, runner, FakeWorktrees(), sandbox=lambda: True)
    store = WorkbenchStore(tmp_path / 'state' / 'workbenches')
    store.save(Workbench('w', 'W', (Entry('linux', '', ()),)))
    runner.add(make_record('w1', scope='workbench', series='workbench:w', workbench='w'))
    app = MaintainerApp(
        FixtureSource(demo_root).load,
        sessions=hub,
        workbenches=FakeService(tmp_path, store, hub.records, sessions.directory),
    )
    async with app.run_test(size=(200, 50)) as pilot:
        app.poll_sessions()
        await settle(pilot)
        await pilot.press('f3')
        await settle(pilot)
        lists = app.screen.query_one('#bench-sessions')
        cleared = []
        original = lists.clear_options
        lists.clear_options = lambda: cleared.append(1) or original()
        for _ in range(3):
            app.poll_sessions()
            await settle(pilot)
        assert cleared == []
        runner.add(make_record('w2', scope='workbench', series='workbench:w', workbench='w'))
        app.poll_sessions()
        await settle(pilot)
        assert cleared == [1]

"""Contract: the app syncs workspace state in the background at startup and on quit."""

import threading

from test_app_actions import FakeLive, settle

from b4_maintainer.adapters.statesync import SyncError
from b4_maintainer.app import MaintainerApp


class Syncer:
    def __init__(self, fail=None, block=None):
        self.calls = []
        self.fail = fail
        self.block = block

    def __call__(self, timeout=None):
        self.calls.append(timeout)
        if self.block is not None:
            self.block.wait(5)
        if self.fail:
            raise SyncError(self.fail)
        return 'State synced'


def make(demo_root, syncer, timeout=5.0):
    live = FakeLive(demo_root)
    loads = []

    def load():
        loads.append(1)
        return live.load()

    app = MaintainerApp(load, actions=live, sync=syncer, sync_timeout=timeout)
    return app, loads


async def test_startup_syncs_in_the_background_then_reloads(demo_root):
    syncer = Syncer()
    app, loads = make(demo_root, syncer)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        await settle(pilot)
        assert len(syncer.calls) == 1
        assert len(loads) == 2
        assert 'State synced' in app.status_text()


async def test_a_failed_sync_is_reported_and_work_continues(demo_root):
    syncer = Syncer(fail='fetch failed: offline')
    app, loads = make(demo_root, syncer)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        await settle(pilot)
        assert 'State sync failed: fetch failed: offline' in app.status_text()
        assert len(loads) == 1


async def test_quit_syncs_before_exiting(demo_root):
    syncer = Syncer()
    app, _ = make(demo_root, syncer, timeout=7.0)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        await pilot.press('Q')
        await pilot.pause()
        await pilot.press('y')
        await pilot.pause(0.3)
    assert syncer.calls == [None, 7.0]
    assert app.return_code is not None or not app.is_running


async def test_quit_does_not_wait_past_the_timeout(demo_root):
    block = threading.Event()
    syncer = Syncer()
    app, _ = make(demo_root, syncer, timeout=0.2)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        syncer.block = block
        await pilot.press('Q')
        await pilot.pause()
        await pilot.press('y')
        await pilot.pause(0.1)
        assert 'Syncing state' in app.status_text()
        await pilot.pause(0.5)
        assert not app.is_running
    block.set()


async def test_without_sync_nothing_runs(demo_root):
    live = FakeLive(demo_root)
    app = MaintainerApp(live.load, actions=live)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        assert 'sync' not in app.status_text().lower()

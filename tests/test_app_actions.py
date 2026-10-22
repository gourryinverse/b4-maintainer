"""Contract: refresh and track are explicit, off the UI thread, and keep the reader's place."""

import dataclasses
import threading

from b4_maintainer.adapters.fixture import FixtureSource
from b4_maintainer.app import MaintainerApp, SeriesList
from b4_maintainer.workspace import Snapshot


class FakeLive:
    def __init__(self, root):
        self.fixture = FixtureSource(root)
        self.tracked: set[str] = set()
        self.hidden: set[str] = set()
        self.read_calls = []
        self.loads = 0
        self.refreshes = 0
        self.track_calls = []
        self.threads = set()
        self.fail = None

    def load(self):
        self.loads += 1
        snapshot = self.fixture.load()
        items = tuple(
            dataclasses.replace(i, state=dataclasses.replace(i.state, to_review=True))
            if i.identity in self.tracked
            else i
            for i in snapshot.items
        )
        items = tuple(
            dataclasses.replace(i, hidden=True) if i.identity in self.hidden else i for i in items
        )
        return Snapshot(snapshot.workspace, items)

    def refresh(self, progress):
        self.threads.add(threading.get_ident())
        progress('Fetching example.org (1/1)…')
        self.refreshes += 1
        return 'Refreshed 1 list(s): 0 new message(s)'

    def toggle_hidden(self, item):
        self.threads.add(threading.get_ident())
        self.hidden.symmetric_difference_update({item.identity})
        return ('Unhidden: ' if item.hidden else 'Hidden: ') + item.series.subject

    def mark_read(self, item):
        self.threads.add(threading.get_ident())
        self.read_calls.append(item.identity)
        return f'Marked 3 message(s) read: {item.series.subject}'

    def mark_seen(self, msgids):
        pass

    def track(self, item):
        self.threads.add(threading.get_ident())
        if self.fail:
            raise RuntimeError(self.fail)
        self.track_calls.append(item.identity)
        self.tracked.add(item.identity)
        return f'Tracking: {item.series.subject}'


def footer_keys(app):
    return {key for key, active in app.screen.active_bindings.items() if active.binding.show}


async def settle(pilot):
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()
    return pilot.app.query_one(SeriesList)


def make(demo_root):
    live = FakeLive(demo_root)
    return MaintainerApp(live.load, actions=live), live


async def test_fixture_workspace_has_no_refresh_or_track(demo_root):
    app = MaintainerApp(FixtureSource(demo_root).load)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        assert not {'r', 't'} & footer_keys(app)


async def test_track_marks_selection_and_keeps_place(demo_root):
    app, live = make(demo_root)
    async with app.run_test(size=(180, 40)) as pilot:
        series_list = await settle(pilot)
        app.select_view('profile:mm')
        target = next(i for i in app.index.items('profile:mm') if not i.state.to_review)
        series_list.select_identity(target.identity)
        await pilot.pause()
        assert not target.state.to_review
        assert 't' in footer_keys(app)

        await pilot.press('t')
        series_list = await settle(pilot)
        assert live.track_calls == [target.identity]
        assert threading.get_ident() not in live.threads
        assert live.loads == 2
        assert app.current_view == 'profile:mm'
        assert series_list.highlighted_item.identity == target.identity
        assert series_list.highlighted_item.state.to_review
        assert 't' not in footer_keys(app)
        assert target.identity in {i.identity for i in app.index.items('review')}


async def test_refresh_reports_progress_and_reloads(demo_root):
    app, live = make(demo_root)
    async with app.run_test(size=(180, 40)) as pilot:
        series_list = await settle(pilot)
        await pilot.press('down')
        selected = series_list.highlighted_item.identity
        assert 'r' in footer_keys(app)
        await pilot.press('r')
        series_list = await settle(pilot)
        assert live.refreshes == 1
        assert live.loads == 2
        assert threading.get_ident() not in live.threads
        assert series_list.highlighted_item.identity == selected
        assert 'Refreshed 1 list(s)' in app.status_text()


async def test_track_failure_is_shown_not_raised(demo_root):
    app, live = make(demo_root)
    live.fail = 'b4 review track failed: Could not retrieve series'
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        await pilot.press('t')
        await settle(pilot)
        assert 'Could not retrieve series' in app.status_text()
        assert live.loads == 1


async def test_hide_removes_series_and_keeps_position(demo_root):
    app, live = make(demo_root)
    async with app.run_test(size=(180, 40)) as pilot:
        series_list = await settle(pilot)
        await pilot.press('down')
        position = series_list.cursor
        target = series_list.highlighted_item
        following = app.index.items('all')[position + 1]
        assert 'x' in footer_keys(app)
        await pilot.press('x')
        series_list = await settle(pilot)
        assert target.identity in live.hidden
        assert target.identity not in {i.identity for i in app.index.items('all')}
        assert series_list.cursor == position
        assert series_list.highlighted_item.identity == following.identity
        assert [i.identity for i in app.index.items('hidden')] == [target.identity]
        assert 'Hidden' in app.status_text()


async def test_unhide_from_hidden_view(demo_root):
    app, live = make(demo_root)
    async with app.run_test(size=(180, 40)) as pilot:
        series_list = await settle(pilot)
        target = series_list.highlighted_item
        await pilot.press('x')
        await settle(pilot)
        app.select_view('hidden')
        await pilot.pause()
        assert series_list.highlighted_item.identity == target.identity
        bindings = {k: b.binding.description for k, b in app.screen.active_bindings.items()}
        assert bindings['x'] == 'unhide'
        await pilot.press('x')
        await settle(pilot)
        assert live.hidden == set()
        assert app.current_view == 'all'  # The Hidden view is gone once empty.


async def test_shift_r_marks_series_read(demo_root):
    app, live = make(demo_root)
    async with app.run_test(size=(180, 40)) as pilot:
        series_list = await settle(pilot)
        await pilot.press('down')
        target = series_list.highlighted_item
        assert 'R' in footer_keys(app)
        await pilot.press('R')
        series_list = await settle(pilot)
        assert live.read_calls == [target.identity]
        assert threading.get_ident() not in live.threads
        assert live.loads == 2
        assert series_list.highlighted_item.identity == target.identity
        assert 'Marked 3' in app.status_text()

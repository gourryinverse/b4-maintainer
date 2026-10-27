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
        self.untrack_calls = []
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

    def untrack(self, item):
        self.threads.add(threading.get_ident())
        self.untrack_calls.append(item.identity)
        self.tracked.discard(item.identity)
        return f'Archived: {item.series.subject}'


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
        assert not {'r', 't', 'dollar_sign'} & footer_keys(app)


async def test_track_marks_selection_and_keeps_place(demo_root):
    app, live = make(demo_root)
    async with app.run_test(size=(180, 40)) as pilot:
        series_list = await settle(pilot)
        app.select_view('profile:mm')
        target = next(i for i in app.index.items('profile:mm') if not i.state.to_review)
        series_list.select_identity(target.identity)
        await pilot.pause()
        assert not target.state.to_review
        assert 'dollar_sign' in footer_keys(app)
        assert 't' not in footer_keys(app)  # t still tracks; $ is the shown key.

        await pilot.press('t')
        series_list = await settle(pilot)
        assert live.track_calls == [target.identity]
        assert threading.get_ident() not in live.threads
        assert live.loads == 2
        assert app.current_view == 'profile:mm'
        assert series_list.highlighted_item.identity == target.identity
        assert series_list.highlighted_item.state.to_review
        assert target.identity in {i.identity for i in app.index.items('review')}


async def test_refresh_reports_progress_and_reloads(demo_root):
    app, live = make(demo_root)
    async with app.run_test(size=(180, 40)) as pilot:
        series_list = await settle(pilot)
        await pilot.press('down')
        selected = series_list.highlighted_item.identity
        assert 'f5' in footer_keys(app)
        await pilot.press('f5')
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


async def test_shift_m_marks_series_read(demo_root):
    app, live = make(demo_root)
    async with app.run_test(size=(180, 40)) as pilot:
        series_list = await settle(pilot)
        await pilot.press('down')
        target = series_list.highlighted_item
        assert 'M' in footer_keys(app)
        await pilot.press('M')
        series_list = await settle(pilot)
        assert live.read_calls == [target.identity]
        assert threading.get_ident() not in live.threads
        assert live.loads == 2
        assert series_list.highlighted_item.identity == target.identity
        assert 'Marked 3' in app.status_text()


async def test_dollar_tracks_then_untracks_after_confirmation(demo_root):
    app, live = make(demo_root)
    async with app.run_test(size=(180, 40)) as pilot:
        series_list = await settle(pilot)
        app.select_view('profile:mm')
        target = next(i for i in app.index.items('profile:mm') if not i.state.to_review)
        series_list.select_identity(target.identity)
        await pilot.press('dollar_sign')
        series_list = await settle(pilot)
        assert live.track_calls == [target.identity]
        assert series_list.highlighted_item.state.to_review
        bindings = {k: b.binding.description for k, b in app.screen.active_bindings.items()}
        assert bindings['dollar_sign'] == 'untrack'

        await pilot.press('dollar_sign')
        await pilot.pause()
        assert app.quit_prompt() == 'Untrack (archive in b4)? [y|N]'
        await pilot.press('n')
        await settle(pilot)
        assert live.untrack_calls == []

        await pilot.press('dollar_sign')
        await pilot.pause()
        await pilot.press('y')
        series_list = await settle(pilot)
        assert live.untrack_calls == [target.identity]
        assert threading.get_ident() not in live.threads
        assert not series_list.highlighted_item.state.to_review
        assert 'Archived' in app.status_text()
        assert target.identity not in {i.identity for i in app.index.items('review')}


class WatchActions:
    def __init__(self):
        import dataclasses as dc

        from b4_maintainer.fixtures import make_item, workspace_config
        from b4_maintainer.workspace import Workspace

        talk = make_item(subject='Path forward?')
        series = dc.replace(talk.series, identity='thread:root@example.org', discussion=True)
        self.item = dc.replace(talk, series=series)
        self.ws = Workspace.from_config(workspace_config())
        self.watched = False
        self.calls = 0

    def load(self):
        return Snapshot(self.ws, (dataclasses.replace(self.item, watched=self.watched),))

    def refresh(self, progress):
        return ''

    def toggle_watched(self, item):
        self.calls += 1
        self.watched = not item.watched
        return ('Watching: ' if self.watched else 'No longer watching: ') + item.series.subject


async def test_dollar_watches_and_unwatches_discussions_without_asking():
    actions = WatchActions()
    app = MaintainerApp(actions.load, actions=actions)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        app.select_view('discussions')
        await pilot.pause()
        bindings = {k: b.binding.description for k, b in app.screen.active_bindings.items()}
        assert bindings['dollar_sign'] == 'watch'
        await pilot.press('dollar_sign')
        series_list = await settle(pilot)
        assert actions.calls == 1 and series_list.highlighted_item.watched
        assert 'Watching' in app.status_text()
        bindings = {k: b.binding.description for k, b in app.screen.active_bindings.items()}
        assert bindings['dollar_sign'] == 'unwatch'
        await pilot.press('dollar_sign')
        await pilot.pause()
        assert app.quit_prompt() is None  # Nothing in b4 to archive: no prompt.
        series_list = await settle(pilot)
        assert actions.calls == 2 and not series_list.highlighted_item.watched
        await pilot.press('t')  # t tracks series only.
        await settle(pilot)
        assert actions.calls == 2


async def test_dollar_label_follows_the_selection_between_discussions():
    import dataclasses as dc

    from b4_maintainer.fixtures import make_item, workspace_config
    from b4_maintainer.workspace import Workspace

    def talk(subject, root, watched):
        item = make_item(subject=subject)
        series = dc.replace(item.series, identity=f'thread:{root}', discussion=True)
        return dc.replace(item, series=series, watched=watched)

    items = (talk('Followed', 'a@example.org', True), talk('Not followed', 'b@example.org', False))
    snapshot = Snapshot(Workspace.from_config(workspace_config()), items)
    app = MaintainerApp(lambda: snapshot, actions=WatchActions())
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        app.select_view('discussions')
        await pilot.pause()

        def label():
            return {k: b.binding.description for k, b in app.screen.active_bindings.items()}.get(
                'dollar_sign'
            )

        from unittest import mock

        item = app.query_one(SeriesList).highlighted_item
        assert label() == ('unwatch' if item.watched else 'watch')
        # The footer on screen is rebuilt only by refresh_bindings: it must run when $ changes.
        with mock.patch.object(app, 'refresh_bindings', wraps=app.refresh_bindings) as refresh:
            await pilot.press('down')
            await pilot.pause()
        item = app.query_one(SeriesList).highlighted_item
        assert label() == ('unwatch' if item.watched else 'watch')
        assert refresh.called, 'the footer kept the previous $ label'


async def test_dollar_works_while_reading_and_keeps_the_reader():
    actions = WatchActions()

    def thread_for(series):
        from b4_maintainer.model import Message, Post, SeriesThread

        msg = Message('root@example.org', 'Starter', 's@example.org', None, 'Path forward?', 'Hi.')
        return SeriesThread(series.identity, 0, Post(0, msg, 'Hi.', '', ()), (), discussion=True)

    app = MaintainerApp(actions.load, actions=actions, thread_loader=thread_for)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        app.select_view('discussions')
        await pilot.press('enter')
        await settle(pilot)
        assert app.reading is not None

        def label():
            return {k: b.binding.description for k, b in app.screen.active_bindings.items()}.get(
                'dollar_sign'
            )

        assert label() == 'watch'
        await pilot.press('dollar_sign')
        await settle(pilot)
        assert actions.calls == 1 and app.reading is not None and app.reading.watched
        assert app.tree_labels() and label() == 'unwatch'
        await pilot.press('dollar_sign')
        await settle(pilot)
        assert actions.calls == 2 and not app.reading.watched


async def test_dollar_tracks_the_series_being_read(demo_root):
    app, live = make(demo_root)
    async with app.run_test(size=(180, 40)) as pilot:
        series_list = await settle(pilot)
        app.select_view('profile:mm')
        target = next(i for i in app.index.items('profile:mm') if not i.state.to_review)
        series_list.select_identity(target.identity)
        await pilot.press('enter')
        await settle(pilot)
        await pilot.press('dollar_sign')
        await settle(pilot)
        assert live.track_calls == [target.identity]
        assert app.reading is not None and app.reading.state.to_review
        assert app.tree_labels()
        await pilot.press('dollar_sign')  # Untracking asks first, in the reader too.
        await pilot.pause()
        await pilot.press('y')
        await settle(pilot)
        assert live.untrack_calls == [target.identity]
        assert app.reading is not None and app.tree_labels()


async def test_f5_in_a_thread_refreshes_and_reloads_it_in_place(demo_root):
    from b4_maintainer.adapters.b4mbox import load_thread

    loads = []

    def counting(series):
        loads.append(series.identity)
        return load_thread(series)

    live = FakeLive(demo_root)
    app = MaintainerApp(live.load, actions=live, thread_loader=counting)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        await pilot.press('enter')
        await settle(pilot)
        item = app.reading
        assert item is not None and loads == [item.identity]
        await pilot.press('down')  # Somewhere in the thread.
        await pilot.pause()
        place = app._tree.highlighted
        assert 'f5' in footer_keys(app)
        await pilot.press('f5')
        await settle(pilot)
        assert live.refreshes == 1
        assert app.reading is not None and app.reading.identity == item.identity
        assert loads == [item.identity, item.identity]  # The thread itself, reloaded.
        assert app._tree.highlighted == place
        assert 'Refreshed 1 list(s)' in app.status_text()

"""Contract: the series list is newest posting first; l orders it by latest activity (a
reply or a posting) and back, keeping the selected series, across views and searches.
Ordered by activity, each row says how long ago its last message came."""

import dataclasses
import datetime

from b4_maintainer.adapters.fixture import FixtureSource
from b4_maintainer.app import MaintainerApp, SeriesList
from b4_maintainer.workspace import Snapshot


def snapshot(demo_root):
    """The demo, with the oldest posting replied to most recently."""
    base = FixtureSource(demo_root).load()
    posted = sorted(
        (i for i in base.items if i.series.sent), key=lambda i: i.series.sent, reverse=True
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    latest = {i.identity: now - datetime.timedelta(hours=n) for n, i in enumerate(reversed(posted))}
    items = tuple(dataclasses.replace(i, last_activity=latest.get(i.identity)) for i in base.items)
    return Snapshot(base.workspace, items)


async def settle(pilot):
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


def order(app):
    series_list = app.query_one(SeriesList)
    return [series_list._items[n].identity for n in range(series_list.row_count)]


async def test_l_orders_the_list_by_latest_activity_and_back(demo_root):
    snap = snapshot(demo_root)
    app = MaintainerApp(lambda: snap)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        posted = order(app)
        series_list = app.query_one(SeriesList)
        await pilot.press('down', 'down')
        chosen = series_list.highlighted_item.identity
        assert app.check_action('sort_latest', ()) and not app.check_action('sort_posted', ())
        await pilot.press('l')
        await settle(pilot)
        by_activity = order(app)
        items = {i.identity: i for i in snap.items}
        stamps = [items[i].last_activity or items[i].series.sent for i in by_activity]
        assert stamps == sorted(stamps, reverse=True) and by_activity != posted
        assert series_list.highlighted_item.identity == chosen  # Still selected.
        assert 'Last' in series_list._heading
        newest = series_list._items[0]
        assert newest.last_activity is not None
        assert '0m' in ''.join(text for text, _ in series_list._row(0))  # Just now.
        assert app.check_action('sort_posted', ()) and not app.check_action('sort_latest', ())
        assert 'latest activity' in app.status_text()

        app.select_view('review')  # Another view: still by activity.
        await settle(pilot)
        app.select_view('all')
        await settle(pilot)
        assert order(app) == by_activity

        await pilot.press('l')  # Back to newest posting first.
        await settle(pilot)
        assert order(app) == posted and 'Last' not in series_list._heading

"""Contract: `/` filters the series list by regex over subject and author."""

from b4_maintainer.adapters.fixture import FixtureSource
from b4_maintainer.app import MaintainerApp, SeriesList
from b4_maintainer.fixtures import make_item
from b4_maintainer.search import compile_query, matching


def subjects(items, text):
    query = compile_query(text)
    return sorted(i.series.subject for i in items if i.identity in matching(items, query))


ITEMS = (
    make_item(subject='mm: fix foo leak'),
    make_item(subject='mm: rework reclaim', author_name='Ann Example'),
    make_item(subject='cxl: add region', author_email='cxl-dev@example.org'),
    make_item(subject='MM: Shouting subject'),
)


def test_case_insensitive_regex():
    assert subjects(ITEMS, 'mm: (fix|rework)') == ['mm: fix foo leak', 'mm: rework reclaim']
    assert subjects(ITEMS, 'shouting') == ['MM: Shouting subject']
    assert subjects(ITEMS, '^cxl') == ['cxl: add region']


def test_capitals_are_case_insensitive_too():
    assert subjects(ITEMS, 'SHOUTING') == ['MM: Shouting subject']
    assert len(subjects(ITEMS, 'MM:')) == 3


def test_author_name_and_email_match():
    assert subjects(ITEMS, 'ann ex') == ['mm: rework reclaim']
    assert subjects(ITEMS, 'cxl-dev@') == ['cxl: add region']


def test_invalid_regex_falls_back_to_literal():
    query = compile_query('fix (')
    assert query.literal
    assert subjects(ITEMS, 'leak(') == []
    assert subjects(ITEMS, '(') == []
    assert not compile_query('fix').literal


def test_empty_query_matches_nothing_special():
    assert compile_query('').pattern is None


async def settle(pilot):
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()
    return pilot.app.query_one(SeriesList)


def rows(series_list):
    return [series_list._items[n].series.subject for n in range(series_list.row_count)]


async def test_slash_filters_live_and_escape_clears(demo_root):
    app = MaintainerApp(FixtureSource(demo_root).load)
    async with app.run_test(size=(180, 40)) as pilot:
        series_list = await settle(pilot)
        total = series_list.row_count
        await pilot.press('slash')
        await pilot.pause()
        assert app.query_one('#search').display
        await pilot.press(*'reclaim|virtio')
        await pilot.pause()
        assert sorted(rows(series_list)) == ['mm: rework reclaim thing', 'virtio: tidy queue setup']
        assert '/reclaim|virtio' in app.status_text()
        assert app.views_counts()['all'] == 2

        await pilot.press('enter')
        await pilot.pause()
        assert not app.query_one('#search').display
        assert app.focused is series_list
        app.select_view('profile:mm')
        await pilot.pause()
        assert rows(series_list) == ['mm: rework reclaim thing']

        await pilot.press('escape')
        await pilot.pause()
        app.select_view('all')
        await pilot.pause()
        assert series_list.row_count == total
        assert '/' not in app.status_text().split('—', 1)[1]


async def test_escape_in_search_line_clears_and_closes(demo_root):
    app = MaintainerApp(FixtureSource(demo_root).load)
    async with app.run_test(size=(180, 40)) as pilot:
        series_list = await settle(pilot)
        total = series_list.row_count
        await pilot.press('slash', *'foo')
        await pilot.pause()
        assert series_list.row_count < total
        await pilot.press('escape')
        await pilot.pause()
        assert not app.query_one('#search').display
        assert series_list.row_count == total
        assert app.focused is series_list


async def test_selection_survives_filtering_when_still_visible(demo_root):
    app = MaintainerApp(FixtureSource(demo_root).load)
    async with app.run_test(size=(180, 40)) as pilot:
        series_list = await settle(pilot)
        target = next(i for i in app.index.items('all') if 'reclaim' in i.series.subject)
        series_list.select_identity(target.identity)
        await pilot.press('slash', *'mm')
        await pilot.pause()
        assert series_list.highlighted_item.identity == target.identity


async def test_escape_closes_an_empty_search_line(demo_root):
    app = MaintainerApp(FixtureSource(demo_root).load)
    async with app.run_test(size=(180, 40)) as pilot:
        series_list = await settle(pilot)
        await pilot.press('slash')
        await pilot.pause()
        await pilot.press('escape')
        await pilot.pause()
        assert not app.query_one('#search').display
        assert app.focused is series_list

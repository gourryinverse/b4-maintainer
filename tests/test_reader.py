import threading
import time

from textual.app import App

from b4_maintainer.adapters.fixture import FixtureSource
from b4_maintainer.app import MaintainerApp, SeriesList
from b4_maintainer.fixtures import make_item, synthetic_snapshot, workspace_config
from b4_maintainer.model import Message, Post, Reply, SeriesThread
from b4_maintainer.widgets import Block, TextPane
from b4_maintainer.workspace import Snapshot, Workspace


def footer_keys(app):
    return {key for key, active in app.screen.active_bindings.items() if active.binding.show}


async def loaded(pilot):
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()
    return pilot.app.query_one(SeriesList)


async def open_reader(pilot):
    await pilot.press('enter')
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


async def test_enter_opens_reader_on_cover_and_escape_returns(demo_root):
    app = MaintainerApp(FixtureSource(demo_root).load)
    async with app.run_test(size=(180, 40)) as pilot:
        series_list = await loaded(pilot)
        item = series_list.highlighted_item
        await open_reader(pilot)

        assert app.reading is item
        assert app.visible_panes() == ()
        labels = app.post_labels()
        assert labels[0].startswith('0/5 cover')
        assert [label.split()[0] for label in labels[1:]] == ['1/5', '2/5', '3/5', '4/5', '5/5']
        text = app.content_text()
        assert 'Synthetic cover letter.' in text
        assert 'Acked-by: Reviewer One' in text

        await pilot.press('escape')
        await pilot.pause()
        assert app.reading is None
        assert app.visible_panes() == ('views', 'series', 'summary')
        assert series_list.highlighted_item is item


async def test_patch_toggles_between_diff_and_comments(demo_root):
    app = MaintainerApp(FixtureSource(demo_root).load)
    async with app.run_test(size=(180, 40)) as pilot:
        await loaded(pilot)
        await open_reader(pilot)
        await pilot.press('down', 'down', 'down')
        await pilot.pause()
        text = app.content_text()
        assert 'Synthetic change touching mm/foo3.c' in text
        assert 'diff --git a/mm/foo3.c b/mm/foo3.c' in text
        assert 'Reviewed-by: Reviewer Two' not in text

        await pilot.press('c')
        await pilot.pause()
        text = app.content_text()
        assert 'Reviewed-by: Reviewer Two' in text
        assert 'diff --git' not in text

        await pilot.press('down')
        await pilot.pause()
        assert 'No comments.' in app.content_text()

        await pilot.press('d')
        await pilot.pause()
        assert 'diff --git a/mm/foo4.c' in app.content_text()


async def test_reader_footer_shows_only_applicable_keys(demo_root):
    app = MaintainerApp(FixtureSource(demo_root).load)
    async with app.run_test(size=(180, 40)) as pilot:
        await loaded(pilot)
        await open_reader(pilot)
        keys = footer_keys(app)
        assert {'escape', 'tab', 'grave_accent', 'Q'} <= keys
        assert not {'d', 'c', 'v', 'p', 'enter'} & keys

        await pilot.press('down')
        await pilot.pause()
        keys = footer_keys(app)
        assert 'c' in keys and 'd' not in keys
        await pilot.press('c')
        await pilot.pause()
        keys = footer_keys(app)
        assert 'd' in keys and 'c' not in keys


async def test_drawer_line_stays_visible_and_pinned_while_reading(demo_root):
    app = MaintainerApp(FixtureSource(demo_root).load)
    async with app.run_test(size=(180, 40)) as pilot:
        series_list = await loaded(pilot)
        before = app.drawer_text()
        await pilot.press('down')
        await open_reader(pilot)
        assert app.reading is series_list.highlighted_item
        assert app.drawer_text() == before
        assert app.query_one('#drawer').display


async def test_reader_opens_within_target(demo_root):
    app = MaintainerApp(FixtureSource(demo_root).load)
    async with app.run_test(size=(180, 40)) as pilot:
        await loaded(pilot)
        start = time.perf_counter()
        await open_reader(pilot)
        assert 'Synthetic cover letter.' in app.content_text()
        assert time.perf_counter() - start <= 0.5


def stub_thread(item, **kwargs):
    msg = Message(
        'p1@example.org', 'Contributor One', 'contributor1@example.org', None, 'subject', 'Body.'
    )
    post = Post(1, msg, 'Body.', 'diff --git a/x b/x', ())
    return SeriesThread(item.identity, item.series.revision, None, (post,), **kwargs)


def single_item_app(loader):
    item = make_item(subject='mm: stub')
    snapshot = Snapshot(Workspace.from_config(workspace_config()), (item,))
    return item, MaintainerApp(lambda: snapshot, thread_loader=lambda s: loader(item, s))


async def test_thread_loads_off_ui_thread_and_errors_are_shown():
    seen = []

    def failing(item, series):
        seen.append(threading.current_thread() is threading.main_thread())
        raise RuntimeError('boom')

    _, app = single_item_app(failing)
    async with app.run_test(size=(180, 40)) as pilot:
        await loaded(pilot)
        await open_reader(pilot)
        text = app.content_text()
        assert 'Could not load thread' in text and 'boom' in text
    assert seen == [False]


async def test_unattached_replies_get_their_own_entry():
    lost = Message(
        'lost@example.org', 'Reviewer One', 'reviewer1@example.org', None, 'Re: x', 'Lost reply.'
    )
    _, app = single_item_app(lambda item, s: stub_thread(item, unattached=(Reply(lost, 0),)))
    async with app.run_test(size=(180, 40)) as pilot:
        await loaded(pilot)
        await open_reader(pilot)
        assert app.post_labels()[-1] == 'Unattached (1)'
        await pilot.press('end')
        await pilot.pause()
        assert 'Lost reply.' in app.content_text()


async def test_series_without_mail_source_reports_it():
    snapshot = synthetic_snapshot(3)
    app = MaintainerApp(lambda: snapshot)
    async with app.run_test(size=(180, 40)) as pilot:
        await loaded(pilot)
        await open_reader(pilot)
        assert 'no mail source' in app.content_text()


async def test_diff_lines_scroll_sideways_while_prose_wraps():
    class Host(App):
        def compose(self):
            yield TextPane(id='pane')

    app = Host()
    async with app.run_test(size=(60, 20)) as pilot:
        pane = app.query_one(TextPane)
        pane.set_blocks([Block('+' + 'x' * 300, wrap=False), Block('word ' * 60)])
        await pilot.pause()
        width = pane.scrollable_content_region.width
        assert pane.virtual_size.width >= 301 > width
        assert pane.virtual_size.height > 3


async def test_titles_and_labels_render_brackets_literally(tmp_path):
    from textual.content import Content
    from textual.widgets import OptionList

    from b4_maintainer.fixtures import build_demo_workspace

    profiles = [
        {'id': 'mm', 'label': '[mm] core', 'project': 'linux', 'lists': ['linux-mm.kvack.org']}
    ]
    root = build_demo_workspace(tmp_path / 'demo', profiles=profiles)
    app = MaintainerApp(FixtureSource(root).load)
    async with app.run_test(size=(180, 40)) as pilot:
        await loaded(pilot)
        views = app.query_one('#views', OptionList)
        prompts = [
            Content.from_markup(str(views.get_option_at_index(i).prompt)).plain
            for i in range(views.option_count)
        ]
        assert any(p.startswith('[mm] core') for p in prompts)

        await open_reader(pilot)
        await pilot.press('down')
        await pilot.pause()
        title = Content.from_markup(str(app.query_one('#content').border_title)).plain
        assert '[c] comments' in title


async def test_patch_label_and_content_include_carried_tags(demo_root):
    app = MaintainerApp(FixtureSource(demo_root).load)
    async with app.run_test(size=(180, 40)) as pilot:
        await loaded(pilot)
        await open_reader(pilot)
        assert app.post_labels()[2] == '2/5 ·R·'
        await pilot.press('down', 'down')
        await pilot.pause()
        assert (
            'Trailers: A:0 R:1 T:0 (carried A:0 R:1 T:0 · replies A:0 R:0 T:0)'
            in app.content_text()
        )

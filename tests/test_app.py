import threading

from b4_maintainer.adapters.fixture import FixtureSource
from b4_maintainer.app import MaintainerApp, SeriesList
from b4_maintainer.layout import Breakpoints


class SpyLauncher:
    def __init__(self):
        self.launches = []

    def launch(self, request):
        self.launches.append(request)


def make_app(demo_root, **kwargs):
    return MaintainerApp(FixtureSource(demo_root).load, **kwargs)


async def loaded(pilot):
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()
    return pilot.app.query_one(SeriesList)


async def test_startup_lists_series_and_summarizes_selection(demo_root):
    app = make_app(demo_root)
    async with app.run_test(size=(180, 40)) as pilot:
        series_list = await loaded(pilot)
        assert series_list.row_count == app.index.count('all') > 0
        first = series_list.highlighted_item
        assert first.series.subject in app.summary_text()
        await pilot.press('down')
        assert series_list.highlighted_item is not first
        assert series_list.highlighted_item.series.subject in app.summary_text()


async def test_switching_views_filters_rows_with_matching_counts(demo_root):
    app = make_app(demo_root)
    async with app.run_test(size=(180, 40)) as pilot:
        series_list = await loaded(pilot)
        for view in app.index.views:
            app.select_view(view.id)
            await pilot.pause()
            assert series_list.row_count == app.index.count(view.id), view.id


async def test_passive_actions_launch_no_agents(demo_root):
    spy = SpyLauncher()
    app = make_app(demo_root, launcher=spy)
    async with app.run_test(size=(180, 40)) as pilot:
        await loaded(pilot)
        await pilot.press('down', 'down', 'up', 'end', 'home')
        for view in app.index.views:
            app.select_view(view.id)
        await pilot.resize_terminal(90, 30)
        await pilot.press('enter', 'escape')
        await pilot.pause()
    assert spy.launches == []


async def test_loading_runs_off_the_ui_thread(demo_root):
    seen = []
    source = FixtureSource(demo_root)

    def load():
        seen.append(threading.current_thread() is threading.main_thread())
        return source.load()

    app = MaintainerApp(load)
    async with app.run_test(size=(180, 40)) as pilot:
        await loaded(pilot)
    assert seen == [False]


async def test_layout_follows_breakpoints(demo_root):
    app = make_app(demo_root, breakpoints=Breakpoints(160, 100))
    async with app.run_test(size=(180, 40)) as pilot:
        await loaded(pilot)
        assert app.visible_panes() == ('views', 'series', 'summary')
        await pilot.resize_terminal(130, 40)
        await pilot.pause()
        assert app.visible_panes() == ('series', 'summary')
        await pilot.press('v')
        await pilot.pause()
        assert app.visible_panes() == ('views', 'series', 'summary')
        await pilot.resize_terminal(90, 40)
        await pilot.pause()
        assert app.visible_panes() == ('series',)
        await pilot.press('p')
        await pilot.pause()
        assert app.visible_panes() == ('summary',)
        await pilot.press('escape')
        await pilot.pause()
        assert app.visible_panes() == ('series',)


async def test_drawer_line_stays_pinned_to_session_series(demo_root):
    app = make_app(demo_root)
    async with app.run_test(size=(180, 40)) as pilot:
        series_list = await loaded(pilot)
        session = app.snapshot.workspace.sessions[0]
        pinned = app.snapshot.item_by_identity(session.series)
        before = app.drawer_text()
        assert pinned.series.subject in before
        await pilot.press('down', 'down')
        assert series_list.highlighted_item is not pinned
        assert app.drawer_text() == before


async def test_zero_profiles_shows_setup_hint(tmp_path):
    from b4_maintainer.fixtures import build_demo_workspace

    root = build_demo_workspace(tmp_path / 'noprof', profiles=[])
    app = MaintainerApp(FixtureSource(root).load)
    async with app.run_test(size=(180, 40)) as pilot:
        await loaded(pilot)
        assert 'No subsystem profiles' in app.status_text()


async def test_view_labels_fit_on_one_line(demo_root):
    from textual.widgets import OptionList

    app = make_app(demo_root)
    async with app.run_test(size=(170, 20)) as pilot:
        await loaded(pilot)
        app.action_toggle_drawer()
        await pilot.pause()
        views = app.query_one('#views', OptionList)
        assert views.virtual_size.height == len(app.index.views)


def footer_keys(app):
    return {key for key, active in app.screen.active_bindings.items() if active.binding.show}


async def test_footer_shows_only_applicable_keys(demo_root):
    app = make_app(demo_root)
    async with app.run_test(size=(180, 40)) as pilot:
        await loaded(pilot)
        keys = footer_keys(app)
        assert {'enter', 'grave_accent', 'tab', 'p', 'Q'} <= keys
        assert not {'v', 'escape'} & keys

        await pilot.resize_terminal(130, 40)
        await pilot.pause()
        assert {'v', 'tab', 'grave_accent'} <= footer_keys(app)

        await pilot.resize_terminal(90, 40)
        await pilot.pause()
        keys = footer_keys(app)
        assert {'enter', 'grave_accent', 'p'} <= keys
        assert not {'v', 'tab', 'escape'} & keys

        await pilot.press('p')
        await pilot.pause()
        keys = footer_keys(app)
        assert {'escape', 'enter', 'grave_accent'} <= keys


async def test_cover_letter_hidden_until_preview_expands_pane(demo_root):
    app = make_app(demo_root)
    async with app.run_test(size=(180, 30)) as pilot:
        series_list = await loaded(pilot)
        summary = app.query_one('#summary')
        default_width = summary.scrollable_content_region.width
        assert series_list.highlighted_item.series.preview_label == 'Cover letter'
        assert 'Synthetic cover letter' not in app.summary_text()
        assert default_width < 100

        await pilot.press('p')
        await pilot.pause()
        assert 'Synthetic cover letter' in app.summary_text()
        assert summary.scrollable_content_region.width == 100

        await pilot.press('p')
        await pilot.pause()
        assert 'Synthetic cover letter' not in app.summary_text()
        assert summary.scrollable_content_region.width == default_width


async def test_preview_follows_selection_and_shows_single_patch_message(demo_root):
    app = make_app(demo_root)
    async with app.run_test(size=(180, 30)) as pilot:
        series_list = await loaded(pilot)
        await pilot.press('p')
        while not series_list.highlighted_item.series.subject.startswith('selftests/mm'):
            await pilot.press('down')
        await pilot.pause()
        text = app.summary_text()
        assert 'Patch 1/1 (no cover letter)' in text
        assert 'Synthetic change touching tools/testing/selftests/mm/merge.c' in text


async def test_expanded_preview_scrolls_and_resets_on_selection(demo_root):
    app = make_app(demo_root)
    async with app.run_test(size=(180, 30)) as pilot:
        await loaded(pilot)
        await pilot.press('p')
        summary = app.query_one('#summary')
        await pilot.press('tab')
        assert app.focused is summary
        await pilot.press('pagedown')
        await pilot.pause()
        assert summary.scroll_y > 0

        await pilot.press('shift+tab')
        await pilot.press('down')
        await pilot.pause()
        assert summary.scroll_y == 0


async def test_summary_strips_terminal_control_sequences():
    from b4_maintainer.fixtures import make_item, workspace_config
    from b4_maintainer.workspace import Snapshot, Workspace

    item = make_item(subject='mm: hostile', preview='before\x1b[2J\x1b]0;title\x07after\tok')
    snapshot = Snapshot(Workspace.from_config(workspace_config()), (item,))
    app = MaintainerApp(lambda: snapshot)
    async with app.run_test(size=(180, 30)) as pilot:
        await loaded(pilot)
        await pilot.press('p')
        text = app.summary_text()
        assert 'before' in text and 'after' in text
        assert '\x1b' not in text and '\x07' not in text


async def test_focused_pane_is_visibly_marked(demo_root):
    app = make_app(demo_root)
    async with app.run_test(size=(180, 30)) as pilot:
        await loaded(pilot)
        panes = {name: app.query_one(f'#{name}') for name in ('views', 'series', 'summary')}

        def marked():
            colors = {name: w.styles.border_top[1] for name, w in panes.items()}
            focused = [n for n, c in colors.items() if list(colors.values()).count(c) == 1]
            return focused[0] if len(focused) == 1 else None

        assert marked() == 'series'
        await pilot.press('tab')
        await pilot.pause()
        assert marked() == 'summary'
        await pilot.press('tab')
        await pilot.pause()
        assert marked() == 'views'
        sizes = {n: w.size for n, w in panes.items()}
        await pilot.press('tab')
        await pilot.pause()
        assert {n: w.size for n, w in panes.items()} == sizes


async def test_quit_requires_shift_q_and_confirmation(demo_root):
    app = make_app(demo_root)
    exits = []
    app.exit = lambda *args, **kwargs: exits.append(args)
    async with app.run_test(size=(180, 30)) as pilot:
        await loaded(pilot)
        keys = footer_keys(app)
        assert 'Q' in keys and 'q' not in keys

        await pilot.press('q')
        await pilot.pause()
        assert app.quit_prompt() is None and exits == []

        for cancel in ('n', 'enter', 'escape', 'p', 'tab'):
            await pilot.press('Q')
            await pilot.pause()
            assert app.quit_prompt() == 'Quit? [y|N]'
            await pilot.press(cancel)
            await pilot.pause()
            assert app.quit_prompt() is None, cancel
        assert exits == []
        assert 'Synthetic cover letter' not in app.summary_text()

        await pilot.press('Q')
        await pilot.press('y')
        await pilot.pause()
        assert exits


async def test_ctrl_q_also_asks_before_quitting(demo_root):
    app = make_app(demo_root)
    exits = []
    app.exit = lambda *args, **kwargs: exits.append(args)
    async with app.run_test(size=(180, 30)) as pilot:
        await loaded(pilot)
        await pilot.press('ctrl+q')
        await pilot.pause()
        assert app.quit_prompt() == 'Quit? [y|N]'
        await pilot.press('N')
        await pilot.pause()
        assert app.quit_prompt() is None and exits == []


def sashiko_app():
    from b4_maintainer.fixtures import make_item, workspace_config
    from b4_maintainer.model import Finding, SashikoPatch, SashikoReview
    from b4_maintainer.workspace import Snapshot, Workspace

    review = SashikoReview(
        'Reviewed',
        high=1,
        url='https://sashiko.example.org/#/patchset/7',
        baseline='mm/mm-new @ 59d7f9ec14ad',
        model='synthetic-model',
        patches=(
            SashikoPatch('p1@example.org', 1, 'Reviewed', (Finding('high', 'mm: leaks foo'),)),
            SashikoPatch(
                'p2@example.org', 2, 'Reviewed', (Finding('low', 'mm: old bug', preexisting=True),)
            ),
        ),
    )
    items = (
        make_item(subject='mm: reviewed', sashiko=review),
        make_item(subject='mm: unseen'),
    )
    snapshot = Snapshot(Workspace.from_config(workspace_config()), items)
    return MaintainerApp(lambda: snapshot)


def screen_line(widget, y):
    return ''.join(seg.text for seg in widget.render_line(y))


async def test_series_list_has_a_heading_row():
    app = sashiko_app()
    async with app.run_test(size=(180, 30)) as pilot:
        series_list = await loaded(pilot)
        head = screen_line(series_list, 0)
        assert 'Patch' in head and 'AI' in head and 'Author' in head and 'Subject' in head
        assert 'mm: reviewed' in screen_line(series_list, 1)
        await pilot.press('end')
        await pilot.pause()
        assert 'Subject' in screen_line(series_list, 0)  # The heading stays put.
        await pilot.click(SeriesList, offset=(5, 1))
        assert series_list.highlighted_item.series.subject == 'mm: reviewed'
        await pilot.click(SeriesList, offset=(5, 0))  # Clicking the heading selects nothing.
        assert series_list.highlighted_item.series.subject == 'mm: reviewed'


async def test_summary_shows_sashiko_review():
    app = sashiko_app()
    async with app.run_test(size=(180, 30)) as pilot:
        await loaded(pilot)
        text = app.summary_text()
        assert 'Sashiko:  Reviewed · 1 high' in text
        assert 'mm/mm-new @ 59d7f9ec14ad' in text and 'synthetic-model' in text
        assert 'https://sashiko.example.org/#/patchset/7' in text
        assert '1/2 high: mm: leaks foo' in text
        assert '2/2 low (pre-existing): mm: old bug' in text
        await pilot.press('down')
        await pilot.pause()
        assert 'Sashiko:  not reviewed' in app.summary_text()


async def test_summary_colours_sashiko_findings_by_severity():
    from b4_maintainer.reader import SEVERITY_STYLES

    app = sashiko_app()
    async with app.run_test(size=(180, 30)) as pilot:
        await loaded(pilot)
        styles = dict(app.query_one('#summary').styled_lines)
        assert styles['Sashiko:  Reviewed · 1 high'] == SEVERITY_STYLES['high']
        assert styles['  1/2 high: mm: leaks foo'] == SEVERITY_STYLES['high']
        assert styles['  2/2 low (pre-existing): mm: old bug'] == SEVERITY_STYLES['low']


async def test_key_bar_shows_enabled_keys_and_follows_changes(demo_root):
    from b4_maintainer.widgets import KeyBar

    app = make_app(demo_root)
    async with app.run_test(size=(180, 40)) as pilot:
        await loaded(pilot)
        bar = app.query_one(KeyBar)

        def hints():
            return ' '.join(str(bar.render()).split())

        assert '⏎ read' in hints() and 'Q quit' in hints()
        assert 'send' not in hints()  # Sending applies only with a draft open.
        await pilot.press('enter')
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert '⏎ read' not in hints() and '[ prev' in hints()

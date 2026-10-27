"""Contract: three tabs with their keys: F2 LKML (the series list and threads), F3
Workbench, F4 Artifacts (bases, session trees, prep branches). Each screen shows the tab
bar; switching keeps each tab's place; F4 in a thread shows that series' session output
in place; `?` lists every key of the current tab, the footer only the common ones."""

from test_branch_screen import FakeBranches
from test_workbench_screen import BENCH, FakeService

from b4_maintainer.adapters.fixture import FixtureSource
from b4_maintainer.app import MaintainerApp, Notice
from b4_maintainer.branchscreen import BranchesScreen
from b4_maintainer.widgets import TabBar
from b4_maintainer.workbench import WorkbenchStore
from b4_maintainer.workbenchscreen import WorkbenchScreen


def make(demo_root, tmp_path):
    store = WorkbenchStore(tmp_path / 'state' / 'workbenches')
    store.save(BENCH)
    app = MaintainerApp(
        FixtureSource(demo_root).load,
        branches=FakeBranches(),
        workbenches=FakeService(tmp_path, store, lambda: []),
    )
    return app


async def settle(pilot):
    for _ in range(3):
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()


def tab_text(app):
    return app.screen.query_one(TabBar).text()


def footer_keys(app):
    return {key for key, active in app.screen.active_bindings.items() if active.binding.show}


async def test_every_tab_shows_the_bar_with_itself_marked(demo_root, tmp_path):
    app = make(demo_root, tmp_path)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        assert tab_text(app) == ' F2 LKML  F3 Workbench  F4 Branches '
        assert app.screen.query_one(TabBar).current == 'lkml'
        await pilot.press('f3')
        await settle(pilot)
        assert isinstance(app.screen, WorkbenchScreen)
        assert app.screen.query_one(TabBar).current == 'workbench'
        await pilot.press('f4')
        await settle(pilot)
        assert isinstance(app.screen, BranchesScreen)
        assert app.screen.query_one(TabBar).current == 'branches'
        await pilot.press('f2')
        await settle(pilot)
        assert not isinstance(app.screen, (WorkbenchScreen, BranchesScreen))
        assert len(app.screen_stack) == 1


async def test_switching_keeps_each_tabs_place(demo_root, tmp_path):
    app = make(demo_root, tmp_path)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        await pilot.press('down', 'enter')  # A thread open in LKML.
        await settle(pilot)
        reading = app.reading
        assert reading is not None
        await pilot.press('escape')  # Back to the list, still in LKML...
        await pilot.press('enter')  # ...and the thread again.
        await settle(pilot)
        await pilot.press('f3')
        await settle(pilot)
        workbench = app.screen
        await pilot.press('tab')  # The sessions pane.
        await pilot.press('f4')
        await settle(pilot)
        artifacts = app.screen
        await pilot.press('down')  # The second item.
        await settle(pilot)
        chosen = artifacts.selected_item()
        await pilot.press('f3')
        await settle(pilot)
        assert app.screen is workbench and workbench.focused.id == 'bench-sessions'
        await pilot.press('f4')
        await settle(pilot)
        assert app.screen is artifacts and artifacts.selected_item() == chosen
        await pilot.press('escape')  # Escape goes back to LKML, as it was.
        await settle(pilot)
        assert app.reading is not None and app.reading.identity == reading.identity


async def test_f4_in_a_thread_shows_that_series_output_in_place(demo_root, tmp_path):
    from sessiondata import FakeSessionRunner, FakeWorktrees

    from b4_maintainer.sessions.hub import SessionHub
    from b4_maintainer.sessions.store import SessionStore

    sessions = SessionStore(tmp_path / 'var' / 'sessions')
    hub = SessionHub(sessions, FakeSessionRunner(sessions), FakeWorktrees())
    app = MaintainerApp(FixtureSource(demo_root).load, sessions=hub, branches=FakeBranches())
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        await pilot.press('enter')
        await settle(pilot)
        await pilot.press('f4')
        await settle(pilot)
        assert app.artifacts_shown() and not isinstance(app.screen, BranchesScreen)
        await pilot.press('f4')
        await settle(pilot)
        assert not app.artifacts_shown()


async def test_question_mark_lists_every_key_and_the_footer_only_common_ones(demo_root, tmp_path):
    app = make(demo_root, tmp_path)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        shown = footer_keys(app)
        assert {'enter', 'slash', 'question_mark', 'Q'} <= shown
        assert not {'M', 'w', 'a', 'd', 'z', 'f2', 'f3', 'f4'} & shown  # In ? and the tabs.
        await pilot.press('question_mark')
        await settle(pilot)
        help_screen = app.screen
        assert isinstance(help_screen, Notice)
        assert 'a  apply' in help_screen.prompt and 'w  to workbench' in help_screen.prompt
        assert 'Scroll Left' not in help_screen.prompt  # Textual's own keys stay out.
        assert 'F3  Workbench' in help_screen.prompt
        await pilot.press('escape')
        await settle(pilot)
        assert not isinstance(app.screen, Notice)


async def test_a_click_on_a_tab_switches_to_it(demo_root, tmp_path):
    app = make(demo_root, tmp_path)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        bar = app.screen.query_one(TabBar)
        start = bar.text().index('F3')
        await pilot.click(TabBar, offset=(start + 1, 0))
        await settle(pilot)
        assert isinstance(app.screen, WorkbenchScreen)


async def test_f4_is_branches_without_session_trees(demo_root, tmp_path):
    app = make(demo_root, tmp_path)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        assert 'F4 Branches' in tab_text(app)
        await pilot.press('f4')
        await settle(pilot)
        assert app.screen.service.trees_asked == [False]

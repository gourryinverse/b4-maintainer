"""Contract: F3 opens the Workbench screen (workbenches | the selected one's projects | that
project's items, with a preview of what a launch prepares) and `w` adds a series to a
workbench from the series list or a thread."""

from b4_maintainer.adapters.fixture import FixtureSource
from b4_maintainer.app import Confirm, MaintainerApp
from b4_maintainer.branchscreen import Ask, Pick
from b4_maintainer.workbench import Entry, Item, Workbench, WorkbenchService, WorkbenchStore
from b4_maintainer.workbenchscreen import WorkbenchScreen

CXL = Item('change-id:cxl', 3, 'cxl: region mode')
FIXES = Item(branch='gourry/fixes')
BENCH = Workbench('cxl', 'CXL', (Entry('linux', 'gourry/base', (CXL, FIXES)),))


class FakeService(WorkbenchService):
    """The real store; scripted choices and preview (no checkouts in the demo)."""

    def base_choices(self, workspace, project):
        return [('gourry/base', 'gourry/base'), ('mainline/master', 'mainline/master')]

    def branch_choices(self, workspace, project):
        return [('gourry/fixes', 'gourry/fixes'), ('tree:s1:b4m/fixup/x', 'b4m/fixup/x (s1)')]

    def preview(self, workspace, bench, series, latest):
        return 'preview: ' + '; '.join(f'{e.project} {len(e.items)}' for e in bench.entries)


def make(demo_root, tmp_path, benches=()):
    store = WorkbenchStore(tmp_path / 'state' / 'workbenches')
    for bench in benches:
        store.save(bench)
    service = FakeService(tmp_path, store, lambda: [])
    app = MaintainerApp(FixtureSource(demo_root).load, workbenches=service)
    return app, store


async def settle(pilot):
    for _ in range(3):
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()


async def open_screen(pilot):
    await settle(pilot)
    await pilot.press('f3')
    await settle(pilot)
    screen = pilot.app.screen
    assert isinstance(screen, WorkbenchScreen)
    return screen


async def test_f3_lists_workbenches_and_previews_the_selected_one(demo_root, tmp_path):
    app, _store = make(demo_root, tmp_path, [BENCH, Workbench('empty', 'Empty')])
    async with app.run_test(size=(180, 40)) as pilot:
        screen = await open_screen(pilot)
        assert screen.bench_labels() == ['CXL · 1 project', 'Empty · 0 projects']
        assert screen.entry_labels() == ['linux on gourry/base']
        assert screen.item_labels() == ['cxl: region mode v3', 'gourry/fixes']
        assert screen.preview_text() == 'preview: linux 2'
        await pilot.press('escape')
        await settle(pilot)
        assert not isinstance(app.screen, WorkbenchScreen)


async def test_n_makes_a_workbench_r_renames_it_x_deletes_it(demo_root, tmp_path):
    app, store = make(demo_root, tmp_path)
    async with app.run_test(size=(180, 40)) as pilot:
        screen = await open_screen(pilot)
        await pilot.press('n')
        await settle(pilot)
        assert isinstance(app.screen, Ask)
        await pilot.press(*'CXL tests', 'enter')
        await settle(pilot)
        assert store.get('cxl-tests') == Workbench('cxl-tests', 'CXL tests')
        await pilot.press('r')
        await settle(pilot)
        app.screen.query_one('Input').value = 'CXL and DAX'
        await pilot.press('enter')
        await settle(pilot)
        assert store.get('cxl-tests').label == 'CXL and DAX'
        await pilot.press('x')
        await settle(pilot)
        assert isinstance(app.screen, Confirm)
        await pilot.press('y')
        await settle(pilot)
        assert store.get('cxl-tests') is None and screen.bench_labels() == []


async def test_a_projects_base_is_picked_and_items_move_go_and_reroll(demo_root, tmp_path):
    app, store = make(demo_root, tmp_path, [BENCH])
    snapshot_latest = {CXL.series: 4}
    async with app.run_test(size=(180, 40)) as pilot:
        screen = await open_screen(pilot)
        screen.latest = snapshot_latest
        await pilot.press('tab')  # Projects.
        await pilot.press('b')
        await settle(pilot)
        assert isinstance(app.screen, Pick)
        await pilot.press('down', 'enter')
        await settle(pilot)
        assert store.get('cxl').entries[0].base == 'mainline/master'
        await pilot.press('tab')  # Items.
        await pilot.press('shift+down')
        await settle(pilot)
        assert store.get('cxl').entries[0].items == (FIXES, CXL)
        assert screen.item_index() == 1  # The cursor moved with it.
        await pilot.press('v')
        await settle(pilot)
        assert store.get('cxl').entries[0].items[1].revision == 4
        await pilot.press('x')
        await settle(pilot)
        assert store.get('cxl').entries[0].items == (FIXES,)
        await pilot.press('B')
        await settle(pilot)
        assert isinstance(app.screen, Pick)
        await pilot.press('down', 'enter')
        await settle(pilot)
        assert store.get('cxl').entries[0].items == (FIXES, Item(branch='tree:s1:b4m/fixup/x'))


async def test_removing_a_project_asks_first(demo_root, tmp_path):
    app, store = make(demo_root, tmp_path, [BENCH])
    async with app.run_test(size=(180, 40)) as pilot:
        screen = await open_screen(pilot)
        await pilot.press('tab', 'x')
        await settle(pilot)
        assert isinstance(app.screen, Confirm) and 'linux' in app.screen.prompt
        await pilot.press('y')
        await settle(pilot)
        assert store.get('cxl').entries == () and screen.entry_labels() == []


async def test_w_adds_the_series_to_a_workbench_or_a_new_one(demo_root, tmp_path):
    app, store = make(demo_root, tmp_path, [BENCH])
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        item = app.query_one('SeriesList').highlighted_item
        await pilot.press('w')
        await settle(pilot)
        assert isinstance(app.screen, Pick)
        assert [key for key, _label in app.screen.choices] == ['cxl', '+new']
        await pilot.press('enter')
        await settle(pilot)
        entry = store.get('cxl').entry(item.project)
        assert entry is not None and entry.items[-1].series == item.identity
        assert f'Added {item.series.subject}' in app.status_text()
        await pilot.press('down')
        other = app.query_one('SeriesList').highlighted_item
        await pilot.press('w')
        await settle(pilot)
        await pilot.press('down', 'enter')  # A new workbench.
        await settle(pilot)
        assert isinstance(app.screen, Ask)
        await pilot.press(*'Fresh', 'enter')
        await settle(pilot)
        fresh = store.get('fresh')
        assert fresh is not None and fresh.entry(other.project).items[0].series == other.identity


async def test_the_apps_own_keys_wait_while_the_screen_is_open(demo_root, tmp_path):
    app, _store = make(demo_root, tmp_path, [BENCH])
    async with app.run_test(size=(140, 40)) as pilot:  # Medium: v toggles the views.
        await open_screen(pilot)
        main = app.screen_stack[0]
        shown = main.has_class('-show-views')
        await pilot.press('v')  # Not a reroll here (the list pane): nothing happens.
        await settle(pilot)
        assert isinstance(app.screen, WorkbenchScreen)
        assert main.has_class('-show-views') == shown


def session_app(demo_root, tmp_path, benches):
    from sessiondata import FakeSessionRunner, FakeWorktrees

    from b4_maintainer.sessions.hub import SessionHub
    from b4_maintainer.sessions.store import SessionStore

    store = WorkbenchStore(tmp_path / 'state' / 'workbenches')
    for bench in benches:
        store.save(bench)
    sessions = SessionStore(tmp_path / 'var' / 'sessions')
    runner, trees = FakeSessionRunner(sessions), FakeWorktrees()
    hub = SessionHub(sessions, runner, trees, sandbox=lambda: True)
    service = FakeService(tmp_path, store, hub.records)
    app = MaintainerApp(FixtureSource(demo_root).load, sessions=hub, workbenches=service)
    return app, runner, trees


def demo_bench(app):
    """A workbench of the demo's first two cached series, on Linux."""
    a, b = app.snapshot.items[0], app.snapshot.items[1]
    items = tuple(Item(i.identity, i.series.revision, i.series.subject) for i in (a, b))
    return Workbench('demo', 'Demo', (Entry('linux', 'HEAD', items),)), (a, b)


async def test_f1_on_a_workbench_launches_a_session_over_its_trees(demo_root, tmp_path):
    from b4_maintainer.launchdialog import LaunchDialog

    app, runner, trees = session_app(demo_root, tmp_path, [])
    async with app.run_test(size=(180, 50)) as pilot:
        await settle(pilot)
        bench, (a, b) = demo_bench(app)
        app._workbenches.store.save(bench)
        await open_screen(pilot)
        await pilot.press('f1')
        await settle(pilot)
        dialog = app.screen
        assert isinstance(dialog, LaunchDialog)
        assert 'Mode' in dialog.labels() and 'base' not in dialog._inputs  # Bases: per tree.
        dialog.pick('goal', 'review')
        dialog.pick('mode', 'worktree')
        await pilot.pause()
        text = dialog.summary_text()
        assert 'Trees:' in text and 'linux on HEAD' in text and a.series.subject in text
        await pilot.press('ctrl+s')
        await settle(pilot)
        [(sid, plan)] = trees.entries
        assert plan.project == 'linux' and [i.source for i in plan.items] == [
            a.series.source,
            b.series.source,
        ]
        [(launched, _request)] = runner.launches
        assert launched == sid
        assert 'workbench Demo' in app.status_text()


async def test_a_tree_that_conflicts_is_offered_to_the_agent(demo_root, tmp_path):
    app, runner, trees = session_app(demo_root, tmp_path, [])
    async with app.run_test(size=(180, 50)) as pilot:
        await settle(pilot)
        bench, _items = demo_bench(app)
        app._workbenches.store.save(bench)
        trees.fail_entry = (
            'linux',
            ('mm: x does not apply at HEAD', 'CONFLICT in mm/x.c', 'c' * 40),
        )
        await open_screen(pilot)
        await pilot.press('f1')
        await settle(pilot)
        app.screen.pick('goal', 'review')
        app.screen.pick('mode', 'worktree')
        await pilot.pause()
        await pilot.press('ctrl+s')
        await settle(pilot)
        offer = app.screen
        assert isinstance(offer, Pick) and 'CONFLICT in mm/x.c' in offer.title_text
        assert "linux's tree" in offer.choices[0][1]
        trees.fail_entry = None
        await pilot.press('enter')  # Hand it to the agent.
        await settle(pilot)
        [(_sid, plan)] = trees.entries
        assert plan.agent_apply == 'c' * 40
        assert len(runner.launches) == 1


async def test_a_lightweight_workbench_session_reads_the_mail_without_trees(demo_root, tmp_path):
    from b4_maintainer.launchdialog import LaunchDialog

    app, runner, trees = session_app(demo_root, tmp_path, [])
    async with app.run_test(size=(180, 50)) as pilot:
        await settle(pilot)
        bench, (a, b) = demo_bench(app)
        gone = Item(branch='tree:nosuch:b4m/fixup/x')  # Matters to trees only.
        bench = Workbench(
            bench.id, bench.label, (Entry('linux', 'HEAD', (*bench.entries[0].items, gone)),)
        )
        app._workbenches.store.save(bench)
        await open_screen(pilot)
        await pilot.press('f1')
        await settle(pilot)
        dialog = app.screen
        assert isinstance(dialog, LaunchDialog)
        dialog.pick('goal', 'review')
        dialog.pick('mode', 'lightweight')
        await pilot.pause()
        text = dialog.summary_text()
        assert 'Mailboxes:' in text and 'Trees:' not in text
        assert f'{a.series.subject} v{a.series.revision} (linux)' in text
        assert 'trees only: tree:nosuch:b4m/fixup/x' in text
        await pilot.press('ctrl+s')
        await settle(pilot)
        assert trees.entries == []
        [(sid, _request)] = runner.launches
        record = app._sessions.store.record(sid)
        assert record.mode == 'lightweight' and record.trees == ()
        assert record.workbench == 'demo'

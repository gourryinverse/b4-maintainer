"""Contract: F1 on a series offers every project in place of Mode: the series' own (tree
with the series applied, tree at its base with the series for the agent, or read its
checkout; following the goal's suggestion until changed) and each other (tree at its
base, read its checkout, or left out). A project added is saved in the series'
workbench, at its first maintainer base, and offered chosen the next time."""

from sessiondata import FakeSessionRunner, FakeWorktrees
from test_workbench_screen import FakeService

from b4_maintainer.adapters.fixture import FixtureSource
from b4_maintainer.app import MaintainerApp, SeriesList
from b4_maintainer.launchdialog import LaunchDialog
from b4_maintainer.sessions.hub import SessionHub
from b4_maintainer.sessions.store import SessionStore
from b4_maintainer.workbench import WorkbenchStore


def make(demo_root, tmp_path):
    store = SessionStore(tmp_path / 'var' / 'sessions')
    runner, trees = FakeSessionRunner(store), FakeWorktrees()
    hub = SessionHub(store, runner, trees, sandbox=lambda: True)
    benches = WorkbenchStore(tmp_path / 'state' / 'workbenches')
    service = FakeService(tmp_path, benches, hub.records, store.directory)
    app = MaintainerApp(FixtureSource(demo_root).load, sessions=hub, workbenches=service)
    return app, runner, trees, benches


async def settle(pilot):
    for _ in range(3):
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()


async def open_dialog(pilot):
    await settle(pilot)
    await pilot.press('f1')
    await settle(pilot)
    dialog = pilot.app.screen
    assert isinstance(dialog, LaunchDialog)
    return dialog


async def test_f1_offers_each_projects_checkout_and_saves_the_added_ones(demo_root, tmp_path):
    app, runner, trees, benches = make(demo_root, tmp_path)
    async with app.run_test(size=(180, 50)) as pilot:
        await settle(pilot)
        item = app.query_one(SeriesList).highlighted_item
        projects = [p.id for p in app.snapshot.workspace.projects]
        other = next(p for p in projects if p != item.project)
        dialog = await open_dialog(pilot)
        labels = dialog.labels()
        assert 'Mode' not in labels and f'Tree: {item.project}' in labels
        assert f'Tree: {other}' in labels
        dialog.pick('goal', 'review')
        await pilot.pause()
        assert dialog.selected(f'tree:{item.project}') == 'read'  # Review: lightweight.
        assert dialog.selected(f'tree:{other}') == 'none'
        dialog.pick(f'tree:{other}', 'read')
        await pilot.pause()
        assert 'Checkouts:' in dialog.summary_text()
        await pilot.press('ctrl+s')
        await settle(pilot)
        [(sid, _request)] = runner.launches
        record = app._sessions.store.record(sid)
        assert record.mode == 'lightweight'
        assert [p for p, _path in record.reads] == [item.project, other]
        bench = benches.get(record.workbench)
        assert bench.entry(other) is not None  # Saved for next time.

        dialog = await open_dialog(pilot)  # Next time: offered chosen.
        assert dialog.selected(f'tree:{other}') == 'read'
        dialog.pick('goal', 'review')
        dialog.pick(f'tree:{item.project}', 'apply')
        dialog.pick(f'tree:{other}', 'base')
        await pilot.pause()
        await pilot.press('ctrl+s')
        await settle(pilot)
        [(_sid, mine), (_sid2, theirs)] = trees.entries
        assert (mine.project, theirs.project, theirs.items) == (item.project, other, ())
        record = app._sessions.store.record(runner.launches[-1][0])
        assert record.series == item.identity and [t.project for t in record.trees] == [
            item.project,
            other,
        ]


async def test_the_series_own_choice_follows_the_goal_until_changed(demo_root, tmp_path):
    app, _runner, _trees, _benches = make(demo_root, tmp_path)
    async with app.run_test(size=(180, 50)) as pilot:
        await settle(pilot)
        item = app.query_one(SeriesList).highlighted_item
        dialog = await open_dialog(pilot)
        group = f'tree:{item.project}'
        dialog.pick('goal', 'reproduce')  # Needs a tree.
        for _ in range(3):
            await pilot.pause()
        assert dialog.selected(group) == 'apply'
        dialog.pick(group, 'read')  # The maintainer's choice stays.
        for _ in range(3):
            await pilot.pause()
        dialog.pick('goal', 'review')
        for _ in range(3):
            await pilot.pause()
        assert dialog.selected(group) == 'read'

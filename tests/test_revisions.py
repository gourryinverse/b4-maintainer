"""Contract: a series' older revisions stay readable. V (in the list or a thread) picks
the revision to read; the reader says when it is not the latest. w while reading one
adds that revision to a workbench, so two revisions can sit side by side to compare;
a workbench session reads each revision's own mail, and refuses to apply two
revisions of one series in one tree."""

import datetime

from b4_maintainer.adapters.fixture import FixtureSource
from b4_maintainer.app import MaintainerApp
from b4_maintainer.branchscreen import Pick
from b4_maintainer.fixtures import EPOCH, PatchSpec, write_series_mbox

CHANGE_ID = '20260930-mm-foo-aaaaaaaaaaaa'  # The demo's mm: fix foo leak (v3).


def with_v2(demo_root):
    write_series_mbox(
        demo_root / 'mail' / 'mm-foo-v2.mbx',
        'mm: fix foo leak',
        revision=2,
        patches=[PatchSpec('mm/foo1.c'), PatchSpec('mm/foo2.c')],
        change_id=CHANGE_ID,
        date=EPOCH + datetime.timedelta(days=2),
    )
    return demo_root


async def settle(pilot):
    for _ in range(3):
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()


def foo_first(app):
    series_list = app.query_one('SeriesList')
    series_list.select_identity(f'change-id:{CHANGE_ID}')
    return series_list.highlighted_item


async def test_v_picks_an_older_revision_to_read_and_back(demo_root):
    app = MaintainerApp(FixtureSource(with_v2(demo_root)).load)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        item = foo_first(app)
        assert [s.revision for s in item.older] == [2]
        await pilot.press('V')
        await settle(pilot)
        assert isinstance(app.screen, Pick)
        assert [key for key, _label in app.screen.choices] == ['3', '2']
        assert 'latest' in app.screen.choices[0][1] and '2 patches' in app.screen.choices[1][1]
        await pilot.press('down', 'enter')
        await settle(pilot)
        assert app.reading is not None and app.reading.series.revision == 2
        assert 'v2 (older: v3 is the latest)' in app.status_text()
        assert len([r for r in app.tree_labels() if r.lstrip().startswith(('1/', '2/'))]) == 2
        await pilot.press('V')  # In the thread too.
        await settle(pilot)
        assert 'reading' in app.screen.choices[1][1]
        await pilot.press('enter')  # v3.
        await settle(pilot)
        assert app.reading.series.revision == 3 and 'older' not in app.status_text()


async def test_v_is_offered_only_with_older_revisions(demo_root):
    app = MaintainerApp(FixtureSource(demo_root).load)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        foo_first(app)
        assert not app.check_action('versions', ())


def postings(demo_root):
    from b4_maintainer.workbench import series_map

    return series_map(FixtureSource(with_v2(demo_root)).load().items)


def test_two_revisions_go_on_a_workbench_side_by_side(demo_root, tmp_path):
    import pytest

    from b4_maintainer.workbench import (
        Workbench,
        WorkbenchError,
        WorkbenchService,
        WorkbenchStore,
        add_series,
    )
    from b4_maintainer.workspace import Workspace

    series = postings(demo_root)
    identity = f'change-id:{CHANGE_ID}'
    bench = add_series(Workbench('w', 'W', ()), 'linux', identity, 2, 'mm: fix foo leak')
    bench = add_series(bench, 'linux', identity, 3, 'mm: fix foo leak')
    with pytest.raises(WorkbenchError):
        add_series(bench, 'linux', identity, 3, 'mm: fix foo leak')  # That revision is on.
    service = WorkbenchService(tmp_path, WorkbenchStore(tmp_path / 'wb'), lambda: [])
    plans, problems, _trees = service.plans(Workspace.from_config({}), bench, series)
    assert problems == []
    v2, v3 = plans[0].items
    assert (v2.revision, v2.source.endswith('mm-foo-v2.mbx')) == (2, True)
    assert (v3.revision, v3.source.endswith('mm-foo.mbx')) == (3, True)
    assert plans[0].compare == ('mm: fix foo leak',)  # Never both applied in one tree.
    rows = service.contents(Workspace.from_config({}), bench, series, {identity: 3})
    mailboxes = [r for r in rows if r.kind == 'mailbox']
    assert [r.revision for r in mailboxes] == [2, 3]
    shown = service.show(Workspace.from_config({}), bench, mailboxes[0], series, {})
    assert shown.startswith('mm: fix foo leak v2') and '2/2' in shown


async def test_w_while_reading_each_revision_puts_both_on_a_workbench(demo_root, tmp_path):
    from test_workbench_screen import FakeService

    from b4_maintainer.workbench import Workbench, WorkbenchStore

    store = WorkbenchStore(tmp_path / 'state' / 'workbenches')
    store.save(Workbench('cmp', 'Compare', ()))
    app = MaintainerApp(
        FixtureSource(with_v2(demo_root)).load,
        workbenches=FakeService(tmp_path, store, lambda: []),
    )
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        item = foo_first(app)
        for down in (('down',), ()):  # v2, then v3.
            await pilot.press('V')
            await settle(pilot)
            await pilot.press(*down, 'enter')
            await settle(pilot)
            await pilot.press('w')
            await settle(pilot)
            await pilot.press('enter')  # Onto Compare.
            await settle(pilot)
        added = store.get('cmp').entry(item.project).items
        assert [(i.series, i.revision) for i in added] == [(item.identity, 2), (item.identity, 3)]
        assert 'mm: fix foo leak v3 to Compare' in app.status_text()
        _item, thread = app.bench_thread(item.identity, 2)  # A Mailboxes row's own thread.
        assert thread.revision == 2 and len(thread.patches) == 2


async def until(pilot, ready, rounds=50):
    """Wait (workers, then a pause) until *ready()*, or fail."""
    for _ in range(rounds):
        if ready():
            return
        await pilot.app.workers.wait_for_complete()
        await pilot.pause(0.05)
    assert ready()


async def test_two_revisions_are_never_applied_in_one_tree(demo_root, tmp_path):
    from test_workbench_screen import open_screen, session_app

    from b4_maintainer.launchdialog import LaunchDialog
    from b4_maintainer.workbench import Entry, Item, Workbench

    identity = f'change-id:{CHANGE_ID}'
    items = tuple(Item(identity, n, 'mm: fix foo leak') for n in (2, 3))
    bench = Workbench('cmp', 'Compare', (Entry('linux', 'HEAD', items),))
    app, runner, trees = session_app(with_v2(demo_root), tmp_path, [bench])
    async with app.run_test(size=(180, 50)) as pilot:
        screen = await open_screen(pilot)
        for use in ('apply', 'deliver'):
            await pilot.press('f1')
            await until(pilot, lambda: isinstance(app.screen, LaunchDialog))
            app.screen.pick('goal', 'review')
            app.screen.pick('tree:linux', use)
            await pilot.pause()
            await pilot.press('ctrl+s')
            await until(pilot, lambda: app.screen is screen)
            if use == 'apply':
                await until(pilot, lambda: 'several revisions' in screen.status_text())
                assert runner.launches == []
        await until(pilot, lambda: bool(trees.entries))
        [(_sid, plan)] = trees.entries  # For the agent: each revision's own mbox.
        assert [(i.revision, i.source.rsplit('/', 1)[-1]) for i in plan.items] == [
            (2, 'mm-foo-v2.mbx'),
            (3, 'mm-foo.mbx'),
        ]

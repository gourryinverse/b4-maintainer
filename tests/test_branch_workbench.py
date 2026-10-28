"""Contract: w on Branches (F4) adds the selected branch to a workbench whole, or only its
marked commits, or only the selected commit (asking which when the whole branch could go
too); a session's commits are fetched from its tree."""

import dataclasses

from test_branch_screen import FakeBranches, settle
from test_workbench_screen import FakeService

from b4_maintainer.adapters.fixture import FixtureSource
from b4_maintainer.app import MaintainerApp
from b4_maintainer.branches import DetailRow
from b4_maintainer.branchscreen import Pick, _bench_branch
from b4_maintainer.workbench import Workbench, WorkbenchStore


class Branches(FakeBranches):
    def snapshot(self, workspace, trees=True):
        data = super().snapshot(workspace, trees)
        details = dict(data.details)
        details['prep:linux:b4/pa'] = (
            DetailRow('Cover letter: x', 'cover', '/repo', show_text='x'),
            DetailRow('  p1  mm: fix p', 'p1' * 20, '/repo', subject='mm: fix p'),
            DetailRow('  p2  mm: fix q', 'p2' * 20, '/repo', subject='mm: fix q'),
        )
        return dataclasses.replace(data, details=details)


def make(demo_root, tmp_path):
    store = WorkbenchStore(tmp_path / 'state' / 'workbenches')
    store.save(Workbench('w', 'W', ()))
    app = MaintainerApp(
        FixtureSource(demo_root).load,
        branches=Branches(),
        workbenches=FakeService(tmp_path, store, lambda: []),
    )
    return app, store


async def open_prep(pilot):
    await settle(pilot)
    await pilot.press('f4')
    await settle(pilot)
    pilot.app.screen.select_item('base:linux:me/base')  # F4 starts folded.
    await settle(pilot)
    pilot.app.screen.select_item('prep:linux:b4/pa')
    await settle(pilot)
    return pilot.app.screen


async def onto_w(pilot):
    await settle(pilot)
    assert isinstance(pilot.app.screen, Pick) and pilot.app.screen.choices[0][0] == 'w'
    await pilot.press('enter')
    await settle(pilot)


async def test_w_from_the_branch_list_adds_the_whole_branch(demo_root, tmp_path):
    app, store = make(demo_root, tmp_path)
    async with app.run_test(size=(180, 40)) as pilot:
        screen = await open_prep(pilot)
        await pilot.press('w')
        await onto_w(pilot)
        [item] = store.get('w').entry('linux').items
        assert (item.branch, item.commit) == ('b4/pa', '')
        assert 'Added b4/pa to W' in screen.status_text()


async def test_w_on_a_commit_asks_whole_branch_or_only_that_commit(demo_root, tmp_path):
    app, store = make(demo_root, tmp_path)
    async with app.run_test(size=(180, 40)) as pilot:
        screen = await open_prep(pilot)
        await pilot.press('tab', 'down')  # Its first commit (past the cover letter).
        await settle(pilot)
        assert screen.selected_row().subject == 'mm: fix p'
        await pilot.press('w')
        await settle(pilot)
        assert [k for k, _label in app.screen.choices] == ['branch', 'commit']
        await pilot.press('down', 'enter')  # Only this commit.
        await onto_w(pilot)
        [item] = store.get('w').entry('linux').items
        assert (item.branch, item.commit, item.subject) == ('b4/pa', 'p1' * 20, 'mm: fix p')
        await pilot.press('w')  # Again: it is already there.
        await settle(pilot)
        await pilot.press('down', 'enter')
        await onto_w(pilot)
        assert len(store.get('w').entry('linux').items) == 1
        assert 'already on W' in screen.status_text()


async def test_w_adds_the_marked_commits_each_alone(demo_root, tmp_path):
    app, store = make(demo_root, tmp_path)
    async with app.run_test(size=(180, 40)) as pilot:
        screen = await open_prep(pilot)
        await pilot.press('tab', 'down', 'space', 'down', 'space')
        await settle(pilot)
        await pilot.press('w')
        await onto_w(pilot)  # No question: the marks say it.
        items = store.get('w').entry('linux').items
        assert [(i.commit, i.subject) for i in items] == [
            ('p1' * 20, 'mm: fix p'),
            ('p2' * 20, 'mm: fix q'),
        ]
        assert 'Added 2 commits of b4/pa' in screen.status_text()


def test_session_branches_name_their_tree():
    row = DetailRow('  c  mm: x', 'c' * 40, '/tree', branch='b4m/fixup/x', subject='mm: x')
    assert _bench_branch('work:s1:b4m/fixup/x', None) == 'tree:s1:b4m/fixup/x'
    assert _bench_branch('work:s1/linux:b4m/fixup/x', None) == 'tree:s1/linux:b4m/fixup/x'
    assert _bench_branch('tree:s1/linux', row) == 'tree:s1/linux:b4m/fixup/x'
    assert _bench_branch('branch:linux:me/fix', None) == 'me/fix'
    assert _bench_branch('base:linux:me/base', row) == 'me/base'

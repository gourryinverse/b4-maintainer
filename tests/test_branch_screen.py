"""Contract: F3 opens the Branches screen: items per project, their commits, what the
selected commit shows; `r` rebases a base after a confirmation; Escape returns."""

from b4_maintainer.adapters.bases import BaseCommit, BaseStatus, RebaseResult
from b4_maintainer.adapters.fixture import FixtureSource
from b4_maintainer.app import Confirm, MaintainerApp
from b4_maintainer.branches import BranchesSnapshot, BranchItem, DetailRow
from b4_maintainer.branchscreen import BranchesScreen
from b4_maintainer.workspace import MaintainerBase

BASE = MaintainerBase('me/base', 'up/stable')
STATUS = BaseStatus(
    BASE,
    True,
    'tip0',
    'fork0',
    2,
    (
        BaseCommit('c1', 'mm: landed', 'A <a@example.org>', True),
        BaseCommit('c2', 'mm: kept', 'B <b@example.org>'),
    ),
)


class FakeBranches:
    def __init__(self):
        self.rebased = []
        self.result = RebaseResult(True, 'tip1', ('mm: landed',))
        self.loads = 0
        self.sessions = []
        self.accepted = []

    def snapshot(self, workspace):
        self.loads += 1
        items = (
            BranchItem('base:linux:me/base', 'base', 'linux', 'me/base on up/stable · 2 behind'),
            BranchItem('tree:s1', 'tree', 'linux', 'linux: mm: foo v3 · s1 · clean'),
        )
        details = {
            'base:linux:me/base': (
                DetailRow('On up/stable, 2 behind'),
                DetailRow('  c1  mm: landed  (landed)', 'c1', '/repo'),
                DetailRow('  c2  mm: kept', 'c2', '/repo'),
            ),
            'tree:s1': (
                DetailRow('Series'),
                DetailRow('  s1c  mm/foo: series patch', 's1c', '/tree'),
            ),
        }
        return BranchesSnapshot(
            items, details, {'base:linux:me/base': STATUS}, rebases={'tree:s1': 'r1'}
        )

    def show(self, row):
        return f'shown {row.key}'

    def rebase(self, workspace, key, expected):
        self.rebased.append((key, expected))
        return self.result

    def rebase_session(self, workspace, key, failed):
        from sessiondata import make_record

        self.sessions.append((key, failed))
        return make_record('r1', scope='rebase')

    def accept(self, workspace, sid):
        self.accepted.append(sid)
        return RebaseResult(True, 'f' * 40)


async def settle(pilot):
    for _ in range(3):
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()


def make_app(demo_root, branches):
    return MaintainerApp(FixtureSource(demo_root).load, branches=branches)


async def test_f3_opens_the_branches_screen_and_escape_returns(demo_root):
    service = FakeBranches()
    app = make_app(demo_root, service)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        await pilot.press('f3')
        await settle(pilot)
        screen = app.screen
        assert isinstance(screen, BranchesScreen)
        assert screen.selected_item() == 'base:linux:me/base'
        assert screen.selected_row().key == 'c1'
        assert screen.shown_text() == 'shown c1'
        await pilot.press('tab', 'down')  # The commits pane: the next commit.
        await settle(pilot)
        assert screen.shown_text() == 'shown c2'
        await pilot.press('shift+tab', 'down')  # The items pane: the session tree.
        await settle(pilot)
        assert screen.selected_item() == 'tree:s1' and screen.shown_text() == 'shown s1c'
        await pilot.press('escape')
        await settle(pilot)
        assert not isinstance(app.screen, BranchesScreen)


async def test_r_rebases_the_selected_base_after_a_confirmation(demo_root):
    service = FakeBranches()
    app = make_app(demo_root, service)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        await pilot.press('f3')
        await settle(pilot)
        await pilot.press('r')
        await settle(pilot)
        assert isinstance(app.screen, Confirm)
        assert 'Rebase me/base onto up/stable? 1 commits replayed, 1 landed dropped' in (
            app.screen.prompt
        )
        await pilot.press('n')
        await settle(pilot)
        assert service.rebased == []
        await pilot.press('r')
        await settle(pilot)
        await pilot.press('y')
        await settle(pilot)
        assert service.rebased == [('base:linux:me/base', 'tip0')]
        screen = app.screen
        assert 'Rebased me/base; dropped: mm: landed' in screen.status_text()
        assert service.loads == 2  # Reloaded after the rebase.


async def test_a_failed_rebase_names_the_commit(demo_root):
    service = FakeBranches()
    service.result = RebaseResult(False, failed=STATUS.commits[1])
    app = make_app(demo_root, service)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        await pilot.press('f3')
        await settle(pilot)
        await pilot.press('r')
        await settle(pilot)
        await pilot.press('y')
        await settle(pilot)
        assert 'me/base not rebased: mm: kept does not apply' in app.screen.status_text()


async def test_r_is_only_for_bases_and_f3_only_with_a_branch_service(demo_root):
    service = FakeBranches()
    app = make_app(demo_root, service)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        await pilot.press('f3')
        await settle(pilot)
        await pilot.press('down')
        await settle(pilot)
        await pilot.press('r')
        await settle(pilot)
        assert not isinstance(app.screen, Confirm)
    app = MaintainerApp(FixtureSource(demo_root).load)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        await pilot.press('f3')
        await settle(pilot)
        assert not isinstance(app.screen, BranchesScreen)


async def test_f3_toggles_back_and_b_does_not_open_it(demo_root):
    app = make_app(demo_root, FakeBranches())
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        await pilot.press('b')
        await settle(pilot)
        assert not isinstance(app.screen, BranchesScreen)
        await pilot.press('f3')
        await settle(pilot)
        assert isinstance(app.screen, BranchesScreen)
        await pilot.press('f3')
        await settle(pilot)
        assert not isinstance(app.screen, BranchesScreen)


async def test_a_failed_rebase_offers_a_rebase_session_after_a_confirmation(demo_root):
    service = FakeBranches()
    service.result = RebaseResult(False, failed=STATUS.commits[1], detail='CONFLICT in b.c')
    app = make_app(demo_root, service)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        await pilot.press('f3')
        await settle(pilot)
        await pilot.press('a')  # Nothing failed yet: not offered.
        await settle(pilot)
        assert not isinstance(app.screen, Confirm)
        await pilot.press('r')
        await settle(pilot)
        await pilot.press('y')
        await settle(pilot)
        assert 'a rebases it with an agent' in app.screen.status_text()
        await pilot.press('a')
        await settle(pilot)
        assert isinstance(app.screen, Confirm) and 'rebases a copy' in app.screen.prompt
        await pilot.press('n')
        await settle(pilot)
        assert service.sessions == []  # Declined: no agent.
        await pilot.press('a')
        await settle(pilot)
        await pilot.press('y')
        await settle(pilot)
        assert service.sessions == [('base:linux:me/base', service.result)]
        assert 'Launched rebase session r1' in app.screen.status_text()


async def test_a_on_a_rebase_sessions_tree_accepts_its_result(demo_root):
    service = FakeBranches()
    app = make_app(demo_root, service)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        await pilot.press('f3')
        await settle(pilot)
        await pilot.press('A')  # A base, not a rebase session: nothing.
        await settle(pilot)
        assert not isinstance(app.screen, Confirm)
        await pilot.press('down')  # The session tree.
        await settle(pilot)
        await pilot.press('A')
        await settle(pilot)
        assert isinstance(app.screen, Confirm) and 'result' in app.screen.prompt
        await pilot.press('y')
        await settle(pilot)
        assert service.accepted == ['r1']
        assert 'Accepted: the base is at ffffffffffff' in app.screen.status_text()

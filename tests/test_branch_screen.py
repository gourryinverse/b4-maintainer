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
        self.plucked = []
        self.cuts = []
        self.runs = []
        self.worktrees = []
        self.targets = {'tree:s1': ('base:linux:me/base',)}

    def snapshot(self, workspace):
        self.loads += 1
        items = (
            BranchItem('base:linux:me/base', 'base', 'linux', 'me/base on up/stable · 2 behind'),
            BranchItem('tree:s1', 'tree', 'linux', 'linux: mm: foo v3 · s1 · clean'),
            BranchItem('prep:linux:b4/pa', 'prep', 'linux', 'b4/pa · 1 commit'),
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
        details['tree:s1'] += (DetailRow('  s2c  mm: agent fix', 's2c', '/tree'),)
        details['prep:linux:b4/pa'] = (
            DetailRow('Cover letter: EDITME: x', 'cover', '/repo', show_text='EDITME: x'),
            DetailRow('  p1  mm: fix p', 'p1', '/repo'),
        )
        statuses = {'base:linux:me/base': STATUS, 'base:linux:me/linus': STATUS}
        tips = {'base:linux:me/base': 'tip0', 'base:linux:me/linus': 'tip0'}
        return BranchesSnapshot(
            items, details, statuses, rebases={'tree:s1': 'r1'}, targets=self.targets, tips=tips
        )

    def show(self, row):
        return row.show_text or f'shown {row.key}'

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

    def fork_points(self, workspace, item):
        return ['up/stable', 'me/base', 'mm/mm-new']

    def cut_prep(self, workspace, item, rows, name, fork):
        from b4_maintainer.branches import CutResult

        self.cuts.append((item, [r.key for r in rows], name, fork))
        return CutResult(True, f'b4/{name}', len(rows))

    def prep_run(self, workspace, item, action):
        self.runs.append((item, action))
        return 0, f'{action} output\nSuccess: 1, Warning: 0, Error: 0'

    def prep_worktree(self, workspace, item):
        self.worktrees.append(item)
        return __import__('pathlib').Path('/w/prep')

    def pluck(self, workspace, item, rows, target, expected):
        from b4_maintainer.adapters.bases import PluckResult

        self.plucked.append((item, [r.key for r in rows], target, expected))
        return PluckResult(True, 'e' * 40, tuple((r.key, 'n') for r in rows))


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


async def open_tree_rows(pilot):
    await settle(pilot)
    await pilot.press('f3')
    await settle(pilot)
    await pilot.press('down')  # The session tree.
    await settle(pilot)
    await pilot.press('tab')  # Its commits.
    await settle(pilot)


async def test_space_marks_commits_and_p_plucks_them_after_a_confirmation(demo_root):
    service = FakeBranches()
    app = make_app(demo_root, service)
    async with app.run_test(size=(180, 40)) as pilot:
        await open_tree_rows(pilot)
        screen = app.screen
        await pilot.press('space', 'down', 'space')
        await settle(pilot)
        assert [r.key for r in screen.marked_rows()] == ['s1c', 's2c']
        await pilot.press('p')
        await settle(pilot)
        assert isinstance(app.screen, Confirm)
        assert 'Pluck 2 commits onto me/base' in app.screen.prompt
        await pilot.press('y')
        await settle(pilot)
        assert service.plucked == [('tree:s1', ['s1c', 's2c'], 'base:linux:me/base', 'tip0')]
        assert 'Plucked 2 onto me/base' in screen.status_text()


async def test_with_several_bases_p_asks_which(demo_root):
    from b4_maintainer.branchscreen import Pick

    service = FakeBranches()
    service.targets = {'tree:s1': ('base:linux:me/base', 'base:linux:me/linus')}
    app = make_app(demo_root, service)
    async with app.run_test(size=(180, 40)) as pilot:
        await open_tree_rows(pilot)
        await pilot.press('p')  # The selected commit, unmarked.
        await settle(pilot)
        assert isinstance(app.screen, Pick)
        await pilot.press('down', 'enter')
        await settle(pilot)
        assert isinstance(app.screen, Confirm) and 'onto me/linus' in app.screen.prompt
        await pilot.press('y')
        await settle(pilot)
        assert service.plucked == [('tree:s1', ['s1c'], 'base:linux:me/linus', 'tip0')]


async def test_pluck_is_not_offered_without_a_target(demo_root):
    service = FakeBranches()
    service.targets = {}
    app = make_app(demo_root, service)
    async with app.run_test(size=(180, 40)) as pilot:
        await open_tree_rows(pilot)
        await pilot.press('p')
        await settle(pilot)
        assert not isinstance(app.screen, Confirm) and service.plucked == []


async def test_n_cuts_a_prep_branch_asking_its_name_and_fork(demo_root):
    from b4_maintainer.branchscreen import Ask, Pick

    service = FakeBranches()
    app = make_app(demo_root, service)
    async with app.run_test(size=(180, 40)) as pilot:
        await open_tree_rows(pilot)
        await pilot.press('space', 'down', 'space', 'n')
        await settle(pilot)
        assert isinstance(app.screen, Ask)
        assert app.screen.query_one('Input').value == 'series-patch'  # From the first subject.
        app.screen.query_one('Input').value = 'leak-fixes'
        await pilot.press('enter')
        await settle(pilot)
        assert isinstance(app.screen, Pick)
        await pilot.press('enter')  # The first fork point: the base's upstream.
        await settle(pilot)
        assert isinstance(app.screen, Confirm)
        assert 'b4/leak-fixes from up/stable with 2 commits' in app.screen.prompt
        await pilot.press('y')
        await settle(pilot)
        assert service.cuts == [('tree:s1', ['s1c', 's2c'], 'leak-fixes', 'up/stable')]
        assert 'Made b4/leak-fixes with 2 commits' in app.screen.status_text()


async def test_t_and_c_run_b4_on_a_prep_branch_and_show_its_output(demo_root):
    service = FakeBranches()
    app = make_app(demo_root, service)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        await pilot.press('f3')
        await settle(pilot)
        await pilot.press('t')  # A base: not a prep branch.
        await settle(pilot)
        assert service.runs == []
        await pilot.press('down', 'down')
        await settle(pilot)
        screen = app.screen
        assert screen.shown_text() == 'EDITME: x'  # The cover letter, first.
        await pilot.press('c')
        await settle(pilot)
        assert service.runs == [('prep:linux:b4/pa', 'check')]
        assert 'check output' in screen.shown_text()
        assert 'Check: Success: 1, Warning: 0, Error: 0' in screen.status_text()
        await pilot.press('t')
        await settle(pilot)
        assert service.runs[-1] == ('prep:linux:b4/pa', 'auto-to-cc')


async def test_e_edits_the_cover_with_b4_while_the_app_is_suspended(demo_root, monkeypatch):
    import contextlib

    calls = []
    monkeypatch.setattr(
        'b4_maintainer.branchscreen.run_b4',
        lambda wt, branch, *args, interactive=False: (
            calls.append((wt, branch, args, interactive)) or (0, '')
        ),
    )
    service = FakeBranches()
    app = make_app(demo_root, service)
    monkeypatch.setattr(type(app), 'suspend', lambda self: contextlib.nullcontext())
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        await pilot.press('f3')
        await settle(pilot)
        await pilot.press('down', 'down', 'e')
        await settle(pilot)
        assert service.worktrees == ['prep:linux:b4/pa']
        [(wt, branch, args, interactive)] = calls
        assert (str(wt), branch, args, interactive) == (
            '/w/prep',
            'b4/pa',
            ('prep', '--edit-cover'),
            True,
        )
        assert 'Cover letter edited' in app.screen.status_text()

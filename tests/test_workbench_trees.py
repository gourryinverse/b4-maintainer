"""Contract: the Workbench's Trees section acts on session trees as Branches did: space
marks commits, p plucks them (or the selected one) onto a base, a prep branch, or another
session tree; P cuts a prep branch from them; x on a tree removes it after saying what it
would lose; D derives a session from it."""

from test_workbench_screen import FakeService

from b4_maintainer.adapters.bases import PluckResult
from b4_maintainer.adapters.fixture import FixtureSource
from b4_maintainer.app import Confirm, MaintainerApp
from b4_maintainer.attachments import Attachment
from b4_maintainer.branches import CutResult
from b4_maintainer.branchscreen import Ask, Pick
from b4_maintainer.workbench import ContentRow, Entry, Item, Workbench, WorkbenchStore
from b4_maintainer.workbenchscreen import PickMany

TREE = ContentRow(
    '  s1 · linux · b4m/session/s1', 'tree', 'linux', session='s1', path='/t', item='tree:s1'
)


def commit(sha, subject):
    a = Attachment(
        'patch',
        f'patch · fixup · {subject}',
        's1',
        repo='/t',
        sha=sha,
        title=subject,
        branch='b4m/fixup/x',
    )
    return ContentRow(
        f'    fixup: {subject}', 'commit', 'linux', session='s1', attachment=a, item='tree:s1'
    )


ROWS = [ContentRow('Trees', 'heading'), TREE, commit('c1', 'mm: one'), commit('c2', 'mm: two')]


class TreeService(FakeService):
    def contents(self, workspace, bench, series, latest):
        return list(ROWS)

    def show(self, workspace, bench, row, series, latest):
        return f'shown {row.text.strip()}'


class TreeBranches:
    def __init__(self):
        self.plucked, self.cuts, self.reaped = [], [], []

    def tree_targets(self, workspace, item):
        return (
            [('base:linux:me/base', 'me/base (maintainer base)'), ('tree:s2', 's2 (session tree)')],
            {'base:linux:me/base': 'tip0', 'tree:s2': 't2'},
        )

    def pluck(self, workspace, item, rows, target, expected):
        self.plucked.append((item, [r.key for r in rows], target, expected))
        return PluckResult(True, 'new', tuple((r.key, 'n') for r in rows))

    def fork_points(self, workspace, item):
        return ['up/stable', 'me/base']

    def cut_prep(self, workspace, item, rows, name, fork):
        self.cuts.append((item, [r.key for r in rows], name, fork))
        return CutResult(True, f'b4/{name}', len(rows))

    def losses(self, workspace, item):
        return 'session tree /t: 1 uncommitted file'

    def reap(self, workspace, item):
        self.reaped.append(item)
        return True, f'Removed the tree of {item}'

    def derive_options(self, workspace, item, row):
        return [('base0', '', 'the series base'), (row.key if row else 'tip', 's1', 'its tree')]

    def derive_context(self, item, row):
        return None


def make(demo_root, tmp_path):
    store = WorkbenchStore(tmp_path / 'state' / 'workbenches')
    store.save(Workbench('w', 'W', (Entry('linux', '', (Item(branch='me/fix'),)),)))
    branches = TreeBranches()
    app = MaintainerApp(
        FixtureSource(demo_root).load,
        branches=branches,
        workbenches=TreeService(tmp_path, store, lambda: []),
    )
    return app, branches


async def settle(pilot):
    for _ in range(3):
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()


async def tab(pilot):
    await settle(pilot)
    await pilot.press('f3')
    await settle(pilot)
    return pilot.app.screen


async def test_marked_commits_are_plucked_onto_a_target_after_a_confirmation(demo_root, tmp_path):
    app, branches = make(demo_root, tmp_path)
    async with app.run_test(size=(200, 50)) as pilot:
        screen = await tab(pilot)
        screen.select_row('commit')
        await pilot.press('space', 'down', 'space')
        assert screen.marked() == ['c1', 'c2']
        await pilot.press('p')
        await settle(pilot)
        assert isinstance(app.screen, Pick)
        await pilot.press('enter')  # me/base.
        await settle(pilot)
        assert isinstance(app.screen, Confirm) and 'me/base' in app.screen.prompt
        await pilot.press('y')
        await settle(pilot)
        assert branches.plucked == [('tree:s1', ['c1', 'c2'], 'base:linux:me/base', 'tip0')]
        assert 'Plucked 2' in screen.status_text() and screen.marked() == []


async def test_p_with_nothing_marked_plucks_the_selected_commit(demo_root, tmp_path):
    app, branches = make(demo_root, tmp_path)
    async with app.run_test(size=(200, 50)) as pilot:
        screen = await tab(pilot)
        screen.select_row('commit')
        await pilot.press('down', 'p')
        await settle(pilot)
        await pilot.press('down', 'enter', 'y')  # The other session tree.
        await settle(pilot)
        assert branches.plucked == [('tree:s1', ['c2'], 'tree:s2', 't2')]


async def test_a_prep_branch_is_cut_from_marked_commits(demo_root, tmp_path):
    app, branches = make(demo_root, tmp_path)
    async with app.run_test(size=(200, 50)) as pilot:
        screen = await tab(pilot)
        screen.select_row('commit')
        await pilot.press('space', 'P')
        await settle(pilot)
        assert isinstance(app.screen, Ask)
        await pilot.press(*'leak-fixes', 'enter')
        await settle(pilot)
        assert isinstance(app.screen, Pick)
        await pilot.press('down', 'enter')  # me/base.
        await settle(pilot)
        assert branches.cuts == [('tree:s1', ['c1'], 'leak-fixes', 'me/base')]


async def test_x_on_a_tree_removes_it_after_saying_what_goes(demo_root, tmp_path):
    app, branches = make(demo_root, tmp_path)
    async with app.run_test(size=(200, 50)) as pilot:
        screen = await tab(pilot)
        screen.select_row('tree')
        await pilot.press('x')
        await settle(pilot)
        assert isinstance(app.screen, Confirm) and '1 uncommitted file' in app.screen.prompt
        await pilot.press('y')
        await settle(pilot)
        assert branches.reaped == ['tree:s1']


async def test_d_derives_a_session_from_a_tree(demo_root, tmp_path):
    app, branches = make(demo_root, tmp_path)
    launched = []
    app.launch_derived = lambda parent, derived, context=None: launched.append((parent, derived))
    async with app.run_test(size=(200, 50)) as pilot:
        screen = await tab(pilot)
        screen.select_row('commit')
        await pilot.press('D')
        await settle(pilot)
        assert isinstance(app.screen, Pick)
        await pilot.press('down', 'enter')
        await settle(pilot)
        [(parent, derived)] = launched
        assert parent == 's1' and (derived.start, derived.start_from) == ('c1', 's1')


FOLDED = ContentRow(
    '    folded: mm: one',
    'commit',
    'linux',
    session='s1',
    attachment=Attachment(
        'patch',
        'patch · folded · mm: one',
        's1',
        repo='/t',
        sha='f1',
        title='mm: one',
        branch='b4m/folded/one',
    ),
    item='tree:s1',
)


class FoldBranches(TreeBranches):
    def __init__(self, replaces):
        super().__init__()
        self.replaces = replaces
        self.folds = []

    def fold_plan(self, workspace, item, folded, series):
        from b4_maintainer.branches import FoldPlan

        return FoldPlan(
            'linux',
            '/t',
            'b' * 40,
            folded,
            2,
            ('b4m/pull-ahead/dep',),
            (('b4m/pull-ahead/leak', 'mm: fix the leak'), ('b4m/pull-ahead/old', 'mm: old')),
            'b4/one' if self.replaces else 'one-series',
            self.replaces,
            'tip0',
            '' if self.replaces else 'cover@example.org',
            'mm: one series',
            3,
        )

    def fold_to_prep(self, workspace, plan, extras, name):
        self.folds.append((plan.folded, list(extras), name))
        return CutResult(True, plan.target if plan.replaces else f'b4/{name}', 3)


class FoldService(TreeService):
    def contents(self, workspace, bench, series, latest):
        return [ContentRow('Trees', 'heading'), TREE, FOLDED]


def fold_app(demo_root, tmp_path, replaces):
    store = WorkbenchStore(tmp_path / 'state' / 'workbenches')
    store.save(Workbench('w', 'W', (Entry('linux', '', (Item(branch='me/fix'),)),)))
    branches = FoldBranches(replaces)
    app = MaintainerApp(
        FixtureSource(demo_root).load,
        branches=branches,
        workbenches=FoldService(tmp_path, store, lambda: []),
    )
    return app, branches


async def test_f_folds_into_the_series_own_prep_branch(demo_root, tmp_path):
    app, branches = fold_app(demo_root, tmp_path, replaces=True)
    async with app.run_test(size=(200, 50)) as pilot:
        screen = await tab(pilot)
        screen.select_row('tree')
        await settle(pilot)
        assert not screen.check_action('fold', ())  # Only on a folded series.
        screen.select_row('commit')
        await settle(pilot)
        assert screen.check_action('fold', ())
        await pilot.press('f')
        await settle(pilot)
        pick = app.screen
        assert isinstance(pick, PickMany) and 'b4m/pull-ahead/dep' in pick.title_text
        assert [key for key, _label in pick.choices] == [
            'b4m/pull-ahead/leak',
            'b4m/pull-ahead/old',
        ]
        await pilot.press('space', 'enter')  # Lead with leak; leave old out.
        await settle(pilot)
        confirm = app.screen
        assert isinstance(confirm, Confirm) and 'b4/one' in confirm.prompt
        assert 'v3' in confirm.prompt and 'cover letter' in confirm.prompt
        await pilot.press('y')
        await settle(pilot)
        assert branches.folds == [('b4m/folded/one', ['b4m/pull-ahead/leak'], '')]
        assert 'Made b4/one' in screen.status_text() and 'F4' in screen.status_text()


async def test_f_makes_a_prep_branch_from_the_thread_for_another_authors_series(
    demo_root, tmp_path
):
    app, branches = fold_app(demo_root, tmp_path, replaces=False)
    async with app.run_test(size=(200, 50)) as pilot:
        screen = await tab(pilot)
        screen.select_row('commit')
        await settle(pilot)
        await pilot.press('f')
        await settle(pilot)
        await pilot.press('enter')  # No extra pull-aheads.
        await settle(pilot)
        ask = app.screen
        assert isinstance(ask, Ask) and ask.query_one('Input').value == 'one-series'
        await pilot.press('enter')
        await settle(pilot)
        confirm = app.screen
        assert isinstance(confirm, Confirm) and 'b4/one-series' in confirm.prompt
        assert 'b4 prep -F' in confirm.prompt
        await pilot.press('y')
        await settle(pilot)
        assert branches.folds == [('b4m/folded/one', [], 'one-series')]
        assert 'Made b4/one-series' in screen.status_text()

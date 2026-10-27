"""Contract: F4 opens the Branches (Artifacts) screen: items per project, their commits, what the
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
        self.rebases = {'tree:s1': 'r1'}  # The fake tree belongs to a rebase session.
        self.cuts = []
        self.runs = []
        self.worktrees = []
        self.signed = []
        self.reaped = []
        self.loss = {}
        self.previews = []
        self.sent = ()  # Revisions b4 has sent.
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
        tips = {
            'base:linux:me/base': 'tip0',
            'base:linux:me/linus': 'tip0',
            'prep:linux:b4/pa': 'ptip',
            'tree:s9': 'applied9',
        }
        return BranchesSnapshot(
            items,
            details,
            statuses,
            rebases=self.rebases,
            targets=self.targets,
            tips=tips,
            identities={'linux': 'Example Maintainer <m@example.org>'},
            preps={'prep:linux:b4/pa': {'revision': 1, 'sent': self.sent}},
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

    def sign_off(self, workspace, item, expected):
        from b4_maintainer.adapters.prep import SignResult

        self.signed.append((item, expected))
        return SignResult(True, 'g' * 40, 1)

    def preview(self, workspace, item):
        self.previews.append(item)
        return 0, 'Subject: [PATCH] mm: fix p\nTo: sub@example.org'

    def send_args(self, workspace, item):
        return __import__('pathlib').Path('/w/prep'), 'b4/pa', ('send', '-d')

    def reapable(self, data, item):
        return item.split(':', 1)[0] in ('prep', 'tree')

    def losses(self, workspace, item):
        return self.loss.get(item, f'{item}: clean')

    def reap(self, workspace, item):
        self.reaped.append(item)
        return True, f'Removed {item}'

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
        await pilot.press('f4')
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
        await pilot.press('f4')
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
        await pilot.press('f4')
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
        await pilot.press('f4')
        await settle(pilot)
        await pilot.press('down')
        await settle(pilot)
        await pilot.press('r')
        await settle(pilot)
        assert not isinstance(app.screen, Confirm)
    app = MaintainerApp(FixtureSource(demo_root).load)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        await pilot.press('f4')
        await settle(pilot)
        assert not isinstance(app.screen, BranchesScreen)


async def test_f3_toggles_back_and_b_does_not_open_it(demo_root):
    app = make_app(demo_root, FakeBranches())
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        await pilot.press('b')
        await settle(pilot)
        assert not isinstance(app.screen, BranchesScreen)
        await pilot.press('f4')
        await settle(pilot)
        assert isinstance(app.screen, BranchesScreen)
        await pilot.press('f2')
        await settle(pilot)
        assert not isinstance(app.screen, BranchesScreen)


async def test_a_failed_rebase_offers_a_rebase_session_after_a_confirmation(demo_root):
    service = FakeBranches()
    service.result = RebaseResult(False, failed=STATUS.commits[1], detail='CONFLICT in b.c')
    app = make_app(demo_root, service)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        await pilot.press('f4')
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
        await pilot.press('f4')
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
    await pilot.press('f4')
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
        await pilot.press('f4')
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
        await pilot.press('f4')
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


async def open_prep(pilot):
    await settle(pilot)
    await pilot.press('f4')
    await settle(pilot)
    await pilot.press('down', 'down')
    await settle(pilot)


async def test_s_signs_off_after_a_dco_confirmation(demo_root):
    service = FakeBranches()
    app = make_app(demo_root, service)
    async with app.run_test(size=(180, 40)) as pilot:
        await open_prep(pilot)
        await pilot.press('s')
        await settle(pilot)
        assert isinstance(app.screen, Confirm)
        prompt = app.screen.prompt
        assert 'Signed-off-by: Example Maintainer <m@example.org>' in prompt
        assert "Developer's Certificate of Origin" in prompt
        await pilot.press('y')
        await settle(pilot)
        assert service.signed == [('prep:linux:b4/pa', 'ptip')]
        assert 'Signed off 1 commit' in app.screen.status_text()


async def test_o_shows_what_would_be_sent_without_sending(demo_root):
    service = FakeBranches()
    app = make_app(demo_root, service)
    async with app.run_test(size=(180, 40)) as pilot:
        await open_prep(pilot)
        await pilot.press('o')
        await settle(pilot)
        assert service.previews == ['prep:linux:b4/pa']
        assert 'To: sub@example.org' in app.screen.shown_text()
        assert 'nothing was sent' in app.screen.status_text()


async def test_capital_s_sends_through_b4_in_the_terminal_after_a_confirmation(
    demo_root, monkeypatch
):
    import contextlib

    calls = []
    monkeypatch.setattr(
        'b4_maintainer.branchscreen.run_b4',
        lambda wt, branch, *args, interactive=False: (
            calls.append((branch, args, interactive)) or (0, '')
        ),
    )
    monkeypatch.setattr('builtins.input', lambda prompt='': '')
    service = FakeBranches()
    app = make_app(demo_root, service)
    monkeypatch.setattr(type(app), 'suspend', lambda self: contextlib.nullcontext())
    async with app.run_test(size=(180, 40)) as pilot:
        await open_prep(pilot)
        await pilot.press('S')
        await settle(pilot)
        assert isinstance(app.screen, Confirm) and 'press Enter' in app.screen.prompt
        await pilot.press('n')
        await settle(pilot)
        assert calls == []  # Declined: b4 never ran.
        await pilot.press('S')
        await settle(pilot)
        await pilot.press('y')
        await settle(pilot)
        assert calls == [('b4/pa', ('send', '-d'), True)]
        assert 'b4 send finished for b4/pa' in app.screen.status_text()


async def test_u_collects_trailers_and_d_compares_once_something_was_sent(demo_root):
    service = FakeBranches()
    app = make_app(demo_root, service)
    async with app.run_test(size=(180, 40)) as pilot:
        await open_prep(pilot)
        await pilot.press('d')  # Nothing sent yet: nothing to compare with.
        await settle(pilot)
        await pilot.press('u')
        await settle(pilot)
        assert service.runs == [('prep:linux:b4/pa', 'trailers')]
    service = FakeBranches()
    service.sent = ('v1',)
    app = make_app(demo_root, service)
    async with app.run_test(size=(180, 40)) as pilot:
        await open_prep(pilot)
        await pilot.press('d')
        await settle(pilot)
        assert service.runs == [('prep:linux:b4/pa', 'compare')]


async def test_a_rebase_says_which_prep_branches_moved_and_which_stayed(demo_root):
    service = FakeBranches()
    service.result = RebaseResult(
        True, 'tip1', moved=('b4/a',), stuck=(('b4/b', 'mm: x does not apply'),)
    )
    app = make_app(demo_root, service)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        await pilot.press('f4')
        await settle(pilot)
        await pilot.press('r')
        await settle(pilot)
        await pilot.press('y')
        await settle(pilot)
        text = app.screen.status_text()
        assert 'moved b4/a' in text and 'b4/b stays: mm: x does not apply' in text


async def test_x_removes_after_saying_what_goes_and_asks_twice_for_changes(demo_root):
    service = FakeBranches()
    service.loss = {'tree:s1': 'session tree /w/s1: 1 uncommitted file'}
    app = make_app(demo_root, service)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        await pilot.press('f4')
        await settle(pilot)
        await pilot.press('x')  # A base: not removable here.
        await settle(pilot)
        assert not isinstance(app.screen, Confirm)
        await pilot.press('down', 'down', 'x')  # The prep branch.
        await settle(pilot)
        assert isinstance(app.screen, Confirm) and 'prep:linux:b4/pa: clean' in app.screen.prompt
        await pilot.press('y')
        await settle(pilot)
        assert service.reaped == ['prep:linux:b4/pa']
        await pilot.press('up', 'x')  # The session tree, with changes.
        await settle(pilot)
        await pilot.press('y')
        await settle(pilot)
        assert isinstance(app.screen, Confirm) and 'This will be lost' in app.screen.prompt
        await pilot.press('n')
        await settle(pilot)
        assert service.reaped == ['prep:linux:b4/pa']


async def test_p_copies_to_another_session_tree_with_its_own_wording(demo_root):
    from b4_maintainer.branchscreen import Pick

    service = FakeBranches()
    service.targets = {'tree:s1': ('base:linux:me/base', 'tree:s9')}
    app = make_app(demo_root, service)
    async with app.run_test(size=(180, 40)) as pilot:
        await open_tree_rows(pilot)
        await pilot.press('p')
        await settle(pilot)
        assert isinstance(app.screen, Pick)
        assert [label for _key, label in app.screen.choices] == ['me/base', "session s9's tree"]
        await pilot.press('down', 'enter')
        await settle(pilot)
        assert isinstance(app.screen, Confirm) and "Copy 1 commit to session s9's tree" in (
            app.screen.prompt
        )


async def test_d_derives_a_session_starting_where_chosen(demo_root, tmp_path):
    from sessiondata import FakeSessionRunner, FakeWorktrees, make_record

    from b4_maintainer.branchscreen import Pick
    from b4_maintainer.launchdialog import LaunchDialog
    from b4_maintainer.sessions.hub import SessionHub
    from b4_maintainer.sessions.policy import ContextItem
    from b4_maintainer.sessions.store import SessionStore

    store = SessionStore(tmp_path / 'var' / 'sessions')
    runner, trees = FakeSessionRunner(store), FakeWorktrees()
    hub = SessionHub(store, runner, trees, catalog=lambda: None, sandbox=lambda: True)
    service = FakeBranches()
    service.rebases = {}  # A review session's tree, not a rebase session's.
    service.derive_options = lambda workspace, item, row: [
        ('base0', '', 'the series base base0'),
        ('tip1', 's1', "session s1's tree"),
    ]
    service.derive_context = lambda item, row: ContextItem(
        'parent', 'From session s1: its findings', text='A pre-existing leak.'
    )
    app = MaintainerApp(FixtureSource(demo_root).load, branches=service, sessions=hub)
    async with app.run_test(size=(180, 50)) as pilot:
        await settle(pilot)
        item = app.snapshot.items[0]
        runner.add(
            make_record('s1', series=item.identity, subject=item.series.subject), alive=False
        )
        await pilot.press('f4')
        await settle(pilot)
        await pilot.press('down', 'D')  # The session tree.
        await settle(pilot)
        assert isinstance(app.screen, Pick)
        await pilot.press('enter')  # The series base.
        await settle(pilot)
        dialog = app.screen
        assert isinstance(dialog, LaunchDialog)
        summary = dialog.summary_text()
        assert 'Derived:  from session s1; tree starts at the series base base0' in summary
        assert 'From session s1: its findings' in summary  # In the prompt package.
        await act(pilot, dialog.pick, 'goal', 'bugs')
        await pilot.press('ctrl+s')
        await settle(pilot)
        [(sid, _request)] = runner.launches
        assert trees.starts == [(sid, 'base0', '')]
        record = store.record(sid)
        assert record.parent == 's1' and record.mode == 'worktree'
        context = store.directory(sid) / 'work' / 'context' / 'parent.md'
        assert context.read_text() == 'A pre-existing leak.'


async def act(pilot, method, *args):
    method(*args)
    for _ in range(3):
        await pilot.pause()

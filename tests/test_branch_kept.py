"""Contract: on Branches, a pluck from a session tree offers to keep the session's other
artifacts with the plucked commits (asking which commit when there were several), and k
on a session's artifact keeps it with a commit or on a shelf."""

from test_branch_screen import FakeBranches, make_app, open_tree_rows, settle

from b4_maintainer.app import Confirm
from b4_maintainer.branches import DetailRow
from b4_maintainer.branchscreen import Pick
from b4_maintainer.sessions.artifacts import Artifact
from b4_maintainer.workbenchscreen import PickMany


def select_row(screen, key):
    rows = screen.query_one('#branch-rows')
    rows.focus()
    rows.highlighted = next(n for n, r in enumerate(screen._rows) if r.key == key)


RACE = Artifact('reproducer', 'race', 'two threads')
FIX = Artifact('pull-ahead', 'mm: agent fix', 'fixed', branch='b4m/pull-ahead/fix')


class KeepBranches(FakeBranches):
    def __init__(self):
        super().__init__()
        self.kept = []

    def snapshot(self, workspace, trees=True):
        found = super().snapshot(workspace, trees)
        found.details['tree:s1'] += (
            DetailRow('Artifacts'),
            DetailRow('  pull-ahead: mm: agent fix', 'artifact:0', '/tree', FIX),
            DetailRow('  reproducer: race', 'artifact:1', '/tree', RACE),
        )
        return found

    def pluck_keep_offer(self, workspace, item, target, picked):
        return [RACE], [
            (f'commit:linux:{n}{k}', f'subject {k}') for k, (_s, n) in enumerate(picked)
        ]

    def keep_targets(self, workspace):
        return [('commit:linux:c2', 'mm: kept  (me/base)'), ('shelf:', 'A new shelf…')]

    def keep(self, workspace, item, artifacts, target):
        self.kept.append((item, [a.title for a in artifacts], target))
        return f'Kept {len(artifacts)}'


async def test_plucking_several_commits_offers_to_keep_asking_which_commit(demo_root):
    service = KeepBranches()
    app = make_app(demo_root, service)
    async with app.run_test(size=(180, 40)) as pilot:
        await open_tree_rows(pilot)
        await pilot.press('space', 'down', 'space', 'p')
        await settle(pilot)
        await pilot.press('y')
        await settle(pilot)
        assert isinstance(app.screen, PickMany) and '2 plucked commits' in app.screen.title_text
        await pilot.press('enter')
        await settle(pilot)
        assert isinstance(app.screen, Pick)
        await pilot.press('down', 'enter')
        await settle(pilot)
        assert service.kept == [('tree:s1', ['race'], 'commit:linux:n1')]
        assert 'Kept 1' in app.screen.status_text()


async def test_k_keeps_a_sessions_artifact_but_not_a_commit_branch(demo_root):
    service = KeepBranches()
    app = make_app(demo_root, service)
    async with app.run_test(size=(180, 40)) as pilot:
        await open_tree_rows(pilot)
        screen = app.screen
        select_row(screen, 'artifact:0')
        await settle(pilot)
        assert not screen.check_action('keep', ())  # A pull-ahead: its commits pluck.
        select_row(screen, 'artifact:1')
        await settle(pilot)
        await pilot.press('k')
        await settle(pilot)
        assert isinstance(app.screen, Pick) and 'race' in app.screen.title_text
        await pilot.press('enter')
        await settle(pilot)
        assert not isinstance(app.screen, Confirm)
        assert service.kept == [('tree:s1', ['race'], 'commit:linux:c2')]

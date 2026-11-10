"""Contract: the Workbench tab keeps session output past its workbench. A pluck from a
session tree offers the session's artifacts, all ticked, to keep with the new commit; k
on an artifact keeps it with a commit or on a shelf (a new one named); Kept artifacts
lists them by commit and shelf, w puts one on a workbench, x removes it after asking."""

from test_workbench_trees import ROWS, TreeBranches, TreeService, settle, tab

from b4_maintainer.adapters.fixture import FixtureSource
from b4_maintainer.app import Confirm, MaintainerApp
from b4_maintainer.attachments import Attachment
from b4_maintainer.branchscreen import Ask, Pick
from b4_maintainer.kept import KeptStore
from b4_maintainer.sessions.artifacts import Artifact
from b4_maintainer.workbench import ContentRow, Entry, Item, Workbench, WorkbenchStore
from b4_maintainer.workbenchscreen import KEPT, PickMany


class KeepBranches(TreeBranches):
    def __init__(self, tmp_path):
        super().__init__()
        self.kept = KeptStore(tmp_path / 'var' / 'kept')
        race = tmp_path / 'out' / 'reproducers' / 'race'
        race.mkdir(parents=True)
        (race / 'README.md').write_text('# race\n')
        (race / 'race.c').write_text('int main(void) {}\n')
        self.out = tmp_path / 'out'
        self.artifacts = [
            Artifact('reproducer', 'race', 'two threads', path=race / 'README.md'),
            Artifact('finding', 'double charge', 'the loser charges twice'),
        ]

    def pluck_keep_offer(self, workspace, item, target, picked):
        return self.artifacts, [(f'commit:linux:{new}', 'mm: one') for _s, new in picked]

    def keep_targets(self, workspace):
        shelves = [(f'shelf:{s}', f'Shelf: {s}') for s in self.kept.shelves()]
        return [('commit:linux:n', 'mm: one  (me/base)'), *shelves, ('shelf:', 'A new shelf…')]

    def keep(self, workspace, item, artifacts, target):
        kind, _, rest = target.partition(':')
        where = {'shelf': rest} if kind == 'shelf' else {'commit': 'n', 'subject': 'mm: one'}
        for a in artifacts:
            self.kept.keep(a.kind, a.title, a.summary, a.tested, a.path, self.out, **where)
        return f'Kept {len(artifacts)}'

    def as_artifact(self, attachment):
        return next(a for a in self.artifacts if a.title == attachment.title)

    def kept_items(self):
        return self.kept.items()

    def unkeep(self, kid):
        return self.kept.remove(kid)


def make(demo_root, tmp_path):
    store = WorkbenchStore(tmp_path / 'state' / 'workbenches')
    store.save(Workbench('w', 'W', (Entry('linux', '', (Item(branch='me/fix'),)),)))
    branches = KeepBranches(tmp_path)
    app = MaintainerApp(
        FixtureSource(demo_root).load,
        branches=branches,
        workbenches=TreeService(tmp_path, store, lambda: []),
    )
    return app, branches


async def test_a_pluck_offers_to_keep_the_sessions_artifacts_with_the_commit(demo_root, tmp_path):
    app, branches = make(demo_root, tmp_path)
    async with app.run_test(size=(200, 50)) as pilot:
        screen = await tab(pilot)
        screen.select_row('commit')
        await pilot.press('p')
        await settle(pilot)
        await pilot.press('enter', 'y')  # Onto me/base.
        await settle(pilot)
        offer = app.screen
        assert isinstance(offer, PickMany) and 'mm: one' in offer.title_text
        assert offer.ticked == {'0', '1'}  # All ticked: Enter keeps them.
        await pilot.press('down', 'space', 'enter')  # Not the finding.
        await settle(pilot)
        [kept] = branches.kept.items()
        assert kept.title == 'race' and kept.commit == 'n'
        assert kept.path is not None and (kept.path / 'race.c').is_file()  # Its folder.
        assert 'Kept 1' in screen.status_text()


async def test_escape_keeps_nothing(demo_root, tmp_path):
    app, branches = make(demo_root, tmp_path)
    async with app.run_test(size=(200, 50)) as pilot:
        screen = await tab(pilot)
        screen.select_row('commit')
        await pilot.press('p')
        await settle(pilot)
        await pilot.press('enter', 'y')
        await settle(pilot)
        await pilot.press('escape')
        await settle(pilot)
        assert branches.kept.items() == [] and 'Plucked 1' in screen.status_text()


async def test_k_keeps_an_artifact_on_a_new_shelf_then_kept_lists_it(demo_root, tmp_path):
    artifact = ContentRow(
        '    reproducer: race',
        'artifact',
        session='s1',
        attachment=Attachment('file', 'reproducer: race', 's1', title='race', group='reproducer'),
    )
    ROWS.extend([ContentRow('Artifacts', 'heading'), artifact])
    try:
        app, branches = make(demo_root, tmp_path)
        store = app._workbenches.store
        async with app.run_test(size=(200, 50)) as pilot:
            screen = await tab(pilot)
            screen.select_row('artifact')
            await pilot.press('k')
            await settle(pilot)
            assert isinstance(app.screen, Pick)
            await pilot.press('end', 'enter')  # A new shelf…
            await settle(pilot)
            assert isinstance(app.screen, Ask)
            await pilot.press(*'memfd', 'space', *'race', 'enter')
            await settle(pilot)
            [kept] = branches.kept.items()
            assert kept.shelf == 'memfd race'
            assert 'Kept 1' in screen.status_text()

            # Kept artifacts: listed apart, by shelf; the right pane says what it is.
            screen.select(KEPT, '')
            await settle(pilot)
            assert screen.bench().id == KEPT
            rows = [(r.kind, r.text.strip()) for r in screen._rows]
            assert rows == [('heading', 'Shelf: memfd race'), ('kept', 'reproducer: race')]
            await pilot.press('tab')
            screen.select_row('kept')
            await settle(pilot)
            assert (
                'race.c' in screen.shown_text() and 'on the shelf memfd race' in screen.shown_text()
            )

            await pilot.press('w')  # Onto W, for a session re-validating the fix.
            await settle(pilot)
            await pilot.press('enter')  # Which project.
            await settle(pilot)
            await pilot.press('enter')  # Onto W.
            await settle(pilot)
            files = [i.file for e in store.get('w').entries for i in e.items if i.file]
            assert files == [str(kept.path)]

            screen.select_row('kept')
            await pilot.press('x')
            await settle(pilot)
            assert isinstance(app.screen, Confirm) and str(kept.path) in app.screen.prompt
            await pilot.press('y')
            await settle(pilot)
            assert branches.kept.items() == [] and not kept.path.exists()
    finally:
        del ROWS[-2:]

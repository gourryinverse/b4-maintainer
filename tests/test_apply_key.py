"""Contract: `a` in the series list or a thread applies the series to a base or a prep
branch: pick the branch (when there are several), then always the patches (all ticked),
and b4 applies them; the outcome is said in the status line."""

from b4_maintainer.adapters.apply import ApplyResult, SeriesPatch
from b4_maintainer.adapters.bases import BaseCommit, PluckResult
from b4_maintainer.adapters.fixture import FixtureSource
from b4_maintainer.app import Confirm, MaintainerApp
from b4_maintainer.applydialog import ApplyDialog
from b4_maintainer.branches import ApplyChoices, DropChoices
from b4_maintainer.branchscreen import Pick

PATCHES = (
    SeriesPatch(1, 'mm: fix the leak', 'A <a@example.org>'),
    SeriesPatch(2, 'mm: add foo', 'A <a@example.org>'),
    SeriesPatch(3, 'mm: use foo', 'A <a@example.org>'),
)
TARGETS = (('base:linux:me/base', 'me/base'), ('prep:linux:b4/pa', 'b4/pa'))
TIPS = {'base:linux:me/base': 'btip', 'prep:linux:b4/pa': 'ptip'}


class FakeBranches:
    def __init__(self, choices=None, result=None):
        self.choices = choices or ApplyChoices(PATCHES, TARGETS, TIPS)
        self.result = result or ApplyResult(True, 'new', ('c1', 'c2'))
        self.asked = []
        self.applied = []
        self.drop_result = PluckResult(True, 'new', dropped=('mm: fix the leak',))

    def apply_choices(self, workspace, project_id, source, revision):
        self.asked.append((project_id, source, revision))
        return self.choices

    def apply(self, workspace, target, source, revision, numbers, expected):
        self.applied.append((target, source, revision, list(numbers), expected))
        return self.result

    drop_found = DropChoices(
        (('base:linux:me/base', 'me/base'),),
        {'base:linux:me/base': (BaseCommit('d1', 'mm: fix the leak', 'A <a@example.org>'),)},
        {'base:linux:me/base': 'btip'},
    )
    dropped = ()

    handoffs = ()

    def apply_session(self, workspace, target, series, numbers, failed):
        from sessiondata import make_record

        self.handoffs += ((target, series, tuple(numbers), failed),)
        return make_record('a1', scope='apply')

    def drop_choices(self, workspace, project_id, patches):
        self.asked.append(('drop', project_id, tuple(patches)))
        return self.drop_found

    def drop(self, workspace, target, shas, expected):
        self.dropped += ((target, tuple(shas), expected),)
        return self.drop_result


async def settle(pilot):
    for _ in range(3):
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()


async def test_a_in_the_list_picks_a_branch_then_the_patches(demo_root):
    service = FakeBranches()
    app = MaintainerApp(FixtureSource(demo_root).load, branches=service)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        item = app.query_one('SeriesList').highlighted_item
        await pilot.press('a')
        await settle(pilot)
        assert service.asked == [(item.project, item.series.source, item.series.revision)]
        assert isinstance(app.screen, Pick)
        await pilot.press('down', 'enter')  # b4/pa.
        await settle(pilot)
        dialog = app.screen
        assert isinstance(dialog, ApplyDialog)
        assert dialog.ticked() == [1, 2, 3]  # All, to start with.
        assert 'b4/pa' in dialog.heading() and 'Link' in dialog.heading()
        await pilot.press('space')  # Untick the first.
        await pilot.press('enter')
        await settle(pilot)
        assert service.applied == [
            ('prep:linux:b4/pa', item.series.source, item.series.revision, [2, 3], 'ptip')
        ]
        assert 'Applied 2 patches to b4/pa' in app.status_text()


async def test_a_in_a_thread_applies_the_open_series(demo_root):
    service = FakeBranches(ApplyChoices(PATCHES, TARGETS[:1], TIPS))
    app = MaintainerApp(FixtureSource(demo_root).load, branches=service)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        await pilot.press('down', 'enter')
        await settle(pilot)
        item = app.reading
        assert item is not None
        await pilot.press('a')
        await settle(pilot)
        assert isinstance(app.screen, ApplyDialog)  # One branch: no question.
        await pilot.press('enter')
        await settle(pilot)
        assert service.applied == [
            ('base:linux:me/base', item.series.source, item.series.revision, [1, 2, 3], 'btip')
        ]
        assert app.reading is not None and app.reading.identity == item.identity  # Still open.


async def test_escape_or_nothing_ticked_applies_nothing(demo_root):
    service = FakeBranches(ApplyChoices(PATCHES, TARGETS[:1], TIPS))
    app = MaintainerApp(FixtureSource(demo_root).load, branches=service)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        await pilot.press('a')
        await settle(pilot)
        await pilot.press('escape')
        await settle(pilot)
        await pilot.press('a')
        await settle(pilot)
        for _ in PATCHES:
            await pilot.press('space', 'down')
        await pilot.press('enter')
        await settle(pilot)
        assert service.applied == []
        assert 'Nothing applied' in app.status_text()


async def test_a_problem_or_a_failure_is_said(demo_root):
    service = FakeBranches(ApplyChoices(problem='linux has no maintainer base'))
    app = MaintainerApp(FixtureSource(demo_root).load, branches=service)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        await pilot.press('a')
        await settle(pilot)
        assert not isinstance(app.screen, (Pick, ApplyDialog))
        assert 'linux has no maintainer base' in app.status_text()
        service.choices = ApplyChoices(PATCHES, TARGETS[:1], TIPS)
        service.result = ApplyResult(False, error='mm: foo v3 does not apply on me/base')
        await pilot.press('a')
        await settle(pilot)
        await pilot.press('enter')
        await settle(pilot)
        text = app.status_text()
        assert 'Not applied: mm: foo v3 does not apply on me/base' in text


async def test_d_drops_the_series_from_a_base_after_a_confirmation(demo_root):
    service = FakeBranches()
    app = MaintainerApp(FixtureSource(demo_root).load, branches=service)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        item = app.query_one('SeriesList').highlighted_item
        await pilot.press('d')
        await settle(pilot)
        patches = tuple((p.msgid, p.subject) for p in item.series.patches)
        assert service.asked == [('drop', item.project, patches)]
        assert isinstance(app.screen, Confirm)
        assert 'Drop 1 commit of' in app.screen.prompt and 'from me/base' in app.screen.prompt
        await pilot.press('n')
        await settle(pilot)
        assert service.dropped == ()
        await pilot.press('d')
        await settle(pilot)
        await pilot.press('y')
        await settle(pilot)
        assert service.dropped == (('base:linux:me/base', ('d1',), 'btip'),)
        assert 'Dropped 1 commit from me/base' in app.status_text()


async def test_a_drop_that_does_not_replay_or_finds_nothing_is_said(demo_root):
    service = FakeBranches()
    service.drop_found = DropChoices(problem='not on any maintainer base of linux')
    app = MaintainerApp(FixtureSource(demo_root).load, branches=service)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        await pilot.press('d')
        await settle(pilot)
        assert not isinstance(app.screen, Confirm)
        assert 'not on any maintainer base of linux' in app.status_text()
        del service.drop_found  # Back to the class default: found on me/base.
        service.drop_result = PluckResult(False, failed='mm: later work')
        await pilot.press('d')
        await settle(pilot)
        await pilot.press('y')
        await settle(pilot)
        text = app.status_text()
        assert 'mm: later work' in text and 'nothing changed' in text


async def test_a_conflict_on_a_base_offers_to_hand_it_to_an_agent(demo_root):
    conflict = ApplyResult(
        False,
        error='mm: foo v3 does not apply on me/base',
        output='CONFLICT in mm/foo.c',
        conflict=True,
    )
    service = FakeBranches(ApplyChoices(PATCHES, TARGETS[:1], TIPS), conflict)
    app = MaintainerApp(FixtureSource(demo_root).load, branches=service)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        item = app.query_one('SeriesList').highlighted_item
        await pilot.press('a')
        await settle(pilot)
        await pilot.press('enter')  # All patches.
        await settle(pilot)
        offer = app.screen
        assert isinstance(offer, Pick)
        assert 'does not apply on me/base' in offer.title_text
        assert 'CONFLICT in mm/foo.c' in offer.title_text
        assert [key for key, _label in offer.choices] == ['agent', 'none']
        assert 'A in F3' in offer.choices[0][1]
        await pilot.press('down', 'enter')  # Don't apply.
        await settle(pilot)
        assert service.handoffs == () and 'Not applied' in app.status_text()
        await pilot.press('a')
        await settle(pilot)
        await pilot.press('enter')
        await settle(pilot)
        await pilot.press('enter')  # Hand it to an agent.
        await settle(pilot)
        [(target, series, numbers, failed)] = service.handoffs
        assert target == 'base:linux:me/base' and numbers == (1, 2, 3) and failed is conflict
        assert (series.series, series.source) == (item.identity, item.series.source)
        assert 'apply session a1' in app.status_text()


async def test_a_asks_for_the_project_first_when_several_have_branches(demo_root):
    targets = (
        ('base:linux:me/base', 'me/base'),
        ('prep:linux:b4/pa', 'b4/pa'),
        ('base:ndctl:me/ndctl', 'me/ndctl'),
    )
    tips = {**TIPS, 'base:ndctl:me/ndctl': 'ntip'}
    service = FakeBranches(ApplyChoices(PATCHES, targets, tips))
    app = MaintainerApp(FixtureSource(demo_root).load, branches=service)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        item = app.query_one('SeriesList').highlighted_item
        await pilot.press('a')
        await settle(pilot)
        projects = app.screen
        assert isinstance(projects, Pick) and 'project' in projects.title_text
        assert [key for key, _label in projects.choices] == ['linux', 'ndctl']
        assert 'this series' in projects.choices[0][1]
        await pilot.press('down', 'enter')  # ndctl: one branch, no second question.
        await settle(pilot)
        dialog = app.screen
        assert isinstance(dialog, ApplyDialog) and 'ndctl: me/ndctl' in dialog.heading()
        await pilot.press('enter')
        await settle(pilot)
        assert service.applied == [
            ('base:ndctl:me/ndctl', item.series.source, item.series.revision, [1, 2, 3], 'ntip')
        ]
        assert 'to ndctl: me/ndctl' in app.status_text()

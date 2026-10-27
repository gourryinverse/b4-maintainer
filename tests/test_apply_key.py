"""Contract: `a` in the series list or a thread applies the series to a base or a prep
branch: pick the branch (when there are several), then always the patches (all ticked),
and b4 applies them; the outcome is said in the status line."""

from b4_maintainer.adapters.apply import ApplyResult, SeriesPatch
from b4_maintainer.adapters.fixture import FixtureSource
from b4_maintainer.app import MaintainerApp
from b4_maintainer.applydialog import ApplyDialog
from b4_maintainer.branches import ApplyChoices
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

    def apply_choices(self, workspace, project_id, source, revision):
        self.asked.append((project_id, source, revision))
        return self.choices

    def apply(self, workspace, target, source, revision, numbers, expected):
        self.applied.append((target, source, revision, list(numbers), expected))
        return self.result


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
        assert 'mm: foo v3 does not apply on me/base' in text and 'F1' in text

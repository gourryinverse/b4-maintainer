"""Contract: the drawer lists the selected thread's saved drafts beside its sessions."""

import pytest
from test_editor_drafts import CapturingActions, FakeEditor

from b4_maintainer.adapters.drafts import DraftStore
from b4_maintainer.app import Confirm, MaintainerApp, SeriesList


async def settle(pilot):
    for _ in range(3):
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()


def make(demo_root, tmp_path):
    actions = CapturingActions(demo_root)
    drafts = DraftStore(tmp_path / 'drafts')
    editor = FakeEditor()
    app = MaintainerApp(actions.load, actions=actions, drafts=drafts, editor=editor)
    return app, actions, drafts, editor


def drafted(actions):
    """A series with a cover letter, and its cover letter's message-id."""
    item = next(i for i in actions.load().items if i.series.has_cover)
    return item, item.series.msgids[0]


async def open_drawer(pilot):
    await pilot.press('grave_accent')
    await settle(pilot)


@pytest.fixture
def setup(demo_root, tmp_path):
    app, actions, drafts, editor = make(demo_root, tmp_path)
    item, msgid = drafted(actions)
    drafts.save(msgid, 'On x wrote:\n> quote\n', 'On x wrote:\n> quote\n\nSaved reply.\n')
    return app, drafts, editor, item, msgid


async def test_the_open_threads_drafts_head_the_drawer(setup):
    app, drafts, editor, item, msgid = setup
    async with app.run_test(size=(200, 50)) as pilot:
        await settle(pilot)
        app.query_one(SeriesList).select_identity(item.identity)
        await pilot.press('enter')
        await settle(pilot)
        await open_drawer(pilot)
        rows = app.drawer_rows()
        assert rows[0].startswith('✎ draft') and item.series.subject in rows[0]
        assert app.selected_draft() == msgid


async def test_enter_resumes_the_draft_and_x_discards_it(setup):
    app, drafts, editor, item, msgid = setup
    async with app.run_test(size=(200, 50)) as pilot:
        await settle(pilot)
        app.query_one(SeriesList).select_identity(item.identity)
        await pilot.pause()
        await open_drawer(pilot)  # From the series list: the highlighted series' drafts.
        assert app.selected_draft() == msgid
        await pilot.press('enter')
        await settle(pilot)
        assert app.reading.identity == item.identity
        assert editor.opened and editor.opened[-1][1] == drafts.text_path(msgid)
        await open_drawer(pilot)  # Closes it.
        await open_drawer(pilot)
        assert 'in your editor' in app.drawer_rows()[0]


async def test_x_discards_a_saved_draft_after_confirmation(setup):
    app, drafts, editor, item, msgid = setup
    async with app.run_test(size=(200, 50)) as pilot:
        await settle(pilot)
        app.query_one(SeriesList).select_identity(item.identity)
        await pilot.pause()
        await open_drawer(pilot)
        await pilot.press('x')
        assert isinstance(app.screen, Confirm)
        await pilot.press('y')
        await settle(pilot)
        assert msgid not in drafts.msgids()
        assert not any(r.startswith('✎') for r in app.drawer_rows())
        assert editor.opened == []


async def test_threads_without_drafts_show_none(setup):
    app, drafts, editor, item, msgid = setup
    async with app.run_test(size=(200, 50)) as pilot:
        await settle(pilot)
        other = next(i for i in app.index.items('all') if i.identity != item.identity)
        app.query_one(SeriesList).select_identity(other.identity)
        await pilot.pause()
        await open_drawer(pilot)
        assert not any(r.startswith('✎') for r in app.drawer_rows())

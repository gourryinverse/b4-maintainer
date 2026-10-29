"""Contract: a series session may read the maintainer's unsent reply drafts on that
series' thread (any revision), but only when asked: the prompt package offers them as
a private source, unticked, saying how many; ticked, they are copied into the session's
context as drafts.md, each with the message it answers. Drafts on other threads never
go."""

from sessiondata import FakeSessionRunner, FakeWorktrees
from test_session_dialog import act, catalog, open_dialog, settle

from b4_maintainer.adapters.drafts import DraftStore, SavedDraft
from b4_maintainer.adapters.fixture import FixtureSource
from b4_maintainer.app import MaintainerApp
from b4_maintainer.package import drafts_context
from b4_maintainer.sessions.hub import SessionHub
from b4_maintainer.sessions.store import SessionStore


def test_drafts_are_a_private_source_with_what_each_answers():
    drafts = [
        (SavedDraft('m1@x', '> quoted', 'My reply.\n', '2026-10-29T15:25:35+00:00'), 'Re: one'),
        (SavedDraft('m2@x', '', 'Another.\n', '2026-10-29T16:00:00+00:00'), ''),
    ]
    item = drafts_context(drafts)
    assert item.available and item.private and item.id == 'drafts'
    assert '2' in item.label and 'private' in item.label
    assert 'Not sent' in item.text and 'Never quote' in item.text
    assert item.text.index('Re: one') < item.text.index('My reply.') < item.text.index('Another.')
    assert '<m2@x>' in item.text
    none = drafts_context([])
    assert not none.available and none.reason == 'no drafts on this thread'


async def test_f1_offers_the_threads_drafts_unticked_and_copies_them_when_ticked(
    demo_root, tmp_path
):
    store = SessionStore(tmp_path / 'var' / 'sessions')
    runner = FakeSessionRunner(store)
    drafts = DraftStore(tmp_path / 'drafts')
    hub = SessionHub(store, runner, FakeWorktrees(), catalog=catalog)
    app = MaintainerApp(FixtureSource(demo_root).load, sessions=hub, drafts=drafts)
    async with app.run_test(size=(180, 50)) as pilot:
        await settle(pilot)
        mine, other = app.snapshot.items[0], app.snapshot.items[1]
        drafts.save(mine.series.msgids[0], '', 'Draft on this series.\n')
        drafts.save(other.series.msgids[0], '', 'Draft elsewhere.\n')
        dialog = await open_dialog(pilot)
        assert dialog.ticked('package') == ['mbox']  # Drafts are private: not ticked.
        assert not dialog.option_disabled('package', 'drafts')
        await act(pilot, dialog.pick, 'goal', 'review')
        await act(pilot, dialog.pick, 'package', 'drafts')
        assert dialog.ticked('package') == ['mbox', 'drafts']
        await pilot.press('ctrl+s')
        await settle(pilot)
        [(sid, _request)] = runner.launches
        text = (store.directory(sid) / 'work' / 'context' / 'drafts.md').read_text()
        assert 'Draft on this series.' in text and 'Draft elsewhere.' not in text
        assert 'drafts' in store.record(sid).context

"""Contract: replies are drafted in a pane beside the thread, which stays browsable."""

import contextlib
import email.message
import pathlib
from types import SimpleNamespace
from unittest import mock

from b4_maintainer.adapters.fixture import FixtureSource
from b4_maintainer.app import MaintainerApp, SeriesList


class DraftActions:
    def __init__(self, root):
        self.fixture = FixtureSource(root)
        self.answered_calls = []

    def load(self):
        return self.fixture.load()

    def refresh(self, progress):
        return ''

    def track(self, item):
        return ''

    def toggle_hidden(self, item):
        return ''

    def mark_seen(self, msgids):
        pass

    def mark_read(self, item):
        return ''

    def prepare_reply(self, item, msgid):
        lmsg = mock.Mock()
        lmsg.fromname, lmsg.fromemail, lmsg.date = 'Contributor One', 'c1@example.org', None
        lmsg.body = 'The patch body.\n'
        lmsg.make_reply.return_value = email.message.EmailMessage()
        return SimpleNamespace(
            lmsg=lmsg, checkout=pathlib.Path('.'), email_dryrun=True, patatt_sign=False
        )

    def answered(self, item, msgid):
        self.answered_calls.append(msgid)
        return f'Replied: {item.series.subject}'


async def settle(pilot):
    for _ in range(2):
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()


async def reading(pilot):
    await settle(pilot)
    await pilot.press('enter')
    await settle(pilot)


async def start_draft(pilot):
    await reading(pilot)
    await pilot.press('r')
    await settle(pilot)
    return pilot.app.query_one('#draft')


def make(demo_root):
    actions = DraftActions(demo_root)
    return MaintainerApp(actions.load, actions=actions), actions


def bindings(app):
    return {k: b.binding.description for k, b in app.screen.active_bindings.items()}


async def test_reply_opens_a_draft_pane_beside_the_thread(demo_root):
    app, _ = make(demo_root)
    async with app.run_test(size=(180, 40)) as pilot:
        draft = await start_draft(pilot)
        assert app.reader_panes() == ('tree', 'content', 'draft')
        assert app.focused is draft
        assert draft.text.startswith('On , Contributor One wrote:\n> The patch body.')
        assert 'r' not in bindings(app)  # One draft at a time.
        assert {'ctrl+s', 'ctrl+g'} <= set(bindings(app))


async def test_thread_stays_browsable_while_drafting(demo_root):
    app, _ = make(demo_root)
    async with app.run_test(size=(180, 40)) as pilot:
        draft = await start_draft(pilot)
        draft.insert('Looks good to me.\n')
        await pilot.press('shift+tab')
        assert app.focused is app.query_one('#content')
        await pilot.press('shift+tab')
        assert app.focused is app.query_one('#tree')
        await pilot.press('right_square_bracket', 'right_square_bracket')
        await pilot.pause()
        assert app.tree_position() == ('2', 0)
        assert 'mm/foo2.c' in app.content_text()
        await pilot.press('tab', 'tab')
        assert app.focused is draft
        assert 'Looks good to me.' in draft.text

        await pilot.press('escape')  # Back to the thread; the draft stays.
        assert app.focused is app.query_one('#tree')
        assert 'Looks good to me.' in draft.text
        assert app.reader_panes() == ('tree', 'content', 'draft')


async def test_send_previews_with_b4_then_closes_the_draft(demo_root):
    app, actions = make(demo_root)
    sent = []
    async with app.run_test(size=(180, 40)) as pilot:
        draft = await start_draft(pilot)
        await pilot.press('ctrl+s')  # Unchanged quote: nothing to send.
        await pilot.pause()
        assert 'No changes' in app.status_text()
        assert type(app.screen).__name__ != 'FollowupReplyPreviewScreen'

        draft.insert('Reviewed-by: Example Maintainer <maintainer@example.org>\n')
        await pilot.press('ctrl+s')
        await pilot.pause()
        assert type(app.screen).__name__ == 'FollowupReplyPreviewScreen'
        await pilot.press('e')  # Back to editing, text intact.
        await pilot.pause()
        assert app.focused is draft and 'Reviewed-by' in draft.text
        await pilot.press('ctrl+s')
        await pilot.pause()
        await pilot.press('escape')  # Abandoning the preview keeps the draft.
        await pilot.pause()
        assert app.reader_panes() == ('tree', 'content', 'draft')

        await pilot.press('ctrl+s')
        await pilot.pause()
        with (
            mock.patch.object(app, 'suspend', contextlib.nullcontext),
            mock.patch('b4.get_smtp', return_value=(None, 'maintainer@example.org')),
            mock.patch('b4.send_mail', side_effect=lambda s, m, **k: sent.extend(m) or 1),
        ):
            await pilot.press('ctrl+y')
            await settle(pilot)
        assert len(sent) == 1
        assert actions.answered_calls
        assert app.reader_panes() == ('tree', 'content')
        assert 'r' in bindings(app)


async def test_discard_asks_first(demo_root):
    app, _ = make(demo_root)
    async with app.run_test(size=(180, 40)) as pilot:
        draft = await start_draft(pilot)
        draft.insert('half a thought')
        await pilot.press('ctrl+g')
        await pilot.pause()
        assert app.quit_prompt() == 'Discard draft? [y|N]'
        await pilot.press('n')
        await pilot.pause()
        assert app.reader_panes() == ('tree', 'content', 'draft')
        await pilot.press('ctrl+g')
        await pilot.pause()
        await pilot.press('y')
        await pilot.pause()
        assert app.reader_panes() == ('tree', 'content')


async def test_closing_the_reader_with_a_draft_asks_first(demo_root):
    app, _ = make(demo_root)
    async with app.run_test(size=(180, 40)) as pilot:
        draft = await start_draft(pilot)
        draft.insert('unsent')
        await pilot.press('escape', 'escape')  # draft -> tree -> close?
        await pilot.pause()
        assert app.quit_prompt() == 'Discard draft? [y|N]'
        await pilot.press('n')
        await pilot.pause()
        assert app.reading is not None and 'unsent' in draft.text
        await pilot.press('escape')
        await pilot.pause()
        await pilot.press('y')
        await pilot.pause()
        assert app.reading is None
        assert app.query_one(SeriesList).has_focus


async def test_narrow_terminal_cycles_panes_while_drafting(demo_root):
    app, _ = make(demo_root)
    async with app.run_test(size=(90, 40)) as pilot:
        await start_draft(pilot)
        assert app.reader_panes() == ('draft',)
        await pilot.press('tab')
        assert app.reader_panes() == ('tree',)
        await pilot.press('tab')
        assert app.reader_panes() == ('content',)
        await pilot.press('shift+tab', 'shift+tab')
        assert app.reader_panes() == ('draft',)


def make_saved(demo_root, tmp_path):
    from b4_maintainer.adapters.drafts import DraftStore

    actions = DraftActions(demo_root)
    store = DraftStore(tmp_path / 'drafts')
    return MaintainerApp(actions.load, actions=actions, drafts=store), actions, store


async def test_typing_saves_the_draft(demo_root, tmp_path):
    app, _, store = make_saved(demo_root, tmp_path)
    async with app.run_test(size=(180, 40)) as pilot:
        draft = await start_draft(pilot)
        msgid = app.draft_msgid()
        await pilot.pause(0.8)
        assert store.get(msgid) is None  # The untouched quote is not a draft.
        draft.insert('Half a thought.')
        await pilot.pause(0.8)
        saved = store.get(msgid)
        assert saved is not None and 'Half a thought.' in saved.text


async def test_closing_the_reader_keeps_the_draft_and_reopening_restores_it(demo_root, tmp_path):
    app, _, store = make_saved(demo_root, tmp_path)
    async with app.run_test(size=(180, 40)) as pilot:
        draft = await start_draft(pilot)
        position, msgid = app.tree_position(), app.draft_msgid()
        draft.insert('Keep me.')
        await pilot.press('escape', 'escape')  # draft -> tree -> close, no prompt
        await settle(pilot)
        assert app.reading is None
        assert app.quit_prompt() is None
        assert 'Draft saved' in app.status_text()
        assert store.msgids() == {msgid}

        await pilot.press('enter')
        await settle(pilot)
        assert app.reader_panes() == ('tree', 'content', 'draft')
        assert app.tree_position() == position
        assert 'Keep me.' in app.query_one('#draft').text


async def test_a_new_session_restores_saved_drafts(demo_root, tmp_path):
    app, _, store = make_saved(demo_root, tmp_path)
    async with app.run_test(size=(180, 40)) as pilot:
        draft = await start_draft(pilot)
        draft.insert('Across restarts.')
    assert len(store.msgids()) == 1  # Saved on exit too.

    again, _, _ = make_saved(demo_root, tmp_path)
    async with again.run_test(size=(180, 40)) as pilot:
        await reading(pilot)
        assert 'Across restarts.' in again.query_one('#draft').text


async def test_send_and_discard_delete_the_saved_draft(demo_root, tmp_path):
    app, _, store = make_saved(demo_root, tmp_path)
    async with app.run_test(size=(180, 40)) as pilot:
        draft = await start_draft(pilot)
        draft.insert('Throw away.')
        await pilot.pause(0.8)
        assert store.msgids()
        await pilot.press('ctrl+g')
        await pilot.pause()
        await pilot.press('y')
        await pilot.pause()
        assert store.msgids() == set()

        draft = app.query_one('#draft')
        await pilot.press('r')
        await settle(pilot)
        draft.insert('Reviewed-by: Example Maintainer <maintainer@example.org>\n')
        await pilot.pause(0.8)
        assert store.msgids()
        await pilot.press('ctrl+s')
        await pilot.pause()
        with (
            mock.patch.object(app, 'suspend', contextlib.nullcontext),
            mock.patch('b4.get_smtp', return_value=(None, 'maintainer@example.org')),
            mock.patch('b4.send_mail', side_effect=lambda s, m, **k: 1),
        ):
            await pilot.press('ctrl+y')
            await settle(pilot)
        assert store.msgids() == set()


class RecordingActions(DraftActions):
    def __init__(self, root):
        super().__init__(root)
        self.replied_to = []

    def prepare_reply(self, item, msgid):
        self.replied_to.append(msgid)
        return super().prepare_reply(item, msgid)


async def test_r_in_the_series_list_replies_to_the_cover_letter(demo_root):
    actions = RecordingActions(demo_root)
    app = MaintainerApp(actions.load, actions=actions)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        item = app.query_one(SeriesList).highlighted_item
        assert item.series.has_cover
        assert 'r' in bindings(app)
        await pilot.press('r')
        await settle(pilot)
        assert app.reading is item
        assert actions.replied_to == [item.series.msgids[0]]  # The cover letter.
        assert app.reader_panes() == ('tree', 'content', 'draft')
        assert app.tree_labels()[0].startswith('0/5 cover') or 'cover' in app.tree_labels()[0]


async def test_r_in_the_series_list_replies_to_the_first_patch_without_a_cover(demo_root):
    actions = RecordingActions(demo_root)
    app = MaintainerApp(actions.load, actions=actions)
    async with app.run_test(size=(180, 40)) as pilot:
        series_list = (await settle(pilot)) or app.query_one(SeriesList)
        target = next(i for i in app.index.items(app.current_view) if not i.series.has_cover)
        series_list.select_identity(target.identity)
        await pilot.press('r')
        await settle(pilot)
        assert app.reading is target
        assert actions.replied_to == [target.series.patches[0].msgid]


async def test_r_in_the_series_list_reopens_a_saved_draft_instead(demo_root, tmp_path):
    from b4_maintainer.adapters.drafts import DraftStore

    store = DraftStore(tmp_path / 'drafts')
    first = RecordingActions(demo_root)
    app = MaintainerApp(first.load, actions=first, drafts=store)
    async with app.run_test(size=(180, 40)) as pilot:
        await reading(pilot)
        await pilot.press('down', 'down')  # A patch, not the cover letter.
        await pilot.press('r')
        await settle(pilot)
        app.query_one('#draft').insert('On the patch.')
        msgid = app.draft_msgid()
        assert msgid != app.reading.series.msgids[0]
    assert store.msgids() == {msgid}

    actions = RecordingActions(demo_root)
    again = MaintainerApp(actions.load, actions=actions, drafts=store)
    async with again.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        await pilot.press('r')
        await settle(pilot)
        assert actions.replied_to == [msgid]  # Not the cover letter: one draft at a time.
        assert 'On the patch.' in again.query_one('#draft').text

"""Contract: with an external editor, drafts are written in it beside the browsable reader."""

import contextlib
from unittest import mock

from test_draft import DraftActions, reading, settle

from b4_maintainer.adapters.drafts import DraftStore
from b4_maintainer.adapters.tmux import EditorError
from b4_maintainer.app import MaintainerApp


class FakeEditor:
    """Panes are names; the test plays the editor by writing the file and exiting."""

    def __init__(self):
        self.opened = []  # (pane, path)
        self.live = set()
        self.focused = []
        self.closed = []
        self.fail = False

    def open(self, path, checkout):
        if self.fail:
            raise EditorError('tmux split-window failed: no server')
        pane = f'%{len(self.opened) + 1}'
        self.opened.append((pane, path))
        self.live.add(pane)
        return pane

    def alive(self, pane):
        return pane in self.live

    def runs(self, pane, path):
        return pane in self.live and any(p == pane and q == path for p, q in self.opened)

    def focus(self, pane):
        self.focused.append(pane)

    def close(self, pane):
        self.closed.append(pane)
        self.live.discard(pane)

    def write(self, text):
        _, path = self.opened[-1]
        path.write_text(text)

    def exit(self):
        self.live.discard(self.opened[-1][0])


class CapturingActions(DraftActions):
    def __init__(self, root):
        super().__init__(root)
        self.targets = []

    def prepare_reply(self, item, msgid):
        target = super().prepare_reply(item, msgid)
        self.targets.append(target)
        return target


def make(demo_root, tmp_path, editor=None):
    actions = CapturingActions(demo_root)
    store = DraftStore(tmp_path / 'drafts')
    editor = editor or FakeEditor()
    app = MaintainerApp(actions.load, actions=actions, drafts=store, editor=editor)
    return app, actions, store, editor


def bindings(app):
    return {k: b.binding.description for k, b in app.screen.active_bindings.items()}


async def start(pilot):
    await reading(pilot)
    await pilot.press('r')
    await settle(pilot)


async def watched(pilot):
    """Let the pane watcher (polling about every second) notice the editor's state."""
    await pilot.pause(1.3)
    await settle(pilot)


async def test_reply_opens_the_editor_on_the_draft_file(demo_root, tmp_path):
    app, _, store, editor = make(demo_root, tmp_path)
    async with app.run_test(size=(180, 40)) as pilot:
        await start(pilot)
        [(pane, path)] = editor.opened
        msgid = app.draft_msgid()
        assert path == store.text_path(msgid)
        assert path.read_text().startswith('On , Contributor One wrote:\n> The patch body.')
        assert app.reader_panes() == ('tree', 'content')  # No in-app Draft pane.
        assert 'Drafting in your editor' in app.status_text()
        assert 'r' not in bindings(app)
        assert store.get(msgid).pane == pane
        await pilot.press('e')
        assert editor.focused == [pane]


async def test_editor_exit_opens_b4s_preview_and_send_cleans_up(demo_root, tmp_path):
    app, actions, store, editor = make(demo_root, tmp_path)
    sent = []
    async with app.run_test(size=(180, 40)) as pilot:
        await start(pilot)
        msgid = app.draft_msgid()
        editor.write(editor.opened[-1][1].read_text() + 'Looks good.\n')
        editor.exit()
        await watched(pilot)
        assert type(app.screen).__name__ == 'FollowupReplyPreviewScreen'
        with (
            mock.patch.object(app, 'suspend', contextlib.nullcontext),
            mock.patch('b4.get_smtp', return_value=(None, 'maintainer@example.org')),
            mock.patch('b4.send_mail', side_effect=lambda s, m, **k: sent.extend(m) or 1),
        ):
            await pilot.press('ctrl+y')
            await settle(pilot)
        assert len(sent) == 1
        assert 'Looks good.' in actions.targets[-1].lmsg.make_reply.call_args.args[0]
        assert actions.answered_calls == [msgid]
        assert store.msgids() == set()
        assert 'r' in bindings(app)


async def test_escape_in_the_preview_keeps_the_draft_and_r_reopens_it(demo_root, tmp_path):
    app, _, store, editor = make(demo_root, tmp_path)
    async with app.run_test(size=(180, 40)) as pilot:
        await start(pilot)
        msgid = app.draft_msgid()
        editor.write('On , Contributor One wrote:\n> The patch body.\n\nLater.\n')
        editor.exit()
        await watched(pilot)
        await pilot.press('escape')
        await settle(pilot)
        assert store.get(msgid).text.endswith('Later.\n')
        assert app.draft_msgid() is None
        await pilot.press('r')
        await settle(pilot)
        assert len(editor.opened) == 2 and editor.opened[1][1] == store.text_path(msgid)
        assert store.get(msgid).text.endswith('Later.\n')


async def test_unchanged_editor_exit_drops_the_draft(demo_root, tmp_path):
    app, _, store, editor = make(demo_root, tmp_path)
    async with app.run_test(size=(180, 40)) as pilot:
        await start(pilot)
        editor.exit()
        await watched(pilot)
        assert type(app.screen).__name__ != 'FollowupReplyPreviewScreen'
        assert store.msgids() == set()
        assert app.draft_msgid() is None


async def test_ctrl_s_previews_while_the_editor_stays_open(demo_root, tmp_path):
    app, _, _, editor = make(demo_root, tmp_path)
    async with app.run_test(size=(180, 40)) as pilot:
        await start(pilot)
        editor.write('On , Contributor One wrote:\n> x\n\nSaved so far.\n')
        await pilot.press('ctrl+s')
        await pilot.pause()
        assert type(app.screen).__name__ == 'FollowupReplyPreviewScreen'
        await pilot.press('e')  # Back to the editor pane.
        await pilot.pause()
        assert editor.focused[-1] == editor.opened[-1][0]
        assert editor.alive(editor.opened[-1][0])


async def test_discard_kills_the_editor_and_deletes_the_draft(demo_root, tmp_path):
    app, _, store, editor = make(demo_root, tmp_path)
    async with app.run_test(size=(180, 40)) as pilot:
        await start(pilot)
        editor.write('half a thought\n')
        await pilot.press('ctrl+g')
        await pilot.pause()
        await pilot.press('y')
        await settle(pilot)
        assert editor.closed == [editor.opened[-1][0]]
        assert store.msgids() == set()


async def test_closing_the_reader_keeps_the_editor_and_reopening_focuses_it(demo_root, tmp_path):
    app, _, store, editor = make(demo_root, tmp_path)
    async with app.run_test(size=(180, 40)) as pilot:
        await start(pilot)
        pane = editor.opened[-1][0]
        editor.write('On , Contributor One wrote:\n> x\n\nWIP\n')
        await pilot.press('escape')
        await settle(pilot)
        assert app.reading is None and editor.alive(pane)
        await pilot.press('enter')
        await settle(pilot)
        assert len(editor.opened) == 1 and editor.focused[-1] == pane


async def test_quitting_closes_the_editor_and_keeps_written_text(demo_root, tmp_path):
    app, _, store, editor = make(demo_root, tmp_path)
    async with app.run_test(size=(180, 40)) as pilot:
        await start(pilot)
        msgid = app.draft_msgid()
        editor.write('On , Contributor One wrote:\n> x\n\nKeep.\n')
        await pilot.press('Q')
        await pilot.pause()
        assert 'editor' in app.quit_prompt()
        await pilot.press('y')
        await pilot.pause()
    assert editor.closed == [editor.opened[-1][0]]
    assert store.get(msgid).text.endswith('Keep.\n')


async def test_a_crashed_sessions_pane_is_adopted(demo_root, tmp_path):
    app, _, store, editor = make(demo_root, tmp_path)
    async with app.run_test(size=(180, 40)) as pilot:
        await start(pilot)
        msgid = app.draft_msgid()
        editor.write('On , Contributor One wrote:\n> x\n\nWIP\n')
        app._draft = None  # The session dies without closing its pane.
    editor.closed.clear()
    editor.live.add(store.get(msgid).pane)

    again, _, _, _ = make(demo_root, tmp_path, editor=editor)
    async with again.run_test(size=(180, 40)) as pilot:
        await reading(pilot)
        await settle(pilot)
        assert len(editor.opened) == 1
        assert editor.focused[-1] == store.get(msgid).pane
        assert again.draft_msgid() == msgid


async def test_editor_failure_falls_back_to_the_in_app_draft(demo_root, tmp_path):
    editor = FakeEditor()
    editor.fail = True
    app, _, _, _ = make(demo_root, tmp_path, editor=editor)
    async with app.run_test(size=(180, 40)) as pilot:
        await start(pilot)
        assert app.reader_panes() == ('tree', 'content', 'draft')
        assert 'no server' in app.status_text()


async def test_after_the_preview_one_escape_closes_the_reader(demo_root, tmp_path):
    app, _, store, editor = make(demo_root, tmp_path)
    async with app.run_test(size=(180, 40)) as pilot:
        await start(pilot)
        editor.write('On , Contributor One wrote:\n> The patch body.\n\nLater.\n')
        editor.exit()
        await watched(pilot)
        await pilot.press('escape')  # Leaves b4's preview; the draft is kept.
        await settle(pilot)
        assert app.reading is not None
        await pilot.press('escape')
        await settle(pilot)
        assert app.reading is None


async def test_with_the_editor_open_one_escape_closes_the_reader(demo_root, tmp_path):
    app, _, store, editor = make(demo_root, tmp_path)
    async with app.run_test(size=(180, 40)) as pilot:
        await start(pilot)
        await pilot.press('escape')
        await settle(pilot)
        assert app.reading is None and editor.alive(editor.opened[-1][0])

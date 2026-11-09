"""Contract: outside tmux, a reply is written in the maintainer's editor in the app's own
terminal: the app suspends until the editor exits, then offers b4's preview and send as
the tmux pane does. The in-app draft pane is only for [drafts] editor = "app"."""

import contextlib
from unittest import mock

from test_draft import reading, settle
from test_editor_drafts import CapturingActions

from b4_maintainer.adapters.drafts import DraftStore
from b4_maintainer.app import MaintainerApp


class FakeTerminal:
    """Plays the editor: appends *text* to the draft file, as if written and :wq."""

    def __init__(self, text='Looks good.\n'):
        self.text = text
        self.edited = []  # (path, checkout)

    def edit(self, path, checkout):
        self.edited.append((path, checkout))
        if self.text:
            path.write_text(path.read_text() + self.text)


def make(demo_root, tmp_path, terminal):
    actions = CapturingActions(demo_root)
    store = DraftStore(tmp_path / 'drafts')
    app = MaintainerApp(actions.load, actions=actions, drafts=store, terminal_editor=terminal)
    return app, store


async def test_reply_suspends_the_app_for_the_editor_then_offers_preview(demo_root, tmp_path):
    terminal = FakeTerminal()
    app, store = make(demo_root, tmp_path, terminal)
    suspended = []

    @contextlib.contextmanager
    def suspend():
        suspended.append(True)
        yield

    async with app.run_test(size=(180, 40)) as pilot:
        await reading(pilot)
        with mock.patch.object(app, 'suspend', suspend):
            await pilot.press('r')
            await settle(pilot)
        [(path, _checkout)] = terminal.edited
        assert suspended == [True]
        msgid = next(iter(store.msgids()))
        assert path == store.text_path(msgid)
        assert store.get(msgid).text.endswith('Looks good.\n')
        assert type(app.screen).__name__ == 'FollowupReplyPreviewScreen'
        assert 'draft' not in app.reader_panes()  # Never the in-app pane.


async def test_an_editor_that_wrote_nothing_drops_the_draft(demo_root, tmp_path):
    app, store = make(demo_root, tmp_path, FakeTerminal(text=''))
    async with app.run_test(size=(180, 40)) as pilot:
        await reading(pilot)
        with mock.patch.object(app, 'suspend', contextlib.nullcontext):
            await pilot.press('r')
            await settle(pilot)
        assert store.msgids() == set()
        assert 'nothing written' in app.status_text()


def test_outside_tmux_the_terminal_editor_is_used_unless_the_app_is_asked_for(
    tmp_path, monkeypatch
):
    from b4_maintainer.adapters.tmux import TerminalEditor, TmuxEditor
    from b4_maintainer.cli import _draft_editors

    class Source:
        def __init__(self, editor):
            self.editor = editor

        def workspace(self):
            from b4_maintainer.workspace import Workspace

            return Workspace.from_config({'drafts': {'editor': self.editor}})

    monkeypatch.delenv('TMUX', raising=False)
    tmux, terminal = _draft_editors(Source('auto'))
    assert tmux is None and isinstance(terminal, TerminalEditor)
    assert _draft_editors(Source('app')) == (None, None)
    monkeypatch.setenv('TMUX', '/tmp/tmux-1000/default,1,0')
    tmux, terminal = _draft_editors(Source('auto'))
    assert isinstance(tmux, TmuxEditor) and isinstance(terminal, TerminalEditor)
    tmux, terminal = _draft_editors(Source('terminal'))
    assert tmux is None and isinstance(terminal, TerminalEditor)

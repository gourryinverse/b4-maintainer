"""Contract: Enter on a workbench's Mailboxes row opens that thread in the Workbench tab
(its tree, the selected message); r and R reply to the selected message there: with the
tmux editor the draft opens beside the app and ctrl+s sends from the tab, without it the
reply opens on LKML. Escape closes the thread."""

from test_draft import settle
from test_editor_drafts import CapturingActions, FakeEditor
from test_workbench_screen import FakeService

from b4_maintainer.adapters.drafts import DraftStore
from b4_maintainer.app import MaintainerApp
from b4_maintainer.workbench import Entry, Item, Workbench, WorkbenchStore
from b4_maintainer.workbenchscreen import WorkbenchScreen


class MsgidActions(CapturingActions):
    def __init__(self, root):
        super().__init__(root)
        self.msgids = []

    def prepare_reply(self, item, msgid):
        self.msgids.append(msgid)
        return super().prepare_reply(item, msgid)


def make(demo_root, tmp_path, editor=True):
    actions = MsgidActions(demo_root)
    benches = WorkbenchStore(tmp_path / 'state' / 'workbenches')
    app = MaintainerApp(
        actions.load,
        actions=actions,
        drafts=DraftStore(tmp_path / 'drafts'),
        editor=FakeEditor() if editor else None,
        workbenches=FakeService(tmp_path, benches, lambda: []),
    )
    return app, actions, benches


async def open_thread(pilot, benches):
    app = pilot.app
    await settle(pilot)
    item = app.snapshot.items[0]
    s = item.series
    benches.save(
        Workbench('w', 'W', (Entry('linux', '', (Item(item.identity, s.revision, s.subject),)),))
    )
    await pilot.press('f3')
    await settle(pilot)
    screen = app.screen
    screen.select_row('mailbox')
    await pilot.press('enter')
    await settle(pilot)
    return screen, item


async def test_a_mailbox_opens_its_thread_in_the_tab_and_escape_closes_it(demo_root, tmp_path):
    app, _actions, benches = make(demo_root, tmp_path)
    async with app.run_test(size=(200, 50)) as pilot:
        screen, item = await open_thread(pilot, benches)
        assert isinstance(screen, WorkbenchScreen) and screen.thread_open()
        labels = screen.thread_labels()
        assert len(labels) >= 2 and item.series.subject  # The postings, at least.
        assert screen.thread_text()  # The cover letter (or first patch).
        await pilot.press('escape')
        await settle(pilot)
        assert app.screen is screen and not screen.thread_open()
        assert screen.query_one('#bench-contents').display


async def test_r_replies_from_the_tab_with_the_tmux_editor(demo_root, tmp_path):
    app, actions, benches = make(demo_root, tmp_path)
    async with app.run_test(size=(200, 50)) as pilot:
        screen, item = await open_thread(pilot, benches)
        await pilot.press('down')  # The next row of the thread.
        await settle(pilot)
        msgid = screen.thread_msgid()
        await pilot.press('r')
        await settle(pilot)
        assert app.screen is screen  # Still the Workbench tab.
        assert actions.msgids and actions.msgids[-1] == msgid
        assert app._editor.opened  # The draft beside the app.
        assert app.check_action('send_draft', ())  # ctrl+s sends from here.


async def test_without_the_tmux_editor_r_replies_on_lkml(demo_root, tmp_path):
    app, actions, benches = make(demo_root, tmp_path, editor=False)
    async with app.run_test(size=(200, 50)) as pilot:
        screen, item = await open_thread(pilot, benches)
        msgid = screen.thread_msgid()
        await pilot.press('r')
        await settle(pilot)
        assert len(app.screen_stack) == 1  # LKML.
        assert app.reading is not None and app.reading.identity == item.identity
        assert actions.msgids[-1] == msgid
        assert app.query_one('#draft').display

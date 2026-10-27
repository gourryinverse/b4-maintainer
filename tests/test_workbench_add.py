"""Contract: w (shown in the footer) adds a series or a discussion to a workbench; on the
Workbench tab, a adds a thread by lore link or Message-ID (fetched into the cache) or a
file from disk, under the selected project (else the item's, else the only one)."""

import dataclasses

from test_workbench_screen import FakeService

from b4_maintainer.adapters.fixture import FixtureSource
from b4_maintainer.app import MaintainerApp
from b4_maintainer.branchscreen import Ask, Pick
from b4_maintainer.fixtures import write_series_mbox
from b4_maintainer.workbench import Entry, Item, Workbench, WorkbenchStore
from b4_maintainer.workspace import Snapshot


class Actions:
    """Enough of the live source: fetch a thread (here: one the demo has) by link."""

    def __init__(self, root, talk=False):
        self.fixture = FixtureSource(root)
        self.talk = talk  # The first item is a discussion.
        self.fetched = []

    def load(self):
        snapshot = self.fixture.load()
        items = list(snapshot.items)
        if self.talk:
            first = items[0]
            items[0] = dataclasses.replace(
                first, series=dataclasses.replace(first.series, discussion=True)
            )
        return Snapshot(snapshot.workspace, tuple(items))

    def fetch_thread(self, target):
        self.fetched.append(target)
        return target.rstrip('/').rsplit('/', 1)[-1]


def make(demo_root, tmp_path, talk=False):
    store = WorkbenchStore(tmp_path / 'state' / 'workbenches')
    actions = Actions(demo_root, talk)
    app = MaintainerApp(
        actions.load, actions=actions, workbenches=FakeService(tmp_path, store, lambda: [])
    )
    return app, store, actions


async def settle(pilot):
    for _ in range(3):
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()


def footer_keys(app):
    return {key for key, active in app.screen.active_bindings.items() if active.binding.show}


async def test_w_is_in_the_footer_and_adds_a_discussion_as_mail(demo_root, tmp_path):
    app, store, _actions = make(demo_root, tmp_path, talk=True)
    async with app.run_test(size=(200, 50)) as pilot:
        await settle(pilot)
        assert 'w' in footer_keys(app)
        app.select_view('discussions')  # Discussions are listed apart.
        await pilot.pause()
        talk = app.query_one('SeriesList').highlighted_item
        assert talk.series.discussion
        await pilot.press('w')
        await settle(pilot)
        await pilot.press('enter')  # New workbench (the only choice).
        await settle(pilot)
        await pilot.press(*'Talks', 'enter')
        await settle(pilot)
        [item] = store.get('talks').entries[0].items
        assert (item.mail, item.msgid) == (talk.identity, talk.series.msgids[0])


async def test_a_adds_a_thread_by_link_under_the_selected_project(demo_root, tmp_path):
    app, store, actions = make(demo_root, tmp_path)
    async with app.run_test(size=(200, 50)) as pilot:
        await settle(pilot)
        wanted = app.snapshot.items[1]
        store.save(Workbench('w', 'W', (Entry('linux', '', (Item(branch='me/fix'),)),)))
        await pilot.press('f3')
        await settle(pilot)
        screen = app.screen
        screen.select_row('entry', 'linux')
        await pilot.press('a')
        await settle(pilot)
        assert isinstance(app.screen, Pick)
        await pilot.press('enter')  # A thread by link.
        await settle(pilot)
        assert isinstance(app.screen, Ask)
        link = f'https://lore.kernel.org/linux-mm/{wanted.series.msgids[0]}/'
        app.screen.query_one('Input').value = link
        await pilot.press('enter')
        await settle(pilot)
        assert actions.fetched == [link]
        added = store.get('w').entries[0].items[-1]
        assert (added.series, added.msgid) == (wanted.identity, wanted.series.msgids[0])


async def test_a_adds_a_file_from_disk(demo_root, tmp_path):
    app, store, _actions = make(demo_root, tmp_path)
    patch = write_series_mbox(tmp_path / 'fix.mbx', subject='mm: fix it').path
    async with app.run_test(size=(200, 50)) as pilot:
        await settle(pilot)
        store.save(Workbench('w', 'W', (Entry('linux', '', (Item(branch='me/fix'),)),)))
        await pilot.press('f3')
        await settle(pilot)
        await pilot.press('a')  # From the workbench list: no project selected.
        await settle(pilot)
        await pilot.press('down', 'enter')  # A file.
        await settle(pilot)
        app.screen.query_one('Input').value = str(patch)
        await pilot.press('enter')
        await settle(pilot)
        assert isinstance(app.screen, Pick)  # Which project? The workbench's first.
        assert [key for key, _label in app.screen.choices][:2] == ['linux', 'qemu']
        await pilot.press('down', 'enter')  # qemu.
        await settle(pilot)
        added = store.get('w').entry('qemu').items[-1]
        assert added.file == str(patch)
        screen = app.screen
        screen.select_row('item', 'qemu', 0)
        await pilot.press('m')  # Moved to linux after all.
        await settle(pilot)
        assert isinstance(app.screen, Pick)
        await pilot.press('enter')
        await settle(pilot)
        assert store.get('w').entry('qemu').items == ()  # Emptied; it stays.
        assert store.get('w').entry('linux').items[-1].file == str(patch)
        await pilot.press('a')
        await settle(pilot)
        await pilot.press('down', 'enter')
        await settle(pilot)
        app.screen.query_one('Input').value = str(tmp_path / 'nope.patch')
        await pilot.press('enter')
        await settle(pilot)
        assert 'does not exist' in app.screen.status_text()


async def test_a_adds_artifacts_from_another_workbenchs_sessions(demo_root, tmp_path):
    from sessiondata import FakeSessionRunner, FakeWorktrees, make_record

    from b4_maintainer.attachdialog import AttachDialog
    from b4_maintainer.sessions.hub import SessionHub
    from b4_maintainer.sessions.store import SessionStore

    sessions = SessionStore(tmp_path / 'var' / 'sessions')
    runner = FakeSessionRunner(sessions)
    hub = SessionHub(sessions, runner, FakeWorktrees())
    store = WorkbenchStore(tmp_path / 'state' / 'workbenches')
    store.save(Workbench('cmp', 'Compare', (Entry('linux', '', ()),)))
    store.save(Workbench('w', 'W', (Entry('linux', '', ()), Entry('qemu', '', ()))))
    runner.add(
        make_record('c1', scope='workbench', series='workbench:cmp', workbench='cmp'), alive=False
    )
    out = sessions.directory('c1') / 'work' / 'out'
    out.mkdir(parents=True)
    (out / 'take-b.diff').write_text('--- a/x\n+++ b/x\n')
    app = MaintainerApp(
        FixtureSource(demo_root).load,
        sessions=hub,
        workbenches=FakeService(tmp_path, store, hub.records, sessions.directory),
    )
    async with app.run_test(size=(200, 50)) as pilot:
        await settle(pilot)
        await pilot.press('f3')
        await settle(pilot)
        screen = app.screen
        while screen.bench().id != 'w':
            await pilot.press('down')
        await pilot.press('a')
        await settle(pilot)
        keys = [key for key, _label in app.screen.choices]
        await pilot.press(*(['down'] * keys.index('artifact')), 'enter')
        await settle(pilot)
        picker = app.screen
        assert isinstance(picker, AttachDialog)
        labels = [a.label for a in picker.offered]
        assert any('take-b.diff' in label and 'Compare' in label for label in labels)
        index = next(n for n, label in enumerate(labels) if 'take-b.diff' in label)
        await pilot.press(*(['down'] * index), 'space', 'enter')
        await settle(pilot)
        assert isinstance(app.screen, Pick)  # Which project for the files?
        await pilot.press('down', 'enter')  # qemu.
        await settle(pilot)
        assert store.get('w').entry('qemu').items == (Item(file=str(out / 'take-b.diff')),)

"""Contract: R offers every session's output, the series' own first, then that of the
workbenches holding the series (labelled with the workbench), then the rest; typing after
/ filters the list, and ticks survive the filter."""

from sessiondata import FakeSessionRunner, FakeWorktrees, make_record
from test_draft import DraftActions, reading, settle
from test_workbench_screen import FakeService

from b4_maintainer.app import MaintainerApp, SeriesList
from b4_maintainer.attachdialog import AttachDialog
from b4_maintainer.sessions.hub import SessionHub
from b4_maintainer.sessions.store import SessionStore
from b4_maintainer.workbench import Entry, Item, Workbench, WorkbenchStore


def make(demo_root, tmp_path):
    actions = DraftActions(demo_root)
    store = SessionStore(tmp_path / 'var' / 'sessions')
    runner = FakeSessionRunner(store)
    hub = SessionHub(store, runner, FakeWorktrees(), sandbox=lambda: True)
    benches = WorkbenchStore(tmp_path / 'state' / 'workbenches')
    service = FakeService(tmp_path, benches, hub.records, store.directory)
    app = MaintainerApp(actions.load, actions=actions, sessions=hub, workbenches=service)
    return app, store, runner, benches


def output(store, sid, name, text):
    out = store.directory(sid) / 'work' / 'out'
    out.mkdir(parents=True, exist_ok=True)
    (out / name).write_text(text)


async def test_r_offers_any_sessions_output_its_own_and_its_workbenches_first(demo_root, tmp_path):
    app, store, runner, benches = make(demo_root, tmp_path)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        item = app.query_one(SeriesList).highlighted_item
        other = app.snapshot.items[1]
        benches.save(
            Workbench(
                'cmp',
                'Compare fixes',
                (
                    Entry(
                        'linux',
                        '',
                        (
                            Item(item.identity, item.series.revision, item.series.subject),
                            Item(other.identity, other.series.revision, other.series.subject),
                        ),
                    ),
                ),
            )
        )
        runner.add(make_record('z1', series='change-id:unrelated'), alive=False)
        output(store, 'z1', 'notes.md', 'unrelated notes\n')
        runner.add(
            make_record('w1', scope='workbench', series='workbench:cmp', workbench='cmp'),
            alive=False,
        )
        output(store, 'w1', 'fixup.diff', '--- a/mm/x.c\n+++ b/mm/x.c\n@@ fixup @@\n')
        runner.add(make_record('s1', series=item.identity), alive=False)
        output(store, 's1', 'own.md', 'own results\n')
        await reading(pilot)
        await pilot.press('R')
        await settle(pilot)
        dialog = app.screen
        assert isinstance(dialog, AttachDialog)
        labels = [a.label for a in dialog.offered]
        own = labels.index(next(label for label in labels if 'own.md' in label))
        bench = labels.index(next(label for label in labels if 'fixup.diff' in label))
        rest = labels.index(next(label for label in labels if 'notes.md' in label))
        assert own < bench < rest
        assert 'Compare fixes' in labels[bench]
        await pilot.press('slash', *'fixup')
        await settle(pilot)
        assert [label for label in dialog.visible_labels()] == [labels[bench]]
        await pilot.press('down')  # Back to the list.
        await pilot.press('space')
        await pilot.press('slash', 'ctrl+u')  # The filter cleared: everything again.
        await settle(pilot)
        assert len(dialog.visible_labels()) == len(labels)
        assert [a.label for a in dialog.ticked()] == [labels[bench]]
        await pilot.press('enter')
        await settle(pilot)
        assert '@@ fixup @@' in app.query_one('#draft').text

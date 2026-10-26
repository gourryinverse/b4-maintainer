"""Contract: R replies with chosen session output at the end of the draft; r and M stay
as they were (M is read-all now)."""

from sessiondata import FakeSessionRunner, FakeWorktrees, make_record
from test_draft import DraftActions, bindings, reading, settle

from b4_maintainer.app import MaintainerApp, SeriesList
from b4_maintainer.attachdialog import AttachDialog
from b4_maintainer.sessions.hub import SessionHub
from b4_maintainer.sessions.store import SessionStore


def make(demo_root, tmp_path, output=True, drafts=None):
    actions = DraftActions(demo_root)
    store = SessionStore(tmp_path / 'var' / 'sessions')
    runner = FakeSessionRunner(store)
    hub = SessionHub(store, runner, FakeWorktrees(), sandbox=lambda: True)
    app = MaintainerApp(actions.load, actions=actions, sessions=hub, drafts=drafts)
    return app, store, runner, output


def give_output(app, store, runner):
    """A session of the highlighted series left a results file and a reproducer."""
    item = app.query_one(SeriesList).highlighted_item
    runner.add(make_record('s1', series=item.identity), alive=False)
    out = store.directory('s1') / 'work' / 'out'
    (out / 'tests').mkdir(parents=True)
    (out / 'tests' / 'run.log').write_text('ok 1 selftest\nok 2 selftest\n')
    (out / 'artifacts.toml').write_text(
        '[[artifact]]\nkind = "results"\ntitle = "selftests on v3"\nsummary = "Both pass."\n'
        'path = "out/tests/run.log"\n'
    )
    (out / 'repro.c').write_text('int main(void) { return 0; }\n')


async def test_r_in_the_thread_adds_chosen_output_to_the_draft(demo_root, tmp_path):
    app, store, runner, _ = make(demo_root, tmp_path)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        give_output(app, store, runner)
        await reading(pilot)
        assert 'R' in bindings(app) and 'M' in bindings(app)
        await pilot.press('R')
        await settle(pilot)
        dialog = app.screen
        assert isinstance(dialog, AttachDialog)
        labels = [a.label for a in dialog.offered]
        assert any('results: selftests on v3' in label for label in labels)
        assert any('repro.c' in label for label in labels)
        await pilot.press('space', 'enter')  # The first: the results.
        await settle(pilot)
        draft = app.query_one('#draft')
        text = draft.text
        assert text.startswith('On , Contributor One wrote:\n> The patch body.')
        assert text.index('> The patch body.') < text.index('--- selftests on v3')
        assert 'Both pass.' in text and 'ok 2 selftest' in text
        assert 'int main' not in text  # Not chosen.


async def test_nothing_chosen_is_a_plain_reply_and_escape_is_none(demo_root, tmp_path):
    app, store, runner, _ = make(demo_root, tmp_path)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        give_output(app, store, runner)
        await reading(pilot)
        await pilot.press('R')
        await settle(pilot)
        await pilot.press('escape')
        await settle(pilot)
        assert app.reader_panes() == ('tree', 'content')  # No draft.
        await pilot.press('R')
        await settle(pilot)
        await pilot.press('enter')
        await settle(pilot)
        assert '---' not in app.query_one('#draft').text


async def test_without_session_output_r_says_so(demo_root, tmp_path):
    app, _store, _runner, _ = make(demo_root, tmp_path)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        await reading(pilot)
        await pilot.press('R')
        await settle(pilot)
        assert not isinstance(app.screen, AttachDialog)
        assert 'No session output for this series' in app.status_text()


async def test_r_in_the_series_list_opens_the_thread_then_asks(demo_root, tmp_path):
    app, store, runner, _ = make(demo_root, tmp_path)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        give_output(app, store, runner)
        await pilot.press('R')
        await settle(pilot)
        assert app.reading is not None and isinstance(app.screen, AttachDialog)


async def test_r_adds_to_a_saved_draft(demo_root, tmp_path):
    from b4_maintainer.adapters.drafts import DraftStore

    drafts = DraftStore(tmp_path / 'drafts')
    app, store, runner, _ = make(demo_root, tmp_path, drafts=drafts)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        give_output(app, store, runner)
        await reading(pilot)
        await pilot.press('r')
        await settle(pilot)
        draft = app.query_one('#draft')
        draft.insert('My review words.\n')
        await settle(pilot)
        await pilot.press('escape', 'escape')  # Draft, tree, closed: the draft is saved.
        await settle(pilot)
        assert app.reading is None
        await pilot.press('enter')  # Back at the message; the saved draft waits.
        await settle(pilot)
        await pilot.press('R')
        await settle(pilot)
        await pilot.press('space', 'enter')
        await settle(pilot)
        text = app.query_one('#draft').text
        assert 'My review words.' in text and 'Both pass.' in text


async def test_with_the_tmux_editor_the_draft_file_holds_the_output(demo_root, tmp_path):
    from test_editor_drafts import CapturingActions, FakeEditor

    from b4_maintainer.adapters.drafts import DraftStore

    actions = CapturingActions(demo_root)
    drafts = DraftStore(tmp_path / 'drafts')
    editor = FakeEditor()
    store = SessionStore(tmp_path / 'var' / 'sessions')
    runner = FakeSessionRunner(store)
    hub = SessionHub(store, runner, FakeWorktrees(), sandbox=lambda: True)
    app = MaintainerApp(actions.load, actions=actions, drafts=drafts, editor=editor, sessions=hub)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        give_output(app, store, runner)
        await reading(pilot)
        await pilot.press('R')
        await settle(pilot)
        await pilot.press('space', 'enter')
        await settle(pilot)
        [(_pane, path)] = editor.opened
        text = path.read_text()
        assert text.startswith('On ') and 'Both pass.' in text
        saved = drafts.get(app.draft_msgid())
        assert saved is not None and saved.quote != saved.text  # Not "untouched".

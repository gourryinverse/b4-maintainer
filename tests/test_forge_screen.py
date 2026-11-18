"""Contract: F7 opens the Forge tab. With no forge open it lists the forges; Enter opens
one, and the whole tab is that forge: its rounds, the selected round's thread, and the
selected message. Escape closes the forge back to the list, then returns to LKML."""

import pytest
from forgedata import posted_rounds

from b4_maintainer.adapters.fixture import FixtureSource
from b4_maintainer.app import MaintainerApp
from b4_maintainer.branchscreen import Ask, Pick
from b4_maintainer.forge import ForgeService, ForgeStore
from b4_maintainer.forgescreen import ForgeScreen
from b4_maintainer.widgets import TabBar, TextPane
from b4_maintainer.workbench import WorkbenchStore

ME = ('Example Maintainer', 'maintainer@example.org')


@pytest.fixture(scope='module')
def posted(tmp_path_factory):
    return posted_rounds(tmp_path_factory)


@pytest.fixture
def identity(home):
    """The maintainer's git identity, isolated from the real one."""
    (home / '.gitconfig').write_text(
        '[user]\n\tname = Example Maintainer\n\temail = maintainer@example.org\n'
    )


def make(demo_root, tmp_path):
    state = tmp_path / 'state'
    service = ForgeService(
        ForgeStore(state / 'forges'), WorkbenchStore(state / 'workbenches'), tmp_path / 'var'
    )
    return MaintainerApp(FixtureSource(demo_root).load, forges=service), service


def reviewed(service, posted):
    """A forge with two rounds; the first has the maintainer's reply to patch 1."""
    forge = service.create('Demo work', 'linux', 'mainline/master', 'Add two files.\n')
    first = service.post_round(forge, posted[0])
    _series, thread = service.thread(forge, first)
    service.post_reply(forge, first, thread.patches[0].message.msgid, ME, 'Why f1?\n')
    service.post_round(forge, posted[1])
    return forge


async def settle(pilot):
    for _ in range(3):
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()


async def open_tab(pilot):
    await settle(pilot)
    await pilot.press('f7')
    await settle(pilot)
    screen = pilot.app.screen
    assert isinstance(screen, ForgeScreen)
    return screen


async def test_the_tab_bar_names_the_forge_tab(demo_root, tmp_path):
    app, _service = make(demo_root, tmp_path)
    async with app.run_test(size=(180, 40)) as pilot:
        screen = await open_tab(pilot)
        assert 'F7 Forge' in screen.query_one(TabBar).text()


async def test_no_forges_says_how_to_make_one(demo_root, tmp_path):
    app, _service = make(demo_root, tmp_path)
    async with app.run_test(size=(180, 40)) as pilot:
        screen = await open_tab(pilot)
        assert screen.forge_labels() == []
        assert 'n makes a forge' in screen.status_text()
        assert not screen.is_open()


async def test_enter_opens_a_forge_on_its_newest_round(demo_root, tmp_path, posted):
    app, service = make(demo_root, tmp_path)
    reviewed(service, posted)
    async with app.run_test(size=(180, 40)) as pilot:
        screen = await open_tab(pilot)
        assert screen.forge_labels() == ['Demo work · linux on mainline/master · v1 r2']
        await pilot.press('enter')
        await settle(pilot)
        assert screen.is_open()
        assert screen.round_labels() == ['v1 r1', 'v1 r2']
        assert screen.selected_round() == 'v1 r2'
        labels = screen.thread_labels()
        assert any('demo: add f1' in label for label in labels)
        assert any('demo: add f2' in label for label in labels)
        assert 'A cover letter.' in screen.content_text()  # The cover letter first.


async def test_an_earlier_round_shows_its_review(demo_root, tmp_path, posted):
    app, service = make(demo_root, tmp_path)
    reviewed(service, posted)
    async with app.run_test(size=(180, 40)) as pilot:
        screen = await open_tab(pilot)
        await pilot.press('enter')
        await settle(pilot)
        screen.select_round('v1 r1')
        await settle(pilot)
        labels = screen.thread_labels()
        assert any('Example Maintainer' in label for label in labels)
        screen.select_message(next(n for n, x in enumerate(labels) if 'Example Maintainer' in x))
        await settle(pilot)
        assert 'Why f1?' in screen.content_text()


async def test_escape_closes_the_forge_then_leaves_the_tab(demo_root, tmp_path, posted):
    app, service = make(demo_root, tmp_path)
    reviewed(service, posted)
    async with app.run_test(size=(180, 40)) as pilot:
        screen = await open_tab(pilot)
        await pilot.press('enter')
        await settle(pilot)
        await pilot.press('escape')
        await settle(pilot)
        assert not screen.is_open()
        assert screen.forge_labels() == ['Demo work · linux on mainline/master · v1 r2']
        await pilot.press('escape')
        await settle(pilot)
        assert not isinstance(app.screen, ForgeScreen)


async def test_n_makes_a_forge_and_starts_nothing(demo_root, tmp_path):
    app, service = make(demo_root, tmp_path)
    async with app.run_test(size=(180, 40)) as pilot:
        screen = await open_tab(pilot)
        await pilot.press('n')
        await settle(pilot)
        assert isinstance(app.screen, Ask)
        await pilot.press(*'CXL rework', 'enter')
        await settle(pilot)
        assert isinstance(app.screen, Pick)  # Two projects in the demo.
        await pilot.press('enter')  # linux, the first.
        await settle(pilot)
        assert isinstance(app.screen, Ask)  # The base.
        await pilot.press(*'mainline/master', 'enter')
        await settle(pilot)
        assert app.screen is screen
        (forge,) = service.store.list()[0]
        assert (forge.label, forge.project, forge.base) == (
            'CXL rework',
            'linux',
            'mainline/master',
        )
        assert service.rounds(forge) == []  # Nothing posted; no session service to start.
        # Made, it opens: no goal yet, and how to give one.
        assert screen.is_open()
        assert 'No goal yet' in screen.content_text()
        assert 'e writes it' in screen.content_text() and 'P' in screen.content_text()
        await pilot.press('escape')
        await settle(pilot)
        assert screen.forge_labels() == ['CXL rework · linux on mainline/master · nothing posted']


async def test_without_a_forge_service_f7_does_nothing(demo_root):
    app = MaintainerApp(FixtureSource(demo_root).load)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        await pilot.press('f7')
        await settle(pilot)
        assert not isinstance(app.screen, ForgeScreen)


async def open_forge(pilot):
    screen = await open_tab(pilot)
    await pilot.press('enter')
    await settle(pilot)
    assert screen.is_open()
    return screen


async def test_r_replies_inline_in_the_editor_and_posts_to_the_local_list(
    demo_root, tmp_path, posted, identity
):
    app, service = make(demo_root, tmp_path)
    forge = reviewed(service, posted)
    edits = []

    def edit(path, cwd):
        text = path.read_text()
        edits.append(text)
        lines = text.splitlines()
        path.write_text('\n'.join([*lines[:3], '', 'Say why f2 comes second.', *lines[3:]]))
        return True

    app.edit_in_terminal = edit
    async with app.run_test(size=(180, 40)) as pilot:
        screen = await open_forge(pilot)
        labels = screen.thread_labels()
        screen.select_message(next(n for n, x in enumerate(labels) if 'add f2' in x))
        await settle(pilot)
        await pilot.press('r')
        await settle(pilot)
        assert edits and edits[0].startswith('On ') and '\n> ' in edits[0]  # b4's quote.
        assert 'Reply posted to v1 r2' in screen.status_text()
        _series, thread = service.thread(forge, service.rounds(forge)[-1])
        (reply,) = thread.patches[1].replies
        assert 'Say why f2 comes second.' in reply.message.body
        assert reply.message.author_email == 'maintainer@example.org'
        assert reply.message.subject == 'Re: [PATCH 2/2] demo: add f2'


async def test_an_untouched_reply_posts_nothing(demo_root, tmp_path, posted):
    app, service = make(demo_root, tmp_path)
    forge = reviewed(service, posted)
    app.edit_in_terminal = lambda path, cwd: True
    async with app.run_test(size=(180, 40)) as pilot:
        screen = await open_forge(pilot)
        await pilot.press('r')
        await settle(pilot)
        assert 'no reply posted' in screen.status_text()
        _series, thread = service.thread(forge, service.rounds(forge)[-1])
        assert thread.cover.replies == ()


async def test_d_shows_why_a_round_cannot_be_compared(demo_root, tmp_path, posted):
    app, service = make(demo_root, tmp_path)
    reviewed(service, posted)  # Posted from files: no snapshots.
    async with app.run_test(size=(180, 40)) as pilot:
        screen = await open_forge(pilot)
        await pilot.press('d')
        await settle(pilot)
        assert 'no snapshot to compare' in screen.status_text()


async def test_e_edits_the_prompt(demo_root, tmp_path, posted):
    app, service = make(demo_root, tmp_path)
    forge = reviewed(service, posted)

    def edit(path, cwd):
        path.write_text('Add two files, f2 first.\n')
        return True

    app.edit_in_terminal = edit
    async with app.run_test(size=(180, 40)) as pilot:
        screen = await open_forge(pilot)
        shown = {b.binding.action for b in screen.active_bindings.values() if b.binding.show}
        assert 'edit_prompt' in shown  # In the footer, not just a hint.
        folder = service.write_context(forge)  # As a launched session has it.
        await pilot.press('e')
        await settle(pilot)
        assert service.store.prompt(forge.id) == 'Add two files, f2 first.\n'
        # A running session reads the new goal.
        assert (folder / 'prompt.md').read_text() == 'Add two files, f2 first.\n'
        assert 'sessions read it' in screen.status_text()
        await pilot.press('t')
        await settle(pilot)
        assert 'Add two files, f2 first.' in screen.content_text()  # The timeline gives it.


async def test_a_forge_with_nothing_posted_shows_its_goal(demo_root, tmp_path):
    app, service = make(demo_root, tmp_path)
    forge = service.create('Demo work', 'linux', 'mainline/master', 'Add two files.\n')
    async with app.run_test(size=(180, 40)) as pilot:
        screen = await open_forge(pilot)
        assert 'Goal:' in screen.content_text() and 'Add two files.' in screen.content_text()
        assert service.rounds(forge) == []


async def test_a_plan_is_accepted_with_A(demo_root, tmp_path, identity):
    app, service = make(demo_root, tmp_path)
    forge = service.create('Demo work', 'linux', 'mainline/master', '')
    service.post_plan(forge, 'Two patches.\n')
    async with app.run_test(size=(180, 40)) as pilot:
        screen = await open_forge(pilot)
        assert screen.round_labels() == ['plan r1']
        assert 'Two patches.' in screen.content_text()
        await pilot.press('A')
        await settle(pilot)
        assert service.accepted_plan(forge) is not None
        assert any('Example Maintainer' in label for label in screen.thread_labels())
        _series, thread = service.thread(forge, service.rounds(forge)[0])
        (ack,) = thread.cover.replies
        assert 'Acked-by: Example Maintainer <maintainer@example.org>' in ack.message.body


async def test_f_and_A_refuse_the_wrong_kind_of_round(demo_root, tmp_path, posted):
    app, service = make(demo_root, tmp_path)
    reviewed(service, posted)
    async with app.run_test(size=(180, 40)) as pilot:
        screen = await open_forge(pilot)
        await pilot.press('A')
        await settle(pilot)
        assert 'select a plan round' in screen.status_text()


async def test_findings_are_marked_like_sashikos(demo_root, tmp_path, posted):
    app, service = make(demo_root, tmp_path)
    forge = reviewed(service, posted)
    newest = service.rounds(forge)[-1]
    service.add_review(
        forge,
        newest,
        'review-prompts',
        {'patches': [{'patch': 1, 'findings': [{'severity': 'high', 'problem': 'f1 leaks'}]}]},
    )
    async with app.run_test(size=(180, 40)) as pilot:
        screen = await open_forge(pilot)
        assert screen.review() is not None and screen.review().high == 1
        labels = screen.thread_labels()
        assert any('add f1' in label and '1!' in label for label in labels)
        screen.select_message(next(n for n, x in enumerate(labels) if 'add f1' in x))
        await settle(pilot)
        assert 'f1 leaks' in screen.content_text()


async def test_h_harvests_a_cached_posting_and_u_starts_from_one(demo_root, tmp_path):
    app, service = make(demo_root, tmp_path)
    async with app.run_test(size=(180, 40)) as pilot:
        screen = await open_tab(pilot)
        await pilot.press('u')
        await settle(pilot)
        assert isinstance(app.screen, Pick)
        await pilot.press('enter')  # The first cached series.
        await settle(pilot)
        assert isinstance(app.screen, Ask)  # Its base.
        await pilot.press(*'mainline/master', 'enter')
        await settle(pilot)
        (forge,) = service.store.list()[0]
        (rnd,) = service.rounds(forge)
        assert rnd.kind == 'upstream' and forge.origin
        assert screen.forge_labels()[0].endswith(rnd.label)
        await pilot.press('enter')
        await settle(pilot)
        await pilot.press('h')
        await settle(pilot)
        assert isinstance(app.screen, Pick)
        await pilot.press('escape')
        await settle(pilot)
        assert service.rounds(forge) == [rnd]


def make_live(demo_root, tmp_path):
    """With agent sessions (fake runner and trees) and the Workbench tab's service."""
    from sessiondata import FakeSessionRunner, FakeWorktrees
    from test_workbench_screen import FakeService

    from b4_maintainer.sessions.hub import SessionHub
    from b4_maintainer.sessions.store import SessionStore

    sessions = SessionStore(tmp_path / 'var' / 'sessions')
    runner = FakeSessionRunner(sessions)
    hub = SessionHub(sessions, runner, FakeWorktrees(), sandbox=lambda: True)
    benches = WorkbenchStore(tmp_path / 'state' / 'workbenches')
    service = ForgeService(ForgeStore(tmp_path / 'state' / 'forges'), benches, tmp_path / 'var')
    app = MaintainerApp(
        FixtureSource(demo_root).load,
        sessions=hub,
        workbenches=FakeService(tmp_path, benches, hub.records, sessions.directory),
        forges=service,
        app_pane='%app',
    )
    return app, service, runner


async def test_a_launch_key_opens_the_dialog_with_only_its_task(demo_root, tmp_path):
    from b4_maintainer.launchdialog import LaunchDialog

    app, service, runner = make_live(demo_root, tmp_path)
    forge = service.create('Demo work', 'linux', 'HEAD', 'Add two files.\n')
    async with app.run_test(size=(220, 50)) as pilot:
        await open_forge(pilot)
        await pilot.press('I')
        await settle(pilot)
        dialog = app.screen
        assert isinstance(dialog, LaunchDialog)
        assert [p.id for p in dialog.profiles] == ['forge-implement']
        assert dialog.selected('task') == 'implement'  # Preselected: one key and confirm.
        # The forge's context is its folder: no series threads, Sashiko, or drafts.
        assert dialog.items['series'] == ()
        assert runner.launches == []  # Nothing started yet.
        context = service.space(forge) / 'context' / 'forge'
        assert (context / 'prompt.md').read_text() == 'Add two files.\n'
        assert forge.next_branch in (context / 'README.md').read_text()
        await pilot.press('escape')
        await settle(pilot)
        assert runner.launches == []


async def test_the_review_key_asks_which_review(demo_root, tmp_path):
    from b4_maintainer.launchdialog import LaunchDialog

    app, service, runner = make_live(demo_root, tmp_path)
    service.create('Demo work', 'linux', 'HEAD', '')
    async with app.run_test(size=(220, 50)) as pilot:
        screen = await open_forge(pilot)
        await pilot.press('R')
        await settle(pilot)
        assert isinstance(app.screen, Pick)
        assert [k for k, _label in app.screen.choices] == [
            'forge-review-prompts',
            'forge-kernel-style',
            'forge-sashiko',
        ]
        await pilot.press('down', 'down', 'enter')  # Sashiko: not built here.
        await settle(pilot)
        assert app.screen is screen and 'Sashiko: needs a build' in screen.status_text()
        await pilot.press('R')
        await settle(pilot)
        await pilot.press('down', 'enter')  # kernel-style.
        await settle(pilot)
        assert isinstance(app.screen, LaunchDialog)
        assert [p.id for p in app.screen.profiles] == ['forge-kernel-style']
        await pilot.press('ctrl+s')
        await settle(pilot)
        [(sid, request)] = runner.launches
        assert 'kernel-style' in request.prompt and 'out/forge/review.json' in request.prompt
        assert 'forge/ in the workbench context' in request.system_prompt


async def test_f1_dialogs_elsewhere_never_offer_forge_profiles(demo_root, tmp_path):
    from b4_maintainer.launchdialog import LaunchDialog

    app, _service, _runner = make_live(demo_root, tmp_path)
    async with app.run_test(size=(220, 50)) as pilot:
        await settle(pilot)
        await pilot.press('f1')
        await settle(pilot)
        if isinstance(app.screen, Pick):  # Which workbench: a new one.
            await pilot.press('enter')
            await settle(pilot)
        assert isinstance(app.screen, LaunchDialog)
        assert not any(p.id.startswith('forge-') for p in app.screen.profiles)


async def test_only_the_forge_keys_show_on_its_tab(demo_root, tmp_path):
    app, _service = make(demo_root, tmp_path)
    async with app.run_test(size=(180, 40)) as pilot:
        screen = await open_tab(pilot)
        shown = {
            b.binding.action
            for b in screen.active_bindings.values()
            if b.binding.show and b.enabled
        }
        assert shown == {'back', 'new', 'from_upstream', 'quit'}


async def test_t_shows_the_timeline(demo_root, tmp_path, posted):
    app, service = make(demo_root, tmp_path)
    reviewed(service, posted)
    async with app.run_test(size=(180, 40)) as pilot:
        screen = await open_forge(pilot)
        await pilot.press('t')
        await settle(pilot)
        text = screen.content_text()
        assert text.startswith('Demo work · linux on mainline/master · iterate')
        assert 'v1 r1' in text and 'replies: maintainer 1' in text and 'Now: v1 r2' in text


async def test_o_opens_the_series_at_the_selected_patch(demo_root, tmp_path, posted):
    app, service = make(demo_root, tmp_path)
    forge = reviewed(service, posted)
    looked, opened = [], []

    def look(f, rnd, part):
        looked.append((f.id, rnd.label, part))
        return tmp_path / 'look'

    service.look = look
    app.open_window = lambda path, name: opened.append((path, name)) or 'opened'
    async with app.run_test(size=(180, 40)) as pilot:
        screen = await open_forge(pilot)
        await pilot.press('o')  # The cover letter: not a patch.
        await settle(pilot)
        assert 'select a patch' in screen.status_text() and looked == []
        labels = screen.thread_labels()
        screen.select_message(next(n for n, x in enumerate(labels) if 'add f2' in x))
        await settle(pilot)
        await pilot.press('o')
        await settle(pilot)
        assert looked == [(forge.id, 'v1 r2', 2)]
        assert opened == [(tmp_path / 'look', f'{forge.id} v1-r2 2')]
        assert screen.status_text() == 'opened'


async def test_harvest_offers_older_versions_too(demo_root, tmp_path):
    app, service = make(demo_root, tmp_path)
    service.create('Demo work', 'linux', 'mainline/master', '')
    async with app.run_test(size=(180, 40)) as pilot:
        screen = await open_forge(pilot)
        import dataclasses

        items = list(app.snapshot.items)
        at = next(n for n, i in enumerate(items) if i.project == 'linux')
        earlier = dataclasses.replace(items[at].series, revision=items[at].series.revision - 1)
        items[at] = dataclasses.replace(items[at], older=(earlier,))
        app.snapshot = dataclasses.replace(app.snapshot, items=tuple(items))
        newest = [items[at]]
        await pilot.press('h')
        await settle(pilot)
        labels = [label for _key, label in app.screen.choices]
        series = newest[0].series
        older = newest[0].older[-1]
        assert f'{series.subject} v{series.revision} ' in ' '.join(labels)
        assert any(label.startswith(f'{older.subject} v{older.revision} ') for label in labels)
        await pilot.press('escape')
        await settle(pilot)
        assert app.screen is screen


async def test_F_on_lkml_starts_a_forge_from_the_series_or_harvests_it(demo_root, tmp_path):
    app, service = make(demo_root, tmp_path)
    existing = service.create('Demo work', 'linux', 'mainline/master', '')
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        item = app.snapshot.items[0]
        await pilot.press('F')
        await settle(pilot)
        assert isinstance(app.screen, Pick)
        assert [k for k, _label in app.screen.choices] == ['', existing.id]
        await pilot.press('enter')  # A new forge from it.
        await settle(pilot)
        assert isinstance(app.screen, Ask)
        await pilot.press(*'mainline/master', 'enter')
        await settle(pilot)
        made = next(f for f in service.store.list()[0] if f.id != existing.id)
        assert made.label == item.series.subject and made.origin == item.series.message_id
        (rnd,) = service.rounds(made)
        assert rnd.label == f'v{item.series.revision} upstream'
        assert 'F7' in app.status_text()  # Stays on LKML; says where it is.
        await pilot.press('F')
        await settle(pilot)
        await pilot.press('down', 'enter')  # Into the existing forge (the first after New).
        await settle(pilot)
        assert [r.label for r in service.rounds(existing)] == [f'v{item.series.revision} upstream']


async def test_C_opens_a_companion_on_the_round_beside_the_app(demo_root, tmp_path, posted):
    from b4_maintainer.launchdialog import LaunchDialog

    app, service, runner = make_live(demo_root, tmp_path)
    forge = reviewed(service, posted)
    async with app.run_test(size=(220, 50)) as pilot:
        screen = await open_forge(pilot)
        screen.select_round('v1 r1')
        await settle(pilot)
        labels = screen.thread_labels()
        screen.select_message(next(n for n, x in enumerate(labels) if 'Example Maintainer' in x))
        await settle(pilot)
        msgid = screen._selected_msgid()
        await pilot.press('C')
        await settle(pilot)
        assert isinstance(app.screen, LaunchDialog)
        assert [p.id for p in app.screen.profiles] == ['forge-companion']
        assert app.screen.selected('task') == 'companion'
        focus = (service.space(forge) / 'context' / 'forge' / 'focus.md').read_text()
        assert 'Round: v1 r1 (rounds/v1-r1.mbx)' in focus and f'<{msgid}>' in focus
        assert (service.space(forge) / 'context' / 'forge' / 'rounds' / 'v1-r1.mbx').is_file()
        await pilot.press('ctrl+s')
        await settle(pilot)
        [(sid, request)] = runner.launches
        assert request.prompt.startswith("Be the maintainer's companion")
        assert runner.beside == sid  # Beside the forge, at once.


async def test_sashiko_runs_as_a_job_with_the_model_picked(demo_root, tmp_path, posted):
    app, service, runner = make_live(demo_root, tmp_path)
    forge = reviewed(service, posted)
    binary = tmp_path / 'sashiko'
    binary.write_text('')
    service.sashiko_binary = binary
    ran = []

    def review(f, rnd, model, thinking='', run=None):
        ran.append((f.id, rnd.label, model))
        return f'Sashiko on {rnd.label} ({model}): 1 new finding (high 1); 10 in, 1 out'

    service.sashiko = review
    async with app.run_test(size=(220, 50)) as pilot:
        screen = await open_forge(pilot)
        await pilot.press('R')
        await settle(pilot)
        await pilot.press('down', 'down', 'enter')  # Sashiko.
        await settle(pilot)
        assert isinstance(app.screen, Pick)  # Which model: pi's list.
        keys = [k for k, _label in app.screen.choices]
        assert 'dry-run' not in keys and keys[0] == 'default'
        await pilot.press('enter')
        await settle(pilot)
        assert ran == [(forge.id, 'v1 r2', '')]
        assert screen.status_text().startswith('Sashiko on v1 r2')
        assert runner.launches == []  # A job, not a session.


async def test_a_forge_session_settling_is_collected_by_itself(demo_root, tmp_path):
    from sessiondata import make_record

    app, service, runner = make_live(demo_root, tmp_path)
    forge = service.create('Demo work', 'linux', 'HEAD', '')
    record = make_record(
        's9', scope='workbench', profile_id='forge-plan', workbench=forge.workbench
    )
    runner.add(record)
    async with app.run_test(size=(220, 50)) as pilot:
        screen = await open_forge(pilot)
        runner.emit('s9', 'working')
        app.poll_sessions()
        await settle(pilot)
        assert service.rounds(forge) == []
        out = runner.store.directory('s9') / 'work' / 'out' / 'forge'
        out.mkdir(parents=True)
        (out / 'design.md').write_text('One patch.\n')
        runner.emit('s9', 'waiting')  # Its turn is over.
        app.poll_sessions()
        await settle(pilot)
        assert [r.label for r in service.rounds(forge)] == ['plan r1']
        assert screen.round_labels() == ['plan r1']  # The open forge shows it.
        assert 'the design, as plan r1' in screen.status_text()
        # Asked again (the session rewrites its design), it is collected again.
        runner.emit('s9', 'working')
        app.poll_sessions()
        await settle(pilot)
        (out / 'design.md').write_text('Two patches.\n')
        runner.emit('s9', 'waiting')
        app.poll_sessions()
        await settle(pilot)
        assert screen.round_labels() == ['plan r1', 'plan r2']


async def test_opening_a_forge_collects_what_is_waiting(demo_root, tmp_path):
    from sessiondata import make_record

    app, service, runner = make_live(demo_root, tmp_path)
    forge = service.create('Demo work', 'linux', 'HEAD', '')
    runner.add(
        make_record('s8', scope='workbench', profile_id='forge-plan', workbench=forge.workbench)
    )
    out = runner.store.directory('s8') / 'work' / 'out' / 'forge'
    out.mkdir(parents=True)
    (out / 'design.md').write_text('One patch.\n')
    async with app.run_test(size=(220, 50)) as pilot:
        screen = await open_forge(pilot)
        assert screen.round_labels() == ['plan r1']


async def test_a_review_runs_in_the_background_and_ends_once_collected(demo_root, tmp_path, posted):
    app, service, runner = make_live(demo_root, tmp_path)
    reviewed(service, posted)
    async with app.run_test(size=(220, 50)) as pilot:
        screen = await open_forge(pilot)
        screen.select_round('v1 r1')
        await settle(pilot)
        await pilot.press('R')
        await settle(pilot)
        await pilot.press('down', 'enter')  # kernel-style
        await settle(pilot)
        await pilot.press('ctrl+s')
        await settle(pilot)
        [(sid, _request)] = runner.launches
        assert runner.beside is None  # Not beside the app: it works on its own.
        assert 'background' in screen.status_text()
        runner.emit(sid, 'working')
        app.poll_sessions()
        await settle(pilot)
        out = runner.store.directory(sid) / 'work' / 'out' / 'forge'
        (out / 'replies').mkdir(parents=True)
        patch = screen._selected_msgid()
        (out / 'replies' / '1.txt').write_text(f'In-Reply-To: <{patch}>\n\n> x\n\nSuggest: y\n')
        runner.emit(sid, 'waiting')  # Its turn is over: collected, then ended.
        app.poll_sessions()
        await settle(pilot)
        assert runner.ends == [sid]
        assert 'a reply on v1 r1' in screen.status_text()


async def test_a_background_review_without_results_is_left_open(demo_root, tmp_path):
    from sessiondata import make_record

    app, service, runner = make_live(demo_root, tmp_path)
    forge = service.create('Demo work', 'linux', 'HEAD', '')
    record = make_record(
        's7', scope='workbench', profile_id='forge-kernel-style', workbench=forge.workbench
    )
    runner.add(record)
    async with app.run_test(size=(220, 50)) as pilot:
        screen = await open_forge(pilot)
        runner.emit('s7', 'working')
        app.poll_sessions()
        await settle(pilot)
        runner.emit('s7', 'waiting')  # Asked something, or failed: nothing to take.
        app.poll_sessions()
        await settle(pilot)
        assert runner.ends == []
        assert 's7' in screen.status_text() and 'left open' in screen.status_text()


async def test_there_is_no_collect_key(demo_root, tmp_path):
    app, service, runner = make_live(demo_root, tmp_path)
    service.create('Demo work', 'linux', 'HEAD', '')
    async with app.run_test(size=(220, 50)) as pilot:
        await open_forge(pilot)
        assert 'c' not in app.screen.active_bindings


async def test_long_quotes_in_a_reply_fold_and_open_with_z_or_a_click(demo_root, tmp_path, posted):
    app, service = make(demo_root, tmp_path)
    forge = service.create('Demo work', 'linux', 'mainline/master', 'Add two files.\n')
    first = service.post_round(forge, posted[0])
    _series, thread = service.thread(forge, first)
    quote = ''.join(f'> quoted line {n}\n' for n in range(1, 11))
    service.post_reply(forge, first, thread.patches[0].message.msgid, ME, quote + '\nWhy f1?\n')
    async with app.run_test(size=(180, 40)) as pilot:
        screen = await open_forge(pilot)
        labels = screen.thread_labels()
        screen.select_message(next(n for n, x in enumerate(labels) if 'Example Maintainer' in x))
        await settle(pilot)
        assert '> [2 more quoted lines]' in screen.content_text()
        shown = [b.binding.action for b in screen.active_bindings.values() if b.enabled]
        assert 'toggle_quotes' in shown
        await pilot.press('z')
        await pilot.pause()
        assert '> quoted line 1\n' in screen.content_text() + '\n'
        assert '> [hide 2 quoted lines]' in screen.content_text()
        await pilot.press('z')
        await pilot.pause()
        assert '> [2 more quoted lines]' in screen.content_text()

        # A click on the marker opens that quote.
        content = screen.query_one('#forge-content', TextPane)
        marker = content.lines.index('> [2 more quoted lines]')
        await pilot.click(content, offset=(2, marker - content.scroll_offset.y))
        await pilot.pause()
        assert '> quoted line 1\n' in screen.content_text() + '\n'

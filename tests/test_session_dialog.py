"""Contract: F1 opens a launch dialog rendered from profiles; only confirming launches."""

import json

import pytest
from sessiondata import FakeSessionRunner, FakeWorktrees

from b4_maintainer.adapters.fixture import FixtureSource
from b4_maintainer.app import MaintainerApp, Notice, SeriesList
from b4_maintainer.launchdialog import LaunchDialog
from b4_maintainer.sessions.hub import SessionHub
from b4_maintainer.sessions.store import SessionStore

LISTING = 'provider model thinking\nalpha alpha-large yes\nalpha alpha-small no\n'


def catalog():
    return LISTING


@pytest.fixture
def store(tmp_path):
    return SessionStore(tmp_path / 'var' / 'sessions')


@pytest.fixture
def runner(store):
    return FakeSessionRunner(store)


def make_app(demo_root, store, runner, worktrees=None, **kwargs):
    hub = SessionHub(store, runner, worktrees or FakeWorktrees(), catalog=catalog, **kwargs)
    return MaintainerApp(FixtureSource(demo_root).load, sessions=hub)


async def settle(pilot):
    for _ in range(2):
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()


async def act(pilot, method, *args):
    """Change a dialog widget, then let its messages run."""
    method(*args)
    for _ in range(3):
        await pilot.pause()


async def open_dialog(pilot):
    await settle(pilot)
    await pilot.press('f1')
    await settle(pilot)
    dialog = pilot.app.screen
    assert isinstance(dialog, LaunchDialog)
    return dialog


async def test_f1_opens_a_dialog_for_the_selected_series(demo_root, store, runner):
    app = make_app(demo_root, store, runner)
    async with app.run_test(size=(180, 50)) as pilot:
        await settle(pilot)
        item = app.query_one(SeriesList).highlighted_item
        dialog = await open_dialog(pilot)
        assert item.series.subject in dialog.title_text()
        labels = dialog.labels()
        for label in (
            'Goal',
            'Help me review',
            'Artifacts',
            'Inline email draft',
            'Testing apparatus',
        ):
            assert label in labels, label
        assert "pi's default model" in labels and 'dry-run (scripted, no model)' in labels
        assert dialog.selected('model') == 'default'


async def test_escape_cancels_without_launching(demo_root, store, runner):
    app = make_app(demo_root, store, runner)
    async with app.run_test(size=(180, 50)) as pilot:
        dialog = await open_dialog(pilot)
        await act(pilot, dialog.pick, 'goal', 'review')
        await pilot.press('escape')
        await settle(pilot)
        assert not isinstance(app.screen, LaunchDialog)
        assert app.focused is app.query_one(SeriesList)
    assert runner.launches == []


async def test_missing_answers_block_the_launch(demo_root, store, runner):
    app = make_app(demo_root, store, runner)
    async with app.run_test(size=(180, 50)) as pilot:
        dialog = await open_dialog(pilot)
        assert 'Goal is required' in dialog.summary_text()
        await pilot.press('ctrl+s')
        await settle(pilot)
        assert isinstance(app.screen, LaunchDialog)
    assert runner.launches == []


async def test_confirm_launches_once_with_the_composed_request(demo_root, store, runner):
    app = make_app(demo_root, store, runner)
    async with app.run_test(size=(180, 50)) as pilot:
        dialog = await open_dialog(pilot)
        item = app.query_one(SeriesList).highlighted_item
        await act(pilot, dialog.pick, 'goal', 'review')
        await act(pilot, dialog.pick, 'artifacts', 'draft')
        assert 'Help me review this series' in dialog.summary_text()
        await pilot.press('ctrl+s', 'ctrl+s')
        await settle(pilot)
        assert not isinstance(app.screen, LaunchDialog)
        [(sid, request)] = runner.launches
        assert request.prompt.startswith('Help me review this series')
        assert 'inline review reply' in request.prompt
        record = store.record(sid)
        assert (record.series, record.subject) == (item.identity, item.series.subject)
        assert record.choices == {'goal': ['review'], 'artifacts': ['draft']}
        assert (record.harness, record.preset, record.model, record.mode, record.permissions) == (
            'pi',
            'default',
            '',
            'lightweight',
            'read-only',
        )
        assert sid in app.status_text()
        assert item.series.subject in app.drawer_text()


async def test_choices_needing_a_tree_switch_the_mode_until_changed_back(demo_root, store, runner):
    app = make_app(demo_root, store, runner)
    async with app.run_test(size=(180, 50)) as pilot:
        dialog = await open_dialog(pilot)
        await act(pilot, dialog.pick, 'goal', 'reproduce')
        assert dialog.selected('mode') == 'worktree'
        assert 'Worktree suggested' in dialog.summary_text()
        await act(pilot, dialog.pick, 'mode', 'lightweight')
        await act(pilot, dialog.pick, 'artifacts', 'selftest')
        assert dialog.selected('mode') == 'lightweight'


async def test_goals_combine_with_custom_text(demo_root, store, runner):
    app = make_app(demo_root, store, runner)
    async with app.run_test(size=(180, 50)) as pilot:
        dialog = await open_dialog(pilot)
        await act(pilot, dialog.pick, 'goal', 'review')
        await act(pilot, dialog.pick, 'goal', 'bugs')
        await act(pilot, dialog.write, 'goal', 'check the locking in foo_free()')
        assert 'Goal: check the locking in foo_free()' in dialog.summary_text()
        await pilot.press('ctrl+s')
        await settle(pilot)
        [(sid, request)] = runner.launches
        choices = store.record(sid).choices
        assert choices['goal'] == ['review', 'bugs']
        assert choices['goal:text'] == 'check the locking in foo_free()'
        assert request.prompt.index('Help me review') < request.prompt.index('Analyze')
        assert request.prompt.index('Analyze') < request.prompt.index('Goal: check the locking')


async def test_choices_select_default_skills_under_advanced(demo_root, store, runner):
    app = make_app(demo_root, store, runner)
    async with app.run_test(size=(180, 50)) as pilot:
        dialog = await open_dialog(pilot)
        assert not dialog.advanced_open()
        assert dialog.ticked('skills') == []
        await act(pilot, dialog.pick, 'goal', 'review')
        await act(pilot, dialog.pick, 'apparatus', 'vng')
        assert dialog.ticked('skills') == ['vng']
        assert 'Skills:   vng' in dialog.summary_text()
        await act(pilot, dialog.pick, 'mode', 'lightweight')  # Worktrees are not built yet.
        await pilot.press('ctrl+s')
        await settle(pilot)
        [(sid, _request)] = runner.launches
        assert store.record(sid).skills == ('vng',)


async def test_skills_the_maintainer_unticks_stay_off(demo_root, store, runner):
    app = make_app(demo_root, store, runner)
    async with app.run_test(size=(180, 50)) as pilot:
        dialog = await open_dialog(pilot)
        await act(pilot, dialog.pick, 'goal', 'review')
        await act(pilot, dialog.pick, 'apparatus', 'vng')
        await act(pilot, dialog.pick, 'skills', 'vng')  # Untick.
        await act(pilot, dialog.pick, 'artifacts', 'selftest')
        assert dialog.ticked('skills') == []
        assert 'Skills:' not in dialog.summary_text()
        await act(pilot, dialog.pick, 'skills', 'vng')  # Tick again: back to the default.
        await act(pilot, dialog.pick, 'apparatus', 'vng')  # Untick the apparatus.
        assert dialog.ticked('skills') == []


async def test_worktree_sessions_are_refused_without_a_sandbox(demo_root, store, runner):
    app = make_app(demo_root, store, runner, sandbox=lambda: False)
    async with app.run_test(size=(180, 50)) as pilot:
        dialog = await open_dialog(pilot)
        assert 'Permissions' not in dialog.labels()  # The mode decides.
        await act(pilot, dialog.pick, 'goal', 'reproduce')
        assert dialog.selected('mode') == 'worktree'
        assert 'worktree sessions unavailable' in dialog.summary_text()
        assert 'sandbox' in dialog.summary_text()
        assert not dialog.can_launch()


async def test_worktree_sessions_are_refused_without_a_checkout(demo_root, store, runner):
    trees = FakeWorktrees()
    trees.why = 'project linux has no checkout'
    app = make_app(demo_root, store, runner, trees, sandbox=lambda: True)
    async with app.run_test(size=(180, 50)) as pilot:
        dialog = await open_dialog(pilot)
        await act(pilot, dialog.pick, 'goal', 'reproduce')
        assert 'has no checkout' in dialog.summary_text() and not dialog.can_launch()


async def test_a_worktree_session_applies_the_series_on_the_chosen_base(demo_root, store, runner):
    trees = FakeWorktrees()
    app = make_app(demo_root, store, runner, trees, sandbox=lambda: True)
    async with app.run_test(size=(180, 50)) as pilot:
        dialog = await open_dialog(pilot)
        assert not dialog.base_shown()
        await act(pilot, dialog.pick, 'goal', 'reproduce')
        assert dialog.base_shown() and 'the base b4 chooses' in dialog.summary_text()
        await act(pilot, dialog.write, 'base', 'v6.18')
        assert 'applied on v6.18' in dialog.summary_text()
        await pilot.press('ctrl+s')
        await settle(pilot)
        [(sid, _request)] = runner.launches
        assert [(p[0], p[2]) for p in trees.prepared] == [(sid, 'v6.18')]
        record = store.record(sid)
        assert record.mode == 'worktree' and record.permissions == 'execute'
        assert record.worktree is not None


async def test_a_series_that_does_not_apply_shows_b4s_output(demo_root, store, runner):
    trees = FakeWorktrees()
    trees.fail = ('v3 does not apply at HEAD', 'error: patch failed: mm/foo.c:3')
    app = make_app(demo_root, store, runner, trees, sandbox=lambda: True)
    async with app.run_test(size=(180, 50)) as pilot:
        dialog = await open_dialog(pilot)
        await act(pilot, dialog.pick, 'goal', 'reproduce')
        await pilot.press('ctrl+s')
        await settle(pilot)
        assert runner.launches == []
        assert isinstance(app.screen, Notice)
        text = app.screen.prompt
        assert 'does not apply' in text and 'patch failed: mm/foo.c:3' in text
        assert 'lightweight' in text


async def test_workspace_sessions_cannot_use_a_worktree(demo_root, store, runner):
    app = make_app(demo_root, store, runner, sandbox=lambda: True)
    async with app.run_test(size=(180, 50)) as pilot:
        dialog = await open_dialog(pilot)
        await act(pilot, dialog.pick, 'scope', 'workspace')
        await act(pilot, dialog.pick, 'mode', 'worktree')
        assert 'no series to apply' in dialog.summary_text() and not dialog.can_launch()


async def test_a_workspace_session_uses_the_workspace_profile(demo_root, store, runner):
    app = make_app(demo_root, store, runner)
    async with app.run_test(size=(180, 50)) as pilot:
        dialog = await open_dialog(pilot)
        await act(pilot, dialog.pick, 'scope', 'workspace')
        assert 'Question' in dialog.labels() and 'Goal' not in dialog.labels()
        await act(pilot, dialog.write, 'question', 'Which tracked series are waiting on me?')
        await pilot.press('ctrl+s')
        await settle(pilot)
        [(sid, request)] = runner.launches
        record = store.record(sid)
        assert (record.scope, record.series, record.profile_id) == ('workspace', '', 'workspace')
        assert 'waiting on me' in request.prompt


async def test_f1_in_the_reader_targets_the_open_series(demo_root, store, runner):
    app = make_app(demo_root, store, runner)
    async with app.run_test(size=(180, 50)) as pilot:
        await settle(pilot)
        await pilot.press('down', 'enter')
        await settle(pilot)
        reading = app.reading
        dialog = await open_dialog(pilot)
        assert reading.series.subject in dialog.title_text()
        await pilot.press('escape')
        await settle(pilot)
        assert app.reading is reading


async def test_without_a_runner_f1_explains_instead(demo_root, store, runner):
    runner.blocker_reason = 'phase 1 sessions need tmux'
    app = make_app(demo_root, store, runner)
    async with app.run_test(size=(180, 50)) as pilot:
        await settle(pilot)
        await pilot.press('f1')
        await settle(pilot)
        assert isinstance(app.screen, Notice)
        assert 'need tmux' in app.screen.prompt
    assert runner.launches == []


async def test_maintainer_profiles_replace_built_ins(demo_root, store, runner, tmp_path):
    mine = tmp_path / 'profiles'
    mine.mkdir()
    (mine / 'series.toml').write_text(
        'id = "series"\nversion = 2\nlabel = "Mine"\n'
        '[[fields]]\nid = "ask"\nlabel = "My question"\nkind = "text"\n'
    )
    app = make_app(demo_root, store, runner, profiles_dir=mine)
    async with app.run_test(size=(180, 50)) as pilot:
        dialog = await open_dialog(pilot)
        assert 'My question' in dialog.labels() and 'Goal' not in dialog.labels()


async def test_tab_moves_between_sections_and_shift_tab_back(demo_root, store, runner):
    app = make_app(demo_root, store, runner)
    async with app.run_test(size=(180, 50)) as pilot:
        dialog = await open_dialog(pilot)
        await act(pilot, dialog.pick, 'goal', 'review')  # Launch is skipped while disabled.
        sections = [
            'goal',
            'goal:text',
            'artifacts',
            'artifacts:text',
            'apparatus',
            'mode',
            'model',
            'advanced',
            'launch',
            'cancel',
            'scope',
        ]
        seen = []
        for _ in sections:
            seen.append(dialog.focused_section())
            await pilot.press('tab')
        assert seen == sections
        assert dialog.focused_section() == 'goal'
        await pilot.press('shift+tab', 'shift+tab')
        assert dialog.focused_section() == 'cancel'


async def test_keys_pick_choices_within_a_section(demo_root, store, runner):
    app = make_app(demo_root, store, runner)
    async with app.run_test(size=(180, 50)) as pilot:
        dialog = await open_dialog(pilot)
        assert dialog.focused_section() == 'goal'
        await pilot.press('enter')  # The highlighted first goal.
        await pilot.press('tab', 'tab', 'space')  # Artifacts: the first one.
        await pilot.press('tab', 'tab', 'space')  # Apparatus: vng.
        await pilot.pause()
        assert dialog.ticked('goal') == ['review']
        assert dialog.ticked('artifacts') == ['quality']
        assert dialog.ticked('apparatus') == ['vng']
        assert dialog.ticked('skills') == ['vng']


def add_presets(demo_root, presets):
    path = demo_root / 'workspace.json'
    config = json.loads(path.read_text())
    config['models'] = presets
    path.write_text(json.dumps(config))


async def test_presets_are_listed_and_unknown_models_disabled(demo_root, store, runner):
    add_presets(
        demo_root,
        [
            {'id': 'deep', 'model': 'alpha/alpha-large', 'thinking': 'high'},
            {'id': 'gone', 'model': 'gamma/old'},
        ],
    )
    app = make_app(demo_root, store, runner)
    async with app.run_test(size=(180, 50)) as pilot:
        dialog = await open_dialog(pilot)
        assert 'deep (alpha/alpha-large)' in dialog.labels()
        assert dialog.option_disabled('model', 'gone')
        assert not dialog.option_disabled('model', 'deep')
        await act(pilot, dialog.pick, 'goal', 'review')
        await act(pilot, dialog.pick, 'model', 'deep')
        assert 'alpha/alpha-large' in dialog.summary_text()
        await pilot.press('ctrl+s')
        await settle(pilot)
        [(sid, _request)] = runner.launches
        record = store.record(sid)
        assert (record.preset, record.model, record.thinking) == (
            'deep',
            'alpha/alpha-large',
            'high',
        )


async def test_advanced_picks_any_catalog_model_and_thinking(demo_root, store, runner):
    app = make_app(demo_root, store, runner)
    async with app.run_test(size=(180, 50)) as pilot:
        dialog = await open_dialog(pilot)
        await act(pilot, dialog.pick, 'goal', 'review')
        await act(pilot, dialog.pick, 'other-model', 'alpha/alpha-small')
        await act(pilot, dialog.pick, 'thinking', 'low')
        assert 'alpha/alpha-small' in dialog.summary_text()
        await pilot.press('ctrl+s')
        await settle(pilot)
        [(sid, _request)] = runner.launches
        record = store.record(sid)
        assert (record.preset, record.model, record.thinking) == (
            'alpha/alpha-small',
            'alpha/alpha-small',
            'low',
        )


async def test_dry_run_is_always_offered(demo_root, store, runner):
    app = make_app(demo_root, store, runner)
    async with app.run_test(size=(180, 50)) as pilot:
        dialog = await open_dialog(pilot)
        await act(pilot, dialog.pick, 'goal', 'review')
        await act(pilot, dialog.pick, 'model', 'dry-run')
        await pilot.press('ctrl+s')
        await settle(pilot)
        [(sid, _request)] = runner.launches
        assert store.record(sid).model == 'b4m/dry-run'


def test_the_series_mbox_is_offered_only_when_its_mail_exists(demo_root, tmp_path):
    import dataclasses

    from b4_maintainer.adapters.fixture import FixtureSource
    from b4_maintainer.app import series_context

    item = FixtureSource(demo_root).load().items[0]
    assert [i.id for i in series_context(item)] == ['mbox']
    gone = dataclasses.replace(item.series, source=str(tmp_path / 'missing'))
    assert series_context(dataclasses.replace(item, series=gone)) == ()


async def test_pis_model_list_is_read_in_the_background_at_startup(demo_root, store, runner):
    import threading

    calls = []

    def slow_catalog():
        calls.append(threading.current_thread() is threading.main_thread())
        return LISTING

    hub = SessionHub(store, runner, FakeWorktrees(), catalog=slow_catalog)
    app = MaintainerApp(FixtureSource(demo_root).load, sessions=hub)
    async with app.run_test(size=(180, 50)) as pilot:
        await settle(pilot)
        assert calls == [False]  # Read once, off the UI thread, before any F1.
        await open_dialog(pilot)
        assert calls == [False]  # F1 used it.
    assert runner.launches == []

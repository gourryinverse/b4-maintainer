"""Contract: a launch is planned from a profile and the dialog's choices before anything runs."""

import datetime

import pytest
from sessiondata import PROFILE, FakeSessionRunner, FakeWorktrees, make_record

from b4_maintainer.sessions.hub import SessionError, SessionHub
from b4_maintainer.sessions.launch import (
    LaunchChoice,
    Situation,
    Target,
    new_session_id,
    plan,
)
from b4_maintainer.sessions.models import model_choices, parse_catalog
from b4_maintainer.sessions.policy import ContextItem, SessionLimits
from b4_maintainer.sessions.profiles import parse_profile
from b4_maintainer.sessions.skills import Skill
from b4_maintainer.sessions.store import SessionStore, valid_id

TARGET = Target('series', 'linux', 'change-id:20261020-mm-foo', 3, 'mm: fix foo leak')
ITEMS = (ContextItem('mbox', 'Series mbox and thread'),)
SKILLS = (
    Skill('vng', 'Boot kernels with virtme-ng.', None),
    Skill('kselftest', 'Run tests.', None),
)


CATALOG = parse_catalog('provider model thinking\nalpha alpha-large yes\n')
MODELS = model_choices(
    CATALOG,
    [
        {'id': 'deep', 'model': 'alpha/alpha-large', 'thinking': 'high'},
        {'id': 'old', 'model': 'x/y'},
    ],
)


def situation(**changes):
    base = dict(
        models=MODELS,
        limits=SessionLimits(),
        worktree_reason='no sandbox here',
        skills=SKILLS,
    )
    return Situation(**{**base, **changes})


@pytest.fixture
def profile():
    return parse_profile(PROFILE)


def choice(**changes):
    base = dict(profile='series-test', choices={'goal': 'review'}, model='deep')
    return LaunchChoice(**{**base, **changes})


def test_a_complete_choice_plans_a_launch(profile):
    p = plan(profile, choice(), situation(), ITEMS)
    assert p.ok and not p.blockers
    assert p.request.prompt == 'Help me review this series.'
    assert (p.mode, p.permissions, p.model.id, p.thinking) == (
        'lightweight',
        'read-only',
        'deep',
        'high',
    )
    assert [i.id for i in p.included] == ['mbox']


def test_missing_answers_and_unknown_models_block(profile):
    p = plan(profile, choice(choices={}), situation(), ITEMS)
    assert not p.ok and p.request is None
    assert p.blockers == ('Goal is required',)
    assert 'Choose a model' in plan(profile, choice(model='nope'), situation(), ITEMS).blockers


def test_unavailable_models_block_with_their_reason(profile):
    p = plan(profile, choice(model='old'), situation(), ITEMS)
    assert p.blockers == ("old: x/y is not in pi's model list",)


def test_the_thinking_level_follows_the_choice_then_the_preset(profile):
    assert plan(profile, choice(thinking='low'), situation(), ITEMS).thinking == 'low'
    assert plan(profile, choice(model='default'), situation(), ITEMS).thinking == ''
    p = plan(profile, choice(thinking='extreme'), situation(), ITEMS)
    assert any('thinking' in b for b in p.blockers)


def test_choices_needing_a_tree_suggest_a_worktree_unless_chosen(profile):
    p = plan(profile, choice(choices={'goal': 'reproduce'}), situation(), ITEMS)
    assert p.mode == 'worktree' and p.notes
    p = plan(profile, choice(choices={'goal': 'reproduce'}, mode='lightweight'), situation(), ITEMS)
    assert p.mode == 'lightweight' and p.ok


def test_the_mode_decides_the_permissions(profile):
    assert plan(profile, choice(), situation(), ITEMS).permissions == 'read-only'
    p = plan(profile, choice(mode='worktree'), situation(worktree_reason=''), ITEMS)
    assert p.ok and p.permissions == 'execute'  # Edit, build, and run in the sandbox.


def test_worktree_sessions_wait_for_what_they_need(profile):
    p = plan(profile, choice(mode='worktree'), situation(), ITEMS)
    assert p.blockers == ('worktree sessions unavailable: no sandbox here',)


def test_choices_select_their_skills_and_the_maintainer_adjusts_them(profile):
    picked = {'goal': 'reproduce', 'artifacts': ['selftest']}
    p = plan(profile, choice(choices=picked), situation(), ITEMS)
    assert p.skills == ('vng',)
    adjusted = choice(
        choices=picked, skills_on=frozenset({'kselftest'}), skills_off=frozenset({'vng'})
    )
    assert plan(profile, adjusted, situation(), ITEMS).skills == ('kselftest',)


def test_skills_not_installed_are_dropped_with_a_warning(profile):
    picked = {'goal': 'reproduce'}
    p = plan(profile, choice(choices=picked, mode='lightweight'), situation(skills=()), ITEMS)
    assert p.skills == () and p.ok
    assert any("skill 'vng' is not installed" in w for w in p.warnings)


def test_limits_block_and_warn(profile):
    p = plan(profile, choice(), situation(live=4), ITEMS)
    assert any('max_live' in b for b in p.blockers)
    tree = choice(mode='worktree')
    p = plan(profile, tree, situation(worktrees=5, worktree_bytes=0, worktree_reason=''), ITEMS)
    assert p.ok and any('worktree_warn' in w for w in p.warnings)


def test_session_ids_are_valid_and_distinct():
    now = datetime.datetime(2026, 10, 23, 12, 0, tzinfo=datetime.timezone.utc)
    ids = {new_session_id(now) for _ in range(20)}
    assert len(ids) == 20 and all(valid_id(i) and i.startswith('20261023-1200') for i in ids)


@pytest.fixture
def store(tmp_path):
    return SessionStore(tmp_path / 'var' / 'sessions')


def test_launch_records_the_session_then_starts_it_once(store, profile):
    runner = FakeSessionRunner(store)
    hub = SessionHub(store, runner, FakeWorktrees())
    record = hub.launch(TARGET, profile, choice(), situation(), ITEMS)
    [(sid, request)] = runner.launches
    assert sid == record.id and request.prompt == 'Help me review this series.'
    saved = store.record(record.id)
    assert saved == record
    assert (saved.series, saved.revision, saved.subject) == (TARGET.series, 3, TARGET.subject)
    assert (saved.profile_id, saved.profile_hash) == ('series-test', profile.hash)
    assert (saved.harness, saved.preset, saved.model, saved.thinking) == (
        'pi',
        'deep',
        'alpha/alpha-large',
        'high',
    )
    assert saved.native_id == saved.id  # pi's session id is ours.
    assert saved.choices == {'goal': 'review'} and saved.context == ('mbox',)
    assert saved.skills == ()
    assert saved.state == ''


def test_a_blocked_launch_records_nothing(store, profile):
    runner = FakeSessionRunner(store)
    hub = SessionHub(store, runner, FakeWorktrees())
    with pytest.raises(SessionError, match='Goal is required'):
        hub.launch(TARGET, profile, choice(choices={}), situation(), ITEMS)
    assert runner.launches == [] and store.poll(runner.alive) == []


def test_a_launch_that_fails_leaves_a_failed_session(store, profile):
    runner = FakeSessionRunner(store)
    runner.fail = 'window could not open'
    hub = SessionHub(store, runner, FakeWorktrees())
    with pytest.raises(SessionError, match='window could not open'):
        hub.launch(TARGET, profile, choice(), situation(), ITEMS)
    [view] = store.poll(runner.alive)
    assert view.state == 'failed'


def test_the_runner_blocker_is_reported(store):
    runner = FakeSessionRunner(store)
    runner.blocker_reason = 'phase 1 sessions need tmux'
    assert SessionHub(store, runner, FakeWorktrees()).blocker() == 'phase 1 sessions need tmux'


def test_a_worktree_launch_prepares_the_tree_before_the_agent_starts(store, profile):
    runner, trees = FakeSessionRunner(store), FakeWorktrees()
    hub = SessionHub(store, runner, trees)
    target = Target(**{**TARGET.__dict__, 'source': '/mail/thread'})
    tree_choice = choice(mode='worktree', base='v6.18')
    record = hub.launch(target, profile, tree_choice, situation(worktree_reason=''), ITEMS)
    assert trees.prepared == [(record.id, '/mail/thread', 'v6.18')]
    assert record.worktree is not None and record.worktree.branch == f'b4m/session/{record.id}'
    assert store.record(record.id) == record and record.permissions == 'execute'
    [(sid, _request)] = runner.launches
    assert sid == record.id


def test_a_tree_that_cannot_be_prepared_fails_the_session_with_b4s_output(store, profile):
    runner, trees = FakeSessionRunner(store), FakeWorktrees()
    trees.fail = ('v3 does not apply at HEAD', 'Patch failed at 0001 mm: fix foo')
    hub = SessionHub(store, runner, trees)
    with pytest.raises(SessionError, match='does not apply') as raised:
        hub.launch(TARGET, profile, choice(mode='worktree'), situation(worktree_reason=''), ITEMS)
    assert raised.value.detail == 'Patch failed at 0001 mm: fix foo'
    assert runner.launches == []
    [view] = store.poll(runner.alive)
    assert view.state == 'failed' and view.record.worktree is None


def test_workspace_sessions_have_no_series_to_apply(store, profile):
    runner, trees = FakeSessionRunner(store), FakeWorktrees()
    hub = SessionHub(store, runner, trees)
    with pytest.raises(SessionError, match='series'):
        hub.launch(
            Target('workspace'), profile, choice(mode='worktree'), situation(worktree_reason=''), ()
        )
    assert trees.prepared == [] and runner.launches == []


def test_a_session_being_prepared_shows_so_and_cannot_be_acted_on(store, profile):
    runner, trees = FakeSessionRunner(store), FakeWorktrees()
    hub = SessionHub(store, runner, trees)
    seen = {}

    def during(record):
        [view] = hub.overview().sessions
        seen['state'] = view.state
        for action in (hub.open, hub.stop, hub.end, lambda sid: hub.show(sid, '%1')):
            with pytest.raises(SessionError, match='being prepared'):
                action(record.id)

    trees.during = during
    record = hub.launch(
        TARGET, profile, choice(mode='worktree'), situation(worktree_reason=''), ITEMS
    )
    assert seen['state'] == 'preparing'
    assert runner.resumes == runner.stops == runner.ends == runner.switches == []
    assert store.record(record.id).state == '' and len(runner.launches) == 1


def test_a_preparation_cut_short_by_a_quit_shows_as_failed(store, profile):
    runner = FakeSessionRunner(store)
    runner.add(make_record('p', mode='worktree', state='preparing'), alive=False)
    hub = SessionHub(store, runner, FakeWorktrees())  # A later app: nothing is preparing.
    [view] = hub.overview().sessions
    assert view.state == 'failed'


def test_limits_count_a_session_still_being_prepared(store):
    from b4_maintainer.app import _situation
    from b4_maintainer.sessions.hub import Overview
    from b4_maintainer.sessions.store import SessionView
    from b4_maintainer.workspace import Workspace

    hub = SessionHub(store, FakeSessionRunner(store), FakeWorktrees(), sandbox=lambda: True)
    preparing = SessionView(make_record('p', mode='worktree', state='preparing'), 'preparing')
    found = _situation(hub, Workspace(), Overview((preparing,)), [], 'linux')
    assert (found.live, found.live_builds) == (1, 1)


def test_the_package_holds_the_ticked_available_sources(profile):
    items = (
        ContextItem('mbox', 'Series mbox and thread'),
        ContextItem('sashiko', 'Sashiko review', text='x'),
        ContextItem('other', 'Not here', available=False, reason='none'),
    )
    p = plan(profile, choice(), situation(), items)
    assert [i.id for i in p.included] == ['mbox', 'sashiko']
    p = plan(profile, choice(context_off=frozenset({'sashiko'})), situation(), items)
    assert [i.id for i in p.included] == ['mbox']


def test_a_launch_writes_its_package(store, profile):
    runner = FakeSessionRunner(store)
    hub = SessionHub(store, runner, FakeWorktrees())
    items = (ContextItem('sashiko', 'Sashiko review', text='# Sashiko review\n'),)
    record = hub.launch(TARGET, profile, choice(), situation(), items)
    context = store.directory(record.id) / 'work' / 'context'
    assert (context / 'sashiko.md').read_text() == '# Sashiko review\n'
    assert record.context == ('sashiko',)


def test_worktree_mode_suggests_the_profiles_worktree_skills(profile):
    import dataclasses

    skills = (*SKILLS, Skill('b4m-branches', 'Structure session work.', None))
    tree_profile = dataclasses.replace(profile, worktree_skills=('b4m-branches',))
    light = plan(tree_profile, choice(), situation(skills=skills), ITEMS)
    assert 'b4m-branches' not in light.skills
    tree = plan(
        tree_profile, choice(mode='worktree'), situation(skills=skills, worktree_reason=''), ITEMS
    )
    assert 'b4m-branches' in tree.skills
    off = choice(mode='worktree', skills_off=frozenset({'b4m-branches'}))
    assert (
        'b4m-branches'
        not in plan(tree_profile, off, situation(skills=skills, worktree_reason=''), ITEMS).skills
    )


def test_the_built_in_series_profile_gives_worktree_sessions_the_branches_skill():
    from b4_maintainer.sessions.profiles import BUILTIN_DIR, parse_profile
    from b4_maintainer.sessions.skills import BUILTIN_DIR as SKILLS_DIR
    from b4_maintainer.sessions.skills import load_skills

    series = parse_profile((BUILTIN_DIR / 'series.toml').read_text())
    assert series.worktree_skills == ('b4m-branches',)
    installed, problems = load_skills(SKILLS_DIR, None)
    assert problems == [] and 'b4m-branches' in {s.id for s in installed}

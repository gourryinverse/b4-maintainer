"""Contract: a launch is planned from a profile and the dialog's choices before anything runs."""

import datetime

import pytest
from sessiondata import PROFILE, FakeSessionRunner, FakeWorktrees

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
        execute_reason='no sandbox here',
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


def test_execute_needs_a_worktree_and_a_sandbox(profile):
    p = plan(profile, choice(permissions='execute'), situation(), ITEMS)
    assert 'execute needs a worktree session' in p.blockers
    p = plan(profile, choice(permissions='execute', mode='worktree'), situation(), ITEMS)
    assert p.blockers == ('execute unavailable: no sandbox here',)
    p = plan(
        profile, choice(permissions='execute', mode='worktree'), situation(execute_reason=''), ITEMS
    )
    assert p.ok


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
    p = plan(profile, choice(choices=picked), situation(skills=()), ITEMS)
    assert p.skills == () and p.ok
    assert any("skill 'vng' is not installed" in w for w in p.warnings)


def test_limits_block_and_warn(profile):
    p = plan(profile, choice(), situation(live=4), ITEMS)
    assert any('max_live' in b for b in p.blockers)
    tree = choice(mode='worktree')
    p = plan(profile, tree, situation(worktrees=5, worktree_bytes=0), ITEMS)
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

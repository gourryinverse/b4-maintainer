"""Contract: a series b4 cannot apply can be handed to an agent. From F1, the session is
launched again with its tree at the base where b4 stopped and the series left to the agent;
from `a` (apply to a base), an apply session works on a copy of the base. Either way the
agent gets b4's mbox, and the maintainer's branch changes only when they accept."""

import datetime

import pytest
from sessiondata import PROFILE, FakeSessionRunner, FakeWorktrees

from b4_maintainer.sessions.hub import SessionError, SessionHub
from b4_maintainer.sessions.launch import LaunchChoice, Situation, Target
from b4_maintainer.sessions.models import model_choices
from b4_maintainer.sessions.policy import ContextItem, SessionLimits
from b4_maintainer.sessions.profiles import parse_profile
from b4_maintainer.sessions.records import session_label
from b4_maintainer.sessions.store import SessionStore

TARGET = Target(
    'series', 'linux', 'change-id:20261020-mm-foo', 3, 'mm: fix foo leak', '/mail/foo.mbx'
)
ITEMS = (ContextItem('mbox', 'Series mbox and thread'),)
NOW = datetime.datetime(2026, 10, 27, 12, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture
def store(tmp_path):
    return SessionStore(tmp_path / 'var' / 'sessions')


def situation(**changes):
    base = dict(models=model_choices(None, []), limits=SessionLimits(), worktree_reason='')
    return Situation(**{**base, **changes})


def choice(**changes):
    base = dict(profile='series-test', choices={'goal': 'review'}, mode='worktree', model='dry-run')
    return LaunchChoice(**{**base, **changes})


def test_a_conflict_says_where_the_series_would_have_applied(store):
    runner, trees = FakeSessionRunner(store), FakeWorktrees()
    trees.fail = ('v3 does not apply at HEAD', 'Patch failed at 0001', 'b' * 40)
    hub = SessionHub(store, runner, trees, sandbox=lambda: True)
    with pytest.raises(SessionError) as raised:
        hub.launch(TARGET, parse_profile(PROFILE), choice(), situation(), ITEMS)
    assert raised.value.base == 'b' * 40 and raised.value.detail == 'Patch failed at 0001'


def test_handed_to_the_agent_the_tree_starts_at_that_base_without_the_series(store):
    runner, trees = FakeSessionRunner(store), FakeWorktrees()
    hub = SessionHub(store, runner, trees, sandbox=lambda: True)
    record = hub.launch(
        TARGET, parse_profile(PROFILE), choice(agent_apply='b' * 40), situation(), ITEMS
    )
    assert trees.unapplied == [(record.id, '/mail/foo.mbx', 'b' * 40)]
    assert trees.prepared == []  # b4 is not asked to apply it again.
    assert record.worktree is not None and record.worktree.agent_applies
    [(sid, request)] = runner.launches
    assert sid == record.id and request.prompt.startswith('Help me review')  # The task.


def test_an_apply_session_applies_the_series_on_a_copy_of_the_base(store):
    runner, trees = FakeSessionRunner(store), FakeWorktrees()
    hub = SessionHub(store, runner, trees, sandbox=lambda: True)
    record = hub.launch_apply(
        TARGET,
        'me/base',
        'c' * 40,
        b'From x\n',
        (1, 3),
        'CONFLICT (content): Merge conflict in mm/foo.c',
        situation(),
        now=NOW,
    )
    assert record.scope == 'apply' and record.series == TARGET.series
    assert record.choices == {'base': 'me/base', 'expected': 'c' * 40, 'patches': '1, 3'}
    assert session_label(record) == 'linux: apply mm: fix foo leak v3 to me/base'
    assert trees.with_mbox == [(record.id, b'From x\n', 'c' * 40)]
    assert record.worktree is not None and record.worktree.agent_applies
    [(sid, request)] = runner.launches
    task = request.prompt
    for text in ('me/base', 'patches 1, 3', 'Merge conflict in mm/foo.c', 'A in Branches'):
        assert text in task, text
    assert 'trailers' in request.system_prompt


def test_an_apply_session_waits_for_the_sandbox_and_the_limits(store):
    runner, trees = FakeSessionRunner(store), FakeWorktrees()
    hub = SessionHub(store, runner, trees, sandbox=lambda: True)
    args = (TARGET, 'me/base', 'c' * 40, b'', (1,), '')
    with pytest.raises(SessionError, match='sandbox'):
        hub.launch_apply(*args, situation(worktree_reason='no sandbox here'))
    with pytest.raises(SessionError, match='max_builds'):
        hub.launch_apply(*args, situation(live_builds=2))
    assert runner.launches == []

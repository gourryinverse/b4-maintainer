"""Contract: a failed base rebase prepares a rebase session: its task says what to do,
its record says which base it serves, and it launches only within the limits."""

import datetime

import pytest
from sessiondata import FakeSessionRunner, FakeWorktrees

from b4_maintainer.adapters.bases import BaseCommit, BaseStatus
from b4_maintainer.sessions.hub import SessionError, SessionHub
from b4_maintainer.sessions.launch import Situation
from b4_maintainer.sessions.models import model_choices
from b4_maintainer.sessions.policy import SessionLimits
from b4_maintainer.sessions.records import session_label
from b4_maintainer.sessions.store import SessionStore
from b4_maintainer.workspace import MaintainerBase

BASE = MaintainerBase('me/base', 'up/stable')
LANDED = BaseCommit('a' * 40, 'mm: landed already', 'One <one@example.org>', True)
KEPT = BaseCommit('b' * 40, 'mm: fix b', 'Two <two@example.org>')
STATUS = BaseStatus(BASE, True, 'c' * 40, 'd' * 40, 3, (LANDED, KEPT))
NOW = datetime.datetime(2026, 10, 26, 12, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture
def store(tmp_path):
    return SessionStore(tmp_path / 'var' / 'sessions')


def situation(**changes):
    base = dict(models=model_choices(None, []), limits=SessionLimits(), worktree_reason='')
    return Situation(**{**base, **changes})


def launch(hub, **changes):
    args = dict(
        project='linux',
        base=BASE,
        status=STATUS,
        failed=KEPT,
        detail='CONFLICT (content): Merge conflict in b.c',
        situation=situation(),
        now=NOW,
    )
    return hub.launch_rebase(**{**args, **changes})


def test_a_rebase_session_records_its_base_and_gets_its_task(store):
    runner, trees = FakeSessionRunner(store), FakeWorktrees()
    hub = SessionHub(store, runner, trees, sandbox=lambda: True)
    record = launch(hub)
    assert (record.scope, record.project, record.mode, record.permissions) == (
        'rebase',
        'linux',
        'worktree',
        'execute',
    )
    assert record.choices == {
        'base': 'me/base',
        'upstream': 'up/stable',
        'expected': 'c' * 40,
        'failed': 'b' * 40,
    }
    assert record.skills == ()  # Not the branches skill: a rebase rewrites the base's commits.
    assert record.worktree is not None and trees.rebases == [(record.id, BASE)]
    assert session_label(record) == 'linux: rebase me/base onto up/stable'
    [(sid, request)] = runner.launches
    assert sid == record.id
    task = request.prompt
    for text in (
        'me/base',
        'up/stable',
        f'b4m/session/{record.id}',
        'b4m/rebased',
        'mm: fix b',  # The commit that did not apply.
        'CONFLICT (content): Merge conflict in b.c',
        'mm: landed already',  # To drop.
        'out/artifacts.toml',
    ):
        assert text in task, text
    assert 'Signed-off-by' in request.system_prompt  # Trailers are kept, not stripped.
    assert store.record(record.id) == record


def test_a_rebase_session_waits_for_the_sandbox_and_the_limits(store):
    runner, trees = FakeSessionRunner(store), FakeWorktrees()
    hub = SessionHub(store, runner, trees, sandbox=lambda: True)
    with pytest.raises(SessionError, match='sandbox'):
        launch(hub, situation=situation(worktree_reason='no sandbox here'))
    with pytest.raises(SessionError, match='max_builds'):
        launch(hub, situation=situation(live_builds=2))
    assert runner.launches == [] and store.poll(runner.alive) == []


def test_a_rebase_tree_that_cannot_be_prepared_fails_the_session(store):
    runner, trees = FakeSessionRunner(store), FakeWorktrees()
    trees.fail = ('me/base does not exist', '')
    hub = SessionHub(store, runner, trees, sandbox=lambda: True)
    with pytest.raises(SessionError, match='does not exist'):
        launch(hub)
    [view] = store.poll(runner.alive)
    assert view.state == 'failed' and runner.launches == []

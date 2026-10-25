"""Contract: session trees are listed with their state, orphans included; removal is
intentional, says what it loses, and never touches a live session's tree."""

import pytest
from gitutil import git
from sessiondata import FakeSessionRunner, make_record

from b4_maintainer.sessions.hub import RecordWorktrees, SessionError, SessionHub
from b4_maintainer.sessions.records import Worktree
from b4_maintainer.sessions.store import SessionStore


def make_tree(path):
    """A small stand-in for a session tree: a repository with one series commit."""
    path.mkdir(parents=True)
    git('init', '-q', '-b', 'b4m/session/x', cwd=path)
    (path / 'a.c').write_text('a\n')
    git('add', 'a.c', cwd=path)
    git('commit', '-q', '-m', 'mm: series', cwd=path)
    return Worktree(str(path), 'b4m/session/x', 'base', git('rev-parse', 'HEAD', cwd=path))


@pytest.fixture
def setup(tmp_path, home):
    (home / '.gitconfig').write_text('[user]\n\tname = Example\n\temail = e@example.org\n')
    store = SessionStore(tmp_path / 'var' / 'sessions')
    runner = FakeSessionRunner(store)
    trees = tmp_path / 'var' / 'worktrees'
    worktrees = RecordWorktrees(trees, {'linux': str(tmp_path / 'linux')})
    hub = SessionHub(store, runner, worktrees, sandbox=lambda: True)
    return hub, runner, trees


def test_trees_are_listed_with_size_state_and_orphans(setup):
    hub, runner, trees = setup
    live = make_tree(trees / 'a')
    ended = make_tree(trees / 'b')
    (trees / 'b' / 'new.txt').write_text('agent output\n')
    make_tree(trees / 'orphan')
    runner.add(make_record('a', mode='worktree', worktree=live))
    runner.add(make_record('b', mode='worktree', worktree=ended), alive=False)
    hub.store.set_state('b', 'ended')
    infos = {w.path: w for w in hub.overview().worktrees}
    assert set(infos) == {str(trees / 'a'), str(trees / 'b'), str(trees / 'orphan')}
    a, b, orphan = infos[str(trees / 'a')], infos[str(trees / 'b')], infos[str(trees / 'orphan')]
    assert a.session == 'a' and a.state is not None and a.state.clean and a.bytes
    assert b.state is not None and b.state.changed == ('?? new.txt',)
    assert orphan.session == '' and orphan.branch == 'b4m/session/x'
    assert orphan.state is not None and orphan.state.commits  # Nothing to compare against.


def test_removal_reports_losses_then_deletes(setup):
    hub, runner, trees = setup
    tree = make_tree(trees / 'b')
    runner.add(make_record('b', mode='worktree', worktree=tree), alive=False)
    hub.store.set_state('b', 'ended')
    hub.overview()
    (trees / 'b' / 'a.c').write_text('edited\n')
    losses = hub.worktree_losses([tree.path])
    assert losses[tree.path].describe() == '1 uncommitted file'
    assert hub.remove_worktrees([tree.path]) == [tree.path]
    assert not (trees / 'b').exists()
    assert hub.overview().worktrees == ()
    assert hub.store.record('b').worktree == tree  # The record keeps what it had.


def test_a_live_sessions_tree_is_never_removed(setup):
    hub, runner, trees = setup
    tree = make_tree(trees / 'a')
    runner.add(make_record('a', mode='worktree', worktree=tree))
    hub.overview()
    with pytest.raises(SessionError, match='stop session a first'):
        hub.worktree_losses([tree.path])
    with pytest.raises(SessionError, match='stop session a first'):
        hub.remove_worktrees([tree.path])
    assert (trees / 'a').exists()


def test_orphans_can_be_removed_but_nothing_outside_the_directory(setup, tmp_path):
    hub, _runner, trees = setup
    make_tree(trees / 'orphan')
    elsewhere = make_tree(tmp_path / 'elsewhere')
    hub.overview()
    assert hub.remove_worktrees([str(trees / 'orphan')]) == [str(trees / 'orphan')]
    with pytest.raises(SessionError, match='not a session tree'):
        hub.remove_worktrees([elsewhere.path])
    assert (tmp_path / 'elsewhere').exists()


def test_a_session_whose_tree_was_removed_is_not_resumed(setup):
    hub, runner, trees = setup
    tree = make_tree(trees / 'a')
    runner.add(make_record('a', mode='worktree', worktree=tree), alive=False)
    hub.store.set_state('a', 'stopped')
    hub.overview()
    hub.remove_worktrees([tree.path])
    with pytest.raises(SessionError, match='worktree was removed'):
        hub.open('a')
    with pytest.raises(SessionError, match='worktree was removed'):
        hub.show('a', '%1')
    assert runner.resumes == []


def test_a_tree_being_prepared_is_not_removed(setup):
    hub, runner, trees = setup
    tree = make_tree(trees / 'p')
    runner.add(make_record('p', mode='worktree', worktree=tree, state='preparing'), alive=False)
    hub._preparing.add('p')  # This app is preparing it.
    hub.overview()
    with pytest.raises(SessionError, match='stop session p first|being prepared'):
        hub.remove_worktrees([tree.path])
    assert (trees / 'p').exists()


def test_a_trees_extra_checkouts_count_toward_its_size(setup, tmp_path):
    hub, runner, trees = setup
    tree = make_tree(trees / 'a')
    runner.add(make_record('a', mode='worktree', worktree=tree), alive=False)
    alone = hub.overview().worktrees[0].bytes
    extra = tmp_path / 'out' / 'worktrees' / 'base'
    git('worktree', 'add', '-q', '--detach', str(extra), 'HEAD', cwd=trees / 'a')
    (extra / 'big.bin').write_bytes(b'x' * 1_000_000)
    hub.worktrees._sizes.clear()  # Measured again, as after the TTL.
    [info] = hub.overview().worktrees
    assert info.bytes is not None and alone is not None and info.bytes >= alone + 1_000_000
    assert info.state is not None and info.state.extras == (str(extra),)

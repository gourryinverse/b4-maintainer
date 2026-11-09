"""Contract: tracking an upstream branch adds its remote to the project checkout when
needed (no tags, the checkout's partial-clone filter) and a fetch line for that branch
alone, then fetches it; untracking drops the fetch line and the remote-tracking ref,
and the whole remote once it tracks nothing. HEAD, index, and worktree never change."""

import pytest
from gitutil import git, make_checkout

from b4_maintainer.adapters import remotes


@pytest.fixture
def upstream(tmp_path):
    repo = make_checkout(tmp_path / 'upstream')
    ident = ['-c', 'user.name=U', '-c', 'user.email=u@example.org']
    for branch in ('for-next', 'for-linus'):
        git('checkout', '-q', '-b', branch, cwd=repo)
        git(*ident, 'commit', '-q', '--allow-empty', '-m', branch, cwd=repo)
    git('checkout', '-q', 'master', cwd=repo)
    return repo


def state(checkout):
    return git('rev-parse', 'HEAD', cwd=checkout), git('status', '--porcelain', cwd=checkout)


def test_track_adds_the_remote_and_one_branch_then_fetches(tmp_path, upstream):
    checkout = make_checkout(tmp_path / 'linux')
    before = state(checkout)
    assert remotes.heads(checkout, upstream.as_uri()) == ['for-linus', 'for-next', 'master']
    remotes.track(checkout, 'core', upstream.as_uri(), 'for-next')
    assert remotes.remotes(checkout) == {'core': upstream.as_uri()}
    assert remotes.tracked(checkout, 'core') == ['for-next']
    assert git('rev-parse', '--verify', 'core/for-next', cwd=checkout)
    assert git('config', 'remote.core.tagopt', cwd=checkout) == '--no-tags'
    remotes.track(checkout, 'core', upstream.as_uri(), 'for-linus')  # A second branch.
    assert remotes.tracked(checkout, 'core') == ['for-next', 'for-linus']
    assert state(checkout) == before


def test_a_new_remote_takes_the_checkouts_partial_clone_filter(tmp_path, upstream):
    checkout = make_checkout(tmp_path / 'linux')
    git('remote', 'add', 'mainline', upstream.as_uri(), cwd=checkout)
    git('config', 'remote.mainline.promisor', 'true', cwd=checkout)
    git('config', 'remote.mainline.partialclonefilter', 'blob:none', cwd=checkout)
    remotes.track(checkout, 'core', upstream.as_uri(), 'for-next')
    assert git('config', 'remote.core.partialclonefilter', cwd=checkout) == 'blob:none'
    assert git('config', 'remote.core.promisor', cwd=checkout) == 'true'


def test_untrack_drops_the_branch_then_the_remote_once_it_tracks_nothing(tmp_path, upstream):
    checkout = make_checkout(tmp_path / 'linux')
    remotes.track(checkout, 'core', upstream.as_uri(), 'for-next')
    remotes.track(checkout, 'core', upstream.as_uri(), 'for-linus')
    before = state(checkout)
    assert remotes.untrack(checkout, 'core', 'for-next') is False  # for-linus remains.
    assert remotes.tracked(checkout, 'core') == ['for-linus']
    assert 'core/for-next' not in git('branch', '-r', cwd=checkout)
    assert remotes.untrack(checkout, 'core', 'for-linus') is True
    assert remotes.remotes(checkout) == {}
    assert git('branch', '-r', cwd=checkout) == ''
    assert state(checkout) == before

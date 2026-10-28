"""Contract: each remote branch matching a project's track_branches globs gets a local
branch of the same name (its upstream set, marked as tracked), made and fast-forwarded
when not checked out; a diverged or checked-out one is left and said; nothing is pushed."""

from gitutil import git

from b4_maintainer.adapters.scratch import track_branches, tracked_branches

ME = ['-c', 'user.name=M', '-c', 'user.email=m@example.org']


def commit(repo, name):
    (repo / f'{name}.c').write_text(f'{name}\n')
    git('add', '-A', cwd=repo)
    git(*ME, 'commit', '-qm', name, cwd=repo)
    return git('rev-parse', 'HEAD', cwd=repo)


def setup(tmp_path):
    remote = tmp_path / 'remote'
    remote.mkdir()
    git('init', '-q', '-b', 'main', cwd=remote)
    commit(remote, 'base')
    for name in ('scratch/me/a', 'scratch/me/b', 'scratch/other/c'):
        git('switch', '-q', '-c', name, 'main', cwd=remote)
        commit(remote, name.replace('/', '-'))
    git('switch', '-q', 'main', cwd=remote)
    clone = tmp_path / 'clone'
    git('clone', '-q', str(remote), str(clone), cwd=tmp_path)
    return remote, clone


def test_matching_remote_branches_get_local_branches(tmp_path):
    remote, clone = setup(tmp_path)
    made, problems = track_branches(clone, ['origin/scratch/me/*'])
    assert (sorted(made), problems) == (['scratch/me/a', 'scratch/me/b'], [])
    assert git('rev-parse', 'scratch/me/a', cwd=clone) == git(
        'rev-parse', 'origin/scratch/me/a', cwd=clone
    )
    assert git('rev-parse', '--abbrev-ref', 'scratch/me/a@{upstream}', cwd=clone) == (
        'origin/scratch/me/a'
    )
    assert tracked_branches(clone) == {'scratch/me/a', 'scratch/me/b'}
    assert 'scratch/other/c' not in git('branch', '--format=%(refname:short)', cwd=clone)


def test_moved_remote_branches_fast_forward_and_diverged_ones_stay(tmp_path):
    remote, clone = setup(tmp_path)
    track_branches(clone, ['origin/scratch/me/*'])
    git('switch', '-q', 'scratch/me/a', cwd=remote)
    ahead = commit(remote, 'more')
    git('switch', '-q', 'scratch/me/b', cwd=remote)
    git(*ME, 'commit', '-q', '--amend', '-m', 'rewritten', cwd=remote)  # Force-pushed.
    git('switch', '-q', 'main', cwd=remote)
    git('fetch', '-q', 'origin', cwd=clone)
    git('switch', '-q', 'scratch/me/b', cwd=clone)
    commit(clone, 'local')  # Local work on b too.
    git('switch', '-q', 'main', cwd=clone)
    before_b = git('rev-parse', 'scratch/me/b', cwd=clone)
    made, problems = track_branches(clone, ['origin/scratch/me/*'])
    assert git('rev-parse', 'scratch/me/a', cwd=clone) == ahead
    assert git('rev-parse', 'scratch/me/b', cwd=clone) == before_b
    assert any('scratch/me/b' in p and 'diverged' in p for p in problems)
    assert made == ['scratch/me/a']


def test_a_checked_out_tracked_branch_is_not_moved(tmp_path):
    remote, clone = setup(tmp_path)
    track_branches(clone, ['origin/scratch/me/*'])
    git('switch', '-q', 'scratch/me/a', cwd=clone)
    before = git('rev-parse', 'HEAD', cwd=clone)
    git('switch', '-q', 'scratch/me/a', cwd=remote)
    commit(remote, 'later')
    git('fetch', '-q', 'origin', cwd=clone)
    _made, problems = track_branches(clone, ['origin/scratch/me/*'])
    assert git('rev-parse', 'HEAD', cwd=clone) == before
    assert any('checked out' in p for p in problems)

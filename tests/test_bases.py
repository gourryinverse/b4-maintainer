"""Contract: a maintainer base shows how far behind its upstream it is and which of its
commits have landed there; rebasing it replays the rest, or changes nothing."""

import pytest
from gitutil import git

from b4_maintainer.adapters.bases import (
    accept_rebase,
    base_status,
    prepare_rebase_tree,
    rebase_base,
)
from b4_maintainer.workspace import MaintainerBase

BASE = MaintainerBase('me/base', 'up/stable')
ME = ['-c', 'user.name=Example Maintainer', '-c', 'user.email=maintainer@example.org']


def commit(repo, path, text, message, author=None):
    (repo / path).write_text(text)
    git('add', path, cwd=repo)
    who = ['--author', author] if author else []
    git(*ME, 'commit', '-q', *who, '-m', message, cwd=repo)
    return git('rev-parse', 'HEAD', cwd=repo)


@pytest.fixture
def project(tmp_path, home):
    """main checked out on its own branch; up/stable an upstream; me/base two commits on it."""
    (home / '.gitconfig').write_text(
        '[user]\n\tname = Example Maintainer\n\temail = maintainer@example.org\n'
    )
    repo = tmp_path / 'linux'
    repo.mkdir()
    git('init', '-q', '-b', 'main', cwd=repo)
    commit(repo, 'mm.c', ''.join(f'line {n}\n' for n in range(1, 21)), 'mm: start')
    git('update-ref', 'refs/remotes/up/stable', 'HEAD', cwd=repo)
    git('switch', '-q', '-c', 'me/base', 'up/stable', cwd=repo)
    commit(repo, 'a.c', 'a\n', 'mm: fix the leak in a', 'Contributor One <one@example.org>')
    commit(repo, 'b.c', 'b\n', 'mm: fix b', 'Contributor Two <two@example.org>')
    git('switch', '-q', 'main', cwd=repo)
    return repo


def upstream_commit(repo, path, text, message):
    """Advance up/stable by one commit without touching the checkout."""
    git('switch', '-q', '--detach', 'up/stable', cwd=repo)
    sha = commit(repo, path, text, message)
    git('update-ref', 'refs/remotes/up/stable', sha, cwd=repo)
    git('switch', '-q', 'main', cwd=repo)
    return sha


def snapshot(repo):
    return (
        git('symbolic-ref', 'HEAD', cwd=repo),
        git('rev-parse', 'HEAD', cwd=repo),
        (repo / '.git' / 'index').read_bytes(),
        git('worktree', 'list', '--porcelain', cwd=repo),
        git('status', '--porcelain', cwd=repo),
    )


def test_an_up_to_date_base_lists_its_own_commits(project):
    status = base_status(project, BASE)
    assert status.exists and status.behind == 0 and status.error == ''
    assert [c.subject for c in status.commits] == ['mm: fix the leak in a', 'mm: fix b']
    assert not any(c.landed for c in status.commits)
    assert status.commits[0].author == 'Contributor One <one@example.org>'


def test_a_base_behind_its_upstream_shows_what_landed(project):
    upstream_commit(project, 'a.c', 'a\n', 'mm: fix the leak in a')  # Landed (same subject).
    upstream_commit(project, 'mm.c', (project / 'mm.c').read_text() + 'tail\n', 'mm: more')
    status = base_status(project, BASE)
    assert status.behind == 2
    assert [(c.subject, c.landed) for c in status.commits] == [
        ('mm: fix the leak in a', True),
        ('mm: fix b', False),
    ]


def test_a_commit_landed_under_another_subject_is_found_by_its_link(project):
    git('switch', '-q', 'me/base', cwd=project)
    commit(project, 'c.c', 'c\n', 'mm: c\n\nLink: https://lore.kernel.org/r/c-1@example.org')
    git('switch', '-q', 'main', cwd=project)
    upstream_commit(
        project, 'c.c', 'c\n', 'mm: reworded c\n\nLink: https://lore.kernel.org/r/c-1@example.org'
    )
    landed = {c.subject: c.landed for c in base_status(project, BASE).commits}
    assert landed['mm: c'] is True


def test_missing_branches_and_upstreams_are_reported(project):
    assert not base_status(project, MaintainerBase('me/none', 'up/stable')).exists
    assert 'up/none' in base_status(project, MaintainerBase('me/base', 'up/none')).error


def test_rebase_drops_landed_commits_and_replays_the_rest(project):
    upstream_commit(project, 'a.c', 'a\n', 'mm: fix the leak in a')
    tip = upstream_commit(project, 'mm.c', (project / 'mm.c').read_text() + 'tail\n', 'mm: x')
    before = snapshot(project)
    result = rebase_base(project, BASE)
    assert result.ok and result.failed is None
    assert result.dropped == ('mm: fix the leak in a',)
    assert git('log', '--format=%s|%an', 'up/stable..me/base', cwd=project) == (
        'mm: fix b|Contributor Two'
    )
    assert git('merge-base', 'me/base', 'up/stable', cwd=project) == tip
    assert snapshot(project) == before  # The checkout and worktree list never changed.
    assert base_status(project, BASE).behind == 0


def test_a_commit_already_upstream_in_substance_is_dropped_as_empty(project):
    upstream_commit(project, 'b.c', 'b\n', 'mm: b, as the maintainer reworded it')
    result = rebase_base(project, BASE)
    assert result.ok and result.dropped == ('mm: fix b',)
    assert git('log', '--format=%s', 'up/stable..me/base', cwd=project) == ('mm: fix the leak in a')


def test_a_conflict_changes_nothing_and_names_the_commit(project):
    old = git('rev-parse', 'me/base', cwd=project)
    upstream_commit(project, 'b.c', 'something else\n', 'mm: other b')
    before = snapshot(project)
    result = rebase_base(project, BASE)
    assert not result.ok and result.failed is not None
    assert result.failed.subject == 'mm: fix b'
    assert 'CONFLICT' in result.detail and 'b.c' in result.detail  # What the session is told.
    assert git('rev-parse', 'me/base', cwd=project) == old
    assert snapshot(project) == before


def test_a_checked_out_base_is_not_rebased(project, tmp_path):
    other = tmp_path / 'other'
    git('worktree', 'add', '-q', str(other), 'me/base', cwd=project)
    upstream_commit(project, 'mm.c', (project / 'mm.c').read_text() + 'tail\n', 'mm: x')
    status = base_status(project, BASE)
    assert status.checked_out == str(other)
    old = git('rev-parse', 'me/base', cwd=project)
    result = rebase_base(project, BASE)
    assert not result.ok and 'checked out' in result.error
    assert git('rev-parse', 'me/base', cwd=project) == old


def test_a_base_moved_meanwhile_is_left_alone(project):
    upstream_commit(project, 'mm.c', (project / 'mm.c').read_text() + 'tail\n', 'mm: x')
    old = git('rev-parse', 'me/base', cwd=project)
    result = rebase_base(project, BASE, expected=git('rev-parse', 'up/stable', cwd=project))
    assert not result.ok and 'moved' in result.error
    assert git('rev-parse', 'me/base', cwd=project) == old


def workspace(tmp_path, project):
    (tmp_path / 'workspace.toml').write_text(
        '[[projects]]\nid = "linux"\npath = "linux"\n'
        'bases = [{ branch = "me/base", upstream = "up/stable" }]\n'
    )
    return tmp_path


def test_the_bases_command_shows_each_base(project, tmp_path, capsys):
    from b4_maintainer.cli import bases_main

    root = workspace(tmp_path, project)
    upstream_commit(project, 'a.c', 'a\n', 'mm: fix the leak in a')
    assert bases_main(['-C', str(root)]) == 0
    out = capsys.readouterr().out
    assert 'linux me/base on up/stable: 1 behind, 2 commits (1 landed)' in out
    assert 'mm: fix the leak in a  (landed)' in out and 'mm: fix b' in out


def test_the_bases_command_rebases_one_base(project, tmp_path, capsys):
    from b4_maintainer.cli import bases_main

    root = workspace(tmp_path, project)
    upstream_commit(project, 'b.c', 'something else\n', 'mm: other b')
    assert bases_main(['-C', str(root), '--rebase', 'me/base']) == 1
    assert 'did not replay: mm: fix b' in capsys.readouterr().err
    git('update-ref', 'refs/remotes/up/stable', 'up/stable~1', cwd=project)
    upstream_commit(project, 'a.c', 'a\n', 'mm: fix the leak in a')
    assert bases_main(['-C', str(root), '--rebase', 'me/base']) == 0
    out = capsys.readouterr().out
    assert 'me/base rebased onto up/stable' in out and 'dropped: mm: fix the leak in a' in out
    assert bases_main(['-C', str(root), '--rebase', 'me/none']) == 2


AGENT = ('b4-maintainer agent (dry-run)', 'agent@b4-maintainer.invalid')


def project_state(repo):
    return (
        *snapshot(repo),
        (repo / '.git' / 'FETCH_HEAD').exists(),
        git('for-each-ref', 'refs/b4m', cwd=repo),
    )


def test_a_rebase_tree_holds_the_base_and_its_upstream(project, tmp_path):
    upstream_commit(project, 'b.c', 'something else\n', 'mm: other b')
    before = project_state(project)
    dest = tmp_path / 'var' / 'worktrees' / 'r1'
    tree = prepare_rebase_tree(project, dest, 'b4m/session/r1', BASE, AGENT)
    status = base_status(project, BASE)
    assert tree.path == str(dest) and tree.branch == 'b4m/session/r1'
    assert (tree.base, tree.applied) == (status.fork, status.tip)
    assert git('rev-parse', 'HEAD', cwd=dest) == status.tip
    # The upstream under its own name, as the maintainer sees it.
    assert git('rev-parse', 'up/stable', cwd=dest) == git('rev-parse', 'up/stable', cwd=project)
    assert git('config', 'user.email', cwd=dest) == AGENT[1]
    assert not (dest / '.git' / 'hooks' / 'commit-msg').exists()  # Sign-offs must survive.
    assert project_state(project) == before


def agent_rebase(dest):
    """What the agent does: rebase onto the upstream, resolving the conflict, on b4m/rebased."""
    git('switch', '-q', '-c', 'b4m/rebased', cwd=dest)
    run = __import__('subprocess').run(
        ['git', 'rebase', '-q', 'up/stable'], cwd=dest, capture_output=True, text=True
    )
    assert run.returncode != 0  # b.c conflicts.
    (dest / 'b.c').write_text('something else\nb\n')
    git('add', 'b.c', cwd=dest)
    git('-c', 'core.editor=true', 'rebase', '--continue', cwd=dest)
    return git('rev-parse', 'HEAD', cwd=dest)


def test_accepting_moves_the_base_to_the_result(project, tmp_path):
    upstream_commit(project, 'b.c', 'something else\n', 'mm: other b')
    old = git('rev-parse', 'me/base', cwd=project)
    dest = tmp_path / 'wt'
    prepare_rebase_tree(project, dest, 'b4m/session/r1', BASE, AGENT)
    result = agent_rebase(dest)
    before = project_state(project)
    outcome = accept_rebase(project, BASE, dest, 'b4m/rebased', expected=old)
    assert outcome.ok and outcome.tip == result
    assert git('rev-parse', 'me/base', cwd=project) == result
    assert git('log', '--format=%s|%an', 'up/stable..me/base', cwd=project).splitlines() == [
        'mm: fix b|Contributor Two',
        'mm: fix the leak in a|Contributor One',
    ]
    after = project_state(project)
    assert after[:6] == before[:6]  # HEAD, index, worktrees, status, FETCH_HEAD untouched.
    assert after[6] == ''  # No scratch ref left behind.
    assert base_status(project, BASE).behind == 0


def test_accepting_is_refused_when_anything_moved(project, tmp_path):
    upstream_commit(project, 'b.c', 'something else\n', 'mm: other b')
    old = git('rev-parse', 'me/base', cwd=project)
    dest = tmp_path / 'wt'
    prepare_rebase_tree(project, dest, 'b4m/session/r1', BASE, AGENT)
    assert 'b4m/rebased' in accept_rebase(project, BASE, dest, 'b4m/rebased', old).error
    agent_rebase(dest)
    moved = accept_rebase(project, BASE, dest, 'b4m/rebased', expected='0' * 40)
    assert not moved.ok and 'moved' in moved.error
    upstream_commit(project, 'mm.c', (project / 'mm.c').read_text() + 'tail\n', 'mm: newer')
    stale = accept_rebase(project, BASE, dest, 'b4m/rebased', expected=old)
    assert not stale.ok and 'up/stable' in stale.error  # The result predates the upstream.
    assert git('rev-parse', 'me/base', cwd=project) == old
    assert git('for-each-ref', 'refs/b4m', cwd=project) == ''

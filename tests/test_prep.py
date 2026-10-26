"""Contract: prep branches are b4's own, made and worked on by running b4 prep in a sparse
worktree of the project; the branch is checked out there only while b4 runs."""

import pytest
from gitutil import git

from b4_maintainer.adapters.bases import pluck_onto
from b4_maintainer.adapters.prep import (
    PrepError,
    cover_text,
    cut_prep,
    outgoing_summary,
    prep_tracking,
    preview_send,
    run_b4,
    sign_off,
)

ME = ['-c', 'user.name=Example Maintainer', '-c', 'user.email=maintainer@example.org']
GET_MAINTAINER = '#!/bin/sh\necho "Subsystem Maintainer <sub@example.org>"\n'
CHECKPATCH = '#!/bin/sh\ncat >/dev/null\nexit 0\n'


def commit(repo, path, text, message):
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text(text)
    git('add', path, cwd=repo)
    git(*ME, 'commit', '-q', '-m', message, cwd=repo)
    return git('rev-parse', 'HEAD', cwd=repo)


@pytest.fixture
def project(tmp_path, home):
    (home / '.gitconfig').write_text(
        '[user]\n\tname = Example Maintainer\n\temail = maintainer@example.org\n'
    )
    repo = tmp_path / 'linux'
    repo.mkdir()
    git('init', '-q', '-b', 'main', cwd=repo)
    for name, text in (
        ('scripts/get_maintainer.pl', GET_MAINTAINER),
        ('scripts/checkpatch.pl', CHECKPATCH),
    ):
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
        (repo / name).chmod(0o755)
        git('add', name, cwd=repo)
    commit(repo, 'MAINTAINERS', 'M: Subsystem Maintainer <sub@example.org>\n', 'start')
    commit(repo, 'mm/deep/big.c', 'x\n' * 1000, 'mm: a big file')
    git('update-ref', 'refs/remotes/up/stable', 'HEAD', cwd=repo)
    git('switch', '-q', '-c', 'side', cwd=repo)
    fix = commit(repo, 'mm/fix.c', 'fixed\n', 'mm: fix a thing\n\nBecause.')
    git('switch', '-q', 'main', cwd=repo)
    return repo, fix


def snapshot(repo):
    return (
        git('symbolic-ref', 'HEAD', cwd=repo),
        git('rev-parse', 'HEAD', cwd=repo),
        (repo / '.git' / 'index').read_bytes(),
        git('status', '--porcelain', cwd=repo),
    )


def test_a_prep_branch_is_cut_by_b4_in_a_sparse_worktree(project, tmp_path):
    repo, _fix = project
    before = snapshot(repo)
    wt = tmp_path / 'var' / 'branches' / 'linux' / 'pull-aheads'
    branch = cut_prep(repo, wt, 'pull-aheads', 'up/stable', 'mm: pulled ahead\n\nTwo fixes.\n')
    assert branch == 'b4/pull-aheads'
    assert git('rev-parse', '--verify', 'refs/heads/b4/pull-aheads', cwd=repo)
    # b4's own: its cover commit and its branch configuration.
    assert cover_text(repo, branch).startswith('mm: pulled ahead\n\nTwo fixes.')
    assert git('config', f'branch.{branch}.b4-prep-cover-strategy', cwd=repo) == 'commit'
    # Sparse: scripts and top-level files, not the tree.
    assert (wt / 'scripts' / 'get_maintainer.pl').exists() and (wt / 'MAINTAINERS').exists()
    assert not (wt / 'mm' / 'deep' / 'big.c').exists()
    # Detached between b4 runs; the throwaway start branch is gone.
    assert git('rev-parse', '--abbrev-ref', 'HEAD', cwd=wt) == 'HEAD'
    assert git('branch', '--list', 'b4m/*', cwd=repo) == ''
    assert snapshot(repo) == before  # The main checkout is untouched.


def test_commits_land_and_b4_runs_on_the_branch(project, tmp_path):
    repo, fix = project
    wt = tmp_path / 'wt'
    branch = cut_prep(repo, wt, 'pa', 'up/stable', None)
    tip = git('rev-parse', branch, cwd=repo)
    result = pluck_onto(repo, branch, repo, [fix], tip, 'agent@b4-maintainer.invalid', 'x')
    assert result.ok
    code, out = run_b4(wt, branch, 'prep', '--auto-to-cc')
    assert code == 0, out
    assert 'sub@example.org' in cover_text(repo, branch)
    code, out = run_b4(wt, branch, 'prep', '--check')
    assert code == 0 and 'mm: fix a thing' in out
    assert git('rev-parse', '--abbrev-ref', 'HEAD', cwd=wt) == 'HEAD'  # Detached again.
    assert git('status', '--porcelain', cwd=wt) == ''


def test_bad_names_and_existing_branches_are_refused(project, tmp_path):
    repo, _fix = project
    with pytest.raises(PrepError, match='name'):
        cut_prep(repo, tmp_path / 'a', 'Bad Name', 'up/stable', None)
    cut_prep(repo, tmp_path / 'b', 'taken', 'up/stable', None)
    with pytest.raises(PrepError, match='exists'):
        cut_prep(repo, tmp_path / 'c', 'taken', 'up/stable', None)
    assert not (tmp_path / 'a').exists() and not (tmp_path / 'c').exists()


def test_a_failed_cut_leaves_nothing(project, tmp_path):
    repo, _fix = project
    with pytest.raises(PrepError):
        cut_prep(repo, tmp_path / 'x', 'nofork', 'no/such/ref', None)
    assert not (tmp_path / 'x').exists()
    assert git('branch', '--list', 'b4/*', 'b4m/*', cwd=repo) == ''
    assert git('worktree', 'list', '--porcelain', cwd=repo).count('worktree ') == 1


def test_a_branch_checked_out_elsewhere_is_not_switched_to(project, tmp_path):
    repo, _fix = project
    wt = tmp_path / 'wt'
    branch = cut_prep(repo, wt, 'busy', 'up/stable', None)
    git('switch', '-q', branch, cwd=repo)  # The maintainer works on it in the main checkout.
    code, out = run_b4(wt, branch, 'prep', '--show-info')
    assert code != 0 and 'checked out' in out


def test_sign_off_adds_the_maintainers_sign_off_to_each_commit(project, tmp_path):
    repo, fix = project
    wt = tmp_path / 'wt'
    branch = cut_prep(repo, wt, 'so', 'up/stable', None)
    tip = git('rev-parse', branch, cwd=repo)
    pluck_onto(repo, branch, repo, [fix], tip, 'agent@b4-maintainer.invalid', 'x')
    tip = git('rev-parse', branch, cwd=repo)
    tree_before = git('rev-parse', f'{branch}^{{tree}}', cwd=repo)
    result = sign_off(repo, branch, tip, 'Example Maintainer', 'maintainer@example.org')
    assert result.ok and result.signed == 1
    body = git('log', '-1', '--format=%B', branch, cwd=repo)
    assert body.rstrip().endswith('Signed-off-by: Example Maintainer <maintainer@example.org>')
    assert git('rev-parse', f'{branch}^{{tree}}', cwd=repo) == tree_before
    assert cover_text(repo, branch)  # The cover commit is untouched (b4 still finds it).
    again = sign_off(repo, branch, result.tip, 'Example Maintainer', 'maintainer@example.org')
    assert again.ok and again.signed == 0 and again.tip == result.tip
    stale = sign_off(repo, branch, tip, 'Example Maintainer', 'maintainer@example.org')
    assert not stale.ok and 'moved' in stale.error


def test_the_outgoing_series_is_previewed_by_b4_without_sending(project, tmp_path):
    repo, fix = project
    wt = tmp_path / 'wt'
    branch = cut_prep(repo, wt, 'out', 'up/stable', 'mm: one fix\n\nIt fixes a thing.\n')
    tip = git('rev-parse', branch, cwd=repo)
    pluck_onto(repo, branch, repo, [fix], tip, 'agent@b4-maintainer.invalid', 'x')
    run_b4(wt, branch, 'prep', '--auto-to-cc')
    out = tmp_path / 'outgoing'
    code, text, messages = preview_send(wt, branch, out, no_sign=True)
    assert code == 0, text
    assert len(messages) == 1  # One patch: b4 sends no separate cover letter.
    summary = outgoing_summary(messages)
    assert 'Subject: [PATCH] mm: fix a thing' in summary
    assert 'sub@example.org' in summary and 'It fixes a thing.' in summary


def test_the_revision_and_sent_history_come_from_b4s_tracking(project, tmp_path):
    repo, _fix = project
    branch = cut_prep(repo, tmp_path / 'wt', 'rev', 'up/stable', None)
    tracking = prep_tracking(repo, branch)
    assert tracking['revision'] == 1 and tracking['sent'] == ()

"""Contract: prep branches are b4's own, made and worked on by running b4 prep in a sparse
worktree of the project; the branch is checked out there only while b4 runs."""

import subprocess

import pytest
from gitutil import git

from b4_maintainer.adapters.bases import pluck_onto
from b4_maintainer.adapters.prep import (
    PrepError,
    cleanup_prep,
    cover_text,
    cut_prep,
    move_prep,
    outgoing_summary,
    prep_fork,
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


def test_a_prep_branch_moves_onto_a_new_base_keeping_b4s_cover(project, tmp_path):
    repo, fix = project
    git('branch', 'me/base', 'up/stable', cwd=repo)
    branch = cut_prep(repo, tmp_path / 'wt', 'mv', 'me/base', 'mm: moving\n\nBody.\n')
    tip = git('rev-parse', branch, cwd=repo)
    pluck_onto(repo, branch, repo, [fix], tip, 'agent@b4-maintainer.invalid', 'x')
    git('switch', '-q', '--detach', 'me/base', cwd=repo)
    new_base = commit(repo, 'other.c', 'o\n', 'mm: the base moved')
    git('switch', '-q', 'main', cwd=repo)
    assert prep_fork(repo, branch) == git('rev-parse', 'up/stable', cwd=repo)
    old = git('rev-parse', branch, cwd=repo)
    result = move_prep(repo, branch, new_base, old)
    assert result.ok
    assert prep_fork(repo, branch) == new_base
    assert git('log', '--format=%s', f'{new_base}..{branch}', cwd=repo).splitlines() == [
        'mm: fix a thing',
        'mm: moving',  # b4's cover commit, recreated.
    ]
    assert cover_text(repo, branch).startswith('mm: moving')
    assert prep_tracking(repo, branch)['revision'] == 1  # b4's tracking survives.


def test_a_prep_branch_that_conflicts_with_the_new_base_is_left_alone(project, tmp_path):
    repo, fix = project
    branch = cut_prep(repo, tmp_path / 'wt', 'stuck', 'up/stable', None)
    tip = git('rev-parse', branch, cwd=repo)
    pluck_onto(repo, branch, repo, [fix], tip, 'agent@b4-maintainer.invalid', 'x')
    git('switch', '-q', '--detach', 'up/stable', cwd=repo)
    clash = commit(repo, 'mm/fix.c', 'different\n', 'mm: clash')
    git('switch', '-q', 'main', cwd=repo)
    old = git('rev-parse', branch, cwd=repo)
    result = move_prep(repo, branch, clash, old)
    assert not result.ok and result.failed == 'mm: fix a thing'
    assert git('rev-parse', branch, cwd=repo) == old


def test_cleanup_archives_and_removes_a_prep_branch_the_b4_way(project, tmp_path):
    repo, fix = project
    wt = tmp_path / 'var' / 'branches' / 'linux' / 'gone'
    branch = cut_prep(repo, wt, 'gone', 'up/stable', 'mm: gone\n\nBody.\n')
    tip = git('rev-parse', branch, cwd=repo)
    pluck_onto(repo, branch, repo, [fix], tip, 'agent@b4-maintainer.invalid', 'x')
    (wt.parent / 'gone.outgoing').mkdir()
    code, out = cleanup_prep(repo, wt, branch)
    assert code == 0, out
    assert git('branch', '--list', branch, cwd=repo) == ''
    assert not wt.exists() and not (wt.parent / 'gone.outgoing').exists()
    assert git('worktree', 'list', '--porcelain', cwd=repo).count('worktree ') == 1


def test_cleanup_of_an_empty_prep_branch_removes_it_directly(project, tmp_path):
    repo, _fix = project
    wt = tmp_path / 'wt'
    branch = cut_prep(repo, wt, 'empty', 'up/stable', None)
    code, out = cleanup_prep(repo, wt, branch)
    assert code == 0, out
    assert git('branch', '--list', branch, cwd=repo) == ''
    left = subprocess.run(
        ['git', 'config', '--get-regexp', 'branch.b4/empty'],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert left.stdout == ''


def test_a_prep_branch_takes_new_commits_in_place_of_its_own(project, tmp_path):
    """The next revision: b4's cover commit (and tracking) recreated on the new base, then
    the given commits in place of the branch's; a moved branch or a conflict changes
    nothing."""
    from b4_maintainer.adapters.prep import reroll_prep

    repo, fix = project
    branch = cut_prep(repo, tmp_path / 'wt', 'next', 'up/stable', 'mm: next\n\nBody.\n')
    tip = git('rev-parse', branch, cwd=repo)
    pluck_onto(repo, branch, repo, [fix], tip, 'agent@b4-maintainer.invalid', 'x')
    git('switch', '-q', '--detach', 'up/stable', cwd=repo)
    lead = commit(repo, 'mm/lead.c', 'lead\n', 'mm: a pull-ahead it needs')
    folded = commit(repo, 'mm/fix.c', 'fixed better\n', 'mm: fix a thing\n\nBecause.')
    git('switch', '-q', 'main', cwd=repo)
    old = git('rev-parse', branch, cwd=repo)
    assert not reroll_prep(repo, branch, 'up/stable', [lead, folded], 'f' * 40).ok  # Moved.
    result = reroll_prep(repo, branch, 'up/stable', [lead, folded], old)
    assert result.ok
    assert git('log', '--format=%s', f'up/stable..{branch}', cwd=repo).splitlines() == [
        'mm: fix a thing',
        'mm: a pull-ahead it needs',
        'mm: next',
    ]
    assert git('show', f'{branch}:mm/fix.c', cwd=repo) == 'fixed better'
    assert prep_tracking(repo, branch)['revision'] == 1 and cover_text(repo, branch).startswith(
        'mm: next'
    )
    now = git('rev-parse', branch, cwd=repo)
    git('switch', '-q', '--detach', 'up/stable', cwd=repo)
    clash = commit(repo, 'mm/lead.c', 'other\n', 'mm: clashes with the lead')
    git('switch', '-q', 'main', cwd=repo)
    assert not reroll_prep(repo, branch, 'up/stable', [clash, lead], now).ok
    assert git('rev-parse', branch, cwd=repo) == now


@pytest.fixture
def lore(tmp_path, monkeypatch):
    """A lore stand-in serving one thread's mbox (b4.midmask points at it)."""
    import gzip
    import http.server
    import threading

    served = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = served.get('mbox')
            if body is None or not self.path.endswith('/t.mbox.gz'):
                self.send_response(404)
                self.end_headers()
                return
            data = gzip.compress(body)
            self.send_response(200)
            self.send_header('Content-Type', 'application/gzip')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    for name in ('NO_PROXY', 'no_proxy'):  # Reach it directly, not through a proxy.
        monkeypatch.setenv(name, '127.0.0.1,localhost')
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield served, f'http://127.0.0.1:{server.server_address[1]}/all/%s'
    server.shutdown()


def test_a_prep_branch_starts_from_a_posted_thread(project, tmp_path, lore):
    """Someone else's series: b4 prep -F imports its cover letter and prepares the
    revision after the posted one, the patches with their authors (the change-id is
    b4's own unless the posting carried b4's tracking)."""
    from b4_maintainer.adapters.prep import prep_from_thread
    from b4_maintainer.fixtures import PatchSpec, write_series_mbox

    repo, _fix = project
    served, midmask = lore
    git('config', 'b4.midmask', midmask, cwd=repo)
    mbox = tmp_path / 'thread.mbx'
    sent = write_series_mbox(
        mbox,
        'mm: their series',
        revision=2,
        patches=[PatchSpec('mm/one.c'), PatchSpec('mm/two.c')],
        change_id='20261001-their-series-0123456789ab',
    )
    served['mbox'] = mbox.read_bytes()
    git('switch', '-q', '--detach', 'up/stable', cwd=repo)
    commit(repo, 'mm/one.c', 'old\n', 'mm: one')
    base = commit(repo, 'mm/two.c', 'old\n', 'mm: two')
    git('switch', '-q', 'main', cwd=repo)
    branch = prep_from_thread(repo, tmp_path / 'wt', 'their-series', base, sent.msgids[0])
    assert branch == 'b4/their-series'
    tracking = prep_tracking(repo, branch)
    assert tracking['revision'] == 3 and '-their-series-' in tracking['change-id']
    assert 'Imported from' in cover_text(repo, branch)
    subjects = git('log', '--format=%s', f'{base}..{branch}', cwd=repo).splitlines()
    assert len(subjects) == 3 and subjects[-1] == 'mm: their series'  # The cover, first.
    authors = git('log', '--format=%ae', f'{prep_fork(repo, branch)}..{branch}~1', cwd=repo)
    assert 'maintainer@example.org' not in authors.splitlines()[0]  # The patch authors.

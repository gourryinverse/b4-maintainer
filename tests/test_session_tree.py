"""Contract: a worktree session's tree is prepared through b4, never touching the project."""

import json
import pathlib

import pytest
from gitutil import git

from b4_maintainer.adapters.sessiontree import PrepareError, prepare_tree

AGENT = ('b4-maintainer agent (dry-run)', 'agent@b4-maintainer.invalid')
CHANGE_ID = '20261020-mm-foo-0123456789ab'
BRANCH = 'b4m/session/s1'


def write(repo, name, text):
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    git('add', name, cwd=repo)


def commit(repo, message):
    git('commit', '-q', '-m', message, cwd=repo)
    return git('rev-parse', 'HEAD', cwd=repo)


@pytest.fixture
def upstream(tmp_path, home):
    """A project checkout at its base, and a v2 series against that base as one mbox."""
    (home / '.gitconfig').write_text(
        '[user]\n\tname = Example Maintainer\n\temail = maintainer@example.org\n'
    )
    project = tmp_path / 'projects' / 'demo'
    project.mkdir(parents=True)
    git('init', '-q', '-b', 'master', cwd=project)
    write(project, 'mm/foo.c', ''.join(f'line {n}\n' for n in range(1, 21)))
    write(project, 'README', 'demo\n')
    base = commit(project, 'mm: start')
    git('checkout', '-q', '-b', 'series', cwd=project)
    write(project, 'mm/foo.c', (project / 'mm/foo.c').read_text().replace('line 3\n', 'three\n'))
    commit(project, 'mm/foo: name line three\n\nWords for line three.')
    write(project, 'mm/bar.c', 'bar\n')
    commit(project, 'mm/bar: add bar\n\nA new file.')
    out = tmp_path / 'patches'
    options = ['-q', '-v2', '--cover-letter', '--thread', f'--base={base}', '-o', str(out)]
    git('format-patch', *options, f'{base}..series', cwd=project)
    cover = sorted(out.glob('*0000-cover-letter.patch'))[0]
    cover.write_text(
        cover.read_text()
        .replace('*** SUBJECT HERE ***', 'mm: foo and bar')
        .replace('*** BLURB HERE ***', f'Two changes.\n\n---\nchange-id: {CHANGE_ID}')
    )
    mbox = tmp_path / 'series.mbx'
    mbox.write_text(''.join(p.read_text() for p in sorted(out.glob('*.patch'))))
    git('checkout', '-q', 'master', cwd=project)
    git('branch', '-q', '-D', 'series', cwd=project)
    return project, base, mbox


def snapshot(project):
    """What a session must never change in the project."""
    gitdir = project / '.git'
    return {
        'head': git('rev-parse', 'HEAD', cwd=project),
        'symref': git('symbolic-ref', '-q', 'HEAD', cwd=project),
        'index': (gitdir / 'index').read_bytes(),
        'refs': git('for-each-ref', cwd=project),
        'worktrees': git('worktree', 'list', '--porcelain', cwd=project),
        'fetch_head': (gitdir / 'FETCH_HEAD').exists(),
        'config': (gitdir / 'config').read_text(),
        'status': git('status', '--porcelain', cwd=project),
    }


def subjects(repo, spec):
    return git('log', '--reverse', '--format=%s', spec, cwd=repo).splitlines()


def test_a_clean_series_applies_on_its_own_branch_in_a_shared_clone(upstream, tmp_path):
    project, base, mbox = upstream
    before = snapshot(project)
    dest = tmp_path / 'var' / 'worktrees' / 's1'
    tree = prepare_tree(project, dest, BRANCH, str(mbox), 2, identity=AGENT)
    assert tree.path == str(dest) and tree.branch == BRANCH and tree.base == base
    assert tree.applied == git('rev-parse', 'HEAD', cwd=dest)
    assert git('symbolic-ref', '--short', 'HEAD', cwd=dest) == BRANCH
    assert subjects(dest, f'{base}..HEAD') == ['mm/foo: name line three', 'mm/bar: add bar']
    assert (dest / 'mm/bar.c').read_text() == 'bar\n'
    assert git('status', '--porcelain', cwd=dest) == ''
    assert (dest / '.git/objects/info/alternates').read_text().strip() == str(
        project / '.git' / 'objects'
    )
    assert git('worktree', 'list', '--porcelain', cwd=dest).count('worktree ') == 1
    assert snapshot(project) == before


def test_agent_commits_carry_the_agent_identity_and_no_trailers(upstream, tmp_path):
    project, _base, mbox = upstream
    dest = tmp_path / 'wt'
    tree = prepare_tree(project, dest, BRANCH, str(mbox), 2, identity=AGENT)
    write(dest, 'notes.txt', 'agent work\n')
    message = 'mm: agent change\n\nBody.\n\nReviewed-by: Some One <one@example.org>\n'
    git('commit', '-q', '-s', '-m', message, cwd=dest)
    assert git('log', '-1', '--format=%an <%ae>|%cn <%ce>', cwd=dest) == (
        f'{AGENT[0]} <{AGENT[1]}>|{AGENT[0]} <{AGENT[1]}>'
    )
    body = git('log', '-1', '--format=%B', cwd=dest)
    assert body.startswith('mm: agent change') and 'Body.' in body
    assert 'Signed-off-by' not in body and 'Reviewed-by' not in body
    # The series commits keep their authors; the maintainer applied them.
    assert git('log', '-1', '--format=%an|%cn', tree.applied, cwd=dest) == (
        'Example Maintainer|Example Maintainer'
    )


def test_the_series_base_is_used_when_the_project_head_has_moved_on(upstream, tmp_path):
    project, base, mbox = upstream
    write(project, 'mm/foo.c', (project / 'mm/foo.c').read_text().replace('line 3\n', 'tres\n'))
    commit(project, 'mm/foo: conflicting change')
    tree = prepare_tree(project, tmp_path / 'wt', BRANCH, str(mbox), 2, identity=AGENT)
    assert tree.base == base


def test_a_conflict_reports_b4s_output_and_leaves_nothing(upstream, tmp_path):
    project, _base, mbox = upstream
    write(project, 'mm/foo.c', (project / 'mm/foo.c').read_text().replace('line 3\n', 'tres\n'))
    commit(project, 'mm/foo: conflicting change')
    before = snapshot(project)
    dest = tmp_path / 'var' / 'worktrees' / 's1'
    with pytest.raises(PrepareError) as raised:
        prepare_tree(project, dest, BRANCH, str(mbox), 2, base='HEAD', identity=AGENT)
    assert 'mm/foo' in raised.value.output
    assert not dest.exists()
    assert snapshot(project) == before


def test_an_unknown_base_or_revision_is_an_error(upstream, tmp_path):
    project, _base, mbox = upstream
    with pytest.raises(PrepareError, match='base'):
        prepare_tree(project, tmp_path / 'a', BRANCH, str(mbox), 2, base='nope', identity=AGENT)
    with pytest.raises(PrepareError, match='v7'):
        prepare_tree(project, tmp_path / 'b', BRANCH, str(mbox), 7, identity=AGENT)
    assert not (tmp_path / 'a').exists() and not (tmp_path / 'b').exists()


def test_an_existing_destination_is_never_reused(upstream, tmp_path):
    project, _base, mbox = upstream
    dest = tmp_path / 'wt'
    dest.mkdir()
    (dest / 'keep').write_text('mine\n')
    with pytest.raises(PrepareError, match='exists'):
        prepare_tree(project, dest, BRANCH, str(mbox), 2, identity=AGENT)
    assert (dest / 'keep').read_text() == 'mine\n'


def review_branch(project: pathlib.Path, mbox: pathlib.Path, base: str, revision: int) -> str:
    """A b4 review branch: the series on its base, then b4's tracking commit."""
    branch = f'b4/review/{CHANGE_ID}'
    git('checkout', '-q', '-b', branch, base, cwd=project)
    patches = sorted(mbox.parent.glob('patches/*.patch'))[1:]  # Not the cover letter.
    git('am', '-q', *map(str, patches), cwd=project)
    write(project, 'review-only', f'v{revision}\n')
    commit(project, f'mm: only on the v{revision} review branch')
    first = git('rev-list', '--reverse', f'{base}..HEAD', cwd=project).splitlines()[0]
    tracking = {'series': {'revision': revision, 'first-patch-commit': first}}
    message = f'mm: foo and bar\n\n--- b4-review-tracking ---\n# b4 data\n{json.dumps(tracking)}\n'
    git('commit', '-q', '--allow-empty', '-m', message, cwd=project)
    git('checkout', '-q', 'master', cwd=project)
    return branch


def test_a_series_under_b4_review_starts_from_its_review_branch(upstream, tmp_path):
    project, base, mbox = upstream
    branch = review_branch(project, mbox, base, revision=2)
    # The review branch is the maintainer's: its commits are the ones the session gets.
    before = snapshot(project)
    dest = tmp_path / 'wt'
    tree = prepare_tree(project, dest, BRANCH, str(mbox), 2, change_id=CHANGE_ID, identity=AGENT)
    assert tree.applied == git('rev-parse', f'{branch}~1', cwd=project)
    assert tree.base == base
    assert subjects(dest, f'{base}..HEAD')[-1] == 'mm: only on the v2 review branch'
    assert git('rev-parse', 'HEAD', cwd=dest) == tree.applied
    assert git('symbolic-ref', '--short', 'HEAD', cwd=dest) == BRANCH
    assert snapshot(project) == before


def test_a_review_branch_of_another_revision_is_not_used(upstream, tmp_path):
    project, base, mbox = upstream
    review_branch(project, mbox, base, revision=1)
    prepare_tree(
        project, tmp_path / 'wt', BRANCH, str(mbox), 2, change_id=CHANGE_ID, identity=AGENT
    )
    assert subjects(tmp_path / 'wt', f'{base}..HEAD') == [
        'mm/foo: name line three',
        'mm/bar: add bar',
    ]

"""Contract: a worktree session's tree is prepared through b4, never touching the project."""

import json
import pathlib

import pytest
from gitutil import git

from b4_maintainer.adapters.sessiontree import (
    PrepareError,
    base_suggestions,
    prepare_start_tree,
    prepare_tree,
)
from b4_maintainer.sessions.trees import classify_tree, inspect_tree, remove_tree, tree_worktrees

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
    dest = tmp_path / 'wt'
    tree = prepare_tree(project, dest, BRANCH, str(mbox), 2, identity=AGENT)
    assert tree.base == base
    # Only the session branch: the project's moved-on HEAD is not work to lose.
    assert git('branch', '--format=%(refname:short)', cwd=dest) == BRANCH
    assert inspect_tree(dest, tree.applied).clean
    # In the sandbox, files belong to another uid: git must not re-hash the tree for it.
    assert git('config', 'core.checkStat', cwd=dest) == 'minimal'


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


def test_a_fresh_tree_is_clean_and_changes_are_listed(upstream, tmp_path):
    project, _base, mbox = upstream
    dest = tmp_path / 'var' / 'worktrees' / 's1'
    tree = prepare_tree(project, dest, BRANCH, str(mbox), 2, identity=AGENT)
    assert inspect_tree(dest, tree.applied).clean
    (dest / 'mm/bar.c').write_text('changed\n')
    (dest / 'scratch.txt').write_text('new\n')
    state = inspect_tree(dest, tree.applied)
    assert len(state.changed) == 2 and state.commits == ()
    git('add', '-A', cwd=dest)
    git('commit', '-q', '-m', 'agent: work', cwd=dest)
    git('checkout', '-q', '-b', 'other', tree.applied, cwd=dest)
    write(dest, 'other.txt', 'x\n')
    git('commit', '-q', '-m', 'agent: elsewhere', cwd=dest)
    state = inspect_tree(dest, tree.applied)
    assert state.changed == ()
    assert sorted(c.split(' ', 1)[1] for c in state.commits) == ['agent: elsewhere', 'agent: work']
    assert state.describe() == '2 commits only here'
    # Without the series' last commit, everything beyond the cloned refs counts.
    assert len(inspect_tree(dest).commits) == 4


def test_removal_deletes_only_a_tree_under_the_worktrees_directory(upstream, tmp_path):
    project, _base, mbox = upstream
    before = snapshot(project)
    trees = tmp_path / 'var' / 'worktrees'
    dest = trees / 's1'
    prepare_tree(project, dest, BRANCH, str(mbox), 2, identity=AGENT)
    with pytest.raises(OSError):
        remove_tree(project, trees)
    with pytest.raises(OSError):
        remove_tree(dest / 'mm', trees)
    remove_tree(dest, trees)
    assert not dest.exists() and project.exists()
    assert snapshot(project) == before
    assert inspect_tree(dest).error == 'not a git tree'


def test_base_suggestions_come_from_b4(upstream):
    project, _base, _mbox = upstream
    git('config', 'b4.review-apply-base', 'master', cwd=project)
    assert base_suggestions(project)[:2] == ['HEAD', 'master']


def agent_work(dest, tree):
    """What an agent following the b4m-branches skill leaves in its tree."""
    git('switch', '-q', '-c', 'b4m/pull-ahead/readme', tree.base, cwd=dest)
    write(dest, 'README', 'demo, fixed\n')
    git('commit', '-q', '-m', 'demo: fix the README', cwd=dest)
    git('switch', '-q', '-c', 'b4m/fixup/three', tree.applied, cwd=dest)
    write(dest, 'mm/foo.c', (dest / 'mm/foo.c').read_text().replace('three\n', 'three!\n'))
    git('commit', '-q', '-m', 'mm/foo: fixup for line three', cwd=dest)
    git('switch', '-q', '-c', 'b4m/tests/bar', tree.applied, cwd=dest)
    write(dest, 'mm/bar_test.c', 'test\n')
    git('commit', '-q', '-m', 'mm/bar: add a test', cwd=dest)
    git('switch', '-q', '-c', 'b4m/pull-ahead/wrong', tree.applied, cwd=dest)  # On the series.
    write(dest, 'wrong.c', 'x\n')
    git('commit', '-q', '-m', 'demo: misplaced pull-ahead', cwd=dest)
    git('switch', '-q', 'b4m/session/s1', cwd=dest)
    write(dest, 'notes.c', 'n\n')
    git('commit', '-q', '-m', 'demo: on the session branch', cwd=dest)


def test_tree_commits_are_classed_by_the_skills_branch_names(upstream, tmp_path):
    project, base, mbox = upstream
    dest = tmp_path / 'wt'
    tree = prepare_tree(project, dest, BRANCH, str(mbox), 2, identity=AGENT)
    agent_work(dest, tree)
    found = classify_tree(dest, tree)
    assert [c.subject for c in found.series] == ['mm/foo: name line three', 'mm/bar: add bar']
    by_name = {b.name: b for b in found.branches}
    assert set(by_name) == {
        BRANCH,
        'b4m/pull-ahead/readme',
        'b4m/pull-ahead/wrong',
        'b4m/fixup/three',
        'b4m/tests/bar',
    }
    readme = by_name['b4m/pull-ahead/readme']
    assert readme.kind == 'pull-ahead' and readme.problem == ''
    assert [c.subject for c in readme.commits] == ['demo: fix the README']
    assert readme.commits[0].author.startswith('b4-maintainer agent')
    assert by_name['b4m/fixup/three'].kind == 'fixup' and by_name['b4m/fixup/three'].problem == ''
    assert [c.subject for c in by_name['b4m/tests/bar'].commits] == ['mm/bar: add a test']
    wrong = by_name['b4m/pull-ahead/wrong']
    assert 'series' in wrong.problem  # Pull-aheads start from the base, not the series.
    assert [c.subject for c in wrong.commits] == ['demo: misplaced pull-ahead']
    session = by_name[BRANCH]
    assert session.kind == 'other' and [c.subject for c in session.commits] == [
        'demo: on the session branch'
    ]


def test_a_folded_series_starts_from_the_base(upstream, tmp_path):
    """b4m/folded/: the series rewritten with its fixups squashed in, from the base; one
    built on top of the series as posted is misplaced."""
    project, base, mbox = upstream
    dest = tmp_path / 'wt'
    tree = prepare_tree(project, dest, BRANCH, str(mbox), 2, identity=AGENT)
    agent_work(dest, tree)
    git('switch', '-q', '-c', 'b4m/folded/v3', tree.base, cwd=dest)
    first, second = git('rev-list', '--reverse', f'{tree.base}..{tree.applied}', cwd=dest).split()
    git('cherry-pick', first, cwd=dest)
    git('cherry-pick', '-n', 'b4m/fixup/three', cwd=dest)  # Squashed into patch 1.
    git('commit', '-q', '--amend', '--no-edit', cwd=dest)
    git('cherry-pick', second, cwd=dest)
    git('switch', '-q', '-c', 'b4m/folded/wrong', tree.applied, cwd=dest)
    write(dest, 'x.c', 'x\n')
    git('commit', '-q', '-m', 'demo: on the series as posted', cwd=dest)
    git('switch', '-q', BRANCH, cwd=dest)
    by_name = {b.name: b for b in classify_tree(dest, tree).branches}
    folded = by_name['b4m/folded/v3']
    assert folded.kind == 'folded' and folded.problem == ''
    assert [c.subject for c in folded.commits] == [
        'mm/foo: name line three',
        'mm/bar: add bar',
    ]
    assert 'base' in by_name['b4m/folded/wrong'].problem


def test_a_trees_losses_are_described_by_kind(upstream, tmp_path):
    project, base, mbox = upstream
    dest = tmp_path / 'wt'
    tree = prepare_tree(project, dest, BRANCH, str(mbox), 2, identity=AGENT)
    assert inspect_tree(dest, tree.applied, tree).describe() == 'clean'
    agent_work(dest, tree)
    state = inspect_tree(dest, tree.applied, tree)
    assert state.describe() == '2 pull-aheads, 1 fixup, 1 test, 1 other commit only here'


def test_extra_checkouts_are_found_counted_and_removed_with_the_tree(upstream, tmp_path):
    project, base, mbox = upstream
    trees = tmp_path / 'var' / 'worktrees'
    dest = trees / 's1'
    tree = prepare_tree(project, dest, BRANCH, str(mbox), 2, identity=AGENT)
    out = tmp_path / 'var' / 'sessions' / 's1' / 'work' / 'out'
    out.mkdir(parents=True)
    extra = out / 'worktrees' / 'base'
    git('worktree', 'add', '-q', '--detach', str(extra), tree.base, cwd=dest)  # As the skill says.
    (extra / 'scratch.c').write_text('x\n')
    second = out / 'worktrees' / 'tip'
    git('worktree', 'add', '-q', '--detach', str(second), tree.applied, cwd=dest)
    found = tree_worktrees(dest)  # Every one, not only the last listed.
    assert [(w.path, w.head) for w in found] == [
        (str(extra), tree.base),
        (str(second), tree.applied),
    ]
    state = inspect_tree(dest, tree.applied, tree)
    assert state.extras == (str(extra), str(second))
    assert state.describe() == '1 uncommitted file, 2 extra checkouts'
    assert not state.clean  # Its uncommitted file would be lost.
    unrelated = tmp_path / 'unrelated'
    unrelated.mkdir()
    (unrelated / '.git').write_text('gitdir: /elsewhere/.git/worktrees/x\n')
    remove_tree(dest, trees)
    assert not dest.exists() and not extra.exists() and not second.exists()
    assert unrelated.exists()
    assert out.exists()  # The rest of the session's output stays.


def test_a_start_tree_holds_nothing_applied(upstream, tmp_path):
    project, base, _mbox = upstream
    before = snapshot(project)
    at_base = prepare_start_tree(project, tmp_path / 'a', 'b4m/session/d1', base, AGENT)
    assert git('rev-parse', 'HEAD', cwd=tmp_path / 'a') == base
    assert (at_base.base, at_base.applied) == (base, base)  # No series applied.
    assert not (tmp_path / 'a' / 'mm/bar.c').exists()
    assert git('config', 'user.email', cwd=tmp_path / 'a') == AGENT[1]
    assert (tmp_path / 'a' / '.git' / 'hooks' / 'commit-msg').exists()
    assert snapshot(project) == before
    with pytest.raises(PrepareError):
        prepare_start_tree(project, tmp_path / 'c', 'b4m/session/d3', 'nope', AGENT)
    assert not (tmp_path / 'c').exists()


def second_series(project, base, tmp_path, path, text, subject, name='other'):
    """Another posted series against the same base, as its own mbox."""
    git('checkout', '-q', '-b', name, base, cwd=project)
    write(project, path, text)
    commit(project, f'{subject}\n\nA second series.')
    out = tmp_path / f'{name}-patches'
    git(
        'format-patch',
        '-q',
        '-v1',
        '--thread',
        f'--base={base}',
        '-o',
        str(out),
        f'{base}..{name}',
        cwd=project,
    )
    mbox = tmp_path / f'{name}.mbx'
    mbox.write_text(''.join(p.read_text() for p in sorted(out.glob('*.patch'))))
    git('checkout', '-q', 'master', cwd=project)
    git('branch', '-q', '-D', name, cwd=project)
    return mbox


def test_a_combined_tree_applies_further_series_in_order(upstream, tmp_path):
    project, base, mbox = upstream
    other = second_series(project, base, tmp_path, 'mm/baz.c', 'baz\n', 'mm/baz: add baz')
    before = snapshot(project)
    dest = tmp_path / 'wt'
    tree = prepare_tree(project, dest, BRANCH, str(mbox), 2, identity=AGENT, also=[(str(other), 1)])
    assert subjects(dest, f'{base}..HEAD') == [
        'mm/foo: name line three',
        'mm/bar: add bar',
        'mm/baz: add baz',
    ]
    assert tree.applied == git('rev-parse', 'HEAD', cwd=dest)  # All of it counts as series.
    assert (dest / 'mm/baz.c').read_text() == 'baz\n'
    assert git('symbolic-ref', '--short', 'HEAD', cwd=dest) == BRANCH
    assert snapshot(project) == before


def test_a_further_series_that_does_not_apply_fails_the_whole_tree(upstream, tmp_path):
    project, base, mbox = upstream
    clash = second_series(
        project,
        base,
        tmp_path,
        'mm/foo.c',
        (project / 'mm/foo.c').read_text().replace('line 3\n', 'tres\n'),
        'mm/foo: clash with line three',
        name='clash',
    )
    dest = tmp_path / 'wt'
    with pytest.raises(PrepareError) as raised:
        prepare_tree(project, dest, BRANCH, str(mbox), 2, identity=AGENT, also=[(str(clash), 1)])
    assert 'clash' in str(raised.value) or 'clash' in raised.value.output
    assert not dest.exists()


def test_a_conflict_says_where_the_series_would_have_applied(upstream, tmp_path):
    project, _base, mbox = upstream
    write(project, 'mm/foo.c', (project / 'mm/foo.c').read_text().replace('line 3\n', 'tres\n'))
    head = commit(project, 'mm/foo: conflicting change')
    with pytest.raises(PrepareError) as raised:
        prepare_tree(project, tmp_path / 'wt', BRANCH, str(mbox), 2, base='HEAD', identity=AGENT)
    assert raised.value.base == head  # Where an agent can apply it by hand.


def test_an_unapplied_tree_leaves_the_series_to_the_agent(upstream, tmp_path):
    from b4_maintainer.adapters.sessiontree import prepare_unapplied_tree, series_mbox
    from b4_maintainer.sessions.trees import series_tip

    project, base, mbox = upstream
    before = snapshot(project)
    dest = tmp_path / 'var' / 'worktrees' / 's1'
    ambytes = series_mbox(project, str(mbox), 2)
    tree = prepare_unapplied_tree(project, dest, BRANCH, ambytes, base, AGENT)
    assert tree.agent_applies and tree.base == base == tree.applied
    assert git('rev-parse', 'HEAD', cwd=dest) == base
    assert git('symbolic-ref', '--short', 'HEAD', cwd=dest) == BRANCH
    assert (dest / '.git' / 'b4m' / 'series.mbx').read_bytes() == ambytes
    assert git('status', '--porcelain', cwd=dest) == ''  # Not in the working tree.
    assert snapshot(project) == before
    # The agent applies it on the session branch: that is the series from then on.
    git('am', '-q', '-3', str(dest / '.git' / 'b4m' / 'series.mbx'), cwd=dest)
    assert series_tip(dest, tree) == git('rev-parse', 'HEAD', cwd=dest)
    classed = classify_tree(dest, tree)
    assert [c.subject for c in classed.series] == ['mm/foo: name line three', 'mm/bar: add bar']
    assert [b.commits for b in classed.branches if b.name == BRANCH] == [()]


def fix_branch(project, base, name='me/fix', path='mm/fix.c'):
    git('branch', name, base, cwd=project)
    git('switch', '-q', name, cwd=project)
    write(project, path, 'fixed\n')
    git(
        '-c',
        'user.name=Fixer',
        '-c',
        'user.email=fixer@example.org',
        'commit',
        '-q',
        '-m',
        'mm: fix it',
        cwd=project,
    )
    git('switch', '-q', 'master', cwd=project)


def test_a_workbench_tree_applies_series_and_branches_in_order(upstream, tmp_path):
    from b4_maintainer.adapters.sessiontree import prepare_items_tree
    from b4_maintainer.sessions.trees import TreeItem

    project, base, mbox = upstream
    fix_branch(project, base)
    before = snapshot(project)
    dest = tmp_path / 'var' / 'worktrees' / 'w1' / 'demo'
    items = (TreeItem('mm: foo and bar v2', str(mbox), 2), TreeItem('me/fix', branch='me/fix'))
    tree = prepare_items_tree(project, dest, BRANCH, base, items, AGENT)
    assert (tree.base, tree.applied) == (base, git('rev-parse', 'HEAD', cwd=dest))
    assert subjects(dest, f'{base}..HEAD') == [
        'mm/foo: name line three',
        'mm/bar: add bar',
        'mm: fix it',
    ]
    assert git('log', '-1', '--format=%an', 'HEAD', cwd=dest) == 'Fixer'  # Its author kept.
    assert git('symbolic-ref', '--short', 'HEAD', cwd=dest) == BRANCH
    assert git('config', 'user.email', cwd=dest) == AGENT[1]
    assert snapshot(project) == before


def test_a_branch_from_another_sessions_tree_is_fetched_in(upstream, tmp_path):
    from b4_maintainer.adapters.sessiontree import prepare_items_tree, prepare_start_tree
    from b4_maintainer.sessions.trees import TreeItem

    project, base, _mbox = upstream
    other = prepare_start_tree(
        project, tmp_path / 'var' / 'worktrees' / 's9', 'b4m/session/s9', base, AGENT
    )
    git('switch', '-q', '-c', 'b4m/fixup/y', cwd=other.path)
    write(pathlib.Path(other.path), 'mm/y.c', 'y\n')
    git('commit', '-q', '-m', 'mm: y', cwd=other.path)
    item = TreeItem('s9: b4m/fixup/y', branch='b4m/fixup/y', repo=other.path)
    dest = tmp_path / 'var' / 'worktrees' / 'w1' / 'demo'
    tree = prepare_items_tree(project, dest, BRANCH, base, (item,), AGENT)
    assert git('log', '-1', '--format=%s', tree.applied, cwd=dest) == 'mm: y'


def test_a_conflicting_item_leaves_nothing_and_says_where_an_agent_starts(upstream, tmp_path):
    from b4_maintainer.adapters.sessiontree import items_mbox, prepare_items_tree
    from b4_maintainer.sessions.trees import TreeItem

    project, base, mbox = upstream
    fix_branch(project, base, path='mm/foo.c')  # Rewrites the file the series changes.
    items = (TreeItem('mm: foo and bar v2', str(mbox), 2), TreeItem('me/fix', branch='me/fix'))
    dest = tmp_path / 'var' / 'worktrees' / 'w1' / 'demo'
    with pytest.raises(PrepareError) as raised:
        prepare_items_tree(project, dest, BRANCH, base, items, AGENT)
    assert 'me/fix' in str(raised.value) and raised.value.base == base
    assert not dest.exists()
    # The handoff: every item, in order, as one mbox for git am.
    text = items_mbox(project, base, items).decode()
    assert text.index('mm/foo: name line three') < text.index('mm: fix it')


def test_a_single_patch_and_a_single_commit_apply_alone(upstream, tmp_path):
    from b4_maintainer.adapters.sessiontree import items_mbox, prepare_items_tree
    from b4_maintainer.sessions.trees import TreeItem

    project, base, mbox = upstream
    fix_branch(project, base)
    git('switch', '-q', 'me/fix', cwd=project)
    write(project, 'mm/other.c', 'other\n')
    single = commit(project, 'mm: other fix')  # The tip alone, not the fix before it.
    git('switch', '-q', 'master', cwd=project)
    items = (
        TreeItem('mm: foo and bar v2 patch 2', str(mbox), 2, patches=(2,)),
        TreeItem('mm: other fix', branch=single, single=True),
    )
    dest = tmp_path / 'var' / 'worktrees' / 'w1' / 'demo'
    prepare_items_tree(project, dest, BRANCH, base, items, AGENT)
    assert subjects(dest, f'{base}..HEAD') == ['mm/bar: add bar', 'mm: other fix']
    text = items_mbox(project, base, items).decode()
    assert 'mm/bar: add bar' in text and 'mm: other fix' in text
    assert 'name line three' not in text and 'mm: fix it' not in text


# Workbench spaces: one shared repository per project, a checkout per session.


def space_paths(tmp_path, project='demo'):
    space = tmp_path / 'var' / 'workbenches' / 'w'
    return space / 'repos' / f'{project}.git', space / 'sessions'


def test_sessions_share_a_space_repo_and_the_space_branch_is_prepared_once(upstream, tmp_path):
    from b4_maintainer.adapters.sessiontree import prepare_space_tree
    from b4_maintainer.sessions.trees import TreeItem

    project, base, mbox = upstream
    before = snapshot(project)
    repo, sessions = space_paths(tmp_path)
    items = (TreeItem('mm: foo and bar v2', str(mbox), 2),)
    one = ('agent (one)', AGENT[1])
    tree, prepared = prepare_space_tree(
        project, repo, sessions / 's1' / 'demo', 'b4m/session/s1', 'b4m/bench/w', base, items, one
    )
    assert prepared and (tree.base, tree.branch) == (base, 'b4m/session/s1')
    dest1 = pathlib.Path(tree.path)
    assert subjects(dest1, f'{base}..HEAD') == ['mm/foo: name line three', 'mm/bar: add bar']
    assert git('rev-parse', 'b4m/bench/w', cwd=repo) == tree.applied
    assert snapshot(project) == before  # The project is never touched.
    assert git('for-each-ref', '--format=%(refname:short)', 'refs/heads', cwd=repo).split() == [
        'b4m/bench/w',
        'b4m/session/s1',
    ]  # The project's own branches are not copied in.

    two = ('agent (two)', AGENT[1])
    tree2, prepared = prepare_space_tree(
        project, repo, sessions / 's2' / 'demo', 'b4m/session/s2', 'b4m/bench/w', base, items, two
    )
    assert not prepared and (tree2.base, tree2.applied) == (base, tree.applied)
    dest2 = pathlib.Path(tree2.path)
    assert git('config', 'user.name', cwd=dest1) == 'agent (one)'  # Each its own identity.
    assert git('config', 'user.name', cwd=dest2) == 'agent (two)'

    git('switch', '-q', '-c', 'b4m/fixup/x', cwd=dest1)
    write(dest1, 'mm/x.c', 'x\n')
    commit(dest1, 'mm: x')
    assert git('branch', '--list', 'b4m/fixup/x', cwd=dest2).strip()  # Shared at once.


def test_removing_a_session_checkout_keeps_every_branch(upstream, tmp_path):
    from b4_maintainer.adapters.sessiontree import prepare_space_tree, remove_space_tree
    from b4_maintainer.sessions.trees import TreeItem, inspect_tree

    project, base, mbox = upstream
    repo, sessions = space_paths(tmp_path)
    items = (TreeItem('mm: foo and bar v2', str(mbox), 2),)
    trees = [
        prepare_space_tree(
            project,
            repo,
            sessions / s / 'demo',
            f'b4m/session/{s}',
            'b4m/bench/w',
            base,
            items,
            AGENT,
        )[0]
        for s in ('s1', 's2')
    ]
    dest1 = pathlib.Path(trees[0].path)
    git('switch', '-q', '-c', 'b4m/fixup/x', cwd=dest1)
    write(dest1, 'mm/x.c', 'x\n')
    commit(dest1, 'mm: x')
    write(dest1, 'mm/y.c', 'uncommitted\n')
    state = inspect_tree(dest1, trees[0].applied, trees[0])
    assert state.changed and not state.commits  # Commits stay in the space: nothing lost.
    remove_space_tree(dest1)
    assert not dest1.exists() and pathlib.Path(trees[1].path).is_dir()  # Only its own.
    heads = git('for-each-ref', '--format=%(refname:short)', 'refs/heads', cwd=repo).split()
    assert {'b4m/fixup/x', 'b4m/session/s1', 'b4m/session/s2', 'b4m/bench/w'} <= set(heads)


def test_a_space_tree_that_does_not_apply_leaves_no_branch_and_no_checkout(upstream, tmp_path):
    from b4_maintainer.adapters.sessiontree import prepare_space_tree
    from b4_maintainer.sessions.trees import TreeItem

    project, base, mbox = upstream
    fix_branch(project, base, path='mm/foo.c')  # Rewrites the file the series changes.
    repo, sessions = space_paths(tmp_path)
    items = (TreeItem('mm: foo and bar v2', str(mbox), 2), TreeItem('me/fix', branch='me/fix'))
    dest = sessions / 's1' / 'demo'
    with pytest.raises(PrepareError) as raised:
        prepare_space_tree(project, repo, dest, 'b4m/session/s1', 'b4m/bench/w', base, items, AGENT)
    assert raised.value.base == base and not dest.exists()
    assert git('for-each-ref', 'refs/heads', cwd=repo) == ''


def test_a_space_tree_for_the_agent_has_its_mbox_where_the_prompt_says(upstream, tmp_path):
    from b4_maintainer.adapters.sessiontree import prepare_space_tree
    from b4_maintainer.sessions.trees import TreeItem, agent_mbox

    project, base, mbox = upstream
    repo, sessions = space_paths(tmp_path)
    items = (TreeItem('mm: foo and bar v2', str(mbox), 2),)
    tree, _prepared = prepare_space_tree(
        project,
        repo,
        sessions / 's1' / 'demo',
        'b4m/session/s1',
        'b4m/bench/w',
        base,
        items,
        AGENT,
        use='deliver',
    )
    assert tree.agent_applies and git('rev-parse', 'HEAD', cwd=tree.path) == base
    assert 'mm/foo: name line three' in agent_mbox(pathlib.Path(tree.path)).read_text()
    assert git('status', '--porcelain', cwd=tree.path) == ''  # Not in the tree's files.
    assert git('for-each-ref', '--format=%(refname:short)', 'refs/heads', cwd=repo).split() == [
        'b4m/session/s1'
    ]  # The space branch is only made from items applied.


def test_each_branch_is_listed_once_by_the_checkout_that_made_it_else_by_the_space(
    upstream, tmp_path
):
    from b4_maintainer.adapters.sessiontree import prepare_space_tree, remove_space_tree
    from b4_maintainer.sessions.records import Worktree
    from b4_maintainer.sessions.trees import TreeItem, classify_tree

    project, base, mbox = upstream
    repo, sessions = space_paths(tmp_path)
    items = (TreeItem('mm: foo and bar v2', str(mbox), 2),)
    trees = {
        s: prepare_space_tree(
            project,
            repo,
            sessions / s / 'demo',
            f'b4m/session/{s}',
            'b4m/bench/w',
            base,
            items,
            AGENT,
        )[0]
        for s in ('s1', 's2')
    }
    for s, name in (('s1', 'b4m/fixup/a'), ('s2', 'b4m/fixup/b')):
        dest = pathlib.Path(trees[s].path)
        git('switch', '-q', '-c', name, cwd=dest)
        write(dest, f'mm/{s}.c', f'{s}\n')
        commit(dest, f'mm: {s}')
    space = Worktree(str(repo), 'b4m/bench/w', base, trees['s1'].applied)

    def listed(path, tree):
        return sorted(b.name for b in classify_tree(pathlib.Path(path), tree).branches)

    assert listed(trees['s1'].path, trees['s1']) == ['b4m/fixup/a', 'b4m/session/s1']
    assert listed(trees['s2'].path, trees['s2']) == ['b4m/fixup/b', 'b4m/session/s2']
    assert listed(repo, space) == ['b4m/bench/w']
    remove_space_tree(pathlib.Path(trees['s1'].path))
    # Its fixup moves to the space; its session branch held nothing of its own.
    assert listed(repo, space) == ['b4m/bench/w', 'b4m/fixup/a']


def test_the_space_branch_takes_new_items_on_top_or_is_rebuilt(upstream, tmp_path):
    from b4_maintainer.adapters.sessiontree import prepare_space_tree, update_space_branch
    from b4_maintainer.sessions.trees import TreeItem

    project, base, mbox = upstream
    fix_branch(project, base)
    repo, sessions = space_paths(tmp_path)
    series = TreeItem('mm: foo and bar v2', str(mbox), 2)
    fix = TreeItem('me/fix', branch='me/fix')
    tree, _ = prepare_space_tree(
        project,
        repo,
        sessions / 's1' / 'demo',
        'b4m/session/s1',
        'b4m/bench/w',
        base,
        (series,),
        AGENT,
    )
    tip = update_space_branch(project, repo, 'b4m/bench/w', base, (fix,))  # Added: on top.
    assert subjects(repo, f'{base}..{tip}') == [
        'mm/foo: name line three',
        'mm/bar: add bar',
        'mm: fix it',
    ]
    assert git('rev-parse', 'b4m/bench/w', cwd=repo) == tip
    assert git('rev-parse', 'HEAD', cwd=tree.path) == tree.applied  # A running one keeps its.
    tip = update_space_branch(project, repo, 'b4m/bench/w', base, (fix, series), rebuild=True)
    assert subjects(repo, f'{base}..{tip}') == [
        'mm: fix it',
        'mm/foo: name line three',
        'mm/bar: add bar',
    ]
    assert not list((repo.parent.parent / 'sessions').glob('.update-*'))  # Nothing left.


def test_a_space_checkouts_extra_checkouts_count_and_go_with_it(upstream, tmp_path):
    from b4_maintainer.adapters.sessiontree import prepare_space_tree, remove_space_tree
    from b4_maintainer.sessions.trees import TreeItem, inspect_tree

    project, base, mbox = upstream
    repo, sessions = space_paths(tmp_path)
    items = (TreeItem('mm: foo and bar v2', str(mbox), 2),)
    mine, theirs = (
        prepare_space_tree(
            project,
            repo,
            sessions / s / 'demo',
            f'b4m/session/{s}',
            'b4m/bench/w',
            base,
            items,
            AGENT,
        )[0]
        for s in ('s1', 's2')
    )
    out = tmp_path / 'var' / 'sessions'
    extra = out / 's1' / 'work' / 'out' / 'worktrees' / 'at-base'  # Per the b4m-branches skill.
    git('worktree', 'add', '-q', '--detach', str(extra), base, cwd=mine.path)
    other = out / 's2' / 'work' / 'out' / 'worktrees' / 'theirs'
    git('worktree', 'add', '-q', '--detach', str(other), base, cwd=theirs.path)
    write(extra, 'scratch.c', 'x\n')
    state = inspect_tree(pathlib.Path(mine.path), mine.applied, mine)
    assert state.extras == (str(extra),) and any('scratch.c' in c for c in state.changed)
    remove_space_tree(pathlib.Path(mine.path))
    assert not extra.exists() and other.is_dir()  # Its own extra goes; another's stays.

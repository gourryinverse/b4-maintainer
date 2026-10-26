"""Contract: plucking copies chosen commits onto a maintainer base: an agent's commits
become the maintainer's with an Assisted-by trailer, anyone else's keep their author and
message; a conflict or a moved base changes nothing; the checkout is never touched."""

import pytest
from gitutil import git
from test_bases import BASE, commit, project, snapshot, upstream_commit  # noqa: F401

from b4_maintainer.adapters.bases import pluck

AGENT = 'agent@b4-maintainer.invalid'


@pytest.fixture
def tree(project, tmp_path):  # noqa: F811
    """A session tree (shared clone) with an agent's pull-ahead and a posted patch."""
    dest = tmp_path / 'wt'
    git('clone', '-q', '--shared', str(project), str(dest), cwd=tmp_path)
    git('switch', '-q', '-c', 'b4m/pull-ahead/mm', 'origin/me/base', cwd=dest)
    commit(
        dest,
        'mm.c',
        (dest / 'mm.c').read_text().replace('line 4\n', 'line four\n'),
        'mm: fix line four\n\nIt was wrong.\n\nFixes: 0123456789ab ("mm: start")',
        f'b4-maintainer agent (anthropic/model-x) <{AGENT}>',
    )
    git('switch', '-q', '-c', 'posted', 'origin/me/base', cwd=dest)
    commit(
        dest,
        'c.c',
        'c\n',
        'mm: posted change\n\nBody.\n\nSigned-off-by: Contributor Three <three@example.org>',
        'Contributor Three <three@example.org>',
    )
    return dest


def sha(repo, rev):
    return git('rev-parse', rev, cwd=repo)


def test_an_agent_commit_becomes_the_maintainers_with_assisted_by(project, tree):  # noqa: F811
    before = snapshot(project)
    old = sha(project, 'me/base')
    result = pluck(
        project, BASE, tree, [sha(tree, 'b4m/pull-ahead/mm')], old, AGENT, 'anthropic/model-x'
    )
    assert result.ok and len(result.picked) == 1
    assert sha(project, 'me/base') == result.tip
    assert git('log', '-1', '--format=%an <%ae>', 'me/base', cwd=project) == (
        'Example Maintainer <maintainer@example.org>'
    )
    body = git('log', '-1', '--format=%B', 'me/base', cwd=project)
    assert body.startswith('mm: fix line four\n\nIt was wrong.')
    assert body.splitlines()[-2:] == [
        'Fixes: 0123456789ab ("mm: start")',
        'Assisted-by: anthropic/model-x',
    ]
    assert 'line four' in git('show', 'me/base:mm.c', cwd=project)
    assert git('rev-parse', 'me/base~1', cwd=project) == old
    assert snapshot(project) == before  # The checkout was not touched.
    assert git('for-each-ref', 'refs/b4m', cwd=project) == ''


def test_someone_elses_commit_keeps_its_author_and_message(project, tree):  # noqa: F811
    posted = sha(tree, 'posted')
    result = pluck(project, BASE, tree, [posted], sha(project, 'me/base'), AGENT, 'x')
    assert result.ok
    assert git('log', '-1', '--format=%an <%ae>%n%ad', 'me/base', cwd=project) == git(
        'log', '-1', '--format=%an <%ae>%n%ad', posted, cwd=tree
    )
    assert git('log', '-1', '--format=%B', 'me/base', cwd=project) == git(
        'log', '-1', '--format=%B', posted, cwd=tree
    )


def test_several_commits_go_on_in_order(project, tree):  # noqa: F811
    shas = [sha(tree, 'b4m/pull-ahead/mm'), sha(tree, 'posted')]
    result = pluck(project, BASE, tree, shas, sha(project, 'me/base'), AGENT, 'x')
    assert result.ok and len(result.picked) == 2
    assert git('log', '--format=%s', '-2', 'me/base', cwd=project).splitlines() == [
        'mm: posted change',
        'mm: fix line four',
    ]


def test_a_conflict_or_a_moved_base_changes_nothing(project, tree):  # noqa: F811
    old = sha(project, 'me/base')
    git('switch', '-q', 'me/base', cwd=project)
    commit(project, 'mm.c', (project / 'mm.c').read_text().replace('line 4\n', 'four\n'), 'mm: 4')
    git('switch', '-q', 'main', cwd=project)
    moved = sha(project, 'me/base')
    stale = pluck(project, BASE, tree, [sha(tree, 'b4m/pull-ahead/mm')], old, AGENT, 'x')
    assert not stale.ok and 'moved' in stale.error
    conflict = pluck(project, BASE, tree, [sha(tree, 'b4m/pull-ahead/mm')], moved, AGENT, 'x')
    assert not conflict.ok and conflict.failed == 'mm: fix line four'
    assert 'CONFLICT' in conflict.detail and 'mm.c' in conflict.detail
    assert sha(project, 'me/base') == moved
    assert git('for-each-ref', 'refs/b4m', cwd=project) == ''


def test_a_change_already_on_the_base_is_dropped(project, tree):  # noqa: F811
    posted = sha(tree, 'posted')
    first = pluck(project, BASE, tree, [posted], sha(project, 'me/base'), AGENT, 'x')
    again = pluck(project, BASE, tree, [posted], first.tip, AGENT, 'x')
    assert again.ok and again.dropped == ('mm: posted change',) and again.picked == ()
    assert sha(project, 'me/base') == first.tip


def test_a_checked_out_base_is_not_changed(project, tree, tmp_path):  # noqa: F811
    git('worktree', 'add', '-q', str(tmp_path / 'co'), 'me/base', cwd=project)
    old = sha(project, 'me/base')
    result = pluck(project, BASE, tree, [sha(tree, 'posted')], old, AGENT, 'x')
    assert not result.ok and 'checked out' in result.error

"""Contract: the Branches screen's data: each project's maintainer bases, session trees,
and prep branches, with their commits and artifacts; what a commit or artifact shows."""

import pytest
from gitutil import git
from sessiondata import make_record

from b4_maintainer.branches import BranchService
from b4_maintainer.sessions.records import Worktree
from b4_maintainer.sessions.store import SessionStore
from b4_maintainer.workspace import Workspace

ME = ['-c', 'user.name=Example Maintainer', '-c', 'user.email=maintainer@example.org']


def commit(repo, path, text, message):
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text(text)
    git('add', path, cwd=repo)
    git(*ME, 'commit', '-q', '-m', message, cwd=repo)
    return git('rev-parse', 'HEAD', cwd=repo)


@pytest.fixture
def ws(tmp_path, home):
    (home / '.gitconfig').write_text(
        '[user]\n\tname = Example Maintainer\n\temail = m@example.org\n'
    )
    root = tmp_path / 'ws'
    project = root / 'projects' / 'linux'
    project.mkdir(parents=True)
    git('init', '-q', '-b', 'main', cwd=project)
    base = commit(project, 'mm.c', 'mm\n', 'mm: start')
    git('update-ref', 'refs/remotes/up/stable', base, cwd=project)
    git('switch', '-q', '-c', 'me/base', cwd=project)
    commit(project, 'a.c', 'a\n', 'mm: pulled ahead fix')
    git('switch', '-q', '-c', 'b4/pull-aheads', 'up/stable', cwd=project)
    git(*ME, 'commit', '-q', '--allow-empty', '-m', 'pull aheads cover', cwd=project)
    commit(project, 'p.c', 'p\n', 'mm: fix p')
    git('switch', '-q', '-c', 'b4/review/20261020-foo', 'up/stable', cwd=project)
    commit(project, 'r.c', 'r\n', 'mm: under review')
    git('switch', '-q', 'main', cwd=project)
    # A session tree: the series applied on the base, and an agent's pull-ahead.
    tree = root / 'var' / 'worktrees' / 's1'
    git('clone', '-q', '--shared', str(project), str(tree), cwd=root)
    git('switch', '-q', '-c', 'b4m/session/s1', base, cwd=tree)
    commit(tree, 'foo.c', 'foo\n', 'mm/foo: the series patch')
    applied = git('rev-parse', 'HEAD', cwd=tree)
    git('switch', '-q', '-c', 'b4m/pull-ahead/leak', base, cwd=tree)
    commit(tree, 'leak.c', 'fixed\n', 'mm: fix the leak')
    git('switch', '-q', 'b4m/session/s1', cwd=tree)
    store = SessionStore(root / 'var' / 'sessions')
    record = make_record(
        's1', mode='worktree', worktree=Worktree(str(tree), 'b4m/session/s1', base, applied)
    )
    store.create(record)
    out = store.directory('s1') / 'work' / 'out'
    (out / 'tests').mkdir(parents=True)
    (out / 'tests' / 'run.log').write_text('all 3 tests pass\n')
    (out / 'artifacts.toml').write_text(
        '[[artifact]]\nkind = "pull-ahead"\ntitle = "mm: fix the leak"\n'
        'summary = "It leaked."\nbranch = "b4m/pull-ahead/leak"\n\n'
        '[[artifact]]\nkind = "results"\ntitle = "test run"\nsummary = "All pass."\n'
        'path = "out/tests/run.log"\n'
    )
    workspace = Workspace.from_config(
        {
            'projects': [
                {
                    'id': 'linux',
                    'path': 'projects/linux',
                    'bases': [{'branch': 'me/base', 'upstream': 'up/stable'}],
                }
            ]
        }
    )
    service = BranchService(root, lambda: [store.record('s1')], store.directory)
    return service, workspace, project


def test_each_project_lists_its_bases_trees_and_prep_branches(ws):
    service, workspace, _project = ws
    found = service.snapshot(workspace)
    kinds = [(i.kind, i.key) for i in found.items]
    assert kinds == [
        ('base', 'base:linux:me/base'),
        ('tree', 'tree:s1'),
        ('prep', 'prep:linux:b4/pull-aheads'),  # Not b4's review branches.
    ]
    base, tree, prep = found.items
    assert 'me/base on up/stable' in base.label and '1 commit' in base.label
    assert 'mm: fix foo leak' in tree.label and '1 pull-ahead' in tree.label
    assert 'b4/pull-aheads' in prep.label


def test_details_show_commits_by_class_and_the_artifacts(ws):
    service, workspace, _project = ws
    found = service.snapshot(workspace)
    base_rows = [r.text for r in found.details['base:linux:me/base']]
    assert any('mm: pulled ahead fix' in t for t in base_rows)
    tree_rows = found.details['tree:s1']
    texts = [r.text for r in tree_rows]
    assert texts.index('Series') < texts.index('pull-ahead b4m/pull-ahead/leak')
    assert any('mm/foo: the series patch' in t for t in texts)
    assert any('mm: fix the leak' in t for t in texts)
    assert 'Artifacts' in texts and any('results: test run' in t for t in texts)
    prep_rows = [r.text for r in found.details['prep:linux:b4/pull-aheads']]
    assert any('mm: fix p' in t for t in prep_rows)


def test_a_commit_shows_its_patch_and_an_artifact_its_file(ws):
    service, workspace, _project = ws
    found = service.snapshot(workspace)
    rows = {r.text.split('  ', 1)[-1]: r for r in found.details['tree:s1'] if r.key}
    fix = next(r for t, r in rows.items() if 'mm: fix the leak' in t)
    shown = service.show(fix)
    assert 'mm: fix the leak' in shown and '+fixed' in shown
    results = next(r for r in found.details['tree:s1'] if 'results: test run' in r.text)
    shown = service.show(results)
    assert 'All pass.' in shown and 'all 3 tests pass' in shown


def test_a_base_rebases_from_here(ws):
    service, workspace, project = ws
    git('switch', '-q', '--detach', 'up/stable', cwd=project)
    tip = commit(project, 'u.c', 'u\n', 'mm: upstream moved')
    git('update-ref', 'refs/remotes/up/stable', tip, cwd=project)
    git('switch', '-q', 'main', cwd=project)
    found = service.snapshot(workspace)
    assert '1 behind' in found.items[0].label
    result = service.rebase(
        workspace, 'base:linux:me/base', found.statuses['base:linux:me/base'].tip
    )
    assert result.ok
    assert '1 behind' not in service.snapshot(workspace).items[0].label

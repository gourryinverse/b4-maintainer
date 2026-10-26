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
    kinds = [(i.kind, i.key) for i in found.items if i.kind in ('base', 'tree', 'prep')]
    assert kinds == [
        ('base', 'base:linux:me/base'),
        ('tree', 'tree:s1'),
        ('prep', 'prep:linux:b4/pull-aheads'),  # Not b4's review branches.
    ]
    by_key = {i.key: i for i in found.items}
    base, tree = by_key['base:linux:me/base'], by_key['tree:s1']
    assert 'me/base on up/stable' in base.label and '1 commit' in base.label
    assert 'mm: fix foo leak' in tree.label and '1 pull-ahead' in tree.label
    assert 'b4/pull-aheads' in by_key['prep:linux:b4/pull-aheads'].label


def test_worktrees_review_branches_and_other_local_work_are_listed(ws, tmp_path):
    service, workspace, project = ws
    (project / 'scratch.txt').write_text('uncommitted\n')  # The main checkout has changes.
    topic = tmp_path / 'topic'
    git('worktree', 'add', '-q', '-b', 'topic', str(topic), 'up/stable', cwd=project)
    commit(topic, 't.c', 't\n', 'mm: topic work')
    git('branch', 'experiment', 'up/stable', cwd=project)
    git('switch', '-q', 'experiment', cwd=project)
    commit(project, 'e.c', 'e\n', 'mm: forgotten experiment')
    git('switch', '-q', 'main', cwd=project)
    git('branch', 'merged', 'up/stable', cwd=project)  # Nothing beyond the remotes.
    before = (project / '.git' / 'index').read_bytes()
    found = service.snapshot(workspace)
    order = [i.kind for i in found.items]
    assert order == ['base', 'worktree', 'worktree', 'tree', 'prep', 'review', 'branch']
    by_key = {i.key: i for i in found.items}
    main = by_key[f'worktree:linux:{project}']
    assert 'main' in main.label and '1 changed' in main.label
    assert '?? scratch.txt' in [r.text.strip() for r in found.details[main.key]]
    linked = by_key[f'worktree:linux:{topic}']
    assert 'topic' in linked.label and 'clean' in linked.label
    assert any('mm: topic work' in r.text for r in found.details[linked.key])
    review = by_key['review:linux:b4/review/20261020-foo']
    assert any('mm: under review' in r.text for r in found.details[review.key])
    # Other branches: with commits not upstream, not checked out, not a base or b4's
    # (main and topic are checked out; merged has nothing beyond the remotes).
    others = [i.key for i in found.items if i.kind == 'branch']
    assert others == ['branch:linux:experiment']
    assert '1 commit not upstream' in by_key['branch:linux:experiment'].label
    assert (project / '.git' / 'index').read_bytes() == before  # Looking never writes.


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


def test_a_trees_extra_checkouts_are_listed_under_it(ws, tmp_path):
    service, workspace, _project = ws
    tree = tmp_path / 'ws' / 'var' / 'worktrees' / 's1'
    extra = tmp_path / 'ws' / 'var' / 'sessions' / 's1' / 'work' / 'out' / 'worktrees' / 'base'
    git('worktree', 'add', '-q', '--detach', str(extra), 'HEAD~1', cwd=tree)
    found = service.snapshot(workspace)
    texts = [r.text for r in found.details['tree:s1']]
    assert 'Extra checkouts' in texts
    assert any(str(extra) in t and 'mm: start' in t for t in texts)
    assert '1 extra checkout' in {i.key: i for i in found.items}['tree:s1'].label


@pytest.fixture
def rebase_ws(ws, tmp_path):
    """The same workspace, with a real hub (stand-in runner) and an upstream move that
    conflicts with the base's commit."""
    from sessiondata import FakeSessionRunner

    from b4_maintainer.sessions.hub import RecordWorktrees, SessionHub

    _service, workspace, project = ws
    git('switch', '-q', '--detach', 'up/stable', cwd=project)
    tip = commit(project, 'a.c', 'upstream a\n', 'mm: upstream a')
    git('update-ref', 'refs/remotes/up/stable', tip, cwd=project)
    git('switch', '-q', 'main', cwd=project)
    root = tmp_path / 'ws'
    store = SessionStore(root / 'var' / 'sessions')
    runner = FakeSessionRunner(store)
    trees = RecordWorktrees(root / 'var' / 'worktrees', {'linux': str(project)})
    hub = SessionHub(store, runner, trees, catalog=lambda: None, sandbox=lambda: True)
    service = BranchService(root, hub.records, store.directory, hub)
    return service, workspace, project, hub, runner


def test_a_failed_rebase_launches_a_rebase_session_on_a_copy(rebase_ws):
    service, workspace, project, hub, runner = rebase_ws
    key = 'base:linux:me/base'
    old = git('rev-parse', 'me/base', cwd=project)
    failed = service.rebase(workspace, key, old)
    assert not failed.ok and failed.failed is not None
    record = service.rebase_session(workspace, key, failed)
    assert record.scope == 'rebase' and record.choices['expected'] == old
    [(sid, request)] = runner.launches
    assert sid == record.id and 'a.c' in request.prompt  # The conflict, from merge-tree.
    tree = record.worktree
    assert tree is not None
    assert git('rev-parse', 'HEAD', cwd=tree.path) == old
    assert git('rev-parse', 'me/base', cwd=project) == old  # Nothing changed yet.
    rows = service.snapshot(workspace).details[f'tree:{record.id}']
    assert any('No result yet' in r.text for r in rows)


def test_a_rebase_sessions_result_is_shown_and_accepted(rebase_ws):
    service, workspace, project, hub, runner = rebase_ws
    key = 'base:linux:me/base'
    old = git('rev-parse', 'me/base', cwd=project)
    record = service.rebase_session(workspace, key, service.rebase(workspace, key, old))
    assert record.worktree is not None
    dest = __import__('pathlib').Path(record.worktree.path)
    # The agent: rebase on b4m/rebased, resolving a.c.
    git('switch', '-q', '-c', 'b4m/rebased', cwd=dest)
    run = __import__('subprocess').run(
        ['git', 'rebase', '-q', 'up/stable'], cwd=dest, capture_output=True
    )
    assert run.returncode != 0
    (dest / 'a.c').write_text('upstream a\na\n')
    git('add', 'a.c', cwd=dest)
    git('-c', 'core.editor=true', 'rebase', '--continue', cwd=dest)
    found = service.snapshot(workspace)
    item = f'tree:{record.id}'
    assert found.rebases[item] == record.id
    rows = found.details[item]
    assert any('mm: pulled ahead fix' in r.text and r.key for r in rows)
    diff = next(r for r in rows if r.text.startswith('Range diff'))
    assert 'mm: pulled ahead fix' in service.show(diff)
    result = service.accept(workspace, record.id)
    assert result.ok
    assert git('rev-parse', 'me/base', cwd=project) == git('rev-parse', 'b4m/rebased', cwd=dest)
    assert service.accept(workspace, record.id).error  # Not twice: the base moved.


def test_marked_commits_are_plucked_onto_the_base(ws):
    service, workspace, project = ws
    found = service.snapshot(workspace)
    row = next(r for r in found.details['tree:s1'] if 'mm: fix the leak' in r.text)
    old = git('rev-parse', 'me/base', cwd=project)
    result = service.pluck(workspace, 'tree:s1', [row], 'base:linux:me/base', old)
    assert result.ok and len(result.picked) == 1
    assert git('log', '-1', '--format=%s', 'me/base', cwd=project) == 'mm: fix the leak'
    assert git('rev-parse', 'me/base~1', cwd=project) == old
    # The fixture's commit is the maintainer's own, so its message is kept as is.
    assert 'Assisted-by' not in git('log', '-1', '--format=%B', 'me/base', cwd=project)


def test_plucking_onto_a_base_names_its_targets(ws):
    service, workspace, _project = ws
    assert service.targets(workspace, 'tree:s1') == ['base:linux:me/base']
    assert service.targets(workspace, 'base:linux:me/base') == []  # Not onto itself.


def test_a_prep_branch_is_cut_from_marked_commits_with_a_drafted_cover(ws, tmp_path):
    service, workspace, project = ws
    found = service.snapshot(workspace)
    row = next(r for r in found.details['tree:s1'] if 'mm: fix the leak' in r.text)
    assert row.branch == 'b4m/pull-ahead/leak'
    assert 'up/stable' in service.fork_points(workspace, 'tree:s1')
    result = service.cut_prep(workspace, 'tree:s1', [row], 'leak-fix', 'up/stable')
    assert result.ok, result.error
    assert result.branch == 'b4/leak-fix'
    assert (tmp_path / 'ws' / 'var' / 'branches' / 'linux' / 'leak-fix' / '.git').exists()
    found = service.snapshot(workspace)
    key = 'prep:linux:b4/leak-fix'
    rows = found.details[key]
    cover = next(r for r in rows if r.text.startswith('Cover letter'))
    text = service.show(cover)
    assert text.startswith('EDITME:') and 'It leaked.' in text  # The artifact's summary.
    assert [r.text.split('  ', 2)[-1] for r in rows if r.key and r is not cover] == [
        'mm: fix the leak'
    ]
    code, info = service.prep_run(workspace, key, 'info')
    assert code == 0 and 'branch: b4/leak-fix' in info
    # Prep branches are pluck targets too.
    assert key in found.targets['tree:s1']


def test_a_cut_whose_commits_do_not_apply_leaves_no_prep_branch(ws):
    service, workspace, project = ws
    found = service.snapshot(workspace)
    row = next(r for r in found.details['tree:s1'] if 'mm/foo: the series patch' in r.text)
    # Onto the prep branch's fork the series patch applies; make one that cannot.
    git('switch', '-q', '--detach', 'up/stable', cwd=project)
    tip = commit(project, 'foo.c', 'other\n', 'mm: other foo')
    git('update-ref', 'refs/remotes/up/stable', tip, cwd=project)
    git('switch', '-q', 'main', cwd=project)
    result = service.cut_prep(workspace, 'tree:s1', [row], 'nope', 'up/stable')
    assert not result.ok and 'does not apply' in result.error
    assert git('branch', '--list', 'b4/nope', cwd=project) == ''


def test_plucks_remember_where_commits_came_from(ws):
    service, workspace, project = ws
    found = service.snapshot(workspace)
    row = next(r for r in found.details['tree:s1'] if 'mm: fix the leak' in r.text)
    old = git('rev-parse', 'me/base', cwd=project)
    service.pluck(workspace, 'tree:s1', [row], 'base:linux:me/base', old)
    found = service.snapshot(workspace)
    base_row = next(r for r in found.details['base:linux:me/base'] if 'mm: fix the leak' in r.text)
    result = service.cut_prep(workspace, 'base:linux:me/base', [base_row], 'from-base', 'up/stable')
    assert result.ok
    cover = next(
        r
        for r in service.snapshot(workspace).details['prep:linux:b4/from-base']
        if r.text.startswith('Cover')
    )
    assert 'It leaked.' in service.show(cover)  # Through the base, back to the artifact.


def test_a_prep_branch_is_signed_off_previewed_and_sent_through_b4(ws, tmp_path):
    service, workspace, project = ws
    service.no_sign = True  # No patatt key here (b4 says how to make one otherwise).
    found = service.snapshot(workspace)
    row = next(r for r in found.details['tree:s1'] if 'mm: fix the leak' in r.text)
    assert service.cut_prep(workspace, 'tree:s1', [row], 'out', 'up/stable').ok
    key = 'prep:linux:b4/out'
    found = service.snapshot(workspace)
    assert {i.key: i for i in found.items}[key].label == 'b4/out · v1 · 1 commit'
    assert found.identities['linux'] == 'Example Maintainer <m@example.org>'
    signed = service.sign_off(workspace, key, found.tips[key])
    assert signed.ok and signed.signed == 1
    assert 'Signed-off-by: Example Maintainer <m@example.org>' in git(
        'log', '-1', '--format=%B', 'b4/out', cwd=project
    )
    code, summary = service.preview(workspace, key)
    assert code == 0, summary
    assert 'Subject: [PATCH] mm: fix the leak' in summary
    outgoing = tmp_path / 'ws' / 'var' / 'branches' / 'linux' / 'out.outgoing'
    assert any(outgoing.iterdir())
    worktree, branch, args = service.send_args(workspace, key)
    assert (worktree.name, branch, args) == ('out', 'b4/out', ('send', '--no-sign'))


def test_the_apps_dry_run_and_no_sign_reach_b4_send(ws):
    service, workspace, _project = ws
    service.email_dryrun, service.no_sign = True, True
    found = service.snapshot(workspace)
    row = next(r for r in found.details['tree:s1'] if 'mm: fix the leak' in r.text)
    service.cut_prep(workspace, 'tree:s1', [row], 'dry', 'up/stable')
    _worktree, _branch, args = service.send_args(workspace, 'prep:linux:b4/dry')
    assert args == ('send', '-d', '--no-sign')


def test_rebasing_a_base_moves_the_prep_branches_cut_from_it(ws):
    service, workspace, project = ws
    found = service.snapshot(workspace)
    row = next(r for r in found.details['tree:s1'] if 'mm: fix the leak' in r.text)
    assert service.cut_prep(workspace, 'tree:s1', [row], 'on-base', 'me/base').ok
    assert service.cut_prep(workspace, 'tree:s1', [row], 'on-upstream', 'up/stable').ok
    git('switch', '-q', '--detach', 'up/stable', cwd=project)
    tip = commit(project, 'u.c', 'u\n', 'mm: upstream moved')
    git('update-ref', 'refs/remotes/up/stable', tip, cwd=project)
    git('switch', '-q', 'main', cwd=project)
    old = git('rev-parse', 'me/base', cwd=project)
    result = service.rebase(workspace, 'base:linux:me/base', old)
    assert result.ok and result.moved == ('b4/on-base',) and result.stuck == ()
    new_base = git('rev-parse', 'me/base', cwd=project)
    assert git('merge-base', '--is-ancestor', new_base, 'b4/on-base', cwd=project) == ''
    # Cut from the upstream, not the base: left where it is.
    assert tip not in git('rev-list', 'b4/on-upstream', cwd=project).split()


def test_reaping_says_what_goes_then_removes_it(rebase_ws, tmp_path):
    service, workspace, project, hub, runner = rebase_ws
    found = service.snapshot(workspace)
    row = next(r for r in found.details['tree:s1'] if 'mm: fix the leak' in r.text)
    assert service.cut_prep(workspace, 'tree:s1', [row], 'reap-me', 'up/stable').ok
    git('branch', 'forgotten', 'up/stable', cwd=project)
    git('switch', '-q', 'forgotten', cwd=project)
    commit(project, 'f.c', 'f\n', 'mm: forgotten work')
    git('switch', '-q', 'main', cwd=project)
    found = service.snapshot(workspace)
    assert service.reapable(found, 'prep:linux:b4/reap-me')
    assert service.reapable(found, 'branch:linux:forgotten')
    assert service.reapable(found, 'tree:s1')
    assert not service.reapable(found, 'base:linux:me/base')
    assert not service.reapable(found, f'worktree:linux:{project}')
    loss = service.losses(workspace, 'prep:linux:b4/reap-me')
    assert 'b4/reap-me' in loss and '1 commit' in loss and 'archives' in loss
    assert 'mm: forgotten work' in service.losses(workspace, 'branch:linux:forgotten')
    assert 'pull-ahead' in service.losses(workspace, 'tree:s1')
    for key in ('prep:linux:b4/reap-me', 'branch:linux:forgotten', 'tree:s1'):
        ok, message = service.reap(workspace, key)
        assert ok, message
    assert git('branch', '--list', 'b4/reap-me', 'forgotten', cwd=project) == ''
    assert not (tmp_path / 'ws' / 'var' / 'worktrees' / 's1').exists()
    keys = {i.key for i in service.snapshot(workspace).items}
    assert not keys & {'prep:linux:b4/reap-me', 'branch:linux:forgotten', 'tree:s1'}


def second_tree(tmp_path, project, store, sid='s2'):
    """Another session tree of the same project (e.g. the series' next revision)."""
    tree = tmp_path / 'ws' / 'var' / 'worktrees' / sid
    git('clone', '-q', '--shared', str(project), str(tree), cwd=tmp_path)
    base = git('rev-parse', 'up/stable', cwd=project)
    git('switch', '-q', '-c', f'b4m/session/{sid}', base, cwd=tree)
    commit(tree, 'foo.c', 'foo v2\n', 'mm/foo: the series patch, v2')
    applied = git('rev-parse', 'HEAD', cwd=tree)
    record = make_record(
        sid,
        mode='worktree',
        revision=4,
        worktree=Worktree(str(tree), f'b4m/session/{sid}', base, applied),
    )
    store.create(record)
    return tree, record


def test_commits_copy_to_another_session_tree_on_branches_of_the_same_name(ws, tmp_path):
    _service, workspace, project = ws
    store = SessionStore(tmp_path / 'ws' / 'var' / 'sessions')
    tree2, record2 = second_tree(tmp_path, project, store)
    service = BranchService(tmp_path / 'ws', lambda: [store.record('s1'), record2], store.directory)
    found = service.snapshot(workspace)
    assert 'tree:s2' in found.targets['tree:s1']
    row = next(r for r in found.details['tree:s1'] if 'mm: fix the leak' in r.text)
    result = service.pluck(workspace, 'tree:s1', [row], 'tree:s2', found.tips['tree:s2'])
    assert result.ok, result.error
    # On the pull-ahead branch of the same name, started at s2's series base.
    assert git('log', '--format=%s', 'b4m/pull-ahead/leak', '-1', cwd=tree2) == 'mm: fix the leak'
    assert git('rev-parse', 'b4m/pull-ahead/leak~1', cwd=tree2) == record2.worktree.base
    note = (store.directory('s2') / 'work' / 'context' / 'inbox.md').read_text()
    assert 'b4m/pull-ahead/leak' in note and 'mm: fix the leak' in note and 's1' in note
    # The branch checked out in the target tree is never moved under it.
    series_row = next(r for r in found.details['tree:s1'] if 'mm/foo: the series patch' in r.text)
    git('switch', '-q', '-c', 'b4m/session/s1', cwd=tree2)  # Same name as the source branch.
    busy = service.pluck(workspace, 'tree:s1', [series_row], 'tree:s2', found.tips['tree:s2'])
    assert not busy.ok and 'checked out' in busy.error


def test_a_derived_session_may_start_at_the_base_the_parents_tree_or_a_maintainer_base(ws):
    service, workspace, project = ws
    found = service.snapshot(workspace)
    row = next(r for r in found.details['tree:s1'] if 'mm: fix the leak' in r.text)
    options = service.derive_options(workspace, 'tree:s1', row)
    starts = [(start_from, label.split(' ')[0]) for _start, start_from, label in options]
    assert starts == [('', 'the'), ('s1', 'session'), ('s1', 'session'), ('', 'maintainer')]
    assert options[0][0] == git('rev-parse', 'up/stable', cwd=project)  # The series base.
    assert options[1][0] == row.key and options[3][0] == 'me/base'
    context = service.derive_context('tree:s1', None)
    assert context is not None and context.id == 'parent'
    assert 'It leaked.' in context.text and 'All pass.' in context.text
    artifact = next(r for r in found.details['tree:s1'] if r.artifact is not None)
    one = service.derive_context('tree:s1', artifact)
    assert one is not None and artifact.artifact.title in one.label

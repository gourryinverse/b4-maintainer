"""Contract: a workbench session has one tree per project, side by side in one folder
(var/worktrees/<id>/<project>); the agent starts in the folder, may write in every tree,
reads every project checkout, and is told what each tree holds. It counts once."""

import datetime
import pathlib

import pytest
from gitutil import git
from sessiondata import PROFILE, FakeSessionRunner, FakeWorktrees

from b4_maintainer.sessions.hub import RecordWorktrees, SessionError, SessionHub, TreePlan
from b4_maintainer.sessions.launch import LaunchChoice, Situation
from b4_maintainer.sessions.models import model_choices
from b4_maintainer.sessions.pi import LaunchSpec, Site, pi_command, prepare_session
from b4_maintainer.sessions.policy import SessionLimits
from b4_maintainer.sessions.profiles import Request, parse_profile
from b4_maintainer.sessions.records import ProjectTree, Worktree, session_label, session_trees
from b4_maintainer.sessions.store import SessionStore
from b4_maintainer.sessions.trees import TreeItem

NOW = datetime.datetime(2026, 10, 27, 12, 0, tzinfo=datetime.timezone.utc)
PLANS = (
    TreePlan('linux', 'gourry/base', (TreeItem('cxl: region mode v3', '/mail/cxl.mbx', 3),)),
    TreePlan('ndctl', 'origin/main', (TreeItem('daxctl: fix v1', '/mail/dax.mbx', 1),)),
)


@pytest.fixture
def store(tmp_path):
    return SessionStore(tmp_path / 'var' / 'sessions')


def situation(**changes):
    base = dict(models=model_choices(None, []), limits=SessionLimits(), worktree_reason='')
    return Situation(**{**base, **changes})


def choice():
    return LaunchChoice('series-test', {'goal': 'review'}, model='dry-run', mode='worktree')


def launch(hub, **changes):
    args = dict(
        bench='cxl-dax',
        label='CXL DAX',
        plans=PLANS,
        profile=parse_profile(PROFILE),
        choice=choice(),
        situation=situation(),
        now=NOW,
    )
    return hub.launch_workbench(**{**args, **changes})


def test_a_workbench_session_prepares_a_tree_per_project_in_one_folder(store):
    runner, trees = FakeSessionRunner(store), FakeWorktrees()
    hub = SessionHub(store, runner, trees, sandbox=lambda: True)
    record = launch(hub)
    assert record.scope == 'workbench' and record.workbench == 'cxl-dax'
    assert record.mode == 'worktree' and record.worktree is None
    assert [t.project for t in record.trees] == ['linux', 'ndctl']
    assert record.trees[0].applied == ('cxl: region mode v3',)
    assert record.home == f'/w/var/worktrees/{record.id}'
    assert [(sid, plan) for sid, plan in trees.entries] == [(record.id, p) for p in PLANS]
    assert session_label(record) == 'workbench: CXL DAX'
    assert [p for p, _w in session_trees(record)] == ['linux', 'ndctl']
    assert store.record(record.id) == record  # Survives the record file.
    [(sid, request)] = runner.launches
    assert sid == record.id and request.prompt.startswith('Help me review')
    context = store.directory(record.id) / 'work' / 'context'
    assert sorted(p.name for p in context.iterdir() if p.suffix == '.mbx') == []  # Fakes.


def test_a_tree_that_fails_leaves_none_of_them(store):
    runner, trees = FakeSessionRunner(store), FakeWorktrees()
    trees.fail_entry = ('ndctl', ('daxctl: fix does not apply at origin/main', 'out', 'c' * 40))
    hub = SessionHub(store, runner, trees, sandbox=lambda: True)
    with pytest.raises(SessionError, match='ndctl') as raised:
        launch(hub)
    assert raised.value.base == 'c' * 40
    assert trees.removed_homes and runner.launches == []
    [view] = store.poll(runner.alive)
    assert view.state == 'failed'


def test_it_counts_once_against_the_limits(store):
    runner, trees = FakeSessionRunner(store), FakeWorktrees()
    hub = SessionHub(store, runner, trees, sandbox=lambda: True)
    launch(hub, situation=situation(live_builds=1))  # max_builds = 2: one slot left.
    with pytest.raises(SessionError, match='max_builds'):
        launch(hub, situation=situation(live_builds=2))
    with pytest.raises(SessionError, match='sandbox'):
        launch(hub, situation=situation(worktree_reason='no sandbox here'))


def test_the_agent_starts_in_the_folder_and_may_write_every_tree(tmp_path, store):
    from sessiondata import make_record

    home = tmp_path / 'var' / 'worktrees' / 'w1'
    trees = (
        ProjectTree('linux', Worktree(str(home / 'linux'), 'b4m/session/w1', 'b', 'a'), ('x v3',)),
        ProjectTree('ndctl', Worktree(str(home / 'ndctl'), 'b4m/session/w1', 'c', 'd'), ()),
    )
    record = make_record(
        'w1',
        scope='workbench',
        mode='worktree',
        permissions='execute',
        workbench='cxl-dax',
        trees=trees,
        home=str(home),
    )
    site = Site(tmp_path, '/bin/b4m', checkouts={'linux': '/w/linux', 'ndctl': '/w/ndctl'})
    run = pi_command(store, record, LaunchSpec(Request('Task.', 'Sys.', 'worktree', ())), site)
    assert run.cwd == str(home) and run.env['B4M_WORKTREE'] == str(home)
    readable = run.env['B4M_READABLE'].split('\n')
    assert {'/w/linux', '/w/ndctl', str(home)} <= set(readable)
    prepare_session(store, record, Request('Task.', 'Sys.', 'worktree', ()), None, True)
    system = (store.directory('w1') / 'system.md').read_text()
    assert f'{home}/linux' in system and f'{home}/ndctl' in system
    assert 'x v3' in system and 'agent identity' in system
    assert 'read-only except the output directory and the trees' in system


def project(path, name):
    path.mkdir(parents=True)
    git('init', '-q', '-b', 'main', cwd=path)
    (path / f'{name}.c').write_text(f'{name}\n')
    git('add', '-A', cwd=path)
    git('-c', 'user.name=M', '-c', 'user.email=m@example.org', 'commit', '-q', '-m', name, cwd=path)
    return git('rev-parse', 'HEAD', cwd=path)


def test_real_trees_sit_side_by_side_and_are_listed_and_removed_together(tmp_path, home):
    from sessiondata import make_record

    linux = project(tmp_path / 'projects' / 'linux', 'linux')
    project(tmp_path / 'projects' / 'ndctl', 'ndctl')
    directory = tmp_path / 'var' / 'worktrees'
    trees = RecordWorktrees(
        directory,
        {
            'linux': str(tmp_path / 'projects' / 'linux'),
            'ndctl': str(tmp_path / 'projects' / 'ndctl'),
        },
    )
    record = make_record('w1', scope='workbench', mode='worktree', model='dry-run')
    made = [trees.prepare_entry(record, TreePlan(p, 'main')) for p in ('linux', 'ndctl')]
    assert [t.worktree.path for t in made] == [
        str(directory / 'w1' / p) for p in ('linux', 'ndctl')
    ]
    assert made[0].worktree.base == linux and made[0].worktree.branch == 'b4m/session/w1'
    record = record.replace(trees=tuple(made), home=str(directory / 'w1'))
    infos = trees.list([record])
    assert [(i.path, i.session) for i in infos] == [(t.worktree.path, 'w1') for t in made]
    for info in infos:
        trees.remove(info.path)
    assert not (directory / 'w1').exists()


def test_branches_shows_each_tree_under_its_project_and_acts_on_it(tmp_path, home):
    from sessiondata import make_record

    from b4_maintainer.branches import BranchService
    from b4_maintainer.sessions.store import SessionStore
    from b4_maintainer.workspace import Workspace

    (home / '.gitconfig').write_text(
        '[user]\n\tname = Example Maintainer\n\temail = m@example.org\n'
    )
    root = tmp_path
    for name in ('linux', 'ndctl'):
        project(root / 'projects' / name, name)
        git('branch', 'me/base', cwd=root / 'projects' / name)
    workspace = Workspace.from_config(
        {
            'projects': [
                {
                    'id': p,
                    'path': f'projects/{p}',
                    'bases': [{'branch': 'me/base', 'upstream': 'main'}],
                }
                for p in ('linux', 'ndctl')
            ]
        }
    )
    trees = RecordWorktrees(
        root / 'var' / 'worktrees', {p: str(root / 'projects' / p) for p in ('linux', 'ndctl')}
    )
    store = SessionStore(root / 'var' / 'sessions')
    record = make_record('w1', scope='workbench', mode='worktree', model='dry-run', subject='CXL')
    made = tuple(trees.prepare_entry(record, TreePlan(p, 'main')) for p in ('linux', 'ndctl'))
    record = record.replace(trees=made, home=str(root / 'var' / 'worktrees' / 'w1'), workbench='b')
    store.create(record)
    ndctl_tree = root / 'var' / 'worktrees' / 'w1' / 'ndctl'
    git('switch', '-q', '-c', 'b4m/fixup/x', cwd=ndctl_tree)
    (ndctl_tree / 'fix.c').write_text('fix\n')
    git('add', '-A', cwd=ndctl_tree)
    git(
        '-c',
        'user.name=Agent',
        '-c',
        'user.email=agent@b4-maintainer.invalid',
        'commit',
        '-q',
        '-m',
        'daxctl: fix x',
        cwd=ndctl_tree,
    )
    service = BranchService(root, lambda: [store.record('w1')], store.directory)
    data = service.snapshot(workspace)
    by_key = {i.key: i for i in data.items}
    assert by_key['tree:w1/linux'].project == 'linux' and by_key['tree:w1/ndctl'].project == 'ndctl'
    assert '(ndctl)' in by_key['tree:w1/ndctl'].label
    rows = data.details['tree:w1/ndctl']
    fix = next(r for r in rows if r.text.strip().endswith('daxctl: fix x'))
    assert 'base:ndctl:me/base' in data.targets['tree:w1/ndctl']
    assert 'base:linux:me/base' not in data.targets['tree:w1/ndctl']  # Its own project's.
    tip = data.tips['base:ndctl:me/base']
    result = service.pluck(workspace, 'tree:w1/ndctl', [fix], 'base:ndctl:me/base', tip)
    assert result.ok, result.error
    assert (
        git('log', '-1', '--format=%s', 'me/base', cwd=root / 'projects' / 'ndctl')
        == 'daxctl: fix x'
    )
    targets, tips = service.tree_targets(workspace, 'tree:w1/ndctl')
    assert [k for k, _label in targets] == ['base:ndctl:me/base']  # Its project's only.
    assert tips['base:ndctl:me/base'] == git(
        'rev-parse', 'me/base', cwd=root / 'projects' / 'ndctl'
    )
    options = service.derive_options(workspace, 'tree:w1/ndctl', None)
    assert any(start_from == 'w1/ndctl' for _start, start_from, _label in options)


def test_a_tree_whose_items_conflict_can_be_left_to_the_agent(tmp_path, home):
    from sessiondata import make_record

    from b4_maintainer.sessions.hub import TreeError

    (home / '.gitconfig').write_text(
        '[user]\n\tname = Example Maintainer\n\temail = m@example.org\n'
    )
    base = project(tmp_path / 'projects' / 'ndctl', 'ndctl')
    repo = tmp_path / 'projects' / 'ndctl'
    for name, text in (('me/a', 'a\n'), ('me/b', 'b\n')):  # Both rewrite ndctl.c.
        git('switch', '-q', '-c', name, base, cwd=repo)
        (repo / 'ndctl.c').write_text(text)
        git('commit', '-q', '-am', f'ndctl: {name}', cwd=repo)
    git('switch', '-q', 'main', cwd=repo)
    trees = RecordWorktrees(tmp_path / 'var' / 'worktrees', {'ndctl': str(repo)})
    record = make_record('w1', scope='workbench', mode='worktree', model='dry-run')
    items = (TreeItem('me/a', branch='me/a'), TreeItem('me/b', branch='me/b'))
    with pytest.raises(TreeError) as raised:
        trees.prepare_entry(record, TreePlan('ndctl', 'main', items))
    assert 'me/b' in str(raised.value) and raised.value.base == base
    made = trees.prepare_entry(record, TreePlan('ndctl', 'main', items, agent_apply=base))
    assert made.worktree.agent_applies and made.worktree.base == base
    mbox = (
        tmp_path / 'var' / 'worktrees' / 'w1' / 'ndctl' / '.git' / 'b4m' / 'series.mbx'
    ).read_text()
    assert mbox.index('ndctl: me/a') < mbox.index('ndctl: me/b')
    assert made.applied == ('me/a', 'me/b')


def test_a_lightweight_workbench_session_compares_mailboxes_without_trees(tmp_path, store):
    from b4_maintainer.fixtures import write_series_mbox

    a = write_series_mbox(tmp_path / 'a.mbx', subject='mm: fix it one way')
    b = write_series_mbox(tmp_path / 'b.mbx', subject='mm: fix it another way')
    plans = (
        TreePlan(
            'linux',
            'gourry/base',
            (
                TreeItem('mm: fix it one way v1', str(a.path), 1),
                TreeItem('mm: fix it another way v1', str(b.path), 1),
                TreeItem('gourry/fixes', branch='gourry/fixes'),  # A tree's only.
            ),
        ),
    )
    runner, trees = FakeSessionRunner(store), FakeWorktrees()
    hub = SessionHub(store, runner, trees, sandbox=lambda: True)
    light = LaunchChoice('series-test', {'goal': 'review'}, model='dry-run', mode='lightweight')
    record = launch(hub, plans=plans, choice=light, situation=situation(live_builds=2))
    assert record.mode == 'lightweight' and record.permissions == 'read-only'
    assert record.trees == () and record.home == '' and trees.entries == []
    assert record.scope == 'workbench' and record.workbench == 'cxl-dax'
    context = store.directory(record.id) / 'work' / 'context'
    assert sorted(p.name for p in context.glob('*.mbx')) == ['linux-1.mbx', 'linux-2.mbx']
    system = (store.directory(record.id) / 'system.md').read_text()
    assert 'linux-1.mbx (mm: fix it one way v1 (linux))' in system
    assert 'Edit, build' not in system and 'trees sit side by side' not in system
    [(sid, _request)] = runner.launches
    assert sid == record.id


def test_read_only_items_reach_the_context_and_never_a_tree(tmp_path, store):
    from b4_maintainer.fixtures import write_series_mbox

    talk = write_series_mbox(tmp_path / 'talk.mbx', subject='RFC: tiers')
    notes = tmp_path / 'notes.md'
    notes.write_text('ideas\n')
    plans = (
        TreePlan(
            'linux',
            'gourry/base',
            (
                TreeItem('RFC: tiers (discussion)', str(talk.path), apply=False),
                TreeItem('notes.md (file)', file=str(notes), apply=False),
            ),
        ),
    )
    runner, trees = FakeSessionRunner(store), FakeWorktrees()
    hub = SessionHub(store, runner, trees, sandbox=lambda: True)
    light = LaunchChoice('series-test', {'goal': 'review'}, model='dry-run', mode='lightweight')
    record = launch(hub, plans=plans, choice=light)
    context = store.directory(record.id) / 'work' / 'context'
    assert (context / 'linux-1.mbx').is_file()
    assert (context / 'files' / 'notes.md').read_text() == 'ideas\n'
    system = (store.directory(record.id) / 'system.md').read_text()
    assert 'files/notes.md (notes.md (file) (linux))' in system


def test_a_tree_can_take_its_items_for_the_agent_instead_of_applied(tmp_path, home, store):
    from sessiondata import make_record

    from b4_maintainer.sessions.pi import prepare_session

    (home / '.gitconfig').write_text(
        '[user]\n\tname = Example Maintainer\n\temail = m@example.org\n'
    )
    base = project(tmp_path / 'projects' / 'ndctl', 'ndctl')
    repo = tmp_path / 'projects' / 'ndctl'
    git('switch', '-q', '-c', 'me/a', base, cwd=repo)
    (repo / 'ndctl.c').write_text('a\n')
    git('commit', '-q', '-am', 'ndctl: a', cwd=repo)
    git('switch', '-q', 'main', cwd=repo)
    trees = RecordWorktrees(tmp_path / 'var' / 'worktrees', {'ndctl': str(repo)})
    record = make_record('w1', scope='workbench', mode='worktree', model='dry-run')
    plan = TreePlan('ndctl', 'main', (TreeItem('me/a', branch='me/a'),), use='deliver')
    made = trees.prepare_entry(record, plan)
    assert made.worktree.base == base and made.applied == () and made.delivered == ('me/a',)
    assert git('rev-parse', 'HEAD', cwd=made.worktree.path) == base  # Nothing applied.
    mbox = pathlib.Path(made.worktree.path) / '.git' / 'b4m' / 'series.mbx'
    assert 'ndctl: a' in mbox.read_text()
    record = record.replace(
        trees=(made,), home=str(tmp_path / 'var' / 'worktrees' / 'w1'), permissions='execute'
    )
    prepare_session(store, record, Request('Task.', 'Sys.', 'worktree', ()), None, True)
    system = (store.directory('w1') / 'system.md').read_text()
    assert 'not applied: me/a' in system and 'as the task directs' in system
    assert 'could not apply' not in system


def test_a_project_without_a_tree_still_gives_its_mail(store, tmp_path):
    from b4_maintainer.fixtures import write_series_mbox

    a = write_series_mbox(tmp_path / 'a.mbx', subject='daxctl: fix')
    plans = (
        TreePlan('linux', 'gourry/base', (TreeItem('cxl: x v3', '/mail/cxl.mbx', 3),)),
        TreePlan('ndctl', 'origin/main', (TreeItem('daxctl: fix v1', str(a.path), 1),), use='none'),
    )
    runner, trees = FakeSessionRunner(store), FakeWorktrees()
    hub = SessionHub(store, runner, trees, sandbox=lambda: True)
    record = launch(hub, plans=plans)
    assert [plan.project for _sid, plan in trees.entries] == ['linux']
    assert [t.project for t in record.trees] == ['linux']
    context = store.directory(record.id) / 'work' / 'context'
    assert (context / 'ndctl-1.mbx').is_file()

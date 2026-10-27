"""Contract: a workbench session has one tree per project, side by side in one folder
(var/worktrees/<id>/<project>); the agent starts in the folder, may write in every tree,
reads every project checkout, and is told what each tree holds. It counts once."""

import datetime

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

NOW = datetime.datetime(2026, 10, 27, 12, 0, tzinfo=datetime.timezone.utc)
PLANS = (
    TreePlan('linux', 'gourry/base', (('/mail/cxl.mbx', 3, 'cxl: region mode'),)),
    TreePlan('ndctl', 'origin/main', (('/mail/dax.mbx', 1, 'daxctl: fix'),)),
)


@pytest.fixture
def store(tmp_path):
    return SessionStore(tmp_path / 'var' / 'sessions')


def situation(**changes):
    base = dict(models=model_choices(None, []), limits=SessionLimits(), worktree_reason='')
    return Situation(**{**base, **changes})


def choice():
    return LaunchChoice('series-test', {'goal': 'review'}, model='dry-run')


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

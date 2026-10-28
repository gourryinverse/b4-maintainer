"""Contract: x on a workbench says what goes (its sessions, live ones ended; each tree and
what it would lose), asks again when a tree holds changes, then ends its sessions, removes
their trees, deletes their records and output, and removes the workbench. Sessions whose
workbench is gone are listed as unfiled."""

from gitutil import git
from sessiondata import FakeSessionRunner, FakeWorktrees, make_record
from test_session_workbench import project
from test_workbench_screen import FakeService

from b4_maintainer.adapters.fixture import FixtureSource
from b4_maintainer.app import Confirm, MaintainerApp
from b4_maintainer.sessions.hub import RecordWorktrees, SessionHub, TreePlan, WorktreeInfo
from b4_maintainer.sessions.store import SessionStore
from b4_maintainer.sessions.trees import TreeState
from b4_maintainer.workbench import Entry, Workbench, WorkbenchStore


def bench_record(sid, bench='cmp', **changes):
    return make_record(
        sid, scope='workbench', series=f'workbench:{bench}', workbench=bench, **changes
    )


def test_removing_sessions_ends_them_and_deletes_their_trees_and_records(tmp_path, home):
    (home / '.gitconfig').write_text('[user]\n\tname = M\n\temail = m@example.org\n')
    for name in ('linux', 'ndctl'):
        project(tmp_path / 'projects' / name, name)
    directory = tmp_path / 'var' / 'worktrees'
    trees = RecordWorktrees(
        directory, {p: str(tmp_path / 'projects' / p) for p in ('linux', 'ndctl')}
    )
    store = SessionStore(tmp_path / 'var' / 'sessions')
    runner = FakeSessionRunner(store)
    hub = SessionHub(store, runner, trees)
    record = bench_record('w1', mode='worktree', model='dry-run')
    made = tuple(trees.prepare_entry(record, TreePlan(p, 'main')) for p in ('linux', 'ndctl'))
    runner.add(record.replace(trees=made, home=str(directory / 'w1')))  # Live.
    runner.add(bench_record('w2'), alive=False)  # Lightweight, ended.
    (store.directory('w2') / 'work' / 'out').mkdir(parents=True)
    (store.directory('w2') / 'work' / 'out' / 'notes.md').write_text('notes\n')
    (directory / 'w1' / 'ndctl' / 'wip.c').write_text('wip\n')
    live, losses = hub.session_losses(['w1', 'w2'])
    assert live == ['w1']
    ndctl = str(directory / 'w1' / 'ndctl')
    assert set(losses) == {str(directory / 'w1' / 'linux'), ndctl}
    assert not losses[ndctl].clean and losses[str(directory / 'w1' / 'linux')].clean
    assert hub.remove_sessions(['w1', 'w2']) == ['w1', 'w2']
    assert runner.ends == ['w1']  # Only the live one is ended.
    assert not (directory / 'w1').exists()
    assert not store.exists('w1') and not store.directory('w2').exists()
    assert hub.records() == []
    assert git('rev-parse', 'HEAD', cwd=tmp_path / 'projects' / 'ndctl')  # Untouched.


def make(demo_root, tmp_path):
    sessions = SessionStore(tmp_path / 'var' / 'sessions')
    runner = FakeSessionRunner(sessions)
    trees = FakeWorktrees()
    hub = SessionHub(sessions, runner, trees, sandbox=lambda: True)
    store = WorkbenchStore(tmp_path / 'state' / 'workbenches')
    store.save(Workbench('cmp', 'Compare', (Entry('linux', '', ()), Entry('ndctl', '', ()))))
    store.save(Workbench('keep', 'Keep', (Entry('linux', '', ()),)))
    service = FakeService(tmp_path, store, hub.records, sessions.directory)
    app = MaintainerApp(FixtureSource(demo_root).load, sessions=hub, workbenches=service)
    return app, store, runner, trees


async def settle(pilot):
    for _ in range(3):
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()


async def select(pilot, wid):
    screen = pilot.app.screen
    while screen.bench().id != wid:
        await pilot.press('down')
    await settle(pilot)
    return screen


async def test_x_on_a_workbench_removes_its_sessions_and_their_trees(demo_root, tmp_path):
    from b4_maintainer.sessions.records import ProjectTree, Worktree

    app, store, runner, trees = make(demo_root, tmp_path)
    paths = [f'/w/var/worktrees/w1/{p}' for p in ('linux', 'ndctl')]
    made = tuple(
        ProjectTree(p, Worktree(path, 'b4m/session/w1', 'b', 'a'))
        for p, path in zip(('linux', 'ndctl'), paths)
    )
    runner.add(bench_record('w1', mode='worktree', trees=made, home='/w/var/worktrees/w1'))
    runner.add(bench_record('w2'), alive=False)
    runner.add(bench_record('k1', bench='keep'), alive=False)
    trees.infos = [WorktreeInfo(p, 'b4m/session/w1', 'w1', 1, TreeState()) for p in paths]
    trees.states[paths[1]] = TreeState(changed=(' M ndctl.c',))
    async with app.run_test(size=(200, 50)) as pilot:
        app.poll_sessions()
        await settle(pilot)
        await pilot.press('f3')
        await settle(pilot)
        await select(pilot, 'cmp')
        await pilot.press('x')
        await settle(pilot)
        first = app.screen
        assert isinstance(first, Confirm)
        text = first.prompt
        assert 'Compare' in text and 'w1 (live: ended)' in text and 'w2' in text
        assert f'{paths[1]}: 1 uncommitted file' in text
        await pilot.press('y')
        await settle(pilot)
        again = app.screen
        assert isinstance(again, Confirm) and 'This will be lost' in again.prompt
        await pilot.press('y')
        await settle(pilot)
        assert store.get('cmp') is None and store.get('keep') is not None
        assert runner.ends == ['w1']
        assert trees.removed == paths
        assert {r.id for r in app._sessions.records()} == {'k1'}


async def test_x_on_an_empty_workbench_asks_once(demo_root, tmp_path):
    app, store, runner, _trees = make(demo_root, tmp_path)
    async with app.run_test(size=(200, 50)) as pilot:
        await settle(pilot)
        await pilot.press('f3')
        await settle(pilot)
        await select(pilot, 'cmp')
        await pilot.press('x')
        await settle(pilot)
        assert isinstance(app.screen, Confirm) and 'no sessions' in app.screen.prompt
        await pilot.press('y')
        await settle(pilot)
        assert store.get('cmp') is None and runner.ends == []


async def test_sessions_of_a_removed_workbench_are_unfiled(demo_root, tmp_path):
    app, _store, runner, _trees = make(demo_root, tmp_path)
    runner.add(bench_record('o1', bench='gone'), alive=False)
    async with app.run_test(size=(200, 50)) as pilot:
        app.poll_sessions()
        await settle(pilot)
        await pilot.press('f3')
        await settle(pilot)
        screen = app.screen
        assert screen.bench_labels()[-1] == 'Unfiled sessions'
        while screen.bench().label != 'Unfiled sessions':
            await pilot.press('down')
        await settle(pilot)
        assert any(label.startswith('o1') for label in screen.session_labels())

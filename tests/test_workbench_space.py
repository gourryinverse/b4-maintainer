"""Contract: a workbench has a space, var/workbenches/<id>/, whose context/ follows the
recipe: items.md lists every project and item and where each is, mail/ holds each
series' (at its revision) and discussion's thread, files/ each file item. A session
started on the workbench reads that context live, so what is added after it started
reaches it; changes.md, and each running session's inbox, say what changed."""

import datetime
import json
import pathlib

from sessiondata import make_record

from b4_maintainer.fixtures import EPOCH, PatchSpec, write_series_mbox
from b4_maintainer.model import Series
from b4_maintainer.space import space_dir
from b4_maintainer.workbench import (
    Workbench,
    WorkbenchService,
    WorkbenchStore,
    add_file,
    add_mail,
    add_series,
)
from b4_maintainer.workspace import Workspace

CHANGE_ID = '20261020-mm-foo-0123456789ab'


def cached(tmp_path):
    """Two revisions of a series and a discussion, as the workbench's series map has them."""
    from b4_maintainer.adapters.b4mbox import parse_mbox

    found = {}
    for revision in (2, 3):
        path = tmp_path / 'mail' / f'foo-v{revision}.mbx'
        path.parent.mkdir(parents=True, exist_ok=True)
        write_series_mbox(
            path,
            'mm: foo',
            revision=revision,
            patches=[PatchSpec('mm/foo.c')],
            change_id=CHANGE_ID,
            date=EPOCH + datetime.timedelta(days=revision),
        )
        [series] = parse_mbox(path)
        found[f'change-id:{CHANGE_ID}#v{revision}'] = series
    found[f'change-id:{CHANGE_ID}'] = found[f'change-id:{CHANGE_ID}#v3']
    talk = tmp_path / 'mail' / 'talk.mbx'
    write_series_mbox(talk, 'mm: a question', patches=[PatchSpec('mm/x.c')])
    [discussion] = parse_mbox(talk)
    found['talk'] = Series(**{**discussion.__dict__, 'identity': 'talk', 'discussion': True})
    return found


def service_for(tmp_path, records=()):
    store = WorkbenchStore(tmp_path / 'state' / 'workbenches')
    return WorkbenchService(tmp_path, store, lambda: list(records), lambda sid: tmp_path / sid)


def test_the_space_follows_the_recipe_and_says_what_changed(tmp_path):
    series = cached(tmp_path)
    service = service_for(tmp_path)
    identity = f'change-id:{CHANGE_ID}'
    bench = add_series(Workbench('cmp', 'Compare', ()), 'linux', identity, 2, 'mm: foo')
    service.sync_space(Workspace.from_config({}), bench, series)
    context = space_dir(tmp_path, 'cmp') / 'context'
    assert sorted(p.name for p in (context / 'mail').iterdir()) == ['linux-mm-foo-v2.mbx']
    assert 'v2' in (context / 'mail' / 'linux-mm-foo-v2.mbx').read_text()
    items = (context / 'items.md').read_text()
    assert 'Compare' in items and 'mail/linux-mm-foo-v2.mbx' in items and 'live' in items

    notes = tmp_path / 'notes.txt'
    notes.write_text('notes\n')
    bench = add_series(bench, 'linux', identity, 3, 'mm: foo')
    bench = add_mail(bench, 'linux', 'talk', 'mm: a question')
    bench = add_file(bench, 'linux', str(notes))
    changes = service.sync_space(Workspace.from_config({}), bench, series)
    assert changes == [
        'added mail/linux-mm-foo-v3.mbx (mm: foo v3, linux)',
        'added mail/linux-mm-a-question.mbx (mm: a question, linux)',
        'added files/notes.txt (notes.txt, linux)',
    ]
    assert (context / 'files' / 'notes.txt').read_text() == 'notes\n'
    log = (context / 'changes.md').read_text()
    assert 'added mail/linux-mm-foo-v3.mbx' in log

    bench = Workbench(bench.id, bench.label, (bench.entries[0].__class__('linux', '', ()),))
    changes = service.sync_space(Workspace.from_config({}), bench, series)
    assert 'removed mail/linux-mm-foo-v2.mbx (mm: foo v2, linux)' in changes
    assert not (context / 'mail').exists() or not list((context / 'mail').iterdir())
    assert service.sync_space(Workspace.from_config({}), bench, series) == []  # No change.


def test_running_sessions_of_the_workbench_hear_of_changes(tmp_path):
    series = cached(tmp_path)
    mine = make_record('w1', scope='workbench', workbench='cmp')
    other = make_record('w2', scope='workbench', workbench='else')
    ended = make_record('w3', scope='workbench', workbench='cmp', state='ended')
    service = service_for(tmp_path, [mine, other, ended])
    bench = add_mail(Workbench('cmp', 'Compare', ()), 'linux', 'talk', 'mm: a question')
    service.sync_space(Workspace.from_config({}), bench, series)
    inbox = tmp_path / 'w1' / 'work' / 'context' / 'inbox.md'
    assert 'added mail/linux-mm-a-question.mbx' in inbox.read_text()
    assert str(space_dir(tmp_path, 'cmp') / 'context') in inbox.read_text()
    assert not (tmp_path / 'w2' / 'work' / 'context' / 'inbox.md').exists()
    assert not (tmp_path / 'w3' / 'work' / 'context' / 'inbox.md').exists()


def test_a_workbench_session_reads_the_space_not_copies(tmp_path):
    from sessiondata import FakeSessionRunner, FakeWorktrees
    from test_session_workbench import launch

    from b4_maintainer.sessions.hub import SessionHub, TreePlan
    from b4_maintainer.sessions.launch import LaunchChoice
    from b4_maintainer.sessions.pi import Site, pi_command
    from b4_maintainer.sessions.store import SessionStore
    from b4_maintainer.sessions.trees import TreeItem

    store = SessionStore(tmp_path / 'var' / 'sessions')
    runner = FakeSessionRunner(store)
    hub = SessionHub(store, runner, FakeWorktrees(), spaces=lambda wid: space_dir(tmp_path, wid))
    space = space_dir(tmp_path, 'cmp')
    (space / 'context').mkdir(parents=True)
    source = tmp_path / 'thread.mbx'
    source.write_text('')
    plans = (TreePlan('linux', items=(TreeItem('mm: foo v3', str(source), 3),), use='none'),)
    light = LaunchChoice('series-test', {'goal': 'review'}, model='dry-run', mode='lightweight')
    record = launch(hub, bench='cmp', plans=plans, choice=light)
    assert record.space == str(space)
    work = store.directory(record.id) / 'work'
    assert not list((work / 'context').glob('*.mbx'))  # Not copied: read live.
    system = (store.directory(record.id) / 'system.md').read_text()
    assert str(space / 'context') in system and 'items.md' in system
    [(_sid, spec)] = [(s, x) for s, x in zip([record.id], runner.specs)]
    command = pi_command(store, record, spec, Site(tmp_path, 'b4-maintainer'))
    assert str(space / 'context') in command.env['B4M_READABLE'].split('\n')
    assert json.loads((store.directory(record.id) / 'session.json').read_text())['space']


async def test_a_mailbox_added_while_a_session_runs_reaches_it(demo_root, tmp_path):
    from sessiondata import FakeSessionRunner, FakeWorktrees
    from test_workbench_screen import FakeService

    from b4_maintainer.adapters.fixture import FixtureSource
    from b4_maintainer.app import MaintainerApp
    from b4_maintainer.sessions.hub import SessionHub
    from b4_maintainer.sessions.store import SessionStore

    sessions = SessionStore(tmp_path / 'var' / 'sessions')
    runner = FakeSessionRunner(sessions)
    hub = SessionHub(sessions, runner, FakeWorktrees(), spaces=lambda w: space_dir(tmp_path, w))
    store = WorkbenchStore(tmp_path / 'state' / 'workbenches')
    store.save(Workbench('cmp', 'Compare', ()))
    runner.add(make_record('w1', scope='workbench', workbench='cmp'))  # Already running.
    service = FakeService(tmp_path, store, hub.records, sessions.directory)
    app = MaintainerApp(FixtureSource(demo_root).load, sessions=hub, workbenches=service)
    async with app.run_test(size=(180, 40)) as pilot:
        for _ in range(3):
            await app.workers.wait_for_complete()
            await pilot.pause()
        item = app.query_one('SeriesList').highlighted_item
        await pilot.press('w')
        for _ in range(3):
            await app.workers.wait_for_complete()
            await pilot.pause()
        await pilot.press('enter')  # Onto Compare.
        for _ in range(3):
            await app.workers.wait_for_complete()
            await pilot.pause()
        mail = list((space_dir(tmp_path, 'cmp') / 'context' / 'mail').iterdir())
        assert [p.name for p in mail] == [f'{item.project}-{_slug(item)}.mbx']
        inbox = sessions.directory('w1') / 'work' / 'context' / 'inbox.md'
        assert mail[0].name in inbox.read_text()


def _slug(item):
    import re

    label = f'{item.series.subject} v{item.series.revision}'
    return re.sub(r'[^a-z0-9]+', '-', label.lower()).strip('-')


def test_workbench_sessions_check_out_the_spaces_repos_and_leave_branches_behind(tmp_path, home):
    from gitutil import git
    from test_session_workbench import project

    from b4_maintainer.sessions.hub import RecordWorktrees, TreePlan
    from b4_maintainer.sessions.sandbox import policy_for
    from b4_maintainer.sessions.store import SessionStore

    linux = project(tmp_path / 'projects' / 'linux', 'linux')
    trees = RecordWorktrees(
        tmp_path / 'var' / 'worktrees', {'linux': str(tmp_path / 'projects' / 'linux')}
    )
    space = space_dir(tmp_path, 'cmp')
    one = make_record('w1', scope='workbench', workbench='cmp', space=str(space), model='dry-run')
    two = make_record('w2', scope='workbench', workbench='cmp', space=str(space), model='dry-run')
    first = trees.prepare_entry(one, TreePlan('linux', 'main'))
    second = trees.prepare_entry(two, TreePlan('linux', 'main'))
    assert first.worktree.path == str(space / 'sessions' / 'w1' / 'linux')
    assert (first.worktree.base, second.worktree.applied) == (linux, linux)
    repo = space / 'repos' / 'linux.git'
    git('switch', '-q', '-c', 'b4m/fixup/x', cwd=first.worktree.path)
    git('commit', '-q', '--allow-empty', '-m', 'x', cwd=first.worktree.path)
    trees.remove(first.worktree.path)
    assert not (space / 'sessions' / 'w1').exists()
    heads = git('for-each-ref', '--format=%(refname:short)', 'refs/heads', cwd=repo).split()
    assert 'b4m/fixup/x' in heads and 'b4m/session/w1' in heads  # Kept in the space.

    store = SessionStore(tmp_path / 'var' / 'sessions')
    record = two.replace(
        trees=(second,), home=str(space / 'sessions' / 'w2'), permissions='execute'
    )
    store.create(record)
    from b4_maintainer.sessions.pi import LaunchSpec, Site, pi_command
    from b4_maintainer.sessions.profiles import Request

    spec = LaunchSpec(Request('Task.', 'Sys.', 'worktree', ()), sandboxed=True)
    command = pi_command(store, record, spec, Site(tmp_path, 'b4-maintainer'))
    (store.directory('w2') / 'launch.json').write_text(
        json.dumps({'argv': command.argv, 'cwd': command.cwd, 'env': command.env})
    )
    policy = policy_for(store, 'w2')
    assert str(space / 'repos') in policy.writable  # Its commits land in the space.
    assert command.cwd == str(space / 'sessions' / 'w2')


def test_a_space_is_listed_as_a_tree_holding_what_its_checkouts_left(tmp_path, home):
    from gitutil import git
    from test_session_workbench import project

    from b4_maintainer.adapters.bases import pluck_onto
    from b4_maintainer.sessions.hub import RecordWorktrees, TreePlan
    from b4_maintainer.space import space_records

    (home / '.gitconfig').write_text('[user]\n\tname = M\n\temail = m@example.org\n')
    project(tmp_path / 'projects' / 'linux', 'linux')
    checkout = tmp_path / 'projects' / 'linux'
    trees = RecordWorktrees(tmp_path / 'var' / 'worktrees', {'linux': str(checkout)})
    space = space_dir(tmp_path, 'cmp')
    one = make_record('w1', scope='workbench', workbench='cmp', space=str(space), model='dry-run')
    made = trees.prepare_entry(one, TreePlan('linux', 'main'))
    dest = made.worktree.path
    git('switch', '-q', '-c', 'b4m/fixup/x', cwd=dest)
    (pathlib.Path(dest) / 'x.c').write_text('x\n')
    git('add', '-A', cwd=dest)
    git('commit', '-q', '-m', 'linux: fix x', cwd=dest)
    sha = git('rev-parse', 'HEAD', cwd=dest)
    trees.remove(dest)  # The session's checkout goes; its branch stays in the space.

    bench = Workbench('cmp', 'Compare', ())
    [view] = space_records(tmp_path, [bench], [one.replace(trees=(made,))])
    assert view.workbench == 'cmp' and view.name == 'Compare (space)'
    [(proj, tree)] = [(t.project, t.worktree) for t in view.trees]
    assert (proj, tree.path, tree.branch) == (
        'linux',
        str(space / 'repos' / 'linux.git'),
        'b4m/bench/cmp',
    )
    service = service_for(tmp_path, [view])
    rows = service.contents(Workspace.from_config({}), bench, {}, {})
    texts = [r.text.strip() for r in rows]
    assert any('Compare (space) · linux' in t for t in texts)
    commit_rows = [r for r in rows if r.kind == 'commit']
    assert [r.attachment.title for r in commit_rows] == ['linux: fix x']
    # Each branch heads its commits, by name (so / finds it): open to start.
    [branch] = [r for r in rows if r.kind == 'group' and r.group.startswith('branch:')]
    assert branch.text.strip() == 'fixup · b4m/fixup/x (1)' and commit_rows[0].group == branch.group
    # The space's own row shows its branch, not a missing tree.
    space_row = next(r for r in rows if r.kind == 'tree')
    shown = service.show(Workspace.from_config({}), bench, space_row, {}, {})
    assert 'the tree is gone' not in shown and 'b4m/bench/cmp' in shown
    git('branch', 'me/target', 'main', cwd=checkout)
    result = pluck_onto(
        checkout,
        'me/target',
        pathlib.Path(commit_rows[0].attachment.repo),
        [sha],
        git('rev-parse', 'me/target', cwd=checkout),
        'agent@b4-maintainer.invalid',
        'LLM',
    )
    assert result.ok  # Plucked straight from the space's repository.
    # Removing the workbench would delete it: its confirmation lists it.
    assert service.space_branches('cmp') == ['linux: b4m/fixup/x']


def test_branches_made_in_extra_checkouts_are_listed_once(tmp_path, home):
    """A branch made in one of a session's extra checkouts (out/worktrees/, as the
    b4m-branches skill says) lists under that session; one made in a checkout of no
    session lists under the space. Never in neither."""
    from gitutil import git
    from test_session_workbench import project

    from b4_maintainer.sessions.hub import RecordWorktrees, TreePlan
    from b4_maintainer.sessions.trees import classify_tree
    from b4_maintainer.space import space_records

    base = project(tmp_path / 'projects' / 'linux', 'linux')
    trees = RecordWorktrees(
        tmp_path / 'var' / 'worktrees', {'linux': str(tmp_path / 'projects' / 'linux')}
    )
    space = space_dir(tmp_path, 'cmp')
    one = make_record('w1', scope='workbench', workbench='cmp', space=str(space), model='dry-run')
    made = trees.prepare_entry(one, TreePlan('linux', 'main'))
    dest = made.worktree.path

    def commit_in(extra: pathlib.Path, branch: str) -> None:
        git('worktree', 'add', '-q', '--detach', str(extra), base, cwd=dest)
        git('switch', '-q', '-c', branch, cwd=extra)
        (extra / f'{branch.rsplit("/", 1)[1]}.c').write_text('x\n')
        git('add', '-A', cwd=extra)
        git('commit', '-q', '-m', f'linux: {branch}', cwd=extra)

    commit_in(
        tmp_path / 'var' / 'sessions' / 'w1' / 'work' / 'out' / 'worktrees' / 'base',
        'b4m/pull-ahead/a',
    )
    commit_in(tmp_path / 'elsewhere', 'b4m/pull-ahead/b')

    def listed(path: str, tree) -> dict[str, int]:
        found = classify_tree(pathlib.Path(path), tree).branches
        return {b.name: len(b.commits) for b in found if b.commits}

    bench = Workbench('cmp', 'Compare', ())
    [view] = space_records(tmp_path, [bench], [one.replace(trees=(made,))])
    [space_tree] = [t.worktree for t in view.trees]
    assert listed(dest, made.worktree) == {'b4m/pull-ahead/a': 1}
    assert listed(space_tree.path, space_tree) == {'b4m/pull-ahead/b': 1}


def test_the_workbench_says_which_items_its_trees_lack_and_u_brings_them(tmp_path, home):
    from gitutil import git
    from test_session_workbench import project

    from b4_maintainer.sessions.hub import RecordWorktrees
    from b4_maintainer.workbench import Entry, Item

    (home / '.gitconfig').write_text('[user]\n\tname = M\n\temail = m@example.org\n')
    project(tmp_path / 'projects' / 'linux', 'linux')
    checkout = tmp_path / 'projects' / 'linux'
    for name in ('me/a', 'me/b'):
        git('switch', '-q', '-c', name, 'main', cwd=checkout)
        (checkout / f'{name[3:]}.c').write_text(name + '\n')
        git('add', '-A', cwd=checkout)
        git('commit', '-q', '-m', f'linux: {name}', cwd=checkout)
    git('switch', '-q', 'main', cwd=checkout)
    workspace = Workspace.from_config(
        {'projects': [{'id': 'linux', 'path': 'projects/linux', 'lists': ['l.example.org']}]}
    )
    space = space_dir(tmp_path, 'cmp')
    bench = Workbench('cmp', 'Compare', (Entry('linux', 'main', (Item(branch='me/a'),)),))
    running = make_record('w9', scope='workbench', workbench='cmp')
    service = service_for(tmp_path, [running])
    [plan] = service.plans(workspace, bench, {})[0]
    trees = RecordWorktrees(tmp_path / 'var' / 'worktrees', {'linux': str(checkout)})
    record = make_record('w1', scope='workbench', workbench='cmp', space=str(space), model='x')
    trees.prepare_entry(record, plan)
    assert service.space_status(workspace, bench, {}) == {'linux': ((), False)}

    bench = Workbench(
        'cmp', 'Compare', (Entry('linux', 'main', (Item(branch='me/a'), Item(branch='me/b'))),)
    )
    assert service.space_status(workspace, bench, {}) == {'linux': (('me/b',), False)}
    rows = service.contents(workspace, bench, {}, {})
    assert any('me/b' in r.text and 'not in the trees yet' in r.text for r in rows)
    service.sync_space(workspace, bench, {})
    items = (space / 'context' / 'items.md').read_text()
    assert 'me/b (branch) — local branch' not in items  # Says where it stands instead:
    assert 'not in the space branch b4m/bench/cmp yet' in items
    service.update_space(workspace, bench, {}, 'linux')
    inbox = (tmp_path / 'w9' / 'work' / 'context' / 'inbox.md').read_text()
    assert 'now also holds me/b' in inbox and 'rebase (or merge) onto b4m/bench/cmp' in inbox
    assert 'in the space branch b4m/bench/cmp' in (space / 'context' / 'items.md').read_text()
    assert service.space_status(workspace, bench, {}) == {'linux': ((), False)}
    repo = space / 'repos' / 'linux.git'
    assert git('log', '-1', '--format=%s', 'b4m/bench/cmp', cwd=repo) == 'linux: me/b'

    bench = Workbench('cmp', 'Compare', (Entry('linux', 'main', (Item(branch='me/b'),)),))
    assert service.space_status(workspace, bench, {}) == {'linux': ((), True)}  # Rebuild.
    service.update_space(workspace, bench, {}, 'linux')
    assert service.space_status(workspace, bench, {}) == {'linux': ((), False)}
    main = git('rev-parse', 'main', cwd=checkout)  # The space carries no project branches.
    assert git('log', '--format=%s', f'{main}..b4m/bench/cmp', cwd=repo) == 'linux: me/b'


def test_a_folder_item_reaches_the_space_whole_and_leaves_it(tmp_path):
    """A kept reproducer (a folder: its README, sources, scripts) as a file item."""
    race = tmp_path / 'kept' / 'race'
    race.mkdir(parents=True)
    (race / 'README.md').write_text('# race\n')
    (race / 'run.sh').write_text('#!/bin/sh\n')
    (race / 'run.sh').chmod(0o755)
    service = service_for(tmp_path)
    bench = add_file(Workbench('fix', 'Fix', ()), 'linux', str(race))
    changes = service.sync_space(Workspace.from_config({}), bench, {})
    assert changes == ['added files/race (race, linux)']
    copy = space_dir(tmp_path, 'fix') / 'context' / 'files' / 'race'
    assert (copy / 'README.md').read_text() == '# race\n' and (
        copy / 'run.sh'
    ).stat().st_mode & 0o111
    (race / 'race.c').write_text('int main(void) {}\n')  # Changed: refreshed.
    assert service.sync_space(Workspace.from_config({}), bench, {}) == []
    assert (copy / 'race.c').is_file()
    entry = bench.entries[0]
    said = service._describe(Workspace.from_config({}), entry, entry.items[0], None, {}, {})
    assert said == 'race (file) · read by the session'  # Not "does not exist".
    bench = Workbench(bench.id, bench.label, (bench.entries[0].__class__('linux', '', ()),))
    assert service.sync_space(Workspace.from_config({}), bench, {}) == [
        'removed files/race (race, linux)'
    ]
    assert not copy.exists()

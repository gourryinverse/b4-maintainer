"""Contract: workbenches are named recipes for sessions over several projects, one file
each in state/workbenches/: per project, a base and the series and local branches
applied on it in order (or one patch of a series, one commit of a branch)."""

import dataclasses
import pathlib

import pytest

from b4_maintainer.workbench import (
    Entry,
    Item,
    Workbench,
    WorkbenchError,
    WorkbenchStore,
    add_branch,
    add_series,
    move_item,
    parse_workbench,
    remove_item,
    reroll,
    rerolls,
    set_base,
    workbench_toml,
)

CXL = Item(series='change-id:20261020-cxl-region', revision=3, subject='cxl: region mode')
DAX = Item(series='msgid@example.org', revision=1, subject='dax: fix "hotplug"')
FIXES = Item(branch='gourry/fixes')
BENCH = Workbench(
    'cxl-dax',
    'CXL DAX',
    (
        Entry('linux', 'gourry/base', (CXL, FIXES)),
        Entry('ndctl', 'origin/main', (DAX,)),
    ),
)


def test_a_workbench_round_trips_through_its_toml():
    text = workbench_toml(BENCH)
    assert parse_workbench(text, 'cxl-dax') == BENCH
    assert 'project = "linux"' in text and 'base = "gourry/base"' in text
    assert parse_workbench(workbench_toml(Workbench('empty', 'Empty')), 'empty') == Workbench(
        'empty', 'Empty'
    )


@pytest.mark.parametrize(
    'text, reason',
    [
        ('label = "x"\ncolour = 1\n', 'unknown key colour'),
        ('label = "x"\n[[entry]]\nbase = "b"\n', 'project'),
        ('label = "x"\n[[entry]]\nproject = "a"\n[[entry]]\nproject = "a"\n', 'twice'),
        (
            'label = "x"\n[[entry]]\nproject = "a"\nitems = [{ series = "s", branch = "b" }]\n',
            'one of series, branch',
        ),
        ('label = "x"\n[[entry]]\nproject = "a"\nitems = [{ revision = 2 }]\n', 'one of'),
        ('label = [\n', 'TOML'),
    ],
)
def test_a_malformed_workbench_is_refused_with_the_reason(text, reason):
    with pytest.raises(WorkbenchError, match=reason):
        parse_workbench(text, 'x')


def test_the_store_saves_lists_and_deletes_workbenches_one_file_each(tmp_path):
    store = WorkbenchStore(tmp_path / 'state' / 'workbenches')
    assert store.list() == ([], [])
    store.save(BENCH)
    other = Workbench(store.new_id('Another one'), 'Another one')
    store.save(other)
    assert other.id == 'another-one'
    assert store.new_id('CXL DAX') == 'cxl-dax-2'  # Taken.
    found, problems = store.list()
    assert [w.id for w in found] == ['another-one', 'cxl-dax'] and problems == []
    assert store.get('cxl-dax') == BENCH
    files = sorted(p.name for p in (tmp_path / 'state' / 'workbenches').iterdir())
    assert files == ['another-one.toml', 'cxl-dax.toml']  # No temporary file left behind.
    (tmp_path / 'state' / 'workbenches' / 'broken.toml').write_text('label = [\n')
    found, problems = store.list()
    assert len(found) == 2 and len(problems) == 1 and 'broken' in problems[0]
    store.delete('another-one')
    assert store.get('another-one') is None


def test_series_and_branches_go_under_their_project_in_order():
    bench = Workbench('w', 'W')
    bench = add_series(bench, 'linux', CXL.series, 3, CXL.subject, base='gourry/base')
    bench = add_branch(bench, 'linux', 'gourry/fixes')
    bench = add_series(bench, 'ndctl', DAX.series, 1, DAX.subject)
    assert bench.entries == (
        Entry('linux', 'gourry/base', (CXL, FIXES)),
        Entry('ndctl', '', (DAX,)),
    )
    with pytest.raises(WorkbenchError, match='already'):
        add_series(bench, 'linux', CXL.series, 3, CXL.subject)
    # Another revision may join it, to compare.
    assert add_series(bench, 'linux', CXL.series, 4, CXL.subject).entries[0].items[-1].revision == 4
    # The base given for a new project's entry; an existing entry keeps its own.
    assert add_branch(bench, 'linux', 'more', base='other').entries[0].base == 'gourry/base'


def test_items_move_and_go_and_an_emptied_project_stays():
    bench = move_item(BENCH, 'linux', 1, -1)
    assert bench.entries[0].items == (FIXES, CXL)
    assert move_item(bench, 'linux', 0, -1) == bench  # Already first.
    bench = remove_item(BENCH, 'ndctl', 0)
    assert bench.entries[1] == Entry('ndctl', 'origin/main', ())  # Its tree can still be had.
    assert set_base(BENCH, 'ndctl', 'pending').entries[1].base == 'pending'


def test_a_newer_revision_is_offered_and_taken_only_when_asked():
    latest = {CXL.series: 4, DAX.series: 1}
    assert rerolls(BENCH, latest) == [('linux', 0, 4)]
    assert rerolls(BENCH, {}) == []
    bench = reroll(BENCH, 'linux', 0, 4)
    assert bench.entries[0].items[0] == Item(CXL.series, 4, CXL.subject)
    assert rerolls(bench, latest) == []


def test_two_machines_editing_different_workbenches_both_keep_their_edits():
    import datetime

    from b4_maintainer.adapters.statesync import merge_states

    now = datetime.datetime(2026, 10, 27, tzinfo=datetime.timezone.utc)
    a = workbench_toml(BENCH).encode()
    b = workbench_toml(Workbench('other', 'Other')).encode()
    edited = workbench_toml(set_base(BENCH, 'ndctl', 'pending')).encode()
    base = {'workbenches/cxl-dax.toml': a, 'workbenches/other.toml': b}
    ours = {'workbenches/cxl-dax.toml': edited, 'workbenches/other.toml': b}
    theirs = {'workbenches/cxl-dax.toml': a}  # The other machine deleted "other".
    merged, notes = merge_states(base, ours, theirs, now)
    assert merged == {'workbenches/cxl-dax.toml': edited} and notes == []


def service_root(tmp_path):
    """Two projects with checkouts; linux has a maintainer base and a fixes branch."""
    from gitutil import git

    from b4_maintainer.workspace import Workspace

    for name in ('linux', 'ndctl'):
        repo = tmp_path / 'projects' / name
        repo.mkdir(parents=True)
        git('init', '-q', '-b', 'main', cwd=repo)
        (repo / 'a.c').write_text('a\n')
        git('add', '-A', cwd=repo)
        git(
            '-c',
            'user.name=M',
            '-c',
            'user.email=m@example.org',
            'commit',
            '-q',
            '-m',
            'a',
            cwd=repo,
        )
    linux = tmp_path / 'projects' / 'linux'
    git('branch', 'gourry/base', cwd=linux)
    git('switch', '-q', '-c', 'gourry/fixes', cwd=linux)
    for n in (1, 2):
        (linux / f'f{n}.c').write_text(f'{n}\n')
        git('add', '-A', cwd=linux)
        git(
            '-c',
            'user.name=M',
            '-c',
            'user.email=m@example.org',
            'commit',
            '-q',
            '-m',
            f'f{n}',
            cwd=linux,
        )
    git('switch', '-q', 'main', cwd=linux)
    git('branch', 'b4/review/x', cwd=linux)  # b4's review branches are not offered.
    workspace = Workspace.from_config(
        {
            'projects': [
                {
                    'id': 'linux',
                    'path': 'projects/linux',
                    'refs': [{'label': 'm', 'name': 'main', 'authority': 'mainline'}],
                    'bases': [{'branch': 'gourry/base', 'upstream': 'main'}],
                },
                {'id': 'ndctl', 'path': 'projects/ndctl'},
            ]
        }
    )
    return workspace


def test_the_service_offers_bases_and_branches_and_previews_a_launch(tmp_path):
    from b4_maintainer.fixtures import make_series
    from b4_maintainer.workbench import WorkbenchService

    workspace = service_root(tmp_path)
    store = WorkbenchStore(tmp_path / 'state' / 'workbenches')
    service = WorkbenchService(tmp_path, store, lambda: [])
    assert [v for v, _label in service.base_choices(workspace, 'linux')] == [
        'gourry/base',
        'main',
        'HEAD',
    ]
    branches = [v for v, _label in service.branch_choices(workspace, 'linux')]
    assert 'gourry/fixes' in branches and 'b4/review/x' not in branches
    series = make_series('cxl: region mode', revision=3)
    bench = Workbench(
        'w',
        'W',
        (
            Entry(
                'linux',
                'gourry/base',
                (Item(series.identity, 3, series.subject), Item(branch='gourry/fixes')),
            ),
            Entry('ndctl', '', (Item('gone@example.org', 1, 'daxctl: old'),)),
        ),
    )
    text = service.preview(workspace, bench, {series.identity: series}, {series.identity: 4})
    assert 'linux on gourry/base' in text
    assert '1. cxl: region mode v3' in text and 'v4 available' in text
    assert '2. gourry/fixes · 2 commits beyond the base' in text
    assert 'ndctl on the base b4 chooses' in text
    assert 'daxctl: old v1 · not in the cache' in text


def test_a_workbench_becomes_tree_plans_from_the_cache_and_branches(tmp_path):
    import dataclasses

    from b4_maintainer.fixtures import make_series
    from b4_maintainer.sessions.hub import TreePlan
    from b4_maintainer.sessions.trees import TreeItem
    from b4_maintainer.workbench import WorkbenchService

    workspace = service_root(tmp_path)
    service = WorkbenchService(tmp_path, WorkbenchStore(tmp_path / 'wb'), lambda: [])
    cxl = dataclasses.replace(make_series('cxl: region mode', revision=3), source='/mail/cxl.mbx')
    bench = Workbench(
        'w',
        'W',
        (
            Entry(
                'linux',
                'gourry/base',
                (Item(cxl.identity, 3, cxl.subject), Item(branch='gourry/fixes')),
            ),
        ),
    )
    plans, problems, tree_problems = service.plans(workspace, bench, {cxl.identity: cxl})
    assert problems == [] and tree_problems == []
    assert plans == (
        TreePlan(
            'linux',
            'gourry/base',
            (
                TreeItem('cxl: region mode v3', '/mail/cxl.mbx', 3),
                TreeItem('gourry/fixes', branch='gourry/fixes'),
            ),
        ),
    )
    stale = Workbench('w', 'W', (Entry('linux', '', (Item(cxl.identity, 2, cxl.subject),)),))
    _plans, problems, _tree = service.plans(workspace, stale, {cxl.identity: cxl})
    assert problems == ['cxl: region mode v2: v2 is not in the cache (v takes v3)']
    _plans, problems, _tree = service.plans(workspace, stale, {})
    assert problems == ['cxl: region mode v2: not in the cache (F5 fetches it)']
    _plans, problems, _tree = service.plans(workspace, Workbench('e', 'E'), {})
    assert problems == ['E has nothing to prepare: w adds series to it']


def test_a_series_workbench_is_made_once_and_found_again(tmp_path):
    from b4_maintainer.workbench import series_bench

    store = WorkbenchStore(tmp_path / 'wb')
    bench = series_bench(store, 'linux', 'change-id:x', 3, 'mm: fix the leak', 'gourry/base')
    assert bench.id == 'mm-fix-the-leak' and bench.origin == 'change-id:x'
    assert bench.label == 'mm: fix the leak'
    assert bench.entries == (
        Entry('linux', 'gourry/base', (Item('change-id:x', 3, 'mm: fix the leak'),)),
    )
    assert store.get(bench.id) == bench  # Saved.
    assert parse_workbench(workbench_toml(bench), bench.id) == bench  # origin round-trips.
    again = series_bench(store, 'linux', 'change-id:x', 4, 'mm: fix the leak', '')
    assert again == bench  # Found by its origin, as it is (v on F3 takes v4).
    other = series_bench(store, 'linux', 'change-id:y', 1, 'mm: fix the leak', '')
    assert other.id == 'mm-fix-the-leak-2'


def test_a_workbenchs_contents_come_in_sections(tmp_path):
    import dataclasses

    from gitutil import git
    from sessiondata import make_record

    from b4_maintainer.fixtures import make_series
    from b4_maintainer.sessions.records import Worktree
    from b4_maintainer.workbench import WorkbenchService

    workspace = service_root(tmp_path)
    linux = tmp_path / 'projects' / 'linux'
    base = git('rev-parse', 'main', cwd=linux)
    tree = tmp_path / 'var' / 'worktrees' / 's1'
    git('clone', '-q', '--shared', str(linux), str(tree), cwd=tmp_path)
    git('switch', '-q', '-c', 'b4m/session/s1', base, cwd=tree)
    git('switch', '-q', '-c', 'b4m/fixup/leak', cwd=tree)
    (tree / 'leak.c').write_text('fixed\n')
    git('add', '-A', cwd=tree)
    git(
        '-c',
        'user.name=A',
        '-c',
        'user.email=a@x',
        'commit',
        '-q',
        '-m',
        'mm: plug the leak',
        cwd=tree,
    )
    out = tmp_path / 'sessions' / 's1' / 'work' / 'out'
    out.mkdir(parents=True)
    (out / 'report.md').write_text('B is better.\n')
    (out / 'artifacts.toml').write_text(
        '[[artifact]]\nkind = "analysis"\ntitle = "Compare A and B"\nsummary = "Take B."\n'
        'path = "out/report.md"\n'
    )
    record = make_record(
        's1',
        series='workbench:w',
        workbench='w',
        mode='worktree',
        worktree=Worktree(str(tree), 'b4m/session/s1', base, base),
    )
    cxl = dataclasses.replace(make_series('cxl: region mode', revision=3), source='/mail/cxl.mbx')
    bench = Workbench(
        'w',
        'W',
        (
            Entry(
                'linux',
                'gourry/base',
                (Item(cxl.identity, 3, cxl.subject), Item(branch='gourry/fixes')),
            ),
        ),
    )
    service = WorkbenchService(
        tmp_path,
        WorkbenchStore(tmp_path / 'wb'),
        lambda: [record],
        lambda sid: tmp_path / 'sessions' / sid,
    )
    rows = service.contents(workspace, bench, {cxl.identity: cxl}, {cxl.identity: 4})
    text = [r.text for r in rows]
    kinds = [r.kind for r in rows]
    assert [t for t, k in zip(text, kinds) if k == 'heading'] == [
        'Items',
        'Mailboxes',
        'Trees',
        'Artifacts',
    ]
    assert '  linux on gourry/base' in text
    item = next(r for r in rows if r.kind == 'item' and r.index == 0)
    assert (
        item.project == 'linux'
        and 'cxl: region mode v3' in item.text
        and 'v4 available' in item.text
    )
    mailbox = next(r for r in rows if r.kind == 'mailbox')
    assert mailbox.series == cxl.identity and 'cxl: region mode v3 (linux)' in mailbox.text
    tree_row = next(r for r in rows if r.kind == 'tree')
    assert tree_row.session == 's1' and 'linux' in tree_row.text
    commit = next(r for r in rows if r.kind == 'commit')
    assert 'mm: plug the leak' in commit.text and 'fixup' in commit.text
    assert commit.attachment is not None and commit.attachment.repo == str(tree)
    artifact = next(r for r in rows if r.kind == 'artifact' and 'Compare A and B' in r.text)
    assert artifact.session == 's1' and artifact.attachment is not None


def test_discussions_and_files_are_items_too():
    from b4_maintainer.workbench import add_file, add_mail

    bench = add_mail(
        Workbench('w', 'W'), 'linux', 'talk@example.org', 'RFC: memory tiers', 'talk@example.org'
    )
    bench = add_file(bench, 'linux', '/home/me/fix.patch')
    talk, fix = bench.entries[0].items
    assert (talk.mail, talk.subject, talk.msgid) == (
        'talk@example.org',
        'RFC: memory tiers',
        'talk@example.org',
    )
    assert talk.label == 'RFC: memory tiers (discussion)' and fix.label == 'fix.patch (file)'
    assert parse_workbench(workbench_toml(bench), 'w') == bench
    with_msgid = add_series(bench, 'linux', 'change-id:x', 2, 'mm: x', msgid='x@example.org')
    assert with_msgid.entries[0].items[-1].msgid == 'x@example.org'
    assert parse_workbench(workbench_toml(with_msgid), 'w') == with_msgid
    with pytest.raises(WorkbenchError, match='one of'):
        parse_workbench(
            'label = "x"\n[[entry]]\nproject = "a"\nitems = [{ mail = "m", file = "f" }]\n', 'x'
        )
    assert rerolls(bench, {'talk@example.org': 5}) == []  # Only series take revisions.


def test_discussions_are_read_files_with_patches_applied_and_others_read(tmp_path):
    import dataclasses

    from b4_maintainer.fixtures import make_series, write_series_mbox
    from b4_maintainer.sessions.trees import TreeItem
    from b4_maintainer.workbench import WorkbenchService, add_file, add_mail

    workspace = service_root(tmp_path)
    service = WorkbenchService(tmp_path, WorkbenchStore(tmp_path / 'wb'), lambda: [])
    talk = dataclasses.replace(make_series('RFC: tiers'), source='/mail/talk.mbx', discussion=True)
    patch = write_series_mbox(tmp_path / 'fix.mbx', subject='mm: fix it').path
    notes = tmp_path / 'notes.md'
    notes.write_text('ideas\n')
    bench = add_mail(Workbench('w', 'W'), 'linux', talk.identity, talk.subject)
    bench = add_file(add_file(bench, 'linux', str(patch)), 'linux', str(notes))
    plans, problems, _tree = service.plans(workspace, bench, {talk.identity: talk})
    assert problems == []
    assert plans[0].items == (
        TreeItem('RFC: tiers (discussion)', '/mail/talk.mbx', apply=False),
        TreeItem('fix.mbx (file)', str(patch)),
        TreeItem('notes.md (file)', file=str(notes), apply=False),
    )
    gone = add_file(Workbench('g', 'G'), 'linux', str(tmp_path / 'nope.patch'))
    _plans, problems, _tree = service.plans(workspace, gone, {})
    assert problems == [f'nope.patch (file): {tmp_path / "nope.patch"} does not exist']


def test_an_item_moves_to_another_project():
    from b4_maintainer.workbench import move_to_project

    bench = move_to_project(BENCH, 'linux', 1, 'ndctl')
    assert bench.entries[0].items == (CXL,) and bench.entries[1].items == (DAX, FIXES)
    alone = move_to_project(BENCH, 'ndctl', 0, 'qemu', base='master')
    assert [e.project for e in alone.entries] == ['linux', 'ndctl', 'qemu']  # ndctl stays.
    assert alone.entries[2] == Entry('qemu', 'master', (DAX,))


def test_a_project_joins_without_items_and_leaves_only_when_removed():
    from b4_maintainer.workbench import add_project, remove_project

    bench = add_project(Workbench('w', 'W'), 'ndctl', 'origin/main')
    assert bench.entries == (Entry('ndctl', 'origin/main', ()),)
    assert parse_workbench(workbench_toml(bench), 'w') == bench
    with pytest.raises(WorkbenchError, match='already'):
        add_project(bench, 'ndctl')
    assert remove_project(bench, 'ndctl').entries == ()


def test_a_sessions_artifacts_become_items():
    from sessiondata import make_record

    from b4_maintainer.attachments import Attachment
    from b4_maintainer.sessions.records import ProjectTree, Worktree
    from b4_maintainer.workbench import artifact_item

    tree = Worktree('/t/s1', 'b4m/session/s1', 'b', 'a')
    single = make_record('s1', worktree=tree)
    commit = Attachment(
        'patch',
        'patch · fixup · mm: x',
        's1',
        repo='/t/s1',
        sha='c',
        title='mm: x',
        branch='b4m/fixup/x',
    )
    assert artifact_item(single, commit) == ('linux', Item(branch='tree:s1:b4m/fixup/x'))
    bench = make_record(
        'w1',
        trees=(ProjectTree('ndctl', Worktree('/t/w1/ndctl', 'b4m/session/w1', 'b', 'a')),),
    )
    commit = Attachment(
        'patch',
        'patch · test · t',
        'w1',
        repo='/t/w1/ndctl',
        sha='c',
        title='t',
        branch='b4m/tests/t',
    )
    assert artifact_item(bench, commit) == ('ndctl', Item(branch='tree:w1/ndctl:b4m/tests/t'))
    report = Attachment(
        'file', 'analysis: Compare', 's1', path=pathlib.Path('/o/report.md'), title='Compare'
    )
    assert artifact_item(single, report) == ('', Item(file='/o/report.md'))
    note = Attachment('text', 'finding: x', 's1', title='x', text='Only words.')
    assert artifact_item(single, note) is None


def test_a_single_patch_and_a_single_commit_round_trip_as_items():
    from b4_maintainer.workbench import workbench_toml

    bench = Workbench('w', 'W', ())
    bench = add_series(bench, 'linux', 'change-id:foo', 3, 'mm: foo', patches=(2,))
    bench = add_series(bench, 'linux', 'change-id:foo', 3, 'mm: foo', patches=(4,))  # Another.
    bench = add_branch(bench, 'linux', 'tree:s1:b4m/fixup/x', commit='c' * 40, subject='mm: fix x')
    again = parse_workbench(workbench_toml(bench), 'w')
    assert again == bench
    first, second, single = again.entries[0].items
    assert (first.patches, second.patches) == ((2,), (4,))
    assert first.label == 'mm: foo v3 patch 2'
    assert (
        single.commit == 'c' * 40
        and single.label == f'mm: fix x ({"c" * 12} of tree:s1:b4m/fixup/x)'
    )
    with pytest.raises(WorkbenchError):
        add_series(again, 'linux', 'change-id:foo', 3, 'mm: foo', patches=(2,))  # Already there.


def test_a_single_patch_and_commit_become_tree_items_that_say_so(tmp_path):
    from b4_maintainer.fixtures import make_series
    from b4_maintainer.workbench import WorkbenchService
    from b4_maintainer.workspace import Workspace

    store = WorkbenchStore(tmp_path / 'wb')
    service = WorkbenchService(tmp_path, store, lambda: [])
    series = {'change-id:foo': make_series('mm: foo', revision=3, change_id='foo')}
    series['change-id:foo'] = dataclasses.replace(
        series['change-id:foo'], identity='change-id:foo', source='/mail/foo'
    )
    bench = add_series(
        Workbench('w', 'W', ()), 'linux', 'change-id:foo', 3, 'mm: foo', patches=(2,)
    )
    bench = add_branch(bench, 'linux', 'me/fix', commit='c' * 40, subject='mm: fix x')
    service.branch_repo = lambda workspace, project, branch: ('/repo', branch)
    plans, problems, _tree = service.plans(Workspace.from_config({}), bench, series)
    assert problems == []
    patch, commit = plans[0].items
    assert (patch.source, patch.revision, patch.patches) == ('/mail/foo', 3, (2,))
    assert (commit.branch, commit.single) == ('c' * 40, True)

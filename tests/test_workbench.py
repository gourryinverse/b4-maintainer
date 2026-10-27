"""Contract: workbenches are named recipes for sessions over several projects, one file
each in state/workbenches/ (synced): per project, a base and the series and local
branches applied on it in order."""

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
            'series or a branch',
        ),
        ('label = "x"\n[[entry]]\nproject = "a"\nitems = [{ revision = 2 }]\n', 'series or a'),
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
        add_series(bench, 'linux', CXL.series, 4, CXL.subject)
    # The base given for a new project's entry; an existing entry keeps its own.
    assert add_branch(bench, 'linux', 'more', base='other').entries[0].base == 'gourry/base'


def test_items_move_and_go_and_an_empty_entry_goes_with_them():
    bench = move_item(BENCH, 'linux', 1, -1)
    assert bench.entries[0].items == (FIXES, CXL)
    assert move_item(bench, 'linux', 0, -1) == bench  # Already first.
    bench = remove_item(BENCH, 'ndctl', 0)
    assert [e.project for e in bench.entries] == ['linux']
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
    plans, problems = service.plans(workspace, bench, {cxl.identity: cxl})
    assert problems == []
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
    _plans, problems = service.plans(workspace, stale, {cxl.identity: cxl})
    assert problems == ['cxl: region mode v2: only v3 is cached (v on F4 takes it)']
    _plans, problems = service.plans(workspace, stale, {})
    assert problems == ['cxl: region mode v2: not in the cache (F5 fetches it)']
    _plans, problems = service.plans(workspace, Workbench('e', 'E'), {})
    assert problems == ['E has nothing to prepare: w adds series to it']

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

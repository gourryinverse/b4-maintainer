from b4_maintainer.adapters.fixture import FixtureSource
from b4_maintainer.fixtures import make_series, workspace_config
from b4_maintainer.model import Authority, Coverage, SeriesState
from b4_maintainer.views import ViewIndex
from b4_maintainer.workspace import Workspace, build_items

LINUX_LISTS = ['linux-mm.kvack.org']
QEMU_LISTS = ['qemu-devel.nongnu.org']


def index_for(config, series, states=None):
    ws = Workspace.from_config(config)
    return ViewIndex(ws, build_items(series, states or {}, ws))


def test_zero_profiles_offers_setup_and_keeps_series_visible():
    idx = index_for(workspace_config(profiles=[]), [make_series('mm: a', lists=LINUX_LISTS)])
    assert idx.needs_setup
    assert idx.count('responsibilities') == 0
    assert idx.count('all') == 1
    assert not [v for v in idx.views if v.kind == 'profile']


def test_one_profile_matches_by_list_or_path():
    config = workspace_config(
        profiles=[{'id': 'mm', 'project': 'linux', 'lists': [], 'paths': ['mm/']}]
    )
    series = [
        make_series('mm: by path', lists=LINUX_LISTS, paths=['mm/vmscan.c']),
        make_series('net: elsewhere', lists=LINUX_LISTS, paths=['net/core/dev.c']),
    ]
    idx = index_for(config, series)
    assert [i.series.subject for i in idx.items('profile:mm')] == ['mm: by path']
    assert idx.count('responsibilities') == 1


def test_cross_posted_series_is_one_item_in_both_projects():
    series = [make_series('hw/cxl: shared change', lists=LINUX_LISTS + QEMU_LISTS)]
    idx = index_for(workspace_config(), series)
    [item] = idx.items('all')
    assert item.project is None
    assert item.candidates == ('linux', 'qemu')
    assert idx.items('project:linux')[0] is item
    assert idx.items('project:qemu')[0] is item


def test_counts_match_filtered_rows_in_every_view(demo_root):
    snapshot = FixtureSource(demo_root).load()
    idx = ViewIndex(snapshot.workspace, snapshot.items)
    assert idx.count('all') > 0
    for view in idx.views:
        assert idx.count(view.id) == len(idx.items(view.id)), view.id


def test_mainline_complete_hidden_from_active_views_but_cached():
    landed = make_series('mm: landed', lists=LINUX_LISTS)
    state = SeriesState(tree=(('m', Authority.MAINLINE, Coverage.FULL),), to_review=True)
    idx = index_for(workspace_config(), [landed], {landed.identity: state})
    assert idx.count('all') == 0
    assert idx.count('review') == 0
    assert idx.count('cached') == 1


def test_disabled_profile_preserves_queued_work():
    config = workspace_config(
        profiles=[{'id': 'mm', 'project': 'linux', 'lists': LINUX_LISTS, 'enabled': False}]
    )
    s = make_series('mm: queued', lists=LINUX_LISTS)
    idx = index_for(config, [s], {s.identity: SeriesState(to_review=True)})
    assert idx.count('review') == 1
    assert idx.count('all') == 1
    assert not [v for v in idx.views if v.kind == 'profile']


def test_duplicate_postings_merge_by_identity():
    a = make_series('mm: same', lists=LINUX_LISTS, msgid='same@example.org')
    b = make_series('mm: same', lists=['linux-kernel.vger.kernel.org'], msgid='same@example.org')
    idx = index_for(workspace_config(), [a, b])
    [item] = idx.items('all')
    assert set(item.series.lists) == {'linux-mm.kvack.org', 'linux-kernel.vger.kernel.org'}


def test_reroll_shows_latest_revision_and_keeps_history():
    v1 = make_series('mm: tidy', revision=1, change_id='tidy-1', lists=LINUX_LISTS)
    v2 = make_series('mm: tidy', revision=2, change_id='tidy-1', lists=LINUX_LISTS)
    idx = index_for(workspace_config(), [v2, v1])
    [item] = idx.items('all')
    assert item.series.revision == 2
    assert item.versions == (1, 2)


def test_unmerged_submissions_match_maintainer_identity():
    mine = make_series('mm: mine', lists=LINUX_LISTS, author_email='maintainer@example.org')
    theirs = make_series('mm: theirs', lists=LINUX_LISTS)
    idx = index_for(workspace_config(), [mine, theirs])
    assert [i.series.subject for i in idx.items('unmerged')] == ['mm: mine']


def test_zero_one_and_many_projects():
    s = make_series('mm: a', lists=LINUX_LISTS)
    assert index_for(workspace_config(projects=[]), [s]).items('all')[0].project is None
    one = index_for(workspace_config(projects=['linux']), [s])
    assert one.items('all')[0].project == 'linux'
    assert [v.id for v in one.views if v.kind == 'project'] == ['project:linux']
    many = index_for(workspace_config(), [s])
    assert [v.id for v in many.views if v.kind == 'project'] == ['project:linux', 'project:qemu']

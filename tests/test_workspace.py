import pytest

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


def test_responsibilities_are_maintained_series_not_profile_matches():
    config = workspace_config(profiles=[{'id': 'mm', 'project': 'linux', 'lists': LINUX_LISTS}])
    series = [
        make_series('mm: mine', lists=LINUX_LISTS, paths=['mm/mempolicy.c']),
        make_series('mm: elsewhere', lists=LINUX_LISTS, paths=['mm/vmscan.c']),
    ]
    ws = Workspace.from_config(config)
    mine = {series[0].identity}
    idx = ViewIndex(ws, build_items(series, {}, ws, responsible=mine))
    assert idx.count('profile:mm') == 2
    assert [i.series.subject for i in idx.items('responsibilities')] == ['mm: mine']


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


def test_configured_lists_accept_addresses_or_list_ids():
    config = {
        'projects': [{'id': 'linux', 'lists': ['Linux-MM@kvack.org', 'linux-cxl.vger.kernel.org']}],
        'profiles': [{'id': 'mm', 'project': 'linux', 'lists': ['linux-mm@kvack.org']}],
    }
    ws = Workspace.from_config(config)
    assert ws.project('linux').lists == ('linux-mm.kvack.org', 'linux-cxl.vger.kernel.org')
    [item] = build_items([make_series('mm: a', lists=LINUX_LISTS)], {}, ws)
    assert item.project == 'linux'
    assert item.profiles == {'mm'}


def test_revisions_without_change_id_group_by_subject_and_sender():
    # b4's own fallback for finding other revisions: subject plus sender.
    v1 = make_series('mm: tidy thing', revision=1, msgid='v1@example.org', lists=LINUX_LISTS)
    v2 = make_series('mm: tidy thing', revision=2, msgid='v2@example.org', lists=LINUX_LISTS)
    idx = index_for(workspace_config(), [v1, v2], {v1.identity: SeriesState(to_review=True)})
    [item] = idx.items('all')
    assert item.series.revision == 2
    assert item.versions == (1, 2)
    assert item.state.to_review  # State recorded against an older revision follows it.


def test_change_id_revision_absorbs_earlier_one_without():
    v1 = make_series('mm: tidy thing', revision=1, msgid='v1@example.org', lists=LINUX_LISTS)
    v2 = make_series('mm: tidy thing', revision=2, change_id='tidy-2', lists=LINUX_LISTS)
    [item] = index_for(workspace_config(), [v1, v2]).items('all')
    assert item.identity == 'change-id:tidy-2'
    assert item.versions == (1, 2)


def test_same_subject_from_different_senders_stays_separate():
    a = make_series('mm: fix typo', msgid='a@example.org', lists=LINUX_LISTS)
    b = make_series(
        'mm: fix typo', msgid='b@example.org', lists=LINUX_LISTS, author_email='x@example.org'
    )
    assert index_for(workspace_config(), [a, b]).count('all') == 2


def test_different_change_ids_stay_separate_despite_subject():
    a = make_series('mm: same words', revision=1, change_id='one', lists=LINUX_LISTS)
    b = make_series('mm: same words', revision=1, change_id='two', lists=LINUX_LISTS)
    assert index_for(workspace_config(), [a, b]).count('all') == 2


def test_hidden_series_leave_every_view_but_hidden():
    import dataclasses

    ws = Workspace.from_config(
        workspace_config(profiles=[{'id': 'mm', 'project': 'linux', 'lists': LINUX_LISTS}])
    )
    keep, drop = build_items(
        [
            make_series('mm: keep', msgid='k@example.org', lists=LINUX_LISTS),
            make_series('mm: drop', msgid='d@example.org', lists=LINUX_LISTS),
        ],
        {},
        ws,
    )
    drop = dataclasses.replace(drop, hidden=True, state=SeriesState(to_review=True))
    idx = ViewIndex(ws, [keep, drop])
    assert list(idx.items('hidden')) == [drop]
    for view in idx.views:
        if view.id != 'hidden':
            assert drop not in idx.items(view.id), view.id
    assert keep in idx.items('profile:mm') and keep in idx.items('cached')
    assert 'hidden' not in [v.id for v in ViewIndex(ws, [keep]).views]


def test_responses_view_lists_items_with_unread_replies():
    import dataclasses

    ws = Workspace.from_config(workspace_config())
    quiet, answered = build_items(
        [
            make_series('mm: quiet', msgid='q@example.org', lists=LINUX_LISTS),
            make_series('mm: answered', msgid='a@example.org', lists=LINUX_LISTS),
        ],
        {},
        ws,
    )
    answered = dataclasses.replace(answered, unread_msgids=frozenset({'reply@example.org'}))
    idx = ViewIndex(ws, [quiet, answered])
    assert [i.series.subject for i in idx.items('responses')] == ['mm: answered']
    labels = [v.label for v in idx.views]
    assert labels.index('Responses') < labels.index('My Responsibilities')


def test_items_remember_every_postings_thread():
    v1 = make_series('mm: tidy thing', revision=1, msgid='v1@example.org', lists=LINUX_LISTS)
    v2 = make_series('mm: tidy thing', revision=2, msgid='v2@example.org', lists=LINUX_LISTS)
    v1 = __import__('dataclasses').replace(v1, source='/cache/threads/aaa')
    v2 = __import__('dataclasses').replace(v2, source='/cache/threads/bbb')
    [item] = build_items([v1, v2], {}, Workspace.from_config(workspace_config()))
    assert set(item.sources) == {'/cache/threads/aaa', '/cache/threads/bbb'}


def test_refs_end_review_by_default_only_for_mainline():
    config = workspace_config()
    config['projects'][0]['refs'] = [
        {'label': 'm', 'name': 'master', 'authority': 'mainline'},
        {'label': 'n', 'name': 'next/master', 'authority': 'acceptance', 'ends_review': True},
        {'label': 's', 'name': 'mm/mm-stable', 'authority': 'acceptance'},
    ]
    refs = Workspace.from_config(config).projects[0].refs
    assert [r.ends_review for r in refs] == [True, True, False]


def test_an_unknown_ref_authority_names_the_allowed_ones():
    config = workspace_config()
    config['projects'][0]['refs'] = [{'label': 'g', 'name': 'me/base', 'authority': 'review'}]
    with pytest.raises(ValueError, match='me/base.*review.*mainline, acceptance, preliminary'):
        Workspace.from_config(config)


def test_landed_series_leave_every_queue_but_cached_and_responses():
    import dataclasses

    ws = Workspace.from_config(
        workspace_config(profiles=[{'id': 'mm', 'project': 'linux', 'lists': LINUX_LISTS}])
    )
    keep, landed = build_items(
        [
            make_series('mm: in review', msgid='k@example.org', lists=LINUX_LISTS),
            make_series('mm: merged', msgid='m@example.org', lists=LINUX_LISTS),
        ],
        {},
        ws,
    )
    landed = dataclasses.replace(
        landed,
        landed=True,
        responsible=True,
        state=SeriesState(to_review=True),
        unread_msgids=frozenset({'reply@example.org'}),
    )
    idx = ViewIndex(ws, [keep, landed])
    for view in ('all', 'project:linux', 'profile:mm', 'responsibilities', 'review'):
        assert landed not in idx.items(view), view
    assert landed in idx.items('cached')
    assert landed in idx.items('responses')


def test_projects_carry_their_maintainer_bases():
    from b4_maintainer.workspace import MaintainerBase

    config = {
        'projects': [
            {
                'id': 'linux',
                'bases': [
                    {'branch': 'me/base', 'upstream': 'mm/mm-stable'},
                    {'branch': 'me/linus', 'upstream': 'mainline/master'},
                ],
            },
            {'id': 'qemu'},
        ]
    }
    ws = Workspace.from_config(config)
    assert ws.projects[0].bases == (
        MaintainerBase('me/base', 'mm/mm-stable'),
        MaintainerBase('me/linus', 'mainline/master'),
    )
    assert ws.projects[1].bases == ()


def test_sandbox_priorities_are_configurable():
    ws = Workspace.from_config({'sessions': {'sandbox_nice': 15, 'sandbox_ionice': 'idle'}})
    assert (ws.sandbox_nice, ws.sandbox_ionice) == (15, 'idle')
    default = Workspace.from_config({})
    assert (default.sandbox_nice, default.sandbox_ionice) == (10, 'low')


def test_unknown_config_keys_are_reported_with_where_they_belong():
    config = {
        'projects': [
            {
                'id': 'linux',
                'refs': [
                    {
                        'label': 'm',
                        'name': 'mainline/master',
                        'authority': 'mainline',
                        'colour': 'red',
                    }
                ],
                'bases': [{'branch': 'me/base', 'upstream': 'up/stable', 'onto': 'x'}],
            }
        ],
        'profiles': [
            {
                'id': 'mm',
                'project': 'linux',
                'bases': [{'branch': 'me/base', 'upstream': 'mm/mm-stable'}],
            }
        ],
        'sessions': {'max_live': 2, 'max_bulds': 3},
        'models': [{'id': 'deep', 'model': 'a/b', 'temperature': 1}],
        'sashiko': {'senders': []},
        'colours': {},
    }
    warnings = Workspace.from_config(config).warnings
    assert warnings == (
        'unknown key colours (top level)',
        'unknown key colour in [[projects]] linux refs',
        'unknown key onto in [[projects]] linux bases',
        'unknown key bases in [[profiles]] mm (it is a [[projects]] setting)',
        'unknown key max_bulds in [sessions]',
        'unknown key temperature in [[models]] deep',
    )
    assert Workspace.from_config({}).warnings == ()


def test_the_assisted_by_trailer_is_configurable():
    assert Workspace.from_config({}).assisted_by == '{model}'
    ws = Workspace.from_config({'sessions': {'assisted_by': 'Claude:{model}'}})
    assert ws.assisted_by == 'Claude:{model}' and ws.warnings == ()


def test_guidance_sources_can_point_at_other_checkouts():
    ws = Workspace.from_config(
        {'guidance': [{'id': 'review-prompts', 'path': '~/git/review-prompts'}]}
    )
    assert ws.guidance == (('review-prompts', '~/git/review-prompts'),) and ws.warnings == ()
    bad = Workspace.from_config({'guidance': [{'id': 'kernel-style', 'path': 'x', 'url': 'y'}]})
    assert bad.warnings == ('unknown key url in [[guidance]] kernel-style',)


def test_a_subject_tag_routes_a_series_on_a_shared_list_to_its_project():
    import dataclasses

    config = {
        'projects': [
            {'id': 'linux', 'lists': ['linux-cxl@vger.kernel.org', 'nvdimm@lists.linux.dev']},
            {
                'id': 'ndctl',
                'lists': ['linux-cxl@vger.kernel.org', 'nvdimm@lists.linux.dev'],
                'subject_tags': ['ndctl'],
            },
        ]
    }
    ws = Workspace.from_config(config)
    assert ws.warnings == () and ws.project('ndctl').subject_tags == ('ndctl',)
    lists = ('nvdimm.lists.linux.dev', 'linux-cxl.vger.kernel.org')
    kernel = make_series('cxl: fix the region mode', lists=lists)
    tool = dataclasses.replace(
        make_series('cxl/list: show the region mode', lists=lists), prefixes=('ndctl', 'patch')
    )
    items = {i.series.subject: i for i in build_items([kernel, tool], {}, ws)}
    assert items['cxl: fix the region mode'].project == 'linux'
    assert items['cxl/list: show the region mode'].project == 'ndctl'
    assert items['cxl/list: show the region mode'].candidates == ()


def test_an_untagged_series_on_shared_lists_goes_where_its_files_are():
    """No subject tag: every project on its lists is a candidate, and the one whose tree
    has the directories its patches touch takes it; with no such evidence the untagged
    project keeps it, as before."""
    config = {
        'projects': [
            {'id': 'linux', 'lists': ['linux-cxl@vger.kernel.org']},
            {
                'id': 'ndctl',
                'lists': ['linux-cxl@vger.kernel.org', 'nvdimm@lists.linux.dev'],
                'subject_tags': ['ndctl'],
            },
        ]
    }
    ws = Workspace.from_config(config)
    layouts = {
        'linux': frozenset({'drivers', 'drivers/cxl', 'tools', 'tools/testing'}),
        'ndctl': frozenset({'daxctl', 'daxctl/lib', 'test', 'cxl'}),
    }
    lists = ('nvdimm.lists.linux.dev', 'linux-cxl.vger.kernel.org')
    tool = make_series(
        'daxctl: use whole-device hotplug',
        lists=lists,
        paths=['daxctl/lib/libdaxctl.c', 'daxctl/device.c', 'test/daxctl-create.sh'],
    )
    kernel = make_series('cxl: fix a region', lists=lists, paths=['drivers/cxl/region.c'])
    unknown = make_series('cxl: no paths known', lists=lists)
    items = {
        i.series.subject: i for i in build_items([tool, kernel, unknown], {}, ws, layouts=layouts)
    }
    assert items['daxctl: use whole-device hotplug'].project == 'ndctl'
    assert items['cxl: fix a region'].project == 'linux'
    assert items['cxl: no paths known'].project == 'linux'  # Untagged keeps it.
    # Without layouts (no checkouts read), routing is as it was.
    plain = {i.series.subject: i for i in build_items([tool], {}, ws)}
    assert plain['daxctl: use whole-device hotplug'].project == 'linux'


def test_a_profile_named_like_its_project_stands_in_for_the_project_view():
    config = {
        'projects': [
            {'id': 'linux', 'label': 'Linux', 'lists': ['linux-mm@kvack.org']},
            {'id': 'ndctl', 'lists': ['nvdimm@lists.linux.dev'], 'subject_tags': ['ndctl']},
        ],
        'profiles': [
            {'id': 'mm', 'label': 'MM', 'project': 'linux', 'lists': ['linux-mm@kvack.org']},
            {'id': 'ndctl', 'project': 'ndctl', 'lists': ['nvdimm@lists.linux.dev']},
        ],
    }
    ws = Workspace.from_config(config)
    labels = [v.label for v in ViewIndex(ws, ()).views]
    assert labels.count('ndctl') == 1 and 'profile:ndctl' in [v.id for v in ViewIndex(ws, ()).views]
    assert 'Linux' in labels and 'MM' in labels  # Different labels: both kept.

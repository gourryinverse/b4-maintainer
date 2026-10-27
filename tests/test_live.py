"""Contract: the live workspace browses cached list mail and tracks through b4."""

import dataclasses
import datetime
import email.message
import email.utils

import b4
import pytest
from gitutil import make_checkout

from b4_maintainer.adapters.b4state import Tracked
from b4_maintainer.adapters.live import LiveSource
from b4_maintainer.adapters.lorelist import MailCache
from b4_maintainer.fixtures import PatchSpec, write_series_mbox
from b4_maintainer.views import ViewIndex

NOW = datetime.datetime(2026, 10, 21, 12, 0, tzinfo=datetime.timezone.utc)
MM = 'linux-mm.kvack.org'
CXL = 'linux-cxl.vger.kernel.org'

CONFIG = """\
[[projects]]
id = "demo"
path = "projects/demo"
lists = ["linux-mm@kvack.org", "linux-cxl@vger.kernel.org"]

[[profiles]]
id = "mm"
label = "MM"
project = "demo"
lists = ["linux-mm@kvack.org"]

[browse]
window_days = 10
"""


def mail(tmp_path, name, subject, days_ago=1, lists=(MM,)):
    path = tmp_path / 'src' / f'{name}.mbx'
    path.parent.mkdir(exist_ok=True)
    date = NOW - datetime.timedelta(days=days_ago)
    sent = write_series_mbox(
        path, subject, patches=[PatchSpec('mm/a.c'), PatchSpec('mm/b.c')], date=date, lists=lists
    )
    return sent, b4.get_msgs_from_mailbox_or_maildir(str(path))


class Fakes:
    def __init__(self):
        self.tracked: list[Tracked] = []
        self.searches: list[tuple[str, str]] = []
        self.answers: dict[str, list] = {}
        self.mine: list = []
        self.track_calls: list[tuple[str, str]] = []
        self.archive_calls: list[tuple[str, str, str, int]] = []
        self.archive_result = (True, '/archive.tar.gz')
        self.unarchive_calls: list[tuple[str, str, str]] = []

    def tracked_series(self, checkout, identifier):
        return list(self.tracked)

    def search_for(self, checkout):
        def search(query):
            self.searches.append((checkout.name, query))
            if 'tc:' in query:
                return list(self.mine)  # Mail from or addressed to the maintainer.
            return next((v for k, v in self.answers.items() if f'l:{k} ' in query), [])

        return search

    def track(self, checkout, msgid, command):
        self.track_calls.append((checkout.name, msgid))

    def archive(self, checkout, identifier, change_id, revision):
        self.archive_calls.append((checkout.name, identifier, change_id, revision))
        return self.archive_result

    def unarchive(self, checkout, identifier, change_id):
        self.unarchive_calls.append((checkout.name, identifier, change_id))


@pytest.fixture
def live(tmp_path):
    root = tmp_path / 'ws'
    make_checkout(root / 'projects' / 'demo')
    (root / 'workspace.toml').write_text(CONFIG)
    fakes = Fakes()
    source = LiveSource(
        root,
        tracked_series=fakes.tracked_series,
        search_for=fakes.search_for,
        track=fakes.track,
        b4_command=['b4'],
        now=lambda: NOW,
        identity=lambda checkout: None,
        archive=fakes.archive,
        unarchive=fakes.unarchive,
    )
    return source, fakes, root


def tracked(msgid, subject='mm: tracked', change_id='cid-1'):
    return Tracked(
        change_id=change_id,
        revision=1,
        subject=subject,
        author_name='Contributor',
        author_email='contributor@example.org',
        sent=NOW,
        msgid=msgid,
        expected=2,
        status='new',
        msgids=frozenset([msgid]),
    )


def test_refresh_fetches_profile_lists_into_the_workspace_cache(live, tmp_path):
    source, fakes, root = live
    _, msgs = mail(tmp_path, 'a', 'mm: browse me')
    fakes.answers[MM] = msgs
    summary = source.refresh()
    assert [name for name, _ in fakes.searches] == ['demo']
    assert f'l:{MM} ' in fakes.searches[0][1]
    assert '3 new' in summary
    assert MailCache(root / 'var' / 'mail').sources()


def test_load_lists_cached_series_in_profile_views(live, tmp_path):
    source, fakes, _ = live
    fakes.answers[MM] = mail(tmp_path, 'a', 'mm: browse me')[1]
    source.refresh()
    snapshot = source.load()
    index = ViewIndex(snapshot.workspace, snapshot.items)
    assert [i.series.subject for i in index.items('profile:mm')] == ['mm: browse me']
    assert index.count('review') == 0
    assert snapshot.items[0].project == 'demo'


def test_tracked_series_appear_in_to_review(live, tmp_path):
    source, fakes, _ = live
    sent, msgs = mail(tmp_path, 'a', 'mm: tracked one')
    fakes.answers[MM] = msgs + mail(tmp_path, 'b', 'mm: untracked')[1]
    source.refresh()
    fakes.tracked = [tracked(sent.msgids[0])]
    index = ViewIndex(*_snap(source))
    assert [i.series.subject for i in index.items('review')] == ['mm: tracked one']
    assert index.count('profile:mm') == 2


def test_archived_series_are_not_in_to_review(live, tmp_path):
    source, fakes, _ = live
    sent, msgs = mail(tmp_path, 'a', 'mm: done with this')
    fakes.answers[MM] = msgs
    source.refresh()
    fakes.tracked = [dataclasses.replace(tracked(sent.msgids[0]), status='archived')]
    index = ViewIndex(*_snap(source))
    assert index.count('review') == 0
    assert index.count('profile:mm') == 1


def test_untrack_archives_through_b4(live, tmp_path):
    source, fakes, _ = live
    sent, msgs = mail(tmp_path, 'a', 'mm: untrack me')
    fakes.answers[MM] = msgs
    source.refresh()
    fakes.tracked = [tracked(sent.msgids[0], change_id='cid-9')]
    [item] = source.load().items
    assert 'Archived' in source.untrack(item)
    assert fakes.archive_calls == [('demo', 'demo', 'cid-9', 1)]

    fakes.archive_result = (False, 'branch is checked out')
    with pytest.raises(RuntimeError, match='branch is checked out'):
        source.untrack(item)


def test_untrack_requires_tracking(live, tmp_path):
    source, fakes, _ = live
    fakes.answers[MM] = mail(tmp_path, 'a', 'mm: never tracked')[1]
    source.refresh()
    [item] = source.load().items
    with pytest.raises(RuntimeError, match='not tracked'):
        source.untrack(item)


def test_tracked_series_outside_the_cache_still_show(live):
    source, fakes, _ = live
    fakes.tracked = [tracked('gone@example.org', subject='mm: older series')]
    index = ViewIndex(*_snap(source))
    [item] = index.items('review')
    assert item.series.subject == 'mm: older series'
    assert item.project == 'demo'
    assert item.state.to_review


def test_track_runs_in_the_items_project(live, tmp_path):
    source, fakes, _ = live
    sent, msgs = mail(tmp_path, 'a', 'mm: track me')
    fakes.answers[MM] = msgs
    source.refresh()
    [item] = source.load().items
    message = source.track(item)
    assert fakes.track_calls == [('demo', sent.msgids[0])]
    assert 'mm: track me' in message


def test_tracking_an_untracked_series_again_brings_it_back(live, tmp_path):
    source, fakes, _ = live
    sent, msgs = mail(tmp_path, 'a', 'mm: changed my mind')
    fakes.answers[MM] = msgs
    source.refresh()
    fakes.tracked = [
        dataclasses.replace(tracked(sent.msgids[0], change_id='cid-7'), status='archived')
    ]
    [item] = source.load().items
    assert 'mm: changed my mind' in source.track(item)
    assert fakes.unarchive_calls == [('demo', 'demo', 'cid-7')]
    assert fakes.track_calls == []  # b4 review track refuses archived series.


def test_track_requires_a_project(live, tmp_path):
    source, fakes, root = live
    (root / 'workspace.toml').write_text(
        CONFIG.replace('lists = ["linux-mm@kvack.org", ', 'lists = [')
    )
    fakes.answers[CXL] = mail(tmp_path, 'a', 'other: unrouted', lists=('other.example.org',))[1]
    fakes.answers[MM] = fakes.answers[CXL]
    source.refresh()
    [item] = source.load().items
    assert item.project is None
    with pytest.raises(RuntimeError, match='project'):
        source.track(item)
    assert fakes.track_calls == []


def test_no_workspace_config_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        LiveSource(tmp_path).load()


def _snap(source):
    snapshot = source.load()
    return snapshot.workspace, snapshot.items


def test_without_profiles_nothing_is_fetched(live):
    source, fakes, root = live
    (root / 'workspace.toml').write_text(CONFIG.split('[[profiles]]')[0])
    assert 'profiles' in source.refresh()
    assert fakes.searches == []


GET_MAINTAINER = """\
#!/bin/sh
input=$(cat)
echo "$input" | grep -q 'mm/mempolicy' && echo "Me <me@example.org>"
echo "Akpm Example <akpm@example.org>"
exit 0
"""


def with_maintainers(root, contributor):
    checkout = root / 'projects' / 'demo'
    (checkout / 'scripts').mkdir()
    script = checkout / 'scripts' / 'get_maintainer.pl'
    script.write_text(GET_MAINTAINER)
    script.chmod(0o755)
    (checkout / 'MAINTAINERS').write_text(
        f'NUMA\nM:\tMe <me@example.org>\nR:\tC <{contributor}>\nF:\tmm/mempolicy.c\n'
    )


def test_responsibilities_follow_maintainers_entries(tmp_path):
    from b4_maintainer.fixtures import CONTRIBUTOR

    root = tmp_path / 'ws'
    make_checkout(root / 'projects' / 'demo')
    (root / 'workspace.toml').write_text(CONFIG)
    with_maintainers(root, CONTRIBUTOR[1])
    fakes = Fakes()
    source = LiveSource(
        root,
        tracked_series=fakes.tracked_series,
        search_for=fakes.search_for,
        track=fakes.track,
        b4_command=['b4'],
        now=lambda: NOW,
        identity=lambda checkout: 'Me@Example.org',
    )
    mine = tmp_path / 'src' / 'mine.mbx'
    mine.parent.mkdir(exist_ok=True)
    date = NOW - datetime.timedelta(days=1)
    write_series_mbox(mine, 'mm: mempolicy fix', patches=[PatchSpec('mm/mempolicy.c')], date=date)
    other = mail(tmp_path, 'b', 'mm: vmscan thing')[1]
    fakes.answers[MM] = b4.get_msgs_from_mailbox_or_maildir(str(mine)) + other
    assert 'MAINTAINERS' in source.refresh()

    snapshot = source.load()
    index = ViewIndex(snapshot.workspace, snapshot.items)
    assert [i.series.subject for i in index.items('responsibilities')] == ['mm: mempolicy fix']
    assert index.count('profile:mm') == 2
    assert snapshot.workspace.me == ('me@example.org',)
    assert {i.author_role for i in snapshot.items} == {'reviewer'}


def test_hide_toggles_and_persists_in_client_state(live, tmp_path):
    source, fakes, root = live
    fakes.answers[MM] = mail(tmp_path, 'a', 'mm: noise')[1] + mail(tmp_path, 'b', 'mm: keep')[1]
    source.refresh()
    noise = next(i for i in source.load().items if i.series.subject == 'mm: noise')
    assert 'Hidden' in source.toggle_hidden(noise)
    assert (root / 'state' / 'client' / 'hidden.json').exists()
    items = {i.series.subject: i for i in source.load().items}
    assert items['mm: noise'].hidden and not items['mm: keep'].hidden
    assert 'Unhidden' in source.toggle_hidden(items['mm: noise'])
    assert not any(i.hidden for i in source.load().items)


def reply_to(parent, sender, msgid, days_ago=0):
    reply = email.message.EmailMessage()
    reply['From'] = sender
    reply['Subject'] = 'Re: ' + str(parent['Subject'])
    reply['Date'] = email.utils.format_datetime(NOW - datetime.timedelta(days=days_ago))
    reply['Message-ID'] = f'<{msgid}>'
    reply['In-Reply-To'] = str(parent['Message-ID'])
    refs = str(parent.get('References', '')).split() + [str(parent['Message-ID'])]
    reply['References'] = ' '.join(refs)
    reply.set_content('Some comment.\n')
    return reply


class SeenStore:
    def __init__(self):
        self.flags: set[str] = set()

    def seen(self, checkout, msgids):
        return self.flags & set(msgids)

    def mark_seen(self, checkout, dated):
        self.flags |= set(dated)


def responses_source(tmp_path):
    root = tmp_path / 'ws'
    make_checkout(root / 'projects' / 'demo')
    (root / 'workspace.toml').write_text(CONFIG)
    fakes, store = Fakes(), SeenStore()
    source = LiveSource(
        root,
        tracked_series=fakes.tracked_series,
        search_for=fakes.search_for,
        track=fakes.track,
        b4_command=['b4'],
        now=lambda: NOW,
        identity=lambda checkout: 'me@example.org',
        seen=store.seen,
        mark_seen=store.mark_seen,
    )
    return source, fakes, store


def test_refresh_fetches_my_own_mail(tmp_path):
    source, fakes, _ = responses_source(tmp_path)
    source.refresh()
    queries = [q for _, q in fakes.searches]
    assert any(q.startswith('(f:me@example.org OR tc:me@example.org) AND rt:') for q in queries)


def test_unread_replies_to_my_mail_make_responses(tmp_path):
    source, fakes, store = responses_source(tmp_path)
    # My own series, with a reply from a reviewer.
    _, mine = mail(tmp_path, 'mine', 'mm: my series')
    for m in mine:
        m.replace_header('From', 'Me <me@example.org>')
    cover = mine[0]
    # Someone else's series where I replied, and someone answered me.
    _, theirs = mail(tmp_path, 'theirs', 'mm: their series')
    patch = next(m for m in theirs if '1/2' in str(m['Subject']))
    my_review = reply_to(patch, 'Me <me@example.org>', 'my-review@example.org')
    answer = reply_to(my_review, 'Contributor <contributor1@example.org>', 'answer@example.org')
    # A reply to someone else is not a response to me.
    aside = reply_to(patch, 'Bystander <by@example.org>', 'aside@example.org')
    review = reply_to(cover, 'Reviewer <reviewer@example.org>', 'review-of-mine@example.org')
    fakes.answers[MM] = mine + theirs + [my_review, answer, aside, review]
    source.refresh()

    index = ViewIndex(*_snap(source))
    responses = {i.series.subject: i for i in index.items('responses')}
    assert set(responses) == {'mm: my series', 'mm: their series'}
    assert responses['mm: their series'].unread_msgids == {'answer@example.org'}
    assert responses['mm: my series'].state.unread == 1

    source.mark_seen(['answer@example.org'])
    assert 'answer@example.org' in store.flags
    index = ViewIndex(*_snap(source))
    assert [i.series.subject for i in index.items('responses')] == ['mm: my series']


def test_mark_read_covers_every_message_in_the_series(tmp_path):
    source, fakes, store = responses_source(tmp_path)
    _, mine = mail(tmp_path, 'mine', 'mm: my series')
    for m in mine:
        m.replace_header('From', 'Me <me@example.org>')
    review = reply_to(mine[0], 'Reviewer <reviewer@example.org>', 'review@example.org')
    fakes.answers[MM] = mine + [review] + mail(tmp_path, 'other', 'mm: other')[1]
    source.refresh()
    item = next(i for i in source.load().items if i.series.subject == 'mm: my series')
    assert 'Marked 4' in source.mark_read(item)
    assert 'review@example.org' in store.flags
    assert len(store.flags) == 4  # The other series stays untouched.
    assert ViewIndex(*_snap(source)).count('responses') == 0


STAGED_CONFIG = """\
[[projects]]
id = "demo"
path = "projects/demo"
lists = ["linux-mm@kvack.org"]
refs = [
  { label = "m", name = "master", authority = "mainline" },
  { label = "u", name = "unstable", authority = "preliminary", view = "MM-unstable" },
  { label = "a", name = "new", authority = "preliminary", view = "MM-new" },
]

[[profiles]]
id = "mm"
label = "MM"
project = "demo"
lists = ["linux-mm@kvack.org"]
"""


def test_staged_series_move_from_profile_to_ref_views(tmp_path):
    from gitutil import git

    root = tmp_path / 'ws'
    checkout = make_checkout(root / 'projects' / 'demo')
    (root / 'workspace.toml').write_text(STAGED_CONFIG)
    fakes = Fakes()
    source = LiveSource(
        root,
        tracked_series=fakes.tracked_series,
        search_for=fakes.search_for,
        track=fakes.track,
        b4_command=['b4'],
        now=lambda: NOW,
        identity=lambda c: None,
    )
    in_unstable, msgs_u = mail(tmp_path, 'u', 'mm: in unstable')
    in_new, msgs_n = mail(tmp_path, 'n', 'mm: only in new')
    nowhere, msgs_o = mail(tmp_path, 'o', 'mm: nowhere yet')
    fakes.answers[MM] = msgs_u + msgs_n + msgs_o
    ident = ['-c', 'user.name=A', '-c', 'user.email=a@example.org']

    def pick(sent, subject):
        for n in (1, 2):
            link = f'Link: https://lore.kernel.org/{sent.msgids[n]}'
            git(
                *ident,
                'commit',
                '-q',
                '--allow-empty',
                '-m',
                f'{subject} {n}\n\n{link}',
                cwd=checkout,
            )

    git('checkout', '-q', '-b', 'unstable', cwd=checkout)
    pick(in_unstable, 'mm: in unstable')
    git('checkout', '-q', '-b', 'new', cwd=checkout)
    pick(in_new, 'mm: only in new')
    git('checkout', '-q', 'master', cwd=checkout)

    source.refresh()  # Local branches: scanned on load as well.
    snapshot = source.load()
    index = ViewIndex(snapshot.workspace, snapshot.items)
    labels = {v.id: v.label for v in index.views}
    assert labels['ref:demo:unstable'] == 'MM-unstable' and labels['ref:demo:new'] == 'MM-new'
    assert [i.series.subject for i in index.items('ref:demo:unstable')] == ['mm: in unstable']
    assert [i.series.subject for i in index.items('ref:demo:new')] == ['mm: only in new']
    assert [i.series.subject for i in index.items('profile:mm')] == ['mm: nowhere yet']
    staged = next(i for i in snapshot.items if i.series.subject == 'mm: in unstable')
    assert [(label, cov.value) for label, _, cov in staged.state.tree] == [
        ('m', 'none'),
        ('u', 'full'),
        ('a', 'full'),
    ]
    # A series applied to a local branch shows in its view on the next load, without F5.
    git('checkout', '-q', 'unstable', cwd=checkout)
    pick(nowhere, 'mm: nowhere yet')
    git('checkout', '-q', 'master', cwd=checkout)
    snapshot = source.load()
    index = ViewIndex(snapshot.workspace, snapshot.items)
    assert sorted(i.series.subject for i in index.items('ref:demo:unstable')) == [
        'mm: in unstable',
        'mm: nowhere yet',
    ]
    assert index.items('profile:mm') == ()


NEXT_CONFIG = """\
[[projects]]
id = "demo"
path = "projects/demo"
lists = ["linux-mm@kvack.org"]
refs = [
  { label = "m", name = "master", authority = "mainline" },
  { label = "n", name = "next", authority = "acceptance", ends_review = true },
]

[[profiles]]
id = "mm"
label = "MM"
project = "demo"
lists = ["linux-mm@kvack.org"]
"""


def test_series_fully_in_linux_next_leave_the_queues(tmp_path):
    from gitutil import git

    root = tmp_path / 'ws'
    checkout = make_checkout(root / 'projects' / 'demo')
    (root / 'workspace.toml').write_text(NEXT_CONFIG)
    fakes = Fakes()
    source = LiveSource(
        root,
        tracked_series=fakes.tracked_series,
        search_for=fakes.search_for,
        track=fakes.track,
        b4_command=['b4'],
        now=lambda: NOW,
        identity=lambda c: None,
    )
    whole, msgs_w = mail(tmp_path, 'w', 'mm: whole series in next')
    half, msgs_h = mail(tmp_path, 'h', 'mm: half in next')
    _, msgs_o = mail(tmp_path, 'o', 'mm: still under review')
    fakes.answers[MM] = msgs_w + msgs_h + msgs_o
    ident = ['-c', 'user.name=A', '-c', 'user.email=a@example.org']
    git('checkout', '-q', '-b', 'next', cwd=checkout)
    for sent, counters in ((whole, (1, 2)), (half, (1,))):
        for n in counters:
            link = f'Link: https://lore.kernel.org/{sent.msgids[n]}'
            git(
                *ident, 'commit', '-q', '--allow-empty', '-m', f'picked {n}\n\n{link}', cwd=checkout
            )
    git('checkout', '-q', 'master', cwd=checkout)

    source.refresh()
    snapshot = source.load()
    index = ViewIndex(snapshot.workspace, snapshot.items)
    assert sorted(i.series.subject for i in index.items('profile:mm')) == [
        'mm: half in next',
        'mm: still under review',
    ]
    assert 'mm: whole series in next' not in [i.series.subject for i in index.items('all')]
    assert 'mm: whole series in next' in [i.series.subject for i in index.items('cached')]


def test_series_with_saved_drafts_are_marked_and_listed(live, tmp_path):
    source, fakes, _ = live
    sent, msgs = mail(tmp_path, 'a', 'mm: drafting')
    fakes.answers[MM] = msgs + mail(tmp_path, 'b', 'mm: quiet')[1]
    source.refresh()
    index = ViewIndex(*_snap(source))
    assert 'drafts' not in {v.id for v in index.views}

    source.drafts.save(sent.msgids[1], 'quote', 'quote and my reply')
    index = ViewIndex(*_snap(source))
    assert [i.series.subject for i in index.items('drafts')] == ['mm: drafting']
    assert index.items('drafts')[0].state.draft


def test_series_that_cc_stable_are_stable_pending(live, tmp_path):
    source, fakes, _ = live
    path = tmp_path / 'src' / 'fix.mbx'
    path.parent.mkdir(exist_ok=True)
    patches = [
        PatchSpec('mm/a.c'),
        PatchSpec('mm/b.c', trailers=('Cc: stable@vger.kernel.org',)),
    ]
    write_series_mbox(path, 'mm: fix it', patches=patches, date=NOW, lists=(MM,))
    fixes_only = tmp_path / 'src' / 'fixes.mbx'
    tagged = [PatchSpec('mm/c.c', trailers=('Fixes: 0123456789ab ("mm: the bug")',))]
    write_series_mbox(fixes_only, 'mm: fix, no stable', patches=tagged, date=NOW, lists=(MM,))
    plain, msgs = mail(tmp_path, 'plain', 'mm: a feature')
    fakes.answers[MM] = (
        msgs
        + b4.get_msgs_from_mailbox_or_maildir(str(path))
        + b4.get_msgs_from_mailbox_or_maildir(str(fixes_only))
    )
    source.refresh()
    flagged = {i.series.subject: i.state.stable_pending for i in source.load().items}
    assert flagged == {'mm: fix it': True, 'mm: fix, no stable': False, 'mm: a feature': False}


def test_a_landed_stable_fix_is_no_longer_pending():
    import dataclasses

    from b4_maintainer.adapters.live import _stable_pending
    from b4_maintainer.fixtures import make_item

    item = make_item(subject='mm: fix')
    patch = dataclasses.replace(item.series.patches[0], stable=True)
    item = dataclasses.replace(item, series=dataclasses.replace(item.series, patches=(patch,)))
    assert _stable_pending((item,))[0].state.stable_pending
    landed = dataclasses.replace(item, landed=True)
    assert not _stable_pending((landed,))[0].state.stable_pending

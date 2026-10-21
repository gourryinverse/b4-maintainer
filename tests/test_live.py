"""Contract: the live workspace browses cached list mail and tracks through b4."""

import datetime

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
        self.track_calls: list[tuple[str, str]] = []

    def tracked_series(self, checkout, identifier):
        return list(self.tracked)

    def search_for(self, checkout):
        def search(query):
            self.searches.append((checkout.name, query))
            return next((v for k, v in self.answers.items() if f'l:{k} ' in query), [])

        return search

    def track(self, checkout, msgid, command):
        self.track_calls.append((checkout.name, msgid))


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

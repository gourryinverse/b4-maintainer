"""Contract: Sashiko review status is fetched on refresh and read offline."""

import datetime

import pytest
from gitutil import git, make_checkout
from sashikodata import URL, FakeSashiko, detail, finding, row
from test_live import CONFIG, MM, NOW, Fakes, mail, reply_to, responses_source

from b4_maintainer.adapters import b4state
from b4_maintainer.adapters.live import LiveSource
from b4_maintainer.adapters.sashiko import SashikoCache, group
from b4_maintainer.views import ViewIndex

DAY = datetime.timedelta(days=1)


def test_group_is_the_list_id_with_its_domain_reversed():
    assert group('linux-mm.kvack.org') == 'org.kvack.linux-mm'
    assert group('linux-cxl.vger.kernel.org') == 'org.kernel.vger.linux-cxl'


def cache_with(tmp_path, rows, window_days=10, since=None):
    fake = FakeSashiko()
    fake.pages['org.kvack.linux-mm'] = rows
    cache = SashikoCache(tmp_path / 'sashiko')
    count = cache.update_list(MM, URL, window_days, fake.get, now=NOW, since=since, per_page=2)
    return cache, fake, count


def test_list_status_pages_back_to_the_window(tmp_path):
    rows = [row(10 - n, f'ps{n}@example.org', date=NOW - n * 3 * DAY) for n in range(6)]
    cache, fake, count = cache_with(tmp_path, rows)
    # Pages of 2: days 0,3 | 6,9 | 12 (older than the window: stop).
    assert [p['page'] for _, p in fake.list_requests] == [1, 2, 3]
    assert {u for u, _ in fake.list_requests} == {f'{URL}/api/patchsets'}
    assert count == 4
    assert set(cache.reviews()) == {f'ps{n}@example.org' for n in range(4)}


def test_list_status_reaches_back_to_older_cached_postings(tmp_path):
    rows = [row(10 - n, f'ps{n}@example.org', date=NOW - n * 3 * DAY) for n in range(8)]
    # A revived thread posted 15 days ago is cached: page back to it, not further.
    cache, fake, count = cache_with(tmp_path, rows, since=NOW - 15 * DAY)
    assert [p['page'] for _, p in fake.list_requests] == [1, 2, 3, 4]
    assert count == 6

    # Next refresh: the window again, then stop at the first page already cached.
    rows[0] = dict(rows[0], status='Embargoed')
    fake.list_requests.clear()
    count = cache.update_list(MM, URL, 10, fake.get, now=NOW, since=NOW - 15 * DAY, per_page=2)
    assert [p['page'] for _, p in fake.list_requests] == [1, 2, 3]
    assert count == 6
    reviews = cache.reviews()
    assert reviews['ps0@example.org'].status == 'Embargoed'
    assert 'ps5@example.org' in reviews  # Kept from the first refresh.


def test_status_and_counts_map_to_reviews(tmp_path):
    rows = [
        row(1, 'clean@example.org'),
        row(2, 'found@example.org', counts=(0, 1, 2, 3)),
        row(3, 'held@example.org', status='Embargoed'),
        row(4, 'busy@example.org', status='In Review'),
        row(5, 'broken@example.org', status='Failed To Apply'),
    ]
    reviews = cache_with(tmp_path, [dict(r, date=int(NOW.timestamp())) for r in rows])[0].reviews()
    found = reviews['found@example.org']
    assert (found.critical, found.high, found.medium, found.low) == (0, 1, 2, 3)
    assert found.findings == 6 and found.worst == 'high'
    assert reviews['clean@example.org'].findings == 0
    assert reviews['clean@example.org'].url == f'{URL}/#/patchset/1'
    assert {m: r.status for m, r in reviews.items()}['held@example.org'] == 'Embargoed'


def test_failed_fetch_keeps_the_previous_cache(tmp_path):
    cache, fake, _ = cache_with(tmp_path, [row(1, 'a@example.org', date=NOW)])
    fake.fail = True
    with pytest.raises(OSError):
        cache.update_list(MM, URL, 10, fake.get, now=NOW)
    assert set(SashikoCache(tmp_path / 'sashiko').reviews()) == {'a@example.org'}


def test_details_fetch_for_findings_and_wanted_series_only(tmp_path):
    rows = [
        row(1, 'found@example.org', counts=(0, 0, 1, 0), date=NOW),
        row(2, 'clean@example.org', date=NOW),
        row(3, 'mine@example.org', date=NOW),
        row(4, 'held@example.org', status='Embargoed', date=NOW),
    ]
    cache, fake, _ = cache_with(tmp_path, rows)
    fake.details['found@example.org'] = detail(
        1,
        'found@example.org',
        [
            ('found-1@example.org', [finding('mm: leaks foo', 'High')]),
            ('found-2@example.org', [finding('mm: old bug', 'Low', preexisting=True)]),
        ],
    )
    fake.details['mine@example.org'] = detail(3, 'mine@example.org', [('mine-1@example.org', [])])
    fake.details['mine@example.org']['baseline']['branch'] = '59d7f9ec14ad4b6f38c0'
    wanted = {'mine@example.org', 'held@example.org'}
    assert cache.update_details(wanted, fake.patchset) == (2, 0)
    assert sorted(fake.detail_requests) == ['found@example.org', 'mine@example.org']

    review = SashikoCache(tmp_path / 'sashiko').reviews()['found@example.org']
    assert review.baseline == 'mm/mm-new @ 59d7f9ec14ad'
    assert SashikoCache(tmp_path / 'sashiko').reviews()['mine@example.org'].baseline == (
        '59d7f9ec14ad'  # Reviewed on a bare commit: no branch to name.
    )
    assert review.model == 'synthetic-model'
    first, second = review.patches
    assert (first.part, first.msgid) == (1, 'found-1@example.org')
    [leak] = first.findings
    assert (leak.severity, leak.problem, leak.preexisting) == ('high', 'mm: leaks foo', False)
    assert leak.locations == (('mm/a.c', 10),)
    assert second.findings[0].preexisting
    assert 'Review of part 1.' in first.inline_review

    # Unchanged status and counts: nothing is fetched again.
    fake.detail_requests.clear()
    assert cache.update_details(wanted, fake.patchset) == (0, 0)
    assert fake.detail_requests == []


def test_missing_detail_counts_as_a_failure(tmp_path):
    cache, fake, _ = cache_with(
        tmp_path, [row(1, 'gone@example.org', counts=(0, 0, 1, 0), date=NOW)]
    )
    assert cache.update_details(set(), fake.patchset) == (0, 1)


def live_source(tmp_path, url=URL):
    root = tmp_path / 'ws'
    make_checkout(root / 'projects' / 'demo')
    (root / 'workspace.toml').write_text(CONFIG)
    fakes, sashiko = Fakes(), FakeSashiko()
    source = LiveSource(
        root,
        tracked_series=fakes.tracked_series,
        search_for=fakes.search_for,
        track=fakes.track,
        b4_command=['b4'],
        now=lambda: NOW,
        identity=lambda checkout: None,
        sashiko_url=lambda checkout: url,
        sashiko_get=sashiko.get,
        sashiko_patchset=sashiko.patchset,
    )
    return source, fakes, sashiko


def test_unset_url_makes_no_sashiko_requests(tmp_path):
    source, fakes, sashiko = live_source(tmp_path, url=None)
    fakes.answers[MM] = mail(tmp_path, 'a', 'mm: browse me')[1]
    source.refresh()
    assert sashiko.list_requests == [] and sashiko.detail_requests == []
    assert all(i.sashiko is None for i in source.load().items)


def test_refresh_attaches_sashiko_reviews_to_series(tmp_path):
    source, fakes, sashiko = live_source(tmp_path)
    sent, msgs = mail(tmp_path, 'a', 'mm: reviewed')
    other = mail(tmp_path, 'b', 'mm: not seen by sashiko')[1]
    fakes.answers[MM] = msgs + other
    cover = sent.msgids[0]
    sashiko.pages['org.kvack.linux-mm'] = [row(7, cover, counts=(0, 0, 1, 0), date=NOW)]
    sashiko.details[cover] = detail(
        7, cover, [(sent.msgids[1], [finding('mm: odd')]), (sent.msgids[2], [])]
    )
    summary = source.refresh()
    assert 'Sashiko: 1 series' in summary
    items = {i.series.subject: i for i in source.load().items}
    review = items['mm: reviewed'].sashiko
    assert review is not None and review.medium == 1
    assert review.patches[0].findings[0].problem == 'mm: odd'
    assert items['mm: not seen by sashiko'].sashiko is None


def test_sashiko_outage_is_reported_not_raised(tmp_path):
    source, fakes, sashiko = live_source(tmp_path)
    fakes.answers[MM] = mail(tmp_path, 'a', 'mm: browse me')[1]
    sashiko.fail = True
    assert 'Sashiko failed' in source.refresh()


def test_sashiko_replies_are_not_responses(tmp_path):
    source, fakes, _ = responses_source(tmp_path)
    _, mine = mail(tmp_path, 'mine', 'mm: my series')
    for m in mine:
        m.replace_header('From', 'Me <me@example.org>')
    bot = reply_to(mine[1], 'sashiko-bot@kernel.org', 'bot@example.org')
    fakes.answers[MM] = mine + [bot]
    source.refresh()
    snapshot = source.load()
    assert ViewIndex(snapshot.workspace, snapshot.items).count('responses') == 0


def test_b4_sashiko_url_comes_from_the_checkout(ws):
    checkout = ws / 'projects' / 'demo'
    assert b4state.sashiko_url(checkout) is None
    git('config', 'b4.sashiko-url', URL, cwd=checkout)
    assert b4state.sashiko_url(checkout) == URL


def test_revived_older_series_still_get_their_review(tmp_path):
    source, fakes, sashiko = live_source(tmp_path)
    sent, msgs = mail(tmp_path, 'old', 'mm: posted long ago', days_ago=20)
    revived = reply_to(msgs[1], 'Reviewer <reviewer@example.org>', 'late@example.org')
    fakes.answers[MM] = msgs + [revived]
    cover = sent.msgids[0]
    newer = [row(100 + n, f'new{n}@example.org', date=NOW - n * DAY / 10) for n in range(60)]
    sashiko.pages['org.kvack.linux-mm'] = newer + [row(7, cover, date=NOW - 20 * DAY)]
    source.refresh()
    [item] = [i for i in source.load().items if i.series.subject == 'mm: posted long ago']
    assert item.sashiko is not None and item.sashiko.status == 'Reviewed'

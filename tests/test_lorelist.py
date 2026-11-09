"""Contract: list browse caches lore search results per thread, incrementally."""

import datetime
import email.message
import email.utils
import re

import b4

from b4_maintainer.adapters.b4mbox import parse_mbox
from b4_maintainer.adapters.lorelist import MailCache, list_query
from b4_maintainer.fixtures import PatchSpec, write_series_mbox

NOW = datetime.datetime(2026, 10, 21, 12, 0, tzinfo=datetime.timezone.utc)
LIST = 'linux-mm.kvack.org'
CXL = 'linux-cxl.vger.kernel.org'


def thread(tmp_path, name, subject, days_ago, **kwargs):
    path = tmp_path / 'src' / f'{name}.mbx'
    path.parent.mkdir(exist_ok=True)
    patches = [PatchSpec('mm/a.c'), PatchSpec('mm/b.c')]
    date = NOW - datetime.timedelta(days=days_ago)
    write_series_mbox(path, subject, patches=patches, date=date, **kwargs)
    return b4.get_msgs_from_mailbox_or_maildir(str(path))


def subjects(cache):
    return sorted(s.subject for src in cache.sources() for s in parse_mbox(src))


class FakeSearch:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.queries = []

    def __call__(self, query):
        self.queries.append(query)
        return self.answers.pop(0)


def test_query_limits_list_patches_and_received_time():
    since = datetime.date(2026, 10, 7)
    assert list_query(LIST, since) == f'l:{LIST} AND (s:PATCH OR s:RFC) AND rt:2026-10-07..'


def test_refresh_caches_series_for_the_window(tmp_path):
    cache = MailCache(tmp_path / 'mail')
    search = FakeSearch(thread(tmp_path, 'a', 'mm: first', 2))
    assert cache.refresh(LIST, 14, search, now=NOW) == 3
    assert search.queries == [list_query(LIST, datetime.date(2026, 10, 7))]
    assert subjects(cache) == ['mm: first']


def test_unrelated_series_are_separate_threads(tmp_path):
    cache = MailCache(tmp_path / 'mail')
    mail = thread(tmp_path, 'a', 'mm: first', 2) + thread(tmp_path, 'b', 'mm: second', 1)
    cache.refresh(LIST, 14, FakeSearch(mail), now=NOW)
    assert len(cache.sources()) == 2
    assert subjects(cache) == ['mm: first', 'mm: second']


def test_reply_with_only_in_reply_to_joins_its_thread(tmp_path):
    cache = MailCache(tmp_path / 'mail')
    mail = thread(tmp_path, 'a', 'mm: first', 2)
    cache.refresh(LIST, 14, FakeSearch(mail), now=NOW)
    patch = next(m for m in mail if '2/2' in str(m['Subject']))
    reply = email.message.EmailMessage()
    reply['From'] = 'Reviewer <reviewer@example.org>'
    reply['Subject'] = 'Re: ' + str(patch['Subject'])
    reply['Date'] = email.utils.format_datetime(NOW)
    reply['Message-ID'] = '<reply-1@example.org>'
    reply['In-Reply-To'] = str(patch['Message-ID'])
    reply.set_content('Reviewed-by: Reviewer <reviewer@example.org>\n')
    cache.refresh(LIST, 14, FakeSearch([reply]), now=NOW + datetime.timedelta(hours=1))
    [source] = cache.sources()
    [series] = parse_mbox(source)
    assert series.patches[1].reply_trailers.reviewed == 1


def test_second_refresh_is_incremental_and_dedupes(tmp_path):
    cache = MailCache(tmp_path / 'mail')
    first = thread(tmp_path, 'a', 'mm: first', 2)
    second = thread(tmp_path, 'b', 'mm: second', 0)
    search = FakeSearch(first, first + second)
    cache.refresh(LIST, 14, search, now=NOW)
    later = NOW + datetime.timedelta(days=3)
    assert cache.refresh(LIST, 14, search, now=later) == 3
    # Overlap one day with the previous fetch; never before the window.
    assert search.queries[1] == list_query(LIST, datetime.date(2026, 10, 20))
    assert subjects(cache) == ['mm: first', 'mm: second']


def test_refresh_drops_mail_older_than_the_window(tmp_path):
    cache = MailCache(tmp_path / 'mail')
    old = thread(tmp_path, 'old', 'mm: old', 10)
    cache.refresh(LIST, 14, FakeSearch(old), now=NOW)
    later = NOW + datetime.timedelta(days=7)
    cache.refresh(LIST, 14, FakeSearch([]), now=later)
    assert cache.sources() == []


def test_no_answer_keeps_cache_and_refetches_next_time(tmp_path):
    cache = MailCache(tmp_path / 'mail')
    cache.refresh(LIST, 14, FakeSearch(thread(tmp_path, 'a', 'mm: first', 2)), now=NOW)
    later = NOW + datetime.timedelta(days=1)
    search = FakeSearch(None, [])
    assert cache.refresh(LIST, 14, search, now=later) == 0
    cache.refresh(LIST, 14, search, now=later)
    assert search.queries[0] == search.queries[1]
    assert subjects(cache) == ['mm: first']


def test_cross_posted_thread_is_stored_once(tmp_path):
    cache = MailCache(tmp_path / 'mail')
    mail = thread(tmp_path, 'a', 'mm/cxl: shared', 1, lists=(LIST, CXL))
    cache.refresh(LIST, 14, FakeSearch(mail), now=NOW)
    assert cache.refresh(CXL, 14, FakeSearch(mail), now=NOW) == 0
    assert subjects(cache) == ['mm/cxl: shared']
    assert sorted(cache.fetched_lists()) == sorted([LIST, CXL])


def test_threads_with_recent_activity_are_kept_whole(tmp_path):
    cache = MailCache(tmp_path / 'mail')
    msgs = thread(tmp_path, 'a', 'mm: older series', 20)
    patch = next(m for m in msgs if '2/2' in str(m['Subject']))
    reply = email.message.EmailMessage()
    reply['From'] = 'Reviewer <reviewer@example.org>'
    reply['Subject'] = 'Re: ' + str(patch['Subject'])
    reply['Date'] = email.utils.format_datetime(NOW)
    reply['Message-ID'] = '<late-reply@example.org>'
    reply['In-Reply-To'] = str(patch['Message-ID'])
    reply['References'] = f'{msgs[0]["Message-ID"]} {patch["Message-ID"]}'
    reply.set_content('Acked-by: Reviewer <reviewer@example.org>\n')
    # Lore expands a recent match to its whole thread.
    assert cache.refresh(LIST, 14, FakeSearch(msgs + [reply]), now=NOW) == 4
    [series] = [s for src in cache.sources() for s in parse_mbox(src)]
    assert series.subject == 'mm: older series'
    assert series.patches[1].reply_trailers.acked == 1


def test_prune_compares_dates_across_time_zones(tmp_path):
    cache = MailCache(tmp_path / 'mail')
    msgs = thread(tmp_path, 'a', 'mm: west', 0)
    for m in msgs:
        when = email.utils.parsedate_to_datetime(str(m['Date']))
        west = when.astimezone(datetime.timezone(datetime.timedelta(hours=-8)))
        m.replace_header('Date', email.utils.format_datetime(west))
    window_end = NOW + datetime.timedelta(days=14, hours=-1)
    cache.refresh(LIST, 14, FakeSearch(msgs), now=NOW)
    cache.refresh(LIST, 14, FakeSearch([]), now=window_end)
    assert subjects(cache) == ['mm: west']


def test_index_records_sender_and_parent(tmp_path):
    cache = MailCache(tmp_path / 'mail')
    msgs = thread(tmp_path, 'a', 'mm: first', 1)
    cache.refresh(LIST, 14, FakeSearch(msgs), now=NOW)
    info = cache.messages()
    cover = b4.LoreMessage.get_clean_msgid(msgs[0])
    patch = next(m for m in msgs if '1/2' in str(m['Subject']))
    entry = info[b4.LoreMessage.get_clean_msgid(patch)]
    assert entry.parent == cover
    assert entry.sender == 'contributor1@example.org'
    assert cache.thread_name(entry.key) == cache.thread_name(info[cover].key)


def test_old_index_entries_are_backfilled_from_mail(tmp_path):
    import json

    cache = MailCache(tmp_path / 'mail')
    msgs = thread(tmp_path, 'a', 'mm: first', 1)
    cache.refresh(LIST, 14, FakeSearch(msgs), now=NOW)
    path = tmp_path / 'mail' / 'index.json'
    old = {m: v[:2] for m, v in json.loads(path.read_text()).items()}
    path.write_text(json.dumps(old))
    info = MailCache(tmp_path / 'mail').messages()
    assert all(e.sender == 'contributor1@example.org' for e in info.values())
    assert all(len(v) == 4 for v in json.loads(path.read_text()).values())


def test_refresh_with_any_query(tmp_path):
    cache = MailCache(tmp_path / 'mail')
    search = FakeSearch(thread(tmp_path, 'a', 'mm: mine', 1))
    added = cache.refresh_query(
        'from-me', lambda since: f'f:me@example.org AND rt:{since:%Y-%m-%d}..', 14, search, now=NOW
    )
    assert added == 3
    assert search.queries == ['f:me@example.org AND rt:2026-10-07..']
    assert 'from-me' in cache.fetched_lists()


def test_kept_threads_outlive_the_window(tmp_path):
    cache = MailCache(tmp_path / 'mail')
    old = thread(tmp_path, 'a', 'mm: watched', 30)
    root = b4.LoreMessage.get_clean_msgid(old[0])
    cache.keep = frozenset({root})
    cache.refresh_query('x', lambda since: 'q', 14, FakeSearch(old), now=NOW)
    assert len(cache.sources()) == 1
    cache.keep = frozenset()
    cache.refresh_query('x', lambda since: 'q', 14, FakeSearch([]), now=NOW)
    assert cache.sources() == []


def test_refresh_thread_adds_new_messages_to_the_thread(tmp_path):
    cache = MailCache(tmp_path / 'mail')
    msgs = thread(tmp_path, 'a', 'mm: watched', 2)
    root = b4.LoreMessage.get_clean_msgid(msgs[0])
    cache.refresh_query('x', lambda since: 'q', 14, FakeSearch(msgs[:2]), now=NOW)
    asked = []

    def fetch(msgid):
        asked.append(msgid)
        return msgs

    assert cache.refresh_thread(root, fetch, 14, now=NOW) == len(msgs) - 2
    assert asked == [root]
    assert len(cache.sources()) == 1
    assert cache.refresh_thread(root, lambda m: None, 14, now=NOW) == 0  # Lookup failed.


STABLE = r'\d+\.\d+(\.\d+)?-rc\d+ review$'


def test_excluded_threads_are_not_cached(tmp_path):
    """A thread whose first message's subject an exclude pattern finds (a stable -rc
    review, fetched whole because one backport copies the maintainer) is left out."""
    cache = MailCache(tmp_path / 'mail')
    cache.exclude = (re.compile(STABLE),)
    mail = thread(tmp_path, 'a', '6.12.117-rc1 review', 1) + thread(tmp_path, 'b', 'mm: x', 1)
    assert cache.refresh_query('mine', lambda since: 'q', 14, FakeSearch(mail), now=NOW) == 3
    assert subjects(cache) == ['mm: x']


def test_excluded_threads_already_cached_go_unless_kept(tmp_path):
    cache = MailCache(tmp_path / 'mail')
    review = thread(tmp_path, 'a', '7.3.4-rc1 review', 1)
    other = thread(tmp_path, 'b', '6.18.61-rc1 review', 1)
    cache.refresh_query('mine', lambda since: 'q', 14, FakeSearch(review + other), now=NOW)
    assert len(cache.sources()) == 2
    cache.exclude = (re.compile(STABLE),)
    cache.keep = frozenset({b4.LoreMessage.get_clean_msgid(other[0])})  # On a workbench.
    cache.refresh_query('mine', lambda since: 'q', 14, FakeSearch([]), now=NOW)
    assert subjects(cache) == ['6.18.61-rc1 review']


def by_query(threads):
    """A whole-thread search answering `m:"<msgid>"` terms with the threads holding them."""
    asked = []

    def search(query):
        asked.append(query)
        wanted = set(re.findall(r'm:"([^"]+)"', query))
        return [
            m for t in threads if wanted & {b4.LoreMessage.get_clean_msgid(x) for x in t} for m in t
        ]

    search.asked = asked
    return search


def from_me(msgs):
    for m in msgs:
        m.replace_header('From', 'Me <me@example.org>')
    return msgs


def reply(to, sender, msgid, days_ago):
    msg = email.message.EmailMessage()
    msg['From'] = sender
    msg['Subject'] = f'Re: {to["Subject"]}'
    msg['Message-ID'] = f'<{msgid}>'
    parent = b4.LoreMessage.get_clean_msgid(to)
    msg['In-Reply-To'] = f'<{parent}>'
    msg['References'] = f'<{parent}>'
    msg['Date'] = email.utils.format_datetime(NOW - datetime.timedelta(days=days_ago))
    msg.set_content('Looks good.\n')
    return msg


def test_own_postings_of_months_back_are_fetched_whole_and_kept(tmp_path):
    """The maintainer's own postings (not their replies) reach back *days*: each new one's
    whole thread is fetched (one m: search for many), and kept past the browse window."""
    cache = MailCache(tmp_path / 'mail')
    mine = from_me(thread(tmp_path, 'a', 'mm: my old series', 100))
    review = reply(mine[1], 'Reviewer <r@example.org>', 'old-review@x', 99)
    theirs = thread(tmp_path, 'b', 'mm: their series', 50)
    my_review = reply(theirs[1], 'Me <me@example.org>', 'my-review@x', 49)
    posts = FakeSearch([*mine, my_review])  # Messages only: postings and a reply.
    threads = by_query([[*mine, review], [*theirs, my_review]])
    added = cache.refresh_own('own-me', lambda since: 'q', 365, 14, posts, threads, now=NOW)
    assert added == 4  # Its three postings and the review: not the thread I only reviewed.
    assert len(threads.asked) == 1
    assert subjects(cache) == ['mm: my old series']
    assert cache.history() == {*(b4.LoreMessage.get_clean_msgid(m) for m in mine), 'old-review@x'}
    cache.own_since = NOW - datetime.timedelta(days=365)
    cache.refresh(LIST, 14, FakeSearch([]), now=NOW)  # The browse window's prune keeps it,
    assert subjects(cache) == ['mm: my old series']
    cache.own_since = NOW - datetime.timedelta(days=90)
    cache.refresh(LIST, 14, FakeSearch([]), now=NOW)  # until it is older than *days*.
    assert subjects(cache) == [] and cache.history() == set()


def test_own_postings_already_cached_are_not_fetched_again(tmp_path):
    cache = MailCache(tmp_path / 'mail')
    mine = from_me(thread(tmp_path, 'a', 'mm: my series', 2))
    cache.refresh(LIST, 14, FakeSearch(mine), now=NOW)
    threads = by_query([mine])
    cache.refresh_own('own-me', lambda since: 'q', 365, 14, FakeSearch(mine), threads, now=NOW)
    assert threads.asked == [] and cache.history() == set()  # Recent: not history.


def test_an_own_thread_lore_did_not_return_is_asked_for_again(tmp_path):
    cache = MailCache(tmp_path / 'mail')
    mine = from_me(thread(tmp_path, 'a', 'mm: my old series', 100))
    posts = FakeSearch(list(mine), list(mine))
    cache.refresh_own('own-me', lambda since: 'q', 365, 14, posts, lambda q: None, now=NOW)
    assert subjects(cache) == []  # Not its postings alone: its whole thread, next time,
    threads = by_query([mine])
    cache.refresh_own('own-me', lambda since: str(since), 365, 14, posts, threads, now=NOW)
    assert subjects(cache) == ['mm: my old series']
    assert posts.queries[1] == str((NOW - datetime.timedelta(days=365)).date())  # from as far.

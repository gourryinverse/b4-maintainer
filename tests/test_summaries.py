"""Contract: parsed series round-trip through on-disk summaries; only changed threads reparse."""

import datetime

import b4

from b4_maintainer.adapters.b4mbox import parse_mbox
from b4_maintainer.adapters.lorelist import MailCache
from b4_maintainer.adapters.summaries import series_from_dict, series_to_dict
from b4_maintainer.fixtures import PatchSpec, write_series_mbox

NOW = datetime.datetime(2026, 10, 21, 12, 0, tzinfo=datetime.timezone.utc)
LIST = 'linux-mm.kvack.org'


def test_series_round_trip(demo_root):
    series = [s for path in sorted((demo_root / 'mail').glob('*.mbx')) for s in parse_mbox(path)]
    assert series
    for s in series:
        assert series_from_dict(series_to_dict(s)) == s


class CountingParse:
    def __init__(self):
        self.calls = []

    def __call__(self, source):
        self.calls.append(source.name)
        return parse_mbox(source)


def fill(tmp_path, cache, *subjects):
    for n, subject in enumerate(subjects):
        path = tmp_path / 'src' / f'{n}-{len(cache.sources())}.mbx'
        path.parent.mkdir(exist_ok=True)
        write_series_mbox(
            path, subject, patches=[PatchSpec('mm/a.c'), PatchSpec('mm/b.c')], date=NOW
        )
        msgs = b4.get_msgs_from_mailbox_or_maildir(str(path))
        cache.refresh(LIST, 14, lambda q, m=msgs: m, now=NOW)


def test_series_reparse_only_changed_threads(tmp_path):
    cache = MailCache(tmp_path / 'mail')
    fill(tmp_path, cache, 'mm: one', 'mm: two')
    parse = CountingParse()
    assert sorted(s.subject for s in cache.series(parse)) == ['mm: one', 'mm: two']
    assert len(parse.calls) == 2
    assert sorted(s.subject for s in cache.series(parse)) == ['mm: one', 'mm: two']
    assert len(parse.calls) == 2
    fill(tmp_path, cache, 'mm: three')
    assert len(cache.series(parse)) == 3
    assert len(parse.calls) == 3


def test_damaged_summary_is_rebuilt(tmp_path):
    cache = MailCache(tmp_path / 'mail')
    fill(tmp_path, cache, 'mm: one')
    cache.series(parse_mbox)
    for summary in (tmp_path / 'mail' / 'series').glob('*.json'):
        summary.write_text('{not json')
    assert [s.subject for s in cache.series(parse_mbox)] == ['mm: one']


def test_pruned_threads_lose_their_summaries(tmp_path):
    cache = MailCache(tmp_path / 'mail')
    fill(tmp_path, cache, 'mm: one')
    cache.series(parse_mbox)
    cache.refresh(LIST, 14, lambda q: [], now=NOW + datetime.timedelta(days=30))
    assert cache.series(parse_mbox) == []
    assert not list((tmp_path / 'mail' / 'series').glob('*.json'))

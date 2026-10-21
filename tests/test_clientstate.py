"""Contract: hidden series persist as diff-friendly text in the synced client state."""

import datetime
import json

from b4_maintainer.adapters.clientstate import HiddenStore
from b4_maintainer.fixtures import make_series
from b4_maintainer.workspace import Workspace, build_items

NOW = datetime.datetime(2026, 10, 21, 12, 0, tzinfo=datetime.timezone.utc)


def item(series):
    [result] = build_items([series], {}, Workspace())
    return result


def test_hidden_series_stay_hidden_across_revisions(tmp_path):
    store = HiddenStore(tmp_path / 'state' / 'client' / 'hidden.json')
    v1 = item(make_series('mm: not for me', revision=1, msgid='v1@example.org'))
    assert not store.load().matches(v1)
    store.hide(v1, now=NOW)
    assert store.load().matches(v1)
    v2 = item(make_series('mm: not for me', revision=2, change_id='later'))
    assert store.load().matches(v2)
    other = item(make_series('mm: something else', msgid='o@example.org'))
    assert not store.load().matches(other)


def test_unhide_removes_every_matching_entry(tmp_path):
    store = HiddenStore(tmp_path / 'hidden.json')
    v1 = item(make_series('mm: not for me', revision=1, msgid='v1@example.org'))
    store.hide(v1, now=NOW)
    v2 = item(make_series('mm: not for me', revision=2, change_id='later'))
    store.hide(v2, now=NOW)
    store.unhide(v2)
    assert not store.load().matches(v1)
    assert not store.load().matches(v2)


def test_file_is_sorted_text_with_context(tmp_path):
    path = tmp_path / 'hidden.json'
    store = HiddenStore(path)
    store.hide(item(make_series('mm: zebra', msgid='z@example.org')), now=NOW)
    store.hide(item(make_series('mm: apple', msgid='a@example.org')), now=NOW)
    entries = json.loads(path.read_text())
    assert [e['subject'] for e in entries] == ['mm: apple', 'mm: zebra']
    assert all(e['hidden_at'] == NOW.isoformat() for e in entries)
    assert path.read_text().count('\n') >= len(entries) * 4


def test_missing_file_hides_nothing(tmp_path):
    assert (
        not HiddenStore(tmp_path / 'absent.json')
        .load()
        .matches(item(make_series('mm: x', msgid='x@example.org')))
    )

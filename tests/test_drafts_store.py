"""Contract: reply drafts persist per answered message under var/drafts."""

from b4_maintainer.adapters.drafts import DraftStore


def test_drafts_round_trip_and_delete(tmp_path):
    store = DraftStore(tmp_path / 'drafts')
    assert store.get('m1@example.org') is None and store.msgids() == set()
    store.save('m1@example.org', 'On ..., X wrote:\n> body\n', 'On ..., X wrote:\n> body\nOK\n')
    saved = DraftStore(tmp_path / 'drafts').get('m1@example.org')
    assert saved is not None
    assert saved.quote == 'On ..., X wrote:\n> body\n'
    assert saved.text.endswith('OK\n')
    assert store.msgids() == {'m1@example.org'}
    store.delete('m1@example.org')
    assert store.get('m1@example.org') is None and store.msgids() == set()
    store.delete('m1@example.org')  # Deleting twice is harmless.


def test_unreadable_drafts_are_skipped(tmp_path):
    store = DraftStore(tmp_path / 'drafts')
    store.save('m1@example.org', 'q', 'q and more')
    (tmp_path / 'drafts' / 'broken.json').write_text('{not json')
    assert store.msgids() == {'m1@example.org'}

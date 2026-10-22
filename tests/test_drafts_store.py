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
    assert list((tmp_path / 'drafts').iterdir()) == []
    store.delete('m1@example.org')  # Deleting twice is harmless.


def test_text_lives_in_an_editable_eml_file(tmp_path):
    store = DraftStore(tmp_path / 'drafts')
    store.save('m1@example.org', 'quote\n', 'quote\nreply\n')
    path = store.text_path('m1@example.org')
    assert path.suffix == '.eml' and path.read_text() == 'quote\nreply\n'
    # An editor saving with CRLF endings still reads back as unix text.
    path.write_bytes(b'quote\r\nedited\r\n')
    assert store.get('m1@example.org').text == 'quote\nedited\n'


def test_editor_pane_is_recorded(tmp_path):
    store = DraftStore(tmp_path / 'drafts')
    store.save('m1@example.org', 'q', 'q and more')
    assert store.get('m1@example.org').pane == ''
    store.set_pane('m1@example.org', '%12')
    assert store.get('m1@example.org').pane == '%12'
    store.save('m1@example.org', 'q', 'q and even more')  # Saving keeps the pane.
    assert store.get('m1@example.org').pane == '%12'


def test_unreadable_drafts_are_skipped(tmp_path):
    store = DraftStore(tmp_path / 'drafts')
    store.save('m1@example.org', 'q', 'q and more')
    (tmp_path / 'drafts' / 'broken.json').write_text('{not json')
    assert store.msgids() == {'m1@example.org'}

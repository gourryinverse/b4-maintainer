"""Contract: tracked series are read from, and added through, the project's own b4 state."""

import os
import stat
from types import SimpleNamespace

import b4
from b4.review import tracking

from b4_maintainer.adapters.b4state import archive, b4_repo, track, tracked_series, unarchive
from b4_maintainer.cli import project_main


def enrolled(ws):
    assert project_main(['add', '-C', str(ws), 'demo']) == 0
    return ws / 'projects' / 'demo'


def add_tracked(checkout, change_id, msgid, patch_msgids=()):
    with b4_repo(checkout):
        conn = tracking.get_db('demo')
        tracking.add_series_to_db(
            conn,
            change_id,
            2,
            'mm: tracked',
            'Contributor',
            'contributor@example.org',
            '2026-10-20T10:00:00+00:00',
            msgid,
            len(patch_msgids),
        )
        patches = [SimpleNamespace(msgid=m, subject='mm: patch') for m in patch_msgids]
        tracking.add_series_patches(conn, change_id, 2, SimpleNamespace(patches=[None, *patches]))
        conn.commit()
        conn.close()


def test_tracked_series_come_from_workspace_state(ws, home):
    checkout = enrolled(ws)
    add_tracked(checkout, 'cid-1', 'cover@example.org', ['p1@example.org', 'p2@example.org'])

    [entry] = tracked_series(checkout, 'demo')
    assert entry.change_id == 'cid-1'
    assert entry.revision == 2
    assert entry.subject == 'mm: tracked'
    assert entry.msgid == 'cover@example.org'
    assert {'cover@example.org', 'p1@example.org', 'p2@example.org'} <= entry.msgids
    assert entry.sent is not None and entry.sent.year == 2026
    assert not (home / 'xdg-data').exists()


def test_archive_marks_the_series_archived_in_b4(ws, home):
    checkout = enrolled(ws)
    add_tracked(checkout, 'cid-1', 'cover@example.org', ['p1@example.org'])
    ok, detail = archive(checkout, 'demo', 'cid-1', 2)
    assert ok, detail
    [entry] = tracked_series(checkout, 'demo')
    assert entry.status == 'archived'


def test_unarchive_tracks_an_archived_series_again(ws, home):
    checkout = enrolled(ws)
    add_tracked(checkout, 'cid-1', 'cover@example.org', ['p1@example.org'])
    assert archive(checkout, 'demo', 'cid-1', 2)[0]
    unarchive(checkout, 'demo', 'cid-1')
    [entry] = tracked_series(checkout, 'demo')
    assert entry.status == 'new'
    assert 'p1@example.org' in entry.msgids


def test_nothing_tracked(ws):
    assert tracked_series(enrolled(ws), 'demo') == []


def test_b4_config_is_restored(ws):
    checkout = enrolled(ws)
    before = b4.MAIN_CONFIG
    with b4_repo(checkout):
        assert b4.get_data_dir() == str(ws / 'var' / 'b4' / 'data')
    assert b4.MAIN_CONFIG is before


def test_track_runs_b4_in_the_project_checkout(ws, tmp_path):
    checkout = enrolled(ws)
    log = tmp_path / 'b4.log'
    fake = tmp_path / 'fake-b4'
    fake.write_text(f'#!/bin/sh\necho "$PWD $*" >> {log}\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    track(checkout, 'cover@example.org', [str(fake)])
    assert log.read_text().split() == [
        os.path.realpath(checkout),
        'review',
        'track',
        'cover@example.org',
    ]


def test_track_failure_is_reported(ws, tmp_path):
    checkout = enrolled(ws)
    fake = tmp_path / 'fake-b4'
    fake.write_text('#!/bin/sh\necho "Could not retrieve series" >&2\nexit 1\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    try:
        track(checkout, 'cover@example.org', [str(fake)])
    except RuntimeError as e:
        assert 'Could not retrieve series' in str(e)
    else:
        raise AssertionError('track() should fail')


def test_seen_flags_live_in_workspace_b4_messages_db(ws, home):
    from b4.review import messages

    from b4_maintainer.adapters.b4state import mark_seen, seen

    checkout = enrolled(ws)
    assert seen(checkout, ['a@example.org', 'b@example.org']) == set()
    mark_seen(checkout, {'a@example.org': '2026-10-21T12:00:00+00:00'})
    assert seen(checkout, ['a@example.org', 'b@example.org']) == {'a@example.org'}
    assert (ws / 'var' / 'b4' / 'data' / 'review' / 'messages.sqlite3').is_file()
    with b4_repo(checkout):
        conn = messages.get_db()
        assert 'Seen' in messages.get_flags(conn, 'a@example.org').split()
        conn.close()
    assert not (home / 'xdg-data').exists()

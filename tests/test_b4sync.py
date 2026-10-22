"""Contract: b4's tracking rows and message flags export and import through b4's own APIs."""

from b4.review import messages, tracking
from gitutil import make_checkout
from test_b4state import add_tracked, enrolled

from b4_maintainer.adapters.b4state import (
    add_flags,
    all_flags,
    b4_repo,
    export_series,
    import_series,
    tracked_series,
)


def other_machine(tmp_path):
    root = tmp_path / 'other'
    make_checkout(root / 'projects' / 'demo')
    return enrolled(root)


def set_status(checkout, change_id, status):
    with b4_repo(checkout):
        conn = tracking.get_db('demo')
        tracking.update_series_status(conn, change_id, status, revision=2)
        conn.close()


def test_tracked_series_move_to_another_machine(ws, tmp_path):
    here = enrolled(ws)
    add_tracked(here, 'cid-1', 'cover@example.org', ['p1@example.org', 'p2@example.org'])
    records = export_series(here, 'demo')
    [row] = records['cid-1']['series']
    assert row['revision'] == 2 and row['status'] == 'new'
    assert [p['message_id'] for p in row['patches']] == ['p1@example.org', 'p2@example.org']

    there = other_machine(tmp_path)
    import_series(there, 'demo', records)
    [entry] = tracked_series(there, 'demo')
    assert (entry.change_id, entry.revision, entry.msgid) == ('cid-1', 2, 'cover@example.org')
    assert {'p1@example.org', 'p2@example.org'} <= entry.msgids
    assert export_series(there, 'demo') == records


def test_status_changes_import(ws, tmp_path):
    here = enrolled(ws)
    add_tracked(here, 'cid-1', 'cover@example.org')
    there = other_machine(tmp_path)
    import_series(there, 'demo', export_series(here, 'demo'))
    set_status(here, 'cid-1', 'archived')
    import_series(there, 'demo', export_series(here, 'demo'))
    [entry] = tracked_series(there, 'demo')
    assert entry.status == 'archived'


def test_review_branch_statuses_import_as_new(ws, tmp_path):
    here = enrolled(ws)
    add_tracked(here, 'cid-1', 'cover@example.org')
    set_status(here, 'cid-1', 'reviewing')
    there = other_machine(tmp_path)
    import_series(there, 'demo', export_series(here, 'demo'))
    [entry] = tracked_series(there, 'demo')
    assert entry.status == 'new'


def test_message_flags_move_with_every_flag(ws, tmp_path):
    here = enrolled(ws)
    with b4_repo(here):
        conn = messages.get_db()
        messages.set_flag(conn, 'a@example.org', 'Seen', '2026-10-20T00:00:00+00:00')
        messages.set_flag(conn, 'a@example.org', 'Answered')
        conn.close()
    there = other_machine(tmp_path)
    add_flags(there, all_flags(here))
    assert all_flags(there) == [
        {
            'msgid': 'a@example.org',
            'msg_date': '2026-10-20T00:00:00+00:00',
            'flags': 'Answered Seen',
        }
    ]

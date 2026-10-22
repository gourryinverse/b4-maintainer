"""Contract: a live workspace's state follows the maintainer to another machine."""

import datetime
import json

import pytest
from b4.review import messages
from gitutil import git, make_checkout
from test_b4state import add_tracked

from b4_maintainer.adapters.b4state import all_flags, b4_repo, tracked_series
from b4_maintainer.adapters.live import LiveSource
from b4_maintainer.cli import project_main
from b4_maintainer.projectsetup import setup_workspace

NOW = datetime.datetime(2026, 10, 22, 12, 0, tzinfo=datetime.timezone.utc)
CONFIG = '[[projects]]\nid = "demo"\npath = "projects/demo"\nlists = ["demo@lists.example.org"]\n'


@pytest.fixture
def remote(tmp_path, home):
    path = tmp_path / 'state.git'
    git('init', '-q', '--bare', '-b', 'main', str(path), cwd=tmp_path)
    return path


def machine(root, remote):
    """A machine: its clone of the state repository, set up as a new machine would be."""
    git('clone', '-q', str(remote), str(root / 'state'), cwd=root.parent)
    git('symbolic-ref', 'HEAD', 'refs/heads/main', cwd=root / 'state')
    if (root / 'state' / 'workspace.toml').exists():
        if not (root / 'projects' / 'demo').exists():
            make_checkout(root / 'projects' / 'demo')
        setup_workspace(root)
    else:
        assert project_main(['add', '-C', str(root), 'demo']) == 0
    return LiveSource(root, now=lambda: NOW)


def test_tracked_series_flags_and_client_state_reach_another_machine(ws, tmp_path, remote):
    a = machine(ws, remote)
    checkout = ws / 'projects' / 'demo'
    add_tracked(checkout, 'cid-1', 'cover@example.org', ['p1@example.org'])
    with b4_repo(checkout):
        conn = messages.get_db()
        messages.set_flag(conn, 'cover@example.org', 'Seen', '2026-10-20T00:00:00+00:00')
        conn.close()
    (ws / 'state' / 'client').mkdir()
    watched = [{'identity': 'thread:d@x', 'msgid': 'd@x', 'subject': 'Path?'}]
    (ws / 'state' / 'client' / 'watched.json').write_text(json.dumps(watched))
    assert 'synced' in a.sync()

    other = tmp_path / 'other'
    other.mkdir()
    b = machine(other, remote)
    b.sync()
    there = other / 'projects' / 'demo'
    assert [e.change_id for e in tracked_series(there, 'demo')] == ['cid-1']
    assert [f['msgid'] for f in all_flags(there)] == ['cover@example.org']
    assert b.watched.msgids() == {'d@x'}
    assert b.workspace().project('demo') is not None
    files = git('ls-files', cwd=other / 'state').splitlines()
    assert 'projects/demo/checkout.json' in files and 'b4/flags.tsv' in files


def test_a_fresh_machine_does_not_erase_tracked_state(ws, tmp_path, remote):
    a = machine(ws, remote)
    add_tracked(ws / 'projects' / 'demo', 'cid-1', 'cover@example.org')
    a.sync()
    other = tmp_path / 'other'
    other.mkdir()
    machine(other, remote).sync()
    a.sync()
    assert [e.change_id for e in tracked_series(ws / 'projects' / 'demo', 'demo')] == ['cid-1']
    assert 'cid-1.json' in git('ls-tree', '-r', '--name-only', 'main', cwd=remote)


def test_refresh_syncs_before_and_after_and_reports_failures(ws, tmp_path, remote):
    a = machine(ws, remote)
    calls = []
    a.sync = lambda timeout=None: calls.append('sync') or 'State synced'
    a._refresh = lambda progress: calls.append('refresh') or 'Refreshed'
    assert a.refresh() == 'Refreshed'
    assert calls == ['sync', 'refresh', 'sync']

    from b4_maintainer.adapters.statesync import SyncError

    def offline(timeout=None):
        raise SyncError('fetch failed: offline')

    a.sync = offline
    assert 'state sync failed: fetch failed: offline' in a.refresh()


def test_no_auto_sync_without_a_remote_or_when_disabled(ws, tmp_path):
    (ws / 'state').mkdir()
    git('init', '-q', '-b', 'main', cwd=ws / 'state')
    (ws / 'state' / 'workspace.toml').write_text(CONFIG)
    assert not LiveSource(ws).can_sync()
    git('remote', 'add', 'origin', str(tmp_path / 'r.git'), cwd=ws / 'state')
    assert LiveSource(ws).can_sync()
    (ws / 'state' / 'workspace.toml').write_text(CONFIG + '\n[sync]\nauto = false\n')
    assert not LiveSource(ws).can_sync()

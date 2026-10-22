"""Contract: workspace state syncs through a private Git repository without Git conflicts."""

import datetime
import json
import subprocess

import pytest
from gitutil import git

from b4_maintainer.adapters.statesync import (
    FlagLine,
    StateRepo,
    SyncError,
    flags_text,
    merge_states,
    parse_flags,
    series_files,
    series_path,
)

NOW = datetime.datetime(2026, 10, 22, 12, 0, tzinfo=datetime.timezone.utc)


def record(change_id, status='new', updated='2026-10-20T00:00:00+00:00', revision=1):
    data = {
        'change_id': change_id,
        'updated': updated,
        'series': [{'revision': revision, 'status': status, 'message_id': f'{change_id}@x'}],
        'revisions': [],
    }
    return (json.dumps(data, indent=1) + '\n').encode()


def client(*entries):
    return (json.dumps(list(entries), indent=1) + '\n').encode()


def watch(identity, subject='s'):
    return {'identity': identity, 'msgid': identity, 'subject': subject}


def merged(base, ours, theirs, now=NOW):
    result, notes = merge_states(base, ours, theirs, now=now)
    return result, notes


S = 'projects/linux/series/'


def test_one_sided_changes_are_taken():
    base = {S + 'a.json': record('a')}
    ours = {S + 'a.json': record('a', 'archived', '2026-10-21T00:00:00+00:00')}
    result, _ = merged(base, ours, base)
    assert result[S + 'a.json'] == ours[S + 'a.json']
    result, _ = merged(base, base, ours)
    assert result[S + 'a.json'] == ours[S + 'a.json']


def test_a_fresh_machine_exporting_nothing_deletes_nothing():
    base = {S + 'a.json': record('a'), 'b4/flags.tsv': b'm@x\t2026-10-21\tSeen\t\n'}
    result, _ = merged(base, {}, base)
    assert result == base


def test_concurrent_series_edits_keep_the_newer():
    base = {S + 'a.json': record('a')}
    ours = {S + 'a.json': record('a', 'archived', '2026-10-21T00:00:00+00:00')}
    theirs = {S + 'a.json': record('a', 'new', '2026-10-22T00:00:00+00:00', revision=2)}
    result, _ = merged(base, ours, theirs)
    assert result[S + 'a.json'] == theirs[S + 'a.json']


def test_client_entries_merge_per_identity_and_removals_propagate():
    base = {'client/watched.json': client(watch('thread:a'), watch('thread:b'))}
    ours = {'client/watched.json': client(watch('thread:a'))}  # Unwatched b here.
    theirs = {
        'client/watched.json': client(watch('thread:a'), watch('thread:b'), watch('thread:c'))
    }
    result, _ = merged(base, ours, theirs)
    identities = [e['identity'] for e in json.loads(result['client/watched.json'])]
    assert identities == ['thread:a', 'thread:c']


def test_flags_union_and_prune_by_age_unless_kept():
    old, recent = '2026-06-01T00:00:00+00:00', '2026-10-20T00:00:00+00:00'
    ours = flags_text(
        [
            FlagLine('new@x', recent, 'Seen', ''),
            FlagLine('both@x', recent, 'Seen', ''),
            FlagLine('kept@x', old, 'Seen', 'a@x'),
        ]
    )
    theirs = flags_text(
        [
            FlagLine('both@x', recent, 'Answered', ''),
            FlagLine('stale@x', old, 'Seen', ''),
            FlagLine('dropped@x', old, 'Seen', 'gone@x'),
        ]
    )
    tracked = {S + 'a.json': record('a')}  # Anchors message-id a@x.
    result, _ = merged({}, {**tracked, 'b4/flags.tsv': ours}, {**tracked, 'b4/flags.tsv': theirs})
    lines = {f.msgid: f for f in parse_flags(result['b4/flags.tsv'])}
    assert set(lines) == {'new@x', 'both@x', 'kept@x'}
    assert lines['both@x'].flags == 'Answered Seen'


def test_workspace_toml_merges_as_text_and_keeps_local_on_conflict():
    base = {'workspace.toml': b'a = 1\n\nb = 2\n\nc = 3\n'}
    ours = {'workspace.toml': b'a = 10\n\nb = 2\n\nc = 3\n'}
    theirs = {'workspace.toml': b'a = 1\n\nb = 2\n\nc = 30\n'}
    result, notes = merged(base, ours, theirs)
    assert result['workspace.toml'] == b'a = 10\n\nb = 2\n\nc = 30\n'
    assert notes == []
    clash = {'workspace.toml': b'a = 99\n\nb = 2\n\nc = 3\n'}
    result, notes = merged(base, ours, clash)
    assert result['workspace.toml'] == ours['workspace.toml']
    assert notes and 'workspace.toml' in notes[0]


def test_old_archived_records_are_dropped():
    ours = {S + 'a.json': record('a', 'archived', '2026-06-01T00:00:00+00:00')}
    result, _ = merged({}, ours, {})
    assert result == {}


def test_series_files_keep_their_stamp_until_content_changes():
    rec = {'change_id': 'c/1', 'series': [{'revision': 1, 'status': 'new'}], 'revisions': []}
    files = series_files('linux', {'c/1': rec}, {}, NOW)
    [path] = files
    assert path == series_path('linux', 'c/1') and path.count('/') == 3
    stamp = json.loads(files[path])['updated']
    later = NOW + datetime.timedelta(days=1)
    assert json.loads(series_files('linux', {'c/1': rec}, files, later)[path])['updated'] == stamp
    changed = {**rec, 'series': [{'revision': 1, 'status': 'archived'}]}
    assert (
        json.loads(series_files('linux', {'c/1': changed}, files, later)[path])['updated'] != stamp
    )


def test_branch_statuses_are_not_overwritten_by_new():
    reviewing = {'change_id': 'c', 'series': [{'revision': 1, 'status': 'reviewing'}]}
    files = series_files('linux', {'c': reviewing}, {}, NOW)
    imported = {'change_id': 'c', 'series': [{'revision': 1, 'status': 'new'}]}
    files = series_files('linux', {'c': imported}, files, NOW)
    assert json.loads(files[series_path('linux', 'c')])['series'][0]['status'] == 'reviewing'


# Repositories: a bare remote and two machines' clones.


@pytest.fixture
def remote(tmp_path, home):
    path = tmp_path / 'remote.git'
    git('init', '-q', '--bare', '-b', 'main', str(path), cwd=tmp_path)
    return path


def clone(remote, path):
    git('clone', '-q', str(remote), str(path), cwd=remote.parent)
    git('symbolic-ref', 'HEAD', 'refs/heads/main', cwd=path)
    return StateRepo(path)


class Machine:
    """A state repository plus a fake b4: export writes `records`, import records changes."""

    def __init__(self, repo):
        self.repo = repo
        self.records = {}  # path -> bytes this machine's b4 holds
        self.imported = []

    def export(self, current):
        return dict(self.records)

    def import_(self, changes):
        self.imported.append(changes)
        for path, data in changes.items():
            if data is None:
                self.records.pop(path, None)
            else:
                self.records[path] = data

    def sync(self):
        return self.repo.sync(self.export, self.import_, now=NOW)


def test_state_moves_between_machines(remote, tmp_path):
    a = Machine(clone(remote, tmp_path / 'a'))
    a.records[S + 'x.json'] = record('x')
    (a.repo.path / 'workspace.toml').write_text('[[projects]]\nid = "linux"\n')
    a.sync()

    b = Machine(clone(remote, tmp_path / 'b'))
    b.sync()
    assert b.records == a.records
    assert (b.repo.path / 'workspace.toml').read_text().startswith('[[projects]]')
    assert git('status', '--porcelain', cwd=b.repo.path) == ''


def test_concurrent_offline_work_merges_and_pushes(remote, tmp_path):
    a = Machine(clone(remote, tmp_path / 'a'))
    a.records[S + 'x.json'] = record('x')
    a.sync()
    b = Machine(clone(remote, tmp_path / 'b'))
    b.sync()

    a.records[S + 'y.json'] = record('y')
    b.records[S + 'z.json'] = record('z')
    a.sync()
    b.sync()  # Behind the remote: merged, then pushed on top.
    a.sync()
    assert set(a.records) == set(b.records) == {S + 'x.json', S + 'y.json', S + 'z.json'}
    log = git('log', '--format=%P', 'origin/main', cwd=b.repo.path).splitlines()
    assert all(len(parents.split()) <= 1 for parents in log)  # Linear history.


def test_a_rejected_push_merges_again(remote, tmp_path, monkeypatch):
    a = Machine(clone(remote, tmp_path / 'a'))
    a.sync()
    b = Machine(clone(remote, tmp_path / 'b'))
    b.records[S + 'b.json'] = record('b')
    original = StateRepo.fetch
    raced = []

    def fetch_then_race(self, timeout=None):
        original(self, timeout)
        if self is b.repo and not raced:
            raced.append(True)
            a.records[S + 'a.json'] = record('a')
            a.sync()  # Lands between b's fetch and push.

    monkeypatch.setattr(StateRepo, 'fetch', fetch_then_race)
    b.sync()
    assert {S + 'a.json', S + 'b.json'} <= set(b.records)
    remote_files = git('ls-tree', '-r', '--name-only', 'main', cwd=remote).splitlines()
    assert {S + 'a.json', S + 'b.json'} <= set(remote_files)


def test_an_empty_remote_and_unborn_branch_sync(remote, tmp_path):
    path = tmp_path / 'state'
    path.mkdir()
    git('init', '-q', '-b', 'main', cwd=path)
    git('remote', 'add', 'origin', str(remote), cwd=path)
    (path / 'client').mkdir()
    (path / 'client' / 'hidden.json').write_text('[]\n')
    m = Machine(StateRepo(path))
    m.sync()
    assert git('ls-tree', '-r', '--name-only', 'main', cwd=remote) == 'client/hidden.json'


def test_without_a_remote_sync_commits_locally(tmp_path, home):
    path = tmp_path / 'state'
    path.mkdir()
    git('init', '-q', '-b', 'main', cwd=path)
    m = Machine(StateRepo(path))
    m.records[S + 'x.json'] = record('x')
    m.sync()
    assert git('ls-tree', '-r', '--name-only', 'HEAD', cwd=path) == S + 'x.json'


def test_an_unreachable_remote_is_a_sync_error_and_keeps_the_local_commit(tmp_path, home):
    path = tmp_path / 'state'
    path.mkdir()
    git('init', '-q', '-b', 'main', cwd=path)
    git('remote', 'add', 'origin', str(tmp_path / 'missing.git'), cwd=path)
    m = Machine(StateRepo(path))
    m.records[S + 'x.json'] = record('x')
    with pytest.raises(SyncError):
        m.sync()
    assert git('ls-tree', '-r', '--name-only', 'HEAD', cwd=path) == S + 'x.json'


def test_sync_commits_are_attributed_without_a_global_identity(remote, tmp_path, monkeypatch):
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(tmp_path / 'nothing'))
    a = Machine(clone(remote, tmp_path / 'a'))
    a.records[S + 'x.json'] = record('x')
    a.sync()
    author = subprocess.run(
        ['git', 'log', '-1', '--format=%an', 'main'], cwd=remote, capture_output=True, text=True
    ).stdout.strip()
    assert author == 'b4-maintainer'

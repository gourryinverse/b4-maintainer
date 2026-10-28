"""Contract: workspace state syncs through a private Git repository without Git conflicts."""

import datetime
import json

from b4_maintainer.adapters.statesync import (
    FlagLine,
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

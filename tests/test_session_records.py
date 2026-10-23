"""Contract: session records round-trip; hook events, read incrementally, drive states."""

import json

import pytest
from sessiondata import NOW, make_record, payload

from b4_maintainer.sessions.records import (
    EventLog,
    SessionRecord,
    Worktree,
    derive_state,
    event_from_payload,
    most_urgent,
)

record = make_record


def test_records_round_trip_through_json(tmp_path):
    wt = Worktree('var/worktrees/s1', 'b4m/session/s1', 'v6.18', 'deadbeef')
    rec = record(mode='worktree', worktree=wt, provenance={'review-prompts': '1234'})
    path = tmp_path / 's1' / 'session.json'
    rec.save(path)
    assert SessionRecord.load(path) == rec
    assert json.loads(path.read_text())['worktree']['branch'] == 'b4m/session/s1'


def events(*pairs):
    return [event_from_payload(payload(name, **fields), NOW) for name, fields in pairs]


@pytest.mark.parametrize(
    'sequence, state',
    [
        ((), 'starting'),
        ((('SessionStart', {}),), 'waiting'),
        ((('SessionStart', {}), ('UserPromptSubmit', {'prompt': 'x'})), 'working'),
        ((('UserPromptSubmit', {}), ('PreToolUse', {'tool_name': 'Read'})), 'working'),
        (
            (
                ('UserPromptSubmit', {}),
                ('Notification', {'message': 'Claude needs your permission to use Bash'}),
            ),
            'approval',
        ),
        (
            (
                ('UserPromptSubmit', {}),
                ('Notification', {'notification_type': 'permission_prompt'}),
            ),
            'approval',
        ),
        (
            (
                ('UserPromptSubmit', {}),
                ('Notification', {'message': 'Claude is waiting for your input'}),
            ),
            'waiting',
        ),
        ((('UserPromptSubmit', {}), ('Stop', {})), 'waiting'),
        ((('Stop', {}), ('SessionEnd', {'reason': 'exit'})), 'ended'),
        ((('Stop', {}), ('SomethingNew', {})), 'waiting'),  # Unknown events are ignored.
    ],
)
def test_events_drive_the_state(sequence, state):
    assert derive_state(record(), events(*sequence), window_alive=True) == state


def test_a_closed_window_is_gone_unless_ended_and_app_states_win():
    working = events(('UserPromptSubmit', {}))
    assert derive_state(record(), working, window_alive=False) == 'gone'
    ended = events(('SessionEnd', {}))
    assert derive_state(record(), ended, window_alive=False) == 'ended'
    assert derive_state(record(state='stopped'), working, window_alive=False) == 'stopped'
    assert derive_state(record(state='failed'), [], window_alive=False) == 'failed'


def test_the_event_log_reads_incrementally_and_skips_malformed_lines(tmp_path):
    path = tmp_path / 'events.jsonl'
    log = EventLog(path)
    assert log.read_new() == []
    with path.open('a') as fh:
        fh.write(json.dumps({'time': NOW, 'event': 'SessionStart'}) + '\n')
        fh.write('not json\n')
        fh.write(json.dumps({'time': NOW, 'event': 'UserPromptSubmit'}))  # No newline yet.
    assert [e.event for e in log.read_new()] == ['SessionStart']
    with path.open('a') as fh:
        fh.write('\n' + json.dumps({'time': NOW, 'event': 'Stop'}) + '\n')
    assert [e.event for e in log.read_new()] == ['UserPromptSubmit', 'Stop']
    assert log.read_new() == []


def test_payloads_become_compact_events():
    event = event_from_payload(
        payload('PreToolUse', tool_name='Bash', tool_input={'command': 'make'}), NOW
    )
    assert (event.event, event.tool, event.time) == ('PreToolUse', 'Bash', NOW)
    assert event_from_payload('not json', NOW) is None
    assert event_from_payload(json.dumps({'no': 'event'}), NOW) is None


def test_most_urgent_orders_what_the_drawer_line_shows():
    assert most_urgent(['working', 'approval', 'waiting']) == 'approval'
    assert most_urgent(['ended', 'working']) == 'working'
    assert most_urgent([]) is None

"""Contract: `b4-maintainer hook <id>` records events and guards shell commands."""

import json
import os
import subprocess
import sys

import pytest
from sessiondata import NOW, FakeSessionRunner, make_record, payload

from b4_maintainer.sessions.hook import run_hook
from b4_maintainer.sessions.store import SessionStore

EVENTS = [
    payload('SessionStart', source='startup'),
    payload('UserPromptSubmit', prompt='synthetic prompt'),
    payload('PreToolUse', tool_name='Read', tool_input={'file_path': 'x'}),
    payload('PostToolUse', tool_name='Read', tool_response={'content': 'x'}),
    payload('Notification', notification_type='permission_prompt', message='Allow?'),
    payload('Stop', stop_hook_active=False),
    payload('SessionEnd', reason='exit'),
]


@pytest.fixture
def store(tmp_path):
    store = SessionStore(tmp_path / 'var' / 'sessions')
    FakeSessionRunner(store).add(make_record('s1'))
    return store


def bash(command):
    return payload('PreToolUse', tool_name='Bash', tool_input={'command': command})


def decision(stdout):
    out = json.loads(stdout)['hookSpecificOutput']
    return out['hookEventName'], out['permissionDecision'], out['permissionDecisionReason']


def test_each_payload_appends_its_event(store):
    for raw in EVENTS:
        assert run_hook(store, 's1', raw, NOW) == (0, '', '')
    lines = (store.directory('s1') / 'events.jsonl').read_text().splitlines()
    assert [json.loads(line)['event'] for line in lines] == [
        json.loads(raw)['hook_event_name'] for raw in EVENTS
    ]
    assert 'synthetic prompt' not in ''.join(lines)  # Payload content is not kept.


def test_the_guard_denies_shell_commands_outside_the_sandbox(store):
    code, out, _err = run_hook(store, 's1', bash('make -j8; curl example.org'), NOW)
    assert code == 0
    event, verdict, reason = decision(out)
    assert (event, verdict) == ('PreToolUse', 'deny')
    assert 'shell control' in reason


@pytest.mark.parametrize('command', ['b4-maintainer sandbox s1 -- make -j8', 'git log -1'])
def test_allowed_commands_get_no_decision(store, command):
    # No decision: Claude's own permission settings and prompt still apply.
    assert run_hook(store, 's1', bash(command), NOW) == (0, '', '')


def test_a_missing_session_is_a_non_blocking_error(store):
    code, out, err = run_hook(store, 'nope', payload('Stop'), NOW)
    assert (code, out) == (1, '')  # Exit 2 would block (or, for Stop, keep the agent going).
    assert 'nope' in err


def test_the_guard_applies_even_without_a_session(store):
    for sid in ('nope', '../s1'):
        code, out, _err = run_hook(store, sid, bash('rm -rf ~'), NOW)
        assert code == 0
        assert decision(out)[1] == 'deny'


def test_malformed_payloads_are_non_blocking_errors(store):
    for raw in ('', '{', '[]', '{"no_event": 1}'):
        code, out, err = run_hook(store, 's1', raw, NOW)
        assert (code, out) == (1, '') and err
    assert not (store.directory('s1') / 'events.jsonl').exists()


def test_the_cli_reads_stdin_without_loading_the_ui(tmp_path):
    store = SessionStore(tmp_path / 'var' / 'sessions')
    FakeSessionRunner(store).add(make_record('s1'))
    probe = (
        'import sys; from b4_maintainer.cli import main\n'
        'try:\n    main(sys.argv[1:])\n'
        'finally:\n    print("textual" in sys.modules, file=sys.stderr)\n'
    )
    result = subprocess.run(
        [sys.executable, '-c', probe, 'hook', '-C', str(tmp_path), 's1'],
        input=bash('curl example.org'),
        capture_output=True,
        text=True,
        env={**os.environ, 'HOME': str(tmp_path)},
    )
    assert result.returncode == 0, result.stderr
    assert decision(result.stdout)[1] == 'deny'
    assert result.stderr.strip() == 'False'
    assert (store.directory('s1') / 'events.jsonl').read_text().count('PreToolUse') == 1

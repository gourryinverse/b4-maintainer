"""Contract: the backend list is configuration; missing or limited backends show why."""

from b4_maintainer.sessions.backends import backend_entries, launch_problems


def found(*names):
    return lambda command: f'/usr/bin/{command}' if command in names else None


def ids(entries):
    return [e.id for e in entries]


def test_built_ins_are_listed_and_unsupported_ones_say_why():
    entries = {e.id: e for e in backend_entries([], which=found('claude', 'codex'))}
    assert list(entries) == ['claude', 'codex', 'pi', 'dry-run']
    assert entries['claude'].available and entries['dry-run'].available
    assert not entries['codex'].available and 'not supported yet' in entries['codex'].reason


def test_configured_entries_add_replace_and_hide():
    config = [
        {'id': 'claude-cyber', 'adapter': 'claude', 'command': ['claude-cyber', '--x']},
        {'id': 'codex-red', 'adapter': 'codex', 'command': 'codex-red'},
        {'id': 'claude', 'adapter': 'claude', 'command': ['/opt/claude/bin/claude']},
        {'id': 'pi', 'enabled': False},
    ]
    entries = {e.id: e for e in backend_entries(config, which=found('claude-cyber'))}
    assert ids(entries.values()) == [
        'claude',
        'codex',
        'dry-run',
        'claude-cyber',
        'codex-red',
    ]
    assert entries['claude'].command == ('/opt/claude/bin/claude',)
    cyber = entries['claude-cyber']
    assert cyber.adapter == 'claude' and cyber.command == ('claude-cyber', '--x')
    assert cyber.available and cyber.state_events  # Inherits the Claude adapter.
    assert not entries['claude'].available  # /opt/... is not installed here.
    assert 'not installed' in entries['claude'].reason


def test_bad_entries_are_reported_not_fatal():
    config = [
        {'id': 'x', 'adapter': 'gemini', 'command': ['x']},
        {'adapter': 'claude', 'command': ['y']},
        {'id': 'z', 'adapter': 'claude', 'command': []},
    ]
    entries = {e.id: e for e in backend_entries(config, which=found('x', 'y'))}
    assert 'x' in entries and not entries['x'].available and 'adapter' in entries['x'].reason
    assert 'z' in entries and 'command' in entries['z'].reason
    assert len(entries) == 6  # The id-less entry is skipped.


def test_env_and_models_are_kept():
    config = [
        {
            'id': 'local',
            'adapter': 'pi',
            'command': ['pi'],
            'models': ['llama'],
            'env': {'PI_HOME': '/srv/pi'},
        }
    ]
    [local] = [e for e in backend_entries(config, which=found('pi')) if e.id == 'local']
    assert local.models == ('llama',) and local.env == {'PI_HOME': '/srv/pi'}


def test_launch_problems_refuse_unenforceable_permissions():
    entries = {e.id: e for e in backend_entries([], which=found('claude'))}
    assert launch_problems(entries['claude'], 'read-only') == []
    assert launch_problems(entries['claude'], 'execute') == []
    assert launch_problems(entries['codex'], 'read-only')  # Not available, and why.
    limited = entries['claude'].with_capabilities(enforces=frozenset({'proposals'}))
    [problem] = launch_problems(limited, 'read-only')
    assert 'cannot enforce read-only' in problem


def test_dry_run_needs_nothing_installed_and_runs_no_provider():
    [dry] = [e for e in backend_entries([], which=found()) if e.id == 'dry-run']
    assert dry.available and dry.state_events
    assert launch_problems(dry, 'execute') == []

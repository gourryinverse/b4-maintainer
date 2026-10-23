# SPDX-License-Identifier: GPL-2.0-or-later
"""The backend list: built-in entries plus `[[backends]]` from workspace.toml.

Each entry speaks one adapter's protocol (claude, codex, other, pi, or the built-in
dry-run) with its own command, so a specially provisioned harness inherits its
adapter's capabilities. Entries are configured by hand; the UI only lists them, and
an entry that cannot run here is shown disabled with the reason.
"""

import dataclasses
import shutil
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence

Which = Callable[[str], Optional[str]]
ALL_PERMISSIONS = frozenset({'read-only', 'proposals', 'execute'})


@dataclass(frozen=True)
class Adapter:
    """What a protocol can do; entries inherit it."""

    name: str
    command: tuple[str, ...]
    supported: bool  # Implemented in this client yet.
    state_events: bool = False
    enforces: frozenset[str] = frozenset()  # Permission profiles it can enforce.
    resume: bool = False
    headless: bool = False


ADAPTERS = {
    'claude': Adapter(
        'claude',
        ('claude',),
        True,
        state_events=True,
        enforces=ALL_PERMISSIONS,
        resume=True,
        headless=True,
    ),
    'codex': Adapter('codex', ('codex',), False),
    'other': Adapter('other', ('other',), False),
    'pi': Adapter('pi', ('pi',), False),
    # Runs a script instead of an agent; the launch, hooks, and sandbox are real.
    'dry-run': Adapter(
        'dry-run',
        (),
        True,
        state_events=True,
        enforces=ALL_PERMISSIONS,
        resume=True,
    ),
}


@dataclass(frozen=True)
class BackendEntry:
    id: str
    adapter: str
    command: tuple[str, ...]
    models: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    state_events: bool = False
    enforces: frozenset[str] = frozenset()
    resume: bool = False
    headless: bool = False
    available: bool = False
    reason: str = ''  # Why it is unavailable.

    def with_capabilities(self, **changes: Any) -> 'BackendEntry':
        return dataclasses.replace(self, **changes)


def _entry(eid: str, adapter_name: str, raw: Mapping[str, Any], which: Which) -> BackendEntry:
    adapter = ADAPTERS.get(adapter_name)
    if adapter is None:
        return BackendEntry(eid, adapter_name, (), reason=f'unknown adapter {adapter_name!r}')
    command = raw.get('command', list(adapter.command))
    if isinstance(command, str):
        command = [command]
    models = raw.get('models', [])
    env = raw.get('env', {})
    entry = BackendEntry(
        eid,
        adapter.name,
        tuple(str(c) for c in command) if isinstance(command, list) else (),
        tuple(str(m) for m in models) if isinstance(models, list) else (),
        {str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else {},
        adapter.state_events,
        adapter.enforces,
        adapter.resume,
        adapter.headless,
    )
    if not adapter.supported:
        return dataclasses.replace(entry, reason=f'{adapter.name} adapter not supported yet')
    if adapter.name == 'dry-run':
        return dataclasses.replace(entry, available=True)
    if not entry.command:
        return dataclasses.replace(entry, reason='no command configured')
    if which(entry.command[0]) is None:
        return dataclasses.replace(entry, reason=f'{entry.command[0]} is not installed here')
    return dataclasses.replace(entry, available=True)


def backend_entries(
    config: Sequence[Mapping[str, Any]], which: Which = shutil.which
) -> list[BackendEntry]:
    """Built-ins in order, replaced or hidden by configured entries of the same id, then
    the other configured entries in config order. Entries without an id are skipped."""
    configured: dict[str, Mapping[str, Any]] = {}
    for raw in config:
        eid = raw.get('id') if isinstance(raw, Mapping) else None
        if isinstance(eid, str) and eid:
            configured[eid] = raw
    order = [*ADAPTERS, *(eid for eid in configured if eid not in ADAPTERS)]
    entries = []
    for eid in order:
        raw = configured.get(eid, {})
        if raw.get('enabled', True) is False:
            continue
        adapter = raw.get('adapter', eid if eid in ADAPTERS else '')
        entries.append(_entry(eid, str(adapter), raw, which))
    return entries


def launch_problems(entry: BackendEntry, permissions: str) -> list[str]:
    """Why a session with *permissions* cannot launch on *entry* (empty: it can)."""
    if not entry.available:
        return [f'{entry.id}: {entry.reason}']  # Its capabilities do not matter yet.
    if permissions not in entry.enforces:
        return [f'{entry.id} cannot enforce {permissions}']
    return []

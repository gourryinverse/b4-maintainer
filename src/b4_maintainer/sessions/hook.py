# SPDX-License-Identifier: GPL-2.0-or-later
"""`b4-maintainer hook <id>`: an agent CLI's hook, run once per event.

Records the event for the drawer and applies the shell guard to `PreToolUse` of Bash.
Speaks Claude Code's hook protocol: a JSON `permissionDecision` on stdout denies a tool
call; exit 1 is a non-blocking error (exit 2 would block the prompt, or keep a stopping
agent going, so it is never used). The guard applies whether or not the session's record
exists. Kept free of UI imports: it runs on every tool call.
"""

import json
from typing import Any

from .guard import rejection
from .records import event_from_payload
from .store import SessionStore

Result = tuple[int, str, str]  # Exit code, stdout, stderr.


def _deny(reason: str) -> str:
    return json.dumps(
        {
            'hookSpecificOutput': {
                'hookEventName': 'PreToolUse',
                'permissionDecision': 'deny',
                'permissionDecisionReason': f'b4-maintainer: {reason}',
            }
        }
    )


def _shell_command(data: Any) -> Any:
    """The command of a Bash PreToolUse payload; None for any other payload."""
    if not isinstance(data, dict) or data.get('hook_event_name') != 'PreToolUse':
        return None
    if data.get('tool_name') != 'Bash':
        return None
    tool_input = data.get('tool_input')
    return tool_input.get('command', '') if isinstance(tool_input, dict) else ''


def run_hook(
    store: SessionStore, session_id: str, raw: str, now: str, wrapper_path: str = ''
) -> Result:
    try:
        data = json.loads(raw)
    except ValueError:
        return 1, '', 'b4-maintainer hook: payload is not JSON\n'
    out = ''
    command = _shell_command(data)
    if command is not None:
        reason = (
            rejection(command, session_id, wrapper_path)
            if isinstance(command, str)
            else 'command is not text'
        )
        if reason is not None:
            out = _deny(reason)
    event = event_from_payload(raw, now)
    if event is None:
        return (0, out, '') if out else (1, '', 'b4-maintainer hook: no hook event name\n')
    if not store.exists(session_id):
        err = f'b4-maintainer hook: no session {session_id!r}\n'
        return (0, out, err) if out else (1, '', err)
    store.append(session_id, event)
    return 0, out, ''

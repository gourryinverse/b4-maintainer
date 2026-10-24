# SPDX-License-Identifier: GPL-2.0-or-later
"""`b4-maintainer hook <id>`: the b4-maintainer pi extension's call into the client.

Records an event for the drawer and decides whether a shell command may run: for a
`tool` event of `bash`, a refusal is `{"decision": "deny", "reason": ...}` on stdout,
which the extension returns to pi as a blocked call; anything else gets no decision.
Errors exit 1 and never block. The guard applies whether or not the session's record
exists. Kept free of UI imports: it runs on every shell command.
"""

import json
from typing import Any

from .guard import rejection
from .records import event_from_payload
from .store import SessionStore

Result = tuple[int, str, str]  # Exit code, stdout, stderr.


def _deny(reason: str) -> str:
    return json.dumps({'decision': 'deny', 'reason': f'b4-maintainer: {reason}'})


def _shell_command(data: Any) -> Any:
    """The command of a bash tool event; None for any other payload."""
    if not isinstance(data, dict) or data.get('event') != 'tool' or data.get('tool') != 'bash':
        return None
    tool_input = data.get('input')
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
        execute = isinstance(data, dict) and data.get('permissions') == 'execute'
        readable = data.get('readable') if isinstance(data, dict) else None
        dirs = [r for r in readable if isinstance(r, str)] if isinstance(readable, list) else []
        reason = (
            rejection(command, session_id, wrapper_path, execute, dirs)
            if isinstance(command, str)
            else 'command is not text'
        )
        if reason is not None:
            out = _deny(reason)
    event = event_from_payload(raw, now)
    if event is None:
        return (0, out, '') if out else (1, '', 'b4-maintainer hook: no event name\n')
    if not store.exists(session_id):
        err = f'b4-maintainer hook: no session {session_id!r}\n'
        return (0, out, err) if out else (1, '', err)
    store.append(session_id, event)
    return 0, out, ''

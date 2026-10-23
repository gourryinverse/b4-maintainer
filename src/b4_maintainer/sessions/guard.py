# SPDX-License-Identifier: GPL-2.0-or-later
"""Which shell commands a session may run: the sandbox wrapper, or read-only git.

The agent's own permission prompt could approve anything, so adapters enforce this
outside the model (for Claude, a PreToolUse hook). Anything the shell would interpret
beyond plain words -- control operators, redirections, substitutions -- is refused
before parsing: `wrapper -- make; curl ...` must not run curl outside the sandbox.
Pipelines belong inside the sandbox: `b4-maintainer sandbox <id> -- sh -c '...'`.
"""

import shlex
from typing import Optional

WRAPPER = 'b4-maintainer'
READ_ONLY_GIT = frozenset(
    {'log', 'show', 'diff', 'status', 'blame', 'grep', 'ls-files', 'rev-parse', 'describe'}
)
# Options that write files or run configured programs, even in read-only subcommands.
GIT_DENIED = ('--output', '--ext-diff', '--textconv', '-O', '--open-files-in-pager')
CONTROL = frozenset(';&|<>()\n\r')


def _shell_syntax(command: str) -> Optional[str]:
    """The first shell construct beyond plain words, outside single quotes."""
    quote = ''
    i = 0
    while i < len(command):
        c = command[i]
        if quote == "'":
            if c == "'":
                quote = ''
        elif c == '\\':
            i += 2  # An escaped character is literal.
            continue
        elif c in '$`':
            return 'expansion'  # Substitutions expand inside double quotes too.
        elif quote == '"':
            if c == '"':
                quote = ''
        elif c in '\'"':
            quote = c
        elif c in CONTROL:
            return 'shell control'
        i += 1
    return None


def rejection(command: str, session_id: str, wrapper_path: str = '') -> Optional[str]:
    """Why *command* may not run in session *session_id*; None if it may.

    The wrapper is the bare name (found on PATH) or exactly *wrapper_path*, never a file
    that merely has its name: a session that can write files must not plant its own.
    """
    if not command.strip():
        return 'empty command'
    syntax = _shell_syntax(command)
    if syntax is not None:
        return f'{syntax} is not allowed; run pipelines inside the sandbox wrapper'
    try:
        words = shlex.split(command)
    except ValueError as ex:
        return f'command cannot be parsed: {ex}'
    if not words:
        return 'empty command'
    program = words[0]
    if program in (WRAPPER, wrapper_path or WRAPPER) and words[1:2] == ['sandbox']:
        if len(words) < 4 or words[3] != '--':
            return f'not the sandbox wrapper: use {WRAPPER} sandbox {session_id} -- COMMAND'
        if words[2] != session_id:
            return f'the sandbox of another session ({words[2]})'
        if len(words) == 4:
            return 'no command given to the sandbox wrapper'
        return None
    if program == 'git':  # Bare: found on PATH, as for the wrapper.
        if len(words) > 1 and words[1] in READ_ONLY_GIT:
            if not any(w.startswith(GIT_DENIED) for w in words[2:]):
                return None
        return 'not a read-only git command'
    return f'not the sandbox wrapper: use {WRAPPER} sandbox {session_id} -- COMMAND'

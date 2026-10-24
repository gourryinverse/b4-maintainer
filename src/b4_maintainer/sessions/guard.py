# SPDX-License-Identifier: GPL-2.0-or-later
"""Which shell commands a session may run: the sandbox wrapper, or read-only git.

Agents have no trustworthy permission prompt of their own, so this is enforced
outside the model (the b4-maintainer pi extension). Anything the shell would interpret
beyond plain words -- control operators, redirections, substitutions -- is refused
before parsing: `wrapper -- make; curl ...` must not run curl outside the sandbox.
Pipelines belong inside the sandbox: `b4-maintainer sandbox <id> -- sh -c '...'`.
"""

import os
import shlex
from typing import Optional, Sequence

WRAPPER = 'b4-maintainer'
READ_ONLY_GIT = frozenset(
    {'log', 'show', 'diff', 'status', 'blame', 'grep', 'ls-files', 'rev-parse', 'describe'}
)
# Options that write files, run configured programs, or read outside the repository.
GIT_DENIED = ('--output', '--ext-diff', '--textconv', '-O', '--open-files-in-pager', '--no-index')
CONTROL = frozenset(';&|<>()\n\r')
# Without the execute profile the shell is for read-only git only; files go through pi's
# write tool, which the extension keeps under the session's out/.
ONLY_GIT = (
    "only read-only git commands run in this session's shell; "
    'inspect files with the read, grep, find, and ls tools, '
    'and write files with the write tool under the output directory (out/)'
)


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


def _within(path: str, roots: Sequence[str]) -> bool:
    real = os.path.realpath(path)
    for root in roots:
        top = os.path.realpath(root)
        if real == top or real.startswith(top.rstrip(os.sep) + os.sep):
            return True
    return False


def rejection(
    command: str,
    session_id: str,
    wrapper_path: str = '',
    execute: bool = True,
    git_dirs: Sequence[str] = (),
) -> Optional[str]:
    """Why *command* may not run in session *session_id*; None if it may.

    The wrapper is the bare name (found on PATH) or exactly *wrapper_path*, never a file
    that merely has its name: a session that can write files must not plant its own.
    Without *execute* the wrapper is refused too, and refusals say what the session can do.
    Read-only git may name a directory with `-C` when it lies within *git_dirs*.
    """
    if not command.strip():
        return 'empty command'
    syntax = _shell_syntax(command)
    if syntax is not None:
        if not execute:
            return f'{syntax} is not allowed: {ONLY_GIT}'
        return f'{syntax} is not allowed; run pipelines inside the sandbox wrapper'
    try:
        words = shlex.split(command)
    except ValueError as ex:
        return f'command cannot be parsed: {ex}'
    if not words:
        return 'empty command'
    program = words[0]
    if program in (WRAPPER, wrapper_path or WRAPPER) and words[1:2] == ['sandbox']:
        if not execute:
            return f'this session cannot run commands (no execute permission): {ONLY_GIT}'
        if len(words) < 4 or words[3] != '--':
            return f'not the sandbox wrapper: use {WRAPPER} sandbox {session_id} -- COMMAND'
        if words[2] != session_id:
            return f'the sandbox of another session ({words[2]})'
        if len(words) == 4:
            return 'no command given to the sandbox wrapper'
        return None
    if program == 'git':  # Bare: found on PATH, as for the wrapper.
        args = words[1:]
        if args[:1] == ['-C'] and len(args) > 2 and _within(args[1], git_dirs):
            args = args[2:]
        if args and args[0] in READ_ONLY_GIT:
            if not any(w.startswith(GIT_DENIED) for w in args[1:]):
                return None
        return 'not a read-only git command'
    if not execute:
        return ONLY_GIT
    return f'not the sandbox wrapper: use {WRAPPER} sandbox {session_id} -- COMMAND'

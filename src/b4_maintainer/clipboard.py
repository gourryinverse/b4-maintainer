# SPDX-License-Identifier: GPL-2.0-or-later
"""The desktop's clipboard tools, beside the terminal's (OSC 52).

Some terminals (VTE-based ones, for one) ignore OSC 52, so a copy also goes through
the session's own tool: wl-copy under Wayland, xclip or xsel under X, and tmux's
paste buffer inside tmux. $B4M_CLIPBOARD, a command reading the text on stdin,
replaces them.
"""

import os
import shlex
import shutil
import subprocess
from typing import Callable, Mapping, Optional, Sequence

ENV = 'B4M_CLIPBOARD'


def commands(
    env: Mapping[str, str] = os.environ,
    which: Callable[[str], Optional[str]] = shutil.which,
) -> list[list[str]]:
    """The commands a copy pipes its text to, for this session."""
    if env.get(ENV, '').strip():
        return [shlex.split(env[ENV])]
    found: list[list[str]] = []
    if env.get('WAYLAND_DISPLAY') and which('wl-copy'):
        found.append(['wl-copy'])
    elif env.get('DISPLAY') and which('xclip'):
        found.append(['xclip', '-selection', 'clipboard'])
    elif env.get('DISPLAY') and which('xsel'):
        found.append(['xsel', '--clipboard', '--input'])
    if env.get('TMUX') and which('tmux'):
        found.append(['tmux', 'load-buffer', '-'])
    return found


def copy(text: str, tools: Sequence[Sequence[str]], timeout: float = 5.0) -> list[str]:
    """Pipe *text* to each tool (blocking: run it off the UI thread). Returns the
    tools that failed. The tools' output is discarded: wl-copy and xclip stay
    behind to serve the selection, and would hold a captured pipe open."""
    failed = []
    for tool in tools:
        try:
            run = subprocess.run(
                list(tool),
                input=text.encode(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            failed.append(tool[0])
            continue
        if run.returncode != 0:
            failed.append(tool[0])
    return failed

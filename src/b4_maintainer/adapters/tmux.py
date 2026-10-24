# SPDX-License-Identifier: GPL-2.0-or-later
"""The maintainer's own editor in a tmux pane beside the app.

The editor is the one b4 would run (b4.get_editor_cmd). Panes are watched by
polling, never waited on, so a killed pane is noticed like an exited editor.
All calls block on tmux briefly; callers run them off the UI thread.
"""

import os
import pathlib
import shlex
import subprocess
from typing import Callable, Optional, Sequence


class EditorError(RuntimeError):
    pass


def b4_editor(checkout: pathlib.Path) -> list[str]:
    import b4

    return b4.get_editor_cmd(str(checkout))


class TmuxEditor:
    def __init__(
        self,
        width: int = 100,
        socket: Optional[str] = None,
        target: Optional[str] = None,
        command: Callable[[pathlib.Path], list[str]] = b4_editor,
    ) -> None:
        self.width = width
        self._socket = socket
        # The app's own pane, which the editor splits; $TMUX_PANE inside tmux.
        self._target = target or os.environ.get('TMUX_PANE', '')
        self._command = command

    def _tmux(self, *args: str) -> subprocess.CompletedProcess[str]:
        base = ['tmux'] + (['-L', self._socket] if self._socket else [])
        try:
            return subprocess.run(
                [*base, *args],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as ex:
            raise EditorError(f'tmux: {ex}') from ex

    def _query(self, pane: str, fmt: str) -> Optional[str]:
        try:
            result = self._tmux('display-message', '-p', '-t', pane, fmt)
        except EditorError:
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    def open(self, path: pathlib.Path, checkout: pathlib.Path) -> str:
        """Split the app's pane and run the editor on *path* to its right; return the pane id."""
        command = shlex.join([*self._command(checkout), str(path)])
        args: Sequence[str] = ['split-window', '-h', '-l', str(self.width), '-c', str(checkout)]
        if self._target:
            args = [*args, '-t', self._target]
        result = self._tmux(*args, '-P', '-F', '#{pane_id}', command)
        pane = result.stdout.strip()
        if result.returncode != 0 or not pane:
            raise EditorError(f'tmux split-window failed: {result.stderr.strip() or "no pane"}')
        return pane

    def alive(self, pane: str) -> bool:
        """Whether the pane still runs its editor (a remain-on-exit pane counts as gone)."""
        return self._query(pane, '#{pane_dead}') == '0'

    def runs(self, pane: str, path: pathlib.Path) -> bool:
        """Whether a live pane was started on this file: a pane worth adopting."""
        start = self._query(pane, '#{pane_dead} #{pane_start_command}')
        return bool(start) and start.startswith('0 ') and shlex.quote(str(path)) in start

    def focus(self, pane: str) -> None:
        result = self._tmux('select-pane', '-t', pane)
        if result.returncode != 0:
            raise EditorError(f'tmux select-pane failed: {result.stderr.strip()}')

    def close(self, pane: str) -> None:
        """Kill the pane; a pane already gone is fine."""
        try:
            self._tmux('kill-pane', '-t', pane)
        except EditorError:
            pass

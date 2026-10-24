# SPDX-License-Identifier: GPL-2.0-or-later
"""Phase 1 sessions: pi in a tmux window per session, outliving the app.

Each window carries the window option `@b4m_session=<id>`, so liveness and reattaching
after a restart need only `tmux list-windows`. A launch saves its command line to
`launch.json`; resuming runs it again without the first message (pi reopens the session
by id). All calls block briefly on tmux; callers run them off the UI thread.
"""

import json
import os
import shutil
import subprocess
import time
from typing import Mapping, Optional, Sequence

from .hub import SessionError
from .pi import LaunchSpec, Site, pi_command
from .records import SessionRecord, session_label
from .store import SessionStore

OPTION = '@b4m_session'
STOP_WAIT = 5.0  # Seconds for pi to quit before its window is killed.


class TmuxRunner:
    def __init__(
        self,
        store: SessionStore,
        site: Site,
        socket: Optional[str] = None,
        environ: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.store = store
        self.site = site
        self._socket = socket  # A private server (tests); else the app's own tmux.
        self._environ = os.environ if environ is None else environ

    def _tmux(self, *args: str) -> subprocess.CompletedProcess[str]:
        base = ['tmux'] + (['-L', self._socket] if self._socket else [])
        try:
            return subprocess.run(
                [*base, *args],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as ex:
            raise SessionError(f'tmux: {ex}') from ex

    def blocker(self) -> Optional[str]:
        if shutil.which('tmux') is None:
            return 'tmux is not installed'
        if self._socket is None and not self._environ.get('TMUX'):
            return 'phase 1 sessions run in tmux windows: start b4-maintainer inside tmux'
        if shutil.which(self.site.pi) is None:
            return f'pi is not installed ({self.site.pi})'
        return None

    def _windows(self) -> dict[str, str]:
        """Session id -> window id, for every session window on the server."""
        try:
            result = self._tmux('list-windows', '-a', '-F', f'#{{window_id}} #{{{OPTION}}}')
        except SessionError:
            return {}
        found = {}
        for line in result.stdout.splitlines():
            window, _, sid = line.partition(' ')
            if sid:
                found[sid] = window
        return found

    def _open(
        self, record: SessionRecord, argv: Sequence[str], cwd: str, env: Mapping[str, str]
    ) -> None:
        name = f'b4m:{session_label(record)}'[:40]
        args = ['new-window', '-d', '-n', name, '-c', cwd, '-P', '-F', '#{window_id}']
        for key, value in env.items():
            args += ['-e', f'{key}={value}']
        result = self._tmux(*args, '--', *argv)
        window = result.stdout.strip()
        if result.returncode != 0 or not window:
            raise SessionError(f'tmux new-window failed: {result.stderr.strip() or "no window"}')
        self._tmux('set-option', '-w', '-t', window, OPTION, record.id)

    def _app_window(self) -> str:
        """The app's own tmux window, for pi's /back; empty when unknown."""
        pane = self._environ.get('TMUX_PANE', '')
        if not pane:
            return ''
        try:
            result = self._tmux('display-message', '-p', '-t', pane, '#{window_id}')
        except SessionError:
            return ''
        return result.stdout.strip() if result.returncode == 0 else ''

    def launch(self, record: SessionRecord, spec: LaunchSpec) -> None:
        cmd = pi_command(self.store, record, spec, self.site)
        directory = self.store.directory(record.id)
        resume = pi_command(self.store, record, spec, self.site, first=False)
        env = {**cmd.env, 'B4M_APP_WINDOW': self._app_window()}
        launch = {'argv': cmd.argv, 'resume': resume.argv, 'cwd': cmd.cwd, 'env': env}
        (directory / 'launch.json').write_text(json.dumps(launch, indent=1) + '\n')
        self.store.save(record.replace(command=tuple(cmd.argv)))
        self._open(record, cmd.argv, cmd.cwd, env)

    def alive(self, records: Sequence[SessionRecord]) -> set[str]:
        windows = self._windows()
        return {r.id for r in records if r.id in windows}

    def switch(self, record: SessionRecord) -> None:
        window = self._windows().get(record.id)
        if window is None:
            raise SessionError(f'Session {record.id} has no window')
        self._tmux('select-window', '-t', window)
        self._tmux('switch-client', '-t', window)  # When it is in another tmux session.

    def stop(self, record: SessionRecord) -> None:
        """Ask pi to quit (it keeps the session); kill the window if it lingers."""
        window = self._windows().get(record.id)
        if window is None:
            return
        self._tmux('send-keys', '-t', window, 'Escape')
        time.sleep(0.2)
        self._tmux('send-keys', '-t', window, '-l', '/quit')
        self._tmux('send-keys', '-t', window, 'Enter')
        end = time.monotonic() + STOP_WAIT
        while time.monotonic() < end:
            if record.id not in self._windows():
                return
            time.sleep(0.1)
        self._tmux('kill-window', '-t', window)

    def resume(self, record: SessionRecord) -> None:
        if record.id in self._windows():
            return
        try:
            launch = json.loads((self.store.directory(record.id) / 'launch.json').read_text())
        except (OSError, ValueError) as ex:
            raise SessionError(f'Session {record.id} cannot be resumed: {ex}') from ex
        self._open(record, launch['resume'], launch['cwd'], launch['env'])

    def end(self, record: SessionRecord) -> None:
        self.stop(record)

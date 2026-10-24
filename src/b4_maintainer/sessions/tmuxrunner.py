# SPDX-License-Identifier: GPL-2.0-or-later
"""Phase 1 sessions: pi in a tmux window per session, outliving the app.

Each session's pane carries the pane option `@b4m_session=<id>`, so liveness and
reattaching after a restart need only `tmux list-panes`, wherever the pane is. A launch saves its command line to
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
APP_OPTION = '@b4m_app_pane'  # tmux session option: the app's pane.
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

    def _panes(self) -> dict[str, tuple[str, str]]:
        """Session id -> (pane id, window id), for every session pane on the server.

        Sessions are tagged on their pane: a pane keeps its options when it moves beside
        the app (join-pane) and back (break-pane), while its window does not survive.
        """
        try:
            result = self._tmux(
                'list-panes', '-a', '-F', f'#{{pane_id}} #{{window_id}} #{{{OPTION}}}'
            )
        except SessionError:
            return {}
        found = {}
        for line in result.stdout.splitlines():
            pane, window, sid = (line.split(' ', 2) + ['', ''])[:3]
            if sid:
                found[sid] = (pane, window)
        return found

    def _open(
        self, record: SessionRecord, argv: Sequence[str], cwd: str, env: Mapping[str, str]
    ) -> None:
        name = f'b4m:{session_label(record)}'[:40]
        args = ['new-window', '-d', '-n', name, '-c', cwd, '-P', '-F', '#{pane_id}']
        for key, value in env.items():
            args += ['-e', f'{key}={value}']
        result = self._tmux(*args, '--', *argv)
        pane = result.stdout.strip()
        if result.returncode != 0 or not pane:
            raise SessionError(f'tmux new-window failed: {result.stderr.strip() or "no pane"}')
        self._tmux('set-option', '-p', '-t', pane, OPTION, record.id)

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
        panes = self._panes()
        return {r.id for r in records if r.id in panes}

    def switch(self, record: SessionRecord) -> None:
        found = self._panes().get(record.id)
        if found is None:
            raise SessionError(f'Session {record.id} has no window')
        pane, window = found
        self._tmux('select-window', '-t', window)
        self._tmux('switch-client', '-t', window)  # When it is in another tmux session.
        self._tmux('select-pane', '-t', pane)

    def stop(self, record: SessionRecord) -> None:
        """Ask pi to quit (it keeps the session); kill its pane, never a window, if it
        lingers: a joined session's window is the app's."""
        found = self._panes().get(record.id)
        if found is None:
            return
        pane = found[0]
        self._tmux('send-keys', '-t', pane, 'Escape')
        time.sleep(0.2)
        self._tmux('send-keys', '-t', pane, '-l', '/quit')
        self._tmux('send-keys', '-t', pane, 'Enter')
        end = time.monotonic() + STOP_WAIT
        while time.monotonic() < end:
            if record.id not in self._panes():
                return
            time.sleep(0.1)
        self._tmux('kill-pane', '-t', pane)

    def resume(self, record: SessionRecord) -> None:
        if record.id in self._panes():
            return
        try:
            launch = json.loads((self.store.directory(record.id) / 'launch.json').read_text())
        except (OSError, ValueError) as ex:
            raise SessionError(f'Session {record.id} cannot be resumed: {ex}') from ex
        self._open(record, launch['resume'], launch['cwd'], launch['env'])

    def end(self, record: SessionRecord) -> None:
        self.stop(record)

    # The session pane beside the app: one at a time, joined without taking focus.

    def _window_of(self, pane: str) -> str:
        result = self._tmux('display-message', '-p', '-t', pane, '#{window_id}')
        return result.stdout.strip() if result.returncode == 0 else ''

    def joined(self, app_pane: str) -> Optional[str]:
        """The session whose pane is beside the app, if any."""
        window = self._window_of(app_pane)
        return next((sid for sid, (_p, w) in self._panes().items() if w == window), None)

    def join(self, record: SessionRecord, app_pane: str, width: int = 90, above: str = '') -> None:
        """Move the session's pane to the right of the app (replacing another session), or
        above *above*, a pane already beside the app (an open drafts editor)."""
        found = self._panes().get(record.id)
        if found is None:
            raise SessionError(f'Session {record.id} has no pane')
        if found[1] == self._window_of(app_pane):
            return
        if self.joined(app_pane) is not None:
            self.release(app_pane)
        args = ('join-pane', '-d', '-h', '-l', str(width), '-s', found[0], '-t', app_pane)
        if above:
            args = ('join-pane', '-d', '-v', '-b', '-s', found[0], '-t', above)
        result = self._tmux(*args)
        if result.returncode != 0:
            raise SessionError(f'tmux join-pane failed: {result.stderr.strip()}')

    def release(self, app_pane: str) -> None:
        """Send the session beside the app back to a window of its own (it keeps running)."""
        sid = self.joined(app_pane)
        if sid is None:
            return
        pane = self._panes()[sid][0]
        try:
            name = f'b4m:{session_label(self.store.record(sid))}'[:40]
        except (OSError, ValueError):
            name = f'b4m:{sid}'
        self._tmux('break-pane', '-d', '-s', pane, '-n', name)

    def focus(self, record: SessionRecord, zoom: bool = False) -> None:
        found = self._panes().get(record.id)
        if found is None:
            raise SessionError(f'Session {record.id} has no pane')
        self._tmux('select-pane', '-t', found[0])
        zoomed = self._tmux('display-message', '-p', '-t', found[0], '#{window_zoomed_flag}')
        if zoom and zoomed.stdout.strip() != '1':
            self._tmux('resize-pane', '-Z', '-t', found[0])

    def pane_of(self, record: SessionRecord) -> Optional[str]:
        found = self._panes().get(record.id)
        return found[0] if found else None

    def announce(self, app_pane: str) -> None:
        """Tell sessions where the app is (pi's F12 and /back read it when used, so it
        survives an app restart)."""
        self._tmux('set-option', '-t', app_pane, APP_OPTION, app_pane)

    def focus_app(self, app_pane: str) -> None:
        """Focus the app's pane (unzooming a zoomed session)."""
        self._tmux('select-pane', '-t', app_pane)

# SPDX-License-Identifier: GPL-2.0-or-later
"""Settings (F6): which key signs mail for every project, and setting one up there (an
existing PGP key from gpg, a new patatt ed25519 key) or stopping signing; each after a
confirmation, in the global git configuration (a checkout's own key still overrides).
"""

import datetime
import pathlib
from typing import Any, Callable, Optional, Sequence

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, OptionList, Static
from textual.widgets.option_list import Option

from .adapters import signing
from .branchscreen import Ask, Pick
from .widgets import TabBar

ACTIONS = (
    ('pgp', 'Sign with an existing PGP key (from gpg) for every project'),
    ('patatt', 'Make a new patatt ed25519 key and sign with it for every project'),
    ('stop', 'Stop signing for every project (messages go unsigned)'),
)


class SettingsScreen(Screen[None]):
    DEFAULT_CSS = """
    SettingsScreen #settings-status {
        height: auto; padding: 0 1; border-top: solid $panel-lighten-2;
        border-title-color: $text-muted;
    }
    SettingsScreen #settings-actions {
        height: auto; border: none; border-top: solid $panel-lighten-2;
        border-title-color: $text-muted;
    }
    SettingsScreen #settings-note { height: auto; padding: 0 1; }
    """
    BINDINGS = [Binding('escape', 'back', 'back')]

    def __init__(self, checkouts: Callable[[], Sequence[tuple[str, pathlib.Path]]]) -> None:
        super().__init__()
        self._checkouts = checkouts  # (project, checkout) of each project, for their own keys.
        self._status_text = ''

    def compose(self) -> ComposeResult:
        yield TabBar('settings')
        yield Static('', id='settings-status', markup=False)
        yield OptionList(*(Option(label, id=key) for key, label in ACTIONS), id='settings-actions')
        yield Static('', id='settings-note', markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one('#settings-status', Static).border_title = 'Signing'
        self.query_one('#settings-actions', OptionList).border_title = 'Set up'
        self.query_one('#settings-actions', OptionList).focus()
        self._reload()

    def on_screen_resume(self) -> None:
        self._reload()

    def _reload(self, note: str = '') -> None:
        def work() -> None:
            lines = signing.signing_status(self._checkouts())
            self.app.call_from_thread(self._show, lines, note)

        self.run_worker(work, thread=True, group='settings', exclusive=True)

    def _show(self, lines: list[str], note: str) -> None:
        self._status_text = '\n'.join([*lines, *([note] if note else [])])
        self.query_one('#settings-status', Static).update('\n'.join(lines))
        self.query_one('#settings-note', Static).update(note)

    def status_text(self) -> str:
        return self._status_text

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        event.stop()  # The app's own lists handle theirs.

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option.id:
            self.choose(event.option.id)

    def choose(self, action: str) -> None:
        if action == 'pgp':
            self._pgp()
        elif action == 'patatt':
            self._patatt()
        elif action == 'stop':
            self._confirm(
                'Stop signing for every project? patatt.signingkey goes from your global git '
                "configuration; messages go unsigned (a checkout's own key stays). [y|N]",
                signing.stop_signing,
                'Signing stopped',
            )

    def _pgp(self) -> None:
        def work() -> None:
            keys = signing.gpg_secret_keys()
            self.app.call_from_thread(offer, keys)

        def offer(keys: list[tuple[str, str]]) -> None:
            if not keys:
                self._reload('gpg has no secret keys: make one (gpg --full-generate-key) first')
                return
            choices = [(fpr, f'{uid}  {fpr[-16:]}') for fpr, uid in keys]
            self.app.push_screen(Pick('Sign with which PGP key?', choices), picked)

        def picked(fpr: Optional[str]) -> None:
            if fpr:
                self._confirm(
                    f'Sign mail with PGP key {fpr} for every project? Sets patatt.signingkey '
                    'in your global git configuration. [y|N]',
                    lambda: signing.use_pgp_key(fpr),
                    f'Signing with openpgp:{fpr}',
                )

        self.run_worker(work, thread=True, group='settings-act')

    def _patatt(self) -> None:
        today = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d')

        def named(name: Optional[str]) -> None:
            if not name:
                return
            self._confirm(
                f'Make a new ed25519 key {name} with patatt and sign with it for every project? '
                'Sets patatt.signingkey in your global git configuration; publish its public '
                "key (in patatt's data directory) so others can verify. [y|N]",
                lambda: signing.new_patatt_key(name),
                f'Signing with ed25519:{name}',
            )

        self.app.push_screen(Ask('Name the new key', today), named)

    def _confirm(self, prompt: str, do: Callable[[], Any], done: str) -> None:
        from .app import Confirm

        def answered(yes: Optional[bool]) -> None:
            if not yes:
                return

            def work() -> None:
                try:
                    do()
                    note = done
                except Exception as exc:
                    note = f'Not changed: {exc}'
                self.app.call_from_thread(self._reload, note)

            self.run_worker(work, thread=True, group='settings-act')

        self.app.push_screen(Confirm(prompt), answered)

    def action_back(self) -> None:
        tab = getattr(self.app, 'action_tab', None)
        if tab is not None:
            tab('lkml')
        else:
            self.app.pop_screen()

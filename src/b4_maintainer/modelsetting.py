# SPDX-License-Identifier: GPL-2.0-or-later
"""The default model for agent sessions, and its thinking level: `[sessions]
default_model` and `default_thinking` in this machine's var/workspace.toml (pi's models
and credentials are per machine). Unset, a launch uses pi's own default model at the
model's own thinking level. Blocking: call from a worker."""

import pathlib
from typing import TYPE_CHECKING

from .projectsetup import read_config, set_local_setting
from .sessions.models import THINKING

if TYPE_CHECKING:
    from .sessions.hub import SessionHub

UNSET = '-'  # The choice that removes the setting.


class DefaultModel:
    def __init__(self, root: pathlib.Path, hub: 'SessionHub') -> None:
        self.root = pathlib.Path(root)
        self.hub = hub

    def _setting(self, key: str) -> str:
        return str(read_config(self.root).get('sessions', {}).get(key, '')).strip()

    def current(self) -> str:
        """The configured default model ('' for pi's own)."""
        return self._setting('default_model')

    def thinking(self) -> str:
        """The configured default thinking level ('' for the model's own)."""
        return self._setting('default_thinking')

    @staticmethod
    def thinking_choices() -> list[tuple[str, str]]:
        """(key, label): the model's own level (UNSET), then pi's levels."""
        return [(UNSET, "the model's own level (none set)"), *((t, t) for t in THINKING)]

    def set_thinking(self, level: str) -> str:
        """*level* (UNSET or '' for the model's own) as the default thinking level."""
        value = '' if level == UNSET else level
        path = set_local_setting(self.root, 'sessions', 'default_thinking', value)
        where = f'var/{path.name}'
        if not value:
            still = self.thinking()
            if still:
                return f'Removed from {where}; the shared configuration still sets {still}'
            return f"Sessions think at the model's own level (removed from {where})"
        return f'Sessions think at {value} unless another level is chosen ({where})'

    def choices(self) -> list[tuple[str, str]]:
        """(key, label): pi's own default (UNSET), the presets pi can run, then pi's catalog."""
        presets = [m for m in read_config(self.root).get('models', []) if isinstance(m, dict)]
        found = self.hub.models(presets)
        listed = [(UNSET, "pi's default model (no default set)")]
        listed += [(m.id, m.label) for m in found if m.kind == 'preset' and m.available]
        listed += [(m.id, m.id) for m in found if m.kind == 'catalog']
        return listed

    def set(self, model: str) -> str:
        """*model* (UNSET or '' for pi's own) as the default; what changed."""
        value = '' if model == UNSET else model
        path = set_local_setting(self.root, 'sessions', 'default_model', value)
        where = f'var/{path.name}'
        if not value:
            still = self.current()
            if still:  # The shared configuration names one.
                return f'Removed from {where}; the shared configuration still sets {still}'
            return f"Sessions use pi's default model (removed from {where})"
        return f'Sessions use {value} unless another model is chosen ({where})'

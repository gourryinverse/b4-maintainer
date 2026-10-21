# SPDX-License-Identifier: GPL-2.0-or-later
"""Layout widths for the browse screen; defaults await usability testing."""

from dataclasses import dataclass
from typing import Any, Literal, Mapping

Mode = Literal['wide', 'medium', 'narrow']


@dataclass(frozen=True)
class Breakpoints:
    three_pane: int = 160
    single_pane: int = 100
    preview_width: int = 100

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> 'Breakpoints':
        default = cls()
        return cls(
            int(config.get('three_pane', default.three_pane)),
            int(config.get('single_pane', default.single_pane)),
            int(config.get('preview_width', default.preview_width)),
        )


def layout_mode(width: int, breakpoints: Breakpoints = Breakpoints()) -> Mode:
    if width >= breakpoints.three_pane:
        return 'wide'
    if width >= breakpoints.single_pane:
        return 'medium'
    return 'narrow'

# SPDX-License-Identifier: GPL-2.0-or-later
"""Load a fixture workspace: workspace.json, mail/*.mbx parsed by b4, state.json.

state.json stands in for native b4 tracking and client state until the
pre-apply ownership decision (docs/design/open.md) is resolved.
"""

import json
import pathlib
from typing import Any, Mapping

from ..model import Authority, Coverage, Progress, SeriesState
from ..workspace import Snapshot, Workspace, build_items
from .b4mbox import parse_mbox


def state_from_config(entry: Mapping[str, Any]) -> SeriesState:
    return SeriesState(
        unread=int(entry.get('unread', 0)),
        stable_pending=bool(entry.get('stable_pending', False)),
        progress=Progress(entry.get('progress', 'none')),
        draft=bool(entry.get('draft', False)),
        to_review=bool(entry.get('to_review', False)),
        tree=tuple(
            (label, Authority[authority.upper()], Coverage(coverage))
            for label, authority, coverage in entry.get('tree', [])
        ),
    )


class FixtureSource:
    def __init__(self, root: 'pathlib.Path | str') -> None:
        self.root = pathlib.Path(root)

    def load(self) -> Snapshot:
        config = json.loads((self.root / 'workspace.json').read_text())
        ws = Workspace.from_config(config)
        state_path = self.root / 'state.json'
        raw_states = json.loads(state_path.read_text()) if state_path.exists() else {}
        states = {k: state_from_config(v) for k, v in raw_states.items()}
        responsible = {k for k, v in raw_states.items() if v.get('responsible')}
        series = []
        for path in sorted((self.root / 'mail').glob('*.mbx')):
            series.extend(parse_mbox(path))
        return Snapshot(ws, build_items(series, states, ws, responsible=responsible))

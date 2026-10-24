# SPDX-License-Identifier: GPL-2.0-or-later
"""Which session belongs in the column beside the app.

Reading a series: its most urgent live session; if it has none, the workspace session
the maintainer put beside the list. In the series list: that workspace session. Stopped
or gone sessions never join on their own: resuming is the maintainer's action.
"""

from typing import Optional, Sequence

from .store import SessionView

LIVE = frozenset({'approval', 'waiting', 'working', 'starting'})


def pane_for(
    views: Sequence[SessionView], reading: Optional[str], workspace: Optional[str]
) -> Optional[str]:
    """The session id for the column; *views* come most urgent first."""
    live = [v for v in views if v.state in LIVE]
    if reading is not None:
        own = next((v for v in live if v.record.series == reading), None)
        if own is not None:
            return own.record.id
    if workspace is not None and any(v.record.id == workspace for v in live):
        return workspace
    return None

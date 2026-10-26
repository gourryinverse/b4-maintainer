# SPDX-License-Identifier: GPL-2.0-or-later
"""R's picker: what this series' sessions produced, to add at the end of a reply.

Space ticks entries, Enter (or ctrl+s) opens the draft with them, Escape cancels; Enter
with nothing ticked opens a plain reply. The highlighted entry is described below.
"""

from typing import Optional, Sequence

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import SelectionList, Static
from textual.widgets.selection_list import Selection

from .attachments import Attachment

KINDS = {
    'patch': 'Inline patch after the reply (git am --scissors)',
    'file': 'Text',
    'text': 'Text',
}


class AttachDialog(ModalScreen[Optional[list[Attachment]]]):
    DEFAULT_CSS = """
    AttachDialog { align: center middle; }
    AttachDialog > Vertical {
        width: 120; max-width: 95%; height: auto; max-height: 85%;
        border: solid $accent; background: $panel;
    }
    AttachDialog SelectionList { height: auto; max-height: 30; border: none; }
    AttachDialog #attach-help, AttachDialog #attach-about { padding: 0 1; }
    """
    BINDINGS = [
        Binding('escape', 'cancel', 'cancel'),
        Binding('enter', 'confirm', 'write the reply', priority=True),
        Binding('ctrl+s', 'confirm', 'write the reply', show=False),
    ]

    def __init__(self, offered: Sequence[Attachment]) -> None:
        super().__init__()
        self.offered = list(offered)

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(
                'Add to the reply · space ticks · enter writes it · escape cancels',
                id='attach-help',
                markup=False,
            )
            yield SelectionList[int](
                *(Selection(a.label, n, False) for n, a in enumerate(self.offered))
            )
            yield Static('', id='attach-about', markup=False)

    def on_mount(self) -> None:
        self.query_one(SelectionList).focus()
        self._describe(0)

    def on_selection_list_selection_highlighted(
        self, event: SelectionList.SelectionHighlighted
    ) -> None:
        event.stop()
        self._describe(event.selection_index)

    def on_selection_list_selected_changed(self, event: SelectionList.SelectedChanged) -> None:
        event.stop()

    def _describe(self, index: int) -> None:
        if not 0 <= index < len(self.offered):
            return
        a = self.offered[index]
        lines = [KINDS.get(a.kind, a.kind), a.title]
        if a.text:
            lines.append(a.text.strip().splitlines()[0][:200])
        self.query_one('#attach-about', Static).update('\n'.join(lines))

    def ticked(self) -> list[Attachment]:
        chosen = set(self.query_one(SelectionList).selected)
        return [a for n, a in enumerate(self.offered) if n in chosen]

    def action_confirm(self) -> None:
        self.dismiss(self.ticked())

    def action_cancel(self) -> None:
        self.dismiss(None)

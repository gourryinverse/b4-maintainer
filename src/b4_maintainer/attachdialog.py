# SPDX-License-Identifier: GPL-2.0-or-later
"""R's picker: what sessions produced, to add at the end of a reply: the series' own
first, then its workbenches', then the rest.

Space ticks entries, / filters them by what you type (↓ returns to the list; ticks
survive the filter), Enter
(or ctrl+s) opens the draft with them, Escape cancels; Enter with nothing ticked opens a
plain reply. The highlighted entry is described below.
"""

from typing import Optional, Sequence

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, SelectionList, Static
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
    AttachDialog #attach-filter { border: none; height: 1; padding: 0 1; }
    """
    BINDINGS = [
        Binding('escape', 'cancel', 'cancel'),
        Binding('enter', 'confirm', 'write the reply', priority=True),
        Binding('ctrl+s', 'confirm', 'write the reply', show=False),
        Binding('slash', 'filter', 'filter', key_display='/'),
    ]

    def __init__(self, offered: Sequence[Attachment]) -> None:
        super().__init__()
        self.offered = list(offered)
        self._ticked: list[int] = []  # Ticked entries, in the order ticked.
        self._visible: list[int] = list(range(len(self.offered)))

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(
                'Add to the reply · space ticks · / filters (↓ back) · enter writes it · escape cancels',
                id='attach-help',
                markup=False,
            )
            yield Input(placeholder='/ filters by what you type', id='attach-filter')
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
        shown = self._visible
        self._describe(shown[event.selection_index] if event.selection_index < len(shown) else -1)

    def on_selection_list_selected_changed(self, event: SelectionList.SelectedChanged) -> None:
        event.stop()
        chosen = set(self.query_one(SelectionList).selected)
        kept = [n for n in self._ticked if n not in self._visible or n in chosen]
        self._ticked = kept + [n for n in self._visible if n in chosen and n not in kept]

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        words = event.value.lower().split()
        self._visible = [
            n for n, a in enumerate(self.offered) if all(w in a.label.lower() for w in words)
        ]
        lists = self.query_one(SelectionList)
        with self.prevent(SelectionList.SelectedChanged):
            lists.clear_options()
            lists.add_options(
                [Selection(self.offered[n].label, n, n in self._ticked) for n in self._visible]
            )
            if self._visible:
                lists.highlighted = 0
        self._describe(self._visible[0] if self._visible else -1)

    def action_filter(self) -> None:
        self.query_one('#attach-filter', Input).focus()

    def on_key(self, event: events.Key) -> None:
        # Down from the filter returns to the list (tab is the app's).
        if event.key == 'down' and isinstance(self.focused, Input):
            event.stop()
            self.query_one(SelectionList).focus()

    def visible_labels(self) -> list[str]:
        return [self.offered[n].label for n in self._visible]

    def _describe(self, index: int) -> None:
        if not 0 <= index < len(self.offered):
            return
        a = self.offered[index]
        lines = [KINDS.get(a.kind, a.kind), a.title]
        if a.text:
            lines.append(a.text.strip().splitlines()[0][:200])
        self.query_one('#attach-about', Static).update('\n'.join(lines))

    def ticked(self) -> list[Attachment]:
        """The ticked entries, in the order offered."""
        chosen = set(self._ticked)
        return [a for n, a in enumerate(self.offered) if n in chosen]

    def action_confirm(self) -> None:
        self.dismiss(self.ticked())

    def action_cancel(self) -> None:
        self.dismiss(None)

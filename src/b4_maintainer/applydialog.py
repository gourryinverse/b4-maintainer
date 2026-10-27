# SPDX-License-Identifier: GPL-2.0-or-later
"""`a`'s picker: which patches of a posted series to apply to a base or a prep branch.

Every patch starts ticked; Space unticks, Enter (or ctrl+s) applies the ticked ones,
Escape cancels. b4 adds the trailers given on the list and a Link to each.
"""

from typing import TYPE_CHECKING, Optional, Sequence

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import SelectionList, Static
from textual.widgets.selection_list import Selection

if TYPE_CHECKING:
    from .adapters.apply import SeriesPatch  # b4: only when applying.


class ApplyDialog(ModalScreen[Optional[list[int]]]):
    DEFAULT_CSS = """
    ApplyDialog { align: center middle; }
    ApplyDialog > Vertical {
        width: 120; max-width: 95%; height: auto; max-height: 85%;
        border: solid $accent; background: $panel;
    }
    ApplyDialog SelectionList { height: auto; max-height: 30; border: none; }
    ApplyDialog #apply-help { padding: 0 1; }
    """
    BINDINGS = [
        Binding('escape', 'cancel', 'cancel'),
        Binding('enter', 'confirm', 'apply', priority=True),
        Binding('ctrl+s', 'confirm', 'apply', show=False),
    ]

    def __init__(self, subject: str, branch: str, patches: Sequence['SeriesPatch']) -> None:
        super().__init__()
        self.subject = subject
        self.branch = branch
        self.patches = list(patches)

    def heading(self) -> str:
        return (
            f'Apply {self.subject} to {self.branch}: b4 adds the trailers given on the list '
            'and a Link to each; authors are kept\n'
            'space unticks · enter applies · escape cancels'
        )

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.heading(), id='apply-help', markup=False)
            yield SelectionList[int](
                *(
                    Selection(f'{p.number:>2}  {p.subject}  ({p.author})', p.number, True)
                    for p in self.patches
                )
            )

    def on_mount(self) -> None:
        self.query_one(SelectionList).focus()

    def on_selection_list_selection_highlighted(
        self, event: SelectionList.SelectionHighlighted
    ) -> None:
        event.stop()

    def on_selection_list_selected_changed(self, event: SelectionList.SelectedChanged) -> None:
        event.stop()

    def ticked(self) -> list[int]:
        return sorted(self.query_one(SelectionList).selected)

    def action_confirm(self) -> None:
        self.dismiss(self.ticked())

    def action_cancel(self) -> None:
        self.dismiss(None)

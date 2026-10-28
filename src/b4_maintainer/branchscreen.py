# SPDX-License-Identifier: GPL-2.0-or-later
"""The Branches screen: each project's maintainer bases, worktrees, session trees, prep
branches, review branches, and other local work (left), the selected one's commits and
artifacts (middle), and the selected commit or artifact (right).

Everything is read and acted on in workers; the screen only shows results. `r` rebases a
maintainer base after a confirmation; when that stops at a conflict, `a` launches a
rebase session on a copy (after a confirmation), and `A` on that session's tree moves the
base to its result. Space marks commits, `p` plucks them, `n` makes a prep branch from
them; on a prep branch `e` `t` `c` `s` `o` `u` `d` edit, address, check, sign off,
preview, collect trailers, and compare, and `S` sends through b4 in the terminal. `x`
removes a prep branch (b4 prep --cleanup), a local branch, or a session tree, after saying
what goes. F5 reloads; F3 or Escape returns to the series list.
"""

import email
import email.policy
import pathlib
import re
from typing import TYPE_CHECKING, Any, Mapping, Optional, Protocol, Sequence

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Footer, Input, OptionList, Static
from textual.widgets.option_list import Option

from .adapters.bases import PluckResult, RebaseResult
from .adapters.prep import SignResult, run_b4
from .branches import (
    ApplyChoices,
    BranchesSnapshot,
    CutResult,
    DetailRow,
    DropChoices,
    FoldPlan,
    SendSummary,
    is_commit,
)
from .launchdialog import Derived
from .sessions.launch import Target
from .sessions.policy import ContextItem
from .sessions.records import SessionRecord
from .widgets import TabBar, TextPane
from .workspace import Workspace

if TYPE_CHECKING:
    from .adapters.apply import ApplyResult
    from .model import Series
    from .patchmail import ReplyTo


class Branches(Protocol):
    def snapshot(self, workspace: Workspace, trees: bool = True) -> BranchesSnapshot: ...

    def show(self, row: DetailRow) -> str: ...

    def rebase(self, workspace: Workspace, key: str, expected: str) -> RebaseResult: ...

    def rebase_session(
        self, workspace: Workspace, key: str, failed: RebaseResult
    ) -> SessionRecord: ...

    def accept(self, workspace: Workspace, sid: str) -> RebaseResult: ...

    def pluck(
        self,
        workspace: Workspace,
        item: str,
        rows: Sequence[DetailRow],
        target: str,
        expected: str,
        into_checkout: bool = False,
    ) -> PluckResult: ...

    def fork_points(self, workspace: Workspace, item: str) -> list[str]: ...

    def tree_targets(
        self, workspace: Workspace, item: str
    ) -> tuple[list[tuple[str, str]], dict[str, str]]: ...

    def fold_plan(
        self, workspace: Workspace, item: str, folded: str, series: Mapping[str, 'Series']
    ) -> FoldPlan: ...

    def fold_to_prep(
        self, workspace: Workspace, plan: FoldPlan, extras: Sequence[str], name: str
    ) -> CutResult: ...

    def branch_starts(self, workspace: Workspace, project_id: str) -> list[str]: ...

    def fixup_target(
        self, workspace: Workspace, item: str, branch: str
    ) -> tuple[str, Optional[int]]: ...

    def patch_message(
        self, workspace: Workspace, item: str, row: DetailRow, reply: Optional['ReplyTo']
    ) -> str: ...

    def new_branch(
        self, workspace: Workspace, project_id: str, name: str, start: str
    ) -> CutResult: ...

    def cut_prep(
        self, workspace: Workspace, item: str, rows: Sequence[DetailRow], name: str, fork: str
    ) -> CutResult: ...

    def prep_run(self, workspace: Workspace, item: str, action: str) -> tuple[int, str]: ...

    def prep_worktree(self, workspace: Workspace, item: str) -> Optional[pathlib.Path]: ...

    def sign_off(self, workspace: Workspace, item: str, expected: str) -> SignResult: ...

    def preview(self, workspace: Workspace, item: str) -> tuple[int, str]: ...

    def send_args(
        self, workspace: Workspace, item: str, sign: bool = True
    ) -> tuple[pathlib.Path, str, tuple[str, ...]]: ...

    def send_summary(self, workspace: Workspace, item: str) -> SendSummary: ...

    def reapable(self, data: BranchesSnapshot, item: str) -> bool: ...

    def derive_options(
        self, workspace: Workspace, item: str, row: Optional[DetailRow]
    ) -> list[tuple[str, str, str]]: ...

    def derive_context(self, item: str, row: Optional[DetailRow]) -> Optional[ContextItem]: ...

    def losses(self, workspace: Workspace, item: str) -> str: ...

    def reap(self, workspace: Workspace, item: str) -> tuple[bool, str]: ...

    def apply_choices(
        self, workspace: Workspace, project_id: str, source: str, revision: int
    ) -> ApplyChoices: ...

    def apply(
        self,
        workspace: Workspace,
        target: str,
        source: str,
        revision: int,
        numbers: Sequence[int],
        expected: str,
    ) -> 'ApplyResult': ...

    def drop_choices(
        self, workspace: Workspace, project_id: str, patches: Sequence[tuple[str, str]]
    ) -> DropChoices: ...

    def apply_session(
        self,
        workspace: Workspace,
        target: str,
        series: Target,
        numbers: Sequence[int],
        failed: 'ApplyResult',
    ) -> SessionRecord: ...

    def drop(
        self, workspace: Workspace, target: str, shas: Sequence[str], expected: str
    ) -> PluckResult: ...


class Ask(ModalScreen[Optional[str]]):
    """One line of text: Enter answers, Escape cancels."""

    DEFAULT_CSS = """
    Ask { align: center middle; }
    Ask > Vertical { width: 70; height: auto; border: solid $accent; background: $panel; }
    Ask Input { border: none; }
    """
    BINDINGS = [Binding('escape', 'cancel', 'cancel')]

    def __init__(self, title: str, value: str = '') -> None:
        super().__init__()
        self.title_text = title
        self.value = value

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.title_text, markup=False)
            yield Input(value=self.value)

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.dismiss(event.value.strip() or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class Pick(ModalScreen[Optional[str]]):
    """Choose one of several: Enter picks, Escape cancels."""

    DEFAULT_CSS = """
    Pick { align: center middle; }
    Pick > Vertical {
        width: auto; min-width: 48; max-width: 90%; height: auto;
        border: solid $accent; background: $panel;
    }
    Pick OptionList { width: auto; min-width: 44; height: auto; border: none; }
    """
    BINDINGS = [Binding('escape', 'cancel', 'cancel')]

    def __init__(self, title: str, choices: Sequence[tuple[str, str]]) -> None:
        super().__init__()
        self.title_text = title
        self.choices = list(choices)  # (key, label)

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.title_text, markup=False)
            yield OptionList(*(Option(label, id=key) for key, label in self.choices))

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        event.stop()  # The app's own lists handle theirs.

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(event.option.id)

    def action_cancel(self) -> None:
        self.dismiss(None)


# The sections of a project, in order: (item kind, heading).
SECTIONS = (
    (('base', 'baseref'), 'Bases'),
    (('tree',), 'Rebase and apply sessions'),
    (('prep',), 'Prep'),
    (('work',), 'Session work'),
    (('review',), 'Review'),
    (('branch',), 'Other'),
    (('worktree',), 'Checkouts'),
)


def _section(kind: str) -> str:
    """The section an item kind is listed in, by its first kind (its heading's name)."""
    return next((kinds[0] for kinds, _title in SECTIONS if kind in kinds), kind)


def _kinds(section: str) -> tuple[str, ...]:
    return next((kinds for kinds, _title in SECTIONS if kinds[0] == section), (section,))


class BranchesScreen(Screen[None]):
    DEFAULT_CSS = """
    BranchesScreen #branch-panes { height: 1fr; }
    BranchesScreen #branch-left { width: 3fr; }
    BranchesScreen #branch-items { height: 1fr; }
    BranchesScreen #branch-info { height: 7; padding: 0 1; }
    BranchesScreen #branch-rows { width: 3fr; }
    BranchesScreen #branch-show-pane { width: 4fr; }
    /* Each pane titled on its top edge, as on the Workbench. */
    BranchesScreen #branch-items, BranchesScreen #branch-rows, BranchesScreen #branch-show-pane,
    BranchesScreen #branch-info {
        border: none; border-top: solid $panel-lighten-2; border-title-color: $text-muted;
    }
    BranchesScreen #branch-rows, BranchesScreen #branch-show-pane {
        border-left: solid $panel-lighten-2;
    }
    BranchesScreen OptionList:focus, BranchesScreen TextPane:focus { border-title-color: $accent; }
    BranchesScreen #branch-status { height: 1; padding: 0 1; }
    """
    BINDINGS = [
        Binding('escape', 'back', 'back'),
        # The footer shows the keys of the selected kind; less common ones stay off it
        # (? lists them all).
        Binding('tab', 'cycle(1)', 'next pane', priority=True, show=False),
        Binding('shift+tab', 'cycle(-1)', 'previous pane', priority=True, show=False),
        Binding('left', 'fold', 'fold', show=False),
        Binding('right', 'unfold', 'unfold', show=False),
        Binding('r', 'rebase', 'rebase base'),
        Binding('a', 'agent_rebase', 'rebase with an agent'),
        Binding('A', 'accept', 'accept result'),
        Binding('space', 'mark', 'mark'),
        Binding('p', 'pluck', 'pluck'),
        Binding('b', 'new_branch', 'new branch'),
        Binding('m', 'mail_patch', 'mail patch'),
        Binding('n', 'new_prep', 'new prep branch'),
        Binding('t', 'prep_run("auto-to-cc")', 'to/cc', show=False),
        Binding('c', 'prep_run("check")', 'check', show=False),
        Binding('e', 'edit_cover', 'edit cover'),
        Binding('s', 'sign_off', 'sign off'),
        Binding('o', 'preview', 'outgoing'),
        Binding('S', 'send', 'send'),
        Binding('u', 'prep_run("trailers")', 'trailers', show=False),
        Binding('d', 'prep_run("compare")', 'compare', show=False),
        Binding('x', 'reap', 'remove'),
        Binding('D', 'derive', 'derive a session', show=False),
    ]

    def __init__(self, service: Branches, workspace: Workspace) -> None:
        super().__init__()
        self.service = service
        self.workspace = workspace
        self.data = BranchesSnapshot()
        self._rows: tuple[DetailRow, ...] = ()
        self._shown = ''
        self._note = ''  # The last action's outcome, kept across the reload it causes.
        self._status_text = ''
        self._failed: dict[str, RebaseResult] = {}  # Base key -> its last failed rebase.
        self._marks: set[int] = set()  # Marked commit rows of the selected item.
        self._pinned = ''  # Shown on the right after the next reload instead of the row.
        self.stale = False  # Set when the app switches back to this tab.
        # Folded headings: a project (its id) or a section (<project>:<kind>). Every
        # heading starts folded; those seen are in _known, so unfolding is kept.
        self._folded: set[str] = set()
        self._known: set[str] = set()
        self._select = ''  # The item (or h:<heading>) to select after the next reload.
        self._details = ''  # The details pane's text.

    def compose(self) -> ComposeResult:
        yield TabBar('branches')
        with Horizontal(id='branch-panes'):
            with Vertical(id='branch-left'):
                yield OptionList(id='branch-items')
                yield Static('', id='branch-info', markup=False)  # The selected one's details.
            yield OptionList(id='branch-rows')
            yield TextPane(id='branch-show-pane')
        yield Static('Loading…', id='branch-status', markup=False)
        yield Footer()

    def on_mount(self) -> None:
        items = self.query_one('#branch-items', OptionList)
        items.border_title = 'Branches'
        self.query_one('#branch-info', Static).border_title = 'Details'
        self.query_one('#branch-rows', OptionList).border_title = 'Commits'
        items.focus()
        self.action_reload()

    def on_screen_resume(self) -> None:
        # Switched back to (the app marks it stale), not just a prompt closing over it:
        # reread, keeping the selection.
        if self.stale:
            self.stale = False
            self.action_reload()

    # Loading

    def action_reload(self) -> None:
        def work() -> None:
            try:
                data = self.service.snapshot(self.workspace, trees=False)  # The Workbench's.
            except Exception as exc:
                self.app.call_from_thread(self._status, f'Cannot read branches: {exc}')
                return
            self.app.call_from_thread(self._loaded, data)

        self.run_worker(work, thread=True, group='branches-load', exclusive=True)

    def _loaded(self, data: BranchesSnapshot) -> None:
        current = self._select or self._current_id()
        self._select = ''
        self.data = data
        self._fill(current)
        empty = '' if data.items else 'No projects with a checkout.'
        self._status('; '.join(p for p in (self._note, *data.problems, empty) if p))
        self._note = ''

    def _current_id(self) -> str:
        option = self.query_one('#branch-items', OptionList).highlighted_option
        return option.id or '' if option is not None else ''

    def _fill(self, select: str) -> None:
        """The list: each project, its sections (folded ones closed), their branches;
        *select* (an item key, or h:<heading>) highlighted, else the heading folding it
        away, else the first row."""
        items = self.query_one('#branch-items', OptionList)
        headings = {i.project for i in self.data.items}
        headings |= {f'{i.project}:{_section(i.kind)}' for i in self.data.items}
        self._folded |= headings - self._known  # New headings start folded.
        self._known |= headings
        ids: list[str] = []
        options: list[Option] = []
        projects = list(dict.fromkeys(i.project for i in self.data.items))
        for project in projects:
            mine = [i for i in self.data.items if i.project == project]
            closed = project in self._folded
            options.append(Option(f'{"▸" if closed else "▾"} {project}', id=f'h:{project}'))
            ids.append(f'h:{project}')
            if closed:
                continue
            for kinds, title in SECTIONS:
                group = [i for i in mine if i.kind in kinds]
                if not group:
                    continue
                heading = f'{project}:{kinds[0]}'
                shut = heading in self._folded
                mark = '▸' if shut else '▾'
                options.append(Option(f'  {mark} {title} ({len(group)})', id=f'h:{heading}'))
                ids.append(f'h:{heading}')
                if shut:
                    continue
                for item in group:
                    star = ' *' if item.flagged else ''  # Its details say why.
                    options.append(Option(f'      {item.label}{star}', id=item.key))
                    ids.append(item.key)
        with self.prevent(OptionList.OptionHighlighted):  # Shown below, once.
            items.clear_options()
            items.add_options(options)
            target = select if select in ids else self._folding(select)
            if target not in ids:
                target = ids[0] if ids else ''
            items.highlighted = ids.index(target) if target in ids else None
        self._show_rows()

    def _folding(self, key: str) -> str:
        """The heading that folds *key* away (its section, else its project)."""
        item = next((i for i in self.data.items if i.key == key), None)
        if item is None:
            return ''
        if item.project in self._folded:
            return f'h:{item.project}'
        return f'h:{item.project}:{_section(item.kind)}'

    def _status(self, text: str) -> None:
        self._status_text = text
        self.query_one('#branch-status', Static).update(text)

    def show_notice(self, text: str) -> None:
        """The app's notice (e.g. F5's progress), kept across the reload that follows."""
        self._note = text
        self._status(text)

    def reread(self) -> None:
        self.action_reload()

    def status_text(self) -> str:
        return self._status_text

    # Panes

    def selected_item(self) -> Optional[str]:
        """The selected branch's key (None on a heading)."""
        current = self._current_id()
        return current if current and not current.startswith('h:') else None

    def details_text(self) -> str:
        return self._details

    def selected_heading(self) -> Optional[str]:
        """The selected heading: a project, or <project>:<kind>."""
        current = self._current_id()
        return current[2:] if current.startswith('h:') else None

    def select_item(self, key: str) -> None:
        """Select *key*, unfolding its project and section."""
        item = next((i for i in self.data.items if i.key == key), None)
        if item is not None:
            self._folded -= {item.project, f'{item.project}:{_section(item.kind)}'}
            self._fill(key)

    def select_heading(self, heading: str) -> None:
        """Select *heading* (a project, or <project>:<kind>), unfolding its project."""
        project = heading.split(':', 1)[0]
        if project in self._folded and project != heading:
            self._folded.discard(project)
            self._fill(f'h:{heading}')
        self._highlight(f'h:{heading}')

    def _highlight(self, option_id: str) -> None:
        items = self.query_one('#branch-items', OptionList)
        for n in range(items.option_count):
            if items.get_option_at_index(n).id == option_id:
                items.highlighted = n
                return

    def item_labels(self) -> list[str]:
        items = self.query_one('#branch-items', OptionList)
        return [str(items.get_option_at_index(n).prompt) for n in range(items.option_count)]

    def item_keys(self) -> list[str]:
        """The branches listed (open sections only)."""
        items = self.query_one('#branch-items', OptionList)
        found = (items.get_option_at_index(n).id or '' for n in range(items.option_count))
        return [i for i in found if not i.startswith('h:')]

    def _project(self) -> str:
        """The selected branch's or heading's project."""
        key = self.selected_item()
        if key is not None:
            return next((i.project for i in self.data.items if i.key == key), '')
        return (self.selected_heading() or '').split(':', 1)[0]

    def action_fold(self) -> None:
        """Left: fold the heading under the cursor, or a branch's section (moving onto
        it); on a folded section, its project."""
        if self.focused is not self.query_one('#branch-items'):
            return
        key, heading = self.selected_item(), self.selected_heading()
        if key is not None:
            item = next(i for i in self.data.items if i.key == key)
            heading = f'{item.project}:{_section(item.kind)}'
        elif heading is not None and heading in self._folded and ':' in heading:
            heading = heading.split(':', 1)[0]
        if heading is None:
            return
        self._folded.add(heading)
        self._fill(f'h:{heading}')

    def action_unfold(self) -> None:
        heading = self.selected_heading()
        if self.focused is not self.query_one('#branch-items') or heading is None:
            return
        self._folded.discard(heading)
        self._fill(f'h:{heading}')

    def _toggle(self) -> None:
        heading = self.selected_heading()
        if heading is None:
            return
        if heading in self._folded:
            self._folded.discard(heading)
        else:
            self._folded.add(heading)
        self._fill(f'h:{heading}')

    def selected_row(self) -> Optional[DetailRow]:
        rows = self.query_one('#branch-rows', OptionList)
        index = rows.highlighted
        if index is None or index >= len(self._rows):
            return None
        return self._rows[index]

    def _show_rows(self) -> None:
        key = self.selected_item()
        self._rows = tuple(self.data.details.get(key, ())) if key else ()
        item = next((i for i in self.data.items if i.key == key), None)
        rows_pane = self.query_one('#branch-rows', OptionList)
        rows_pane.border_title = item.label.split(' · ', 1)[0] if item else 'Commits'
        heading = self.selected_heading()
        if item is not None:
            details = '\n'.join(item.info or (item.label,))
        elif heading is not None:
            project, _, kind = heading.partition(':')
            group = [
                i
                for i in self.data.items
                if i.project == project and (not kind or i.kind in _kinds(kind))
            ]
            flagged = sum(i.flagged for i in group)
            details = f'{len(group)} listed' + (
                f', {flagged} marked * (see each)' if flagged else ''
            )
        else:
            details = ''
        self._details = details
        self.query_one('#branch-info', Static).update(details)
        self.refresh_bindings()  # The footer shows the selected kind's keys.
        self._marks = set()
        self._render_rows(next((n for n, r in enumerate(self._rows) if r.key), None))
        if self._pinned:  # A b4 run's output, kept across the reload it caused.
            self._set_shown(self._pinned)
            self._pinned = ''
        else:
            self._show(self.selected_row())

    def _render_rows(self, highlighted: Optional[int]) -> None:
        rows = self.query_one('#branch-rows', OptionList)
        rows.clear_options()
        marks = bool(self._marks)
        for n, row in enumerate(self._rows):
            prefix = ('* ' if n in self._marks else '  ') if marks else ''
            rows.add_option(Option(prefix + row.text, disabled=not row.key))
        with self.prevent(OptionList.OptionHighlighted):  # The caller shows what it needs.
            rows.highlighted = highlighted

    def marked_rows(self) -> list[DetailRow]:
        return [self._rows[n] for n in sorted(self._marks)]

    def _is_commit(self, row: Optional[DetailRow]) -> bool:
        return row is not None and is_commit(row)

    def _chosen(self) -> list[DetailRow]:
        """The marked commits, else the selected one."""
        selected = self.selected_row()
        return self.marked_rows() or ([selected] if selected and self._is_commit(selected) else [])

    def _show(self, row: Optional[DetailRow]) -> None:
        if row is None or not row.key:
            self._set_shown('')
            return

        def work() -> None:
            text = self.service.show(row)
            self.app.call_from_thread(self._set_shown, text)

        self.run_worker(work, thread=True, group='branches-show', exclusive=True)

    def _set_shown(self, text: str) -> None:
        self._shown = text
        pane = self.query_one('#branch-show-pane', TextPane)
        pane.set_text(text)
        row = self.selected_row()
        pane.border_title = row.text.strip()[:60] if row is not None and row.key else ''

    def shown_text(self) -> str:
        return self._shown

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        event.stop()  # The app's own lists handle theirs; these are this screen's.
        if event.option_list.id == 'branch-items':
            self._show_rows()
        elif event.option_list.id == 'branch-rows':
            self._show(self.selected_row())
            self.refresh_bindings()

    def action_cycle(self, step: int) -> None:
        panes = [self.query_one('#branch-items'), self.query_one('#branch-rows')]
        panes.append(self.query_one('#branch-show-pane'))
        focused = self.focused
        index = next((n for n, p in enumerate(panes) if p is focused), 0)
        panes[(index + step) % len(panes)].focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option_list.id == 'branch-items':
            self._toggle()  # Enter on a heading folds or unfolds it.

    def action_back(self) -> None:
        tab = getattr(self.app, 'action_tab', None)  # Back to LKML, the column with it.
        if tab is not None:
            tab('lkml')
        else:
            self.app.pop_screen()

    # Actions

    def check_action(self, action: str, parameters: tuple[object, ...]) -> Optional[bool]:
        key = self.selected_item() or ''
        if action == 'rebase':
            return key.startswith('base:')
        if action == 'agent_rebase':
            return key in self._failed
        if action == 'accept':
            return key in self.data.rebases
        if action == 'mark':
            rows = self.query_one('#branch-rows', OptionList)
            return self.focused is rows and self._is_commit(self.selected_row())
        if action == 'pluck':
            return bool(self._chosen() and self.data.targets.get(key))
        if action == 'new_prep':
            return bool(self._chosen())
        if action == 'new_branch':
            return bool(self._project())
        if action == 'mail_patch':
            return self._is_commit(self.selected_row()) and len(self._chosen()) == 1
        if action in ('fold', 'unfold'):
            return self.focused is self.query_one('#branch-items')
        if action == 'reap':
            return self.service.reapable(self.data, key)
        if action == 'derive':
            return key.startswith('tree:') and key not in self.data.rebases
        if action == 'prep_run' and parameters == ('compare',):
            return bool(self.data.preps.get(key, {}).get('sent'))
        if action in ('prep_run', 'edit_cover', 'sign_off', 'preview', 'send'):
            return key.startswith('prep:')
        return True

    def action_rebase(self) -> None:
        from .app import Confirm

        key = self.selected_item()
        status = self.data.statuses.get(key or '')
        if key is None or status is None:
            return
        base = status.base
        keep = sum(not c.landed for c in status.commits)
        drop = len(status.commits) - keep
        prompt = (
            f'Rebase {base.branch} onto {base.upstream}? {keep} commits replayed'
            + (f', {drop} landed dropped' if drop else '')
            + ' [y|N]'
        )

        def answered(confirmed: Optional[bool]) -> None:
            if confirmed:
                self._rebase(key, status.tip)

        self.app.push_screen(Confirm(prompt), answered)

    def _rebase(self, key: str, expected: str) -> None:
        self._status('Rebasing…')

        def work() -> None:
            try:
                result = self.service.rebase(self.workspace, key, expected)
            except Exception as exc:
                result = RebaseResult(False, error=str(exc))
            self.app.call_from_thread(self._rebased, key, result)

        self.run_worker(work, thread=True, group='branches-act')

    def _rebased(self, key: str, result: RebaseResult) -> None:
        branch = key.split(':', 2)[2]
        self._failed.pop(key, None)
        if result.ok:
            dropped = f'; dropped: {", ".join(result.dropped)}' if result.dropped else ''
            self._status(f'Rebased {branch}{dropped}{_moved(result)}')
        elif result.failed is not None:
            self._failed[key] = result
            self._status(
                f'{branch} not rebased: {result.failed.subject} does not apply; '
                'a rebases it with an agent'
            )
        else:
            self._status(f'{branch} not rebased: {result.error}')
        self._note = self.status_text()
        self.action_reload()

    def action_agent_rebase(self) -> None:
        """a, after a failed rebase: a rebase session on a copy of the base (approved
        here: nothing starts without this confirmation)."""
        from .app import Confirm

        key = self.selected_item()
        failed = self._failed.get(key or '')
        if key is None or failed is None:
            return
        status = self.data.statuses.get(key)
        where = f'{status.base.branch} onto {status.base.upstream}' if status else key
        prompt = (
            f"Launch a rebase session for {where} (pi's default model)? It rebases a copy; "
            'your branch changes only when you accept its result (A). [y|N]'
        )

        def answered(confirmed: Optional[bool]) -> None:
            if confirmed:
                self._launch_rebase(key, failed)

        self.app.push_screen(Confirm(prompt), answered)

    def _launch_rebase(self, key: str, failed: RebaseResult) -> None:
        self._status('Preparing the rebase session…')

        def work() -> None:
            try:
                record = self.service.rebase_session(self.workspace, key, failed)
                note = f'Launched rebase session {record.id}: its tree is listed here'
            except Exception as exc:
                note = f'Cannot launch the rebase session: {exc}'
            self.app.call_from_thread(self._acted, note)

        self.run_worker(work, thread=True, group='branches-act')

    def action_accept(self) -> None:
        """A, on a rebase or apply session's tree: move the base to the session's result."""
        from .app import Confirm

        key = self.selected_item()
        sid = self.data.rebases.get(key or '')
        if key is None or sid is None:
            return
        label = next((i.label for i in self.data.items if i.key == key), sid)
        prompt = f"Move the base to this session's result ({label})? [y|N]"

        def answered(confirmed: Optional[bool]) -> None:
            if confirmed:
                self._accept(sid)

        self.app.push_screen(Confirm(prompt), answered)

    def _accept(self, sid: str) -> None:
        self._status('Accepting…')

        def work() -> None:
            try:
                result = self.service.accept(self.workspace, sid)
            except Exception as exc:
                result = RebaseResult(False, error=str(exc))
            note = (
                f'Accepted: the base is at {result.tip[:12]}{_moved(result)}; '
                f'end session {sid} when done'
                if result.ok
                else f'Not accepted: {result.error}'
            )
            self.app.call_from_thread(self._acted, note)

        self.run_worker(work, thread=True, group='branches-act')

    def _acted(self, note: str) -> None:
        self._status(note)
        self._note = note
        self.action_reload()

    def action_mark(self) -> None:
        rows = self.query_one('#branch-rows', OptionList)
        index = rows.highlighted
        if index is None or not self._is_commit(self.selected_row()):
            return
        self._marks ^= {index}
        self._render_rows(index)
        self.refresh_bindings()

    def action_pluck(self) -> None:
        """p: the marked commits (or the selected one) onto a base, after a confirmation."""
        key = self.selected_item()
        if key is None:
            return
        rows = self._chosen()
        targets = list(self.data.targets.get(key, ()))
        if not rows or not targets:
            return
        if len(targets) == 1:
            self._confirm_pluck(key, rows, targets[0])
            return
        labels = [(t, _target_label(t)) for t in targets]

        def picked(target: Optional[str]) -> None:
            if target is not None:
                self._confirm_pluck(key, rows, target)

        self.app.push_screen(Pick('Pluck onto which branch?', labels), picked)

    def _confirm_pluck(self, key: str, rows: list[DetailRow], target: str) -> None:
        from .app import Confirm

        tip = self.data.tips.get(target)
        branch = _target_label(target)
        if tip is None:
            self._status(f'{branch}: not known yet; reload with F5')
            return
        n = len(rows)
        commits = f'{n} commit{"s" if n != 1 else ""}'
        if target.startswith('tree:'):
            prompt = (
                f'Copy {commits} to {branch}? They go on branches of the same name and keep '
                "their authors; a note in that session's context says what arrived. [y|N]"
            )
        else:
            prompt = (
                f'Pluck {commits} onto {branch}? Agent commits become yours, with '
                'Assisted-by; others keep their authors. [y|N]'
            )

        def answered(confirmed: Optional[bool]) -> None:
            if confirmed:
                self._pluck(key, rows, target, tip)

        self.app.push_screen(Confirm(prompt), answered)

    def _pluck(
        self,
        key: str,
        rows: list[DetailRow],
        target: str,
        expected: str,
        into_checkout: bool = False,
    ) -> None:
        self._status('Plucking…')
        branch = _target_label(target)

        def offer(checkout: str) -> None:
            from .app import Confirm

            def answered(yes: Optional[bool]) -> None:
                if yes:
                    self._pluck(key, rows, target, expected, into_checkout=True)
                else:
                    self._status(f'Not plucked: {branch} is checked out in {checkout}')

            self.app.push_screen(
                Confirm(
                    f'{branch} is checked out in {checkout}. Pluck there, fast-forwarding that '
                    'checkout (git merge --ff-only: its local changes stay, and it refuses if '
                    'they would be overwritten)? [y|N]'
                ),
                answered,
            )

        def work() -> None:
            try:
                kwargs = {'into_checkout': True} if into_checkout else {}
                result = self.service.pluck(self.workspace, key, rows, target, expected, **kwargs)
            except Exception as exc:
                result = PluckResult(False, error=str(exc))
            if result.checked_out and not into_checkout:
                self.app.call_from_thread(offer, result.checked_out)
                return
            if result.ok:
                dropped = f'; already there: {", ".join(result.dropped)}' if result.dropped else ''
                note = f'Plucked {len(result.picked)} onto {branch}{dropped}'
            elif result.failed:
                first = result.detail.splitlines()[0] if result.detail else ''
                note = f'Not plucked: {result.failed} does not apply onto {branch}' + (
                    f' ({first})' if first else ''
                )
            else:
                note = f'Not plucked: {result.error}'
            self.app.call_from_thread(self._acted, note)

        self.run_worker(work, thread=True, group='branches-act')

    def action_mail_patch(self) -> None:
        """m: one commit as a patch email through b4. A fixup replies to the series patch
        it fixes; a pull-ahead (or a fix chosen as one) is its own thread, addressed as b4
        addresses a new patch. The .eml opens in the editor; it goes after a last look."""
        key, row = self.selected_item(), self.selected_row()
        if key is None or row is None or not self._is_commit(row):
            return
        project = self._project()
        reply_to: Any = getattr(self.app, 'branch_mail_reply', None)

        def build(as_reply: bool) -> None:
            def work() -> None:
                try:
                    reply = None
                    if as_reply:
                        identity, counter = self.service.fixup_target(
                            self.workspace, key, row.branch
                        )
                        if reply_to is None or not identity:
                            raise ValueError('no series to reply to for this commit')
                        reply = reply_to(identity, counter)
                        if reply is None:
                            raise ValueError('the patch it fixes is not in the cache')
                    text = self.service.patch_message(self.workspace, key, row, reply)
                except Exception as exc:
                    self.app.call_from_thread(self._status, f'Not mailed: {exc}')
                    return
                self.app.call_from_thread(edit, text)

            self._status('Building the patch email…')
            self.run_worker(work, thread=True, group='branches-act')

        def edit(text: str) -> None:
            editor: Any = getattr(self.app, 'edit_message', None)
            edited = editor(text) if editor is not None else text
            if not edited:
                self._status('Not mailed')
                return
            confirm(edited)

        def confirm(text: str) -> None:
            msg = email.message_from_string(text, policy=email.policy.default)
            if not msg['To'] and not msg['Cc']:
                self._status('Not mailed: no recipients (add To: in the .eml next time)')
                return
            where = f'In reply to {msg["In-Reply-To"]}' if msg['In-Reply-To'] else 'As a new thread'
            lines = [
                f'Send {msg["Subject"]}?',
                where,
                f'To: {msg["To"] or "(nobody)"}',
                *([f'Cc: {msg["Cc"]}'] if msg['Cc'] else []),
                'It goes as you, with your Signed-off-by (you certify the DCO).',
            ]
            signing: Any = getattr(self.app, 'mail_signing', None)
            key = signing(project) if signing is not None else ''
            choices = [('unsigned', 'Send unsigned')]
            if key:  # A key configured: signed is the default.
                choices.insert(0, ('signed', f'Send signed with patatt ({key})'))
            else:
                lines.append('No signing key: F6 Settings sets one up for every project.')

            def answered(way: Optional[str]) -> None:
                if way in ('signed', 'unsigned'):
                    send(text, way == 'signed')

            self.app.push_screen(Pick('\n'.join(lines), choices), answered)

        def send(text: str, sign: bool) -> None:
            sender: Any = getattr(self.app, 'send_message', None)
            if sender is None:
                self._status('Not mailed: sending is not available here')
                return
            try:
                self._status(sender(project, text, sign))
            except Exception as exc:
                self._status(f'Not sent: {exc}')

        kind = row.branch.removeprefix('b4m/').split('/', 1)[0] if row.branch else ''
        if kind == 'fixup':
            build(True)
        elif kind == 'pull-ahead':
            build(False)
        else:
            choices = [
                ('reply', 'A fixup: a reply to the series patch it fixes'),
                ('thread', 'A fix of its own: a new thread, addressed as b4 does'),
            ]
            self.app.push_screen(
                Pick('Send this commit as', choices),
                lambda way: way and build(way == 'reply'),
            )

    def action_new_branch(self) -> None:
        """b: a plain branch of the selected project, at a base, an upstream, a prep
        branch, or a ref typed, to pluck onto."""
        project = self._project()
        if not project:
            return

        def make(name: str, start: str) -> None:
            def work() -> None:
                result = self.service.new_branch(self.workspace, project, name, start)
                self.app.call_from_thread(made, result)

            self._status(f'Making {name}…')
            self.run_worker(work, thread=True, group='branches-act')

        def made(result: CutResult) -> None:
            if not result.ok:
                self._status(f'No branch: {result.error}')
                return
            self._folded -= {project, f'{project}:branch'}
            self._select = f'branch:{project}:{result.branch}'
            self._note = f'Made {result.branch}: p plucks marked commits onto it'
            self.action_reload()

        def named(name: Optional[str]) -> None:
            if not name:
                return

            def started(start: Optional[str]) -> None:
                if start == '\0typed':
                    self.app.push_screen(
                        Ask(f'Start {name} at (any ref)'), lambda ref: ref and make(name, ref)
                    )
                elif start:
                    make(name, start)

            def offered(starts: list[str]) -> None:
                options = [(s, s) for s in starts] + [('\0typed', 'Another ref…')]
                self.app.push_screen(Pick(f'Start {name} at', options), started)

            def work() -> None:
                starts = self.service.branch_starts(self.workspace, project)
                self.app.call_from_thread(offered, starts)

            self.run_worker(work, thread=True, group='branches-act')

        self.app.push_screen(Ask(f'Name the new {project} branch'), named)

    def action_new_prep(self) -> None:
        """n: a prep branch from the marked commits: its name, its fork point, then a
        confirmation."""
        key = self.selected_item()
        rows = self._chosen()
        if key is None or not rows:
            return
        first = rows[0].text.split('  ', 2)[-1]
        suggested = re.sub(r'[^a-z0-9]+', '-', first.split(':', 1)[-1].lower()).strip('-')[:40]

        def named(name: Optional[str]) -> None:
            if not name:
                return
            points = self.service.fork_points(self.workspace, key)

            def typed(ref: Optional[str]) -> None:
                if ref:
                    self._confirm_cut(key, rows, name, ref)

            def forked(fork: Optional[str]) -> None:
                if fork == '\0typed':
                    self.app.push_screen(Ask(f'Fork b4/{name} from (any ref):'), typed)
                elif fork:
                    self._confirm_cut(key, rows, name, fork)

            if not points:  # None known for it: any ref, typed.
                self.app.push_screen(Ask(f'Fork b4/{name} from (any ref):'), typed)
                return
            choices = [(p, p) for p in points] + [('\0typed', 'Another ref…')]
            self.app.push_screen(Pick(f'Fork b4/{name} from?', choices), forked)

        self.app.push_screen(Ask('Prep branch name (b4/<name>):', suggested), named)

    def _confirm_cut(self, key: str, rows: list[DetailRow], name: str, fork: str) -> None:
        from .app import Confirm

        n = len(rows)
        prompt = (
            f'Make prep branch b4/{name} from {fork} with {n} commit{"s" if n != 1 else ""} '
            '(cover letter drafted from their summaries)? [y|N]'
        )

        def answered(confirmed: Optional[bool]) -> None:
            if confirmed:
                self._cut(key, rows, name, fork)

        self.app.push_screen(Confirm(prompt), answered)

    def _cut(self, key: str, rows: list[DetailRow], name: str, fork: str) -> None:
        self._status(f'Making b4/{name}…')

        def work() -> None:
            try:
                result = self.service.cut_prep(self.workspace, key, rows, name, fork)
            except Exception as exc:
                result = CutResult(False, error=str(exc))
            note = (
                f'Made {result.branch} with {result.picked} commits; edit its cover (e), '
                'add recipients (t), check it (c)'
                if result.ok
                else f'No prep branch: {result.error}'
            )
            self.app.call_from_thread(self._acted, note)

        self.run_worker(work, thread=True, group='branches-act')

    def action_prep_run(self, action: str) -> None:
        """t, c: b4 prep --auto-to-cc or --check on the selected prep branch."""
        key = self.selected_item()
        if key is None or not key.startswith('prep:'):
            return
        label = {
            'auto-to-cc': 'Collecting recipients',
            'check': 'Checking',
            'trailers': 'Collecting trailers from the list',
            'compare': 'Comparing with the last sent revision',
        }.get(action, action)
        self._status(f'{label}…')

        def work() -> None:
            try:
                code, out = self.service.prep_run(self.workspace, key, action)
            except Exception as exc:
                code, out = 1, str(exc)
            self.app.call_from_thread(self._prep_ran, action, code, out)

        self.run_worker(work, thread=True, group='branches-act')

    def _prep_ran(self, action: str, code: int, out: str) -> None:
        self._set_shown(out)
        self._pinned = out
        last = next((line for line in reversed(out.splitlines()) if line.strip()), '')
        verb = {
            'auto-to-cc': 'Recipients',
            'check': 'Check',
            'trailers': 'Trailers',
            'compare': 'Compared',
        }.get(action, action)
        self._note = f'{verb}: {last}' if code == 0 else f'{verb} failed: {last}'
        self.action_reload()

    def action_edit_cover(self) -> None:
        """e: the cover letter in the editor, through b4 prep --edit-cover (its worktree
        made in a worker first if missing; the app is suspended while the editor runs)."""
        key = self.selected_item()
        if key is None or not key.startswith('prep:'):
            return

        def work() -> None:
            try:
                worktree = self.service.prep_worktree(self.workspace, key)
            except Exception as exc:
                self.app.call_from_thread(self._status, f'Cannot edit the cover: {exc}')
                return
            if worktree is not None:
                self.app.call_from_thread(self._edit_cover, key, worktree)

        self.run_worker(work, thread=True, group='branches-act')

    def _edit_cover(self, key: str, worktree: pathlib.Path) -> None:
        branch = key.split(':', 2)[2]
        with self.app.suspend():
            code, _out = run_b4(worktree, branch, 'prep', '--edit-cover', interactive=True)
        self._note = 'Cover letter edited' if code == 0 else 'Cover letter not changed'
        self.action_reload()

    def action_sign_off(self) -> None:
        """s: the maintainer's Signed-off-by on each commit lacking it (DCO, confirmed)."""
        from .app import Confirm

        key = self.selected_item()
        tip = self.data.tips.get(key or '')
        if key is None or not key.startswith('prep:') or tip is None:
            return
        project = key.split(':', 2)[1]
        who = self.data.identities.get(project) or 'your git identity'
        n = sum(1 for r in self.data.details.get(key, ()) if is_commit(r))
        prompt = (
            f'Add Signed-off-by: {who} to the {n} commit{"s" if n != 1 else ""} of '
            f"{key.split(':', 2)[2]} that lack it? You certify the Developer's Certificate "
            'of Origin for each. [y|N]'
        )

        def answered(confirmed: Optional[bool]) -> None:
            if confirmed:
                self._sign_off(key, tip)

        self.app.push_screen(Confirm(prompt), answered)

    def _sign_off(self, key: str, tip: str) -> None:
        def work() -> None:
            try:
                result = self.service.sign_off(self.workspace, key, tip)
            except Exception as exc:
                result = SignResult(False, error=str(exc))
            note = (
                f'Signed off {result.signed} commit{"s" if result.signed != 1 else ""}'
                if result.ok
                else f'Not signed off: {result.error}'
            )
            self.app.call_from_thread(self._acted, note)

        self.run_worker(work, thread=True, group='branches-act')

    def action_preview(self) -> None:
        """o: what b4 send would send, without sending (b4 send -o)."""
        key = self.selected_item()
        if key is None or not key.startswith('prep:'):
            return
        self._status('Preparing the outgoing messages (nothing is sent)…')

        def work() -> None:
            try:
                code, text = self.service.preview(self.workspace, key)
            except Exception as exc:
                code, text = 1, str(exc)
            self.app.call_from_thread(self._previewed, code, text)

        self.run_worker(work, thread=True, group='branches-act')

    def _previewed(self, code: int, text: str) -> None:
        self._set_shown(text)
        if code == 0:
            self._status('Outgoing messages on the right; nothing was sent (S sends)')
        else:
            last = next((line for line in reversed(text.splitlines()) if line.strip()), '')
            self._status(f'b4 cannot prepare them: {last}')

    def action_send(self) -> None:
        """S: one summary of the prep branch (recipients, checks, sign-offs, cover), then
        send signed, send unsigned, or not now; what blocks it is said and refused. b4
        send then runs in the terminal, showing its own summary and asking first."""
        key = self.selected_item()
        if key is None or not key.startswith('prep:'):
            return

        def work() -> None:
            try:
                summary = self.service.send_summary(self.workspace, key)
            except Exception as exc:
                self.app.call_from_thread(self._status, f'Cannot send: {exc}')
                return
            self.app.call_from_thread(offer, summary)

        def offer(summary: Any) -> None:
            self._status('')
            lines = [
                f'Send {summary.branch} v{summary.revision}: {summary.subject}',
                f'{summary.commits} patch{"es" if summary.commits != 1 else ""}',
                f'To: {", ".join(summary.to) or "(nobody)"}',
                *([f'Cc: {", ".join(summary.cc)}'] if summary.cc else []),
                f'Checks: {summary.checks}',
            ]
            if summary.blockers:
                lines += ['Blocked until fixed:', *(f'  {b}' for b in summary.blockers)]
                choices = [('later', 'Not now: fix it first')]
            else:
                lines.append('It goes as you (you certify the DCO for each Signed-off-by).')
                choices = [('unsigned', 'Send unsigned (--no-sign)'), ('later', 'Not now')]
                if summary.signing_key:
                    choices.insert(
                        0, ('signed', f'Send signed with patatt ({summary.signing_key})')
                    )
                else:
                    lines.append('No signing key: F6 Settings sets one up for every project.')

            def answered(way: Optional[str]) -> None:
                if way in ('signed', 'unsigned'):
                    self._send(key, way == 'signed')

            self.app.push_screen(Pick('\n'.join(lines), choices), answered)

        self._status('Checking the branch before sending (b4 prep --check)…')
        self.run_worker(work, thread=True, group='branches-act')

    def _send(self, key: str, sign: bool = True) -> None:
        before = tuple(self.data.preps.get(key, {}).get('sent', ()))

        def work() -> None:
            try:
                worktree, branch, args = self.service.send_args(self.workspace, key, sign)
            except Exception as exc:
                self.app.call_from_thread(self._status, f'Cannot send: {exc}')
                return
            self.app.call_from_thread(self._send_in_terminal, worktree, branch, args, key, before)

        self.run_worker(work, thread=True, group='branches-act')

    def _send_in_terminal(
        self,
        worktree: pathlib.Path,
        branch: str,
        args: tuple[str, ...],
        key: str = '',
        before: tuple[str, ...] = (),
    ) -> None:
        with self.app.suspend():
            code, _out = run_b4(worktree, branch, *args, interactive=True)
            if code == 0:
                input('Press Enter to return to b4-maintainer…')
        self._note = f'b4 send finished for {branch}' if code == 0 else f'b4 send stopped ({code})'
        if code == 0 and key:
            record: Any = getattr(self.service, 'record_sent', None)
            try:
                self._note += record(self.workspace, key, before) if record else ''
            except Exception as exc:
                self._note += f'; not tracked: {exc}'
        self.action_reload()

    def action_reap(self) -> None:
        """x: remove the selected prep branch, local branch, or session tree, after a
        confirmation saying what goes (a session tree with changes asks twice)."""
        key = self.selected_item()
        if key is None or not self.service.reapable(self.data, key):
            return

        def work() -> None:
            try:
                text = self.service.losses(self.workspace, key)
            except Exception as exc:
                self.app.call_from_thread(self._status, f'Cannot tell what goes: {exc}')
                return
            self.app.call_from_thread(self._confirm_reap, key, text)

        self.run_worker(work, thread=True, group='branches-act')

    def _confirm_reap(self, key: str, text: str) -> None:
        from .app import Confirm

        changed = key.startswith('tree:') and not text.endswith(': clean')

        def again(confirmed: Optional[bool]) -> None:
            if confirmed:
                self._reap(key)

        def first(confirmed: Optional[bool]) -> None:
            if not confirmed:
                return
            if changed:
                prompt = f'This will be lost: {text.split(": ", 1)[-1]}. Remove anyway? [y|N]'
                self.app.push_screen(Confirm(prompt), again)
            else:
                self._reap(key)

        self.app.push_screen(Confirm(f'Remove {text}? [y|N]'), first)

    def _reap(self, key: str) -> None:
        self._status('Removing…')

        def work() -> None:
            try:
                _ok, note = self.service.reap(self.workspace, key)
            except Exception as exc:
                note = f'Not removed: {exc}'
            self.app.call_from_thread(self._acted, note)

        self.run_worker(work, thread=True, group='branches-act')

    def action_derive(self) -> None:
        """D on a session tree: a new session from its output, after choosing where the new
        tree starts; the launch dialog follows (nothing starts without it)."""
        key = self.selected_item()
        if key is None or not key.startswith('tree:') or key in self.data.rebases:
            return
        row = self.selected_row()

        def work() -> None:
            try:
                options = self.service.derive_options(self.workspace, key, row)
                context = self.service.derive_context(key, row)
            except Exception as exc:
                self.app.call_from_thread(self._status, f'Cannot derive: {exc}')
                return
            self.app.call_from_thread(self._pick_start, key, options, context)

        self.run_worker(work, thread=True, group='branches-act')

    def _pick_start(
        self, key: str, options: list[tuple[str, str, str]], context: Optional[ContextItem]
    ) -> None:
        if not options:
            self._status('That session has no tree to derive from')
            return
        parent = key.removeprefix('tree:').partition('/')[0]  # The session.
        by_key = {f'{n}': option for n, option in enumerate(options)}

        def picked(choice: Optional[str]) -> None:
            if choice is None:
                return
            start, start_from, label = by_key[choice]
            launch = getattr(self.app, 'launch_derived', None)
            if launch is None:
                self._status('Agent sessions are not available here')
                return
            launch(parent, Derived(parent, start, start_from, label), context)

        choices = [(n, option[2]) for n, option in by_key.items()]
        self.app.push_screen(
            Pick(f'Start the new tree at (from session {parent}):', choices), picked
        )


def _moved(result: RebaseResult) -> str:
    """What happened to the prep branches cut from a rebased base."""
    text = f'; moved {", ".join(result.moved)}' if result.moved else ''
    for branch, why in result.stuck:
        text += f'; {branch} stays: {why}'
    return text


def _target_label(target: str) -> str:
    """A pluck target as the maintainer reads it: a branch, or a session's tree."""
    kind, _, rest = target.partition(':')
    if kind == 'tree':
        return f"session {rest}'s tree"
    return rest.partition(':')[2]

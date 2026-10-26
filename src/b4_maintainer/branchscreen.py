# SPDX-License-Identifier: GPL-2.0-or-later
"""The Branches screen: each project's maintainer bases, worktrees, session trees, prep
branches, review branches, and other local work (left), the selected one's commits and
artifacts (middle), and the selected commit or artifact (right).

Everything is read and acted on in workers; the screen only shows results. `r` rebases a
maintainer base after a confirmation; when that stops at a conflict, `a` launches a
rebase session on a copy (after a confirmation), and `A` on that session's tree moves the
base to its result. Space marks commits, `p` plucks them, `n` makes a prep branch from
them; on a prep branch `e` `t` `c` `s` `o` `u` `d` edit, address, check, sign off,
preview, collect trailers, and compare, and `S` sends through b4 in the terminal. F5
reloads; F3 or Escape returns to the series list.
"""

import pathlib
import re
from typing import Optional, Protocol, Sequence

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Footer, Input, OptionList, Static
from textual.widgets.option_list import Option

from .adapters.bases import PluckResult, RebaseResult
from .adapters.prep import SignResult, run_b4
from .branches import BranchesSnapshot, CutResult, DetailRow, is_commit
from .sessions.records import SessionRecord
from .workspace import Workspace


class Branches(Protocol):
    def snapshot(self, workspace: Workspace) -> BranchesSnapshot: ...

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
    ) -> PluckResult: ...

    def fork_points(self, workspace: Workspace, item: str) -> list[str]: ...

    def cut_prep(
        self, workspace: Workspace, item: str, rows: Sequence[DetailRow], name: str, fork: str
    ) -> CutResult: ...

    def prep_run(self, workspace: Workspace, item: str, action: str) -> tuple[int, str]: ...

    def prep_worktree(self, workspace: Workspace, item: str) -> Optional[pathlib.Path]: ...

    def sign_off(self, workspace: Workspace, item: str, expected: str) -> SignResult: ...

    def preview(self, workspace: Workspace, item: str) -> tuple[int, str]: ...

    def send_args(
        self, workspace: Workspace, item: str
    ) -> tuple[pathlib.Path, str, tuple[str, ...]]: ...


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
    Pick > Vertical { width: auto; height: auto; border: solid $accent; background: $panel; }
    Pick OptionList { width: auto; height: auto; border: none; }
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


class BranchesScreen(Screen[None]):
    DEFAULT_CSS = """
    BranchesScreen #branch-title { height: 1; padding: 0 1; text-style: bold; }
    BranchesScreen #branch-panes { height: 1fr; }
    BranchesScreen #branch-items { width: 2fr; border: none; }
    BranchesScreen #branch-rows { width: 3fr; border: none; border-left: solid $panel-lighten-2; }
    BranchesScreen #branch-show-pane { width: 4fr; border-left: solid $panel-lighten-2; }
    BranchesScreen #branch-status { height: 1; padding: 0 1; }
    """
    BINDINGS = [
        Binding('escape', 'back', 'back'),
        Binding('f3', 'back', 'series', key_display='F3'),
        Binding('tab', 'cycle(1)', 'next pane', priority=True),
        Binding('shift+tab', 'cycle(-1)', 'previous pane', priority=True, show=False),
        Binding('r', 'rebase', 'rebase base'),
        Binding('a', 'agent_rebase', 'rebase with an agent'),
        Binding('A', 'accept', 'accept rebase'),
        Binding('space', 'mark', 'mark'),
        Binding('p', 'pluck', 'pluck'),
        Binding('n', 'new_prep', 'new prep branch'),
        Binding('t', 'prep_run("auto-to-cc")', 'to/cc'),
        Binding('c', 'prep_run("check")', 'check'),
        Binding('e', 'edit_cover', 'edit cover'),
        Binding('s', 'sign_off', 'sign off'),
        Binding('o', 'preview', 'outgoing'),
        Binding('S', 'send', 'send'),
        Binding('u', 'prep_run("trailers")', 'trailers'),
        Binding('d', 'prep_run("compare")', 'compare'),
        Binding('f5', 'reload', 'reload', key_display='F5'),
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

    def compose(self) -> ComposeResult:
        yield Static('Branches', id='branch-title')
        with Horizontal(id='branch-panes'):
            yield OptionList(id='branch-items')
            yield OptionList(id='branch-rows')
            with VerticalScroll(id='branch-show-pane'):
                yield Static('', id='branch-show', markup=False)
        yield Static('Loading…', id='branch-status', markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one('#branch-items', OptionList).focus()
        self.action_reload()

    # Loading

    def action_reload(self) -> None:
        def work() -> None:
            try:
                data = self.service.snapshot(self.workspace)
            except Exception as exc:
                self.app.call_from_thread(self._status, f'Cannot read branches: {exc}')
                return
            self.app.call_from_thread(self._loaded, data)

        self.run_worker(work, thread=True, group='branches-load', exclusive=True)

    def _loaded(self, data: BranchesSnapshot) -> None:
        items = self.query_one('#branch-items', OptionList)
        selected = self.selected_item()
        self.data = data
        items.clear_options()
        project = None
        for item in data.items:
            if item.project != project:
                project = item.project
                items.add_option(Option(project, disabled=True))
            items.add_option(Option(f'  {item.label}', id=item.key))
        keys = [i.key for i in data.items]
        if keys:
            target = selected if selected in keys else keys[0]
            with self.prevent(OptionList.OptionHighlighted):  # Shown below, once.
                items.highlighted = next(
                    n
                    for n in range(items.option_count)
                    if items.get_option_at_index(n).id == target
                )
        self._show_rows()
        empty = '' if keys else 'No projects with a checkout.'
        self._status('; '.join(p for p in (self._note, *data.problems, empty) if p))
        self._note = ''

    def _status(self, text: str) -> None:
        self._status_text = text
        self.query_one('#branch-status', Static).update(text)

    def status_text(self) -> str:
        return self._status_text

    # Panes

    def selected_item(self) -> Optional[str]:
        option = self.query_one('#branch-items', OptionList).highlighted_option
        return option.id if option is not None else None

    def selected_row(self) -> Optional[DetailRow]:
        rows = self.query_one('#branch-rows', OptionList)
        index = rows.highlighted
        if index is None or index >= len(self._rows):
            return None
        return self._rows[index]

    def _show_rows(self) -> None:
        key = self.selected_item()
        self._rows = tuple(self.data.details.get(key, ())) if key else ()
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
        self.query_one('#branch-show', Static).update(text)

    def shown_text(self) -> str:
        return self._shown

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        event.stop()  # The app's own lists handle theirs; these are this screen's.
        if event.option_list.id == 'branch-items':
            self._show_rows()
        elif event.option_list.id == 'branch-rows':
            self._show(self.selected_row())

    def action_cycle(self, step: int) -> None:
        panes = [self.query_one('#branch-items'), self.query_one('#branch-rows')]
        panes.append(self.query_one('#branch-show-pane'))
        focused = self.focused
        index = next((n for n, p in enumerate(panes) if p is focused), 0)
        panes[(index + step) % len(panes)].focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()

    def action_back(self) -> None:
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
            self._status(f'Rebased {branch}{dropped}')
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
        """A, on a rebase session's tree: move the base to the session's result."""
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
                f'Accepted: the base is at {result.tip[:12]}; end session {sid} when done'
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
        labels = [(t, t.split(':', 2)[2]) for t in targets]

        def picked(target: Optional[str]) -> None:
            if target is not None:
                self._confirm_pluck(key, rows, target)

        self.app.push_screen(Pick('Pluck onto which branch?', labels), picked)

    def _confirm_pluck(self, key: str, rows: list[DetailRow], target: str) -> None:
        from .app import Confirm

        tip = self.data.tips.get(target)
        branch = target.split(':', 2)[2]
        if tip is None:
            self._status(f'{branch}: not known yet; reload with F5')
            return
        n = len(rows)
        prompt = (
            f'Pluck {n} commit{"s" if n != 1 else ""} onto {branch}? Agent commits become '
            'yours, with Assisted-by; others keep their authors. [y|N]'
        )

        def answered(confirmed: Optional[bool]) -> None:
            if confirmed:
                self._pluck(key, rows, target, tip)

        self.app.push_screen(Confirm(prompt), answered)

    def _pluck(self, key: str, rows: list[DetailRow], target: str, expected: str) -> None:
        self._status('Plucking…')
        branch = target.split(':', 2)[2]

        def work() -> None:
            try:
                result = self.service.pluck(self.workspace, key, rows, target, expected)
            except Exception as exc:
                result = PluckResult(False, error=str(exc))
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
            choices = [(p, p) for p in points]

            def forked(fork: Optional[str]) -> None:
                if fork:
                    self._confirm_cut(key, rows, name, fork)

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
        """S: send through b4 send in the terminal; b4 shows its summary and asks first."""
        from .app import Confirm

        key = self.selected_item()
        if key is None or not key.startswith('prep:'):
            return
        branch = key.split(':', 2)[2]
        tracking = self.data.preps.get(key, {})
        prompt = (
            f'Send {branch} v{tracking.get("revision", "?")} with b4 send? The app steps aside: '
            'b4 shows what it will send and asks you to press Enter (Ctrl-C aborts). [y|N]'
        )

        def answered(confirmed: Optional[bool]) -> None:
            if confirmed:
                self._send(key)

        self.app.push_screen(Confirm(prompt), answered)

    def _send(self, key: str) -> None:
        def work() -> None:
            try:
                worktree, branch, args = self.service.send_args(self.workspace, key)
            except Exception as exc:
                self.app.call_from_thread(self._status, f'Cannot send: {exc}')
                return
            self.app.call_from_thread(self._send_in_terminal, worktree, branch, args)

        self.run_worker(work, thread=True, group='branches-act')

    def _send_in_terminal(self, worktree: pathlib.Path, branch: str, args: tuple[str, ...]) -> None:
        with self.app.suspend():
            code, _out = run_b4(worktree, branch, *args, interactive=True)
            if code == 0:
                input('Press Enter to return to b4-maintainer…')
        self._note = f'b4 send finished for {branch}' if code == 0 else f'b4 send stopped ({code})'
        self.action_reload()

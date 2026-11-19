# SPDX-License-Identifier: GPL-2.0-or-later
"""The Forge tab (F7): agentic development as a local mailing list.

With no forge open it lists the forges; Enter opens one, and the whole tab is that
forge: its rounds (plans, series rounds, upstream postings), the selected round's thread
(read as any thread is, findings marked), and the selected message. One forge is open at
a time. Escape closes it, then returns to LKML.

Sessions start only through the launch dialog, its task preselected (P, I, s, R, T);
nothing here starts one by itself, and nothing here sends mail: a finished round is
folded into a prep branch (f), sent from Branches.
"""

import dataclasses
import pathlib
import subprocess
from typing import Any, Callable, Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, OptionList, Static
from textual.widgets.option_list import Option

from .branchscreen import Ask, Menu, Pick
from .forge import Forge, ForgeService, Round, check_base, note_label
from .model import SashikoReview, Series, SeriesState, SeriesThread, WorkItem
from .reader import (
    QUOTE_LINES,
    TreeRow,
    find_post,
    has_long_quote,
    message_blocks,
    post_keys,
    root_blocks,
    thread_replies,
    tree_prompt,
    tree_rows,
)
from .widgets import Block, TabBar, TextPane, tree_width
from .workspace import Workspace

# The reviews s r offers: (profile, label, letter). Sashiko is a job, not a session.
REVIEWS = (
    ('forge-review-prompts', 'review-prompts (kernel review protocol)', 'p'),
    ('forge-kernel-style', 'kernel-style (style, structure, changelogs)', 'k'),
    ('forge-sashiko', 'Sashiko (local pipeline, pi as its model)', 's'),
)


def companion_mail(rnd: Round, msgid: str) -> str:
    """What a companion is told when the maintainer replies on the forge."""
    return (
        f"The maintainer wrote on {rnd.label}: message <{msgid}> in the forge's "
        f'rounds/{rnd.key}.mbx (focus.md is on it). Answer it as a reply in '
        'out/forge/replies/ (a new file, In-Reply-To that Message-ID), then end your turn.'
    )


def forge_label(forge: Forge, rounds: list[Round]) -> str:
    newest = rounds[-1].label if rounds else 'nothing posted'
    return f'{forge.label} · {forge.project} on {forge.base or "(no base)"} · {newest}'


class ForgeScreen(Screen[None]):
    DEFAULT_CSS = """
    ForgeScreen #forge-list { height: 1fr; border: solid $panel-lighten-2; }
    ForgeScreen #forge-open { display: none; height: 1fr; }
    ForgeScreen.-open #forge-list { display: none; }
    ForgeScreen.-open #forge-open { display: block; }
    ForgeScreen #forge-left { width: 3fr; }
    ForgeScreen #forge-rounds { height: auto; max-height: 10; border: solid $panel-lighten-2; }
    ForgeScreen #forge-tree { height: 1fr; border: solid $panel-lighten-2; }
    ForgeScreen #forge-content { width: 5fr; border: solid $panel-lighten-2; }
    ForgeScreen #forge-status { height: 1; }
    """
    BINDINGS = [
        Binding('escape', 'back', 'back'),
        Binding('tab', 'cycle(1)', 'next pane', priority=True, show=False),
        Binding('shift+tab', 'cycle(-1)', 'previous pane', priority=True, show=False),
        # No forge open.
        Binding('n', 'new', 'new forge…'),
        Binding('x', 'remove', 'remove forge'),
        # A forge open: few keys, each group a menu with a letter per choice.
        Binding('s', 'sessions', 'session…'),
        Binding('r', 'reply', 'reply'),
        Binding('A', 'accept', 'accept plan'),
        Binding('v', 'view', 'view…'),
        Binding('u', 'upstream', 'upstream…'),
        Binding('e', 'edit_prompt', 'goal'),
        Binding('c', 'collect', 'collect'),
        Binding('z', 'toggle_quotes', 'quotes'),
    ]
    _CLOSED = frozenset({'new', 'remove'})
    _OPEN = frozenset({'sessions', 'reply', 'view', 'upstream', 'edit_prompt', 'collect'})

    def __init__(self, service: ForgeService, workspace: Optional[Workspace] = None) -> None:
        super().__init__()
        self.service = service
        self.workspace = workspace
        self.stale = False  # Set when the app switches back to this tab.
        self._forges: list[tuple[Forge, list[Round]]] = []
        self._open: Optional[Forge] = None
        self._rounds: list[Round] = []
        self._thread: Optional[SeriesThread] = None
        self._item: Optional[WorkItem] = None
        self._rows: list[TreeRow] = []
        self._status_text = ''
        self._keep_round = ''  # The round to select after a reload (its key).
        # The reader's place kept across a reload: the message (its Message-ID), and, for
        # one in the background, where the message was scrolled to (None: not kept).
        self._keep_msgid = ''
        self._keep_scroll: Optional[float] = None
        self._note = ''  # What the status line says after it.
        self._goal = ''  # The open forge's goal (prompt), as read with its rounds.
        self._expand_quotes = False  # z: long quotes open in every message.
        self._sashiko_progress = ''  # The open forge's Sashiko job, as far as it is.
        self._quiet = False  # The reload under way is in the background.

    def compose(self) -> ComposeResult:
        yield TabBar('forge')
        yield OptionList(id='forge-list')
        with Horizontal(id='forge-open'):
            with Vertical(id='forge-left'):
                yield OptionList(id='forge-rounds')
                yield OptionList(id='forge-tree')
            yield TextPane(id='forge-content')
        yield Static('', id='forge-status', markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one('#forge-list').border_title = 'Forges'
        self.query_one('#forge-rounds').border_title = 'Rounds'
        self.query_one('#forge-tree').border_title = 'Thread'
        self.query_one('#forge-list', OptionList).focus()
        self._reload()

    def on_screen_resume(self) -> None:
        # Switched back to (the app marks it stale), not just a prompt closing over it.
        if not self.stale:
            return
        self.stale = False
        if self._open is not None:
            rnd = self._round()
            self._reopen(rnd.key if rnd is not None else '', collect=True)
        else:
            self._reload()

    def reload_rounds(self, note: str = '') -> None:
        """The open forge's rounds read again (the app collected new ones) in the
        background: the maintainer keeps their place (round, message, scroll, focus);
        *note* says what arrived."""
        rnd = self._round()
        self._reopen(rnd.key if rnd is not None else '', note, quiet=True)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> Optional[bool]:
        if action == 'remove':
            return not self.is_open() and bool(self._forges)
        if action == 'accept':  # Only on a plan round.
            rnd = self._round() if self.is_open() else None
            return rnd is not None and rnd.kind == 'plan'
        if action in self._CLOSED:
            return not self.is_open()
        if action in self._OPEN:
            return self.is_open()
        if action == 'toggle_quotes':
            if not self.is_open():
                return False
            _row, reply = self._message()
            return reply is not None and has_long_quote(reply, self._quote_lines())
        return True

    # Helpers the tests and the app read.

    def is_open(self) -> bool:
        return self.has_class('-open')

    def status_text(self) -> str:
        return self._status_text

    def forge_labels(self) -> list[str]:
        return [forge_label(f, r) for f, r in self._forges]

    def open_forge(self) -> Optional[Forge]:
        return self._open

    def round_labels(self) -> list[str]:
        return [r.label for r in self._rounds]

    def selected_round(self) -> str:
        rnd = self._round()
        return rnd.label if rnd is not None else ''

    def select_forge(self, label: str) -> None:
        index = next(n for n, (f, _r) in enumerate(self._forges) if f.label == label)
        self.query_one('#forge-list', OptionList).highlighted = index

    def select_round(self, label: str) -> None:
        index = next(n for n, r in enumerate(self._rounds) if r.label == label)
        self.query_one('#forge-rounds', OptionList).highlighted = index

    def thread_labels(self) -> list[str]:
        return [r.label for r in self._rows]

    def select_message(self, index: int) -> None:
        tree = self.query_one('#forge-tree', OptionList)
        tree.highlighted = index
        tree.focus()

    def content_text(self) -> str:
        return self.query_one('#forge-content', TextPane).text

    def review(self) -> Optional[SashikoReview]:
        return self._item.sashiko if self._item is not None else None

    def show_notice(self, text: str) -> None:
        """The app's notices (a background session's news) in this tab's status line."""
        self._status(text)

    def _status(self, text: str) -> None:
        self._status_text = text
        self.query_one('#forge-status', Static).update(text)

    def _work(self, call: Callable[[], Any], done: Callable[[Any], None], group: str) -> None:
        """Run blocking *call* in a worker; *done* gets its result on the UI thread, or the
        status line its error."""

        def work() -> None:
            try:
                result = call()
            except Exception as exc:
                self.app.call_from_thread(self._status, f'Forge: {exc}')
                return
            self.app.call_from_thread(done, result)

        self.run_worker(work, thread=True, group=group, exclusive=True)

    def _root(self) -> pathlib.Path:
        return self.service.var.parent  # The workspace.

    def _checkout(self, forge: Forge) -> pathlib.Path:
        return self._project_checkout(forge.project)

    def _project_checkout(self, project_id: str) -> pathlib.Path:
        project = self.workspace.project(project_id) if self.workspace is not None else None
        path = project.path if project is not None else f'projects/{project_id}'
        return self._root() / path

    def _me(self, forge: Forge) -> tuple[str, str]:
        """The maintainer as replies name them (blocking): git's identity in the project's
        checkout, the address from `me` when it is configured."""

        checkout = self._checkout(forge)

        def config(key: str) -> str:
            where = checkout if checkout.is_dir() else None  # Else the global identity.
            run = subprocess.run(['git', 'config', key], cwd=where, capture_output=True, text=True)
            return run.stdout.strip() if run.returncode == 0 else ''

        me = self.workspace.me if self.workspace is not None else ()
        address = me[0] if me else config('user.email')
        return config('user.name') or address, address

    # The list of forges.

    def _reload(self, select: str = '', note: str = '') -> None:
        """Read the forges again; select *select*; *note* is the status line after."""
        service = self.service

        def read() -> tuple[list[tuple[Forge, list[Round]]], list[str]]:
            forges, problems = service.store.list()
            return [(f, service.rounds(f)) for f in forges], problems

        self._work(read, lambda found: self._fill(*found, select, note), 'forge-list')

    def _fill(
        self,
        forges: list[tuple[Forge, list[Round]]],
        problems: list[str],
        select: str,
        note: str = '',
    ) -> None:
        self._forges = forges
        listing = self.query_one('#forge-list', OptionList)
        current = select or self._highlighted_id()
        listing.clear_options()
        listing.add_options([Option(forge_label(f, r), id=f.id) for f, r in forges])
        if forges:
            ids = [f.id for f, _r in forges]
            listing.highlighted = ids.index(current) if current in ids else 0
        self.refresh_bindings()  # x needs a forge.
        said = '; '.join([*([note] if note else []), *problems])
        if not forges:
            said = said or 'n makes a forge: blank, from an upstream series, or from an mbox'
        elif not said:
            said = 'Enter opens a forge; n makes one; x removes one'
        self._status(said)

    def _highlighted_id(self) -> str:
        option = self.query_one('#forge-list', OptionList).highlighted_option
        return (option.id or '') if option is not None else ''

    def action_remove(self) -> None:
        """x: remove the highlighted forge, once asked: what goes (its rounds, workbench
        and space, its sessions, live ones ended) and what stays (its prep branch). A
        session tree with changes asks a second time."""
        from .app import Confirm

        fid = self._highlighted_id()
        forge = next((f for f, _r in self._forges if f.id == fid), None)
        if forge is None:
            return
        service = self.service
        checkout = self._checkout(forge)
        look = getattr(self.app, 'bench_removal', None)
        reap = getattr(self.app, 'remove_bench_sessions', None)

        def gather() -> tuple[tuple[int, str], tuple[list[str], list[str], dict[str, Any]]]:
            found = look(forge.workbench) if look is not None else ([], [], {})
            return service.removal(forge, checkout), found

        def ask(found: tuple[tuple[int, str], tuple[list[str], list[str], dict[str, Any]]]) -> None:
            (rounds, prep), (sids, live, losses) = found
            many = 'round' if rounds == 1 else 'rounds'
            lines = [
                f'Remove the forge {forge.label}?',
                f'  {rounds} {many}, its goal and design, its workbench and space go',
            ]
            if sids:
                listed = ', '.join(f'{s} (live: ended)' if s in live else s for s in sids)
                lines.append(f'  Sessions, deleted with their output: {listed}')
                lines += [f'  {path}: {state.describe()}' for path, state in losses.items()]
            if prep:
                lines.append(f'  Its prep branch {prep} stays in {forge.project}')
            else:
                lines.append('  Nothing folded: no prep branch to keep')
            changed = [f'  {p}: {st.describe()}' for p, st in losses.items() if not st.clean]

            def removed(_none: object) -> None:
                ended = f' and {len(sids)} session{"s" if len(sids) != 1 else ""}' if sids else ''
                self._reload(note=f'Removed {forge.label}{ended}')

            def remove() -> None:
                def work() -> None:
                    if sids and reap is not None:
                        reap(sids)
                    service.remove(forge)

                self._work(work, removed, 'forge-remove')

            def again(yes: Optional[bool]) -> None:
                if yes:
                    remove()

            def first(yes: Optional[bool]) -> None:
                if not yes:
                    return
                if changed:
                    prompt = 'This will be lost:\n' + '\n'.join(changed) + '\nRemove anyway? [y|N]'
                    self.app.push_screen(Confirm(prompt), again)
                else:
                    remove()

            self.app.push_screen(Confirm('\n'.join([*lines, '[y|N]'])), first)

        self._status('Looking at what removing it takes…')
        self._work(gather, ask, 'forge-remove')

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()  # The app's own lists handle theirs.
        if event.option_list.id == 'forge-list':
            found = next((f for f, _r in self._forges if f.id == event.option.id), None)
            if found is not None:
                self._open_forge(found)

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        event.stop()
        if event.option_list.id == 'forge-rounds':
            self._load_thread()
        elif event.option_list.id == 'forge-tree':
            self._show_message()

    # An open forge.

    def _open_forge(self, forge: Forge) -> None:
        self._open = forge
        self._thread = self._item = None
        self._rows = []
        self.add_class('-open')
        self._reopen(collect=True)

    def _reopen(
        self,
        keep: str = '',
        note: str = '',
        collect: bool = False,
        asked: bool = False,
        quiet: bool = False,
    ) -> None:
        """Read the open forge's rounds again; select *keep* (a round key), else the
        newest, on the message selected when it is the round shown; *note* stays in the
        status line. *collect*: first take what its sessions left since (opening the
        forge, coming back to the tab); *asked*: c asked for it, so nothing new is said
        too. *quiet*: in the background: the scroll and focus stay too."""
        forge = self._open
        if forge is None:
            return
        shown = self._round()
        self._keep_round = keep
        self._keep_msgid = self._selected_msgid() if shown is not None and shown.key == keep else ''
        content = self.query_one('#forge-content', TextPane)
        self._keep_scroll = content.scroll_y if quiet and self._keep_msgid else None
        self._quiet = quiet
        self._note = note
        service = self.service

        def read() -> list[Round]:
            if collect:
                try:
                    said = service.collect(forge)
                except Exception as exc:  # The forge still opens: its rounds as they are.
                    said = [f'Cannot collect: {exc} (c tries again)']
                if said:
                    self._note = '; '.join(said)
                    self._keep_round = self._keep_msgid = ''  # Show what arrived.
                    self._keep_scroll = None
                elif asked:
                    self._note = "Nothing new from the forge's sessions"
            if collect:  # A Sashiko job that ended (the app closed meanwhile, say).
                try:
                    progress, landed = service.sashiko_poll(forge)
                except Exception as exc:
                    progress, landed = '', f'Sashiko: {exc}'
                if landed:
                    self._note = '; '.join(x for x in (self._note, landed) if x)
                self._sashiko_progress = progress
            self._goal = service.store.prompt(forge.id)
            return service.rounds(forge)

        self._work(read, self._fill_rounds, 'forge-rounds')

    def _fill_rounds(self, rounds: list[Round]) -> None:
        self._rounds = rounds
        if self._open is not None:
            self.show_sashiko(self._open.id, self._sashiko_progress)
        listing = self.query_one('#forge-rounds', OptionList)
        with self.prevent(OptionList.OptionHighlighted):
            listing.clear_options()
            listing.add_options([Option(r.label) for r in rounds])
        if self._open is not None:
            self.query_one('#forge-tree').border_title = self._open.label
        if rounds:
            keys = [r.key for r in rounds]
            index = keys.index(self._keep_round) if self._keep_round in keys else len(rounds) - 1
            with self.prevent(OptionList.OptionHighlighted):
                listing.highlighted = index
            self._load_thread()
            if not self._quiet:  # In the background, focus stays where the reader is.
                self.query_one('#forge-tree', OptionList).focus()
            self._status(
                self._note or 's starts a session (review, submit the round…); r replies; v views'
            )
        else:
            self.query_one('#forge-tree', OptionList).clear_options()
            goal = self._goal.strip()
            said = (
                f'Goal:\n\n{goal}\n\ne edits it in your editor; running sessions read it. '
                's p designs the work with you; s i implements it.'
                if goal
                else 'No goal yet: e writes it in your editor, or s p starts a design session '
                'that works it out with you.'
            )
            self.query_one('#forge-content', TextPane).set_text(
                f'Nothing posted yet.\n\n{said}\n\nWhat a session leaves arrives here when it '
                'finishes.'
            )
            listing.focus()
            self._status(self._note or 'Nothing posted yet; escape closes the forge')

    def show_sashiko(self, fid: str, progress: str) -> None:
        """The open forge's Sashiko job, as far as it is (patches reviewed of all), on the
        rounds list; '' when none runs."""
        if self._open is None or self._open.id != fid:
            return
        self._sashiko_progress = progress  # Kept for the next reload of the rounds.
        rounds = self.query_one('#forge-rounds', OptionList)
        rounds.border_subtitle = f'Sashiko {progress}' if progress else ''

    def _round(self) -> Optional[Round]:
        index = self.query_one('#forge-rounds', OptionList).highlighted
        if index is None or index >= len(self._rounds):
            return None
        return self._rounds[index]

    def _load_thread(self) -> None:
        forge, rnd = self._open, self._round()
        if forge is None or rnd is None:
            return
        service = self.service

        def read() -> tuple[Any, SeriesThread, Optional[SashikoReview]]:
            series, thread = service.thread(forge, rnd)
            return series, thread, service.review(forge, rnd, thread)

        self._work(read, self._thread_loaded, 'forge-thread')

    def _thread_loaded(self, loaded: tuple[Any, SeriesThread, Optional[SashikoReview]]) -> None:
        series, thread, review = loaded
        project = self._open.project if self._open is not None else None
        self._item = WorkItem(series, SeriesState(), project=project, sashiko=review)
        self._thread = thread
        bots = self.workspace.bots if self.workspace is not None else ()
        rows = tree_rows(thread, self._item, set(post_keys(thread)), bots=bots)
        self._rows = [self._note_row(thread, row) for row in rows]
        self.call_after_refresh(self._fill_tree)  # Shown first: rows fit its width.

    @staticmethod
    def _note_row(thread: SeriesThread, row: TreeRow) -> TreeRow:
        """A row of one of the round's notes named for it: `notes: Summary · forge agent`."""
        replies = thread_replies(thread, row.key) if row.node else ()
        if not row.node or row.node > len(replies):
            return row
        message = replies[row.node - 1].message
        label = note_label(message.subject)
        if not label or message.author_name not in row.label:
            return row
        named = row.label.replace(message.author_name, f'notes: {label} · {message.author_name}', 1)
        return dataclasses.replace(row, label=named)

    def _fill_tree(self) -> None:
        tree = self.query_one('#forge-tree', OptionList)
        with self.prevent(OptionList.OptionHighlighted):
            tree.clear_options()
            width = tree_width(tree)
            tree.add_options([Option(tree_prompt(r, width)) for r in self._rows])
            if self._rows:
                kept = self._row_of(self._keep_msgid) if self._keep_msgid else None
                tree.highlighted = kept if kept is not None else 0
        scroll = self._keep_scroll if self._keep_msgid else None
        self._keep_msgid, self._keep_scroll = '', None
        self._show_message(scroll)

    def _row_of(self, msgid: str) -> Optional[int]:
        """The tree row showing message *msgid*, if any."""
        for index in range(len(self._rows)):
            if self._msgid_at(index) == msgid:
                return index
        return None

    def _quote_lines(self) -> int:
        return self.workspace.quote_lines if self.workspace is not None else QUOTE_LINES

    def action_collect(self) -> None:
        """c: collect now what the forge's sessions left: collecting happens on its own
        when a session's turn ends and when the forge opens, and this is the way again
        when that failed. Each result is taken once."""
        rnd = self._round()
        self._status('Collecting…')
        self._reopen(rnd.key if rnd is not None else '', collect=True, asked=True)

    def action_toggle_quotes(self) -> None:
        """z: every long quote opened, or folded again (a click toggles one); later
        messages open the same way."""
        self._expand_quotes = not self._expand_quotes
        self.query_one('#forge-content', TextPane).set_folds(self._expand_quotes)

    def _message(self, index: Optional[int] = -1) -> tuple[Optional[TreeRow], Any]:
        """The row at *index* (the highlighted one by default) and its reply, if any."""
        if index == -1:
            index = self.query_one('#forge-tree', OptionList).highlighted
        if self._thread is None or index is None or index >= len(self._rows):
            return None, None
        row = self._rows[index]
        replies = thread_replies(self._thread, row.key) if row.node else ()
        reply = replies[row.node - 1] if row.node and row.node <= len(replies) else None
        return row, reply

    def _show_message(self, scroll: Optional[float] = None) -> None:
        """The highlighted message in the content pane; *scroll*: the same message shown
        again (a reload in the background): unchanged, it stays as it is (scroll, opened
        quotes); changed, it is shown scrolled to where it was."""
        content = self.query_one('#forge-content', TextPane)
        row, reply = self._message()
        self.refresh_bindings()  # z shows only on a message with a long quote.
        if row is None or self._thread is None or self._item is None:
            content.set_text('')
            return
        if reply is not None:
            blocks, opened = message_blocks(reply, self._quote_lines()), self._expand_quotes
        else:
            blocks, opened = root_blocks(self._thread, row.key, self._item), False
        if scroll is not None and content.shows(blocks):
            return
        content.set_blocks(blocks, open_folds=opened)
        if scroll is not None:
            content.scroll_to(y=scroll, animate=False, immediate=True)

    def _selected_msgid(self) -> str:
        return self._msgid_at(-1)

    def _msgid_at(self, index: Optional[int]) -> str:
        """The Message-ID of the message at tree row *index* (-1: the highlighted one)."""
        row, reply = self._message(index)
        if reply is not None:
            return reply.message.msgid
        post = find_post(self._thread, row.key) if row is not None and self._thread else None
        return post.message.msgid if post is not None else ''

    def _close_forge(self) -> None:
        opened = self._open
        self._open = None
        self._rounds, self._rows = [], []
        self._thread = self._item = None
        self.remove_class('-open')
        self.query_one('#forge-list', OptionList).focus()
        self._reload(opened.id if opened is not None else '')

    # Keys.

    def action_back(self) -> None:
        if self.is_open():
            self._close_forge()
            return
        tab = getattr(self.app, 'action_tab', None)  # Back to LKML.
        if tab is not None:
            tab('lkml')
        else:
            self.app.pop_screen()

    def action_cycle(self, step: int) -> None:
        if self.is_open():
            panes = ['#forge-rounds', '#forge-tree', '#forge-content']
        else:
            panes = ['#forge-list']
        focused = self.focused
        ids = [f'#{focused.id}'] if focused is not None and focused.id else []
        at = panes.index(ids[0]) if ids and ids[0] in panes else -1
        self.query_one(panes[(at + step) % len(panes)]).focus()

    def _projects(self) -> list[tuple[str, str]]:
        projects = self.workspace.projects if self.workspace is not None else ()
        return [(p.id, p.label) for p in projects]

    def _default_base(self, project: str) -> str:
        found = self.workspace.project(project) if self.workspace is not None else None
        return found.bases[0].branch if found is not None and found.bases else ''

    def action_new(self) -> None:
        """n: a new forge: blank, from a series posted upstream, or from an mbox file."""
        choices = [
            ('blank', 'blank: a name, a project, a base', 'b'),
            ('upstream', 'from a series posted upstream (its newest version)', 'u'),
            ('mbox', 'from an mbox file (a saved thread, or b4 send -o)', 'm'),
        ]
        actions = {
            'blank': self._new_blank,
            'upstream': self._from_upstream,
            'mbox': self._import_mbox,
        }

        def picked(key: Optional[str]) -> None:
            if key in actions:
                actions[key]()

        self.app.push_screen(Menu('New forge', choices), picked)

    def action_sessions(self) -> None:
        """s: start a session on the forge: each opens the launch dialog, its task
        ticked; reviews ask which."""
        choices = [
            ('forge-plan', 'design: work out the goal and design with an agent', 'p'),
            ('forge-implement', 'implement the first round', 'i'),
            ('forge-rework', 'submit the round: rework it into the next version', 's'),
            ('review', 'review the selected round…', 'r'),
            ('forge-test', 'test the newest round, a reply per patch', 't'),
            ('companion', 'companion: ask about the selected round', 'c'),
            ('open', "open one of the forge's sessions already running…", 'o'),
        ]

        def picked(key: Optional[str]) -> None:
            if key is None:
                return
            if key == 'review':
                self._review()
            elif key == 'open':
                self._open_session()
            elif key == 'companion':
                self._companion()
            else:
                self.action_launch(key)

        self.app.push_screen(Menu('Start a session', choices), picked)

    def _open_session(self) -> None:
        """s o: one of the forge's live sessions (they run in its workbench) beside the
        app again, focused: one left behind when the forge was closed, a review, a
        companion."""
        forge = self._open
        listed = getattr(self.app, 'bench_sessions', None)
        opener = getattr(self.app, 'open_bench_session', None)
        if forge is None or listed is None or opener is None:
            self._status('Agent sessions are not available here')
            return
        found = listed(forge.workbench)
        if not found:
            self._status("None of the forge's sessions is running: s starts one")
            return

        def picked(sid: Optional[str]) -> None:
            if sid:
                opener(sid)

        self.app.push_screen(Pick(f'{forge.label}: which session?', found), picked)

    def action_view(self) -> None:
        """v: other views of the selected round."""
        choices = [
            ('timeline', 'timeline: every round, where the work started and is', 't'),
            ('diff', 'range-diff against the round before', 'd'),
            ('look', 'checkout at the selected patch, in a tmux window', 'o'),
        ]
        actions = {'timeline': self._timeline, 'diff': self._range_diff, 'look': self._look}

        def picked(key: Optional[str]) -> None:
            if key in actions:
                actions[key]()

        self.app.push_screen(Menu('View', choices), picked)

    def action_upstream(self) -> None:
        """u: between the forge and upstream."""
        choices = [
            ('fold', "fold the selected round into the project's prep branch", 'f'),
            ('harvest', 'harvest an upstream posting as the next input', 'h'),
        ]
        actions = {'fold': self._fold, 'harvest': self._harvest}

        def picked(key: Optional[str]) -> None:
            if key in actions:
                actions[key]()

        self.app.push_screen(Menu('Upstream', choices), picked)

    def _new_blank(self) -> None:
        """A forge, named, for one project, from a base; its workbench made with it.
        Nothing starts."""
        projects = self._projects()
        if not projects:
            self._status('No projects in the workspace')
            return

        def named(label: Optional[str]) -> None:
            if not label or not label.strip():
                return

            def picked(project: Optional[str]) -> None:
                if not project:
                    return

                def based(base: Optional[str]) -> None:
                    if base is None:
                        return
                    service = self.service
                    self._make(
                        project,
                        base.strip(),
                        lambda: service.create(label.strip(), project, base.strip(), ''),
                    )

                title = f'Base for {label.strip()} ({project})'
                self.app.push_screen(Ask(title, self._default_base(project)), based)

            if len(projects) == 1:
                picked(projects[0][0])
            else:
                title = f'Which project is {label.strip()} for?'
                self.app.push_screen(Pick(title, projects), picked)

        self.app.push_screen(Ask('Name the new forge'), named)

    def _import_mbox(self) -> None:
        """n m: a forge jumpstarted from an mbox file (a thread saved from a list, or what
        `b4 send -o` wrote): its newest version is the first round, authorship kept, and
        the next round is the next version's first, as for u."""
        projects = self._projects()
        if not projects:
            self._status('No projects in the workspace')
            return
        service = self.service
        root = self._root()

        def given(text: Optional[str]) -> None:
            if not text or not text.strip():
                return
            path = pathlib.Path(text.strip()).expanduser()
            path = path if path.is_absolute() else root / path
            self._status(f'Reading {path}…')
            self._work(
                lambda: service.read_mbox(path),
                lambda found: picked_series(found, path),
                'forge-import',
            )

        def picked_series(series: Series, path: pathlib.Path) -> None:
            def picked(project: Optional[str]) -> None:
                if not project:
                    return

                def based(base: Optional[str]) -> None:
                    if base is None:
                        return
                    self._make(
                        project,
                        base.strip(),
                        lambda: service.create_from_upstream(
                            series.subject,
                            project,
                            base.strip(),
                            path,
                            series.revision,
                            series.message_id,
                        ),
                    )

                title = f'Base for {series.subject} v{series.revision} ({project})'
                self.app.push_screen(Ask(title, self._default_base(project)), based)

            if len(projects) == 1:
                picked(projects[0][0])
            else:
                title = f'Which project is {series.subject} for?'
                self.app.push_screen(Pick(title, projects), picked)

        self.app.push_screen(Ask('Import which mbox? (a path)'), given)

    def _make(self, project: str, base: str, make: Callable[[], Forge]) -> None:
        """Make a forge in a worker, its base checked first where *project* is checked
        out: a base git cannot resolve there is refused with why, nothing made."""
        checkout = self._project_checkout(project)

        def work() -> Forge:
            check_base(checkout, base)
            return make()

        self._work(work, self._made, 'forge-new')

    def _made(self, forge: Forge) -> None:
        """Made, the forge opens: its goal is the next thing to give."""
        self._reload(forge.id)
        self._open_forge(forge)

    def _upstream_choices(
        self, project: str = '', older: bool = False
    ) -> tuple[list[tuple[str, str]], list[Series]]:
        """Cached postings to continue or harvest (the maintainer's own first), the
        newest of each series; with *older*, each older version after it: the Pick's
        choices and their postings."""
        snapshot = getattr(self.app, 'snapshot', None)
        if snapshot is None:
            return [], []
        me = {a.lower() for a in snapshot.workspace.me}
        items = [
            i
            for i in snapshot.items
            if i.series.source and not i.series.discussion and (not project or i.project == project)
        ]
        items.sort(key=lambda i: (i.series.author_email.lower() not in me, i.series.subject))
        postings: list[Series] = []
        for i in items[:200]:
            postings.append(i.series)
            if older:
                postings += [s for s in reversed(i.older) if s.source]
        choices = []
        for n, s in enumerate(postings):
            who = s.author_name or s.author_email
            choices.append((str(n), f'{s.subject} v{s.revision} ({who})'))
        return choices, postings

    def _from_upstream(self) -> None:
        """n u: a forge that continues a series posted upstream (any author's: authorship
        stays theirs): its newest version harvested; the next round is the next
        version's first."""
        choices, items = self._upstream_choices()
        if not choices:
            self._status('No cached series to start from')
            return

        def picked(key: Optional[str]) -> None:
            if key is None:
                return
            series = items[int(key)]
            projects = self._projects()
            routed = next((i.project for i in self.app.snapshot.items if i.series is series), '')
            project = routed or (projects[0][0] if projects else '')

            def based(base: Optional[str]) -> None:
                if base is None:
                    return
                service = self.service
                self._make(
                    project,
                    base.strip(),
                    lambda: service.create_from_upstream(
                        series.subject,
                        project,
                        base.strip(),
                        pathlib.Path(series.source),
                        series.revision,
                        series.message_id,
                    ),
                )

            title = f'Base for {series.subject} ({project})'
            self.app.push_screen(Ask(title, self._default_base(project)), based)

        self.app.push_screen(Pick('Continue which upstream series?', choices), picked)

    def _edit(self, path: pathlib.Path, done: Callable[[], None]) -> None:
        edit = getattr(self.app, 'edit_in_terminal', None)
        if edit is None or self._open is None:
            self._status('No editor here')
            return
        if not edit(path, self._checkout(self._open)):
            self._status('Cannot run your editor here')
            return
        done()

    def action_edit_prompt(self) -> None:
        """e: the forge's prompt (its goal) in the editor."""
        forge = self._open
        if forge is None:
            return
        path = self.service.store.prompt_path(forge.id)
        if not path.exists():
            self.service.store.set_prompt(forge.id, '')
        service = self.service

        def saved() -> None:
            def refresh() -> None:
                service.refresh_goal(forge)

            self._work(refresh, lambda _none: self._goal_saved(), 'forge-goal')

        self._edit(path, saved)

    def _goal_saved(self) -> None:
        if self._rounds:
            self._status('Goal saved; sessions read it in forge/prompt.md')
        else:
            self._reopen(note='Goal saved; sessions read it in forge/prompt.md')

    def action_reply(self) -> None:
        """r: reply to the selected message in the editor (b4's quote); saved, it is posted
        to the local list. Never mailed."""
        forge, rnd, msgid = self._open, self._round(), self._selected_msgid()
        if forge is None or rnd is None or not msgid:
            return
        service = self.service
        try:
            start = service.quote(forge, rnd, msgid)
        except Exception as exc:
            self._status(f'Cannot reply: {exc}')
            return
        drafts = service.forge_dir(forge) / 'drafts'
        drafts.mkdir(parents=True, exist_ok=True)
        path = drafts / f'{rnd.key}-reply.txt'
        path.write_text(start)

        def edited() -> None:
            text = path.read_text()
            path.unlink(missing_ok=True)
            if text.strip() == start.strip() or not text.strip():
                self._status('Nothing written: no reply posted')
                return

            listed = getattr(self.app, 'bench_companions', None)
            mailer = getattr(self.app, 'mail_session', None)
            companions = listed(forge.workbench) if listed is not None and mailer else []

            def post() -> str:
                # b4's own trimming of a hand-edited reply (its scaffolding and trailing
                # quote); no signature on the local list.
                from b4.review import _trim_quoted_reply

                me = self._me(forge)
                sent = service.post_reply(forge, rnd, msgid, me, _trim_quoted_reply(text))
                if companions:  # The thread as it is now, then word to each companion.
                    service.write_context(forge, rnd, sent)
                    for sid in companions:
                        mailer(sid, companion_mail(rnd, sent))
                return sent

            def posted(_sent: str) -> None:
                told = f'; {len(companions)} companion(s) answer it' if companions else ''
                self._reopen(rnd.key, f'Reply posted to {rnd.label}{told}')

            self._work(post, posted, 'forge-reply')

        self._edit(path, edited)

    def _range_diff(self) -> None:
        """v d: the selected round against the one before it, as git range-diff shows it."""
        forge, rnd = self._open, self._round()
        if forge is None or rnd is None:
            return
        service = self.service

        def show(text: str) -> None:
            content = self.query_one('#forge-content', TextPane)
            content.set_blocks([Block(f'range-diff of {rnd.label}\n'), Block(text or '(same)')])
            self._status('d: range-diff; select a message to read it again')

        self._work(lambda: service.range_diff(forge, rnd), show, 'forge-diff')

    def _timeline(self) -> None:
        """v t: where the work started and where it is: every round, its replies and
        findings, and what changed since the round before."""
        forge = self._open
        if forge is None:
            return
        service = self.service

        def show(text: str) -> None:
            self.query_one('#forge-content', TextPane).set_text(text)
            self._status('t: the timeline; select a message to read it again')

        self._work(lambda: service.summary(forge), show, 'forge-timeline')

    def _look(self) -> None:
        """v o: the series at the selected patch, checked out in the forge's space, in a
        tmux window: the files in context."""
        forge, rnd, (row, _reply) = self._open, self._round(), self._message()
        post = find_post(self._thread, row.key) if row is not None and self._thread else None
        if forge is None or rnd is None or post is None or post.counter < 1:
            self._status('v o opens the series at a patch: select a patch')
            return
        opener = getattr(self.app, 'open_window', None)
        service, part = self.service, post.counter

        def opened(path: pathlib.Path) -> None:
            name = f'{forge.id} {rnd.key} {part}'
            self._status(opener(path, name) if opener is not None else f'Checked out at {path}')

        checkout = self._checkout(forge)

        def look() -> pathlib.Path:
            service.snapshot(forge, rnd, checkout)  # An upstream round: applied first.
            return service.look(forge, rnd, part)

        self._work(look, opened, 'forge-look')

    def action_accept(self) -> None:
        """A: accept the selected plan (an Acked-by reply): sessions follow its design."""
        forge, rnd = self._open, self._round()
        if forge is None or rnd is None:
            return
        if rnd.kind != 'plan':
            self._status('A accepts a plan: select a plan round')
            return
        service = self.service

        def accepted(_sent: str) -> None:
            self._reopen(rnd.key, f'Accepted {rnd.label}')

        self._work(
            lambda: service.accept_plan(forge, rnd, self._me(forge)), accepted, 'forge-accept'
        )

    def _companion(self) -> None:
        """s c: a read-only companion beside the app, on the round (and message) selected:
        each reply posted on the forge (r) reaches it, and it answers on the thread; or ask
        it in its pane."""
        self.action_launch('forge-companion', focus=True)

    def action_launch(self, profile: str, focus: bool = False) -> None:
        """A forge session: the launch dialog with its task preselected, over the forge's
        workbench (its context brought up to date first; *focus*: on the selected round
        and message). Nothing starts until the dialog is confirmed."""
        forge = self._open
        rnd = self._round() if focus else None
        msgid = self._selected_msgid() if focus else ''
        launch = getattr(self.app, 'launch_workbench', None)
        if forge is None:
            return
        if launch is None or getattr(self.app, '_sessions', None) is None:
            self._status('Agent sessions are not available here')
            return
        service = self.service

        checkout = self._checkout(forge)

        def prepare() -> Any:
            if checkout.is_dir():
                service.seed(forge, checkout)  # After an upstream round: vN, its trailers.
            service.write_context(forge, rnd, msgid)
            bench = service.benches.get(forge.workbench)
            if bench is None:
                raise ValueError(f'its workbench {forge.workbench} is gone')
            return bench

        self._work(
            prepare,
            lambda bench: launch(bench, only=profile, follow=forge.project),
            'forge-launch',
        )

    def _sashiko_missing(self) -> str:
        binary = self.service.sashiko_binary
        if binary is None or not binary.is_file():
            return 'needs a build of deps/sashiko (cargo build --release)'
        return ''

    def _review(self) -> None:
        """s r: which review: review-prompts, kernel-style (sessions), or Sashiko (a local
        job, pi answering its model turns)."""
        if self._open is None:
            return
        missing = self._sashiko_missing()
        choices = [
            (pid, label + (f' — {missing}' if pid == 'forge-sashiko' and missing else ''), key)
            for pid, label, key in REVIEWS
        ]

        def picked(pid: Optional[str]) -> None:
            if pid is None:
                return
            if pid == 'forge-sashiko':
                self._sashiko()
                return
            self.action_launch(pid)

        self.app.push_screen(Menu('Which review?', choices), picked)

    def _sashiko(self) -> None:
        """Sashiko on the selected round: the model picked from pi's list, then a job of
        its own, detached (minutes to hours; it runs on if the app quits): its progress on
        the rounds list, its findings and inline review landing on the round when it
        ends. While one runs, this offers to stop it."""
        from .app import Confirm

        forge = self._open
        if forge is None:
            return
        service = self.service

        def running(job: dict[str, Any]) -> None:
            if not job:
                self._sashiko_start()
                return

            def answered(yes: Optional[bool]) -> None:
                if yes:
                    self._work(
                        lambda: service.stop_sashiko(forge), self._sashiko_stopped, 'forge-sashiko'
                    )

            label = job.get('label', 'a round')
            prompt = f'Sashiko is reviewing {label}. Stop it? [y|N]'
            self.app.push_screen(Confirm(prompt), answered)

        self._work(lambda: service.sashiko_job(forge), running, 'forge-sashiko')

    def _sashiko_stopped(self, said: str) -> None:
        self.show_sashiko(self._open.id if self._open is not None else '', '')
        self._status(said)

    def _sashiko_start(self) -> None:
        forge, rnd = self._open, self._round()
        missing = self._sashiko_missing()
        if missing:
            self._status(f'Sashiko: {missing}')
            return
        if forge is None or rnd is None or rnd.kind == 'plan':
            self._status('Sashiko reviews a series (a vN round): select one, not a plan')
            return
        hub = getattr(self.app, '_sessions', None)
        presets = self.workspace.models if self.workspace is not None else ()

        by_key: dict[str, str] = {}

        def models() -> list[tuple[str, str]]:
            found = hub.models(presets) if hub is not None else []
            usable = [m for m in found if m.available and m.kind != 'dry-run']
            by_key.update({m.id: m.model for m in usable} or {'default': ''})
            return [(m.id, m.label) for m in usable] or [('default', "pi's default model")]

        def chosen(key: Optional[str]) -> None:
            if key is None:
                return
            model = by_key.get(key, '')
            service = self.service

            def started(said: str) -> None:
                self.show_sashiko(forge.id, 'starting')
                self._status(
                    f'{said}: its progress shows on the rounds list; it runs on if you quit'
                )

            self._status(f'Checking out {rnd.label} for Sashiko…')
            checkout = self._checkout(forge)
            self._work(
                lambda: service.start_sashiko(forge, rnd, model, project=checkout),
                started,
                'forge-sashiko',
            )

        def offer(choices: list[tuple[str, str]]) -> None:
            self.app.push_screen(Pick(f'Sashiko on {rnd.label}: which model?', choices), chosen)

        self._work(models, offer, 'forge-models')

    def _fold(self) -> None:
        """u f: the selected round's series as the project's prep branch for this forge
        (made, or its commits replaced: the next send is its next revision); sent from
        Branches."""
        from .app import Confirm

        forge, rnd = self._open, self._round()
        if forge is None or rnd is None:
            return
        if rnd.kind != 'series':
            self._status('u f folds a series round: select one')
            return
        service = self.service
        repo = self._checkout(forge)
        worktree = self._root() / 'var' / 'branches' / forge.project / forge.id

        def folded(branch: str) -> None:
            self._status(f'{rnd.label} is on {branch}: send it from Branches (F4)')

        def answered(yes: Optional[bool]) -> None:
            if yes:
                self._work(lambda: service.fold(forge, rnd, repo, worktree), folded, 'forge-fold')

        prompt = (
            f'Fold {rnd.label} into the prep branch b4/{forge.id} in {forge.project}? '
            'It is made, or its commits are replaced. [y|N]'
        )
        self.app.push_screen(Confirm(prompt), answered)

    def _harvest(self) -> None:
        """u h: an upstream posting of this series (its thread from the mail cache, with the
        list's replies) as that version's upstream round: the next cycle's input."""
        forge = self._open
        if forge is None:
            return
        choices, items = self._upstream_choices(forge.project, older=True)
        if not choices:
            self._status(f'No cached series in {forge.project} to harvest')
            return
        service = self.service

        def harvested(rnd: Round) -> None:
            self._reopen(rnd.key, f'Harvested {rnd.label}')

        def picked(key: Optional[str]) -> None:
            if key is None:
                return
            series = items[int(key)]
            source = pathlib.Path(series.source)
            self._work(
                lambda: service.harvest(forge, source, series.revision, series.message_id),
                harvested,
                'forge-harvest',
            )

        self.app.push_screen(Pick(f'Harvest which posting into {forge.label}?', choices), picked)

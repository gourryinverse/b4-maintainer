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

import pathlib
import subprocess
from typing import Any, Callable, Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, OptionList, Static
from textual.widgets.option_list import Option

from .branchscreen import Ask, Pick
from .forge import Forge, ForgeService, Round
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

# The reviews the review key offers: (profile, label). Sashiko is a job, not a session.
REVIEWS = (
    ('forge-review-prompts', 'review-prompts (kernel review protocol)'),
    ('forge-kernel-style', 'kernel-style (style, structure, changelogs)'),
    ('forge-sashiko', 'Sashiko (local pipeline, pi as its model)'),
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
        Binding('n', 'new', 'new forge'),
        Binding('u', 'from_upstream', 'from upstream'),
        # A forge open.
        Binding('r', 'reply', 'reply'),
        Binding('d', 'range_diff', 'range-diff'),
        Binding('t', 'timeline', 'timeline'),
        Binding('o', 'look', 'open at patch'),
        Binding('A', 'accept', 'accept plan'),
        Binding('P', "launch('forge-plan')", 'design'),
        Binding('I', "launch('forge-implement')", 'implement'),
        Binding('s', "launch('forge-rework')", 'submit round'),
        Binding('C', 'companion', 'companion'),
        Binding('R', 'review', 'review'),
        Binding('T', "launch('forge-test')", 'test'),
        Binding('f', 'fold', 'fold to prep'),
        Binding('h', 'harvest', 'harvest upstream'),
        Binding('e', 'edit_prompt', 'goal'),
        Binding('z', 'toggle_quotes', 'quotes'),
    ]
    _CLOSED = frozenset({'new', 'from_upstream'})
    _OPEN = frozenset(
        {'reply', 'range_diff', 'accept', 'launch', 'review', 'fold', 'harvest'}
        | {'edit_prompt', 'timeline', 'look', 'companion'}
    )

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
        self._note = ''  # What the status line says after it.
        self._goal = ''  # The open forge's goal (prompt), as read with its rounds.
        self._expand_quotes = False  # z: long quotes open in every message.

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
        """The open forge's rounds read again (the app collected new ones), keeping the
        selected round unless a newer one arrived."""
        self._reopen('', note)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> Optional[bool]:
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
        project = self.workspace.project(forge.project) if self.workspace is not None else None
        path = project.path if project is not None else f'projects/{forge.project}'
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

    def _reload(self, select: str = '') -> None:
        service = self.service

        def read() -> tuple[list[tuple[Forge, list[Round]]], list[str]]:
            forges, problems = service.store.list()
            return [(f, service.rounds(f)) for f in forges], problems

        self._work(read, lambda found: self._fill(*found, select), 'forge-list')

    def _fill(
        self, forges: list[tuple[Forge, list[Round]]], problems: list[str], select: str
    ) -> None:
        self._forges = forges
        listing = self.query_one('#forge-list', OptionList)
        current = select or self._highlighted_id()
        listing.clear_options()
        listing.add_options([Option(forge_label(f, r), id=f.id) for f, r in forges])
        if forges:
            ids = [f.id for f, _r in forges]
            listing.highlighted = ids.index(current) if current in ids else 0
        said = '; '.join(problems)
        if not forges:
            said = said or 'n makes a forge; u starts one from an upstream series'
        elif not said:
            said = 'Enter opens a forge; n makes one; u starts one from upstream'
        self._status(said)

    def _highlighted_id(self) -> str:
        option = self.query_one('#forge-list', OptionList).highlighted_option
        return (option.id or '') if option is not None else ''

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

    def _reopen(self, keep: str = '', note: str = '', collect: bool = False) -> None:
        """Read the open forge's rounds again; select *keep* (a round key), else the
        newest; *note* stays in the status line. *collect*: first take what its sessions
        left since (opening the forge, coming back to the tab)."""
        forge = self._open
        if forge is None:
            return
        self._keep_round = keep
        self._note = note
        service = self.service

        def read() -> list[Round]:
            if collect:
                said = service.collect(forge)
                if said:
                    self._note = '; '.join(said)
                    self._keep_round = ''  # Show what arrived.
            self._goal = service.store.prompt(forge.id)
            return service.rounds(forge)

        self._work(read, self._fill_rounds, 'forge-rounds')

    def _fill_rounds(self, rounds: list[Round]) -> None:
        self._rounds = rounds
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
            self.query_one('#forge-tree', OptionList).focus()
            self._status(self._note or 'r replies; R reviews; s submits the round')
        else:
            self.query_one('#forge-tree', OptionList).clear_options()
            goal = self._goal.strip()
            said = (
                f'Goal:\n\n{goal}\n\ne edits it in your editor; running sessions read it.'
                if goal
                else 'No goal yet: e writes it in your editor, or P launches a design session '
                'that works it out with you.'
            )
            self.query_one('#forge-content', TextPane).set_text(
                f'Nothing posted yet.\n\n{said}\n\nP designs the work, I implements it; what '
                'a session leaves arrives here when it finishes.'
            )
            listing.focus()
            self._status(self._note or 'Nothing posted yet; escape closes the forge')

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
        self._rows = tree_rows(thread, self._item, set(post_keys(thread)), bots=bots)
        self.call_after_refresh(self._fill_tree)  # Shown first: rows fit its width.

    def _fill_tree(self) -> None:
        tree = self.query_one('#forge-tree', OptionList)
        with self.prevent(OptionList.OptionHighlighted):
            tree.clear_options()
            width = tree_width(tree)
            tree.add_options([Option(tree_prompt(r, width)) for r in self._rows])
            if self._rows:
                tree.highlighted = 0
        self._show_message()

    def _quote_lines(self) -> int:
        return self.workspace.quote_lines if self.workspace is not None else QUOTE_LINES

    def action_toggle_quotes(self) -> None:
        """z: every long quote opened, or folded again (a click toggles one); later
        messages open the same way."""
        self._expand_quotes = not self._expand_quotes
        self.query_one('#forge-content', TextPane).set_folds(self._expand_quotes)

    def _message(self) -> tuple[Optional[TreeRow], Any]:
        index = self.query_one('#forge-tree', OptionList).highlighted
        if self._thread is None or index is None or index >= len(self._rows):
            return None, None
        row = self._rows[index]
        replies = thread_replies(self._thread, row.key) if row.node else ()
        reply = replies[row.node - 1] if row.node and row.node <= len(replies) else None
        return row, reply

    def _show_message(self) -> None:
        content = self.query_one('#forge-content', TextPane)
        row, reply = self._message()
        self.refresh_bindings()  # z shows only on a message with a long quote.
        if row is None or self._thread is None or self._item is None:
            content.set_text('')
            return
        if reply is not None:
            blocks = message_blocks(reply, self._quote_lines())
            content.set_blocks(blocks, open_folds=self._expand_quotes)
        else:
            content.set_blocks(root_blocks(self._thread, row.key, self._item))

    def _selected_msgid(self) -> str:
        row, reply = self._message()
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
        """n: a forge, named, for one project, from a base; its workbench made with it.
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
                    self._work(
                        lambda: service.create(label.strip(), project, base.strip(), ''),
                        self._made,
                        'forge-new',
                    )

                title = f'Base for {label.strip()} ({project})'
                self.app.push_screen(Ask(title, self._default_base(project)), based)

            if len(projects) == 1:
                picked(projects[0][0])
            else:
                title = f'Which project is {label.strip()} for?'
                self.app.push_screen(Pick(title, projects), picked)

        self.app.push_screen(Ask('Name the new forge'), named)

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

    def action_from_upstream(self) -> None:
        """u: a forge that continues a series posted upstream (any author's: authorship
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
                self._work(
                    lambda: service.create_from_upstream(
                        series.subject,
                        project,
                        base.strip(),
                        pathlib.Path(series.source),
                        series.revision,
                        series.message_id,
                    ),
                    self._made,
                    'forge-new',
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

            def post() -> str:
                # b4's own trimming of a hand-edited reply (its scaffolding and trailing
                # quote); no signature on the local list.
                from b4.review import _trim_quoted_reply

                me = self._me(forge)
                return service.post_reply(forge, rnd, msgid, me, _trim_quoted_reply(text))

            def posted(_sent: str) -> None:
                self._reopen(rnd.key, f'Reply posted to {rnd.label}')

            self._work(post, posted, 'forge-reply')

        self._edit(path, edited)

    def action_range_diff(self) -> None:
        """d: the selected round against the one before it, as git range-diff shows it."""
        forge, rnd = self._open, self._round()
        if forge is None or rnd is None:
            return
        service = self.service

        def show(text: str) -> None:
            content = self.query_one('#forge-content', TextPane)
            content.set_blocks([Block(f'range-diff of {rnd.label}\n'), Block(text or '(same)')])
            self._status('d: range-diff; select a message to read it again')

        self._work(lambda: service.range_diff(forge, rnd), show, 'forge-diff')

    def action_timeline(self) -> None:
        """t: where the work started and where it is: every round, its replies and
        findings, and what changed since the round before."""
        forge = self._open
        if forge is None:
            return
        service = self.service

        def show(text: str) -> None:
            self.query_one('#forge-content', TextPane).set_text(text)
            self._status('t: the timeline; select a message to read it again')

        self._work(lambda: service.summary(forge), show, 'forge-timeline')

    def action_look(self) -> None:
        """o: the series at the selected patch, checked out in the forge's space, in a
        tmux window: the files in context."""
        forge, rnd, (row, _reply) = self._open, self._round(), self._message()
        post = find_post(self._thread, row.key) if row is not None and self._thread else None
        if forge is None or rnd is None or post is None or post.counter < 1:
            self._status('o opens the series at a patch: select a patch')
            return
        opener = getattr(self.app, 'open_window', None)
        service, part = self.service, post.counter

        def opened(path: pathlib.Path) -> None:
            name = f'{forge.id} {rnd.key} {part}'
            self._status(opener(path, name) if opener is not None else f'Checked out at {path}')

        self._work(lambda: service.look(forge, rnd, part), opened, 'forge-look')

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

    def action_companion(self) -> None:
        """C: a short-lived, read-only companion beside the app, on the round (and
        message) selected: ask it questions while reviewing."""
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

    def action_review(self) -> None:
        """R: which review: review-prompts, kernel-style (sessions), or Sashiko (a local
        job, pi answering its model turns)."""
        if self._open is None:
            return
        missing = self._sashiko_missing()
        choices = [
            (pid, label + (f' — {missing}' if pid == 'forge-sashiko' and missing else ''))
            for pid, label in REVIEWS
        ]

        def picked(pid: Optional[str]) -> None:
            if pid is None:
                return
            if pid == 'forge-sashiko':
                self._sashiko()
                return
            self.action_launch(pid)

        self.app.push_screen(Pick('Which review?', choices), picked)

    def _sashiko(self) -> None:
        """Sashiko on the selected round: the model picked from pi's list, then a job in
        the background (minutes); its findings and inline review land on the round."""
        forge, rnd = self._open, self._round()
        missing = self._sashiko_missing()
        if missing:
            self._status(f'Sashiko: {missing}')
            return
        if forge is None or rnd is None or rnd.kind != 'series':
            self._status('Sashiko reviews a series round: select one')
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

            def done(said: str) -> None:
                self._reopen(rnd.key, said)

            self._status(f'Sashiko is reviewing {rnd.label} (pi: {model or "default"})…')
            self._work(lambda: service.sashiko(forge, rnd, model), done, 'forge-sashiko')

        def offer(choices: list[tuple[str, str]]) -> None:
            self.app.push_screen(Pick(f'Sashiko on {rnd.label}: which model?', choices), chosen)

        self._work(models, offer, 'forge-models')

    def action_fold(self) -> None:
        """f: the selected round's series as the project's prep branch for this forge
        (made, or its commits replaced: the next send is its next revision); sent from
        Branches."""
        from .app import Confirm

        forge, rnd = self._open, self._round()
        if forge is None or rnd is None:
            return
        if rnd.kind != 'series':
            self._status('f folds a series round: select one')
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

    def action_harvest(self) -> None:
        """h: an upstream posting of this series (its thread from the mail cache, with the
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

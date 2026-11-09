# SPDX-License-Identifier: GPL-2.0-or-later
"""Settings (F6): which key signs mail for every project, and setting one up there (an
existing PGP key from gpg, a new patatt ed25519 key) or stopping signing; each after a
confirmation, in the global git configuration (a checkout's own key still overrides).

Then each project's tracked upstream branches: tracking one (its remote fetched into the
checkout, the ref written to workspace.toml), changing how membership in it counts, and
untracking it (the remote goes once nothing else uses it).
"""

import datetime
import pathlib
from typing import TYPE_CHECKING, Any, Callable, Optional, Sequence

from textual.app import ComposeResult
from textual.binding import Binding
from textual.markup import escape
from textual.screen import Screen
from textual.widgets import Footer, OptionList, Static
from textual.widgets.option_list import Option

from .adapters import signing
from .branchscreen import Ask, Pick
from .model import Authority
from .widgets import TabBar
from .workspace import Ref, make_ref

if TYPE_CHECKING:
    from .upstreams import Upstream, UpstreamService

ACTIONS = (
    ('pgp', 'Sign with an existing PGP key (from gpg) for every project'),
    ('patatt', 'Make a new patatt ed25519 key and sign with it for every project'),
    ('stop', 'Stop signing for every project (messages go unsigned)'),
)

AUTHORITIES = (
    ('mainline', 'mainline: where series land (one per project; ends review by default)'),
    ('acceptance', 'acceptance: a maintainer took the series (e.g. mm-stable, a for-next)'),
    ('preliminary', 'preliminary: queued for testing, may still change (e.g. mm-unstable)'),
)
UPSTREAM_HELP = (
    'A series in a tracked branch shows its label in the list and its tree evidence. '
    'Authority ranks the branches. A view lists what a branch holds, under Upstream '
    'branches, and takes those series out of the list views. Ends review: a series '
    'fully in that branch leaves every queue. Enter changes a branch; d untracks it.'
)


class SettingsScreen(Screen[None]):
    DEFAULT_CSS = """
    SettingsScreen #settings-status {
        height: auto; padding: 0 1; border-top: solid $panel-lighten-2;
        border-title-color: $text-muted;
    }
    SettingsScreen #settings-actions {
        height: auto; border: none; border-top: solid $panel-lighten-2;
        border-title-color: $text-muted;
    }
    SettingsScreen #settings-note { height: auto; padding: 0 1; }
    SettingsScreen #settings-upstreams {
        height: auto; max-height: 20; border: none; border-top: solid $panel-lighten-2;
        border-title-color: $text-muted;
    }
    SettingsScreen #settings-upstream-help { height: auto; padding: 0 1; color: $text-muted; }
    """
    BINDINGS = [
        Binding('escape', 'back', 'back'),
        Binding('d', 'untrack', 'untrack'),
    ]

    def __init__(
        self,
        checkouts: Callable[[], Sequence[tuple[str, pathlib.Path]]],
        upstreams: Optional['UpstreamService'] = None,
    ) -> None:
        super().__init__()
        self._checkouts = checkouts  # (project, checkout) of each project, for their own keys.
        self._upstreams = upstreams
        self._listed: dict[str, 'Upstream'] = {}  # Option id: tracked branch.
        self._upstreams_text = ''
        self._status_text = ''

    def compose(self) -> ComposeResult:
        yield TabBar('settings')
        yield Static('', id='settings-status', markup=False)
        yield OptionList(*(Option(label, id=key) for key, label in ACTIONS), id='settings-actions')
        yield Static('', id='settings-note', markup=False)
        if self._upstreams is not None:
            yield OptionList(id='settings-upstreams')
            yield Static(UPSTREAM_HELP, id='settings-upstream-help', markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one('#settings-status', Static).border_title = 'Signing'
        self.query_one('#settings-actions', OptionList).border_title = 'Set up'
        self.query_one('#settings-actions', OptionList).focus()
        if self._upstreams is not None:
            self.query_one('#settings-upstreams', OptionList).border_title = 'Upstream branches'
        self._reload()

    def on_screen_resume(self) -> None:
        self._reload()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> Optional[bool]:
        if action == 'untrack':
            return self._highlighted_upstream() is not None
        return True

    def _reload(self, note: str = '') -> None:
        service = self._upstreams

        def work() -> None:
            lines = signing.signing_status(self._checkouts())
            self.app.call_from_thread(self._show, lines, note)
            if service is not None:
                try:
                    listed: Optional[list['Upstream']] = service.listing()
                except Exception as exc:  # A broken workspace.toml: say so, keep going.
                    listed, note2 = None, f'Upstream branches: {exc}'
                    self.app.call_from_thread(self.show_notice, note2)
                if listed is not None:
                    self.app.call_from_thread(self._show_upstreams, listed)

        self.run_worker(work, thread=True, group='settings', exclusive=True)

    def _show(self, lines: list[str], note: str) -> None:
        self._status_text = '\n'.join([*lines, *([note] if note else [])])
        self.query_one('#settings-status', Static).update('\n'.join(lines))
        self.query_one('#settings-note', Static).update(note)

    def status_text(self) -> str:
        return self._status_text

    def upstreams_text(self) -> str:
        return self._upstreams_text

    def _show_upstreams(self, listed: list['Upstream']) -> None:
        widget = self.query_one('#settings-upstreams', OptionList)
        highlighted = widget.highlighted_option.id if widget.highlighted_option else None
        options, lines, self._listed = [], [], {}
        projects = list(dict.fromkeys(u.project for u in listed))
        for project in projects:
            options.append(Option(f'[b]{project}[/]', disabled=True))
            lines.append(project)
            for upstream in (u for u in listed if u.project == project):
                key = f'ref:{project}:{upstream.ref.name}'
                self._listed[key] = upstream
                text = f'  {upstream.ref.label}  {upstream.ref.name:<28} {_counts(upstream)}'
                options.append(Option(escape(text), id=key))
                lines.append(text)
            text = '  + Track a branch…'
            options.append(Option(escape(text), id=f'add:{project}'))
            lines.append(text)
        widget.clear_options()
        widget.add_options(options)
        if highlighted and highlighted in [o.id for o in options]:
            widget.highlighted = widget.get_option_index(highlighted)
        self._upstreams_text = '\n'.join(lines)
        self.refresh_bindings()

    def select_upstream(self, key: str) -> None:
        widget = self.query_one('#settings-upstreams', OptionList)
        widget.focus()
        widget.highlighted = widget.get_option_index(key)
        self.refresh_bindings()

    def _highlighted_upstream(self) -> Optional[str]:
        if self._upstreams is None:
            return None
        widget = self.query_one('#settings-upstreams', OptionList)
        option = widget.highlighted_option
        if not widget.has_focus or option is None or option.id not in self._listed:
            return None
        return option.id

    def show_notice(self, text: str) -> None:
        """The app's notice (e.g. F5's progress)."""
        self.query_one('#settings-note', Static).update(text)

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        event.stop()  # The app's own lists handle theirs.
        self.refresh_bindings()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option.id:
            self.choose(event.option.id)

    def choose(self, action: str) -> None:
        if action.startswith('add:'):
            self._track(action.removeprefix('add:'))
        elif action in self._listed:
            self._change(self._listed[action])
        elif action == 'pgp':
            self._pgp()
        elif action == 'patatt':
            self._patatt()
        elif action == 'stop':
            self._confirm(
                'Stop signing for every project? patatt.signingkey goes from your global git '
                "configuration; messages go unsigned (a checkout's own key stays). [y|N]",
                signing.stop_signing,
                'Signing stopped',
            )

    def _pgp(self) -> None:
        def work() -> None:
            keys = signing.gpg_secret_keys()
            self.app.call_from_thread(offer, keys)

        def offer(keys: list[tuple[str, str]]) -> None:
            if not keys:
                self._reload('gpg has no secret keys: make one (gpg --full-generate-key) first')
                return
            choices = [(fpr, f'{uid}  {fpr[-16:]}') for fpr, uid in keys]
            self.app.push_screen(Pick('Sign with which PGP key?', choices), picked)

        def picked(fpr: Optional[str]) -> None:
            if fpr:
                self._confirm(
                    f'Sign mail with PGP key {fpr} for every project? Sets patatt.signingkey '
                    'in your global git configuration. [y|N]',
                    lambda: signing.use_pgp_key(fpr),
                    f'Signing with openpgp:{fpr}',
                )

        self.run_worker(work, thread=True, group='settings-act')

    def _patatt(self) -> None:
        today = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d')

        def named(name: Optional[str]) -> None:
            if not name:
                return
            self._confirm(
                f'Make a new ed25519 key {name} with patatt and sign with it for every project? '
                'Sets patatt.signingkey in your global git configuration; publish its public '
                "key (in patatt's data directory) so others can verify. [y|N]",
                lambda: signing.new_patatt_key(name),
                f'Signing with ed25519:{name}',
            )

        self.app.push_screen(Ask('Name the new key', today), named)

    def _confirm(self, prompt: str, do: Callable[[], Any], done: str) -> None:
        from .app import Confirm

        def answered(yes: Optional[bool]) -> None:
            if not yes:
                return

            def work() -> None:
                try:
                    do()
                    note = done
                except Exception as exc:
                    note = f'Not changed: {exc}'
                self.app.call_from_thread(self._reload, note)

            self.run_worker(work, thread=True, group='settings-act')

        self.app.push_screen(Confirm(prompt), answered)

    # Upstream branches.

    def _upstream_work(self, do: Callable[[], str]) -> None:
        """Run a change off the UI thread; then the listing, this note, and the app reload."""

        def work() -> None:
            try:
                note = do()
            except Exception as exc:
                note = f'Not changed: {exc}'
            else:
                reload = getattr(self.app, 'reload_snapshot', None)
                if reload is not None:
                    self.app.call_from_thread(reload)
            self.app.call_from_thread(self._reload, note)

        self.run_worker(work, thread=True, group='settings-act')

    def _track(self, project: str) -> None:
        service = self._upstreams
        if service is None:
            return
        chosen: dict[str, Any] = {}

        def listed_remotes() -> None:
            try:
                known = service.remotes(project)
            except Exception as exc:
                self.app.call_from_thread(self._reload, f'Cannot list remotes: {exc}')
                return
            self.app.call_from_thread(offer_remote, known)

        def offer_remote(known: dict[str, str]) -> None:
            chosen['known'] = known
            choices = [(name, f'{name}  {url}') for name, url in known.items()]
            choices.append(('new', 'A new remote, by its URL…'))
            self.app.push_screen(
                Pick(f'Track a branch for {project}: from which remote?', choices), remote_picked
            )

        def remote_picked(remote: Optional[str]) -> None:
            if not remote:
                return
            if remote == 'new':
                self.app.push_screen(Ask('URL of the repository'), url_given)
                return
            chosen.update(remote=remote, url=chosen['known'][remote])
            self.run_worker(lambda: list_heads(remote), thread=True, group='settings-act')

        def url_given(url: Optional[str]) -> None:
            if not url:
                return
            chosen['url'] = url
            self.app.push_screen(Ask('Name the remote', _remote_name(url)), named)

        def named(name: Optional[str]) -> None:
            if not name:
                return
            if name in chosen['known']:
                self._reload(f'A remote {name} exists: pick it instead')
                return
            chosen['remote'] = name
            self.run_worker(lambda: list_heads(chosen['url']), thread=True, group='settings-act')

        def list_heads(where: str) -> None:
            self.app.call_from_thread(self.show_notice, f'Listing the branches of {where}…')
            try:
                branches = service.heads(project, where)
            except Exception as exc:
                self.app.call_from_thread(self._reload, f'Cannot list branches: {exc}')
                return
            self.app.call_from_thread(offer_branch, branches)

        def offer_branch(branches: list[str]) -> None:
            self.show_notice('')
            if not branches:
                self._reload('That repository has no branches')
                return
            self.app.push_screen(
                Pick(f'Which branch of {chosen["remote"]}?', [(b, b) for b in branches]),
                branch_picked,
            )

        def branch_picked(branch: Optional[str]) -> None:
            if not branch:
                return
            chosen['branch'] = branch
            name = f'{chosen["remote"]}/{branch}'
            taken = {u.ref.label for u in self._listed.values() if u.project == project}
            self._ask_ref(name, taken, None, made)

        def made(ref: Ref) -> None:
            remote, url, branch = chosen['remote'], chosen['url'], chosen['branch']
            self._confirm_upstream(
                f'Track {ref.name} for {project} ({_counts_ref(ref)})? Fetches {branch} from '
                f'{url} into the checkout (remote {remote}) and adds it to workspace.toml. [y|N]',
                lambda: service.add(project, remote, url, branch, ref),
            )

        self.run_worker(listed_remotes, thread=True, group='settings-act')

    def _ask_ref(
        self, name: str, taken: set[str], current: Optional[Ref], done: Callable[[Ref], None]
    ) -> None:
        """Label, authority, view, ends review for *name*, then *done*(the ref)."""
        answers: dict[str, Any] = {}
        default = next(
            (
                c
                for c in name.split('/')[-1] + 'abcdefghijklmnopqrstuvwxyz'
                if c.isalnum() and c not in taken
            ),
            '',
        )

        def labelled(label: Optional[str]) -> None:
            if not label:
                return
            if len(label) != 1 or label in taken:
                self._reload(
                    f'The label must be one character, not one in use ({" ".join(sorted(taken))})'
                )
                return
            answers['label'] = label
            self.app.push_screen(Pick(f'Authority of {name}', AUTHORITIES), ranked)

        def ranked(authority: Optional[str]) -> None:
            if not authority:
                return
            answers['authority'] = Authority[authority.upper()]
            self._ask_view(name, viewed)

        def viewed(view: Optional[str]) -> None:
            if view is None:
                return
            answers['view'] = view
            self._ask_ends(name, answers['authority'], ended)

        def ended(ends: Optional[bool]) -> None:
            if ends is None:
                return
            done(make_ref(answers['label'], name, answers['authority'], answers['view'], ends))

        self.app.push_screen(
            Ask(f'Label for {name} (one character, shown in the list)', default), labelled
        )

    def _ask_view(self, name: str, done: Callable[[Optional[str]], None]) -> None:
        def picked(how: Optional[str]) -> None:
            if how == 'none':
                done('')
            elif how == 'named':
                self.app.push_screen(Ask(f'Name the view of {name}', name.split('/')[-1]), done)
            else:
                done(None)

        choices = [
            (
                'named',
                'A view of its own, under Upstream branches (its series leave the list views)',
            ),
            ('none', 'No view: its series stay in the list views, marked with its label'),
        ]
        self.app.push_screen(Pick(f'A sidebar view for {name}?', choices), picked)

    def _ask_ends(
        self, name: str, authority: Authority, done: Callable[[Optional[bool]], None]
    ) -> None:
        def picked(answer: Optional[str]) -> None:
            done(None if answer is None else answer == 'yes')

        choices = [
            ('no', 'No: series in it stay in the queues'),
            ('yes', 'Yes: a series fully in it leaves every queue (as merged)'),
        ]
        if authority == Authority.MAINLINE:
            choices.reverse()
        self.app.push_screen(Pick(f'Does {name} end review?', choices), picked)

    def _change(self, upstream: 'Upstream') -> None:
        service, ref = self._upstreams, upstream.ref
        if service is None:
            return
        taken = {u.ref.label for u in self._listed.values() if u.project == upstream.project} - {
            ref.label
        }

        def save(new: Ref) -> None:
            self._upstream_work(lambda: service.update(upstream.project, ref.name, new))

        def field(which: Optional[str]) -> None:
            if which == 'label':

                def labelled(label: Optional[str]) -> None:
                    if label and len(label) == 1 and label not in taken:
                        save(make_ref(label, ref.name, ref.authority, ref.view, ref.ends_review))
                    elif label:
                        self._reload('The label must be one character, not one in use')

                self.app.push_screen(Ask(f'Label for {ref.name}', ref.label), labelled)
            elif which == 'authority':

                def ranked(authority: Optional[str]) -> None:
                    if authority:
                        save(
                            make_ref(
                                ref.label,
                                ref.name,
                                Authority[authority.upper()],
                                ref.view,
                                ref.ends_review,
                            )
                        )

                self.app.push_screen(Pick(f'Authority of {ref.name}', AUTHORITIES), ranked)
            elif which == 'view':

                def viewed(view: Optional[str]) -> None:
                    if view is not None:
                        save(make_ref(ref.label, ref.name, ref.authority, view, ref.ends_review))

                self._ask_view(ref.name, viewed)
            elif which == 'ends':

                def ended(ends: Optional[bool]) -> None:
                    if ends is not None:
                        save(make_ref(ref.label, ref.name, ref.authority, ref.view, ends))

                self._ask_ends(ref.name, ref.authority, ended)

        choices = [
            ('authority', f'Authority: {ref.authority.name.lower()}'),
            ('view', f'View: {ref.view or "none"}'),
            ('ends', f'Ends review: {"yes" if ref.ends_review else "no"}'),
            ('label', f'Label: {ref.label}'),
        ]
        self.app.push_screen(Pick(f'Change {ref.name} ({upstream.project})', choices), field)

    def action_untrack(self) -> None:
        key, service = self._highlighted_upstream(), self._upstreams
        if key is None or service is None:
            return
        upstream = self._listed[key]
        name = upstream.ref.name
        if upstream.remote:
            what = (
                f'Its fetch line goes from the checkout, and remote {upstream.remote} too once '
                'nothing else uses it.'
            )
        else:
            what = 'The branch itself stays.'
        self._confirm_upstream(
            f'Stop tracking {name} for {upstream.project}? {what} [y|N]',
            lambda: service.remove(upstream.project, name),
        )

    def _confirm_upstream(self, prompt: str, do: Callable[[], str]) -> None:
        from .app import Confirm

        def answered(yes: Optional[bool]) -> None:
            if yes:
                self._upstream_work(do)

        self.app.push_screen(Confirm(prompt), answered)

    def action_back(self) -> None:
        tab = getattr(self.app, 'action_tab', None)
        if tab is not None:
            tab('lkml')
        else:
            self.app.pop_screen()


def _counts_ref(ref: Ref) -> str:
    """How membership in *ref* counts, in a few words."""
    parts = [ref.authority.name.lower()]
    if ref.view:
        parts.append(f'view {ref.view}')
    if ref.ends_review:
        parts.append('ends review')
    return ' · '.join(parts)


def _counts(upstream: 'Upstream') -> str:
    text = _counts_ref(upstream.ref)
    return text + (' · local branch' if not upstream.remote else '')


def _remote_name(url: str) -> str:
    """A remote name from a URL: its last two path parts (…/mm/core.git: mm-core)."""
    parts = [p for p in url.rstrip('/').removesuffix('.git').split('/') if p]
    return '-'.join(parts[-2:]) if len(parts) >= 2 else (parts[-1] if parts else 'upstream')

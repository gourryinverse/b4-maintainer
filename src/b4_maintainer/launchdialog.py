# SPDX-License-Identifier: GPL-2.0-or-later
"""The F1 launch dialog: rendered from task profiles, re-planned on every change.

Nothing launches from here: confirming returns the choices, and the app asks the hub to
launch them. The summary shows what the session would receive and may write, and every
reason it cannot launch. Skills follow the other choices; the Advanced section adjusts them.
Each section is one Tab stop: arrows move within it, Enter or Space picks.
"""

import dataclasses
from dataclasses import dataclass
from typing import Optional, Sequence

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.suggester import SuggestFromList
from textual.widget import Widget
from textual.widgets import (
    Button,
    Collapsible,
    Input,
    Label,
    RadioButton,
    RadioSet,
    Select,
    SelectionList,
    Static,
)
from textual.widgets.collapsible import CollapsibleTitle
from textual.widgets.selection_list import Selection

from .drawer import session_label
from .sessions.launch import WORKSPACE, LaunchChoice, Plan, Situation, Target, plan
from .sessions.models import THINKING, ModelChoice
from .sessions.policy import ContextItem
from .sessions.profiles import MODES, Profile, suggested_skills

# What each mode's session may do (the mode decides its permissions).
PERMISSION_LABELS = {
    'read-only': 'reads and searches the series, the checkout, and its notes; writes only out/',
    'execute': 'also edits, builds, and runs in its own worktree, in the sandbox',
}
BASE_PLACEHOLDER = 'Base: empty lets b4 choose (HEAD if it applies, the series base, a guess)'
NO_SERIES = 'a workspace session has no series to apply'
LAUNCH = 'Launch (ctrl+s)'
MODE_LABELS = {
    'lightweight': 'lightweight: analyze the series mail and the checkout; writes only out/',
    'worktree': 'worktree: the series applied in its own tree, to edit, build, run, and test',
}


@dataclass(frozen=True)
class Derived:
    """A session started from another's output: where its tree starts (no series
    applied) and the session it came from."""

    parent: str
    start: str  # A commit or ref.
    start_from: str = ''  # The session whose tree holds *start*; empty: the project.
    label: str = ''  # Where it starts, as the maintainer reads it.


@dataclass(frozen=True)
class LaunchResult:
    target: Target
    profile: Profile
    choice: LaunchChoice
    items: tuple[ContextItem, ...]


class LaunchDialog(ModalScreen[Optional[LaunchResult]]):
    DEFAULT_CSS = """
    LaunchDialog { align: center middle; }
    LaunchDialog > Vertical {
        width: 150; max-width: 95%; height: 90%;
        border: solid $accent; background: $panel;
    }
    LaunchDialog #launch-columns { height: 1fr; }
    LaunchDialog #launch-summary-pane {
        width: 2fr; height: 1fr; padding: 0 1; border-left: solid $panel-lighten-2;
    }
    LaunchDialog #launch-title { height: 1; padding: 0 1; text-style: bold; }
    LaunchDialog #choices { width: 3fr; height: 1fr; padding: 0 1; }
    LaunchDialog #launch-body { height: auto; }
    LaunchDialog .group { margin-top: 1; text-style: bold; }
    LaunchDialog RadioSet { border: none; height: auto; width: 1fr; }
    LaunchDialog RadioSet:focus { border: none; background: $boost; }
    LaunchDialog SelectionList { border: none; height: auto; width: 1fr; padding: 0; }
    LaunchDialog SelectionList:focus { border: none; background: $boost; }
    LaunchDialog Collapsible { margin-top: 1; border: none; padding: 0; }
    LaunchDialog Input { border: none; height: 1; margin-left: 4; background: $boost; }
    LaunchDialog Select { margin-left: 2; width: 60; }
    LaunchDialog Input:focus { border: none; background: $primary-background; }
    LaunchDialog #launch-summary { width: 1fr; height: auto; }
    LaunchDialog #buttons { height: 3; align: right middle; padding: 0 1; }
    """
    BINDINGS = [
        Binding('escape', 'cancel', 'cancel'),
        Binding('ctrl+s', 'launch', 'launch'),
    ]

    def __init__(
        self,
        target: Optional[Target],
        profiles: Sequence[Profile],
        situation: Situation,
        items: dict[str, tuple[ContextItem, ...]],
        problems: Sequence[str] = (),
        derived: Optional[Derived] = None,
    ) -> None:
        super().__init__()
        self.target = target  # The selected or open series; None offers the workspace only.
        self.derived = derived  # A session derived from another's output.
        self.profiles = list(profiles)
        self.situation = situation
        self.items = items  # By scope.
        self.problems = list(problems)
        self.scope = 'series' if target is not None and self._of_scope('series') else 'workspace'
        self.profile: Optional[Profile] = None
        self.plan: Optional[Plan] = None
        self._explicit_mode: Optional[str] = None
        self._skills_on: set[str] = set()  # The maintainer's additions to suggested skills,
        self._skills_off: set[str] = set()  # and removals.
        self._suggested_mode = 'lightweight'
        self._mode_shown = 'lightweight'
        self._sets: dict[str, RadioSet] = {}
        self._buttons: dict[tuple[str, str], RadioButton] = {}
        self._lists: dict[str, SelectionList[str]] = {}
        self._keys: dict[int, tuple[str, str]] = {}
        self._inputs: dict[str, Input] = {}
        self._selects: dict[str, Select[str]] = {}
        self._sections: dict[int, str] = {}  # Focusable widget -> section name.
        self._skills_shown: set[str] = set()
        self._labels: list[str] = []
        self._serial = 0

    def _of_scope(self, scope: str) -> list[Profile]:
        return [p for p in self.profiles if p.scope == scope]

    # Building

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static('', id='launch-title', markup=False)
            with Horizontal(id='launch-columns'):
                with VerticalScroll(id='choices', can_focus=False):
                    if self.target is not None:
                        yield from self._group('Session for', 'scope', self._scope_options(), 0)
                    yield Vertical(id='launch-body')
                with VerticalScroll(id='launch-summary-pane', can_focus=False):
                    yield Static('', id='launch-summary', markup=False)
            with Horizontal(id='buttons'):
                yield Button(LAUNCH, id='launch-go', variant='primary')
                yield Button('Cancel (esc)', id='launch-cancel')

    def _scope_options(self) -> list[tuple[str, str, bool]]:
        assert self.target is not None
        series_ok = bool(self._of_scope('series'))
        label = session_label(self.target) + ('' if series_ok else ' (no series profile)')
        return [('series', label, not series_ok), ('workspace', 'Workspace', False)]

    async def on_mount(self) -> None:
        await self._rebuild()

    async def _rebuild(self) -> None:
        """Render the scope's profile; called again when the scope or profile changes."""
        for group in [g for g in self._sets if g != 'scope']:
            del self._sets[group]
        self._buttons = {k: v for k, v in self._buttons.items() if k[0] == 'scope'}
        self._lists.clear()
        self._inputs.clear()
        self._selects.clear()
        self._skills_shown = set()
        self._labels = []  # The profile's labels only.
        profiles = self._of_scope(self.scope)
        if self.profile not in profiles:
            self.profile = profiles[0] if profiles else None
        # A derived session works in its own tree.
        self._explicit_mode = 'worktree' if self.derived is not None else None
        self._skills_on.clear()
        self._skills_off.clear()
        body = self.query_one('#launch-body', Vertical)
        await body.remove_children()
        await body.mount_all(list(self._body(profiles)))
        for options in self._lists.values():
            options.highlighted = 0 if options.option_count else None
        self.query_one('#launch-title', Static).update(self.title_text())
        sections = body.query('RadioSet, SelectionList, Input')
        if sections:
            sections.first().focus()  # The profile's first section, in screen order.
        self._update()

    def _body(self, profiles: list[Profile]) -> list[Widget]:
        widgets: list[Widget] = []
        if self.profile is None:
            widgets.append(Static(f'No {self.scope} profile is configured.', markup=False))
            return widgets
        if len(profiles) > 1:
            options = [(p.id, p.label, False) for p in profiles]
            widgets += self._group('Task', 'profile', options, profiles.index(self.profile))
        for field in self.profile.fields:
            options = [(c.id, c.label, False) for c in field.choices]
            if field.kind == 'choice':
                widgets += self._group(field.label, field.id, options, None)
            elif field.kind == 'multi':
                widgets += self._checks(field.label, field.id, options)
            else:
                widgets.append(self._label(field.label))
            if field.kind == 'text' or field.custom:
                placeholder = field.label if field.kind == 'text' else field.custom_label
                placeholder = placeholder or f'Custom {field.label.lower()}'
                section = field.id if field.kind == 'text' else f'{field.id}:text'
                widgets.append(self._input(field.id, section, placeholder))
        widgets += self._package()
        modes = [(m, MODE_LABELS[m], False) for m in MODES]
        widgets += self._group('Mode', 'mode', modes, 0)
        bases = self.situation.bases
        hint = f'{BASE_PLACEHOLDER}; → completes: {", ".join(bases)}' if bases else BASE_PLACEHOLDER
        base = self._input('base', 'base', hint, bases)
        base.display = False  # Worktree mode only.
        widgets.append(base)
        self._mode_shown = 'lightweight'
        shown = [m for m in self.situation.models if m.kind != 'catalog']
        models = [(m.id, _model_label(m), not m.available) for m in shown]
        default = next((n for n, m in enumerate(shown) if m.available), None)
        widgets += self._group('Model', 'model', models, default)
        widgets.append(self._advanced())
        return widgets

    def _package(self) -> list[Widget]:
        """The prompt package: the scope's sources, available ones ticked, others disabled
        with the reason."""
        items = self.items.get(self.scope, ())
        if not items:
            return []
        options = [
            (i.id, i.label if i.available else f'{i.label} — {i.reason}', not i.available)
            for i in items
        ]
        ticked = frozenset(i.id for i in items if i.available)
        return self._checks('Prompt package', 'package', options, ticked)

    def _advanced(self) -> Collapsible:
        """Skills (ticked from the other choices), any model in pi's catalog, and thinking:
        rarely needed, so collapsed."""
        skills = [
            (s.id, f'{s.id}: {_first_sentence(s.description)}', False)
            for s in self.situation.skills
        ]
        children: list[Widget] = (
            self._checks('Skills', 'skills', skills)
            if skills
            else [Static('No skills installed.', markup=False)]
        )
        catalog = [(m.label, m.id) for m in self.situation.models if m.kind == 'catalog']
        children.append(self._label("Other model (pi's catalog; overrides Model)"))
        children.append(self._select('other-model', catalog, 'Use Model above'))
        children.append(self._label('Thinking'))
        children.append(self._select('thinking', [(t, t) for t in THINKING], "The model's default"))
        return Collapsible(*children, title='Advanced', collapsed=True, id=self._new_id())

    def _select(self, key: str, options: list[tuple[str, str]], prompt: str) -> Select[str]:
        select: Select[str] = Select(options, prompt=prompt, compact=True, id=self._new_id())
        self._selects[key] = select
        self._sections[id(select)] = key
        return select

    def _selected_value(self, key: str) -> str:
        select = self._selects.get(key)
        value = select.value if select is not None else Select.NULL
        return value if isinstance(value, str) else ''

    def _input(
        self, key: str, section: str, placeholder: str, suggestions: Sequence[str] = ()
    ) -> Input:
        suggester = SuggestFromList(suggestions, case_sensitive=False) if suggestions else None
        box = Input(placeholder=placeholder, suggester=suggester, id=self._new_id())
        self._inputs[key] = box
        self._sections[id(box)] = section
        return box

    def _new_id(self) -> str:
        self._serial += 1
        return f'w{self._serial}'

    def _label(self, text: str) -> Label:
        self._labels.append(text)
        return Label(text, classes='group', markup=False)

    def _group(
        self, title: str, group: str, options: list[tuple[str, str, bool]], pressed: Optional[int]
    ) -> list[Widget]:
        label = self._label(title)
        self._labels += [text for _key, text, _disabled in options]
        return [label, self._radio(group, options, pressed)]

    def _radio(
        self, group: str, options: list[tuple[str, str, bool]], pressed: Optional[int]
    ) -> RadioSet:
        buttons = []
        for n, (key, label, disabled) in enumerate(options):
            button = RadioButton(label, value=n == pressed, disabled=disabled, id=self._new_id())
            self._buttons[(group, key)] = button
            self._keys[id(button)] = (group, key)
            buttons.append(button)
        radio = RadioSet(*buttons, id=self._new_id())
        self._sets[group] = radio
        self._keys[id(radio)] = (group, '')
        self._sections[id(radio)] = group
        return radio

    def _show_mode(self, mode: str) -> None:
        """Show the suggested mode as pressed, without counting it as the maintainer's choice.

        A replacement set: changing a button from within another set's Changed handler
        would be dropped (Textual prevents RadioButton.Changed for the whole handler).
        """
        old = self._sets['mode']
        options = [(m, MODE_LABELS[m], False) for m in MODES]
        new = self._radio('mode', options, MODES.index(mode))
        self.query_one('#launch-body', Vertical).mount(new, before=old)
        old.remove()
        self._mode_shown = mode

    def _checks(
        self,
        title: str,
        group: str,
        options: list[tuple[str, str, bool]],
        ticked: frozenset[str] = frozenset(),
    ) -> list[Widget]:
        """Any of several: one list (a single Tab stop), Space toggles the highlighted one."""
        selections = [
            Selection(label, key, key in ticked, id=key, disabled=off)
            for key, label, off in options
        ]
        widget: SelectionList[str] = SelectionList(*selections, id=self._new_id())
        self._lists[group] = widget
        self._keys[id(widget)] = (group, '')
        self._sections[id(widget)] = group
        label = self._label(title)
        self._labels += [text for _key, text, _disabled in options]
        return [label, widget]

    # State

    def selected(self, group: str) -> Optional[str]:
        radio = self._sets.get(group)
        button = radio.pressed_button if radio is not None else None
        return self._keys[id(button)][1] if button is not None else None

    def ticked(self, group: str) -> list[str]:
        """Ticked keys of a list, in option order."""
        widget = self._lists.get(group)
        if widget is None:
            return []
        chosen = set(widget.selected)
        options = (widget.get_option_at_index(n) for n in range(widget.option_count))
        return [o.value for o in options if o.value in chosen]

    def _choice(self, mode: Optional[str]) -> LaunchChoice:
        assert self.profile is not None
        choices: dict[str, object] = {}
        text: dict[str, str] = {}
        for field in self.profile.fields:
            if field.kind == 'choice':
                picked = self.selected(field.id)
                if picked is not None:
                    choices[field.id] = picked
            elif field.kind == 'multi':
                ticked = self.ticked(field.id)
                if ticked:
                    choices[field.id] = ticked
            box = self._inputs.get(field.id)
            value = box.value.strip() if box is not None else ''
            if field.kind == 'text':
                if value:
                    choices[field.id] = value
            elif value:
                text[field.id] = value
        return LaunchChoice(
            self.profile.id,
            choices,
            text,
            mode,
            self._selected_value('other-model') or self.selected('model') or '',
            self._selected_value('thinking'),
            self._inputs['base'].value.strip() if 'base' in self._inputs else '',
            frozenset(self._skills_on),
            frozenset(self._skills_off),
            self._context_off(),
            *self._derived_choice(),
        )

    def _derived_choice(self) -> tuple[str, str, str]:
        """(start, start_from, parent) of a derived session (the start only matters to a
        worktree session)."""
        d = self.derived
        return (d.start, d.start_from, d.parent) if d is not None else ('', '', '')

    def _context_off(self) -> frozenset[str]:
        """Available sources the maintainer unticked."""
        if 'package' not in self._lists:
            return frozenset()
        ticked = set(self.ticked('package'))
        items = self.items.get(self.scope, ())
        return frozenset(i.id for i in items if i.available and i.id not in ticked)

    def _target(self) -> Target:
        return self.target if self.scope == 'series' and self.target is not None else WORKSPACE

    def _update(self) -> None:
        if self.profile is None:
            self.plan = None
            self.query_one('#launch-summary', Static).update('\n'.join(self.problems))
            self.query_one('#launch-go', Button).disabled = True
            return
        items = self.items.get(self.scope, ())
        situation = self.situation
        if self.scope != 'series' and not situation.worktree_reason:
            situation = dataclasses.replace(situation, worktree_reason=NO_SERIES)
        self._suggested_mode = plan(self.profile, self._choice(None), situation, items).mode
        self.plan = plan(self.profile, self._choice(self._explicit_mode), situation, items)
        if self._explicit_mode is None and self._mode_shown != self.plan.mode:
            self._show_mode(self.plan.mode)
        if 'base' in self._inputs:
            self._inputs['base'].display = self.plan.mode == 'worktree'
        skills = self._lists.get('skills')
        if skills is not None and set(self.ticked('skills')) != set(self.plan.skills):
            with self.prevent(SelectionList.SelectedChanged):  # Suggested, not chosen.
                skills.deselect_all()
                for key in self.plan.skills:
                    skills.select(key)
        self._skills_shown = set(self.plan.skills)
        self.query_one('#launch-summary', Static).update(self.summary_text())
        go = self.query_one('#launch-go', Button)
        go.disabled = not self.plan.ok
        go.label = LAUNCH if self.plan.ok else 'Cannot launch: see the summary'

    async def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        group = self._keys.get(id(event.radio_set), ('', ''))[0]
        key = self._keys.get(id(event.pressed), ('', ''))[1]
        if group == 'scope':
            self.scope = key
            await self._rebuild()
        elif group == 'profile':
            self.profile = next(p for p in self.profiles if p.id == key)
            await self._rebuild()
        else:
            if group == 'mode':
                self._mode_shown = key
                self._explicit_mode = None if key == self._suggested_mode else key
            self._update()

    def on_selection_list_selected_changed(self, event: SelectionList.SelectedChanged) -> None:
        group = self._keys.get(id(event.selection_list), ('', ''))[0]
        if group == 'skills' and self.profile is not None:
            mode = self.plan.mode if self.plan is not None else ''
            suggested = set(suggested_skills(self.profile, self._choice(None).choices, mode))
            now = set(self.ticked('skills'))
            on, off = self._skills_on, self._skills_off
            for key in now - self._skills_shown:  # Ticked by the maintainer.
                (off.discard if key in suggested else on.add)(key)
            for key in self._skills_shown - now:  # Unticked by the maintainer.
                (off.add if key in suggested else on.discard)(key)
        self._update()

    def on_select_changed(self, event: Select.Changed) -> None:
        self._update()

    def on_input_changed(self, event: Input.Changed) -> None:
        self._update()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == 'launch-go':
            self.action_launch()
        elif event.button.id == 'launch-cancel':
            self.action_cancel()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_launch(self) -> None:
        if self.profile is None or self.plan is None or not self.plan.ok:
            return
        result = LaunchResult(
            self._target(),
            self.profile,
            self._choice(self._explicit_mode),
            self.items.get(self.scope, ()),
        )
        self.dismiss(result)

    # Text, for the summary and tests

    def title_text(self) -> str:
        return f'Agent session · {session_label(self._target())}'

    def labels(self) -> list[str]:
        return list(self._labels)

    def option_disabled(self, group: str, key: str) -> bool:
        if group in self._lists:
            return self._lists[group].get_option(key).disabled
        return self._buttons[(group, key)].disabled

    def focused_section(self) -> Optional[str]:
        focused = self.focused
        if focused is None:
            return None
        if isinstance(focused, CollapsibleTitle):
            return 'advanced'
        if isinstance(focused, Button):
            return {'launch-go': 'launch', 'launch-cancel': 'cancel'}.get(focused.id or '')
        return self._sections.get(id(focused))

    def base_input(self) -> Input:
        return self._inputs['base']

    def base_shown(self) -> bool:
        return 'base' in self._inputs and bool(self._inputs['base'].display)

    def advanced_open(self) -> bool:
        return not self.query_one(Collapsible).collapsed

    def can_launch(self) -> bool:
        return self.plan is not None and self.plan.ok

    def pick(self, group: str, key: str) -> None:
        """Press a radio button, or toggle a list entry, as a click would."""
        if group in self._lists:
            self._lists[group].toggle(key)
        elif group in self._selects:
            self._selects[group].value = key
        else:
            self._buttons[(group, key)].value = True

    def write(self, field: str, text: str) -> None:
        self._inputs[field].value = text

    def summary_text(self) -> str:
        p = self.plan
        if p is None:
            return ''
        lines = [f'Cannot launch: {b}' for b in p.blockers]  # First, where it is seen.
        lines += [
            f'Session:  {session_label(self._target())} · {p.mode}',
            f'Model:    {_model_text(p.model, p.thinking)}',
        ]
        if self.derived is not None:
            where = self.derived.label or self.derived.start
            tree = f'; tree starts at {where}, no series applied' if p.mode == 'worktree' else ''
            lines.append(f'Derived:  from session {self.derived.parent}{tree}')
        lines += [
            f'Receives: {", ".join(i.label for i in p.included) or "the task prompt only"}',
        ]
        guidance = [
            f'{s.source} {s.version}'.rstrip()
            for s in self.situation.skills
            if s.source and s.id in p.skills
        ]
        if guidance:
            lines.append(f'Guidance: {", ".join(guidance)}')
        if p.skills:
            lines.append(f'Skills:   {", ".join(p.skills)}')
        lines.append(f'May:      {PERMISSION_LABELS.get(p.permissions, p.permissions)}')
        if p.mode == 'worktree':
            base = self._inputs['base'].value.strip() if 'base' in self._inputs else ''
            where = base or 'the base b4 chooses'
            lines.append(f'Worktree: the series applied on {where}, on its own branch')
        lines += [f'Note:     {n}' for n in p.notes]
        lines += [f'Warning:  {w}' for w in (*p.warnings, *self.problems)]
        if p.request is not None:
            lines += ['', 'Prompt:', *(f'  {line}' for line in p.request.prompt.splitlines())]
        return '\n'.join(lines)


def _model_label(model: ModelChoice) -> str:
    return f'{model.label} — {model.reason}' if not model.available else model.label


def _model_text(model: Optional[ModelChoice], thinking: str) -> str:
    if model is None:
        return '?'
    name = model.model or "pi's default model"
    return f'{name} · thinking {thinking}' if thinking else name


def _first_sentence(text: str) -> str:
    head, dot, _rest = text.partition('. ')
    return head + ('.' if dot else '')

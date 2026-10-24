# SPDX-License-Identifier: GPL-2.0-or-later
"""Planning a launch: the dialog's choices checked against models, limits, and skills.

The dialog re-plans on every change to show what the session would receive and why it
cannot launch; the hub plans again when the maintainer confirms. Nothing here runs.
"""

import datetime
import secrets
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

from .models import THINKING, ModelChoice
from .policy import ContextItem, SessionLimits, launch_check
from .profiles import PERMISSIONS, Profile, ProfileError, Request, compose, suggested_skills
from .records import SessionRecord
from .skills import Skill


@dataclass(frozen=True)
class Target:
    """What a session is pinned to: one series version, or the workspace."""

    scope: str  # series or workspace
    project: str = ''
    series: str = ''  # Series identity.
    revision: int = 0
    subject: str = ''
    source: str = ''  # The thread's mbox, copied into the session's context.


WORKSPACE = Target('workspace')


@dataclass(frozen=True)
class LaunchChoice:
    profile: str
    choices: Mapping[str, Any] = field(default_factory=dict)
    text: Mapping[str, str] = field(default_factory=dict)
    mode: Optional[str] = None  # Chosen explicitly; else the profile's suggestion.
    model: str = ''  # A ModelChoice id.
    thinking: str = ''  # Empty: the model choice's.
    permissions: str = ''  # Empty: the profile's default.
    # The maintainer's adjustments to the suggested skills (the dialog's Advanced section).
    skills_on: frozenset[str] = frozenset()
    skills_off: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Situation:
    """What a launch is checked against, gathered by the app."""

    models: Sequence[ModelChoice]
    limits: SessionLimits = SessionLimits()
    live: int = 0
    worktrees: int = 0
    worktree_bytes: int = 0
    execute_reason: str = ''  # Why sandboxed execution is unavailable; empty if it is.
    skills: Sequence[Skill] = ()  # Installed skills.


@dataclass(frozen=True)
class Plan:
    request: Optional[Request]
    mode: str
    permissions: str
    model: Optional[ModelChoice]
    thinking: str = ''
    included: tuple[ContextItem, ...] = ()
    skills: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.request is not None and not self.blockers


def plan(
    profile: Profile, choice: LaunchChoice, situation: Situation, items: Iterable[ContextItem]
) -> Plan:
    blockers: list[str] = []
    warnings: list[str] = []
    request: Optional[Request] = None
    try:
        request = compose(profile, choice.choices, choice.text, choice.mode)
    except ProfileError as ex:
        blockers.append(str(ex))
    mode = request.mode if request else (choice.mode or 'lightweight')
    permissions = choice.permissions or profile.permissions
    if permissions not in PERMISSIONS:
        blockers.append(f'unknown permission profile {permissions!r}')
    model = next((m for m in situation.models if m.id == choice.model), None)
    if model is None:
        blockers.append('Choose a model')
    elif not model.available:
        blockers.append(f'{model.id}: {model.reason}')
    thinking = choice.thinking or (model.thinking if model else '')
    if thinking and thinking not in THINKING:
        blockers.append(f'thinking must be one of {", ".join(THINKING)}')
    if permissions == 'execute':
        if mode != 'worktree':
            blockers.append('execute needs a worktree session')
        elif situation.execute_reason:
            blockers.append(f'execute unavailable: {situation.execute_reason}')
    limit_blockers, limit_warnings = launch_check(
        situation.limits, situation.live, situation.worktrees, situation.worktree_bytes, mode
    )
    blockers.extend(limit_blockers)
    warnings.extend(limit_warnings)
    installed = {s.id for s in situation.skills}
    wanted = [s for s in chosen_skills(profile, choice) if s not in choice.skills_off]
    warnings.extend(f'skill {s!r} is not installed' for s in wanted if s not in installed)
    return Plan(
        request,
        mode,
        permissions,
        model,
        thinking,
        tuple(items),
        tuple(s for s in wanted if s in installed),
        tuple(blockers),
        tuple(warnings),
        request.notes if request else (),
    )


def chosen_skills(profile: Profile, choice: LaunchChoice) -> tuple[str, ...]:
    """Suggested skills plus the maintainer's additions, before removals."""
    suggested = suggested_skills(profile, choice.choices)
    return tuple(dict.fromkeys([*suggested, *sorted(choice.skills_on)]))


def new_session_id(now: datetime.datetime) -> str:
    return f'{now:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}'


def new_record(
    target: Target, profile: Profile, choice: LaunchChoice, planned: Plan, now: datetime.datetime
) -> SessionRecord:
    assert planned.model is not None
    sid = new_session_id(now)
    return SessionRecord(
        id=sid,
        created=now.isoformat(timespec='seconds'),
        scope=target.scope,
        project=target.project,
        series=target.series,
        revision=target.revision,
        subject=target.subject,
        profile_id=profile.id,
        profile_version=profile.version,
        profile_hash=profile.hash,
        choices=dict(choice.choices) | {f'{k}:text': v for k, v in choice.text.items() if v},
        mode=planned.mode,
        harness='pi',
        preset=planned.model.id,
        command=(),
        native_id=sid,
        model=planned.model.model,
        thinking=planned.thinking,
        permissions=planned.permissions,
        context=tuple(i.id for i in planned.included),
        skills=planned.skills,
    )

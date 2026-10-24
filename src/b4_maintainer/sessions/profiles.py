# SPDX-License-Identifier: GPL-2.0-or-later
"""Task profiles: data that the launch dialog renders and composes into a session's prompts.

Built-in profiles ship here; the maintainer's own (state/profiles/) add to them, and
one with a built-in's id replaces it. Profiles are trusted configuration; the series
they are applied to is not.
"""

import hashlib
import pathlib
import tomllib
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

BUILTIN_DIR = pathlib.Path(__file__).parent / 'profiles'
KINDS = ('choice', 'multi', 'text')
# Decided by the mode: lightweight sessions analyze (read-only), worktree sessions also
# edit, build, and run in their sandbox (execute).
PERMISSIONS = ('read-only', 'execute')
MODE_PERMISSIONS = {'lightweight': 'read-only', 'worktree': 'execute'}
SCOPES = ('series', 'workspace')
MODES = ('lightweight', 'worktree')


class ProfileError(ValueError):
    pass


@dataclass(frozen=True)
class Choice:
    id: str
    label: str
    prompt: str = ''
    files: tuple[str, ...] = ()  # Guidance files, e.g. review-prompts:agents/review.md.
    needs: str = ''  # 'worktree' when the choice needs a tree to build or run.
    skills: tuple[str, ...] = ()  # Skills suggested when picked.


@dataclass(frozen=True)
class Field:
    id: str
    label: str
    kind: str
    required: bool = False
    choices: tuple[Choice, ...] = ()
    prompt: str = ''  # For text fields: filled with {text}.
    # Choice fields: optional free text added to the picked choices, filling {text}.
    custom: str = ''
    custom_label: str = ''


@dataclass(frozen=True)
class Profile:
    id: str
    version: int
    label: str
    scope: str
    system_prompt: str
    fields: tuple[Field, ...]
    hash: str
    skills: tuple[str, ...] = ()  # Skills suggested for every session of this profile.
    worktree_skills: tuple[str, ...] = ()  # And for its worktree sessions.


@dataclass(frozen=True)
class Request:
    """What a profile and the dialog's choices compose: a session's prompts and mode."""

    prompt: str
    system_prompt: str
    files: tuple[str, ...]
    mode: str
    notes: tuple[str, ...]
    skills: tuple[str, ...] = ()  # Suggested by the profile and the picked choices.


def _str(data: Mapping[str, Any], key: str, where: str, default: Optional[str] = None) -> str:
    value = data.get(key, default)
    if not isinstance(value, str) or (default is None and not value):
        raise ProfileError(f'{where}: {key} must be a non-empty string')
    return value


def _choice(raw: Any, where: str) -> Choice:
    if not isinstance(raw, dict):
        raise ProfileError(f'{where}: a choice must be a table')
    cid = _str(raw, 'id', where)
    needs = _str(raw, 'needs', f'{where}.{cid}', '')
    if needs not in ('', 'worktree'):
        raise ProfileError(f'{where}.{cid}: needs must be "worktree"')
    return Choice(
        cid,
        _str(raw, 'label', f'{where}.{cid}'),
        _str(raw, 'prompt', f'{where}.{cid}', ''),
        _strings(raw, 'files', f'{where}.{cid}'),
        needs,
        _strings(raw, 'skills', f'{where}.{cid}'),
    )


def _strings(raw: Mapping[str, Any], key: str, where: str) -> tuple[str, ...]:
    values = raw.get(key, [])
    if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
        raise ProfileError(f'{where}: {key} must be a list of strings')
    return tuple(values)


def _field(raw: Any) -> Field:
    if not isinstance(raw, dict):
        raise ProfileError('a field must be a table')
    fid = _str(raw, 'id', 'field')
    kind = _str(raw, 'kind', fid)
    if kind not in KINDS:
        raise ProfileError(f'{fid}: kind must be one of {", ".join(KINDS)}')
    choices = tuple(_choice(c, fid) for c in raw.get('choices', []))
    if len({c.id for c in choices}) != len(choices):
        raise ProfileError(f'{fid}: duplicate choice ids')
    if kind != 'text' and not choices:
        raise ProfileError(f'{fid}: a {kind} field needs choices')
    custom = _str(raw, 'custom', fid, '')
    if custom and (kind == 'text' or '{text}' not in custom):
        raise ProfileError(f'{fid}: custom is for choice fields and must contain {{text}}')
    return Field(
        fid,
        _str(raw, 'label', fid),
        kind,
        bool(raw.get('required', False)),
        choices,
        _str(raw, 'prompt', fid, ''),
        custom,
        _str(raw, 'custom_label', fid, ''),
    )


def parse_profile(text: str) -> Profile:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as ex:
        raise ProfileError(f'not valid TOML: {ex}') from ex
    pid = _str(data, 'id', 'profile')
    version = data.get('version')
    if not isinstance(version, int):
        raise ProfileError(f'{pid}: version must be an integer')
    scope = _str(data, 'scope', pid, 'series')
    if scope not in SCOPES:
        raise ProfileError(f'{pid}: scope must be one of {", ".join(SCOPES)}')
    fields = tuple(_field(f) for f in data.get('fields', []))
    if len({f.id for f in fields}) != len(fields):
        raise ProfileError(f'{pid}: duplicate field ids')
    return Profile(
        pid,
        version,
        _str(data, 'label', pid),
        scope,
        _str(data, 'system_prompt', pid, ''),
        fields,
        hashlib.sha256(text.encode()).hexdigest(),
        _strings(data, 'skills', pid),
        _strings(data, 'worktree_skills', pid),
    )


def load_profiles(
    builtin: pathlib.Path, mine: Optional[pathlib.Path]
) -> tuple[list[Profile], list[str]]:
    """Built-in profiles, then the maintainer's (replacing by id); and per-file problems."""
    found: dict[str, Profile] = {}
    problems: list[str] = []
    for directory in (builtin, mine):
        if directory is None or not directory.is_dir():
            continue
        for path in sorted(directory.glob('*.toml')):
            try:
                profile = parse_profile(path.read_text())
            except (OSError, ProfileError) as ex:
                problems.append(f'{path.name}: {ex}')
                continue
            found[profile.id] = profile
    return list(found.values()), problems


def _picked(field: Field, value: Any) -> list[str]:
    if field.kind == 'text':
        return []
    return [value] if isinstance(value, str) else list(value or [])


def suggested_skills(
    profile: Profile, choices: Mapping[str, Any], mode: str = ''
) -> tuple[str, ...]:
    """The profile's skills (and its worktree skills in worktree *mode*), then those of
    each picked choice, in field order, once each.

    Unknown choices are ignored here: the dialog shows suggestions while it is incomplete.
    """
    skills = list(profile.skills)
    if mode == 'worktree':
        skills.extend(profile.worktree_skills)
    for field in profile.fields:
        by_id = {c.id: c for c in field.choices}
        for cid in _picked(field, choices.get(field.id)):
            choice = by_id.get(cid)
            if choice is not None:
                skills.extend(choice.skills)
    return tuple(dict.fromkeys(skills))


def compose(
    profile: Profile,
    choices: Mapping[str, Any],
    text: Optional[Mapping[str, str]] = None,
    mode: Optional[str] = None,
) -> Request:
    """The session's prompts from the dialog's choices, in field order.

    *choices* maps field ids to a choice id (choice), a list of ids (multi), or text
    (text fields); *text* holds each choice field's custom text, added after its picked
    choices. Choices needing a tree suggest the worktree mode; an explicit *mode* wins.
    """
    text = text or {}
    parts: list[str] = []
    files: list[str] = []
    needing: list[str] = []
    for field in profile.fields:
        value = choices.get(field.id)
        if field.kind == 'text':
            if value:
                parts.append(field.prompt.replace('{text}', str(value)) if field.prompt else value)
            elif field.required:
                raise ProfileError(f'{field.label} is required')
            continue
        picked: Sequence[str] = _picked(field, value)
        free = text.get(field.id, '').strip() if field.custom else ''
        if field.required and not picked and not free:
            raise ProfileError(f'{field.label} is required')
        by_id = {c.id: c for c in field.choices}
        for cid in picked:
            choice = by_id.get(cid)
            if choice is None:
                raise ProfileError(f'{field.label}: unknown choice {cid!r}')
            if choice.prompt:
                parts.append(choice.prompt)
            files.extend(f for f in choice.files if f not in files)
            if choice.needs == 'worktree':
                needing.append(choice.label)
        if free:
            parts.append(field.custom.replace('{text}', free))
    if mode is not None and mode not in MODES:
        raise ProfileError(f'mode must be one of {", ".join(MODES)}')
    suggested = 'worktree' if needing else 'lightweight'
    notes = (f'Worktree suggested: {", ".join(needing)} needs a tree',) if needing else ()
    return Request(
        '\n\n'.join(parts),
        profile.system_prompt,
        tuple(files),
        mode or suggested,
        notes,
        suggested_skills(profile, choices, mode or suggested),
    )

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
PERMISSIONS = ('read-only', 'proposals', 'execute')
SCOPES = ('series', 'workspace')
MODES = ('lightweight', 'worktree')


class ProfileError(ValueError):
    pass


@dataclass(frozen=True)
class Choice:
    id: str
    label: str
    prompt: str = ''
    text: bool = False  # Takes free text, filling {text} in its prompt.
    files: tuple[str, ...] = ()  # Guidance files, e.g. review-prompts:agents/review.md.
    needs: str = ''  # 'worktree' when the choice needs a tree to build or run.


@dataclass(frozen=True)
class Field:
    id: str
    label: str
    kind: str
    required: bool = False
    choices: tuple[Choice, ...] = ()
    prompt: str = ''  # For text fields: filled with {text}.


@dataclass(frozen=True)
class Profile:
    id: str
    version: int
    label: str
    scope: str
    system_prompt: str
    permissions: str
    fields: tuple[Field, ...]
    hash: str


@dataclass(frozen=True)
class Request:
    """What a profile and the dialog's choices compose: a session's prompts and mode."""

    prompt: str
    system_prompt: str
    files: tuple[str, ...]
    mode: str
    notes: tuple[str, ...]


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
    files = raw.get('files', [])
    if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
        raise ProfileError(f'{where}.{cid}: files must be a list of strings')
    return Choice(
        cid,
        _str(raw, 'label', f'{where}.{cid}'),
        _str(raw, 'prompt', f'{where}.{cid}', ''),
        bool(raw.get('text', False)),
        tuple(files),
        needs,
    )


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
    return Field(
        fid,
        _str(raw, 'label', fid),
        kind,
        bool(raw.get('required', False)),
        choices,
        _str(raw, 'prompt', fid, ''),
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
    permissions = _str(data, 'permissions', pid, 'read-only')
    if permissions not in PERMISSIONS:
        raise ProfileError(f'{pid}: permissions must be one of {", ".join(PERMISSIONS)}')
    fields = tuple(_field(f) for f in data.get('fields', []))
    if len({f.id for f in fields}) != len(fields):
        raise ProfileError(f'{pid}: duplicate field ids')
    return Profile(
        pid,
        version,
        _str(data, 'label', pid),
        scope,
        _str(data, 'system_prompt', pid, ''),
        permissions,
        fields,
        hashlib.sha256(text.encode()).hexdigest(),
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


def compose(
    profile: Profile,
    choices: Mapping[str, Any],
    text: Optional[Mapping[str, str]] = None,
    mode: Optional[str] = None,
) -> Request:
    """The session's prompts from the dialog's choices, in field order.

    *choices* maps field ids to a choice id (choice), a list of ids (multi), or text
    (text fields); *text* holds the free text of choices that take it. Choices needing a
    tree suggest the worktree mode; an explicit *mode* wins.
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
        picked: Sequence[str] = [value] if isinstance(value, str) else list(value or [])
        if field.required and not picked:
            raise ProfileError(f'{field.label} is required')
        by_id = {c.id: c for c in field.choices}
        for cid in picked:
            choice = by_id.get(cid)
            if choice is None:
                raise ProfileError(f'{field.label}: unknown choice {cid!r}')
            prompt = choice.prompt
            if choice.text:
                free = text.get(field.id, '').strip()
                if not free:
                    raise ProfileError(f'{field.label}: {choice.label} needs text')
                prompt = prompt.replace('{text}', free) if prompt else free
            if prompt:
                parts.append(prompt)
            files.extend(f for f in choice.files if f not in files)
            if choice.needs == 'worktree':
                needing.append(choice.label)
    if mode is not None and mode not in MODES:
        raise ProfileError(f'mode must be one of {", ".join(MODES)}')
    suggested = 'worktree' if needing else 'lightweight'
    notes = (f'Worktree suggested: {", ".join(needing)} needs a tree',) if needing else ()
    return Request(
        '\n\n'.join(parts), profile.system_prompt, tuple(files), mode or suggested, notes
    )

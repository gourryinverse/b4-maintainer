# SPDX-License-Identifier: GPL-2.0-or-later
"""Skills a session can be given: Agent Skills directories (`<id>/SKILL.md`).

Built-in skills ship here; the maintainer's own (state/skills/) add to them, and one with
a built-in's id replaces it. Profile choices suggest skills; the dialog's Advanced section
adjusts them. Adapters install the chosen ones where their agent CLI loads skills.
"""

import pathlib
import re
from dataclasses import dataclass
from typing import Optional

BUILTIN_DIR = pathlib.Path(__file__).parent / 'skills'
SKILL_FILE = 'SKILL.md'
NAME = re.compile(r'[a-z0-9]+(-[a-z0-9]+)*')


class SkillError(ValueError):
    pass


@dataclass(frozen=True)
class Skill:
    id: str
    description: str
    path: Optional[pathlib.Path]  # The skill's directory.
    root: Optional[pathlib.Path] = None  # Also readable in the sandbox (a guidance repo).
    source: str = ''  # The guidance source it comes from (review-prompts, kernel-style).
    version: str = ''  # That source's commit.


def _frontmatter(text: str) -> dict[str, str]:
    """`key: value` lines between the leading `---` lines (name and description only)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != '---':
        raise SkillError('SKILL.md must start with --- frontmatter')
    try:
        end = next(n for n, line in enumerate(lines[1:], 1) if line.strip() == '---')
    except StopIteration:
        raise SkillError('SKILL.md frontmatter is not closed with ---') from None
    fields = {}
    for line in lines[1:end]:
        key, sep, value = line.partition(':')
        if sep and not line.startswith((' ', '\t')):
            fields[key.strip()] = value.strip().strip('"\'')
    return fields


def parse_skill(directory: pathlib.Path) -> Skill:
    try:
        text = (directory / SKILL_FILE).read_text()
    except OSError as ex:
        raise SkillError(f'cannot read {SKILL_FILE}: {ex.strerror}') from ex
    fields = _frontmatter(text)
    name = fields.get('name', '')
    if not NAME.fullmatch(name):
        raise SkillError('name must be lowercase letters, digits, and single hyphens')
    if name != directory.name:
        raise SkillError(f'name {name!r} must match its directory {directory.name!r}')
    description = fields.get('description', '')
    if not description:
        raise SkillError('description is required')
    return Skill(name, description, directory)


def load_skills(
    builtin: pathlib.Path, mine: Optional[pathlib.Path]
) -> tuple[list[Skill], list[str]]:
    """Built-in skills, then the maintainer's (replacing by id); and per-skill problems.

    Directories without SKILL.md are not skills and are skipped silently.
    """
    found: dict[str, Skill] = {}
    problems: list[str] = []
    for root in (builtin, mine):
        if root is None or not root.is_dir():
            continue
        for directory in sorted(p for p in root.iterdir() if p.is_dir()):
            if not (directory / SKILL_FILE).is_file():
                continue
            try:
                skill = parse_skill(directory)
            except SkillError as ex:
                problems.append(f'{directory.name}: {ex}')
                continue
            found[skill.id] = skill
    return list(found.values()), problems

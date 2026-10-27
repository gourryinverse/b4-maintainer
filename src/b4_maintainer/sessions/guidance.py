# SPDX-License-Identifier: GPL-2.0-or-later
"""Guidance repositories as pi skills: review-prompts (kernel review, debugging, inline
reply protocols) and kernel-style (code, changelog, and review style).

Both are written to be loaded as agent skills that read further files on demand, so a
session gets each as a skill and its checkout readable in the sandbox; the agent follows
the repository's own load protocol. kernel-style's skill ships ready
(`skills/kernel-style/SKILL.md`, which reads from the repository root); review-prompts'
kernel skill is a template whose prompts directory is filled in here, as its setup
scripts do, into a generated directory outside the repository.

Sources default to the pinned checkouts in deps/; `[[guidance]]` in workspace.toml points
one at another checkout. Each session records the commit of every source it was given.
"""

import pathlib
import subprocess
from typing import Iterable, Mapping

from .skills import SKILL_FILE, Skill, SkillError, parse_skill

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]  # src/b4_maintainer/sessions/
DEFAULTS = {
    'review-prompts': REPO_ROOT / 'deps' / 'review-prompts',
    'kernel-style': REPO_ROOT / 'deps' / 'kernel-style',
}
PLACEHOLDER = '{{KERNEL_REVIEW_PROMPTS_DIR}}'


def sources(overrides: Mapping[str, str], root: pathlib.Path) -> dict[str, pathlib.Path]:
    """Each guidance source's checkout: the pinned default, or the configured path (~
    expanded; relative to the workspace *root*)."""
    found = dict(DEFAULTS)
    for sid, path in overrides.items():
        where = pathlib.Path(path).expanduser()
        found[sid] = where if where.is_absolute() else root / where
    return found


def _commit(path: pathlib.Path, short: bool = False) -> str:
    args = ['rev-parse', *(['--short=12'] if short else []), 'HEAD']
    run = subprocess.run(['git', *args], cwd=path, capture_output=True, text=True)
    return run.stdout.strip() if run.returncode == 0 else ''


def _dirty(path: pathlib.Path) -> bool:
    run = subprocess.run(['git', 'status', '--porcelain'], cwd=path, capture_output=True, text=True)
    return run.returncode == 0 and bool(run.stdout.strip())


def _kernel_skill(prompts: pathlib.Path, cache: pathlib.Path) -> Skill:
    template = prompts / 'kernel' / 'skills' / 'kernel.md'
    try:
        text = template.read_text()
    except OSError as ex:
        raise SkillError(f'no kernel skill template ({ex.strerror})') from ex
    directory = cache / 'kernel'
    directory.mkdir(parents=True, exist_ok=True)
    (directory / SKILL_FILE).write_text(text.replace(PLACEHOLDER, str(prompts / 'kernel')))
    return parse_skill(directory)


def guidance_skills(
    found: Mapping[str, pathlib.Path], cache: pathlib.Path
) -> tuple[list[Skill], list[str]]:
    """The skills the guidance sources provide (generated ones under *cache*), and a
    problem per source that is missing or malformed."""
    skills: list[Skill] = []
    problems: list[str] = []
    for sid, path in found.items():
        try:
            if not path.is_dir():
                raise SkillError(f'{path} is not a checkout')
            if sid == 'review-prompts':
                skill = _kernel_skill(path, cache)
                root = path / 'kernel'
            elif sid == 'kernel-style':
                skill = parse_skill(path / 'skills' / 'kernel-style')
                root = path
            else:
                raise SkillError('unknown guidance source')
        except SkillError as ex:
            problems.append(f'guidance {sid}: {ex}')
            continue
        version = _commit(path, short=True)
        skills.append(Skill(skill.id, skill.description, skill.path, root, sid, version))
    return skills, problems


def provenance(skills: Iterable[Skill], chosen: Iterable[str]) -> dict[str, str]:
    """{source: commit} of the guidance a session was given; `-dirty` when the checkout
    has changes."""
    wanted = set(chosen)
    found: dict[str, str] = {}
    for skill in skills:
        if skill.source and skill.id in wanted and skill.root is not None:
            checkout = skill.root if skill.source == 'kernel-style' else skill.root.parent
            commit = _commit(checkout)
            found[skill.source] = commit + ('-dirty' if commit and _dirty(checkout) else '')
    return found

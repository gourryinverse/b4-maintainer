# SPDX-License-Identifier: GPL-2.0-or-later
"""Register project checkouts in workspace.toml with workspace-local b4 state.

Checks everything before changing anything: the checkout's b4 settings are
set and b4 enrolls it before the project is appended to workspace.toml, so
a failure never leaves a registered project without b4 state.
"""

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tomllib
from typing import Optional, Sequence

from b4.review.tracking import get_repo_identifier

from .workspace import Project, Workspace, make_ref

CONFIG_NAME = 'workspace.toml'
B4_DATA = ('var', 'b4', 'data')
B4_CACHE = ('var', 'b4', 'cache')


class SetupError(Exception):
    pass


def load_config(root: pathlib.Path) -> tuple[str, Workspace]:
    path = root / CONFIG_NAME
    text = path.read_text() if path.exists() else ''
    try:
        return text, Workspace.from_config(tomllib.loads(text))
    except (tomllib.TOMLDecodeError, KeyError, TypeError, ValueError) as e:
        raise SetupError(f'{path}: invalid workspace configuration: {e}') from e


def project_toml(project: Project) -> str:
    def s(value: str) -> str:
        return json.dumps(value, ensure_ascii=False)

    lines = ['[[projects]]', f'id = {s(project.id)}', f'path = {s(project.path)}']
    if project.label != project.id:
        lines.append(f'label = {s(project.label)}')
    if project.short != project.id[:4]:
        lines.append(f'short = {s(project.short)}')
    lines.append('lists = [' + ', '.join(s(lst) for lst in project.lists) + ']')
    if project.refs:
        lines.append('refs = [')
        for r in project.refs:
            extra = f', view = {s(r.view)}' if r.view else ''
            if r.ends_review != make_ref(r.label, r.name, r.authority).ends_review:
                extra += f', ends_review = {str(r.ends_review).lower()}'
            lines.append(
                f'  {{ label = {s(r.label)}, name = {s(r.name)}, '
                f'authority = {s(r.authority.name.lower())}{extra} }},'
            )
        lines.append(']')
    return '\n'.join(lines) + '\n'


def _git(checkout: pathlib.Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ['git', '-C', str(checkout), *args], capture_output=True, text=True, check=False
    )


def _toplevel(checkout: pathlib.Path) -> pathlib.Path:
    result = _git(checkout, 'rev-parse', '--show-toplevel') if checkout.is_dir() else None
    if result is None or result.returncode != 0:
        raise SetupError(f'{checkout}: not a git checkout')
    top = pathlib.Path(result.stdout.strip()).resolve()
    if top != checkout.resolve():
        raise SetupError(f'{checkout}: not a git checkout (top level is {top})')
    return top


def _dir_setting(root: pathlib.Path, top: pathlib.Path, parts: Sequence[str]) -> str:
    target = root.joinpath(*parts)
    if top.is_relative_to(root):
        # Relative to the checkout, so the workspace can move as a whole
        return os.path.relpath(target, top)
    return str(target)


def default_b4_command() -> list[str]:
    venv_bin = str(pathlib.Path(sys.executable).parent)
    found = shutil.which('b4', path=venv_bin) or shutil.which('b4')
    if not found:
        raise SetupError('b4 is not installed; see deps/README.md')
    return [found]


def add_project(
    root: pathlib.Path,
    project: Project,
    b4_command: Optional[Sequence[str]] = None,
    sashiko_url: Optional[str] = None,
) -> None:
    root = root.resolve()
    text, workspace = load_config(root)
    if workspace.project(project.id):
        raise SetupError(f'project {project.id!r} is already registered in {CONFIG_NAME}')

    top = _toplevel(root / project.path)

    sep = '' if not text or text.endswith('\n\n') else ('\n' if text.endswith('\n') else '\n\n')
    new_text = text + sep + project_toml(project)
    try:
        if Workspace.from_config(tomllib.loads(new_text)).project(project.id) != project:
            raise ValueError('project did not round-trip')
    except (tomllib.TOMLDecodeError, KeyError, TypeError, ValueError) as e:
        raise SetupError(f'{root / CONFIG_NAME}: cannot append project: {e}') from e

    settings = {
        'b4.data-dir': _dir_setting(root, top, B4_DATA),
        'b4.cache-dir': _dir_setting(root, top, B4_CACHE),
    }
    if sashiko_url:
        settings['b4.sashiko-url'] = sashiko_url
    for key, value in settings.items():
        current = _git(top, 'config', '--local', '--get', key).stdout.strip()
        if current and current != value:
            raise SetupError(f'{top}: {key} is already set to {current!r}, expected {value!r}')

    enrolled = get_repo_identifier(str(top))
    if enrolled and enrolled != project.id:
        raise SetupError(f'{top}: already enrolled in b4 as {enrolled!r}')

    command = list(b4_command) if b4_command else default_b4_command()

    for key, value in settings.items():
        result = _git(top, 'config', '--local', key, value)
        if result.returncode != 0:
            raise SetupError(f'{top}: cannot set {key}: {result.stderr.strip()}')

    result = subprocess.run(
        [*command, 'review', 'enroll', '-i', project.id],
        cwd=top,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or get_repo_identifier(str(top)) != project.id:
        detail = (result.stderr or result.stdout).strip().splitlines()[-1:] or ['no output']
        raise SetupError(f'b4 review enroll failed: {detail[0]}')

    config_path = root / CONFIG_NAME
    tmp = config_path.with_name(f'.{CONFIG_NAME}.tmp')
    tmp.write_text(new_text)
    tmp.replace(config_path)

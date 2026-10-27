# SPDX-License-Identifier: GPL-2.0-or-later
"""Register project checkouts in workspace.toml with workspace-local b4 state, and
recreate a workspace on another machine from its state repository (setup).

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
from typing import Any, Mapping, Optional, Sequence

from b4.review.tracking import get_repo_identifier

from .workspace import Project, Workspace, make_ref, merge_config

CONFIG_NAME = 'workspace.toml'
B4_DATA = ('var', 'b4', 'data')
B4_CACHE = ('var', 'b4', 'cache')


class SetupError(Exception):
    pass


def config_path(root: pathlib.Path) -> pathlib.Path:
    """The workspace's configuration: a root workspace.toml, else the synced one in state/."""
    local = root / CONFIG_NAME
    if local.is_file() or not (root / 'state').is_dir():
        return local
    return root / 'state' / CONFIG_NAME


def local_config_path(root: pathlib.Path) -> pathlib.Path:
    """This machine's overrides of the configuration: var/workspace.toml, never synced."""
    return root / 'var' / CONFIG_NAME


def read_config(root: pathlib.Path) -> dict[str, Any]:
    """The configuration in effect: config_path()'s, with this machine's overrides."""
    config = tomllib.loads(config_path(root).read_text())
    local = local_config_path(root)
    if local.is_file():
        try:
            overrides = tomllib.loads(local.read_text())
        except tomllib.TOMLDecodeError as e:
            raise ValueError(f'var/{CONFIG_NAME}: {e}') from e
        config = merge_config(config, overrides)
    return config


def has_config(root: pathlib.Path) -> bool:
    return config_path(root).is_file()


def load_config(root: pathlib.Path) -> tuple[str, Workspace]:
    path = config_path(root)
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
    if project.subject_tags:
        lines.append('subject_tags = [' + ', '.join(s(tag) for tag in project.subject_tags) + ']')
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
        raise SetupError(f'{config_path(root)}: cannot append project: {e}') from e

    extra = {'b4.sashiko-url': sashiko_url} if sashiko_url else {}
    _enroll(root, top, project.id, extra, b4_command)

    path = config_path(root)
    tmp = path.with_name(f'.{CONFIG_NAME}.tmp')
    tmp.write_text(new_text)
    tmp.replace(path)


def _enroll(
    root: pathlib.Path,
    top: pathlib.Path,
    identifier: str,
    extra: Mapping[str, str],
    b4_command: Optional[Sequence[str]],
) -> None:
    """Point the checkout's b4 directories at the workspace, set *extra*, and enroll it."""
    settings = {
        'b4.data-dir': _dir_setting(root, top, B4_DATA),
        'b4.cache-dir': _dir_setting(root, top, B4_CACHE),
        **extra,
    }
    for key, value in settings.items():
        current = _git(top, 'config', '--local', '--get', key).stdout.strip()
        if current and current != value:
            raise SetupError(f'{top}: {key} is already set to {current!r}, expected {value!r}')

    enrolled = get_repo_identifier(str(top))
    if enrolled and enrolled != identifier:
        raise SetupError(f'{top}: already enrolled in b4 as {enrolled!r}')

    command = list(b4_command) if b4_command else default_b4_command()

    for key, value in settings.items():
        result = _git(top, 'config', '--local', key, value)
        if result.returncode != 0:
            raise SetupError(f'{top}: cannot set {key}: {result.stderr.strip()}')

    result = subprocess.run(
        [*command, 'review', 'enroll', '-i', identifier],
        cwd=top,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or get_repo_identifier(str(top)) != identifier:
        detail = (result.stderr or result.stdout).strip().splitlines()[-1:] or ['no output']
        raise SetupError(f'b4 review enroll failed: {detail[0]}')


# Recreating checkouts: what `checkout.json` in the state repository records.

_LOCAL_B4 = ('b4.data-dir', 'b4.cache-dir')  # Workspace paths, set by setup itself.


def describe_checkout(top: pathlib.Path) -> dict[str, Any]:
    """Remotes, the current branch and its upstream, and local b4 settings."""
    listing = _git(top, 'config', '--local', '--list', '-z').stdout
    pairs = [entry.split('\n', 1) for entry in listing.split('\0') if entry]
    remotes: dict[str, dict[str, Any]] = {}
    config: dict[str, list[str]] = {}
    for pair in pairs:
        key, value = pair[0], pair[1] if len(pair) > 1 else ''
        config.setdefault(key, []).append(value)
        parts = key.split('.')
        if parts[0] == 'remote' and len(parts) >= 3:
            name, field = '.'.join(parts[1:-1]), parts[-1]
            remote = remotes.setdefault(name, {'name': name})
            if field == 'url':
                remote['url'] = value
            elif field == 'pushurl':
                remote['pushurl'] = value
            elif field == 'fetch':
                remote.setdefault('fetch', []).append(value)
            elif field == 'partialclonefilter':
                remote['filter'] = value
    branch = _git(top, 'symbolic-ref', '--short', '-q', 'HEAD').stdout.strip()
    desc: dict[str, Any] = {'branch': branch}
    remote_of = config.get(f'branch.{branch}.remote', [''])[-1]
    merge = config.get(f'branch.{branch}.merge', [''])[-1]
    if branch and remote_of and merge:
        desc['upstream'] = {'remote': remote_of, 'merge': merge}
    desc['remotes'] = [r for r in remotes.values() if 'url' in r]
    desc['b4'] = {
        key: values[-1]
        for key, values in sorted(config.items())
        if key.startswith('b4.') and key not in _LOCAL_B4
    }
    return desc


def clone_checkout(path: pathlib.Path, desc: Mapping[str, Any], verbose: bool = False) -> None:
    """Recreate a checkout: remotes, refspecs and partial-clone filters as recorded, then the
    recorded branch on its upstream. Fetches; never pushes."""

    def run(*args: str, live: bool = False) -> None:
        result = subprocess.run(
            ['git', '-C', str(path), *args],
            stdin=subprocess.DEVNULL,
            capture_output=not live,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise SetupError(f'{path}: git {args[0]} failed: {(result.stderr or "").strip()}')

    branch = desc.get('branch') or 'master'
    path.mkdir(parents=True, exist_ok=False)
    run('init', '-q', '-b', branch)
    for remote in desc.get('remotes', []):
        name = remote['name']
        run('remote', 'add', name, remote['url'])
        if remote.get('pushurl'):
            run('config', f'remote.{name}.pushurl', remote['pushurl'])
        if remote.get('fetch'):
            run('config', '--unset-all', f'remote.{name}.fetch')
            for refspec in remote['fetch']:
                run('config', '--add', f'remote.{name}.fetch', refspec)
        if remote.get('filter'):
            run('config', f'remote.{name}.promisor', 'true')
            run('config', f'remote.{name}.partialclonefilter', remote['filter'])
    for remote in desc.get('remotes', []):
        run('fetch', *([] if verbose else ['-q']), remote['name'], live=verbose)
    upstream = desc.get('upstream')
    if upstream:
        start = f'{upstream["remote"]}/{upstream["merge"].removeprefix("refs/heads/")}'
        run('checkout', '-q', '-B', branch, start)
        run('config', f'branch.{branch}.remote', upstream['remote'])
        run('config', f'branch.{branch}.merge', upstream['merge'])


def _remote_differences(top: pathlib.Path, desc: Mapping[str, Any]) -> list[str]:
    have = {r['name']: r for r in describe_checkout(top)['remotes']}
    notes = []
    for remote in desc.get('remotes', []):
        mine = have.get(remote['name'])
        if mine is None:
            notes.append(f'remote {remote["name"]} is missing ({remote["url"]})')
        elif mine.get('url') != remote['url']:
            notes.append(f'remote {remote["name"]} is {mine.get("url")}, recorded {remote["url"]}')
    return notes


def setup_workspace(
    root: pathlib.Path, b4_command: Optional[Sequence[str]] = None, verbose: bool = False
) -> list[str]:
    """Recreate the workspace from state/: clone missing checkouts, enroll them with
    workspace b4 state, and import the synced b4 state. Returns a report, line by line."""
    from .adapters import b4state, statesync

    root = root.resolve()
    _, workspace = load_config(root)
    state = root / 'state'
    report: list[str] = []
    enrolled: list[pathlib.Path] = []
    for project in workspace.projects:
        top = root / project.path
        recorded = state / 'projects' / project.id / 'checkout.json'
        desc: dict[str, Any] = json.loads(recorded.read_text()) if recorded.is_file() else {}
        if not top.exists():
            if not desc:
                report.append(f'{project.id}: no checkout and no recorded checkout; skipped')
                continue
            if verbose:
                print(f'{project.id}: cloning into {project.path}', flush=True)
            clone_checkout(top, desc, verbose=verbose)
            report.append(f'{project.id}: cloned into {project.path}')
        else:
            _toplevel(top)
            for note in _remote_differences(top, desc):
                report.append(f'{project.id}: {note}; checkout left as is')
        _enroll(root, top, project.id, desc.get('b4', {}), b4_command)
        enrolled.append(top)
        records = {}
        for path in sorted((state / 'projects' / project.id / 'series').glob('*.json')):
            rec = json.loads(path.read_text())
            records[rec['change_id']] = rec
        b4state.import_series(top, project.id, records)
        report.append(f'{project.id}: enrolled; {len(records)} tracked series imported')
    flags = state / 'b4' / 'flags.tsv'
    if enrolled and flags.is_file():
        lines = statesync.parse_flags(flags.read_bytes())
        entries = [{'msgid': f.msgid, 'msg_date': f.date or None, 'flags': f.flags} for f in lines]
        b4state.add_flags(enrolled[0], entries)
        report.append(f'{len(entries)} message flags imported')
    return report

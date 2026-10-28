# SPDX-License-Identifier: GPL-2.0-or-later
"""State carried between machines by hand: a tar.gz of state/'s layout (configuration,
workbenches, client state, tracked series, flags, how each checkout was cloned).

Private data stays out unless asked for: the machine-local var/workspace.toml, projects
only it defines, the remotes its base_refs and track_branches name (their URLs too),
and workbenches or checkouts on private bases or tracked branches.
"""

import dataclasses
import fnmatch
import io
import json
import pathlib
import tarfile
import tomllib
from typing import Any, Mapping

from ..workspace import Workspace

PRIVATE_CONFIG = 'private/workspace.toml'  # var/workspace.toml, with --private only.
MANIFEST = 'bundle.json'
VERSION = 1


@dataclasses.dataclass(frozen=True)
class Private:
    """What an export leaves out: projects only the machine-local configuration defines,
    and per project the remotes and ref globs its base_refs and track_branches name."""

    projects: frozenset[str] = frozenset()
    remotes: Mapping[str, frozenset[str]] = dataclasses.field(default_factory=dict)
    globs: Mapping[str, tuple[str, ...]] = dataclasses.field(default_factory=dict)

    def ref(self, project: str, name: str) -> bool:
        """Whether a ref or branch name is private: a match of a private glob (as named,
        or as the local branch of a remote one), or on a private remote."""
        if not name:
            return False
        for glob in self.globs.get(project, ()):
            local = glob.split('/', 1)[1] if '/' in glob else glob
            if fnmatch.fnmatchcase(name, glob) or fnmatch.fnmatchcase(name, local):
                return True
        return name.split('/', 1)[0] in self.remotes.get(project, frozenset())


def private_parts(shared: Workspace, effective: Workspace) -> Private:
    """*shared*: the configuration in state/; *effective*: with var/workspace.toml."""
    known = {p.id for p in shared.projects}
    remotes: dict[str, frozenset[str]] = {}
    globs: dict[str, tuple[str, ...]] = {}
    for project in effective.projects:
        named = (*project.base_refs, *project.track_branches)
        globs[project.id] = named
        remotes[project.id] = frozenset(g.split('/', 1)[0] for g in named if '/' in g)
    private = frozenset(p.id for p in effective.projects if p.id not in known)
    return Private(private, remotes, globs)


def public_checkout(project: str, desc: dict[str, Any], private: Private) -> dict[str, Any]:
    """A checkout's record without its private remotes or a private current branch."""
    out = dict(desc)
    out['remotes'] = [
        r for r in desc.get('remotes', []) if r.get('name') not in private.remotes.get(project, ())
    ]
    upstream = desc.get('upstream') or {}
    if private.ref(project, desc.get('branch', '')) or upstream.get(
        'remote'
    ) in private.remotes.get(project, ()):
        out.pop('branch', None)
        out.pop('upstream', None)
    return out


def private_bench(text: str, private: Private) -> bool:
    """Whether a workbench uses anything private: a private project, or a base or branch
    item on a private ref."""
    try:
        data = tomllib.loads(text)
    except ValueError:
        return False
    for entry in data.get('entry', []):
        project = str(entry.get('project', ''))
        if project in private.projects or private.ref(project, str(entry.get('base', ''))):
            return True
        for item in entry.get('items', []):
            branch = str(item.get('branch', '')) if isinstance(item, dict) else ''
            if branch.startswith('tree:'):
                continue  # A session tree's branch: local to this machine anyway.
            if private.ref(project, branch):
                return True
    return False


def write_bundle(path: pathlib.Path, files: Mapping[str, bytes], private: bool) -> None:
    manifest = json.dumps({'version': VERSION, 'private': private}).encode()
    with tarfile.open(path, 'w:gz') as tar:
        for name, data in sorted({MANIFEST: manifest, **files}.items()):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o600 if private else 0o644
            tar.addfile(info, io.BytesIO(data))


def read_bundle(path: pathlib.Path) -> dict[str, bytes]:
    """The bundle's files (relative paths only; anything else is refused)."""
    files: dict[str, bytes] = {}
    with tarfile.open(path, 'r:gz') as tar:
        for member in tar.getmembers():
            name = member.name
            parts = pathlib.PurePosixPath(name).parts
            if not member.isfile() or name.startswith('/') or '..' in parts:
                raise ValueError(f'{path}: unexpected entry {name!r}')
            extracted = tar.extractfile(member)
            if extracted is not None:
                files[name] = extracted.read()
    manifest = json.loads(files.pop(MANIFEST, b'{}') or b'{}')
    if manifest.get('version') != VERSION:
        raise ValueError(f'{path}: not a b4-maintainer state export')
    return files

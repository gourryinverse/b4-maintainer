# SPDX-License-Identifier: GPL-2.0-or-later
"""Tracked upstream branches (a project's refs), as Settings (F6) edits them.

workspace.toml is the maintainer's own file: a write rewrites only one project's
refs = [...] (one ref per line) and leaves every other byte as it was, then must read
back as the refs asked for, or nothing is written. The file holding the project's refs
is the one written: this machine's var/workspace.toml when it sets them.
"""

import json
import os
import pathlib
import re
import tomllib
from dataclasses import dataclass
from typing import Any, Sequence

from .adapters import remotes
from .model import Authority
from .projectsetup import config_path, local_config_path
from .workspace import Project, Ref, Workspace

_KEY = re.compile(r'[ \t]*refs[ \t]*=[ \t]*')
_ID = re.compile(r'[ \t]*id[ \t]*=')


def ref_toml(ref: Ref) -> str:
    """One ref as an inline table, with only what differs from the defaults."""
    parts = [
        f'label = {json.dumps(ref.label)}',
        f'name = {json.dumps(ref.name)}',
        f'authority = {json.dumps(ref.authority.name.lower())}',
    ]
    if ref.view:
        parts.append(f'view = {json.dumps(ref.view)}')
    if ref.ends_review != (ref.authority == Authority.MAINLINE):
        parts.append(f'ends_review = {str(ref.ends_review).lower()}')
    return '{ ' + ', '.join(parts) + ' }'


def refs_text(refs: Sequence[Ref]) -> str:
    if not refs:
        return 'refs = []'
    return 'refs = [\n' + ''.join(f'  {ref_toml(r)},\n' for r in refs) + ']'


def _projects(path: pathlib.Path) -> list[dict[str, Any]]:
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return []
    return [p for p in data.get('projects', []) if isinstance(p, dict)]


def refs_file(root: pathlib.Path, project: str) -> pathlib.Path:
    """The file whose [[projects]] entry for *project* sets its refs: this machine's
    overrides when they do (or define the project alone), else the configuration."""
    local, main = local_config_path(root), config_path(root)
    mine = [p for p in _projects(local) if p.get('id') == project]
    if any('refs' in p for p in mine):
        return local
    if mine and not any(p.get('id') == project for p in _projects(main)):
        return local
    return main


def _scan(text: str) -> tuple[list[int], dict[int, int]]:
    """Offsets of the lines that start outside any value (bracket depth 0, no open
    string), and each '[' value's closing offset (just past its ']')."""
    tops, closes, stack = [0], {}, []
    i, n, depth = 0, len(text), 0
    while i < n:
        c = text[i]
        if c == '#':
            i = text.find('\n', i)
            i = n if i < 0 else i
            continue
        if c in '"\'':
            triple = text.startswith(c * 3, i)
            end = text.find(c * 3, i + 3) + 3 if triple else i + 1
            if not triple:
                while end < n and text[end] != c and text[end] != '\n':
                    end += 2 if (c == '"' and text[end] == '\\') else 1
                end += 1
            i = max(end, i + 1)
            continue
        if c == '[' and depth > 0 or (c == '[' and not _at_line_start(text, i)):
            stack.append(i)
            depth += 1
        elif c == ']' and stack:
            closes[stack.pop()] = i + 1
            depth -= 1
        elif c == '\n' and depth == 0:
            tops.append(i + 1)
        i += 1
    return tops, closes


def _at_line_start(text: str, i: int) -> bool:
    """Whether only blanks come before *i* on its line (a table header's '[')."""
    start = text.rfind('\n', 0, i) + 1
    return not text[start:i].strip()


def _splice(text: str, project: str, refs: Sequence[Ref]) -> str:
    tops, closes = _scan(text)
    headers = [t for t in tops if text[t:].lstrip(' \t').startswith('[')]
    for k, start in enumerate(headers):
        line = text[start : text.find('\n', start) if '\n' in text[start:] else len(text)]
        if line.strip().split('#', 1)[0].strip() != '[[projects]]':
            continue
        end = headers[k + 1] if k + 1 < len(headers) else len(text)
        body = text[start:end]
        try:
            entry = tomllib.loads(body.split('\n', 1)[1] if '\n' in body else '')
        except tomllib.TOMLDecodeError:
            continue
        if entry.get('id') != project:
            continue
        for top in (t for t in tops if start < t < end):
            key = _KEY.match(text, top)
            if key is None:
                continue
            opening = key.end()
            if text[opening : opening + 1] != '[' or opening not in closes:
                raise ValueError(f'{project}: its refs are not a [...] list')
            return text[:top] + refs_text(refs) + text[closes[opening] :]
        # No refs yet: after the table's last line that is not blank.
        last = start + len(body.rstrip('\n'))
        return text[:last] + '\n' + refs_text(refs) + text[last:]
    raise ValueError(f'no [[projects]] entry with id {project!r}')


def _check(refs: Sequence[Ref]) -> None:
    labels = [r.label for r in refs]
    if any(len(lb) != 1 for lb in labels) or len(set(labels)) != len(labels):
        raise ValueError('each tracked branch needs its own one-character label')
    if len({r.name for r in refs}) != len(refs):
        raise ValueError('a branch is tracked twice')
    if sum(r.authority == Authority.MAINLINE for r in refs) > 1:
        raise ValueError('only one branch can be mainline')


def write_refs(root: pathlib.Path, project: str, refs: Sequence[Ref]) -> pathlib.Path:
    """*project*'s tracked branches become *refs*, in the file holding them; that file."""
    from .projectsetup import read_config

    _check(refs)
    path = refs_file(root, project)
    if path.suffix != '.toml':
        raise ValueError(f'{path.name}: only TOML configuration is edited here')
    text = path.read_text() if path.is_file() else ''
    new = _splice(text, project, refs)
    try:
        tomllib.loads(new)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f'{path}: the edit would not parse ({e})') from e
    tmp = path.with_name(f'.{path.name}.tmp')
    tmp.write_text(new)
    os.replace(tmp, path)
    try:
        found = Workspace.from_config(read_config(root)).project(project)
        ok = found is not None and found.refs == tuple(refs)
    except (ValueError, KeyError, TypeError, tomllib.TOMLDecodeError):
        ok = False
    if not ok:
        tmp.write_text(text)
        os.replace(tmp, path)
        raise ValueError(f'{path}: the edit did not read back as asked; left unchanged')
    return path


@dataclass(frozen=True)
class Upstream:
    """A tracked branch of a project: its ref, and the remote it is fetched from ('' for
    a local branch, such as a maintainer base)."""

    project: str
    ref: Ref
    remote: str = ''
    url: str = ''


class UpstreamService:
    """Track, change, and untrack a workspace's upstream branches (blocking: git and
    the network; callers run it off the UI thread)."""

    def __init__(self, root: pathlib.Path) -> None:
        self.root = pathlib.Path(root)

    def _workspace(self) -> Workspace:
        from .projectsetup import read_config

        return Workspace.from_config(read_config(self.root))

    def _project(self, project: str) -> Project:
        found = self._workspace().project(project)
        if found is None:
            raise ValueError(f'no project {project!r}')
        return found

    def checkout(self, project: str) -> pathlib.Path:
        return self.root / self._project(project).path

    def listing(self) -> list[Upstream]:
        found = []
        for project in self._workspace().projects:
            checkout = self.root / project.path
            known = remotes.remotes(checkout) if (checkout / '.git').exists() else {}
            for ref in project.refs:
                remote = ref.name.split('/', 1)[0] if '/' in ref.name else ''
                if remote in known:
                    found.append(Upstream(project.id, ref, remote, known[remote]))
                else:
                    found.append(Upstream(project.id, ref))
        return found

    def remotes(self, project: str) -> dict[str, str]:
        return remotes.remotes(self.checkout(project))

    def heads(self, project: str, where: str) -> list[str]:
        return remotes.heads(self.checkout(project), where)

    def add(self, project: str, remote: str, url: str, branch: str, ref: Ref) -> str:
        """Fetch *branch* from *remote* (at *url*), then track it as *ref*."""
        current = self._project(project).refs
        _check([*current, ref])  # Before the network: a clash needs no fetch.
        checkout = self.checkout(project)
        had = remote in remotes.remotes(checkout) and branch in remotes.tracked(checkout, remote)
        remotes.track(checkout, remote, url, branch)
        try:
            write_refs(self.root, project, [*current, ref])
        except Exception:
            if not had:
                remotes.untrack(checkout, remote, branch)
            raise
        return f'Tracking {ref.name} ({ref.authority.name.lower()})'

    def update(self, project: str, name: str, ref: Ref) -> str:
        """The tracked branch *name* counts as *ref* (its label, authority, view, ends review)."""
        current = self._project(project).refs
        if name not in [r.name for r in current]:
            raise ValueError(f'{name} is not tracked')
        write_refs(self.root, project, [ref if r.name == name else r for r in current])
        return f'{name} updated'

    def remove(self, project: str, name: str) -> str:
        """Untrack *name*; its fetch line goes, and its remote once it fetches nothing."""
        found = self._project(project)
        bases = [b.branch for b in found.bases if b.upstream == name]
        if bases:
            raise ValueError(f'{name} stays: the maintainer base {", ".join(bases)} builds on it')
        write_refs(self.root, project, [r for r in found.refs if r.name != name])
        checkout = self.root / found.path
        remote, _, branch = name.partition('/')
        if not branch or remote not in remotes.remotes(checkout):
            return f'No longer tracking {name}'
        if remotes.untrack(checkout, remote, branch):
            return f'No longer tracking {name}; remote {remote} removed (nothing else used it)'
        return f'No longer tracking {name}'

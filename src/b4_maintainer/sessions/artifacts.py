# SPDX-License-Identifier: GPL-2.0-or-later
"""A session's artifact manifest: `out/artifacts.toml`, written by the agent (see the
b4m-branches skill), read strictly by the client.

Each `[[artifact]]` names one thing the session produced: a finding, commits on a
branch of its tree (pull-ahead, fixup, test), or a file under `out/` (results,
reproducer, analysis, diff: a proposed change from a session without a tree). An entry that breaks a rule is reported with the reason and
skipped; the others still load. Model output is data: nothing here runs it.
"""

import pathlib
import tomllib
from dataclasses import dataclass
from typing import Any, Collection, Optional

MANIFEST = 'artifacts.toml'
KINDS = ('finding', 'pull-ahead', 'fixup', 'test', 'results', 'reproducer', 'analysis', 'diff')
FILE_KINDS = frozenset({'diff'})  # A proposed change without a tree: a diff file.
COMMIT_KINDS = frozenset({'pull-ahead', 'fixup', 'test'})
KEYS = frozenset({'kind', 'title', 'summary', 'branch', 'path', 'answers', 'tested'})


@dataclass(frozen=True)
class Artifact:
    kind: str
    title: str
    summary: str
    branch: str = ''  # Commits: a branch of the session tree.
    path: Optional[pathlib.Path] = None  # Files: under out/.
    answers: str = ''  # The finding it answers, if any.
    tested: str = ''  # How it was tested.


def _artifact(raw: Any, out: pathlib.Path, branches: Collection[str]) -> Artifact:
    if not isinstance(raw, dict):
        raise ValueError('not a table')
    for key in raw:
        if key not in KEYS:
            raise ValueError(f'unknown key {key!r}')
    for key, value in raw.items():
        if not isinstance(value, str):
            raise ValueError(f'{key} must be text')
    kind = raw.get('kind', '')
    if kind not in KINDS:
        raise ValueError(f'unknown kind {kind!r} (one of {", ".join(KINDS)})')
    for key in ('title', 'summary'):
        if not raw.get(key, '').strip():
            raise ValueError(f'{key} is required')
    branch = raw.get('branch', '')
    if kind in COMMIT_KINDS and not branch:
        raise ValueError(f'a {kind} needs a branch')
    if kind in FILE_KINDS and not raw.get('path'):
        raise ValueError(f'a {kind} needs a path')
    if branch and branch not in branches:
        raise ValueError(f'no branch {branch} in the session tree')
    path = None
    if raw.get('path'):
        path = (out.parent / raw['path']).resolve()
        if not path.is_relative_to(out.resolve()):
            raise ValueError(f'path {raw["path"]} is outside out/')
        if not path.exists():
            raise ValueError(f'path {raw["path"]} does not exist')
    return Artifact(
        kind,
        raw['title'].strip(),
        raw['summary'].strip(),
        branch,
        path,
        raw.get('answers', ''),
        raw.get('tested', ''),
    )


def load_artifacts(
    out: pathlib.Path, branches: Collection[str]
) -> tuple[list[Artifact], list[str]]:
    """The manifest's valid artifacts, and a problem per entry that is not. *branches*:
    the session tree's branches (empty for a session without one)."""
    path = out / MANIFEST
    if not path.exists():
        return [], []
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, ValueError) as ex:
        return [], [f'{MANIFEST}: {ex}']
    entries = data.get('artifact', [])
    if not isinstance(entries, list):
        return [], [f'{MANIFEST}: artifact must be an array of tables ([[artifact]])']
    found, problems = [], []
    for n, raw in enumerate(entries, 1):
        try:
            found.append(_artifact(raw, out, branches))
        except ValueError as ex:
            title = raw.get('title', '') if isinstance(raw, dict) else ''
            problems.append(f'{MANIFEST} artifact {n}{f" ({title})" if title else ""}: {ex}')
    return found, problems

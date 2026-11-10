# SPDX-License-Identifier: GPL-2.0-or-later
"""Kept artifacts, var/kept/: session output saved apart from its workbench, for when the
work comes back (a pulled-ahead fix cleaned up and re-validated with its reproducer).

Each kept artifact is a folder: kept.json (what it is, where it came from, what it goes
with) and a copy of its file, or of the folder the file belongs to (a reproducer's
README with its sources and scripts: out/<kind folder>/<name>/). It goes with a commit
(found again after a rebase by its patch-id, else its subject) or sits on a named shelf.
Removing a workbench and its sessions leaves it. This machine only: var/ is not synced.
"""

import dataclasses
import datetime
import json
import os
import pathlib
import secrets
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional, Sequence

KEPT = pathlib.Path('var') / 'kept'
RECORD = 'kept.json'
MAX_BYTES = 50_000_000  # A larger file or folder is refused.
SHOW_LINES = 400


class KeptError(RuntimeError):
    pass


@dataclass(frozen=True)
class Kept:
    id: str
    kind: str  # The manifest's: reproducer, results, analysis, finding, diff, …
    title: str
    summary: str = ''
    tested: str = ''
    name: str = ''  # Its copy, in its folder ('' for a finding without a file).
    project: str = ''
    commit: str = ''  # The commit it goes with, as it was kept…
    subject: str = ''
    patch_id: str = ''
    shelf: str = ''  # …else the shelf it sits on.
    session: str = ''
    session_name: str = ''
    workbench: str = ''
    source: str = ''  # Where the copy came from.
    kept_at: str = ''
    folder: Optional[pathlib.Path] = dataclasses.field(default=None, compare=False)

    @property
    def path(self) -> Optional[pathlib.Path]:
        return self.folder / self.name if self.folder is not None and self.name else None

    def where(self) -> str:
        if self.commit:
            return f'with {self.subject or self.commit[:12]} ({self.project})'
        return f'on the shelf {self.shelf}'


def copy_source(path: pathlib.Path, out: Optional[pathlib.Path] = None) -> pathlib.Path:
    """What keeping *path* copies: the folder it belongs to when it lies in one under a
    kind folder of *out* (out/reproducers/<name>/README.md: that reproducer), else it."""
    if out is not None and path.is_relative_to(out):
        parts = path.relative_to(out).parts
        if len(parts) >= 3:
            return out / parts[0] / parts[1]
    return path


def _size(path: pathlib.Path) -> int:
    if not path.is_dir():
        return path.stat().st_size
    total = 0
    for top, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d != '.git']
        total += sum(os.lstat(os.path.join(top, f)).st_size for f in files)
    return total


def _git(repo: pathlib.Path, *args: str, stdin: str = '') -> str:
    try:
        run = subprocess.run(
            ['git', *args],
            cwd=repo,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ''
    return run.stdout if run.returncode == 0 else ''


def patch_id(repo: pathlib.Path, sha: str) -> str:
    """The commit's stable patch-id ('' when git cannot say): the same change after a
    rebase or a pluck has the same one."""
    diff = _git(repo, 'diff-tree', '-p', '--no-color', sha)
    found = _git(repo, 'patch-id', '--stable', stdin=diff).split()
    return found[0] if found else ''


def subject(repo: pathlib.Path, sha: str) -> str:
    return _git(repo, 'log', '-1', '--format=%s', sha).strip()


class KeptStore:
    """var/kept/ (blocking: copies and git; callers run it off the UI thread)."""

    def __init__(self, folder: pathlib.Path) -> None:
        self.folder = pathlib.Path(folder)

    def items(self) -> list[Kept]:
        found = []
        for record in sorted(self.folder.glob(f'*/{RECORD}')):
            try:
                data = json.loads(record.read_text())
                fields = {f.name for f in dataclasses.fields(Kept)} - {'folder'}
                kept = Kept(**{k: v for k, v in data.items() if k in fields})
            except (OSError, ValueError, TypeError):
                continue
            found.append(dataclasses.replace(kept, folder=record.parent))
        return sorted(found, key=lambda k: (k.kept_at, k.id))

    def get(self, kid: str) -> Optional[Kept]:
        return next((k for k in self.items() if k.id == kid), None)

    def shelves(self) -> list[str]:
        return sorted({k.shelf for k in self.items() if k.shelf})

    def keep(
        self,
        kind: str,
        title: str,
        summary: str = '',
        tested: str = '',
        path: Optional[pathlib.Path] = None,
        out: Optional[pathlib.Path] = None,
        *,
        project: str = '',
        commit: str = '',
        subject: str = '',
        patch_id: str = '',
        shelf: str = '',
        session: str = '',
        session_name: str = '',
        workbench: str = '',
        now: Optional[datetime.datetime] = None,
    ) -> Kept:
        """A copy of *path* (or of its folder, see copy_source) kept with *commit* or on
        *shelf*. The same source kept again is refreshed and goes where asked."""
        if not commit and not shelf.strip():
            raise KeptError('keep it with a commit or on a shelf')
        source = copy_source(path, out) if path is not None else None
        if source is not None:
            if not source.exists():
                raise KeptError(f'{source} does not exist')
            if _size(source) > MAX_BYTES:
                raise KeptError(f'{source.name} is over {MAX_BYTES // 1_000_000} MB')
        origin = str(source) if source is not None else f'{session}:{title}'
        known = next((k for k in self.items() if k.source == origin), None)
        when = now or datetime.datetime.now(datetime.timezone.utc)
        kid = known.id if known is not None else f'{when:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}'
        folder = self.folder / kid
        folder.mkdir(parents=True, exist_ok=True)
        name = ''
        if source is not None:
            name = source.name
            tmp = folder / f'.{name}.tmp'
            _remove(tmp)
            if source.is_dir():
                shutil.copytree(source, tmp, symlinks=True, ignore=shutil.ignore_patterns('.git'))
            else:
                shutil.copy2(source, tmp)
            _remove(folder / name)
            os.replace(tmp, folder / name)
        kept = Kept(
            kid,
            kind,
            title,
            summary,
            tested,
            name,
            project,
            commit,
            subject,
            patch_id,
            '' if commit else shelf.strip(),
            session,
            session_name,
            workbench,
            origin,
            when.isoformat(timespec='seconds'),
        )
        data = {k: v for k, v in dataclasses.asdict(kept).items() if k != 'folder'}
        tmp = folder / f'.{RECORD}.tmp'
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
        os.replace(tmp, folder / RECORD)
        return dataclasses.replace(kept, folder=folder)

    def remove(self, kid: str) -> bool:
        folder = self.folder / kid
        if not kid or '/' in kid or not (folder / RECORD).is_file():
            return False
        shutil.rmtree(folder)
        return True

    def with_commit(self, repo: pathlib.Path, sha: str, title: str = '') -> list[Kept]:
        """What was kept with the commit *sha*: kept with it, or with the same change as
        it was before a rebase or a pluck (its patch-id), else with its subject."""
        withs = [k for k in self.items() if k.commit]
        if not withs:
            return []
        exact = [k for k in withs if sha and k.commit == sha]
        others = [k for k in withs if k not in exact]
        if not others:
            return exact
        mine = patch_id(repo, sha) if any(k.patch_id for k in others) else ''
        title = title or subject(repo, sha)
        same = [
            k for k in others if (mine and k.patch_id == mine) or (title and k.subject == title)
        ]
        return exact + same


def _remove(path: pathlib.Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def describe(kept: Kept) -> str:
    """The right pane for a kept artifact: what it is, where it came from, and its file
    (a folder: its files, then its README's text)."""
    lines = [f'{kept.kind}: {kept.title}', '', kept.summary]
    lines += [f'\nTested: {kept.tested}'] if kept.tested else []
    lines += ['', f'Kept {kept.where()}']
    if kept.commit:
        lines.append(f'  commit {kept.commit[:12]} when kept')
    came = ', '.join(p for p in (kept.session_name or kept.session, kept.workbench) if p)
    lines += [f'From: {came}'] if came else []
    lines += [f'Kept at: {kept.kept_at}'] if kept.kept_at else []
    path = kept.path
    if path is None:
        return '\n'.join(lines)
    lines += [f'Copy: {path}']
    shown = path
    if path.is_dir():
        files = sorted(p for p in path.rglob('*') if p.is_file())
        lines += ['', f'--- {path.name}/', *(f'  {p.relative_to(path)}' for p in files)]
        readme = next((p for p in files if p.name.lower().startswith('readme')), None)
        if readme is None:
            return '\n'.join(lines)
        shown = readme
    try:
        text = shown.read_text(errors='replace')
    except OSError as ex:
        text = f'(cannot read: {ex})'
    lines += ['', f'--- {shown.name}', *text.splitlines()[:SHOW_LINES]]
    return '\n'.join(lines)


def keep_label(kept: Sequence[Kept]) -> str:
    """The Branches pane's section for a commit's kept artifacts."""
    if not kept:
        return ''
    lines = ['', 'Kept with this commit (Workbench tab: Kept artifacts):']
    for k in kept:
        where = f'  {k.path}' if k.path is not None else ''
        lines.append(f'  {k.kind}: {k.title}{where}')
    return '\n'.join(lines)

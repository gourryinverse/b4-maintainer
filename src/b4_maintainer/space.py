# SPDX-License-Identifier: GPL-2.0-or-later
"""A workbench's space, var/workbenches/<id>/: the container its sessions work in.

context/ follows the workbench's recipe and is read live by its sessions: items.md (each
project, its base, and its items, with where each is), mail/ (each series' thread at the
revision named, each discussion's), files/ (each file item), and changes.md (what changed
and when). A manifest (.manifest.json) records what was written from where, so a sync
rewrites only what changed and removes what left the recipe.
"""

import datetime
import fcntl
import json
import os
import pathlib
import re
import shutil
from dataclasses import dataclass
from typing import Sequence

SPACES = pathlib.Path('var') / 'workbenches'
MANIFEST = '.manifest.json'


def space_dir(root: pathlib.Path, wid: str) -> pathlib.Path:
    return pathlib.Path(root) / SPACES / wid


@dataclass(frozen=True)
class SpaceItem:
    """One recipe item as the space shows it."""

    label: str
    kind: str  # series, discussion, branch, file, missing
    source: str = ''  # A thread's mail, or a file's path.
    note: str = ''  # How it reaches trees, or why it is missing.


@dataclass(frozen=True)
class SpaceEntry:
    project: str
    base: str
    items: tuple[SpaceItem, ...] = ()


def _slug(text: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')[:60].rstrip('-') or 'item'


def _signature(path: pathlib.Path) -> list[int]:
    """What says a source changed: size and modification time (newest file of a maildir)."""
    try:
        if path.is_dir():
            files = [p for p in path.rglob('*') if p.is_file()]
            return [len(files), max((p.stat().st_mtime_ns for p in files), default=0)]
        stat = path.stat()
        return [stat.st_size, stat.st_mtime_ns]
    except OSError:
        return [-1, -1]


def _write(path: pathlib.Path, text: str) -> None:
    tmp = path.with_name(f'.{path.name}.tmp')
    tmp.write_text(text)
    os.replace(tmp, path)


def sync_context(
    space: pathlib.Path, label: str, entries: Sequence[SpaceEntry], now: str = ''
) -> list[str]:
    """*space*'s context/ brought in line with the recipe (*entries*): what changed, as
    `added <path> (<label>, <project>)` or `removed …`, also appended to changes.md."""
    context = space / 'context'
    context.mkdir(parents=True, exist_ok=True)
    with open(space / '.lock', 'a') as lock:  # One sync at a time (threads, instances).
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _sync(context, label, entries, now)


def _sync(context: pathlib.Path, label: str, entries: Sequence[SpaceEntry], now: str) -> list[str]:
    try:
        old = json.loads((context / MANIFEST).read_text())
    except (OSError, ValueError):
        old = {}
    wanted: dict[str, dict] = {}
    placed: dict[tuple[int, int], str] = {}
    for e, entry in enumerate(entries):
        for n, item in enumerate(entry.items):
            if item.kind in ('series', 'discussion') and item.source:
                name = f'mail/{entry.project}-{_slug(item.label)}.mbx'
            elif item.kind == 'file' and item.source:
                name = f'files/{pathlib.Path(item.source).name}'
            else:
                continue
            base, k = name, 1
            while name in wanted:
                k += 1
                stem, dot, ext = base.rpartition('.')
                name = f'{stem}-{k}{dot}{ext}'
            wanted[name] = {'source': item.source, 'label': item.label, 'project': entry.project}
            placed[(e, n)] = name
    changes = []
    for name, info in wanted.items():
        target = context / name
        signature = _signature(pathlib.Path(info['source']))
        before = old.get(name)
        if before and before.get('source') == info['source'] and target.exists():
            if before.get('signature') != signature:
                _copy(info['source'], target)  # Its thread grew (new replies): refreshed.
            info['signature'] = signature
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if not _copy(info['source'], target):
            continue
        info['signature'] = signature
        changes.append(f'added {name} ({info["label"]}, {info["project"]})')
    for name, info in old.items():
        if name not in wanted:
            (context / name).unlink(missing_ok=True)
            changes.append(f'removed {name} ({info.get("label", "")}, {info.get("project", "")})')
    for sub in ('mail', 'files'):
        folder = context / sub
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
    _write(context / MANIFEST, json.dumps(wanted, indent=1, sort_keys=True))
    _write(context / 'items.md', _items_text(label, entries, placed))
    if changes:
        now = now or datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
        with (context / 'changes.md').open('a') as f:
            f.write(''.join(f'- {now}: {c}\n' for c in changes))
    return changes


def _copy(source: str, target: pathlib.Path) -> bool:
    """*source* at *target*: a thread as one mbox (as sessions get it), a file as it is."""
    from .sessions.pi import write_thread

    path = pathlib.Path(source).expanduser()
    tmp = target.with_name(f'.{target.name}.tmp')
    try:
        if path.is_dir() or target.suffix == '.mbx':
            if not write_thread(path, tmp):
                return False
        else:
            shutil.copyfile(path, tmp)
        os.replace(tmp, target)
        return True
    except OSError:
        tmp.unlink(missing_ok=True)
        return False


def _items_text(
    label: str, entries: Sequence[SpaceEntry], placed: dict[tuple[int, int], str]
) -> str:
    lines = [
        f'# Workbench: {label}',
        '',
        'This folder is live: it changes when the maintainer edits the workbench (items '
        'added, moved, or removed). changes.md says what changed and when.',
    ]
    for e, entry in enumerate(entries):
        lines += ['', f'## {entry.project} on {entry.base or "the base b4 chooses"}', '']
        if not entry.items:
            lines.append('No items: a tree at its base.')
        for n, item in enumerate(entry.items, 1):
            where = placed.get((e, n - 1))
            parts = [f'{n}. {item.label} ({item.kind})']
            if where:
                parts.append(f'in {where}')
            if item.note:
                parts.append(item.note)
            lines.append(' — '.join(parts))
    return '\n'.join(lines) + '\n'


def remove_space(space: pathlib.Path) -> None:
    """The space, gone (the workbench was removed)."""
    if space.is_dir() and not space.is_symlink():
        shutil.rmtree(space)

# SPDX-License-Identifier: GPL-2.0-or-later
"""Ownership from the project's own get_maintainer.pl; author roles from MAINTAINERS.

get_maintainer.pl owns the F:/X:/N: matching rules, so ownership is asked of
it in patch mode, with a diff naming only the series' paths (new files need
not exist in the tree; K: keyword sections are not matched). Calls run in
parallel during refresh and are cached per path set until MAINTAINERS or the
command changes.
"""

import concurrent.futures
import email.utils
import hashlib
import json
import os
import pathlib
import re
import subprocess
from typing import Iterable, Optional, Sequence

FLAGS = (
    '--no-git',
    '--no-git-fallback',
    '--no-rolestats',
    '--no-l',
    '--no-r',
    '--no-s',
    '--no-keywords',
)
_ROLE = re.compile(r'^([MR]):\s*(.+)$')

Paths = Sequence[str]


def author_roles(maintainers: pathlib.Path) -> dict[str, str]:
    """Map each M:/R: address to 'maintainer' or 'reviewer'; maintainer wins."""
    roles: dict[str, str] = {}
    try:
        text = maintainers.read_text(errors='replace')
    except OSError:
        return roles
    for line in text.splitlines():
        match = _ROLE.match(line)
        if not match:
            continue
        addr = email.utils.parseaddr(match.group(2))[1].lower()
        if not addr:
            continue
        if match.group(1) == 'M':
            roles[addr] = 'maintainer'
        else:
            roles.setdefault(addr, 'reviewer')
    return roles


def _key(paths: Paths) -> str:
    return '\n'.join(sorted(set(paths)))


def _patch(paths: Paths) -> str:
    return ''.join(
        f'diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n@@ -1 +1 @@\n-\n+\n' for p in paths
    )


class OwnerCache:
    def __init__(
        self,
        path: pathlib.Path,
        checkout: pathlib.Path,
        command: Optional[Sequence[str]] = None,
        maintainers: str = 'MAINTAINERS',
        workers: int = 8,
    ) -> None:
        self.path = path
        self.checkout = checkout
        script = checkout / 'scripts' / 'get_maintainer.pl'
        if command is None:
            command = (str(script), *FLAGS) if script.is_file() else ()
        self.command = tuple(command)
        self.maintainers = checkout / maintainers
        self.workers = workers
        self.errors = 0
        self._stamp = self._compute_stamp()
        self._entries: dict[str, list[str]] = {}
        try:
            data = json.loads(path.read_text())
            if data.get('stamp') == self._stamp:
                self._entries = data['entries']
        except (OSError, ValueError, KeyError, TypeError):
            pass

    @property
    def available(self) -> bool:
        return bool(self.command) and self.maintainers.is_file()

    def _compute_stamp(self) -> str:
        digest = hashlib.sha1('\0'.join(self.command).encode())
        try:
            digest.update(self.maintainers.read_bytes())
        except OSError:
            pass
        return digest.hexdigest()

    def get(self, paths: Paths) -> Optional[frozenset[str]]:
        found = self._entries.get(_key(paths))
        return None if found is None else frozenset(found)

    def update(self, path_sets: Iterable[Paths]) -> int:
        """Ask get_maintainer.pl about uncached path sets; return how many were answered."""
        if not self.available:
            return 0
        missing = {_key(p): tuple(sorted(set(p))) for p in path_sets if p}
        missing = {k: v for k, v in missing.items() if k not in self._entries}
        if not missing:
            return 0
        with concurrent.futures.ThreadPoolExecutor(self.workers) as pool:
            results = dict(zip(missing, pool.map(self._run, missing.values())))
        answered = 0
        for key, owners in results.items():
            if owners is None:
                self.errors += 1
                continue
            self._entries[key] = sorted(owners)
            answered += 1
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f'.{self.path.name}.tmp')
        tmp.write_text(json.dumps({'stamp': self._stamp, 'entries': self._entries}))
        os.replace(tmp, self.path)
        return answered

    def _run(self, paths: Paths) -> Optional[set[str]]:
        result = subprocess.run(
            self.command,
            cwd=self.checkout,
            input=_patch(paths),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return None
        owners = set()
        for line in result.stdout.splitlines():
            addr = email.utils.parseaddr(line)[1].lower()
            if '@' in addr:
                owners.add(addr)
        return owners

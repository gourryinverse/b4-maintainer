# SPDX-License-Identifier: GPL-2.0-or-later
"""Tree evidence: which posted patches appear in configured refs.

Matches as b4 ty's auto_locate_series does, minus patch hashes: a commit's
Link:/Message-ID: trailers naming the patch's message ID, else an identical
subject. Patch hashes need diffs, which a blobless clone would fetch blob by
blob. Only commit messages are read: beyond the mainline ref for other refs,
recent history for mainline itself. Scans are cached per ref tip.
"""

import datetime
import json
import os
import pathlib
import re
import subprocess
import urllib.parse
from typing import Optional, Sequence

from ..model import Authority, Coverage, Series
from ..workspace import Ref

_TRACKER = re.compile(r'^\s*(?:message-id|link):[ \t]+(\S+)', re.I | re.M)
_MSGID = re.compile(r'([^/\s<>]+@[^/\s<>]+)')


def message_ids(body: str) -> set[str]:
    found = set()
    for value in _TRACKER.findall(body):
        match = _MSGID.search(urllib.parse.unquote(value))
        if match:
            found.add(match.group(1))
    return found


def subject_key(text: str) -> str:
    """A subject as matched: whitespace collapsed, lower case."""
    return ' '.join(text.split()).lower()


class RefCache:
    """var/refs/<project>.json: per ref, the scanned tip, message IDs, and subjects."""

    def __init__(self, path: pathlib.Path, checkout: pathlib.Path) -> None:
        self.path = path
        self.checkout = checkout
        try:
            self._refs: dict[str, dict] = json.loads(path.read_text())
        except (OSError, ValueError):
            self._refs = {}
        self._sets: dict[str, tuple[frozenset[str], frozenset[str]]] = {}
        self._tips: dict[str, Optional[str]] = {}

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ['git', *args], cwd=self.checkout, capture_output=True, text=True, check=False
        )

    def tip(self, name: str) -> Optional[str]:
        if name not in self._tips:
            result = self._git('rev-parse', '--verify', '-q', f'{name}^{{commit}}')
            self._tips[name] = result.stdout.strip() if result.returncode == 0 else None
        return self._tips[name]

    def local(self, name: str) -> bool:
        """Whether *name* is a local branch (moved by the maintainer's own work, not a fetch)."""
        return self._git('rev-parse', '--verify', '-q', f'refs/heads/{name}').returncode == 0

    def update(
        self, refs: Sequence[Ref], since: datetime.datetime, local_only: bool = False
    ) -> int:
        """Scan refs whose tips moved; return how many were scanned. *local_only*: only
        refs that are local branches (each load does this; a refresh scans them all)."""
        mainline = next((r.name for r in refs if r.authority == Authority.MAINLINE), None)
        scanned = 0
        changed = False
        for ref in refs:
            if local_only and not self.local(ref.name):
                continue
            tip = self.tip(ref.name)
            if tip is None:
                changed |= self._refs.pop(ref.name, None) is not None
                continue
            base = None if ref.name == mainline else mainline
            if base is not None and self.tip(base) is None:
                base = None
            key = f'{tip} {base and self.tip(base)}'
            if self._refs.get(ref.name, {}).get('key') == key:
                continue
            if base is not None:
                args = [f'{base}..{ref.name}']
            else:
                args = [f'--since={since.isoformat()}', ref.name]
            log = self._git('log', '--no-merges', '--format=%x1e%s%x00%B', *args)
            msgids: set[str] = set()
            subjects: set[str] = set()
            for record in log.stdout.split('\x1e')[1:]:
                subject, _, body = record.partition('\x00')
                subjects.add(subject_key(subject))
                msgids |= message_ids(body)
            self._refs[ref.name] = {
                'key': key,
                'tip': tip,
                'msgids': sorted(msgids),
                'subjects': sorted(subjects),
            }
            self._sets.pop(ref.name, None)
            scanned += 1
        if not (scanned or changed) and self.path.exists():
            return 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f'.{self.path.name}.tmp')
        tmp.write_text(json.dumps(self._refs))
        os.replace(tmp, self.path)
        return scanned

    def coverage(self, series: Series, ref: Ref) -> Coverage:
        entry = self._refs.get(ref.name)
        if entry is None or not series.patches:
            return Coverage.UNCHECKED
        if entry['tip'] != self.tip(ref.name):
            return Coverage.STALE
        if ref.name not in self._sets:
            self._sets[ref.name] = (frozenset(entry['msgids']), frozenset(entry['subjects']))
        msgids, subjects = self._sets[ref.name]
        found = sum(
            1 for p in series.patches if p.msgid in msgids or subject_key(p.subject) in subjects
        )
        if found == len(series.patches):
            return Coverage.FULL
        return Coverage.PARTIAL if found else Coverage.NONE

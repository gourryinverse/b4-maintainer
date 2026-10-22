# SPDX-License-Identifier: GPL-2.0-or-later
"""Reply drafts kept across restarts: var/drafts/<sha1 of the answered message-id>.json.

Drafts are local work in progress, not synced state. Each records the message
it answers, the quote it started from (to tell an untouched draft), and the text.
"""

import datetime
import hashlib
import json
import os
import pathlib
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class SavedDraft:
    msgid: str
    quote: str
    text: str
    saved: str


class DraftStore:
    def __init__(self, root: pathlib.Path) -> None:
        self.root = root

    def _path(self, msgid: str) -> pathlib.Path:
        return self.root / f'{hashlib.sha1(msgid.encode()).hexdigest()}.json'

    @staticmethod
    def _read(path: pathlib.Path) -> Optional[SavedDraft]:
        try:
            data = json.loads(path.read_text())
            return SavedDraft(data['msgid'], data['quote'], data['text'], data.get('saved', ''))
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def get(self, msgid: str) -> Optional[SavedDraft]:
        return self._read(self._path(msgid))

    def save(self, msgid: str, quote: str, text: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(msgid)
        now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
        data = {'msgid': msgid, 'quote': quote, 'text': text, 'saved': now}
        tmp = path.with_name(f'.{path.name}.tmp')
        tmp.write_text(json.dumps(data, indent=1))
        os.replace(tmp, path)

    def delete(self, msgid: str) -> None:
        self._path(msgid).unlink(missing_ok=True)

    def msgids(self) -> set[str]:
        if not self.root.is_dir():
            return set()
        found = (self._read(p) for p in self.root.glob('*.json'))
        return {d.msgid for d in found if d is not None}

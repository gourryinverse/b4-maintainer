# SPDX-License-Identifier: GPL-2.0-or-later
"""Reply drafts kept across restarts, under var/drafts, per answered message-id hash.

<hash>.eml holds the text, edited in place by an external editor (the .eml name
lets editors pick mail settings); <hash>.json records the answered message-id,
the quote the draft started from (to tell an untouched draft), and the editor
pane, if any. Drafts are local work in progress, not synced state.
"""

import datetime
import hashlib
import json
import os
import pathlib
from dataclasses import dataclass
from typing import Any, Optional

from b4 import unix_newlines


@dataclass(frozen=True)
class SavedDraft:
    msgid: str
    quote: str
    text: str
    saved: str
    # External editor pane (e.g. a tmux pane id) last editing this draft.
    pane: str = ''


def _write(path: pathlib.Path, data: str) -> None:
    tmp = path.with_name(f'.{path.name}.tmp')
    tmp.write_text(data)
    os.replace(tmp, path)


class DraftStore:
    def __init__(self, root: pathlib.Path) -> None:
        self.root = root

    def _base(self, msgid: str) -> pathlib.Path:
        return self.root / hashlib.sha1(msgid.encode()).hexdigest()

    def text_path(self, msgid: str) -> pathlib.Path:
        return self._base(msgid).with_suffix('.eml')

    @staticmethod
    def _meta(path: pathlib.Path) -> Optional[dict[str, Any]]:
        try:
            data = json.loads(path.read_text())
            return data if isinstance(data, dict) and isinstance(data.get('msgid'), str) else None
        except (OSError, ValueError):
            return None

    def get(self, msgid: str) -> Optional[SavedDraft]:
        meta = self._meta(self._base(msgid).with_suffix('.json'))
        if meta is None:
            return None
        try:
            text = unix_newlines(self.text_path(msgid).read_bytes()).decode(errors='replace')
        except OSError:
            return None
        return SavedDraft(
            meta['msgid'], meta.get('quote', ''), text, meta.get('saved', ''), meta.get('pane', '')
        )

    def save(self, msgid: str, quote: str, text: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        old = self._meta(self._base(msgid).with_suffix('.json')) or {}
        now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
        _write(self.text_path(msgid), text)
        meta = {'msgid': msgid, 'quote': quote, 'saved': now, 'pane': old.get('pane', '')}
        _write(self._base(msgid).with_suffix('.json'), json.dumps(meta, indent=1))

    def set_pane(self, msgid: str, pane: str) -> None:
        path = self._base(msgid).with_suffix('.json')
        meta = self._meta(path)
        if meta is not None:
            _write(path, json.dumps({**meta, 'pane': pane}, indent=1))

    def delete(self, msgid: str) -> None:
        for suffix in ('.eml', '.json'):
            self._base(msgid).with_suffix(suffix).unlink(missing_ok=True)

    def msgids(self) -> set[str]:
        if not self.root.is_dir():
            return set()
        found = (self._meta(p) for p in self.root.glob('*.json'))
        return {m['msgid'] for m in found if m is not None}

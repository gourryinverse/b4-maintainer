# SPDX-License-Identifier: GPL-2.0-or-later
"""Cache recent patch threads from lore search results, one maildir per thread.

b4 parses one thread per LoreMailbox, so search results for a whole list are
split into threads on arrival; the reader then parses only the thread it
opens. Cross-posted threads are stored once. Fetches happen only on refresh;
the search itself is b4's (b4.get_pi_search_results), injected for tests.

Workaround: neither b4 nor liblore splits many threads in linear time
(liblore.utils.get_strict_thread walks every message per thread). Threads are
keyed by their root: the first References entry, or the parent's thread for
In-Reply-To only. Remove when liblore offers a thread splitter.
"""

import datetime
import email.message
import email.parser
import email.utils
import hashlib
import json
import os
import pathlib
import re
from dataclasses import dataclass
from typing import Callable, Collection, Optional, Sequence

import b4

from ..model import Series
from .summaries import READABLE, VERSION, series_from_dict, series_to_dict

Search = Callable[[str], Optional[Sequence[email.message.EmailMessage]]]

OVERLAP = datetime.timedelta(days=1)
_MSGID = re.compile(r'<([^>]+)>')


def list_query(list_id: str, since: datetime.date) -> str:
    # rt: takes git approxidate; ISO dates work, YYYYMMDD is silently ignored.
    return f'l:{list_id} AND s:PATCH AND rt:{since:%Y-%m-%d}..'


def _date(msg: email.message.Message) -> Optional[datetime.datetime]:
    try:
        value = email.utils.parsedate_to_datetime(str(msg['Date']))
    except (TypeError, ValueError, IndexError):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return value


def _refs(msg: email.message.Message, header: str) -> list[str]:
    return [ref for value in msg.get_all(header, []) for ref in _MSGID.findall(str(value))]


def _write(path: pathlib.Path, tmp: pathlib.Path, data: bytes) -> None:
    tmp.write_bytes(data)
    os.replace(tmp, path)


# msgid -> [thread key, UTC ISO date or None, sender address, parent msgid or None]
Index = dict[str, list]


@dataclass(frozen=True)
class MessageInfo:
    key: str
    date: Optional[str]
    sender: str
    parent: Optional[str]


def _sender(msg: email.message.Message) -> str:
    return email.utils.parseaddr(str(msg.get('From', '')))[1].lower()


def _parent(msg: email.message.Message, msgid: str) -> Optional[str]:
    parents = [r for r in _refs(msg, 'In-Reply-To') if r != msgid]
    refs = [r for r in _refs(msg, 'References') if r != msgid]
    return parents[0] if parents else refs[-1] if refs else None


class MailCache:
    """var/mail: threads/<hash>/ maildirs, series/<hash>.json summaries,
    index.json (msgid -> thread key, date, sender, parent), lists/<name>.json fetch state."""

    def __init__(self, root: 'pathlib.Path | str') -> None:
        self.root = pathlib.Path(root)
        self._threads = self.root / 'threads'
        self._summaries = self.root / 'series'
        self._lists = self.root / 'lists'
        self._index_path = self.root / 'index.json'
        # Message-ids whose threads stay cached past the window (watched discussions).
        self.keep: frozenset[str] = frozenset()

    def sources(self) -> list[pathlib.Path]:
        if not self._threads.is_dir():
            return []
        return sorted(p for p in self._threads.iterdir() if any((p / 'cur').iterdir()))

    def fetched_lists(self) -> list[str]:
        if not self._lists.is_dir():
            return []
        return sorted(p.stem for p in self._lists.glob('*.json'))

    def thread_name(self, key: str) -> str:
        """Directory name of a thread, as in Series.source."""
        return self._thread_dir(key).name

    def _load_index(self) -> Index:
        index: Index = json.loads(self._index_path.read_text()) if self._index_path.exists() else {}
        old = [m for m, entry in index.items() if len(entry) < 4]
        for msgid in old:
            # Written before senders and parents were indexed: read them once from the mail.
            key, date = index[msgid][:2]
            path = self._thread_dir(key) / 'cur' / hashlib.sha1(msgid.encode()).hexdigest()
            try:
                with path.open('rb') as fh:
                    msg = email.message_from_binary_file(fh, policy=b4.emlpolicy)
                index[msgid] = [key, date, _sender(msg), _parent(msg, msgid)]
            except OSError:
                index[msgid] = [key, date, '', None]
        if old:
            _write(self._index_path, self.root / '.index.json.tmp', json.dumps(index).encode())
        return index

    def messages(self) -> dict[str, MessageInfo]:
        return {m: MessageInfo(*entry) for m, entry in self._load_index().items()}

    def headers(self, msgid: str, key: str) -> Optional[email.message.Message]:
        """One cached message's headers, without parsing its body."""
        path = self._thread_dir(key) / 'cur' / hashlib.sha1(msgid.encode()).hexdigest()
        try:
            with path.open('rb') as fh:
                return email.parser.BytesHeaderParser(policy=b4.emlpolicy).parse(fh)
        except OSError:
            return None

    def recipients(self, msgid: str, key: str) -> set[str]:
        """Addresses in a cached message's To and Cc, lower-cased."""
        msg = self.headers(msgid, key)
        if msg is None:
            return set()
        values = [str(v) for h in ('To', 'Cc') for v in msg.get_all(h, [])]
        return {addr.lower() for _, addr in email.utils.getaddresses(values) if '@' in addr}

    def discussions(self, covered: Collection[str]) -> list[Series]:
        """Threads no series covers, by thread name, as discussions led by their first message.

        Threads whose first message is a patch (say, a cover letter whose patches
        were not fetched) are left out: they are incomplete series, not talk.
        """
        threads: dict[str, dict[str, MessageInfo]] = {}
        for msgid, entry in self.messages().items():
            threads.setdefault(entry.key, {})[msgid] = entry
        found = []
        for key, msgs in threads.items():
            tdir = self._thread_dir(key)
            if tdir.name in covered or not tdir.is_dir():
                continue
            roots = [m for m, e in msgs.items() if not e.parent or e.parent not in msgs]
            root = min(roots or msgs, key=lambda m: msgs[m].date or '')
            head = self.headers(root, key)
            if head is None:
                continue
            subject = b4.LoreSubject(str(head.get('Subject', '')))
            if subject.patch:
                continue
            name, addr = email.utils.parseaddr(str(head.get('From', '')))
            lists = [email.utils.parseaddr(str(v))[1] for v in head.get_all('List-Id', [])]
            date = msgs[root].date
            found.append(
                Series(
                    identity=f'thread:{root}',
                    subject=subject.subject or '(no subject)',
                    revision=0,
                    expected=0,
                    complete=True,
                    has_cover=True,
                    author_name=name or addr,
                    author_email=addr.lower(),
                    sent=datetime.datetime.fromisoformat(date) if date else None,
                    patches=(),
                    lists=tuple(lst.lower() for lst in lists if lst),
                    recipients=tuple(sorted(self.recipients(root, key))),
                    msgids=tuple(sorted(msgs)),
                    source=str(tdir),
                    discussion=True,
                )
            )
        return found

    def series(
        self, parse: Callable[[pathlib.Path], list[Series]], upgrade: bool = True
    ) -> list[Series]:
        """Every cached thread's series; threads whose files changed are reparsed.

        Without *upgrade* (startup), summaries in an older readable format are used as
        they are; a refresh reparses them.
        """
        accepted = {VERSION} if upgrade else READABLE
        result: list[Series] = []
        for tdir in self.sources():
            stamp = hashlib.sha1(
                '\n'.join(sorted(p.name for p in (tdir / 'cur').iterdir())).encode()
            ).hexdigest()
            path = self._summaries / f'{tdir.name}.json'
            try:
                cached = json.loads(path.read_text())
                if cached['version'] in accepted and cached['stamp'] == stamp:
                    result.extend(series_from_dict(d) for d in cached['series'])
                    continue
            except (OSError, ValueError, KeyError, TypeError):
                pass
            parsed = parse(tdir)
            self._summaries.mkdir(parents=True, exist_ok=True)
            data = {
                'version': VERSION,
                'stamp': stamp,
                'series': [series_to_dict(s) for s in parsed],
            }
            _write(path, self._summaries / f'.{tdir.name}.tmp', json.dumps(data).encode())
            result.extend(parsed)
        return result

    def refresh(
        self,
        list_id: str,
        window_days: int,
        search: Search,
        now: Optional[datetime.datetime] = None,
    ) -> int:
        """Fetch new list mail into the cache; return how many messages were added."""
        return self.refresh_query(
            list_id, lambda since: list_query(list_id, since), window_days, search, now
        )

    def refresh_query(
        self,
        name: str,
        make_query: Callable[[datetime.date], str],
        window_days: int,
        search: Search,
        now: Optional[datetime.datetime] = None,
    ) -> int:
        """Fetch a search's new mail into the cache; *name* keys its incremental state."""
        now = now or datetime.datetime.now(datetime.timezone.utc)
        start = now - datetime.timedelta(days=window_days)
        self._lists.mkdir(parents=True, exist_ok=True)
        self._threads.mkdir(parents=True, exist_ok=True)
        state_path = self._lists / f'{name}.json'
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        since = start
        if 'fetched_at' in state:
            since = max(start, datetime.datetime.fromisoformat(state['fetched_at']) - OVERLAP)

        msgs = search(make_query(since.date()))
        index = self._load_index()
        added = self._add(msgs or (), index)
        self._prune(start, index)
        _write(self._index_path, self.root / '.index.json.tmp', json.dumps(index).encode())
        if msgs is not None:
            state['fetched_at'] = now.isoformat()
            _write(state_path, self._lists / f'.{name}.tmp', json.dumps(state).encode())
        return added

    def _thread_dir(self, key: str) -> pathlib.Path:
        return self._threads / hashlib.sha1(key.encode()).hexdigest()

    @staticmethod
    def _thread_key(msg: email.message.Message, msgid: str, index: Index) -> str:
        refs = [r for r in _refs(msg, 'References') if r != msgid]
        parents = [r for r in _refs(msg, 'In-Reply-To') if r != msgid]
        root = refs[0] if refs else parents[0] if parents else msgid
        return index[root][0] if root in index else root

    def refresh_thread(
        self,
        msgid: str,
        fetch: Callable[[str], Optional[Sequence[email.message.EmailMessage]]],
        window_days: int,
        now: Optional[datetime.datetime] = None,
    ) -> int:
        """Fetch one whole thread by a message-id in it; return how many messages were added."""
        now = now or datetime.datetime.now(datetime.timezone.utc)
        msgs = fetch(msgid)
        if not msgs:
            return 0
        self._threads.mkdir(parents=True, exist_ok=True)
        index = self._load_index()
        added = self._add(msgs, index)
        self._prune(now - datetime.timedelta(days=window_days), index)
        _write(self._index_path, self.root / '.index.json.tmp', json.dumps(index).encode())
        return added

    def _add(self, msgs: Sequence[email.message.EmailMessage], index: Index) -> int:
        epoch = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
        dated = [(_date(m), m) for m in msgs]
        added = 0
        # Oldest first, so parents are indexed before their replies.
        for date, msg in sorted(dated, key=lambda dm: dm[0] or epoch):
            msgid = b4.LoreMessage.get_clean_msgid(msg)
            if not msgid or msgid in index:
                continue
            key = self._thread_key(msg, msgid, index)
            tdir = self._thread_dir(key)
            for sub in ('cur', 'new', 'tmp'):
                (tdir / sub).mkdir(parents=True, exist_ok=True)
            name = hashlib.sha1(msgid.encode()).hexdigest()
            _write(tdir / 'cur' / name, tdir / 'tmp' / name, msg.as_bytes(policy=b4.emlpolicy))
            # UTC, so the stored dates compare correctly as strings.
            utc = date.astimezone(datetime.timezone.utc).isoformat() if date else None
            index[msgid] = [key, utc, _sender(msg), _parent(msg, msgid)]
            added += 1
        return added

    def _prune(self, start: datetime.datetime, index: Index) -> None:
        """Drop whole threads whose newest message is older than the window.

        Undated messages count as recent, so their threads are kept, as are
        threads holding a message-id in *keep*.
        """
        newest: dict[str, str] = {}
        for key, date, *_ in index.values():
            latest = date or '9999'
            if latest > newest.get(key, ''):
                newest[key] = latest
        kept = {index[m][0] for m in self.keep if m in index}
        cutoff = start.astimezone(datetime.timezone.utc).isoformat()
        stale = {key for key, latest in newest.items() if latest < cutoff and key not in kept}
        for key in stale:
            tdir = self._thread_dir(key)
            if tdir.is_dir():
                for sub in ('cur', 'new', 'tmp'):
                    for leftover in (tdir / sub).iterdir():
                        leftover.unlink()
                    (tdir / sub).rmdir()
                tdir.rmdir()
            (self._summaries / f'{tdir.name}.json').unlink(missing_ok=True)
        for msgid in [m for m, (key, *_) in index.items() if key in stale]:
            del index[msgid]

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
import email.policy
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


THREAD_BATCH = 25  # Message-IDs per whole-thread search.
_REPLY = re.compile(r'^\s*(re|fwd?|aw)\s*:', re.I)


def _posting(msg: email.message.Message) -> bool:
    """A message that posts work (a patch or cover letter), not a reply to one."""
    return not _REPLY.match(str(msg.get('Subject', '')))


def _root(msg: email.message.Message, msgid: str) -> str:
    refs = [r for r in _refs(msg, 'References') if r != msgid]
    parents = [r for r in _refs(msg, 'In-Reply-To') if r != msgid]
    return refs[0] if refs else parents[0] if parents else msgid


def _sender(msg: email.message.Message) -> str:
    return email.utils.parseaddr(str(msg.get('From', '')))[1].lower()


def _parent(msg: email.message.Message, msgid: str) -> Optional[str]:
    parents = [r for r in _refs(msg, 'In-Reply-To') if r != msgid]
    refs = [r for r in _refs(msg, 'References') if r != msgid]
    return parents[0] if parents else refs[-1] if refs else None


class MailCache:
    """var/mail: threads/<hash>/ maildirs, series/<hash>.json summaries,
    index.json (msgid -> thread key, date, sender, parent), lists/<name>.json fetch state,
    own.json (the maintainer's own threads -> their newest posting's date), history.json
    (message-ids that arrived already older than the browse window with them)."""

    def __init__(self, root: 'pathlib.Path | str') -> None:
        self.root = pathlib.Path(root)
        self._threads = self.root / 'threads'
        self._summaries = self.root / 'series'
        self._lists = self.root / 'lists'
        self._index_path = self.root / 'index.json'
        # Message-ids whose threads stay cached past the window (watched discussions).
        self.keep: frozenset[str] = frozenset()
        # Threads left out: patterns found in their first message's subject ([browse] exclude).
        self.exclude: tuple[re.Pattern[str], ...] = ()
        # The maintainer's own threads stay cached while posted since this ([browse] own_days).
        self.own_since: Optional[datetime.datetime] = None
        self._own_path = self.root / 'own.json'
        self._history_path = self.root / 'history.json'

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
        added = self._add(self._without_excluded(msgs or ()), index)
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

    def refresh_own(
        self,
        name: str,
        make_query: Callable[[datetime.date], str],
        days: int,
        window_days: int,
        posts: Search,
        threads: Search,
        now: Optional[datetime.datetime] = None,
    ) -> int:
        """Fetch the maintainer's own postings (patches, not replies) of the last *days*:
        *posts* finds their messages; *threads* fetches each new posting's whole thread
        (m: searches, many at once). Their threads stay cached for *days*; what arrives
        already older than *window_days* is history, never an unread response."""
        now = now or datetime.datetime.now(datetime.timezone.utc)
        self.own_since = now - datetime.timedelta(days=days)
        self._lists.mkdir(parents=True, exist_ok=True)
        self._threads.mkdir(parents=True, exist_ok=True)
        state_path = self._lists / f'{name}.json'
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        since = self.own_since
        if 'fetched_at' in state:
            since = max(since, datetime.datetime.fromisoformat(state['fetched_at']) - OVERLAP)
        found = posts(make_query(since.date()))
        if found is None:
            return 0  # No answer: the next refresh asks again from the same point.
        postings = [m for m in self._without_excluded(found) if _posting(m)]
        index = self._load_index()
        new: dict[str, str] = {}  # A posting not yet cached per thread root.
        for msg in postings:
            msgid = b4.LoreMessage.get_clean_msgid(msg)
            if msgid and msgid not in index:
                new.setdefault(_root(msg, msgid), msgid)
        fetched: list[email.message.EmailMessage] = []
        wanted = sorted(new.values())
        unanswered: set[str] = set()  # Postings whose threads lore did not return.
        for start in range(0, len(wanted), THREAD_BATCH):
            batch = wanted[start : start + THREAD_BATCH]
            got = threads(' OR '.join(f'm:"{m}"' for m in batch))
            held = {b4.LoreMessage.get_clean_msgid(m) for m in got or ()}
            unanswered |= set(batch) - held
            fetched += got or []
        if unanswered:  # Left for the next refresh, which asks again from the same point.
            roots = {r for r, m in new.items() if m in unanswered}
            postings = [
                m
                for m in postings
                if _root(m, b4.LoreMessage.get_clean_msgid(m) or '') not in roots
            ]
        arrived: list[tuple[str, Optional[datetime.datetime]]] = []
        added = self._add([*self._without_excluded(fetched), *postings], index, arrived)
        cutoff = now - datetime.timedelta(days=window_days)
        history = self.history() | {m for m, date in arrived if date is not None and date < cutoff}
        own = self._own()
        for msg in postings:
            msgid = b4.LoreMessage.get_clean_msgid(msg)
            if msgid in index:
                key, date = index[msgid][0], index[msgid][1] or now.isoformat()
                own[key] = max(own.get(key, ''), date)
        self._prune(now - datetime.timedelta(days=window_days), index, own, history)
        _write(self._index_path, self.root / '.index.json.tmp', json.dumps(index).encode())
        if not unanswered:
            state['fetched_at'] = now.isoformat()
            _write(state_path, self._lists / f'.{name}.tmp', json.dumps(state).encode())
        return added

    def history(self) -> set[str]:
        """Message-ids that arrived with the maintainer's own threads already older than
        the browse window: read long ago, never unread responses."""
        try:
            return set(json.loads(self._history_path.read_text()))
        except (OSError, ValueError, TypeError):
            return set()

    def _own(self) -> dict[str, str]:
        try:
            data = json.loads(self._own_path.read_text())
            return {str(k): str(v) for k, v in data.items()}
        except (OSError, ValueError, TypeError, AttributeError):
            return {}

    def _excluded(self, subject: str) -> bool:
        subject = ' '.join(subject.split())
        return any(p.search(subject) for p in self.exclude)

    def _without_excluded(
        self, msgs: Sequence[email.message.EmailMessage]
    ) -> Sequence[email.message.EmailMessage]:
        """*msgs* less the threads whose first message an exclude pattern finds."""
        if not self.exclude:
            return msgs
        roots = {
            b4.LoreMessage.get_clean_msgid(m)
            for m in msgs
            if not _refs(m, 'References')
            and not _refs(m, 'In-Reply-To')
            and self._excluded(str(m.get('Subject', '')))
        }
        if not roots:
            return msgs

        return [m for m in msgs if _root(m, b4.LoreMessage.get_clean_msgid(m) or '') not in roots]

    def _excluded_threads(self, index: Index) -> set[str]:
        """Cached threads whose first message an exclude pattern finds."""
        if not self.exclude:
            return set()
        found = set()
        parser = email.parser.BytesHeaderParser(policy=email.policy.default)
        for key in {key for key, *_ in index.values()}:
            path = self._thread_dir(key) / 'cur' / hashlib.sha1(key.encode()).hexdigest()
            try:
                with path.open('rb') as f:
                    subject = str(parser.parse(f).get('Subject', ''))
            except (OSError, ValueError):
                continue  # Its first message never arrived: nothing to judge it by.
            if self._excluded(subject):
                found.add(key)
        return found

    def _add(
        self,
        msgs: Sequence[email.message.EmailMessage],
        index: Index,
        arrived: Optional[list[tuple[str, Optional[datetime.datetime]]]] = None,
    ) -> int:
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
            if arrived is not None:
                arrived.append((msgid, date))
        return added

    def _prune(
        self,
        start: datetime.datetime,
        index: Index,
        own: Optional[dict[str, str]] = None,
        history: Optional[set[str]] = None,
    ) -> None:
        """Drop whole threads whose newest message is older than the window.

        Undated messages count as recent, so their threads are kept, as are
        threads holding a message-id in *keep*, and the maintainer's own threads
        posted since *own_since*. Excluded threads go whatever their age.
        """
        own = self._own() if own is None else own
        history = self.history() if history is None else history
        newest: dict[str, str] = {}
        for key, date, *_ in index.values():
            latest = date or '9999'
            if latest > newest.get(key, ''):
                newest[key] = latest
        kept = {index[m][0] for m in self.keep if m in index}
        cutoff = start.astimezone(datetime.timezone.utc).isoformat()
        since = (
            self.own_since.astimezone(datetime.timezone.utc).isoformat() if self.own_since else ''
        )
        mine = {key for key, posted in own.items() if since and posted >= since}
        aged = {key for key, latest in newest.items() if latest < cutoff} - mine
        stale = (aged | self._excluded_threads(index)) - kept
        for key in stale:
            tdir = self._thread_dir(key)
            if tdir.is_dir():
                for sub in ('cur', 'new', 'tmp'):
                    for leftover in (tdir / sub).iterdir():
                        leftover.unlink()
                    (tdir / sub).rmdir()
                tdir.rmdir()
            (self._summaries / f'{tdir.name}.json').unlink(missing_ok=True)
        gone = [m for m, (key, *_) in index.items() if key in stale]
        for msgid in gone:
            del index[msgid]
        own = {k: v for k, v in own.items() if k in newest and k not in stale}
        history = history - set(gone)
        if own != self._own():
            _write(self._own_path, self.root / '.own.json.tmp', json.dumps(own).encode())
        if history != self.history():
            data = json.dumps(sorted(history)).encode()
            _write(self._history_path, self.root / '.history.json.tmp', data)

# SPDX-License-Identifier: GPL-2.0-or-later
"""Read and change native b4 review state in a project checkout.

b4 keeps its configuration in module globals; b4_repo() loads one checkout's
configuration (and so its b4.data-dir) under a lock and restores the
previous globals, as b4 documents for multi-repository callers.
"""

import argparse
import contextlib
import datetime
import pathlib
import subprocess
import threading
from dataclasses import dataclass
from email.message import EmailMessage
from types import SimpleNamespace
from typing import Any, Callable, Generator, Mapping, Optional, Sequence, cast

import b4
from b4.review import messages, tracking

from .lorelist import Search
from .statesync import BRANCH_STATUSES

_LOCK = threading.RLock()


@contextlib.contextmanager
def b4_repo(checkout: pathlib.Path) -> Generator[None, None, None]:
    with _LOCK:
        saved = (b4.MAIN_CONFIG, b4.USER_CONFIG, b4.SENDEMAIL_CONFIG)
        try:
            b4.setup_config(argparse.Namespace(config=None), topdir=str(checkout))
            yield
        finally:
            b4.MAIN_CONFIG, b4.USER_CONFIG, b4.SENDEMAIL_CONFIG = saved


def activate(checkout: pathlib.Path) -> None:
    """Load a checkout's b4 configuration and leave it loaded.

    b4's interactive flows (the reply preview, sending) read the global
    configuration while the UI runs, after any worker call has returned.
    b4_repo() restores to whatever was loaded, so this stays in effect.
    """
    with _LOCK:
        b4.setup_config(argparse.Namespace(config=None), topdir=str(checkout))


def patatt_sign_default() -> bool:
    """Sign replies with patatt unless b4.review-no-patatt-sign says not to, as b4 review does."""
    value = str(b4.get_main_config().get('review-no-patatt-sign', '') or '')
    return value.lower() not in {'yes', 'true', 'y'}


def load_lore_message(sources: Sequence[str], msgid: str) -> 'b4.LoreMessage':
    """The message with this ID from the item's cached threads, as b4 parses it."""
    for source in sources:
        for msg in b4.get_msgs_from_mailbox_or_maildir(source):
            if b4.LoreMessage.get_clean_msgid(msg) == msgid:
                return b4.LoreMessage(msg)
    raise LookupError(f'message {msgid} is not in the local cache')


def mark_answered(checkout: pathlib.Path, msgid: str, date: Optional[str]) -> None:
    """Set b4's Answered flag, as b4's own reply flow does after sending."""
    with b4_repo(checkout):
        conn = messages.get_db()
        try:
            messages.set_flag(conn, msgid, 'Answered', date)
        finally:
            conn.close()


@dataclass(frozen=True)
class Tracked:
    change_id: str
    revision: int
    subject: str
    author_name: str
    author_email: str
    sent: Optional[datetime.datetime]
    msgid: str
    expected: int
    status: str
    msgids: frozenset[str]


def _when(value: str) -> Optional[datetime.datetime]:
    try:
        return datetime.datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def tracked_series(checkout: pathlib.Path, identifier: str) -> list[Tracked]:
    with b4_repo(checkout):
        rows = tracking.get_all_tracked_series(identifier)
        known = tracking.get_all_series_message_ids(identifier)
    return [
        Tracked(
            change_id=row['change_id'],
            revision=int(row['revision']),
            subject=row['subject'],
            author_name=row['sender_name'],
            author_email=row['sender_email'],
            sent=_when(row['sent_at']),
            msgid=row['message_id'],
            expected=int(row['num_patches']),
            status=row['status'],
            msgids=frozenset([row['message_id'], *known.get(row['change_id'], [])]) - {''},
        )
        for row in rows
    ]


def user_email(checkout: pathlib.Path) -> Optional[str]:
    """The maintainer's address as b4 resolves it for this checkout."""
    with b4_repo(checkout):
        value = b4.USER_CONFIG.get('email')
    return value if isinstance(value, str) and value else None


def sashiko_url(checkout: pathlib.Path) -> Optional[str]:
    """The Sashiko instance b4 uses for this checkout (b4.sashiko-url), if any."""
    with b4_repo(checkout):
        value = b4.get_main_config().get('sashiko-url')
    return value if isinstance(value, str) and value else None


def seen(checkout: pathlib.Path, msgids: Sequence[str]) -> set[str]:
    """Which of these messages carry b4's Seen flag."""
    with b4_repo(checkout):
        conn = messages.get_db()
        try:
            flags = messages.get_flags_bulk(conn, list(msgids))
        finally:
            conn.close()
    return {m for m, f in flags.items() if 'Seen' in f.split()}


def mark_seen(checkout: pathlib.Path, dated: Mapping[str, Optional[str]]) -> None:
    """Set b4's Seen flag on messages, given {msgid: ISO date} (b4 prunes by date)."""
    entries = [{'msgid': m, 'msg_date': d} for m, d in dated.items()]
    with b4_repo(checkout):
        conn = messages.get_db()
        try:
            messages.set_flags_bulk(conn, entries, 'Seen')  # type: ignore[arg-type]
        finally:
            conn.close()


def track(checkout: pathlib.Path, msgid: str, b4_command: Sequence[str]) -> None:
    """Track a series with b4 in its project; b4 retrieves it from lore."""
    result = subprocess.run(
        [*b4_command, 'review', 'track', msgid],
        cwd=checkout,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        lines = (result.stderr or result.stdout).strip().splitlines()
        raise RuntimeError(f'b4 review track failed: {lines[-1] if lines else "no output"}')


def archive(
    checkout: pathlib.Path, identifier: str, change_id: str, revision: int
) -> tuple[bool, str]:
    """Archive a tracked series as b4 review's TUI does: (ok, archive path or error).

    b4 saves the review branch to its data directory, deletes the branch, and
    marks the series archived (and archives it in Patchwork when configured).
    It refuses rather than switch away from a checked-out review branch.
    """
    with b4_repo(checkout):
        from b4.review import archive_series

        return archive_series(str(checkout), identifier, change_id, revision)


def unarchive(checkout: pathlib.Path, identifier: str, change_id: str) -> None:
    """Track an archived series again, keeping b4's records of it.

    b4 review track refuses archived series (it offers forget, which
    erases them); b4 brings the newest archived revision back to new.
    """
    with b4_repo(checkout):
        conn = tracking.get_db(identifier)
        try:
            if tracking.unarchive_series(conn, change_id) is None:
                raise RuntimeError(f'{change_id} is not archived in {identifier}')
        finally:
            conn.close()


def lore_thread(checkout: pathlib.Path) -> Callable[[str], Optional[list[EmailMessage]]]:
    """b4's whole-thread lookup by message-id, with this checkout's configuration."""

    def fetch(msgid: str) -> Optional[list[EmailMessage]]:
        with b4_repo(checkout):
            return b4.get_pi_thread_by_msgid(msgid, nocache=True, quiet=True)

    return fetch


def lore_search(checkout: pathlib.Path) -> Search:
    """b4's lore search with this checkout's configuration (mirror, cache directory)."""

    def search(query: str):
        with b4_repo(checkout):
            return b4.get_pi_search_results(query, nocache=True, full_threads=True)

    return search


# State export/import: tracking rows and flags as plain records, through b4's own APIs.

_SERIES_FIELDS = (
    'revision',
    'status',
    'message_id',
    'subject',
    'sender_name',
    'sender_email',
    'sent_at',
    'added_at',
    'num_patches',
    'pw_series_id',
    'target_branch',
    'is_rethreaded',
    'snoozed_until',
)
_REVISION_FIELDS = (
    'revision',
    'message_id',
    'subject',
    'link',
    'found_at',
    'fingerprint',
    'source',
    'is_rethreaded',
)


def export_series(checkout: pathlib.Path, identifier: str) -> dict[str, dict[str, Any]]:
    """{change_id: record}: series rows with patches, and known revisions (no thread blobs)."""
    with b4_repo(checkout):
        rows = tracking.get_all_tracked_series(identifier)
        if not rows:
            return {}
        conn = tracking.get_db(identifier)
        try:
            records: dict[str, dict[str, Any]] = {}
            for row in sorted(rows, key=lambda r: (r['change_id'], r['revision'])):
                cid = row['change_id']
                rec = records.setdefault(
                    cid,
                    {
                        'change_id': cid,
                        'series': [],
                        'revisions': [
                            {k: r[k] for k in _REVISION_FIELDS}
                            for r in tracking.get_revisions(conn, cid)
                        ],
                    },
                )
                entry = {k: row[k] for k in _SERIES_FIELDS}
                entry['is_rethreaded'] = bool(entry['is_rethreaded'])
                entry['patches'] = tracking.get_series_patches(conn, cid, row['revision'])
                rec['series'].append(entry)
        finally:
            conn.close()
    return records


def import_series(
    checkout: pathlib.Path, identifier: str, records: Mapping[str, Optional[dict[str, Any]]]
) -> None:
    """Add or update b4's rows from records.

    Review-branch statuses are neither imported nor overwritten: b4 marks a
    branch-less series gone, and a local review branch is work in progress.
    """
    with b4_repo(checkout):
        conn = tracking.get_db(identifier)
        try:
            for cid, rec in records.items():
                if rec is None:
                    continue  # Dropped (archived long ago): b4 keeps its own row.
                for rev in rec.get('revisions', []):
                    tracking.add_revision(
                        conn,
                        cid,
                        rev['revision'],
                        rev['message_id'],
                        subject=rev.get('subject'),
                        link=rev.get('link'),
                        fingerprint=rev.get('fingerprint'),
                        source=rev.get('source') or 'heuristic',
                        is_rethreaded=bool(rev.get('is_rethreaded')),
                    )
                for row in rec.get('series', []):
                    _import_row(conn, cid, row)
            conn.commit()
        finally:
            conn.close()


def _import_row(conn: Any, cid: str, row: Mapping[str, Any]) -> None:
    revision = row['revision']
    current = tracking.get_series_status(conn, cid, revision)
    if current is None:
        tracking.add_series_to_db(
            conn,
            cid,
            revision,
            row.get('subject'),
            row.get('sender_name'),
            row.get('sender_email'),
            row.get('sent_at'),
            row['message_id'],
            row.get('num_patches') or 0,
            pw_series_id=row.get('pw_series_id'),
            added_at=row.get('added_at') or None,
            is_rethreaded=bool(row.get('is_rethreaded')),
        )
        patches = [
            SimpleNamespace(msgid=p['message_id'], subject=p.get('subject'))
            for p in row.get('patches', [])
        ]
        positions = {p['position'] for p in row.get('patches', [])}
        slots: list[Any] = [None] * (max(positions, default=0) + 1)
        for p, lmsg in zip(row.get('patches', []), patches):
            slots[p['position']] = lmsg
        # b4 reads only .patches (msgid, subject per position; None for gaps).
        series = cast('b4.LoreSeries', SimpleNamespace(patches=slots))
        tracking.add_series_patches(conn, cid, revision, series)
        current = 'new'
    status = row.get('status') or 'new'
    if status != current and status not in BRANCH_STATUSES and current not in BRANCH_STATUSES:
        tracking.update_series_status(conn, cid, status, revision=revision)
    if row.get('target_branch') != tracking.get_target_branch(conn, cid, revision):
        tracking.update_target_branch(conn, cid, row.get('target_branch'), revision=revision)


def all_flags(checkout: pathlib.Path) -> list[dict[str, Optional[str]]]:
    """Every message b4 holds flags for: {'msgid', 'msg_date', 'flags'}."""
    with b4_repo(checkout):
        conn = messages.get_db()
        try:
            return messages.get_all_flags(conn)
        finally:
            conn.close()


def add_flags(checkout: pathlib.Path, entries: Sequence[Mapping[str, Optional[str]]]) -> None:
    """Add each entry's flags to b4's (flags are only ever added by an import)."""
    by_flag: dict[str, list[dict[str, Optional[str]]]] = {}
    for e in entries:
        for flag in (e.get('flags') or '').split():
            by_flag.setdefault(flag, []).append({'msgid': e['msgid'], 'msg_date': e['msg_date']})
    with b4_repo(checkout):
        conn = messages.get_db()
        try:
            for flag, dated in by_flag.items():
                messages.set_flags_bulk(conn, dated, flag)
        finally:
            conn.close()

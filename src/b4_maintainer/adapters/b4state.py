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
from typing import Generator, Mapping, Optional, Sequence

import b4
from b4.review import messages, tracking

from .lorelist import Search

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


def lore_search(checkout: pathlib.Path) -> Search:
    """b4's lore search with this checkout's configuration (mirror, cache directory)."""

    def search(query: str):
        with b4_repo(checkout):
            return b4.get_pi_search_results(query, nocache=True, full_threads=True)

    return search

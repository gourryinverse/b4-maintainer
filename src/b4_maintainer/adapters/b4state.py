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
from typing import Generator, Optional, Sequence

import b4
from b4.review import tracking

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


def lore_search(checkout: pathlib.Path) -> Search:
    """b4's lore search with this checkout's configuration (mirror, cache directory)."""

    def search(query: str):
        with b4_repo(checkout):
            return b4.get_pi_search_results(query, nocache=True, full_threads=True)

    return search

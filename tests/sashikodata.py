"""Synthetic Sashiko API responses, shaped like the pinned deps/sashiko server."""

import datetime
import json
from typing import Any, Optional

URL = 'https://sashiko.example.org'


def row(
    ps_id: int,
    msgid: str,
    status: str = 'Reviewed',
    counts: tuple[int, int, int, int] = (0, 0, 0, 0),
    date: Optional[datetime.datetime] = None,
    subject: str = '[PATCH 0/2] synthetic',
) -> dict[str, Any]:
    """One /api/patchsets item."""
    critical, high, medium, low = counts
    date = date or datetime.datetime(2026, 10, 20, tzinfo=datetime.timezone.utc)
    return {
        'id': ps_id,
        'subject': subject,
        'status': status,
        'author': '"Synthetic Author" <contributor1@example.org>',
        'date': int(date.timestamp()),
        'message_id': msgid,
        'total_parts': 2,
        'received_parts': 2,
        'mailing_lists': ['linux-mm'],
        'findings_critical': critical,
        'findings_high': high,
        'findings_medium': medium,
        'findings_low': low,
        'baseline_id': None,
        'model_name': None,
    }


def page(items: list[dict[str, Any]], number: int = 1, total: int = 0) -> dict[str, Any]:
    return {'items': items, 'total': total or len(items), 'page': number, 'per_page': 50}


def finding(
    problem: str, severity: str = 'Medium', preexisting: bool = False, path: str = 'mm/a.c'
) -> dict[str, Any]:
    return {
        'id': 'PVF1',
        'problem': problem,
        'severity': severity,
        'severity_explanation': 'Synthetic consequence.',
        'preexisting': preexisting,
        'locations': [{'file': path, 'line': 10, 'function_or_symbol': 'foo'}],
    }


def detail(
    ps_id: int,
    cover: str,
    patches: list[tuple[str, list[dict[str, Any]]]],
    status: str = 'Reviewed',
) -> dict[str, Any]:
    """One /api/patchset response: patches as (msgid, findings) in posting order."""
    return {
        'id': ps_id,
        'message_id': cover,
        'status': status,
        'model_name': 'synthetic-model',
        'baseline': {'branch': 'mm/mm-new', 'commit': '59d7f9ec14ad4b6f38c0', 'repo_url': ''},
        'patches': [
            {'id': 1000 + n, 'message_id': msgid, 'part_index': n, 'status': 'Reviewed'}
            for n, (msgid, _) in enumerate(patches, 1)
        ],
        'reviews': [
            {
                'id': 2000 + n,
                'patch_id': 1000 + n,
                'status': 'Reviewed',
                'output': json.dumps({'findings': found}),
                'inline_review': f'> diff --git a/mm/a.c b/mm/a.c\n\nReview of part {n}.\n',
            }
            for n, (_, found) in enumerate(patches, 1)
        ],
    }


class FakeSashiko:
    """Answers list pages per group and series details per message-id; records requests."""

    def __init__(self) -> None:
        self.pages: dict[str, list[dict[str, Any]]] = {}
        self.details: dict[str, dict[str, Any]] = {}
        self.list_requests: list[tuple[str, dict[str, Any]]] = []
        self.detail_requests: list[str] = []
        self.fail = False

    def get(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        self.list_requests.append((url, dict(params)))
        if self.fail:
            raise OSError('synthetic outage')
        rows = self.pages.get(params['mailing_list'], [])
        n, size = params['page'], params['per_page']
        return page(rows[(n - 1) * size : n * size], n, len(rows))

    def patchset(self, msgid: str, url: str) -> Optional[dict[str, Any]]:
        self.detail_requests.append(msgid)
        return self.details.get(msgid)

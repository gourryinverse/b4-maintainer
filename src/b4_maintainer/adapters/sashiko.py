# SPDX-License-Identifier: GPL-2.0-or-later
"""Sashiko AI review status from its HTTP API; see docs/design/sashiko.md.

Per-list status pages are a client connector: b4 reads Sashiko one series at a
time. Series details come through b4's fetch and parse, so both read Sashiko
the same way. Everything is fetched on refresh and read offline afterwards.
"""

import dataclasses
import datetime
import hashlib
import json
import os
import pathlib
from typing import Any, Callable, Iterable, Mapping, Optional

from ..model import Finding, SashikoPatch, SashikoReview

# (url, query parameters) -> decoded JSON
Get = Callable[[str, Mapping[str, Any]], Any]
# (series message-id, base url) -> /api/patchset JSON, or None if unavailable
GetPatchset = Callable[[str, str], Optional[dict[str, Any]]]

# Statuses whose details are worth fetching; pending and embargoed ones have none.
DETAILED = frozenset({'Reviewed', 'Failed', 'Failed To Apply'})
_COUNTS = ('findings_critical', 'findings_high', 'findings_medium', 'findings_low')


def group(list_id: str) -> str:
    """Sashiko's list group: the list-id's domain reversed, then its name."""
    name, _, domain = list_id.partition('.')
    return '.'.join([*reversed(domain.split('.')), name])


def http_get(url: str, params: Mapping[str, Any]) -> Any:
    """GET through b4's requests session (its user agent and proxies)."""
    import b4

    resp = b4.get_requests_session().get(url, params=dict(params), timeout=30)
    resp.raise_for_status()
    return resp.json()


def b4_patchset(msgid: str, url: str) -> Optional[dict[str, Any]]:
    from b4.review.checks import fetch_sashiko_patchset

    return fetch_sashiko_patchset(msgid, url)


def clear_b4_cache() -> None:
    """b4 caches series details per process; a refresh wants current ones."""
    from b4.review.checks import clear_sashiko_cache

    clear_sashiko_cache()


def _write(path: pathlib.Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'.{path.name}.tmp')
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
    os.replace(tmp, path)


def _read(path: pathlib.Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _key(entry: Mapping[str, Any]) -> list[Any]:
    """What a list row says about a series; details are refetched when it changes."""
    return [entry['status'], entry['counts']]


class SashikoCache:
    """var/sashiko: lists/<list-id>.json status rows, patchsets/<hash>.json details."""

    def __init__(self, root: pathlib.Path) -> None:
        self.root = root

    def _list_path(self, list_id: str) -> pathlib.Path:
        return self.root / 'lists' / f'{list_id}.json'

    def _detail_path(self, msgid: str) -> pathlib.Path:
        return self.root / 'patchsets' / f'{hashlib.sha1(msgid.encode()).hexdigest()}.json'

    def update_list(
        self,
        list_id: str,
        url: str,
        window_days: int,
        get: Get,
        now: datetime.datetime,
        since: Optional[datetime.datetime] = None,
        per_page: int = 50,
        max_pages: int = 40,
    ) -> int:
        """Refresh a list's rows back to *since* (at least the window); raises on failure.

        Statuses change within the window, so it is paged every time. Older rows,
        for threads still active though posted earlier, are fetched once: paging
        stops at the first page past the window that the cache already holds.
        """
        window = (now - datetime.timedelta(days=window_days)).timestamp()
        oldest = min(window, since.timestamp()) if since else window
        base = url.rstrip('/')
        previous = _read(self._list_path(list_id)) or {}
        old: dict[str, dict[str, Any]] = (
            previous.get('rows', {}) if previous.get('url') == base else {}
        )
        rows: dict[str, dict[str, Any]] = {}
        reached = float('inf')
        for number in range(1, max_pages + 1):
            params = {'mailing_list': group(list_id), 'page': number, 'per_page': per_page}
            items = [
                i
                for i in get(f'{base}/api/patchsets', params).get('items', [])
                if i.get('message_id')
            ]
            for item in items:
                reached = min(reached, item.get('date', 0))
                if item.get('date', 0) >= oldest:
                    rows[item['message_id']] = {
                        'id': item['id'],
                        'status': item.get('status', ''),
                        'counts': [int(item.get(c) or 0) for c in _COUNTS],
                        'date': item['date'],
                    }
            if len(items) < per_page or reached < oldest:
                break
            if any(i['message_id'] in old for i in items if i.get('date', 0) < window):
                break
        for msgid, entry in old.items():
            if msgid not in rows and oldest <= entry['date'] < reached:
                rows[msgid] = entry
        _write(self._list_path(list_id), {'url': base, 'rows': rows})
        return len(rows)

    def _rows(self) -> dict[str, tuple[str, dict[str, Any]]]:
        """Series message-id -> (base url, row), across lists."""
        found: dict[str, tuple[str, dict[str, Any]]] = {}
        for path in sorted((self.root / 'lists').glob('*.json')):
            data = _read(path)
            if not data:
                continue
            for msgid, entry in data.get('rows', {}).items():
                found[msgid] = (data['url'], entry)
        return found

    def update_details(self, wanted: Iterable[str], get_patchset: GetPatchset) -> tuple[int, int]:
        """Fetch changed details for wanted series and any with findings: (fetched, failed)."""
        wanted = set(wanted)
        fetched = failed = 0
        for msgid, (url, entry) in self._rows().items():
            if entry['status'] not in DETAILED or not (any(entry['counts']) or msgid in wanted):
                continue
            path = self._detail_path(msgid)
            old = _read(path)
            if old and old.get('key') == _key(entry):
                continue
            data = get_patchset(msgid, url)
            if not data:
                failed += 1
                continue
            _write(path, {'key': _key(entry), **_digest(data)})
            fetched += 1
        return fetched, failed

    def reviews(self) -> dict[str, SashikoReview]:
        """Series message-id -> review, with details where the cached ones still match."""
        result = {}
        for msgid, (url, entry) in self._rows().items():
            critical, high, medium, low = entry['counts']
            review = SashikoReview(
                status=entry['status'],
                critical=critical,
                high=high,
                medium=medium,
                low=low,
                url=f'{url}/#/patchset/{entry["id"]}',
            )
            data = _read(self._detail_path(msgid))
            if data and data.get('key') == _key(entry):
                review = dataclasses.replace(
                    review,
                    baseline=data['baseline'],
                    model=data['model'],
                    patches=tuple(_patch(p) for p in data['patches']),
                )
            result[msgid] = review
        return result


def _digest(data: Mapping[str, Any]) -> dict[str, Any]:
    """The parts of a /api/patchset response the client shows, findings parsed by b4."""
    from b4.review.checks import parse_sashiko_findings

    baseline = data.get('baseline') or {}
    branch, commit = baseline.get('branch') or '', baseline.get('commit') or ''
    if commit and (not branch or commit.startswith(branch)):
        label = commit[:12]  # Reviewed on a bare commit: no branch to name.
    else:
        label = f'{branch} @ {commit[:12]}' if commit else branch
    latest: dict[Any, Mapping[str, Any]] = {}
    for review in data.get('reviews', []):
        current = latest.get(review.get('patch_id'))
        if current is None or review.get('id', 0) > current.get('id', 0):
            latest[review.get('patch_id')] = review
    patches = []
    for patch in sorted(data.get('patches', []), key=lambda p: p.get('part_index', 0)):
        review = latest.get(patch.get('id'), {})
        findings = [
            {
                'severity': f['state'],
                'problem': f['problem'],
                'explanation': f['explanation'],
                'preexisting': f['preexisting'],
                'locations': _locations(f.get('locations')),
            }
            for f in parse_sashiko_findings(dict(review))
        ]
        patches.append(
            {
                'msgid': patch.get('message_id', ''),
                'part': patch.get('part_index', 0),
                'status': review.get('status') or patch.get('status', ''),
                'findings': findings,
                'inline_review': review.get('inline_review') or '',
            }
        )
    return {'baseline': label, 'model': data.get('model_name') or '', 'patches': patches}


def _locations(value: Any) -> list[list[Any]]:
    if isinstance(value, dict):
        value = [value]
    found = []
    for loc in value if isinstance(value, list) else ():
        if not isinstance(loc, dict):
            continue
        path = loc.get('file') or loc.get('file_name') or ''
        line = loc.get('line') or loc.get('line_number')
        if path and isinstance(line, int):
            found.append([path, line])
    return found


def _patch(data: Mapping[str, Any]) -> SashikoPatch:
    return SashikoPatch(
        msgid=data['msgid'],
        part=data['part'],
        status=data['status'],
        findings=tuple(
            Finding(
                severity=f['severity'],
                problem=f['problem'],
                explanation=f['explanation'],
                preexisting=f['preexisting'],
                locations=tuple((p, n) for p, n in f['locations']),
            )
            for f in data['findings']
        ),
        inline_review=data['inline_review'],
    )

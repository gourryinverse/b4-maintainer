# SPDX-License-Identifier: GPL-2.0-or-later
"""Synthetic fixtures: example.org identities only, no real mail or people."""

import copy
import datetime
import email.message
import email.utils
import hashlib
import json
import mailbox
import pathlib
import random
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

from .model import (
    Authority,
    Coverage,
    Patch,
    Progress,
    SashikoReview,
    Series,
    SeriesState,
    Tag,
    Trailers,
    WorkItem,
)
from .workspace import Snapshot, Workspace, build_items

ME = 'maintainer@example.org'
CONTRIBUTOR = ('Contributor One', 'contributor1@example.org')
EPOCH = datetime.datetime(2026, 10, 1, 12, 0, tzinfo=datetime.timezone.utc)

PROJECTS = {
    'linux': {
        'id': 'linux',
        'label': 'Linux',
        'short': 'lnx',
        'lists': [
            'linux-mm.kvack.org',
            'linux-kernel.vger.kernel.org',
            'linux-kselftest.vger.kernel.org',
            'linux-cxl.vger.kernel.org',
        ],
        'refs': [
            {'label': 'm', 'name': 'mainline/master', 'authority': 'mainline'},
            {'label': 'n', 'name': 'next/master', 'authority': 'acceptance'},
            {'label': 'p', 'name': 'maint/queue', 'authority': 'preliminary'},
        ],
    },
    'qemu': {
        'id': 'qemu',
        'label': 'QEMU',
        'short': 'qemu',
        'lists': ['qemu-devel.nongnu.org'],
        'refs': [{'label': 'm', 'name': 'upstream/master', 'authority': 'mainline'}],
    },
}

DEFAULT_PROFILES = [
    {
        'id': 'mm',
        'label': 'mm',
        'project': 'linux',
        'lists': ['linux-mm.kvack.org'],
        'paths': ['mm/', 'tools/testing/selftests/mm/'],
    },
    {
        'id': 'cxl',
        'label': 'cxl',
        'project': 'linux',
        'lists': ['linux-cxl.vger.kernel.org'],
        'paths': ['drivers/cxl/'],
    },
]


def workspace_config(
    projects: Sequence[str] = ('linux', 'qemu'),
    profiles: Optional[list[dict[str, Any]]] = None,
    sessions: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    return {
        'me': [ME],
        'projects': [copy.deepcopy(PROJECTS[p]) for p in projects],
        'profiles': copy.deepcopy(DEFAULT_PROFILES if profiles is None else profiles),
        'sessions': list(sessions),
    }


def _msgid(*parts: object) -> str:
    digest = hashlib.sha1('/'.join(map(str, parts)).encode()).hexdigest()[:12]
    return f'{digest}@example.org'


def synthetic_tags(trailers: Trailers, source: str) -> frozenset[Tag]:
    """Distinct example.org reviewers per kind; source keeps carried and replied apart."""
    return frozenset(
        (kind, f'{source}-{kind}{n}@example.org')
        for kind, count in (
            ('acked', trailers.acked),
            ('reviewed', trailers.reviewed),
            ('tested', trailers.tested),
        )
        for n in range(count)
    )


def make_series(
    subject: str,
    revision: int = 1,
    lists: Iterable[str] = (),
    paths: Iterable[str] = (),
    change_id: Optional[str] = None,
    msgid: Optional[str] = None,
    author_email: str = CONTRIBUTOR[1],
    author_name: str = 'Synthetic Author',
    complete: bool = True,
    trailers: Trailers = Trailers(),
    carried: Trailers = Trailers(),
    sent: Optional[datetime.datetime] = None,
    expected: int = 1,
    preview: str = '',
) -> Series:
    msgid = msgid or _msgid(subject, revision, change_id)
    identity = f'change-id:{change_id}' if change_id else msgid
    return Series(
        identity=identity,
        subject=subject,
        revision=revision,
        expected=expected,
        complete=complete,
        has_cover=expected > 1,
        author_name=author_name,
        author_email=author_email,
        sent=sent or EPOCH,
        patches=(
            Patch(
                1,
                msgid,
                subject,
                synthetic_tags(trailers, 'reply'),
                synthetic_tags(carried, 'carried'),
            ),
        ),
        change_id=change_id,
        lists=tuple(lists),
        paths=tuple(paths),
        msgids=(msgid,),
        preview_label='Cover letter' if expected > 1 else 'Patch 1/1 (no cover letter)',
        preview=preview,
    )


def make_item(
    subject: str = 'subject',
    revision: int = 1,
    complete: bool = True,
    acked: int = 0,
    reviewed: int = 0,
    tested: int = 0,
    unread: int = 0,
    fixes_pending: bool = False,
    draft: bool = False,
    progress: Progress = Progress.NONE,
    to_review: bool = False,
    tree: Sequence[tuple[str, Authority, Coverage]] = (),
    project: Optional[str] = None,
    short: str = '',
    candidates: tuple[str, ...] = (),
    preview: str = '',
    carried: Trailers = Trailers(),
    author_name: str = 'Synthetic Author',
    author_email: str = CONTRIBUTOR[1],
    author_role: str = '',
    sashiko: Optional[SashikoReview] = None,
) -> WorkItem:
    series = make_series(
        subject,
        revision,
        author_email=author_email,
        author_name=author_name,
        complete=complete,
        trailers=Trailers(acked, reviewed, tested),
        carried=carried,
        preview=preview,
    )
    state = SeriesState(unread, fixes_pending, progress, draft, to_review, tuple(tree))
    return WorkItem(
        series,
        state,
        project,
        short,
        candidates,
        versions=(revision,),
        author_role=author_role,
        sashiko=sashiko,
    )


@dataclass(frozen=True)
class PatchSpec:
    path: str
    summary: str = ''
    trailers: tuple[str, ...] = ()


@dataclass(frozen=True)
class Followup:
    """A reply; target 0 is the cover letter.

    parent is the 1-based index of an earlier followup to reply to (0 replies
    to the target); threaded=False omits In-Reply-To/References.
    """

    target: int
    trailer: str = ''
    body: str = ''
    parent: int = 0
    threaded: bool = True
    sender: tuple[str, str] = ('Reviewer One', 'reviewer1@example.org')
    quote_lines: int = 0


@dataclass
class SeriesMail:
    path: pathlib.Path
    msgids: dict[int, str] = field(default_factory=dict)
    followups: list[str] = field(default_factory=list)


def _list_address(list_id: str) -> str:
    local, domain = list_id.split('.', 1)
    return f'{local}@{domain}'


def _patch_body(spec: PatchSpec, author: tuple[str, str], change_id: Optional[str]) -> str:
    footer = f'\nchange-id: {change_id}\n' if change_id else ''
    return (
        f'Synthetic change touching {spec.path}.\n\n'
        + ''.join(f'{t}\n' for t in spec.trailers)
        + f'Signed-off-by: {author[0]} <{author[1]}>\n'
        '---\n'
        f' {spec.path} | 2 +-\n'
        ' 1 file changed, 1 insertion(+), 1 deletion(-)\n\n'
        f'diff --git a/{spec.path} b/{spec.path}\n'
        'index 1111111..2222222 100644\n'
        f'--- a/{spec.path}\n'
        f'+++ b/{spec.path}\n'
        '@@ -1 +1 @@\n'
        '-old\n'
        '+new\n'
        f'{footer}'
        '-- \n'
        '2.51.0\n'
    )


def write_series_mbox(
    path: 'pathlib.Path | str',
    subject: str,
    revision: int = 1,
    patches: Optional[Sequence[PatchSpec]] = None,
    cover: Optional[bool] = None,
    lists: Sequence[str] = ('linux-mm.kvack.org',),
    change_id: Optional[str] = None,
    omit: Iterable[int] = (),
    followups: Sequence[Followup] = (),
    author: tuple[str, str] = CONTRIBUTOR,
    append: bool = False,
    date: Optional[datetime.datetime] = None,
    cover_text: Optional[str] = None,
) -> SeriesMail:
    """Write one synthetic thread in b4-parsable form and return its message IDs."""
    path = pathlib.Path(path)
    patches = list(patches or [PatchSpec('mm/example.c')])
    total = len(patches)
    cover = total > 1 if cover is None else cover
    date = date or EPOCH
    omit = set(omit)
    result = SeriesMail(path)
    prefix = 'PATCH' if revision == 1 else f'PATCH v{revision}'

    def message(n: int, subj: str, body: str, sender: tuple[str, str], parent: Optional[str]):
        msg = email.message.EmailMessage()
        msg['From'] = email.utils.formataddr(sender)
        msg['To'] = email.utils.formataddr(('Maintainer', ME))
        msg['Cc'] = ', '.join(_list_address(lst) for lst in lists)
        msg['Subject'] = subj
        msg['Date'] = email.utils.format_datetime(date + datetime.timedelta(minutes=n))
        msgid = _msgid(path.name, subject, revision, n, subj)
        msg['Message-ID'] = f'<{msgid}>'
        if parent:
            msg['In-Reply-To'] = f'<{parent}>'
            msg['References'] = f'<{parent}>'
        if lists:
            msg['List-Id'] = f'<{lists[0]}>'
        msg.set_content(body)
        return msgid, msg

    messages = []
    root: Optional[str] = None
    if cover:
        text = cover_text or 'Synthetic cover letter.'
        body = f'{text}\n\n---\nbase-commit: {"0" * 40}\n'
        if change_id:
            body += f'change-id: {change_id}\n'
        root, msg = message(0, f'[{prefix} 0/{total}] {subject}', body, author, None)
        result.msgids[0] = root
        messages.append(msg)
    for n, spec in enumerate(patches, start=1):
        counter = f' {n}/{total}' if cover or total > 1 else ''
        title = spec.summary or (subject if total == 1 else f'{subject} (part {n})')
        body = _patch_body(spec, author, None if cover else change_id)
        msgid, msg = message(n, f'[{prefix}{counter}] {title}', body, author, root)
        result.msgids[n] = msgid
        root = root or msgid
        if n not in omit:
            messages.append(msg)
    for k, fu in enumerate(followups, start=1):
        parent = result.followups[fu.parent - 1] if fu.parent else result.msgids[fu.target]
        sender = email.utils.parseaddr(fu.trailer.split(':', 1)[1]) if fu.trailer else fu.sender
        text = '\n\n'.join(t for t in (fu.body, fu.trailer) if t)
        quoted = ''.join(f'> quoted line {n}\n' for n in range(1, fu.quote_lines + 1))
        quoted = quoted or '> Synthetic quoted text.\n'
        body = f'{quoted}\n{text}\n'
        msgid, msg = message(
            100 + k, f'Re: {subject}', body, sender, parent if fu.threaded else None
        )
        result.followups.append(msgid)
        messages.append(msg)

    box = mailbox.mbox(path)
    try:
        if not append:
            box.clear()
        for msg in messages:
            box.add(msg)
        box.flush()
    finally:
        box.close()
    return result


LONG_COVER = 'Synthetic cover letter.\n\n' + '\n'.join(
    f'Paragraph {n}: synthetic rationale text that wraps across the preview pane width.'
    for n in range(1, 41)
)
SYNTHETIC_COVER = '\n'.join(f'Synthetic cover line {n}.' for n in range(40))


def _tree(*entries: tuple[str, str, str]) -> list[list[str]]:
    return [list(e) for e in entries]


def build_demo_workspace(
    root: 'pathlib.Path | str', profiles: Optional[list] = None
) -> pathlib.Path:
    """A small multi-project workspace exercising the row and view states."""
    root = pathlib.Path(root)
    mail = root / 'mail'
    mail.mkdir(parents=True, exist_ok=True)
    states: dict[str, Any] = {}

    def add(name, state, **kwargs):
        written = write_series_mbox(mail / f'{name}.mbx', **kwargs)
        cid = kwargs.get('change_id')
        identity = f'change-id:{cid}' if cid else written.msgids[0 if 0 in written.msgids else 1]
        states[identity] = state
        return identity

    day = datetime.timedelta(days=1)
    session_series = add(
        'mm-foo',
        {
            'unread': 1,
            'fixes_pending': True,
            'draft': True,
            'responsible': True,
            'tree': _tree(('n', 'acceptance', 'partial')),
        },
        subject='mm: fix foo leak',
        revision=3,
        cover_text=LONG_COVER,
        patches=[
            PatchSpec(
                f'mm/foo{n}.c',
                trailers=('Reviewed-by: Reviewer Three <reviewer3@example.org>',) if n == 2 else (),
            )
            for n in range(1, 6)
        ],
        change_id='20260930-mm-foo-aaaaaaaaaaaa',
        followups=[
            Followup(0, 'Acked-by: Reviewer One <reviewer1@example.org>'),
            Followup(3, 'Reviewed-by: Reviewer Two <reviewer2@example.org>'),
            Followup(3, body='Comment after a long quote.', quote_lines=10),
            Followup(3, body='Thanks, will fix.', parent=3, sender=CONTRIBUTOR),
        ],
        date=EPOCH + 9 * day,
    )
    add(
        'cxl-bar',
        {
            'progress': 'partial',
            'responsible': True,
            'tree': _tree(('n', 'acceptance', 'unchecked')),
        },
        subject='cxl: add bar region',
        revision=2,
        lists=['linux-cxl.vger.kernel.org'],
        patches=[PatchSpec('drivers/cxl/region.c'), PatchSpec('drivers/cxl/core.c')],
        followups=[Followup(1, 'Reviewed-by: Reviewer Two <reviewer2@example.org>')],
        date=EPOCH + 8 * day,
    )
    add(
        'selftest-merge',
        {'to_review': True, 'unread': 0},
        subject='selftests/mm: merge: keep far targets in the carveout',
        revision=2,
        cover=False,
        lists=['linux-kselftest.vger.kernel.org', 'linux-mm.kvack.org'],
        patches=[PatchSpec('tools/testing/selftests/mm/merge.c')],
        date=EPOCH + 7 * day,
    )
    add(
        'hw-cxl',
        {'unread': 2},
        subject='hw/cxl: emulate bar region',
        lists=['linux-cxl.vger.kernel.org', 'qemu-devel.nongnu.org'],
        patches=[PatchSpec('hw/cxl/cxl-device.c'), PatchSpec('include/hw/cxl/cxl.h')],
        date=EPOCH + 6 * day,
    )
    add(
        'mm-rework',
        {'to_review': True},
        subject='mm: rework reclaim thing',
        patches=[PatchSpec('mm/vmscan.c'), PatchSpec('mm/swap.c'), PatchSpec('mm/memcg.c')],
        omit=(2,),
        date=EPOCH + 5 * day,
    )
    add(
        'mm-landed',
        {'to_review': True, 'tree': _tree(('m', 'mainline', 'full'), ('n', 'acceptance', 'full'))},
        subject='mm: already landed change',
        date=EPOCH + 4 * day,
    )
    add(
        'mm-mine',
        {
            'progress': 'done',
            'tree': _tree(('m', 'mainline', 'none'), ('p', 'preliminary', 'full')),
        },
        subject='mm: maintainer own change',
        author=('Maintainer', ME),
        date=EPOCH + 3 * day,
    )
    add(
        'qemu-virtio',
        {'tree': _tree(('m', 'mainline', 'stale'))},
        subject='virtio: tidy queue setup',
        lists=['qemu-devel.nongnu.org'],
        patches=[PatchSpec('hw/virtio/virtio.c')],
        followups=[Followup(1, 'Tested-by: Tester One <tester1@example.org>')],
        date=EPOCH + 2 * day,
    )
    add(
        'net-misc',
        {'tree': _tree(('m', 'mainline', 'none'), ('n', 'acceptance', 'none'))},
        subject='net: unrelated cleanup',
        lists=['linux-kernel.vger.kernel.org'],
        patches=[PatchSpec('net/core/dev.c')],
        date=EPOCH + 1 * day,
    )

    sessions = [
        {
            'id': 'A',
            'series': session_series,
            'project': 'linux',
            'backend': 'claude',
            'state': 'running',
            'approvals': 1,
        }
    ]
    config = workspace_config(profiles=profiles, sessions=sessions)
    (root / 'workspace.json').write_text(json.dumps(config, indent=2) + '\n')
    (root / 'state.json').write_text(json.dumps(states, indent=2) + '\n')
    return root


def synthetic_snapshot(n: int, seed: int = 0) -> Snapshot:
    """In-memory workspace with n series for performance targets."""
    rng = random.Random(seed)
    ws = Workspace.from_config(workspace_config())
    linux = PROJECTS['linux']['lists']
    areas = ['mm', 'cxl', 'net', 'fs', 'virtio', 'selftests/mm']
    coverages = list(Coverage)
    series, states, responsible = [], {}, set()
    for k in range(n):
        area = rng.choice(areas)
        lists = ['qemu-devel.nongnu.org'] if area == 'virtio' else [rng.choice(linux)]
        s = make_series(
            f'{area}: synthetic change {k}',
            revision=rng.randint(1, 6),
            lists=lists,
            paths=[f'{area}/file{k % 50}.c'],
            complete=rng.random() > 0.1,
            trailers=Trailers(rng.randint(0, 2), rng.randint(0, 2), rng.randint(0, 1)),
            sent=EPOCH - datetime.timedelta(minutes=k),
            msgid=f'synthetic-{k}@example.org',
            preview=SYNTHETIC_COVER,
        )
        series.append(s)
        tree = [('n', Authority.ACCEPTANCE, rng.choice(coverages))]
        if rng.random() < 0.08:
            tree.append(('m', Authority.MAINLINE, Coverage.FULL))
        states[s.identity] = SeriesState(
            unread=rng.choice([0, 0, 0, 1, 3]),
            fixes_pending=rng.random() < 0.05,
            progress=rng.choice(list(Progress)),
            draft=rng.random() < 0.05,
            to_review=rng.random() < 0.2,
            tree=tuple(tree),
        )
        if rng.random() < 0.3:
            responsible.add(s.identity)
    return Snapshot(ws, build_items(series, states, ws, responsible=responsible))

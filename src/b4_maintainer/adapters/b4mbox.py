# SPDX-License-Identifier: GPL-2.0-or-later
"""Parse local mailboxes into series records through b4's LoreMailbox."""

import email.utils
import os
from typing import Iterable, Optional

import b4
from b4.review_tui._lite_app import ThreadNode, build_thread_tree

from ..model import Message, Patch, Post, Reply, Series, SeriesThread, Tag, Trailers

TRAILER_KINDS = {'acked-by': 'acked', 'reviewed-by': 'reviewed', 'tested-by': 'tested'}


def _list_ids(msgs: Iterable['b4.LoreMessage']) -> tuple[str, ...]:
    found: dict[str, None] = {}
    for lmsg in msgs:
        for header in lmsg.msg.get_all('List-Id', []):
            _, addr = email.utils.parseaddr(str(header))
            if addr:
                found[addr.lower()] = None
    return tuple(found)


def _recipients(msgs: Iterable['b4.LoreMessage']) -> tuple[str, ...]:
    found: dict[str, None] = {}
    for lmsg in msgs:
        for header in ('To', 'Cc'):
            for _, addr in email.utils.getaddresses(lmsg.msg.get_all(header, [])):
                if '@' in addr:
                    found[addr.lower()] = None
    return tuple(found)


def _tags(trailers: Iterable['b4.LoreTrailer']) -> frozenset[Tag]:
    tags = set()
    for trailer in trailers:
        kind = TRAILER_KINDS.get(trailer.name.lower())
        if kind is not None:
            who = trailer.addr[1].lower() if trailer.addr else trailer.value.lower()
            tags.add((kind, who))
    return frozenset(tags)


def _replied(lmsg: Optional['b4.LoreMessage']) -> frozenset[Tag]:
    return _tags(lmsg.followup_trailers) if lmsg is not None else frozenset()


STABLE_ADDRESSES = frozenset({'stable@vger.kernel.org', 'stable@kernel.org'})


def _fix_tags(lmsg: 'b4.LoreMessage') -> tuple[tuple[str, ...], bool]:
    """Fixes: trailers in the commit message, and a stable Cc there or in the mail headers."""
    _headers, _message, trailers, _basement, _signature = b4.LoreMessage.get_body_parts(lmsg.body)
    fixes = tuple(t.value for t in trailers if t.name.lower() == 'fixes')
    cced = {t.addr[1].lower() for t in trailers if t.name.lower() == 'cc' and t.addr}
    for header in ('To', 'Cc'):
        cced.update(a.lower() for _, a in email.utils.getaddresses(lmsg.msg.get_all(header, [])))
    return fixes, bool(cced & STABLE_ADDRESSES)


def _carried(lmsg: 'b4.LoreMessage') -> frozenset[Tag]:
    """Tags in the patch's own commit message, usually from earlier revisions."""
    _headers, _message, trailers, _basement, _signature = b4.LoreMessage.get_body_parts(lmsg.body)
    return _tags(trailers)


def _paths(patches: Iterable['b4.LoreMessage']) -> tuple[str, ...]:
    found: dict[str, None] = {}
    for lmsg in patches:
        for _, _, newfile, _ in sorted(b4.LoreMessage.get_indexes(lmsg.body)):
            found[newfile] = None
    return tuple(found)


def _preview(cover: Optional['b4.LoreMessage'], first: 'b4.LoreMessage', expected: int):
    """Cover letter, or the first patch's message and diffstat when there is none."""
    if cover is not None:
        return 'Cover letter', cover.body.strip('\n')
    description = first.body.split('\ndiff --git ', 1)[0]
    return f'Patch {first.counter}/{expected} (no cover letter)', description.strip('\n')


def _series(lser: 'b4.LoreSeries', source: str) -> Series:
    cover = lser.patches[0]
    patches = [p for p in lser.patches[1:] if p is not None]
    change_id = getattr(lser, 'change_id', None)
    if change_id:
        identity = f'change-id:{change_id}'
    elif cover is not None:
        identity = cover.msgid
    else:
        identity = patches[0].msgid
    present = ([cover] if cover is not None else []) + patches
    preview_label, preview = _preview(cover, patches[0], lser.expected)
    return Series(
        identity=identity,
        subject=(cover or patches[0]).lsubject.subject,
        revision=lser.revision,
        expected=lser.expected,
        complete=bool(lser.complete),
        has_cover=bool(lser.has_cover),
        author_name=lser.fromname,
        author_email=lser.fromemail,
        sent=present[0].date,
        patches=tuple(
            Patch(p.counter, p.msgid, p.lsubject.subject, _replied(p), _carried(p), *_fix_tags(p))
            for p in patches
        ),
        cover_trailers=Trailers.of(_replied(cover)),
        change_id=change_id,
        lists=_list_ids(present),
        recipients=_recipients(present),
        paths=_paths(patches),
        msgids=tuple(p.msgid for p in present),
        preview_label=preview_label,
        preview=preview,
        source=source,
    )


def parse_mbox(path: 'os.PathLike[str] | str') -> list[Series]:
    """Return every revision found in a mailbox or maildir, oldest first.

    Offline: lore trailer searches and reroll gap-filling stay disabled so
    completeness and trailers reflect the local mail only.
    """
    source = os.fspath(path)
    lmbx = _mailbox(source)
    return [_series(lser, source) for lser in _revisions(lmbx).values()]


def _mailbox(source: str) -> 'b4.LoreMailbox':
    lmbx = b4.LoreMailbox()
    for msg in b4.get_msgs_from_mailbox_or_maildir(source):
        lmbx.add_message(msg)
    return lmbx


def _revisions(lmbx: 'b4.LoreMailbox') -> dict[int, 'b4.LoreSeries']:
    result = {}
    for revision in sorted(lmbx.series):
        lser = lmbx.get_series(revision, reroll=False, codereview_trailers=False)
        if lser is not None and any(lser.patches[1:]):
            result[revision] = lser
    return result


def _message(lmsg: 'b4.LoreMessage') -> Message:
    # b4 collects trailers from replies only, so postings carry no tags here.
    return Message(
        lmsg.msgid,
        lmsg.fromname,
        lmsg.fromemail,
        lmsg.date,
        lmsg.full_subject,
        lmsg.body,
        _tags(lmsg.trailers),
    )


def load_thread(series: Series) -> SeriesThread:
    """Load one revision's postings with discussion grouped by posting.

    Threading comes from b4's build_thread_tree. Postings of every revision
    split the tree, so a reply belongs to its nearest posting; messages that
    reach no posting are unattached. Subjects never link messages.
    """
    if not series.source:
        raise ValueError('no mail source for this series')
    if series.discussion:
        return _discussion(series)
    lmbx = _mailbox(series.source)
    revisions = _revisions(lmbx)
    lser = revisions[series.revision]
    nodes = {node.lmsg.msgid: node for node in build_thread_tree(lmbx)}
    posts = {p.msgid for rev in revisions.values() for p in rev.patches if p is not None}

    def replies(children: list[ThreadNode], depth: int, seen: set[str]) -> list[Reply]:
        found = []
        for child in children:
            msgid = child.lmsg.msgid
            if msgid in posts or msgid in seen:
                continue
            seen.add(msgid)
            found.append(Reply(_message(child.lmsg), depth))
            found.extend(replies(child.children, depth + 1, seen))
        return found

    reached: set[str] = set()
    for msgid in posts:
        replies(nodes[msgid].children, 0, reached)

    def post(counter: int, lmsg: 'b4.LoreMessage') -> Post:
        description, _, diff = lmsg.body.partition('\ndiff --git ')
        return Post(
            counter,
            _message(lmsg),
            description.strip('\n'),
            f'diff --git {diff}'.rstrip('\n') if diff else '',
            tuple(replies(nodes[lmsg.msgid].children, 0, set())),
        )

    cover = lser.patches[0]
    # Roots follow b4's tree, not In-Reply-To: b4 may break a reply cycle by
    # making a message a root while its header still names a parent.
    children = {c.lmsg.msgid for n in nodes.values() for c in n.children}
    roots = [
        n
        for n in nodes.values()
        if n.lmsg.msgid not in posts
        and n.lmsg.msgid not in reached
        and n.lmsg.msgid not in children
    ]
    roots.sort(key=lambda n: n.lmsg.date)
    unattached: list[Reply] = []
    for root in roots:
        unattached.append(Reply(_message(root.lmsg), 0))
        unattached.extend(replies(root.children, 1, set()))
    return SeriesThread(
        identity=series.identity,
        revision=series.revision,
        cover=post(0, cover) if cover is not None else None,
        patches=tuple(post(p.counter, p) for p in lser.patches[1:] if p is not None),
        unattached=tuple(unattached),
    )


def _discussion(series: Series) -> SeriesThread:
    """A conversation without patches: its first message, with every reply beneath it."""
    lmbx = _mailbox(series.source)
    nodes = {node.lmsg.msgid: node for node in build_thread_tree(lmbx)}
    first = series.identity.removeprefix('thread:')

    def replies(children: list[ThreadNode], depth: int, seen: set[str]) -> list[Reply]:
        found = []
        for child in children:
            if child.lmsg.msgid in seen:
                continue
            seen.add(child.lmsg.msgid)
            found.append(Reply(_message(child.lmsg), depth))
            found.extend(replies(child.children, depth + 1, seen))
        return found

    root = nodes.get(first)
    if root is None:
        raise ValueError(f'first message {first} is not in the cached thread')
    reached = {first}
    cover = Post(
        0,
        _message(root.lmsg),
        root.lmsg.body.strip('\n'),
        '',
        tuple(replies(root.children, 0, reached)),
    )
    children = {c.lmsg.msgid for n in nodes.values() for c in n.children}
    others = sorted(
        (n for n in nodes.values() if n.lmsg.msgid not in reached | children),
        key=lambda n: n.lmsg.date,
    )
    unattached: list[Reply] = []
    for node in others:
        unattached.append(Reply(_message(node.lmsg), 0))
        unattached.extend(replies(node.children, 1, set(reached)))
    return SeriesThread(
        identity=series.identity,
        revision=series.revision,
        cover=cover,
        patches=(),
        unattached=tuple(unattached),
        discussion=True,
    )

# SPDX-License-Identifier: GPL-2.0-or-later
"""Pre-apply reader content: the posting tree with collapsible replies, and message blocks."""

import dataclasses
import re
from dataclasses import dataclass
from typing import Collection, Optional

from rich.style import Style

from .model import (
    Finding,
    Message,
    Post,
    Reply,
    SashikoPatch,
    SashikoReview,
    SeriesThread,
    Tag,
    Trailers,
    WorkItem,
)
from .widgets import Block

UNATTACHED = 'unattached'
# Message IDs of Sashiko reviews built from its API rather than read from mail.
SASHIKO_PREFIX = 'sashiko:'

HEADER = Style(bold=True)
QUOTE = Style(dim=True)
SEVERITY_STYLES = {
    'critical': Style(color='red', bold=True),
    'high': Style(color='red'),
    'medium': Style(color='yellow'),
    'low': Style(dim=True),
}
MARKED = Style(underline=True)
_HUNK = re.compile(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@')


def diff_style(line: str) -> Style:
    if line == '-- ':
        return Style()  # Mail signature separator after the diff, not a removed line.
    if line.startswith(('diff --git', '+++', '---', 'index ')):
        return Style(bold=True)
    if line.startswith('@@'):
        return Style(color='cyan')
    if line.startswith('+'):
        return Style(color='green')
    if line.startswith('-'):
        return Style(color='red')
    return Style()


def prose_style(line: str) -> Style:
    return QUOTE if line.lstrip().startswith('>') else Style()


def post_keys(thread: SeriesThread) -> list[str]:
    keys = ['0'] if thread.cover else []
    keys += [str(p.counter) for p in thread.patches]
    if thread.unattached:
        keys.append(UNATTACHED)
    return keys


def find_post(thread: SeriesThread, key: str) -> Optional[Post]:
    if key == '0':
        return thread.cover
    return next((p for p in thread.patches if str(p.counter) == key), None)


def _trailers(item: WorkItem, counter: int) -> Trailers:
    if counter == 0:
        return item.series.cover_trailers
    patch = next((p for p in item.series.patches if p.counter == counter), None)
    return patch.trailers if patch else Trailers()


def _counts(t: Trailers) -> str:
    return f'A:{t.acked} R:{t.reviewed} T:{t.tested}'


def trailer_line(item: WorkItem, counter: int) -> str:
    """All evidence, split into tags carried in the commit and tags from replies."""
    patch = next((p for p in item.series.patches if p.counter == counter), None)
    if patch is None:
        return f'Trailers: {_counts(_trailers(item, counter))}'
    return (
        f'Trailers: {_counts(patch.trailers)} '
        f'(carried {_counts(patch.carried_trailers)} · replies {_counts(patch.reply_trailers)})'
    )


def _marks(t: Trailers, absent: str) -> str:
    return ''.join(
        c if n else absent for c, n in (('A', t.acked), ('R', t.reviewed), ('T', t.tested))
    )


def sashiko_patch(review: Optional[SashikoReview], post: Post) -> Optional[SashikoPatch]:
    """Sashiko's review of one posted patch, matched by message ID, else by part."""
    if review is None or post.counter == 0:
        return None
    found = next((p for p in review.patches if p.msgid == post.message.msgid), None)
    return found or next((p for p in review.patches if p.part == post.counter), None)


def is_sashiko(message: Message, bots: Collection[str]) -> bool:
    return message.msgid.startswith(SASHIKO_PREFIX) or message.author_email.lower() in bots


def with_sashiko(
    thread: SeriesThread, review: Optional[SashikoReview], bots: Collection[str]
) -> SeriesThread:
    """Add Sashiko's API review as a reply to each patch whose Sashiko mail is not cached."""
    if review is None or not review.patches:
        return thread
    posts = []
    for post in thread.patches:
        found = sashiko_patch(review, post)
        if found is None or not found.inline_review:
            posts.append(post)
            continue
        if any(is_sashiko(r.message, bots) for r in post.replies):
            posts.append(post)  # Its mail is here: never both.
            continue
        message = Message(
            SASHIKO_PREFIX + post.message.msgid,
            'Sashiko',
            '',
            None,
            f'Sashiko review: {post.message.subject}',
            found.inline_review,
        )
        posts.append(dataclasses.replace(post, replies=post.replies + (Reply(message, 0),)))
    return dataclasses.replace(thread, patches=tuple(posts))


def _new_findings(found: Optional[SashikoPatch]) -> list[Finding]:
    return [f for f in found.findings if not f.preexisting] if found else []


def post_label(thread: SeriesThread, key: str, item: WorkItem, absent: str = '·') -> str:
    if key == UNATTACHED:
        return f'Unattached ({len(thread.unattached)})'
    post = find_post(thread, key)
    assert post is not None
    total = item.series.expected
    replies = f' ({len(post.replies)})' if post.replies else ''
    if thread.discussion:
        return f'discussion{replies}'
    if post.counter == 0:
        return f'0/{total} cover{replies}'
    found = len(_new_findings(sashiko_patch(item.sashiko, post)))
    badge = f' {found}!' if found else ''
    marks = _marks(_trailers(item, post.counter), absent)
    return f'{post.counter}/{total} {marks}{replies}{badge}'


def thread_replies(thread: SeriesThread, key: str) -> tuple[Reply, ...]:
    if key == UNATTACHED:
        return thread.unattached
    post = find_post(thread, key)
    assert post is not None
    return post.replies


def tree_art(depths: list[int]) -> list[str]:
    """Connector art for DFS-ordered depths, drawn under an implicit root line."""
    art = []
    for i, depth in enumerate(depths):
        prefix = ''
        for level in range(depth):
            prefix += '  ' if _is_last(depths, i, level) else '│ '
        art.append(prefix + ('└─>' if _is_last(depths, i, depth) else '├─>'))
    return art


def _is_last(depths: list[int], i: int, level: int) -> bool:
    """No later sibling at `level` before the subtree containing depths[i] closes."""
    for depth in depths[i + 1 :]:
        if depth < level:
            return True
        if depth == level:
            return False
    return True


def _tag_marks(tags: frozenset[Tag]) -> str:
    kinds = {kind for kind, _ in tags}
    return ''.join(
        c for c, kind in (('A', 'acked'), ('R', 'reviewed'), ('T', 'tested')) if kind in kinds
    )


def root_label(thread: SeriesThread, key: str) -> str:
    if key == UNATTACHED:
        return f'Unattached replies ({len(thread.unattached)})'
    post = find_post(thread, key)
    assert post is not None
    return post.message.subject


def reply_labels(
    replies: tuple[Reply, ...], unread: Collection[str] = (), bots: Collection[str] = ()
) -> list[str]:
    """Tree labels; unread replies (to the maintainer's mail) are marked ●, Sashiko's ◆."""
    labels = []
    for art, reply in zip(tree_art([r.depth for r in replies]), replies):
        m = reply.message
        date = f' {m.date:%m-%d}' if m.date else ''
        marks = _tag_marks(m.tags)
        dot = '● ' if m.msgid in unread else ''
        name = m.author_name
        if m.msgid.startswith(SASHIKO_PREFIX):
            name = '◆ Sashiko (not on list)'
        elif is_sashiko(m, bots):
            name = '◆ Sashiko'
        labels.append(f'{art} {dot}{name}{date}{" " + marks if marks else ""}')
    return labels


@dataclass(frozen=True)
class TreeRow:
    """One reader-tree row: a posting (node 0) or its nth reply."""

    key: str
    node: int
    label: str


def initially_expanded(thread: SeriesThread, unread: Collection[str]) -> set[str]:
    """Postings whose replies include unread ones start expanded."""
    return {
        key
        for key in post_keys(thread)
        if any(r.message.msgid in unread for r in thread_replies(thread, key))
    }


def tree_rows(
    thread: SeriesThread,
    item: WorkItem,
    expanded: Collection[str],
    unread: Collection[str] = (),
    absent: str = '·',
    bots: Collection[str] = (),
) -> list[TreeRow]:
    """Postings in series order, each followed by its replies when expanded.

    ▸/▾ mark postings with replies; a collapsed one with unread replies ends in ●.
    """
    rows = []
    for key in post_keys(thread):
        replies = thread_replies(thread, key)
        is_open = key in expanded
        marker = ('▾ ' if is_open else '▸ ') if replies else '  '
        label = marker + post_label(thread, key, item, absent)
        if not is_open and any(r.message.msgid in unread for r in replies):
            label += ' ●'
        rows.append(TreeRow(key, 0, label))
        if is_open:
            for n, reply_label in enumerate(reply_labels(replies, unread, bots), 1):
                rows.append(TreeRow(key, n, '  ' + reply_label))
    return rows


def content_title(thread: SeriesThread, key: str, reply: Optional[Reply], total: int) -> str:
    if reply is not None:
        date = f' · {reply.message.date:%Y-%m-%d %H:%M}' if reply.message.date else ''
        return f'{reply.message.author_name}{date}'
    if key == UNATTACHED:
        return 'Unattached replies'
    post = find_post(thread, key)
    assert post is not None
    if thread.discussion:
        return 'Discussion'
    return 'Cover letter' if post.counter == 0 else f'Patch {post.counter}/{total}'


def _header(message: Message) -> str:
    date = f' · {message.date:%Y-%m-%d %H:%M %z}' if message.date else ''
    return f'{message.author_name} <{message.author_email}>{date}'


def _is_quote(line: str) -> bool:
    return line.lstrip().startswith('>')


def collapse_quotes(text: str, expand: bool, keep: int = 2, threshold: int = 3) -> list[Block]:
    """Fold quoted runs longer than threshold to a marker plus their last `keep` lines."""
    lines = text.split('\n')
    blocks: list[Block] = []
    i = 0
    while i < len(lines):
        j = i
        while j < len(lines) and _is_quote(lines[j]) == _is_quote(lines[i]):
            j += 1
        run = lines[i:j]
        if not _is_quote(lines[i]):
            blocks.append(Block('\n'.join(run)))
        elif expand or len(run) <= threshold:
            blocks.append(Block('\n'.join(run), style=QUOTE))
        else:
            hidden = len(run) - keep
            blocks.append(Block(f'> [{hidden} quoted lines]', style=QUOTE))
            blocks.append(Block('\n'.join(run[-keep:]), style=QUOTE))
        i = j
    return blocks


def has_long_quote(reply: Reply, threshold: int = 3) -> bool:
    run = 0
    for line in reply.message.body.split('\n'):
        run = run + 1 if _is_quote(line) else 0
        if run > threshold:
            return True
    return False


def message_blocks(reply: Reply, expand: bool) -> list[Block]:
    m = reply.message
    blocks = [Block(m.subject, style=HEADER), Block(f'From: {_header(m)}')]
    marks = _tag_marks(m.tags)
    if marks:
        blocks.append(Block(f'Gives: {marks}'))
    blocks.append(Block(''))
    return blocks + collapse_quotes(m.body.strip('\n'), expand)


def finding_anchor(counter: int, n: int) -> str:
    return f'finding-{counter}-{n}'


def finding_anchors(thread: SeriesThread, item: WorkItem) -> list[tuple[str, str]]:
    """(posting key, anchor) for every Sashiko finding, in series order."""
    return [
        (str(post.counter), finding_anchor(post.counter, n))
        for post in thread.patches
        if (found := sashiko_patch(item.sashiko, post)) is not None
        for n in range(1, len(found.findings) + 1)
    ]


def diff_marks(diff: str, findings: tuple[Finding, ...]) -> dict[int, list[int]]:
    """Diff line index -> findings (by index) located at that post-patch file line."""
    wanted: dict[tuple[str, int], list[int]] = {}
    for n, finding in enumerate(findings):
        for location in finding.locations:
            wanted.setdefault(location, []).append(n)
    marks: dict[int, list[int]] = {}
    path, line, left = '', 0, 0
    for index, text in enumerate(diff.split('\n')):
        hunk = _HUNK.match(text)
        if hunk:
            line, left = int(hunk.group(1)), int(hunk.group(2) or 1)
        elif text.startswith('+++ '):
            path, left = (text[6:] if text.startswith('+++ b/') else ''), 0
        elif left and not text.startswith(('-', '\\')):
            for n in wanted.get((path, line), []):
                marks.setdefault(index, []).append(n)
            line, left = line + 1, left - 1
    return marks


def _severity(finding: Finding) -> str:
    return finding.severity + (' (pre-existing)' if finding.preexisting else '')


def _sashiko_blocks(item: WorkItem, post: Post) -> tuple[list[Block], list[Block]]:
    """(summary blocks before the diff, the diff with findings marked under their lines)."""
    found = sashiko_patch(item.sashiko, post)
    diff = post.diff or 'No diff.'
    if found is None or item.sashiko is None:
        return [], [Block(diff, wrap=False, style=diff_style)]
    marks = diff_marks(diff, found.findings)
    marked = {n for ns in marks.values() for n in ns}
    new = len(_new_findings(found))
    old = len(found.findings) - new
    head = f'Sashiko: {new} finding{"" if new == 1 else "s"}'
    head += f', {old} pre-existing' if old else ''
    head += f' · {item.sashiko.url}' if item.sashiko.url else ''
    summary = [Block(''), Block(head, style=HEADER)]
    for n, f in enumerate(found.findings):
        where = ', '.join(f'{p}:{line}' for p, line in f.locations[:1]) or '-'
        anchor = '' if n in marked else finding_anchor(post.counter, n + 1)
        style = SEVERITY_STYLES.get(f.severity)
        summary.append(Block(f'{_severity(f)}  {where}  {f.problem}', style=style, anchor=anchor))
    lines = diff.split('\n')
    blocks: list[Block] = []
    start, anchored = 0, set()
    for index in sorted(marks):
        if index > start:
            blocks.append(Block('\n'.join(lines[start:index]), wrap=False, style=diff_style))
        blocks.append(Block(lines[index], wrap=False, style=lambda t: diff_style(t) + MARKED))
        for n in marks[index]:
            f = found.findings[n]
            anchor = '' if n in anchored else finding_anchor(post.counter, n + 1)
            anchored.add(n)
            style = SEVERITY_STYLES.get(f.severity)
            blocks.append(Block(f'▲ {_severity(f)}: {f.problem}', style=style, anchor=anchor))
        start = index + 1
    if start < len(lines):
        blocks.append(Block('\n'.join(lines[start:]), wrap=False, style=diff_style))
    return summary, blocks


def root_blocks(thread: SeriesThread, key: str, item: WorkItem) -> list[Block]:
    if key == UNATTACHED:
        intro = 'These messages could not be linked to a posting by their thread headers.'
        return [Block(intro)]
    post = find_post(thread, key)
    assert post is not None
    blocks = [
        Block(post.message.subject, style=HEADER),
        Block(f'From: {_header(post.message)}'),
    ]
    if not thread.discussion:
        blocks.append(Block(trailer_line(item, post.counter)))
    blocks += [Block(''), Block(post.description, style=prose_style)]
    if post.counter == 0:
        return blocks
    summary, diff = _sashiko_blocks(item, post)
    return blocks + summary + [Block(''), Block('── Diff ──', style=HEADER)] + diff

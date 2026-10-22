# SPDX-License-Identifier: GPL-2.0-or-later
"""Pre-apply reader content: patch list, per-posting comment tree, and message blocks."""

from typing import Collection, Optional

from rich.style import Style

from .model import Message, Post, Reply, SeriesThread, Tag, Trailers, WorkItem
from .widgets import Block

UNATTACHED = 'unattached'

HEADER = Style(bold=True)
QUOTE = Style(dim=True)


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


def post_label(thread: SeriesThread, key: str, item: WorkItem, absent: str = '·') -> str:
    if key == UNATTACHED:
        return f'Unattached ({len(thread.unattached)})'
    post = find_post(thread, key)
    assert post is not None
    total = item.series.expected
    replies = f' ({len(post.replies)})' if post.replies else ''
    if post.counter == 0:
        return f'0/{total} cover{replies}'
    return f'{post.counter}/{total} {_marks(_trailers(item, post.counter), absent)}{replies}'


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


def reply_labels(replies: tuple[Reply, ...], unread: Collection[str] = ()) -> list[str]:
    """Tree labels; unread replies (to the maintainer's mail) are marked ●."""
    labels = []
    for art, reply in zip(tree_art([r.depth for r in replies]), replies):
        m = reply.message
        date = f' {m.date:%m-%d}' if m.date else ''
        marks = _tag_marks(m.tags)
        dot = '● ' if m.msgid in unread else ''
        labels.append(f'{art} {dot}{m.author_name}{date}{" " + marks if marks else ""}')
    return labels


def content_title(thread: SeriesThread, key: str, reply: Optional[Reply], total: int) -> str:
    if reply is not None:
        date = f' · {reply.message.date:%Y-%m-%d %H:%M}' if reply.message.date else ''
        return f'{reply.message.author_name}{date}'
    if key == UNATTACHED:
        return 'Unattached replies'
    post = find_post(thread, key)
    assert post is not None
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


def root_blocks(thread: SeriesThread, key: str, item: WorkItem) -> list[Block]:
    if key == UNATTACHED:
        intro = 'These messages could not be linked to a posting by their thread headers.'
        return [Block(intro)]
    post = find_post(thread, key)
    assert post is not None
    blocks = [
        Block(post.message.subject, style=HEADER),
        Block(f'From: {_header(post.message)}'),
        Block(trailer_line(item, post.counter)),
        Block(''),
        Block(post.description, style=prose_style),
    ]
    if post.counter == 0:
        return blocks
    blocks += [Block(''), Block('── Diff ──', style=HEADER)]
    blocks.append(Block(post.diff or 'No diff.', wrap=False, style=diff_style))
    return blocks

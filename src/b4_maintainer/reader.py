# SPDX-License-Identifier: GPL-2.0-or-later
"""Pre-apply reader content: patch-list labels and per-posting blocks."""

from typing import Literal, Optional

from rich.style import Style

from .model import Message, Post, Reply, SeriesThread, Trailers, WorkItem
from .widgets import Block

ContentMode = Literal['diff', 'comments']
UNATTACHED = 'unattached'

HEADER = Style(bold=True)
QUOTE = Style(dim=True)


def diff_style(line: str) -> Style:
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


def content_title(thread: SeriesThread, key: str, mode: ContentMode) -> str:
    if key == UNATTACHED:
        return f'Unattached ({len(thread.unattached)})'
    post = find_post(thread, key)
    assert post is not None
    if post.counter == 0:
        return f'Cover letter · comments ({len(post.replies)})'
    if mode == 'diff':
        return f'Patch {post.counter} · Diff · [c] comments ({len(post.replies)})'
    return f'Patch {post.counter} · Comments ({len(post.replies)}) · [d] diff'


def _header(message: Message) -> str:
    date = f' · {message.date:%Y-%m-%d %H:%M %z}' if message.date else ''
    return f'{message.author_name} <{message.author_email}>{date}'


def reply_blocks(replies: tuple[Reply, ...]) -> list[Block]:
    if not replies:
        return [Block('No comments.')]
    blocks: list[Block] = []
    for reply in replies:
        indent = reply.depth * 2
        arrow = '└─> ' if reply.depth else ''
        blocks.append(Block(f'{arrow}{_header(reply.message)}', style=HEADER, indent=indent))
        body = reply.message.body.strip('\n')
        blocks.append(Block(body, style=prose_style, indent=indent + 2))
        blocks.append(Block(''))
    return blocks


def post_blocks(thread: SeriesThread, key: str, item: WorkItem, mode: ContentMode) -> list[Block]:
    if key == UNATTACHED:
        intro = 'These messages could not be linked to a posting by their thread headers.'
        return [Block(intro), Block('')] + reply_blocks(thread.unattached)
    post = find_post(thread, key)
    assert post is not None
    blocks = [
        Block(post.message.subject, style=HEADER),
        Block(f'From: {_header(post.message)}'),
        Block(trailer_line(item, post.counter)),
        Block(''),
        Block(post.description, style=prose_style),
        Block(''),
    ]
    if post.counter == 0 or mode == 'comments':
        blocks.append(Block(f'── Comments ({len(post.replies)}) ──', style=HEADER))
        blocks.append(Block(''))
        return blocks + reply_blocks(post.replies)
    blocks.append(Block('── Diff ──', style=HEADER))
    blocks.append(Block(post.diff or 'No diff.', wrap=False, style=diff_style))
    return blocks

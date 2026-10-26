# SPDX-License-Identifier: GPL-2.0-or-later
"""What a series' sessions produced, for a reply: commits from their trees (fixups, tests,
pull-ahead fixes) and files from their output directories (the manifest's artifacts, then
other text files).

Rendered for the end of a reply draft, below where the maintainer writes: text first,
then each patch after a `-- >8 --` scissors line in `git format-patch` form, so
`git am --scissors` applies it. An agent's commit is shown as the maintainer's (their
From:, sign-offs dropped, an Assisted-by trailer), as plucking does. Plain git and files.
"""

import os
import pathlib
import subprocess
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Sequence, Union

from .sessions.artifacts import MANIFEST, load_artifacts
from .sessions.records import SessionRecord
from .sessions.trees import classify_tree

MAX_FILES = 100  # Other files listed per session.
MAX_BYTES = 1_000_000  # Larger files are not offered.
SCISSORS = '-- >8 --'


@dataclass(frozen=True)
class Attachment:
    kind: str  # patch, file, or text
    label: str
    session: str
    repo: str = ''  # patch: the session tree
    sha: str = ''
    path: Optional[pathlib.Path] = None  # file
    title: str = ''
    text: str = ''  # A summary (an artifact's), shown above a file or alone.


def _text_file(path: pathlib.Path) -> bool:
    try:
        if path.stat().st_size > MAX_BYTES:
            return False
        with path.open('rb') as f:
            return b'\0' not in f.read(8192)
    except OSError:
        return False


def _other_files(out: pathlib.Path, skip: set[pathlib.Path]) -> list[pathlib.Path]:
    """Text files under out/ the manifest does not name; checkouts (with .git) skipped."""
    found: list[pathlib.Path] = []
    for top, dirs, files in os.walk(out):
        dirs[:] = sorted(d for d in dirs if not os.path.lexists(os.path.join(top, d, '.git')))
        for name in sorted(files):
            path = pathlib.Path(top) / name
            if path.resolve() in skip or path.name == MANIFEST or not _text_file(path):
                continue
            found.append(path)
            if len(found) >= MAX_FILES:
                return found
    return found


def gather(
    records: Sequence[SessionRecord], series: str, directory: Callable[[str], pathlib.Path]
) -> list[Attachment]:
    """Everything the series' sessions (any revision, newest first) left to attach."""
    found: list[Attachment] = []
    mine = sorted((r for r in records if r.series == series), key=lambda r: r.created, reverse=True)
    for record in mine:
        sid, tree = record.id, record.worktree
        branches: set[str] = set()
        if tree is not None and pathlib.Path(tree.path).is_dir():
            classed = classify_tree(pathlib.Path(tree.path), tree)
            for branch in classed.branches:
                branches.add(branch.name)
                for c in branch.commits:
                    label = f'patch · {branch.kind} · {c.subject}  ({branch.name}, {sid})'
                    found.append(
                        Attachment('patch', label, sid, repo=tree.path, sha=c.sha, title=c.subject)
                    )
        out = (directory(sid) / 'work' / 'out').resolve()
        if not out.is_dir():
            continue
        artifacts, _problems = load_artifacts(out, branches)
        named: set[pathlib.Path] = set()
        for a in artifacts:
            if a.branch:
                continue  # Its commits are listed above.
            if a.path is not None:
                named.add(a.path.resolve())
                rel = (
                    a.path.relative_to(out.parent) if a.path.is_relative_to(out.parent) else a.path
                )
                if _text_file(a.path):
                    label = f'{a.kind}: {a.title}  ({rel}, {sid})'
                    found.append(
                        Attachment('file', label, sid, path=a.path, title=a.title, text=a.summary)
                    )
            else:
                found.append(
                    Attachment(
                        'text', f'{a.kind}: {a.title}  ({sid})', sid, title=a.title, text=a.summary
                    )
                )
        for path in _other_files(out, named):
            rel = path.relative_to(out.parent)
            found.append(
                Attachment('file', f'file: {rel}  ({sid})', sid, path=path, title=str(rel))
            )
    return found


def _cut(text: str, max_lines: int) -> str:
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text.rstrip('\n') + '\n'
    head, tail = lines[: max_lines // 2], lines[-(max_lines // 2) :]
    cut = len(lines) - len(head) - len(tail)
    return '\n'.join([*head, f'… {cut} lines cut …', *tail]) + '\n'


def _git(repo: str, *args: str, stdin: Optional[str] = None) -> str:
    run = subprocess.run(
        ['git', *args], cwd=repo, input=stdin, capture_output=True, text=True, check=False
    )
    if run.returncode != 0:
        raise OSError(run.stderr.strip() or f'git {args[0]} failed')
    return run.stdout


def _patch(a: Attachment, agent_email: str, assisted: str, me: str) -> str:
    """`git format-patch` text for an inline patch: From and Subject headers, the message,
    the diff; no mbox line, date, or format-patch signature."""
    raw = _git(a.repo, 'format-patch', '--stdout', '--no-signature', '-1', a.sha)
    head, _, rest = raw.partition('\n\n')
    unfolded: list[str] = []
    for line in head.splitlines()[1:]:  # Past the mbox "From <sha>" line.
        if line[:1] in (' ', '\t') and unfolded:
            unfolded[-1] += ' ' + line.strip()  # A folded header continues.
        else:
            unfolded.append(line)
    headers = [line for line in unfolded if not line.startswith('Date:')]
    message, sep, diff = rest.partition('\n---\n')
    author = _git(a.repo, 'log', '-1', '--format=%ae', a.sha).strip()
    if author.lower() == agent_email.lower():
        headers = [f'From: {me}' if line.startswith('From:') else line for line in headers]
        kept = '\n'.join(
            line for line in message.splitlines() if not line.lower().startswith('signed-off-by:')
        )
        message = _git(
            a.repo, 'interpret-trailers', '--trailer', f'Assisted-by: {assisted}', stdin=kept + '\n'
        ).rstrip('\n')
    return '\n'.join(headers) + '\n\n' + message + sep + diff


def git_identity(repo: pathlib.Path) -> str:
    """`Name <email>` as git resolves it in *repo* (the maintainer's)."""
    values = []
    for key in ('user.name', 'user.email'):
        run = subprocess.run(['git', 'config', key], cwd=repo, capture_output=True, text=True)
        values.append(run.stdout.strip())
    return f'{values[0]} <{values[1]}>' if all(values) else ''


def render(
    attachments: Sequence[Attachment],
    agent_email: str,
    assisted: Union[str, Mapping[str, str]],
    me: str = '',
    max_lines: int = 200,
) -> str:
    """The attachments as the end of a reply: text blocks, then patches after scissors.
    *assisted*: the Assisted-by value, or one per session."""
    blocks = []
    for a in (x for x in attachments if x.kind != 'patch'):
        lines = [f'--- {a.title}', '']
        if a.text:
            lines += [a.text.strip(), '']
        if a.path is not None:
            try:
                lines.append(_cut(a.path.read_text(errors='replace'), max_lines))
            except OSError as ex:
                lines.append(f'(cannot read {a.path.name}: {ex})')
        blocks.append('\n'.join(lines).rstrip('\n'))
    for a in (x for x in attachments if x.kind == 'patch'):
        value = assisted if isinstance(assisted, str) else assisted.get(a.session, 'LLM')
        blocks.append(f'{SCISSORS}\n{_patch(a, agent_email, value, me).rstrip()}')
    return ('\n\n'.join(blocks) + '\n') if blocks else ''

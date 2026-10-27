# SPDX-License-Identifier: GPL-2.0-or-later
"""The pi command line and files for a session.

pi is the only harness: a session is `pi` in a tmux window (or headless, later) with the
b4-maintainer extension (`extension/b4m.ts`), the session's own pi session directory, and
nothing discovered from the project. The extension learns the session through B4M_*
environment variables. A site's pi build keeps its own extensions; site launcher
options come from the site configuration, never from here.
"""

import mailbox
import pathlib
import shutil
from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

from .policy import ContextItem
from .profiles import Request
from .records import SessionRecord, session_label
from .store import EVENTS, SessionStore

EXTENSION = pathlib.Path(__file__).parent / 'extension' / 'b4m.ts'
# The agent's own directory (its working directory in lightweight sessions): context/ and
# out/, apart from the session's internals (record, events, launch, pi's transcript).
WORK = 'work'
# Every session writes its artifacts under out/; the extension and the sandbox decide
# where writes go (out/ only, or also the worktree for execute).
TOOLS = ('read', 'grep', 'find', 'ls', 'bash', 'edit', 'write')
PERMISSION_TEXT = {
    'read-only': 'analysis: inspect files with the read, grep, find, and ls tools (not shell '
    'pipelines); write files only under the output directory; the shell runs only read-only '
    'git commands',
    'execute': 'build: also edit, build, and run in the worktree',
}


@dataclass(frozen=True)
class LaunchSpec:
    """What a runner needs beyond the record: the request, skills, and preset extras."""

    request: Request
    skills: tuple[pathlib.Path, ...] = ()
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    sandboxed: bool = False  # Shell commands run in `b4-maintainer sandbox`.


@dataclass(frozen=True)
class Site:
    root: pathlib.Path  # The workspace.
    hook: str  # The b4-maintainer executable the extension calls.
    pi: str = 'pi'
    args: tuple[str, ...] = ()  # Site launcher options (site configuration).
    checkouts: Mapping[str, str] = field(default_factory=dict)  # Project id -> checkout.
    nice: int = 10  # Sandboxed commands' CPU priority (0: normal).
    ionice: str = 'low'  # And I/O priority: low, idle, or off.


@dataclass(frozen=True)
class PiCommand:
    argv: list[str]
    cwd: str
    env: dict[str, str]


def pi_command(
    store: SessionStore, record: SessionRecord, spec: LaunchSpec, site: Site, first: bool = True
) -> PiCommand:
    """The command starting the session (*first*) or reopening it (the task is not resent)."""
    directory = store.directory(record.id)
    argv = [
        site.pi,
        '--session-id',
        record.id,
        '--session-dir',
        str(directory / 'pi'),
        '--name',
        session_label(record),
    ]
    if record.model:
        argv += ['--model', record.model]
    if record.thinking:
        argv += ['--thinking', record.thinking]
    argv += ['-e', str(EXTENSION), '--no-skills']
    for skill in spec.skills:
        argv += ['--skill', str(skill)]
    argv += [
        '--append-system-prompt',
        str(directory / 'system.md'),
        '--no-context-files',
        '--no-mcp',
        '--no-approve',
        '--tools',
        ','.join(TOOLS),
        *site.args,
        *spec.args,
    ]
    if first:
        argv += ['--', spec.request.prompt]
    work = directory / WORK
    worktree = record.worktree.path if record.worktree else ''
    readable = [str(work), worktree, site.checkouts.get(record.project, '')]
    readable += [str(s) for s in spec.skills]
    env = {
        'B4M_SESSION': record.id,
        'B4M_ROOT': str(site.root),
        'B4M_HOOK': site.hook,
        'B4M_PERMISSIONS': record.permissions,
        'B4M_EVENTS': str(directory / EVENTS),
        'B4M_OUT': str(work / 'out'),
        'B4M_WORKTREE': worktree,
        'B4M_READABLE': '\n'.join(r for r in readable if r),
        # git must not find a repository around the session (e.g. the workspace's own).
        'GIT_CEILING_DIRECTORIES': str(directory),
        'B4M_SANDBOX': '1' if spec.sandboxed else '',
        'B4M_NICE': str(site.nice),
        'B4M_IONICE': site.ionice,
        **spec.env,
    }
    return PiCommand(argv, worktree or str(work), env)


SANDBOXED_TEXT = (
    "Shell commands run in a sandbox without network that sees only this session's "
    'directories (this one, the project checkout, the worktree), read-only except the '
    'output directory'
)
REBASE_TREE_TEXT = (
    'Rebase in the worktree {path} (branch {branch}: a copy of the base, tip {applied}). '
    "Commits there carry the agent identity as committer; keep each commit's author, "
    'message, and trailers'
)
TREE_TEXT = (
    'Edit, build, and commit in the worktree {path} (branch {branch}: the series applied '
    'on base {base}, its last patch {applied}). Commits there carry the agent identity; '
    'never add Signed-off-by or review trailers'
)


def system_prompt(
    record: SessionRecord,
    request: Request,
    work: pathlib.Path,
    sandboxed: bool = False,
    files: Sequence[tuple[str, str]] = (),
) -> str:
    """*files*: (name, what it holds) in context/, besides the task."""
    permissions = PERMISSION_TEXT.get(record.permissions, record.permissions)
    if sandboxed:
        permissions = record.permissions
    listed = ', '.join(f'{name} ({what})' for name, what in (('task.md', 'the task'), *files))
    facts = [
        f'This session: {session_label(record)}.',
        f'Context in {work / "context"}: {listed}.',
        f'Notes from the maintainer may arrive in {work / "context" / "inbox.md"} while you work.',
        f'Write output files only under {work / "out"}.',
        f'Permissions: {permissions}.',
    ]
    tree = record.worktree
    writable = tree is not None and record.permissions == 'execute'
    if sandboxed:
        facts.append(f'{SANDBOXED_TEXT}{" and the worktree" if writable else ""}.')
    if tree is not None and writable and record.scope == 'rebase':
        facts.append(
            REBASE_TREE_TEXT.format(path=tree.path, branch=tree.branch, applied=tree.applied[:12])
            + '.'
        )
    elif tree is not None and writable:
        facts.append(
            TREE_TEXT.format(
                path=tree.path, branch=tree.branch, base=tree.base[:12], applied=tree.applied[:12]
            )
            + '.'
        )
    elif tree is not None:
        facts.append(f'Worktree with the series applied: {tree.path}.')
    also = record.choices.get('also') or []
    if tree is not None and also:
        facts.append(f'The tree also has, applied after this series in order: {"; ".join(also)}.')
    return '\n\n'.join(p for p in (request.system_prompt, '\n'.join(facts)) if p) + '\n'


def prepare_session(
    store: SessionStore,
    record: SessionRecord,
    request: Request,
    source: Optional[str],
    sandboxed: bool = False,
    items: Optional[Sequence[ContextItem]] = None,
) -> None:
    """system.md, and work/ with context/ (the task and the prompt package's *items*;
    None: the thread mbox from *source*) and out/, for a new session."""
    directory = store.directory(record.id)
    work = directory / WORK
    context = work / 'context'
    context.mkdir(parents=True, exist_ok=True)
    (work / 'out').mkdir(exist_ok=True)
    (context / 'task.md').write_text(request.prompt + '\n')
    if items is None:
        items = [ContextItem('mbox', 'the series thread')] if source else []
    files = []
    for item in items:
        if item.id == 'mbox':
            if source and write_thread(pathlib.Path(source), context / 'thread.mbx'):
                files.append(('thread.mbx', 'the series thread as an mbox'))
        elif item.text:
            (context / f'{item.id}.md').write_text(item.text)
            files.append((f'{item.id}.md', item.label))
    prompt = system_prompt(record, request, work, sandboxed, files)
    (directory / 'system.md').write_text(prompt)


def write_thread(source: pathlib.Path, target: pathlib.Path) -> bool:
    """The thread as one mbox, from an mbox file or a maildir (live mail cache)."""
    if source.is_file():
        shutil.copyfile(source, target)
        return True
    if not (source / 'cur').is_dir():
        return False
    out = mailbox.mbox(target)
    try:
        for message in mailbox.Maildir(source, create=False):
            out.add(message)
        out.flush()
    finally:
        out.close()
    return True

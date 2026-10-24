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
from typing import Mapping, Optional

from .profiles import Request
from .records import SessionRecord, session_label
from .store import EVENTS, SessionStore

EXTENSION = pathlib.Path(__file__).parent / 'extension' / 'b4m.ts'
# The agent's own directory (its working directory in lightweight sessions): context/ and
# out/, apart from the session's internals (record, events, launch, pi's transcript).
WORK = 'work'
# Every session writes its artifacts under out/; the extension decides where writes go.
TOOLS = {
    'read-only': ('read', 'grep', 'find', 'ls', 'bash', 'edit', 'write'),
    'proposals': ('read', 'grep', 'find', 'ls', 'bash', 'edit', 'write'),
    'execute': ('read', 'grep', 'find', 'ls', 'bash', 'edit', 'write'),
}
PERMISSION_TEXT = {
    'read-only': 'read-only: inspect files with the read, grep, find, and ls tools (not shell '
    'pipelines); write files only under the output directory; the shell runs only read-only '
    'git commands',
    'proposals': 'proposals: inspect files with the read, grep, find, and ls tools; also '
    'edit files under the output directory and the worktree; the shell runs only read-only '
    'git commands',
    'execute': 'execute: also run commands; they run in a sandbox without network',
}


@dataclass(frozen=True)
class LaunchSpec:
    """What a runner needs beyond the record: the request, skills, and preset extras."""

    request: Request
    skills: tuple[pathlib.Path, ...] = ()
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Site:
    root: pathlib.Path  # The workspace.
    hook: str  # The b4-maintainer executable the extension calls.
    pi: str = 'pi'
    args: tuple[str, ...] = ()  # Site launcher options (site configuration).
    checkouts: Mapping[str, str] = field(default_factory=dict)  # Project id -> checkout.


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
        ','.join(TOOLS.get(record.permissions, TOOLS['read-only'])),
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
        **spec.env,
    }
    return PiCommand(argv, worktree or str(work), env)


def system_prompt(record: SessionRecord, request: Request, work: pathlib.Path) -> str:
    facts = [
        f'This session: {session_label(record)}.',
        f'Context (the series thread as an mbox, the task): {work / "context"}.',
        f'Write output files only under {work / "out"}.',
        f'Permissions: {PERMISSION_TEXT.get(record.permissions, record.permissions)}.',
    ]
    if record.worktree:
        facts.append(f'Worktree with the series applied: {record.worktree.path}.')
    return '\n\n'.join(p for p in (request.system_prompt, '\n'.join(facts)) if p) + '\n'


def prepare_session(
    store: SessionStore, record: SessionRecord, request: Request, source: Optional[str]
) -> None:
    """system.md, and work/ with context/ (task, thread mbox) and out/, for a new session."""
    directory = store.directory(record.id)
    work = directory / WORK
    context = work / 'context'
    context.mkdir(parents=True, exist_ok=True)
    (work / 'out').mkdir(exist_ok=True)
    (directory / 'system.md').write_text(system_prompt(record, request, work))
    (context / 'task.md').write_text(request.prompt + '\n')
    if source:
        write_thread(pathlib.Path(source), context / 'thread.mbx')


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

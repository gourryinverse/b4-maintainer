# SPDX-License-Identifier: GPL-2.0-or-later
"""b4-maintainer entry point: the fixture prototype and project setup.

The UI, b4, and mail stacks are imported where needed: `hook` runs on every agent tool
call and must start quickly.
"""

import argparse
import faulthandler
import os
import pathlib
import signal
import sys
import tempfile
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional, Sequence

from .adapters.tmux import TmuxEditor
from .model import Authority
from .workspace import Project, Ref, Workspace, list_id, make_ref

if TYPE_CHECKING:
    from .adapters.live import LiveSource
    from .app import MaintainerApp
    from .sessions.hub import SessionHub


def build_app(argv: Optional[Sequence[str]] = None) -> 'MaintainerApp':
    from .adapters.fixture import FixtureSource
    from .adapters.live import LiveSource
    from .app import MaintainerApp
    from .fixtures import build_demo_workspace
    from .projectsetup import CONFIG_NAME, has_config

    parser = argparse.ArgumentParser(
        prog='b4-maintainer',
        description=f'Open a workspace: {CONFIG_NAME} (live) or a fixture directory '
        '(workspace.json, mail/*.mbx, state.json). Subcommands: project add, setup, sync.',
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        'workspace',
        nargs='?',
        type=pathlib.Path,
        help=f'workspace directory (default: .), or a project in ./{CONFIG_NAME} to open on',
    )
    group.add_argument(
        '--demo',
        type=pathlib.Path,
        metavar='DIR',
        help='write a synthetic demo workspace to DIR and open it',
    )
    parser.add_argument(
        '--email-dry-run',
        action='store_true',
        help='replies are logged by b4, not sent (as b4 review --email-dry-run)',
    )
    parser.add_argument(
        '--no-sign', action='store_true', help='do not patatt-sign replies (as b4 review --no-sign)'
    )
    args = parser.parse_args(argv)
    if args.demo:
        root = build_demo_workspace(args.demo)
        return MaintainerApp(
            FixtureSource(root).load, sessions=_sessions(root), app_pane=_app_pane()
        )
    root = args.workspace or pathlib.Path('.')
    if has_config(root):
        return _live_app(LiveSource(root), args)
    if (root / 'workspace.json').is_file():
        return MaintainerApp(
            FixtureSource(root).load, sessions=_sessions(root), app_pane=_app_pane()
        )
    name = str(args.workspace or '')
    is_name = name and '/' not in name and not root.exists()
    if is_name and has_config(pathlib.Path('.')):
        source = LiveSource('.')
        projects = [p.id for p in source.workspace().projects]
        if name in projects:
            app = _live_app(source, args)
            app.current_view = f'project:{name}'
            return app
        parser.error(f'{name}: not a workspace directory or project ({", ".join(projects)})')
    parser.error(f'{root}: no {CONFIG_NAME} (live workspace) or workspace.json (fixture)')


def _live_app(source: 'LiveSource', args: argparse.Namespace) -> 'MaintainerApp':
    from .app import MaintainerApp
    from .branches import BranchService

    source.email_dryrun = args.email_dry_run
    source.no_sign = args.no_sign
    try:
        workspace: Optional['Workspace'] = source.workspace()
        timeout = workspace.sync_timeout if workspace else 10.0
    except (OSError, ValueError):
        workspace, timeout = None, 10.0
    return MaintainerApp(
        source.load,
        actions=source,
        drafts=source.drafts,
        editor=_draft_editor(source),
        sync=source.sync if source.can_sync() else None,
        sync_timeout=timeout,
        sessions=(hub := _sessions(source.root, workspace, private=True)),
        branches=BranchService(
            source.root,
            hub.records,
            hub.store.directory,
            hub,
            email_dryrun=source.email_dryrun,
            no_sign=source.no_sign,
        ),
        app_pane=_app_pane(),
    )


def _app_pane() -> str:
    """The app's own tmux pane, beside which sessions open; empty outside tmux."""
    return os.environ.get('TMUX_PANE', '')


def _sessions(
    root: pathlib.Path, workspace: Optional['Workspace'] = None, private: bool = False
) -> 'SessionHub':
    """Sessions under var/sessions/, run as pi in tmux windows. Live workspaces add the
    maintainer's own profiles and skills from state/ (*private*)."""
    from .sessions.hub import AGENT_EMAIL, AGENT_NAME, RecordWorktrees, SessionHub
    from .sessions.pi import Site
    from .sessions.store import SessionStore
    from .sessions.tmuxrunner import TmuxRunner

    hook = pathlib.Path(sys.executable).parent / 'b4-maintainer'
    projects = workspace.projects if workspace else ()
    site = Site(
        root,
        str(hook) if hook.exists() else 'b4-maintainer',
        workspace.pi if workspace else 'pi',
        workspace.pi_args if workspace else (),
        {p.id: str(root / p.path) for p in projects if p.path},
        workspace.sandbox_nice if workspace else 10,
        workspace.sandbox_ionice if workspace else 'low',
    )
    store = SessionStore(root / 'var' / 'sessions')
    agent = (
        (workspace.agent_name, workspace.agent_email) if workspace else (AGENT_NAME, AGENT_EMAIL)
    )
    return SessionHub(
        store,
        TmuxRunner(store, site),
        RecordWorktrees(root / 'var' / 'worktrees', site.checkouts, agent),
        profiles_dir=root / 'state' / 'profiles' if private else None,
        skills_dir=root / 'state' / 'skills' if private else None,
    )


def _draft_editor(source: 'LiveSource') -> Optional[TmuxEditor]:
    """The tmux editor when configured (or auto) and running inside tmux; else in-app drafts."""
    try:
        ws = source.workspace()
    except (OSError, ValueError):
        return None
    if ws.drafts_editor not in ('auto', 'tmux') or not os.environ.get('TMUX'):
        return None
    return TmuxEditor(width=ws.tmux_width)


def parse_ref(value: str) -> Ref:
    """LABEL:NAME:AUTHORITY, e.g. m:mainline/master:mainline."""
    parts = value.split(':')
    authorities = ', '.join(a.name.lower() for a in Authority)
    if len(parts) != 3 or len(parts[0]) != 1 or not parts[1]:
        raise argparse.ArgumentTypeError(f'{value!r}: expected LABEL:REF:AUTHORITY')
    try:
        authority = Authority[parts[2].upper()]
    except KeyError:
        raise argparse.ArgumentTypeError(
            f'{value!r}: authority must be one of {authorities}'
        ) from None
    return make_ref(parts[0], parts[1], authority)


def project_main(argv: Sequence[str]) -> int:
    from .projectsetup import CONFIG_NAME, SetupError, add_project

    parser = argparse.ArgumentParser(prog='b4-maintainer project')
    commands = parser.add_subparsers(dest='command', required=True)
    add = commands.add_parser(
        'add',
        help='register an existing checkout and enroll it with workspace-local b4 state',
        description=f'Register a checkout in {CONFIG_NAME}, point its b4 data and cache '
        'at var/b4/, and enroll it with b4 review. Never clones, fetches, or pushes.',
    )
    add.add_argument('id', help='project identifier, also the b4 review identifier')
    add.add_argument(
        '-C',
        dest='root',
        type=pathlib.Path,
        default=pathlib.Path('.'),
        help='workspace root (default: current directory)',
    )
    add.add_argument('--path', help='checkout relative to the workspace (default: projects/ID)')
    add.add_argument('--label', help='display label (default: ID)')
    add.add_argument('--short', help='short label (default: first 4 characters of ID)')
    add.add_argument(
        '--list',
        dest='lists',
        action='append',
        default=[],
        metavar='LIST',
        help='mailing list address or list ID; repeatable',
    )
    add.add_argument(
        '--ref',
        dest='refs',
        action='append',
        default=[],
        type=parse_ref,
        metavar='LABEL:REF:AUTHORITY',
        help='tree-evidence ref, e.g. m:mainline/master:mainline; repeatable',
    )
    add.add_argument(
        '--sashiko-url',
        metavar='URL',
        help='Sashiko instance for AI review status, e.g. https://sashiko.dev (b4.sashiko-url)',
    )
    args = parser.parse_args(argv)
    project = Project(
        id=args.id,
        label=args.label or args.id,
        short=args.short or args.id[:4],
        lists=tuple(list_id(lst) for lst in args.lists),
        refs=tuple(args.refs),
        path=args.path or f'projects/{args.id}',
    )
    try:
        add_project(args.root, project, sashiko_url=args.sashiko_url)
    except SetupError as e:
        print(f'b4-maintainer: {e}', file=sys.stderr)
        return 1
    print(f'Registered {project.id} at {project.path} in {CONFIG_NAME}')
    return 0


def setup_main(argv: Sequence[str]) -> int:
    from .projectsetup import SetupError, setup_workspace

    parser = argparse.ArgumentParser(
        prog='b4-maintainer setup',
        description='Recreate this workspace from state/ (a clone of the state repository): '
        'clone missing project checkouts as recorded, enroll them with workspace b4 state, '
        'and import synced b4 state. Existing checkouts are left as they are.',
    )
    parser.add_argument('-C', dest='root', type=pathlib.Path, default=pathlib.Path('.'))
    args = parser.parse_args(argv)
    try:
        report = setup_workspace(args.root, verbose=True)
    except SetupError as e:
        print(f'b4-maintainer: {e}', file=sys.stderr)
        return 1
    print('\n'.join(report))
    return 0


def sync_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog='b4-maintainer sync',
        description='Sync state/ with the state repository once: export, merge, import, push.',
    )
    parser.add_argument('-C', dest='root', type=pathlib.Path, default=pathlib.Path('.'))
    args = parser.parse_args(argv)
    from .adapters.live import LiveSource
    from .adapters.statesync import SyncError

    try:
        print(LiveSource(args.root).sync())
    except (SyncError, OSError, ValueError) as e:
        print(f'b4-maintainer: sync failed: {e}', file=sys.stderr)
        return 1
    return 0


def bases_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog='b4-maintainer bases',
        description="Show each project's maintainer bases: how far behind their upstream, "
        'and which of their commits have landed there; or rebase one onto its upstream.',
    )
    parser.add_argument('-C', dest='root', type=pathlib.Path, default=pathlib.Path('.'))
    parser.add_argument('--rebase', metavar='BRANCH', help='rebase this base onto its upstream')
    args = parser.parse_args(argv)
    import tomllib

    from .adapters.bases import base_status, rebase_base
    from .projectsetup import config_path

    try:
        workspace = Workspace.from_config(tomllib.loads(config_path(args.root).read_text()))
    except (OSError, ValueError) as e:
        print(f'b4-maintainer: {e}', file=sys.stderr)
        return 2
    for warning in workspace.warnings:
        print(f'b4-maintainer: workspace.toml: {warning}', file=sys.stderr)
    found = [(p, b) for p in workspace.projects for b in p.bases]
    if args.rebase:
        match = [(p, b) for p, b in found if b.branch == args.rebase]
        if not match:
            print(f'b4-maintainer: no maintainer base {args.rebase}', file=sys.stderr)
            return 2
        project, base = match[0]
        result = rebase_base(args.root / project.path, base)
        if not result.ok:
            why = f'did not replay: {result.failed.subject}' if result.failed else ''
            print(f'b4-maintainer: {base.branch}: {why or result.error}', file=sys.stderr)
            return 1
        print(f'{base.branch} rebased onto {base.upstream} ({result.tip[:12]})')
        for subject in result.dropped:
            print(f'  dropped: {subject}')
        return 0
    for project, base in found:
        status = base_status(args.root / project.path, base)
        head = f'{project.id} {base.branch} on {base.upstream}'
        if not status.exists:
            print(f'{head}: no such branch')
            continue
        landed = sum(c.landed for c in status.commits)
        print(
            f'{head}: {status.behind} behind, {len(status.commits)} commits ({landed} landed)'
            + (f'; checked out in {status.checked_out}' if status.checked_out else '')
            + (f'; {status.error}' if status.error else '')
        )
        for commit in status.commits:
            print(
                f'  {commit.sha[:12]}  {commit.subject}' + ('  (landed)' if commit.landed else '')
            )
    return 0


def hook_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog='b4-maintainer hook',
        description='Agent CLI hook: record the event on stdin for session ID, and deny '
        'shell commands outside the sandbox wrapper (for the b4-maintainer pi extension).',
    )
    parser.add_argument('-C', dest='root', type=pathlib.Path, default=pathlib.Path('.'))
    parser.add_argument('id', help='session id')
    args = parser.parse_args(argv)
    from .sessions.hook import run_hook
    from .sessions.store import SessionStore

    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    store = SessionStore(args.root / 'var' / 'sessions')
    code, out, err = run_hook(store, args.id, sys.stdin.read(), now)
    if out:
        print(out)
    if err:
        print(err, end='', file=sys.stderr)
    return code


def sandbox_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog='b4-maintainer sandbox',
        description="Run a session's shell command confined to the session (no network, "
        'only its directories); the command comes from B4M_SANDBOX_COMMAND or after --.',
    )
    parser.add_argument('-C', dest='root', type=pathlib.Path, default=pathlib.Path('.'))
    parser.add_argument('id', help='session id')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    from .sessions.sandbox import SandboxError, exec_sandboxed
    from .sessions.store import SessionStore

    words = args.command[1:] if args.command[:1] == ['--'] else args.command
    command = os.environ.get('B4M_SANDBOX_COMMAND') or ' '.join(words)
    if not command:
        print('b4-maintainer sandbox: no command', file=sys.stderr)
        return 2
    try:
        exec_sandboxed(SessionStore(args.root / 'var' / 'sessions'), args.id, command)
    except (SandboxError, ValueError, OSError) as ex:
        print(f'b4-maintainer sandbox: {ex}', file=sys.stderr)
        return 2
    return 0  # Not reached: the command replaced this process.


def enable_stack_dumps(directory: Optional[pathlib.Path] = None) -> pathlib.Path:
    """Let `kill -USR1 <pid>` write every thread's stack to a file, to diagnose hangs."""
    directory = directory or pathlib.Path(tempfile.gettempdir())
    path = directory / f'b4-maintainer-{os.getpid()}.stacks'
    # Held open for the process's life: faulthandler writes to the descriptor from a signal.
    out = path.open('a')
    faulthandler.register(signal.SIGUSR1, file=out, all_threads=True, chain=False)
    return path


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ['project']:
        sys.exit(project_main(args[1:]))
    if args[:1] == ['setup']:
        sys.exit(setup_main(args[1:]))
    if args[:1] == ['sync']:
        sys.exit(sync_main(args[1:]))
    if args[:1] == ['bases']:
        sys.exit(bases_main(args[1:]))
    if args[:1] == ['hook']:
        sys.exit(hook_main(args[1:]))
    if args[:1] == ['sandbox']:
        sys.exit(sandbox_main(args[1:]))
    enable_stack_dumps()
    build_app(args).run()

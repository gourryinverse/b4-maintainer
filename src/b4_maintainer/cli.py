# SPDX-License-Identifier: GPL-2.0-or-later
"""b4-maintainer entry point: the fixture prototype and project setup."""

import argparse
import pathlib
import sys
from typing import Optional, Sequence

from .adapters.fixture import FixtureSource
from .adapters.live import LiveSource
from .app import MaintainerApp
from .fixtures import build_demo_workspace
from .model import Authority
from .projectsetup import CONFIG_NAME, SetupError, add_project
from .workspace import Project, Ref, list_id, make_ref


def build_app(argv: Optional[Sequence[str]] = None) -> MaintainerApp:
    parser = argparse.ArgumentParser(
        prog='b4-maintainer',
        description=f'Open a workspace: {CONFIG_NAME} (live) or a fixture directory '
        '(workspace.json, mail/*.mbx, state.json). Subcommand: project add.',
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
        return MaintainerApp(FixtureSource(build_demo_workspace(args.demo)).load)
    root = args.workspace or pathlib.Path('.')
    if (root / CONFIG_NAME).is_file():
        return _live_app(LiveSource(root), args)
    if (root / 'workspace.json').is_file():
        return MaintainerApp(FixtureSource(root).load)
    name = str(args.workspace or '')
    is_name = name and '/' not in name and not root.exists()
    if is_name and pathlib.Path(CONFIG_NAME).is_file():
        source = LiveSource('.')
        projects = [p.id for p in source.workspace().projects]
        if name in projects:
            app = _live_app(source, args)
            app.current_view = f'project:{name}'
            return app
        parser.error(f'{name}: not a workspace directory or project ({", ".join(projects)})')
    parser.error(f'{root}: no {CONFIG_NAME} (live workspace) or workspace.json (fixture)')


def _live_app(source: LiveSource, args: argparse.Namespace) -> MaintainerApp:
    source.email_dryrun = args.email_dry_run
    source.no_sign = args.no_sign
    return MaintainerApp(source.load, actions=source)


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
        add_project(args.root, project)
    except SetupError as e:
        print(f'b4-maintainer: {e}', file=sys.stderr)
        return 1
    print(f'Registered {project.id} at {project.path} in {CONFIG_NAME}')
    return 0


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ['project']:
        sys.exit(project_main(args[1:]))
    build_app(args).run()

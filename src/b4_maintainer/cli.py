# SPDX-License-Identifier: GPL-2.0-or-later
"""b4-maintainer entry point (fixture prototype)."""

import argparse
import pathlib
from typing import Optional, Sequence

from .adapters.fixture import FixtureSource
from .app import MaintainerApp
from .fixtures import build_demo_workspace


def build_app(argv: Optional[Sequence[str]] = None) -> MaintainerApp:
    parser = argparse.ArgumentParser(
        prog='b4-maintainer',
        description='Browse a fixture workspace: workspace.json, mail/*.mbx, state.json.',
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('workspace', nargs='?', type=pathlib.Path, help='workspace directory')
    group.add_argument(
        '--demo',
        type=pathlib.Path,
        metavar='DIR',
        help='write a synthetic demo workspace to DIR and open it',
    )
    args = parser.parse_args(argv)
    if args.demo:
        root = build_demo_workspace(args.demo)
    else:
        root = args.workspace
        if not (root / 'workspace.json').is_file():
            parser.error(f'{root}: no workspace.json')
    return MaintainerApp(FixtureSource(root).load)


def main(argv: Optional[Sequence[str]] = None) -> None:
    build_app(argv).run()

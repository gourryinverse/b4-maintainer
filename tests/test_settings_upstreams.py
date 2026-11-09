"""Contract: F6 Settings lists each project's tracked upstream branches (label, branch,
authority, view, ends review; local branches marked) with what membership does, and
manages them: Enter on "track a branch" walks remote (or a new one by URL), branch
(listed from the remote), label, authority, view, and ends review, then confirms;
Enter on a branch changes one of those; d untracks it after a confirmation."""

import pytest
from gitutil import make_checkout
from test_remotes import upstream  # noqa: F401 (fixture)
from test_settings import open_settings, settle

from b4_maintainer.adapters import remotes
from b4_maintainer.adapters.fixture import FixtureSource
from b4_maintainer.app import Confirm, MaintainerApp
from b4_maintainer.branchscreen import Ask, Pick
from b4_maintainer.projectsetup import read_config
from b4_maintainer.upstreams import UpstreamService
from b4_maintainer.workspace import Workspace

CONFIG = """\
[[projects]]
id = "linux"
path = "projects/linux"
lists = ["linux-mm@kvack.org"]
refs = [
  { label = "m", name = "mainline/master", authority = "mainline" },
  { label = "g", name = "me/base", authority = "preliminary", view = "Mine" },
]
"""


@pytest.fixture
def root(tmp_path):
    ws = tmp_path / 'ws'
    ws.mkdir()
    (ws / 'workspace.toml').write_text(CONFIG)
    make_checkout(ws / 'projects' / 'linux')
    return ws


def refs(root):
    return Workspace.from_config(read_config(root)).project('linux').refs


def app_for(demo_root, root):
    return MaintainerApp(FixtureSource(demo_root).load, upstreams=UpstreamService(root))


async def answer(pilot, value):
    """Answer the dialog on top: a Pick by key, an Ask by text, a Confirm by y."""
    screen = pilot.app.screen
    if isinstance(screen, Pick):
        assert value in [k for k, _ in screen.choices], (value, screen.choices)
        screen.dismiss(value)
    elif isinstance(screen, Ask):
        screen.query_one('Input').value = value
        await pilot.press('enter')
    else:
        assert isinstance(screen, Confirm), screen
        await pilot.press('y' if value else 'n')
    await settle(pilot)


async def test_settings_lists_tracked_branches_and_what_membership_does(demo_root, root):
    app = app_for(demo_root, root)
    async with app.run_test(size=(160, 50)) as pilot:
        screen = await open_settings(pilot)
        text = screen.upstreams_text()
        assert 'mainline/master' in text and 'mainline · ends review' in text
        assert 'me/base' in text and 'view Mine' in text and 'local branch' in text
        assert 'Track a branch' in text
        assert 'ends review' in screen.query_one('#settings-upstream-help').render().plain.lower()


async def test_track_a_branch_from_a_new_remote(demo_root, root, upstream):  # noqa: F811
    app = app_for(demo_root, root)
    async with app.run_test(size=(160, 50)) as pilot:
        screen = await open_settings(pilot)
        screen.choose('add:linux')
        await settle(pilot)
        await answer(pilot, 'new')  # Remote: a new one,
        await answer(pilot, upstream.as_uri())  # at this URL,
        assert isinstance(app.screen, Ask) and app.screen.query_one('Input').value
        await answer(pilot, 'core')  # named core.
        assert [k for k, _ in app.screen.choices] == ['for-linus', 'for-next', 'master']
        await answer(pilot, 'for-next')
        await answer(pilot, 'c')  # Label.
        await answer(pilot, 'acceptance')
        await answer(pilot, 'named')
        await answer(pilot, 'Core')  # View.
        await answer(pilot, 'no')  # Ends review.
        assert 'core/for-next' in app.screen.prompt
        await answer(pilot, True)
        assert [r.name for r in refs(root)] == ['mainline/master', 'me/base', 'core/for-next']
        core = refs(root)[-1]
        assert (core.label, core.authority.name, core.view, core.ends_review) == (
            'c',
            'ACCEPTANCE',
            'Core',
            False,
        )
        assert remotes.tracked(root / 'projects' / 'linux', 'core') == ['for-next']
        assert 'core/for-next' in screen.upstreams_text()
        assert 'Tracking core/for-next' in screen.status_text()


async def test_change_how_a_branch_counts_and_untrack_it(demo_root, root):
    app = app_for(demo_root, root)
    async with app.run_test(size=(160, 50)) as pilot:
        screen = await open_settings(pilot)
        screen.choose('ref:linux:me/base')
        await settle(pilot)
        await answer(pilot, 'ends')
        await answer(pilot, 'yes')
        assert refs(root)[1].ends_review is True
        screen.choose('ref:linux:me/base')
        await settle(pilot)
        await answer(pilot, 'view')
        await answer(pilot, 'none')
        assert refs(root)[1].view == ''
        screen.select_upstream('ref:linux:me/base')
        await pilot.press('d')
        await settle(pilot)
        assert 'me/base' in app.screen.prompt
        await answer(pilot, True)
        assert [r.name for r in refs(root)] == ['mainline/master']
        assert 'me/base' not in screen.upstreams_text()

"""Contract: the sidebar is divided: "Mailing lists" heads the views of list mail (All,
Discussions, projects, profiles, your queues), "Upstream branches" the views of what
tracked branches hold. Headings are not views: the cursor passes over them."""

import json

from rich.text import Text

from b4_maintainer.adapters.fixture import FixtureSource
from b4_maintainer.app import MaintainerApp
from b4_maintainer.fixtures import build_demo_workspace


async def settle(pilot):
    for _ in range(3):
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()


def with_branch_view(tmp_path):
    root = build_demo_workspace(tmp_path / 'demo')
    path = root / 'workspace.json'
    config = json.loads(path.read_text())
    for ref in config['projects'][0]['refs']:
        if ref['name'] == 'maint/queue':
            ref['view'] = 'Queue'
    path.write_text(json.dumps(config))
    return root


def rows(app):
    views = app._views
    return [
        (
            Text.from_markup(str(views.get_option_at_index(i).prompt)).plain.strip('▌ ').split()[0],
            views.get_option_at_index(i).disabled,
        )
        for i in range(views.option_count)
    ]


async def test_list_views_and_branch_views_have_their_own_headings(tmp_path):
    app = MaintainerApp(FixtureSource(with_branch_view(tmp_path)).load)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        shown = rows(app)
        assert shown[0] == ('MAILING', True)
        assert shown[1][0] == 'All'
        heading = shown.index(('UPSTREAM', True))
        assert [name for name, _ in shown[heading + 1 :]] == ['Queue']
        assert all(not disabled for _, disabled in shown[1:heading])
        assert [v.id for v in app.index.views][-1] == 'ref:linux:maint/queue'


async def test_the_cursor_passes_over_headings(tmp_path):
    app = MaintainerApp(FixtureSource(with_branch_view(tmp_path)).load)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        last_list_view = [v.id for v in app.index.views][-2]
        app.select_view(last_list_view)
        app._views.focus()
        await pilot.press('down')
        await settle(pilot)
        assert app.current_view == 'ref:linux:maint/queue'
        await pilot.press('up')
        await settle(pilot)
        assert app.current_view == last_list_view


async def test_counts_follow_a_filter_with_headings_in_place(tmp_path):
    app = MaintainerApp(FixtureSource(with_branch_view(tmp_path)).load)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        await pilot.press('slash', *'zzzz-no-match', 'enter')
        await settle(pilot)
        shown = rows(app)
        assert shown[0] == ('MAILING', True) and ('UPSTREAM', True) in shown
        prompts = [
            Text.from_markup(str(app._views.get_option_at_index(i).prompt)).plain
            for i in range(app._views.option_count)
        ]
        assert prompts[1].split()[-1] == '0'  # All, filtered to nothing.

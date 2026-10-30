"""Contract: the Workbench's Artifacts are grouped by kind (prompt, finding, analysis, diff,
results, reproducer, file), each group a header with its count, collapsed to start: Enter
or right opens it, left closes it. Rows say what each artifact is, not which session left
it, unless several sessions did. / searches Contents as it does the series list."""

from sessiondata import make_record
from test_workbench_screen import FakeService

from b4_maintainer.adapters.fixture import FixtureSource
from b4_maintainer.app import MaintainerApp
from b4_maintainer.sessions.store import SessionStore
from b4_maintainer.workbench import Workbench, WorkbenchStore
from b4_maintainer.workspace import Workspace


def session(store, sid, files, name):
    record = make_record(sid, scope='workbench', workbench='w')
    store.create(record)
    store.rename(sid, name)
    out = store.directory(sid) / 'work' / 'out'
    context = store.directory(sid) / 'work' / 'context'
    context.mkdir(parents=True, exist_ok=True)
    (context / 'task.md').write_text('Review it.\n')
    for rel in files:
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        (out / rel).write_text(f'{rel}\n')
    return store.record(sid)


def contents(tmp_path, records, sessions):
    service = FakeService(
        tmp_path, WorkbenchStore(tmp_path / 'wb'), lambda: records, sessions.directory
    )
    return service.contents(Workspace.from_config({}), Workbench('w', 'W', ()), {}, {})


def test_artifacts_are_grouped_by_kind_and_say_what_they_are(tmp_path):
    sessions = SessionStore(tmp_path / 'var' / 'sessions')
    one = session(sessions, 's1', ['reports/review.md', 'diffs/fix.diff', 'run.log'], 'review')
    rows = contents(tmp_path, [one], sessions)
    start = next(n for n, r in enumerate(rows) if r.text == 'Artifacts')
    shown = [(r.kind, r.text.strip(), r.group) for r in rows[start + 1 :]]
    assert shown == [
        ('group', 'prompt (1)', 'artifact:prompt'),
        ('artifact', 'the initial prompt', 'artifact:prompt'),
        ('group', 'analysis (1)', 'artifact:analysis'),
        ('artifact', 'out/reports/review.md', 'artifact:analysis'),
        ('group', 'diff (1)', 'artifact:diff'),
        ('artifact', 'out/diffs/fix.diff', 'artifact:diff'),
        ('group', 'results (1)', 'artifact:results'),
        ('artifact', 'out/run.log', 'artifact:results'),
    ]


def test_with_several_sessions_each_row_names_its_own(tmp_path):
    sessions = SessionStore(tmp_path / 'var' / 'sessions')
    one = session(sessions, 's1', ['reports/a.md'], 'review')
    two = session(sessions, 's2', ['reports/b.md'], 'retest')
    rows = contents(tmp_path, [one, two], sessions)
    analysis = [r.text.strip() for r in rows if r.group == 'artifact:analysis']
    assert analysis[0] == 'analysis (2)'
    assert sorted(analysis[1:]) == ['out/reports/a.md · review', 'out/reports/b.md · retest']


def app_with(demo_root, tmp_path):
    sessions = SessionStore(tmp_path / 'var' / 'sessions')
    record = session(sessions, 's1', ['reports/review.md', 'reports/other.md', 'run.log'], 'r')
    store = WorkbenchStore(tmp_path / 'state' / 'workbenches')
    store.save(Workbench('w', 'W', ()))
    service = FakeService(tmp_path, store, lambda: [record], sessions.directory)
    return MaintainerApp(FixtureSource(demo_root).load, workbenches=service)


async def settle(pilot):
    for _ in range(3):
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()


def visible(screen):
    return [r.text.strip() for r in screen.content_rows()]


async def test_groups_start_collapsed_and_open_with_enter_or_right(demo_root, tmp_path):
    app = app_with(demo_root, tmp_path)
    async with app.run_test(size=(200, 50)) as pilot:
        await settle(pilot)
        await pilot.press('f3')
        await settle(pilot)
        screen = app.screen
        rows = visible(screen)
        assert '▸ analysis (2)' in rows and 'out/reports/review.md' not in rows
        screen.select_row('group')  # The first: prompt.
        await pilot.press('down', 'enter')  # analysis
        await settle(pilot)
        rows = visible(screen)
        assert '▾ analysis (2)' in rows and 'out/reports/review.md' in rows
        await pilot.press('left')  # Closes it again.
        await settle(pilot)
        assert 'out/reports/review.md' not in visible(screen)
        await pilot.press('right')
        await settle(pilot)
        assert 'out/reports/review.md' in visible(screen)


async def test_slash_searches_contents_and_escape_clears_it(demo_root, tmp_path):
    app = app_with(demo_root, tmp_path)
    async with app.run_test(size=(200, 50)) as pilot:
        await settle(pilot)
        await pilot.press('f3')
        await settle(pilot)
        screen = app.screen
        screen.select_row('group')
        await pilot.press('slash', *'review', 'enter')
        await settle(pilot)
        rows = visible(screen)
        assert 'out/reports/review.md' in rows  # Found inside a collapsed group,
        assert '▾ analysis (2)' in rows and 'out/reports/other.md' not in rows  # alone.
        assert 'out/run.log' not in rows
        await pilot.press('escape')  # Clears the search.
        await settle(pilot)
        rows = visible(screen)
        assert '▸ analysis (2)' in rows and '▸ results (1)' in rows

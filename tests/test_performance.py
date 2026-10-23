"""Interface performance targets with 10,000 series (docs/design/interface.md).

Pilot waits for the app to settle after every input, so raw wall time includes
harness idle detection (~50 ms for a bare app). Latency is checked two ways:
CPU per action (app work) and wall time above a bare-app floor measured in the
same run (catches blocking waits that use no CPU).
"""

import gc
import statistics
import time

from textual.app import App
from textual.widgets import Static

from b4_maintainer.app import MaintainerApp, SeriesList
from b4_maintainer.fixtures import synthetic_snapshot

SERIES = 10_000
TARGET = 0.100


def p95(samples):
    return statistics.quantiles(samples, n=20)[-1]


class Floor(App):
    BINDINGS = [('down', 'noop'), ('pagedown', 'noop')]

    def compose(self):
        yield Static('floor')

    def action_noop(self):
        pass


async def sample(steps):
    wall, cpu = [], []
    for step in steps:
        w, c = time.perf_counter(), time.process_time()
        await step()
        wall.append(time.perf_counter() - w)
        cpu.append(time.process_time() - c)
    return wall, cpu


def keys(pilot):
    return [lambda k=k: pilot.press(k) for k in ['down'] * 40 + ['pagedown'] * 10]


async def floor_samples():
    app = Floor()
    async with app.run_test(size=(180, 50)) as pilot:
        await pilot.pause()
        press_wall, _ = await sample(keys(pilot))
        pause_wall, _ = await sample([pilot.pause] * 30)
    return p95(press_wall), p95(pause_wall)


async def test_ten_thousand_series_meet_interface_targets():
    gc.collect()  # Earlier tests' garbage is not this app's cost.
    press_floor, pause_floor = await floor_samples()
    snapshot = synthetic_snapshot(SERIES)
    app = MaintainerApp(lambda: snapshot)
    start = time.perf_counter()
    async with app.run_test(size=(180, 50)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert time.perf_counter() - start <= 1.0, 'cached useful screen'
        series_list = app.query_one(SeriesList)
        assert series_list.row_count == app.index.count('all') >= SERIES // 2

        wall, cpu = await sample(keys(pilot))
        assert p95(cpu) <= TARGET, f'selection cpu p95 {p95(cpu):.3f}s'
        assert p95(wall) - press_floor <= TARGET, f'selection wall p95 {p95(wall):.3f}s'

        async def switch(view_id):
            app.select_view(view_id)
            await pilot.pause()

        views = [v.id for v in app.index.views] * 3
        wall, cpu = await sample([lambda v=v: switch(v) for v in views])
        assert p95(cpu) <= TARGET, f'view switch cpu p95 {p95(cpu):.3f}s'
        assert p95(wall) - pause_floor <= TARGET, f'view switch wall p95 {p95(wall):.3f}s'

        await pilot.press('p')
        await pilot.pause()
        wall, cpu = await sample(keys(pilot))
        assert p95(cpu) <= TARGET, f'preview selection cpu p95 {p95(cpu):.3f}s'
        assert p95(wall) - press_floor <= TARGET, f'preview selection wall p95 {p95(wall):.3f}s'

        await pilot.press('p', 'slash')
        await pilot.pause()

        async def type_key(key):
            await pilot.press(key)

        # About as many samples as the selection checks, so one GC pause is not the p95.
        query = 'synthetic change 1'
        keystrokes = [*query, *['backspace'] * len(query), *query, *['backspace'] * 6]
        typed = [lambda k=k: type_key(k) for k in keystrokes]
        wall, cpu = await sample(typed)
        assert p95(cpu) <= TARGET, f'search keystroke cpu p95 {p95(cpu):.3f}s'
        assert p95(wall) - press_floor <= TARGET, f'search keystroke wall p95 {p95(wall):.3f}s'


def deep_thread(item, replies=160, depth=21):
    """One posting with a long, deep conversation, like a busy mailing-list discussion."""
    from b4_maintainer.model import Message, Post, Reply, SeriesThread

    rows = []
    for n in range(replies):
        msg = Message(
            f'r{n}@example.org',
            f'Reviewer {n % 9}',
            f'r{n % 9}@example.org',
            None,
            'Re: subject',
            'A comment.\n' * 20,
        )
        rows.append(Reply(msg, n % depth))
    root = Message('p@example.org', 'Author', 'a@example.org', None, 'subject', 'Body.')
    post = Post(1, root, 'Body.', 'diff --git a/x b/x', tuple(rows))
    return SeriesThread(item.identity, item.series.revision, None, (post,))


async def test_reader_moves_and_folds_in_a_deep_thread_meet_targets():
    from unittest import mock

    from b4_maintainer.fixtures import make_item, workspace_config
    from b4_maintainer.workspace import Snapshot, Workspace

    gc.collect()
    item = make_item(subject='mm: busy discussion')
    snapshot = Snapshot(Workspace.from_config(workspace_config()), (item,))
    app = MaintainerApp(lambda: snapshot, thread_loader=lambda s: deep_thread(item))
    async with app.run_test(size=(180, 50)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press('enter')
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press('equals_sign', 'down')
        await pilot.pause()
        with mock.patch.object(app, 'refresh_bindings', wraps=app.refresh_bindings) as refresh:
            wall, cpu = await sample([lambda: pilot.press('down')] * 40)
            assert refresh.call_count == 0, 'moving between replies refreshed every key hint'
        assert p95(cpu) <= TARGET, f'reply move cpu p95 {p95(cpu):.3f}s'
        wall, cpu = await sample([lambda k=k: pilot.press(k) for k in ['left', 'right'] * 10])
        assert p95(cpu) <= TARGET, f'fold cpu p95 {p95(cpu):.3f}s'


async def test_drawer_polls_twenty_sessions_with_steady_events_within_targets(tmp_path):
    from sessiondata import FakeSessionRunner, FakeWorktrees, make_record

    from b4_maintainer.sessions.hub import SessionHub
    from b4_maintainer.sessions.store import SessionStore

    store = SessionStore(tmp_path / 'var' / 'sessions')
    runner = FakeSessionRunner(store)
    for n in range(20):
        runner.add(make_record(f's{n:02}', subject=f'mm: synthetic change {n}'))
    snapshot = synthetic_snapshot(SERIES)
    hub = SessionHub(store, runner, FakeWorktrees())
    app = MaintainerApp(lambda: snapshot, sessions=hub)
    events = ['UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'Stop']

    async def tick(n=0):
        for k in range(5):  # A quarter of the sessions report each second.
            runner.emit(f's{(n * 5 + k) % 20:02}', events[n % 4], tool_name='Read')
        app.poll_sessions()
        await app.workers.wait_for_complete()
        await pilot.pause()

    async with app.run_test(size=(180, 50)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.press('grave_accent')
        await tick()
        _, cpu = await sample([lambda n=n: tick(n) for n in range(40)])
        assert len(app.drawer_rows()) == 20
        assert p95(cpu) <= TARGET, f'session poll cpu p95 {p95(cpu):.3f}s'

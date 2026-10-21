"""Interface performance targets with 10,000 series (docs/design/interface.md).

Pilot waits for the app to settle after every input, so raw wall time includes
harness idle detection (~50 ms for a bare app). Latency is checked two ways:
CPU per action (app work) and wall time above a bare-app floor measured in the
same run (catches blocking waits that use no CPU).
"""

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

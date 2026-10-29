"""Contract: sessions have names. var/sessions/names.toml maps each session's id (its
timestamp directory, and its tree's under var/worktrees/) to its name; records read
through the store carry it. A new session gets a default name from what it is pinned
to, unique among sessions; sessions without one get theirs on the next poll. A rename
must be unique and short; removing a session drops its name."""

import tomllib

import pytest
from sessiondata import make_record

from b4_maintainer.sessions.records import session_name
from b4_maintainer.sessions.store import SessionStore


@pytest.fixture
def store(tmp_path):
    return SessionStore(tmp_path / 'var' / 'sessions')


def names_file(store):
    return tomllib.loads((store.root / 'names.toml').read_text())


def test_a_new_session_is_named_after_what_it_is_pinned_to(store):
    store.create(make_record('20261024-010101-aaaa'))
    store.create(make_record('20261024-020202-bbbb'))  # The same series again.
    store.create(make_record('20261024-030303-cccc', scope='workbench', subject='CXL + ndctl'))
    store.create(make_record('20261024-040404-dddd', scope='rebase', subject='rebase me/base'))
    assert names_file(store) == {
        '20261024-010101-aaaa': 'mm-fix-foo-leak-v3',
        '20261024-020202-bbbb': 'mm-fix-foo-leak-v3-2',
        '20261024-030303-cccc': 'cxl-ndctl',
        '20261024-040404-dddd': 'rebase-me-base',
    }
    record = store.record('20261024-020202-bbbb')
    assert record.name == 'mm-fix-foo-leak-v3-2' and session_name(record) == record.name
    assert '"name"' not in (store.root / '20261024-020202-bbbb' / 'session.json').read_text()
    assert record == make_record('20261024-020202-bbbb')  # The name is not the record's.


def test_sessions_from_before_get_default_names_on_the_next_poll(store):
    make_record('old1').save(store.root / 'old1' / 'session.json')
    make_record('old2', scope='workspace', series='').save(store.root / 'old2' / 'session.json')
    views = store.poll(lambda records: set())
    assert {v.record.id: v.record.name for v in views} == {
        'old1': 'mm-fix-foo-leak-v3',
        'old2': 'workspace',
    }
    assert names_file(store) == {'old1': 'mm-fix-foo-leak-v3', 'old2': 'workspace'}


def test_a_rename_is_unique_short_and_kept(store):
    store.create(make_record('a'))
    store.create(make_record('b'))
    assert store.rename('a', '  leak review  ') == 'leak review'
    assert store.record('a').name == 'leak review'
    polled = {v.record.id: v.record.name for v in store.poll(lambda r: set())}
    assert polled['a'] == 'leak review'
    for bad in ('', '   ', 'x' * 41, 'two\nlines'):
        with pytest.raises(ValueError):
            store.rename('a', bad)
    with pytest.raises(ValueError, match='already'):
        store.rename('b', 'Leak Review')  # Another's, in any case.
    store.rename('a', 'Leak Review')  # Its own, recased.
    store.delete('b')
    assert names_file(store) == {'a': 'Leak Review'}


def test_without_a_name_a_session_is_shown_by_its_id():
    assert session_name(make_record('20261024-010101-aaaa')) == '20261024-010101-aaaa'


def test_the_hub_renames_and_retitles_the_sessions_window(store):
    from sessiondata import FakeSessionRunner, FakeWorktrees

    from b4_maintainer.sessions.hub import SessionHub

    runner = FakeSessionRunner(store)
    runner.add(make_record('a'))
    hub = SessionHub(store, runner, FakeWorktrees())
    assert hub.rename('a', 'leak review').name == 'leak review'
    assert runner.retitled == [('a', 'leak review')]


async def test_n_in_the_drawer_renames_the_selected_session(demo_root, store):
    from sessiondata import FakeSessionRunner, FakeWorktrees

    from b4_maintainer.adapters.fixture import FixtureSource
    from b4_maintainer.app import MaintainerApp
    from b4_maintainer.branchscreen import Ask
    from b4_maintainer.sessions.hub import SessionHub

    runner = FakeSessionRunner(store)
    runner.add(make_record('a'))
    hub = SessionHub(store, runner, FakeWorktrees())
    app = MaintainerApp(FixtureSource(demo_root).load, sessions=hub)
    async with app.run_test(size=(180, 40)) as pilot:
        for _ in range(2):
            await app.workers.wait_for_complete()
            await pilot.pause()
        await pilot.press('grave_accent')
        await pilot.pause()
        assert app.drawer_rows()[0].split(' ', 1)[1].startswith('mm-fix-foo-leak-v3 · ')
        await pilot.press('n')
        await pilot.pause()
        assert isinstance(app.screen, Ask)
        assert app.screen.query_one('Input').value == 'mm-fix-foo-leak-v3'  # To edit.
        app.screen.query_one('Input').value = 'leak review'
        await pilot.press('enter')
        for _ in range(3):
            await app.workers.wait_for_complete()
            await pilot.pause()
        assert store.names()['a'] == 'leak review'
        assert 'Renamed to leak review' in app.status_text()
        assert 'leak review · ' in app.drawer_rows()[0]

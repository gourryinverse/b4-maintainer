"""Contract: which session belongs in the column beside the app."""

from sessiondata import make_record

from b4_maintainer.sessions.pane import pane_for
from b4_maintainer.sessions.store import SessionView

FOO = 'change-id:20261020-mm-foo'
BAR = 'change-id:20261020-mm-bar'


def view(sid, state, series=FOO, scope='series'):
    record = make_record(sid, series=series if scope == 'series' else '', scope=scope)
    return SessionView(record, state)


# Views come most urgent first, as the store lists them.
VIEWS = [
    view('a', 'approval'),
    view('w', 'waiting', scope='workspace'),
    view('b', 'working'),
    view('c', 'waiting', series=BAR),
    view('s', 'stopped', series=BAR),
]


def test_reading_a_series_shows_its_most_urgent_live_session():
    assert pane_for(VIEWS, reading=FOO, workspace=None) == 'a'
    assert pane_for(VIEWS, reading=BAR, workspace=None) == 'c'


def test_stopped_or_gone_sessions_never_join_on_their_own():
    views = [view('s', 'stopped', series=BAR), view('g', 'gone', series=BAR)]
    assert pane_for(views, reading=BAR, workspace=None) is None


def test_a_series_without_sessions_keeps_the_workspace_session():
    assert pane_for(VIEWS, reading='change-id:other', workspace='w') == 'w'
    assert pane_for(VIEWS, reading=FOO, workspace='w') == 'a'  # The series' own wins.


def test_the_series_list_shows_the_workspace_session_while_it_runs():
    assert pane_for(VIEWS, reading=None, workspace='w') == 'w'
    assert pane_for(VIEWS, reading=None, workspace=None) is None
    ended = [view('w', 'ended', scope='workspace')]
    assert pane_for(ended, reading=None, workspace='w') is None

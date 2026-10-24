"""Contract: the prompt package offers each context source with its availability, and a
session receives exactly the sources the maintainer leaves ticked."""

import dataclasses

from sessiondata import make_record

from b4_maintainer.adapters.fixture import FixtureSource
from b4_maintainer.model import Finding, SashikoPatch, SashikoReview
from b4_maintainer.package import series_context
from b4_maintainer.sessions.pi import prepare_session
from b4_maintainer.sessions.policy import ContextItem
from b4_maintainer.sessions.profiles import Request
from b4_maintainer.sessions.store import SessionStore

REQUEST = Request('Review the series.', 'You assist a maintainer.', (), 'lightweight', ())
LEAK = Finding('high', 'Leaks the folio on error.', 'Memory grows.', False, (('mm/a.c', 10),))
OLD = Finding('low', 'Old typo.', '', True, ())
REVIEW = SashikoReview(
    'Reviewed',
    high=1,
    url='https://sashiko.example.org/#/patchset/7',
    baseline='v6.18',
    patches=(
        SashikoPatch('p1@example.org', 1, 'Reviewed', (LEAK, OLD), '> +\tleak();\n\nThis leaks.\n'),
        SashikoPatch('p2@example.org', 2, 'Reviewed', (), ''),
    ),
)


def item(demo_root, **changes):
    found = FixtureSource(demo_root).load().items[0]
    return dataclasses.replace(found, **changes)


def by_id(items):
    return {i.id: i for i in items}


def test_the_mailbox_and_sashikos_review_are_offered(demo_root):
    items = by_id(series_context(item(demo_root, sashiko=REVIEW)))
    assert list(items) == ['mbox', 'sashiko']
    assert items['mbox'].available and items['sashiko'].available
    text = items['sashiko'].text
    assert 'https://sashiko.example.org/#/patchset/7' in text and 'v6.18' in text
    assert '## Patch 1 (p1@example.org)' in text and '## Patch 2 (p2@example.org)' in text
    assert '[high] Leaks the folio on error. (mm/a.c:10)' in text and 'Memory grows.' in text
    assert '[low, pre-existing] Old typo.' in text
    assert 'This leaks.' in text and 'No findings.' in text


def test_unavailable_sources_say_why(demo_root, tmp_path):
    found = item(demo_root)
    gone = dataclasses.replace(found.series, source=str(tmp_path / 'missing'))
    items = by_id(series_context(dataclasses.replace(found, series=gone, sashiko=None)))
    assert not items['mbox'].available and 'not cached' in items['mbox'].reason
    assert not items['sashiko'].available and 'not reviewed' in items['sashiko'].reason
    pending = SashikoReview('Pending')
    items = by_id(series_context(dataclasses.replace(found, sashiko=pending)))
    assert not items['sashiko'].available and 'Pending' in items['sashiko'].reason


def test_the_session_receives_exactly_the_chosen_sources(tmp_path):
    store = SessionStore(tmp_path / 'var' / 'sessions')
    mbox = tmp_path / 'thread.mbx'
    mbox.write_text('From x\nSubject: s\n\nbody\n')
    sashiko = ContextItem('sashiko', 'Sashiko review', text='# Sashiko review\n')
    prepare_session(store, make_record('a'), REQUEST, str(mbox), items=[sashiko])
    context = store.directory('a') / 'work' / 'context'
    assert sorted(p.name for p in context.iterdir()) == ['sashiko.md', 'task.md']
    system = (store.directory('a') / 'system.md').read_text()
    assert 'sashiko.md' in system and 'thread.mbx' not in system
    both = [ContextItem('mbox', 'Series mbox and thread'), sashiko]
    prepare_session(store, make_record('b'), REQUEST, str(mbox), items=both)
    context = store.directory('b') / 'work' / 'context'
    assert sorted(p.name for p in context.iterdir()) == ['sashiko.md', 'task.md', 'thread.mbx']
    system = (store.directory('b') / 'system.md').read_text()
    assert 'thread.mbx' in system and 'sashiko.md' in system

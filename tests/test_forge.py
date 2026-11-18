"""Contract: a forge is a workbench with one project, a goal, and a local list. Each round
is what b4 would send, kept as posted in its own maildir; replies to it are written there,
and both read back through the reader's own thread loader."""

import email
import email.policy

import pytest
from forgedata import posted_rounds

from b4_maintainer.forge import ForgeError, ForgeService, ForgeStore, Round
from b4_maintainer.workbench import WorkbenchStore

ME = ('Example Maintainer', 'maintainer@example.org')


@pytest.fixture(scope='module')
def posted(tmp_path_factory):
    return posted_rounds(tmp_path_factory)


@pytest.fixture
def service(tmp_path):
    state = tmp_path / 'state'
    return ForgeService(
        ForgeStore(state / 'forges'), WorkbenchStore(state / 'workbenches'), tmp_path / 'var'
    )


def msgid(path):
    msg = email.message_from_bytes(path.read_bytes(), policy=email.policy.default)
    return msg['Message-Id'].strip().strip('<>')


def test_no_forges_reads_nothing_and_writes_nothing(service, tmp_path):
    forges, problems = service.store.list()
    assert forges == [] and problems == []
    assert not (tmp_path / 'state').exists() and not (tmp_path / 'var').exists()


def test_a_new_forge_owns_a_workbench_and_keeps_its_prompt(service):
    forge = service.create('Demo work', 'linux', 'mainline/master', 'Add two files.\n')
    assert (forge.id, forge.label, forge.project, forge.base) == (
        'demo-work',
        'Demo work',
        'linux',
        'mainline/master',
    )
    assert forge.mode == 'iterate'
    assert service.store.prompt(forge.id) == 'Add two files.\n'
    bench = service.benches.get(forge.workbench)
    assert bench is not None and bench.origin == f'forge:{forge.id}'
    assert [(e.project, e.base, e.items) for e in bench.entries] == [
        ('linux', 'mainline/master', ())
    ]
    forges, _problems = service.store.list()
    assert forges == [forge]
    assert service.rounds(forge) == []  # Nothing posted, nothing started.


def test_modes_are_iterate_or_aggressive(service):
    assert service.create('A', 'linux', 'm', '', mode='aggressive').mode == 'aggressive'
    with pytest.raises(ForgeError):
        service.create('B', 'linux', 'm', '', mode='reckless')


def test_a_round_is_posted_as_b4_wrote_it_and_reads_as_a_thread(service, posted):
    forge = service.create('Demo', 'linux', 'main', '')
    first = service.post_round(forge, posted[0])
    assert first == Round(1, 1)
    assert first.label == 'v1 r1'
    assert [r.key for r in service.rounds(forge)] == ['v1-r1']
    series, thread = service.thread(forge, first)
    assert series.subject == 'demo: two changes'
    assert [p.message.subject for p in thread.patches] == [
        '[PATCH 1/2] demo: add f1',
        '[PATCH 2/2] demo: add f2',
    ]
    assert thread.cover is not None and 'A cover letter.' in thread.cover.message.body
    # Kept as posted: the same Message-IDs b4 made.
    assert {p.message.msgid for p in thread.patches} == {msgid(m) for m in posted[0][1:]}


def test_an_inline_reply_lands_under_its_patch(service, posted):
    forge = service.create('Demo', 'linux', 'main', '')
    first = service.post_round(forge, posted[0])
    _series, thread = service.thread(forge, first)
    patch = thread.patches[0].message
    body = f'> {patch.body.splitlines()[0]}\n\nPlease explain why.\n'
    sent = service.post_reply(forge, first, patch.msgid, ME, body)
    _series, thread = service.thread(forge, first)
    (reply,) = thread.patches[0].replies
    assert reply.message.msgid == sent
    assert (reply.message.author_name, reply.message.author_email) == ME
    assert reply.message.subject == 'Re: [PATCH 1/2] demo: add f1'
    assert 'Please explain why.' in reply.message.body
    assert thread.patches[1].replies == ()
    # A reply to a reply threads under it.
    again = service.post_reply(forge, first, sent, ME, '> Please explain why.\n\nStill?\n')
    _series, thread = service.thread(forge, first)
    assert [(r.message.msgid, r.depth) for r in thread.patches[0].replies] == [
        (sent, 0),
        (again, 1),
    ]


def test_replying_to_a_message_not_in_the_round_is_refused(service, posted):
    forge = service.create('Demo', 'linux', 'main', '')
    first = service.post_round(forge, posted[0])
    with pytest.raises(ForgeError):
        service.post_reply(forge, first, 'nobody@example.org', ME, 'Hello\n')


def test_the_next_round_is_its_own_thread(service, posted):
    forge = service.create('Demo', 'linux', 'main', '')
    first = service.post_round(forge, posted[0])
    _series, thread = service.thread(forge, first)
    service.post_reply(forge, first, thread.patches[0].message.msgid, ME, 'Why?\n')
    second = service.post_round(forge, posted[1])
    assert second == Round(1, 2)
    assert [r.label for r in service.rounds(forge)] == ['v1 r1', 'v1 r2']
    _series, thread = service.thread(forge, second)
    assert thread.patches[0].replies == ()  # The first round keeps its review.
    _series, thread = service.thread(forge, first)
    assert len(thread.patches[0].replies) == 1


def test_a_round_needs_a_posting(service, tmp_path):
    forge = service.create('Demo', 'linux', 'main', '')
    with pytest.raises(ForgeError):
        service.post_round(forge, [])
    assert service.rounds(forge) == []


def test_forges_are_kept_apart(service, posted):
    one = service.create('Demo', 'linux', 'main', '')
    two = service.create('Demo', 'linux', 'main', 'Other.\n')
    assert two.id == 'demo-2' and two.workbench != one.workbench
    service.post_round(one, posted[0])
    assert service.rounds(two) == []

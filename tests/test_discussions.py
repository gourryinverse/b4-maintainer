"""Contract: conversations without patches become discussions, and responses reach them."""

import datetime
import email.message
import email.utils

import pytest
from test_live import MM, NOW, mail, reply_to, responses_source

from b4_maintainer.adapters.b4mbox import load_thread
from b4_maintainer.reader import root_blocks, tree_rows
from b4_maintainer.rows import render_row
from b4_maintainer.views import ViewIndex

ME = 'Me <me@example.org>'
DAY = datetime.timedelta(days=1)


def message(msgid, sender, subject, days_ago, parent=None, cc=(), to='linux-mm@kvack.org'):
    msg = email.message.EmailMessage()
    msg['From'] = sender
    msg['To'] = to
    if cc:
        msg['Cc'] = ', '.join(cc)
    msg['Subject'] = subject if parent is None else f'Re: {subject}'
    msg['Date'] = email.utils.format_datetime(NOW - days_ago * DAY)
    msg['Message-ID'] = f'<{msgid}>'
    msg['List-Id'] = '<linux-mm.kvack.org>'
    if parent is not None:
        msg['In-Reply-To'] = f'<{parent}>'
        msg['References'] = f'<root@example.org> <{parent}>'
    msg.set_content(f'Text of {msgid}.\n')
    return msg


SUBJECT = 'Path forward for synthetic swap?'


def discussion():
    """A long conversation: my posts are old; the recent replies Cc me but answer others."""
    starter = 'Starter <starter@example.org>'
    other = 'Other <other@example.org>'
    return [
        message('root@example.org', starter, SUBJECT, 30, cc=['me@example.org']),
        message('early@example.org', other, SUBJECT, 29, 'root@example.org'),
        message('mine@example.org', ME, SUBJECT, 25, 'root@example.org'),
        message('answer@example.org', other, SUBJECT, 24, 'mine@example.org'),
        message('late1@example.org', starter, SUBJECT, 2, 'early@example.org', ['me@example.org']),
        message('late2@example.org', other, SUBJECT, 1, 'late1@example.org', ['me@example.org']),
        message('aside@example.org', other, SUBJECT, 1, 'late1@example.org'),  # Not to me.
    ]


def snap(source):
    snapshot = source.load()
    return snapshot, ViewIndex(snapshot.workspace, snapshot.items)


def test_threads_without_patches_become_discussions(tmp_path):
    source, fakes, _ = responses_source(tmp_path)
    fakes.mine = discussion()
    source.refresh()
    snapshot, index = snap(source)
    [item] = [i for i in snapshot.items if i.series.discussion]
    assert item.series.subject == SUBJECT
    assert item.series.author_name == 'Starter'
    assert item.series.patches == () and item.series.has_cover
    assert item.project == 'demo'  # Routed by its list, so replies work.
    assert [i.identity for i in index.items('discussions')] == [item.identity]
    assert item in index.items('cached')
    for view in ('all', 'profile:mm', 'project:demo', 'review', 'responsibilities'):
        assert item not in index.items(view), view


def test_discussion_responses_are_messages_to_me_after_my_first_post(tmp_path):
    source, fakes, store = responses_source(tmp_path)
    fakes.mine = discussion()
    source.refresh()
    _, index = snap(source)
    [item] = index.items('responses')
    # early (before my post) and aside (not addressed to me) do not count.
    assert item.unread_msgids == {'answer@example.org', 'late1@example.org', 'late2@example.org'}
    store.flags |= {'late1@example.org', 'late2@example.org', 'answer@example.org'}
    assert snap(source)[1].count('responses') == 0


def test_patch_responses_reach_below_my_messages(tmp_path):
    source, fakes, _ = responses_source(tmp_path)
    _, theirs = mail(tmp_path, 'theirs', 'mm: their series')
    patch = next(m for m in theirs if '1/2' in str(m['Subject']))
    my_review = reply_to(patch, ME, 'my-review@example.org')
    answer = reply_to(my_review, 'Contributor <contributor1@example.org>', 'answer@example.org')
    deeper = reply_to(answer, 'Bystander <by@example.org>', 'deeper@example.org')
    aside = reply_to(patch, 'Bystander <by@example.org>', 'aside@example.org')
    fakes.answers[MM] = theirs + [my_review, answer, deeper, aside]
    source.refresh()
    [item] = snap(source)[1].items('responses')
    assert item.unread_msgids == {'answer@example.org', 'deeper@example.org'}


def test_discussion_rows_and_reader(tmp_path):
    source, fakes, _ = responses_source(tmp_path)
    fakes.mine = discussion()
    source.refresh()
    [item] = [i for i in source.load().items if i.series.discussion]
    row = render_row(item)
    assert ' disc ' in row and SUBJECT in row and ' v0 ' not in row

    thread = load_thread(item.series)
    assert thread.cover is not None and thread.patches == ()
    assert thread.cover.message.msgid == 'root@example.org'
    assert [r.message.msgid for r in thread.cover.replies][:2] == [
        'early@example.org',
        'late1@example.org',
    ]
    labels = [r.label for r in tree_rows(thread, item, {'0'})]
    assert labels[0].startswith('▾ discussion (6)')
    text = '\n'.join(b.text for b in root_blocks(thread, '0', item))
    assert 'Text of root@example.org.' in text and '── Diff ──' not in text


def watching_source(tmp_path):
    source, fakes, store = responses_source(tmp_path)
    fakes.thread_calls = []

    def thread_for(checkout):
        def fetch(msgid):
            fakes.thread_calls.append(msgid)
            return list(fakes.mine)

        return fetch

    source._thread_for = thread_for
    return source, fakes, store


def test_dollar_watches_a_discussion_into_the_watching_view(tmp_path):
    source, fakes, _ = watching_source(tmp_path)
    fakes.mine = discussion()
    source.refresh()
    [talk] = [i for i in source.load().items if i.series.discussion]
    assert 'Watching' in source.toggle_watched(talk)
    snapshot, index = snap(source)
    [talk] = index.items('watching')
    assert talk.watched and render_row(talk)[3] == '$'
    assert talk not in index.items('review')
    assert 'No longer watching' in source.toggle_watched(talk)
    assert 'watching' not in {v.id for v in snap(source)[1].views}


def test_watched_discussions_count_every_new_message(tmp_path):
    source, fakes, _ = watching_source(tmp_path)
    fakes.mine = discussion()
    source.refresh()
    [talk] = [i for i in source.load().items if i.series.discussion]
    source.toggle_watched(talk)
    [item] = snap(source)[1].items('responses')
    assert item.unread_msgids == {
        'root@example.org',
        'early@example.org',
        'answer@example.org',
        'late1@example.org',
        'late2@example.org',
        'aside@example.org',
    }


def test_refresh_keeps_and_refetches_watched_threads(tmp_path):
    source, fakes, _ = watching_source(tmp_path)
    fakes.mine = discussion()
    source.refresh()
    [talk] = [i for i in source.load().items if i.series.discussion]
    source.toggle_watched(talk)
    newer = message(
        'newest@example.org', 'Other <other@example.org>', SUBJECT, 0, 'late2@example.org'
    )
    fakes.mine = discussion() + [newer]
    later = NOW + 40 * DAY  # Long after the thread went quiet.
    source._now = lambda: later
    newer.replace_header('Date', email.utils.format_datetime(later))
    source.refresh()
    assert fakes.thread_calls[-1] == 'root@example.org'
    [talk] = [i for i in source.load().items if i.series.discussion]
    assert 'newest@example.org' in talk.series.msgids


def test_watching_needs_a_discussion(tmp_path):
    import pytest

    source, fakes, _ = watching_source(tmp_path)
    _, fakes.answers[MM] = mail(tmp_path, 'a', 'mm: a series')
    source.refresh()
    [series] = source.load().items
    with pytest.raises(RuntimeError, match='discussion'):
        source.toggle_watched(series)


def test_a_thread_is_fetched_by_link_or_message_id_and_kept_for_its_workbench(tmp_path):
    from b4_maintainer.adapters.live import message_id_of
    from b4_maintainer.projectsetup import config_path
    from b4_maintainer.workbench import Workbench, WorkbenchStore, add_mail

    assert message_id_of('<a@b.org>') == 'a@b.org'
    assert message_id_of('id:a@b.org') == 'a@b.org'
    assert message_id_of('https://lore.kernel.org/linux-mm/a@b.org/T/#t') == 'a@b.org'
    assert message_id_of('https://lore.kernel.org/r/a%40b.org') == 'a@b.org'
    assert message_id_of('https://patch.msgid.link/a@b.org') == 'a@b.org'
    assert message_id_of('not a message id') == ''
    source, fakes, _ = watching_source(tmp_path)
    fakes.mine = discussion()
    msgid = source.fetch_thread('https://lore.kernel.org/linux-mm/root@example.org/')
    assert msgid == 'root@example.org' and fakes.thread_calls == ['root@example.org']
    assert any('root@example.org' in i.series.msgids for i in source.load().items)
    with pytest.raises(ValueError, match='Message-ID'):
        source.fetch_thread('nothing here')
    store = WorkbenchStore(config_path(source.root).parent / 'workbenches')
    store.save(
        add_mail(Workbench('w', 'W'), 'linux', 'root@example.org', 'Talk', 'root@example.org')
    )
    assert 'root@example.org' in source.bench_msgids()  # Kept past the browse window.

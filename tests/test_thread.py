import b4
import pytest

from b4_maintainer.adapters.b4mbox import load_thread, parse_mbox
from b4_maintainer.fixtures import Followup, PatchSpec, write_series_mbox


def two_patch_series(tmp_path, **kwargs):
    return write_series_mbox(
        tmp_path / 'series.mbx',
        subject='mm: two step change',
        patches=[PatchSpec('mm/a.c'), PatchSpec('mm/b.c')],
        **kwargs,
    )


def test_series_records_its_mail_source(tmp_path):
    mail = two_patch_series(tmp_path)
    [series] = parse_mbox(mail.path)
    assert series.source == str(mail.path)


def test_discussion_is_grouped_by_patch_and_cover(tmp_path):
    mail = two_patch_series(
        tmp_path,
        followups=[
            Followup(0, body='Cover question.'),
            Followup(2, 'Reviewed-by: Reviewer Two <reviewer2@example.org>'),
            Followup(
                2,
                body='Nested reply.',
                parent=2,
                sender=('Contributor One', 'contributor1@example.org'),
            ),
        ],
    )
    [series] = parse_mbox(mail.path)
    thread = load_thread(series)

    assert thread.cover is not None
    assert [r.message.msgid for r in thread.cover.replies] == [mail.followups[0]]
    assert thread.patches[0].replies == ()
    replies = thread.patches[1].replies
    assert [(r.message.msgid, r.depth) for r in replies] == [
        (mail.followups[1], 0),
        (mail.followups[2], 1),
    ]
    assert 'Nested reply.' in replies[1].message.body
    assert replies[1].message.author_email == 'contributor1@example.org'
    assert thread.unattached == ()


def test_patch_description_and_diff_are_split(tmp_path):
    [series] = parse_mbox(two_patch_series(tmp_path).path)
    patch = load_thread(series).patches[0]
    assert patch.counter == 1
    assert 'Synthetic change touching mm/a.c' in patch.description
    assert '1 file changed' in patch.description
    assert 'diff --git' not in patch.description
    assert patch.diff.startswith('diff --git a/mm/a.c b/mm/a.c')
    assert '+new' in patch.diff


def test_cover_has_no_diff(tmp_path):
    [series] = parse_mbox(two_patch_series(tmp_path).path)
    cover = load_thread(series).cover
    assert cover.counter == 0
    assert cover.diff == ''
    assert 'Synthetic cover letter.' in cover.description


def test_single_patch_without_cover(tmp_path):
    mail = write_series_mbox(
        tmp_path / 'single.mbx',
        subject='selftests/mm: single',
        revision=2,
        cover=False,
        followups=[Followup(1, body='Looks fine.')],
    )
    [series] = parse_mbox(mail.path)
    thread = load_thread(series)
    assert thread.cover is None
    assert [p.counter for p in thread.patches] == [1]
    assert [r.message.msgid for r in thread.patches[0].replies] == [mail.followups[0]]


def test_missing_patch_is_absent_not_invented(tmp_path):
    mail = write_series_mbox(
        tmp_path / 'gap.mbx',
        subject='mm: gap',
        patches=[PatchSpec('mm/a.c'), PatchSpec('mm/b.c'), PatchSpec('mm/c.c')],
        omit=(2,),
    )
    [series] = parse_mbox(mail.path)
    assert [p.counter for p in load_thread(series).patches] == [1, 3]


def test_unthreaded_reply_is_unattached_not_matched_by_subject(tmp_path):
    mail = two_patch_series(tmp_path, followups=[Followup(1, body='Lost reply.', threaded=False)])
    [series] = parse_mbox(mail.path)
    thread = load_thread(series)
    assert all(p.replies == () for p in thread.patches)
    assert [r.message.msgid for r in thread.unattached] == [mail.followups[0]]


def test_reroll_thread_excludes_other_revisions(tmp_path):
    v1 = write_series_mbox(
        tmp_path / 'both.mbx',
        subject='mm: tidy',
        change_id='tidy-1',
        followups=[Followup(1, body='v1 comment.')],
    )
    v2 = write_series_mbox(
        v1.path,
        subject='mm: tidy',
        revision=2,
        change_id='tidy-1',
        append=True,
        followups=[Followup(1, body='v2 comment.')],
    )
    series = {s.revision: s for s in parse_mbox(v1.path)}
    thread = load_thread(series[2])
    assert [p.message.msgid for p in thread.patches] == [v2.msgids[1]]
    assert [r.message.msgid for r in thread.patches[0].replies] == [v2.followups[0]]
    assert thread.unattached == ()


def test_loading_a_thread_never_queries_lore(tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail('load_thread must not search lore')

    monkeypatch.setattr(b4, 'get_pi_search_results', refuse)
    mail = two_patch_series(tmp_path, followups=[Followup(1, body='Hi.')])
    [series] = parse_mbox(mail.path)
    assert load_thread(series).patches


def test_cyclic_replies_are_kept_as_unattached(tmp_path):
    import email.message
    import mailbox

    mail = two_patch_series(tmp_path)
    box = mailbox.mbox(mail.path)
    for me, parent in (
        ('cycle-a@example.org', 'cycle-b@example.org'),
        ('cycle-b@example.org', 'cycle-a@example.org'),
    ):
        msg = email.message.EmailMessage()
        msg['From'] = 'Reviewer One <reviewer1@example.org>'
        msg['Subject'] = 'Re: mm: two step change'
        msg['Date'] = 'Thu, 01 Oct 2026 13:00:00 +0000'
        msg['Message-ID'] = f'<{me}>'
        msg['In-Reply-To'] = f'<{parent}>'
        msg.set_content('Cyclic.')
        box.add(msg)
    box.flush()
    box.close()

    [series] = parse_mbox(mail.path)
    thread = load_thread(series)
    assert sorted(r.message.msgid for r in thread.unattached) == [
        'cycle-a@example.org',
        'cycle-b@example.org',
    ]

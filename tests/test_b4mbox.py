import b4
import pytest

from b4_maintainer.adapters.b4mbox import parse_mbox
from b4_maintainer.fixtures import Followup, PatchSpec, write_series_mbox
from b4_maintainer.model import Trailers

KSELFTEST = 'linux-kselftest.vger.kernel.org'
LINUX_MM = 'linux-mm.kvack.org'


def test_single_patch_without_cover(tmp_path):
    mail = write_series_mbox(
        tmp_path / 'a.mbx',
        subject='selftests/mm: merge: keep far targets in the carveout',
        revision=2,
        patches=[PatchSpec('tools/testing/selftests/mm/merge.c')],
        cover=False,
        lists=[KSELFTEST, LINUX_MM],
    )
    [series] = parse_mbox(mail.path)
    assert series.revision == 2
    assert series.complete
    assert not series.has_cover
    assert series.change_id is None
    assert series.identity == mail.msgids[1]
    assert series.subject == 'selftests/mm: merge: keep far targets in the carveout'
    assert series.paths == ('tools/testing/selftests/mm/merge.c',)
    assert series.lists == (KSELFTEST,)
    assert 'linux-mm@kvack.org' in series.recipients
    assert {KSELFTEST, LINUX_MM} <= set(series.list_candidates())
    assert series.author_email.endswith('@example.org')


def test_missing_patch_is_incomplete_and_change_id_is_identity(tmp_path):
    mail = write_series_mbox(
        tmp_path / 'b.mbx',
        subject='cxl: add bar region',
        patches=[
            PatchSpec('drivers/cxl/a.c'),
            PatchSpec('drivers/cxl/b.c'),
            PatchSpec('drivers/cxl/c.c'),
        ],
        omit=(2,),
        change_id='20261001-cxl-bar-0123456789ab',
    )
    [series] = parse_mbox(mail.path)
    assert series.has_cover
    assert not series.complete
    assert series.identity == 'change-id:20261001-cxl-bar-0123456789ab'
    assert [p.counter for p in series.patches] == [1, 3]


def test_followup_trailers_keep_cover_and_patch_evidence_separate(tmp_path):
    mail = write_series_mbox(
        tmp_path / 'c.mbx',
        subject='mm: two step change',
        patches=[PatchSpec('mm/a.c'), PatchSpec('mm/b.c')],
        followups=[
            Followup(0, 'Acked-by: Reviewer One <reviewer1@example.org>'),
            Followup(1, 'Reviewed-by: Reviewer Two <reviewer2@example.org>'),
            Followup(2, 'Tested-by: Tester One <tester1@example.org>'),
        ],
    )
    [series] = parse_mbox(mail.path)
    assert series.cover_trailers.acked == 1
    assert [(p.trailers.reviewed, p.trailers.tested) for p in series.patches] == [(1, 0), (0, 1)]
    assert series.trailer_totals() == (1, 1, 1)


def test_all_revisions_in_a_mailbox_are_returned(tmp_path):
    first = write_series_mbox(tmp_path / 'v1.mbx', subject='mm: tidy', revision=1, change_id='x-1')
    write_series_mbox(first.path, subject='mm: tidy', revision=2, change_id='x-1', append=True)
    revisions = sorted(s.revision for s in parse_mbox(first.path))
    assert revisions == [1, 2]


def test_parsing_never_queries_lore(tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail('parse_mbox must not search lore for trailers')

    monkeypatch.setattr(b4, 'get_pi_search_results', refuse)
    mail = write_series_mbox(
        tmp_path / 'd.mbx', subject='mm: x', followups=[Followup(1, 'Acked-by: A <a@example.org>')]
    )
    assert parse_mbox(mail.path)


def test_preview_is_cover_letter_when_present(tmp_path):
    mail = write_series_mbox(
        tmp_path / 'e.mbx',
        subject='mm: with cover',
        patches=[PatchSpec('mm/a.c'), PatchSpec('mm/b.c')],
        cover_text='Why this series exists.\n\nMore detail.',
    )
    [series] = parse_mbox(mail.path)
    assert series.preview_label == 'Cover letter'
    assert 'Why this series exists.' in series.preview


def test_preview_is_commit_message_without_cover(tmp_path):
    mail = write_series_mbox(tmp_path / 'f.mbx', subject='mm: single', cover=False)
    [series] = parse_mbox(mail.path)
    assert series.preview_label == 'Patch 1/1 (no cover letter)'
    assert 'Synthetic change touching mm/example.c' in series.preview
    assert '1 file changed' in series.preview
    assert 'diff --git' not in series.preview


def test_commit_trailers_are_carried_evidence_deduplicated_with_replies(tmp_path):
    mail = write_series_mbox(
        tmp_path / 'g.mbx',
        subject='mm: carried tags',
        patches=[
            PatchSpec(
                'mm/a.c',
                trailers=(
                    'Acked-by: Reviewer One <reviewer1@example.org>',
                    'Reviewed-by: Reviewer Two <reviewer2@example.org>',
                ),
            ),
            PatchSpec('mm/b.c'),
        ],
        followups=[
            Followup(1, 'Reviewed-by: Reviewer Two <REVIEWER2@example.org>'),
            Followup(2, 'Tested-by: Tester One <tester1@example.org>'),
        ],
    )
    [series] = parse_mbox(mail.path)
    first, second = series.patches
    assert first.carried_trailers == Trailers(acked=1, reviewed=1)
    assert first.reply_trailers == Trailers(reviewed=1)
    assert first.trailers == Trailers(acked=1, reviewed=1)
    assert second.carried_trailers == Trailers()
    assert second.trailers == Trailers(tested=1)
    assert series.trailer_totals() == (1, 1, 1)


def test_fixes_and_stable_tags_are_read_from_every_patch(tmp_path):
    mail = write_series_mbox(
        tmp_path / 'f.mbx',
        subject='mm: fix things',
        patches=[
            PatchSpec('mm/a.c'),
            PatchSpec(
                'mm/b.c',
                trailers=(
                    'Fixes: 0123456789ab ("mm: introduce the bug")',
                    'Cc: stable@vger.kernel.org # 6.1+',
                ),
            ),
            PatchSpec('mm/c.c', trailers=('Cc: <stable@kernel.org>',)),
        ],
    )
    [series] = parse_mbox(mail.path)
    first, second, third = series.patches
    assert first.fixes == () and not first.stable
    assert second.fixes == ('0123456789ab ("mm: introduce the bug")',) and second.stable
    assert third.fixes == () and third.stable
    assert series.cc_stable


def test_series_without_fix_tags(tmp_path):
    mail = write_series_mbox(
        tmp_path / 'n.mbx', subject='mm: feature', patches=[PatchSpec('mm/a.c')]
    )
    [series] = parse_mbox(mail.path)
    assert not series.cc_stable


def test_a_project_tag_in_the_subject_prefix_is_kept(tmp_path):
    mail = write_series_mbox(
        tmp_path / 'n.mbx',
        subject='cxl/list: show the region mode',
        revision=2,
        patches=[PatchSpec('cxl/list.c'), PatchSpec('test/cxl-region.sh')],
        tag='ndctl',
        lists=['nvdimm.lists.linux.dev'],
    )
    [series] = parse_mbox(mail.path)
    assert 'ndctl' in series.prefixes and series.subject == 'cxl/list: show the region mode'
    untagged = write_series_mbox(tmp_path / 'k.mbx', subject='cxl: fix the region mode')
    assert 'ndctl' not in parse_mbox(untagged.path)[0].prefixes

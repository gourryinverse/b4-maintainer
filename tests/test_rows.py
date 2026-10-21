from b4._textwidth import display_width

from b4_maintainer.fixtures import make_item
from b4_maintainer.model import Authority, Coverage, Progress
from b4_maintainer.rows import (
    AUTHOR_WIDTH,
    DEFAULT_SYMBOLS,
    Symbols,
    render_row,
    row_segments,
    tree_field,
)

ACC = Authority.ACCEPTANCE
MAIN = Authority.MAINLINE
PRE = Authority.PRELIMINARY


def test_row_matches_documented_example():
    item = make_item(
        subject='mm: fix foo leak',
        revision=3,
        acked=2,
        reviewed=1,
        unread=1,
        fixes_pending=True,
        draft=True,
        tree=[('n', ACC, Coverage.PARTIAL)],
    )
    assert render_row(item) == '●⚠✎ CAR· n≈ Synthetic Author v3  mm: fix foo leak'


def test_quiet_incomplete_unchecked_row():
    item = make_item(subject='ndctl: baz cleanup', complete=False)
    assert render_row(item) == '    I···  ? Synthetic Author v1  ndctl: baz cleanup'


def test_progress_symbols():
    assert render_row(make_item(progress=Progress.PARTIAL))[2] == '◐'
    assert render_row(make_item(progress=Progress.DONE))[2] == '✓'
    # A pending draft outranks local completion in the single progress column.
    assert render_row(make_item(progress=Progress.DONE, draft=True))[2] == '✎'


def test_tree_field_prefers_highest_authority_match():
    ev = [('m', MAIN, Coverage.PARTIAL), ('n', ACC, Coverage.FULL)]
    assert tree_field(make_item(tree=ev).state.tree, DEFAULT_SYMBOLS) == 'm≈'
    ev = [('m', MAIN, Coverage.NONE), ('p', PRE, Coverage.FULL)]
    assert tree_field(make_item(tree=ev).state.tree, DEFAULT_SYMBOLS) == 'p≡'


def test_tree_field_unknown_is_not_absent():
    def field(ev):
        return tree_field(make_item(tree=ev).state.tree, DEFAULT_SYMBOLS)

    assert field([]) == ' ?'
    assert field([('m', MAIN, Coverage.NONE), ('n', ACC, Coverage.UNCHECKED)]) == ' ?'
    assert field([('m', MAIN, Coverage.NONE), ('n', ACC, Coverage.STALE)]) == ' ~'
    assert field([('m', MAIN, Coverage.NONE), ('n', ACC, Coverage.NONE)]) == ' ·'


def test_subject_column_aligns_across_symbol_combinations():
    items = [
        make_item(subject='x'),
        make_item(subject='x', unread=3, fixes_pending=True, draft=True, revision=12),
        make_item(subject='x', acked=1, reviewed=1, tested=1, tree=[('n', ACC, Coverage.FULL)]),
    ]
    offsets = {display_width(render_row(i).rsplit('x', 1)[0]) for i in items}
    assert len(offsets) == 1


def test_symbols_are_configurable():
    ascii_syms = Symbols(fixes='!', unread='*')
    row = render_row(make_item(unread=1, fixes_pending=True), ascii_syms)
    assert row.startswith('*!')


def test_project_column_when_requested():
    assert ' lnx  ' in render_row(make_item(project='linux', short='lnx'), project_width=5)
    assert ' ?    ' in render_row(make_item(candidates=('linux', 'qemu')), project_width=5)


def test_carried_commit_tags_show_in_row():
    from b4_maintainer.model import Trailers

    assert ' C·R· ' in render_row(make_item(carried=Trailers(reviewed=1)))


def test_author_column_fits_names_by_display_width():
    def author(**kwargs):
        [segment] = [t for t, role in row_segments(make_item(**kwargs)) if role is not None]
        return segment

    assert author(author_name='Ann') == 'Ann'.ljust(AUTHOR_WIDTH)
    long = author(author_name='Bartholomew Longname-Example')
    assert display_width(long) == AUTHOR_WIDTH and long.endswith('…')
    wide = author(author_name='山田太郎山田太郎山田太郎')
    assert display_width(wide) == AUTHOR_WIDTH
    assert author(author_name='', author_email='someone@example.org').startswith('someone ')


def test_author_segment_carries_maintainers_role():
    roles = {
        role
        for item_role in ('maintainer', 'reviewer', '')
        for _, role in row_segments(make_item(author_role=item_role))
        if role is not None
    }
    assert roles == {'maintainer', 'reviewer', ''}
    text = ''.join(t for t, _ in row_segments(make_item(author_role='maintainer')))
    assert text == render_row(make_item(author_role='maintainer'))

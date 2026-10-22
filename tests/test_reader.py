import threading
import time

from textual.app import App

from b4_maintainer.adapters.fixture import FixtureSource
from b4_maintainer.app import MaintainerApp, SeriesList
from b4_maintainer.fixtures import make_item, synthetic_snapshot, workspace_config
from b4_maintainer.model import Message, Post, Reply, SeriesThread
from b4_maintainer.widgets import Block, TextPane
from b4_maintainer.workspace import Snapshot, Workspace


def footer_keys(app):
    return {key for key, active in app.screen.active_bindings.items() if active.binding.show}


async def loaded(pilot):
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()
    return pilot.app.query_one(SeriesList)


async def open_reader(pilot):
    await pilot.press('enter')
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


async def test_enter_opens_reader_on_cover_and_escape_returns(demo_root):
    app = MaintainerApp(FixtureSource(demo_root).load)
    async with app.run_test(size=(180, 40)) as pilot:
        series_list = await loaded(pilot)
        item = series_list.highlighted_item
        await open_reader(pilot)

        assert app.reading is item
        assert app.visible_panes() == ()
        assert app.reader_panes() == ('tree', 'content')
        labels = app.tree_labels()
        assert labels[0].startswith('▸ 0/5 cover (1)')
        assert [label.split()[0] for label in labels[1:]] == ['1/5', '2/5', '▸', '4/5', '5/5']
        assert labels[3].startswith('▸ 3/5') and labels[3].endswith('(3)')
        text = app.content_text()
        assert 'Synthetic cover letter.' in text
        assert 'Acked-by: Reviewer One' not in text

        await pilot.press('escape')
        await pilot.pause()
        assert app.reading is None
        assert app.visible_panes() == ('views', 'series', 'summary')
        assert series_list.highlighted_item is item


async def select_row(pilot, index):
    pilot.app.query_one('#tree').highlighted = index
    await pilot.pause()


async def test_minus_expands_and_collapses_a_patchs_replies(demo_root):
    app = MaintainerApp(FixtureSource(demo_root).load)
    async with app.run_test(size=(180, 40)) as pilot:
        await loaded(pilot)
        await open_reader(pilot)
        await select_row(pilot, 3)
        text = app.content_text()
        assert 'Synthetic change touching mm/foo3.c' in text
        assert 'diff --git a/mm/foo3.c b/mm/foo3.c' in text
        assert 'minus' in footer_keys(app)

        await pilot.press('minus')
        await pilot.pause()
        labels = app.tree_labels()
        assert labels[3].startswith('▾ 3/5')
        # Branches without unread replies start folded.
        assert [label.split(' 10-')[0] for label in labels[4:6]] == [
            '  ├─> Reviewer Two',
            '  └─> Reviewer One',
        ]
        assert labels[4].endswith(' R') and labels[5].endswith(' (+1)')
        assert app.tree_position() == ('3', 0)

        await pilot.press('down')
        await pilot.pause()
        assert app.tree_position() == ('3', 1)
        text = app.content_text()
        assert 'Reviewed-by: Reviewer Two <reviewer2@example.org>' in text
        assert 'diff --git' not in text

        # On a reply, - collapses its patch and returns to the patch row.
        await pilot.press('minus')
        await pilot.pause()
        assert len(app.tree_labels()) == len(labels) - 2
        assert app.tree_position() == ('3', 0)


async def test_long_quotes_collapse_and_expand(demo_root):
    app = MaintainerApp(FixtureSource(demo_root).load)
    async with app.run_test(size=(180, 40)) as pilot:
        await loaded(pilot)
        await open_reader(pilot)
        await select_row(pilot, 3)
        await pilot.press('minus')
        await pilot.pause()
        await select_row(pilot, 5)
        text = app.content_text()
        assert '> [8 quoted lines]' in text
        assert '> quoted line 1\n' not in text + '\n'
        assert '> quoted line 9' in text and '> quoted line 10' in text
        assert 'Comment after a long quote.' in text
        assert 'z' in footer_keys(app)

        await pilot.press('z')
        await pilot.pause()
        assert '> quoted line 1\n' in app.content_text() + '\n'
        assert 'quoted lines]' not in app.content_text()
        await pilot.press('z')
        await pilot.pause()
        assert '> [8 quoted lines]' in app.content_text()


async def test_bracket_and_n_keys_move_between_patches_and_comments(demo_root):
    app = MaintainerApp(FixtureSource(demo_root).load)
    async with app.run_test(size=(180, 40)) as pilot:
        await loaded(pilot)
        await open_reader(pilot)
        await pilot.press('right_square_bracket')
        await pilot.pause()
        assert app.tree_position() == ('1', 0)
        await pilot.press('left_square_bracket')
        await pilot.pause()
        assert app.tree_position() == ('0', 0)

        # n walks replies in series order, expanding each patch it enters.
        await pilot.press('n')
        await pilot.pause()
        assert app.tree_position() == ('0', 1)
        assert app.tree_labels()[0].startswith('▾ 0/5')
        await pilot.press('n')
        await pilot.pause()
        assert app.tree_position() == ('3', 1)
        await pilot.press('n')
        await pilot.pause()
        assert app.tree_position() == ('3', 2)
        assert 'Comment after a long quote.' in app.content_text()

        # ] from a reply goes to the next patch row.
        await pilot.press('right_square_bracket')
        await pilot.pause()
        assert app.tree_position() == ('4', 0)


async def test_reader_layout_follows_width(demo_root):
    app = MaintainerApp(FixtureSource(demo_root).load)
    async with app.run_test(size=(130, 40)) as pilot:
        await loaded(pilot)
        await open_reader(pilot)
        assert app.reader_panes() == ('tree', 'content')

        await pilot.resize_terminal(90, 40)
        await pilot.pause()
        assert app.reader_panes() == ('tree',)
        await pilot.press('enter')
        await pilot.pause()
        assert app.reader_panes() == ('content',)
        await pilot.press('escape')
        await pilot.pause()
        assert app.reader_panes() == ('tree',)
        await pilot.press('escape')
        await pilot.pause()
        assert app.reading is None


async def test_reader_footer_shows_only_applicable_keys(demo_root):
    app = MaintainerApp(FixtureSource(demo_root).load)
    async with app.run_test(size=(180, 40)) as pilot:
        await loaded(pilot)
        await open_reader(pilot)
        keys = footer_keys(app)
        assert {'escape', 'tab', 'right_square_bracket', 'n', 'minus', 'grave_accent', 'Q'} <= keys
        assert not {'d', 'c', 'v', 'p', 'z'} & keys
        await select_row(pilot, 1)
        assert 'minus' not in footer_keys(app)  # Patch 1/5 has no replies.


async def test_drawer_line_stays_visible_and_pinned_while_reading(demo_root):
    app = MaintainerApp(FixtureSource(demo_root).load)
    async with app.run_test(size=(180, 40)) as pilot:
        series_list = await loaded(pilot)
        before = app.drawer_text()
        await pilot.press('down')
        await open_reader(pilot)
        assert app.reading is series_list.highlighted_item
        assert app.drawer_text() == before
        assert app.query_one('#drawer').display


async def test_reader_opens_within_target(demo_root):
    app = MaintainerApp(FixtureSource(demo_root).load)
    async with app.run_test(size=(180, 40)) as pilot:
        await loaded(pilot)
        start = time.perf_counter()
        await open_reader(pilot)
        assert 'Synthetic cover letter.' in app.content_text()
        assert time.perf_counter() - start <= 0.5


def stub_thread(item, **kwargs):
    msg = Message(
        'p1@example.org', 'Contributor One', 'contributor1@example.org', None, 'subject', 'Body.'
    )
    post = Post(1, msg, 'Body.', 'diff --git a/x b/x', ())
    return SeriesThread(item.identity, item.series.revision, None, (post,), **kwargs)


def single_item_app(loader):
    item = make_item(subject='mm: stub')
    snapshot = Snapshot(Workspace.from_config(workspace_config()), (item,))
    return item, MaintainerApp(lambda: snapshot, thread_loader=lambda s: loader(item, s))


async def test_thread_loads_off_ui_thread_and_errors_are_shown():
    seen = []

    def failing(item, series):
        seen.append(threading.current_thread() is threading.main_thread())
        raise RuntimeError('boom')

    _, app = single_item_app(failing)
    async with app.run_test(size=(180, 40)) as pilot:
        await loaded(pilot)
        await open_reader(pilot)
        text = app.content_text()
        assert 'Could not load thread' in text and 'boom' in text
    assert seen == [False]


async def test_unattached_replies_get_their_own_entry():
    lost = Message(
        'lost@example.org', 'Reviewer One', 'reviewer1@example.org', None, 'Re: x', 'Lost reply.'
    )
    _, app = single_item_app(lambda item, s: stub_thread(item, unattached=(Reply(lost, 0),)))
    async with app.run_test(size=(180, 40)) as pilot:
        await loaded(pilot)
        await open_reader(pilot)
        assert app.tree_labels()[-1] == '▸ Unattached (1)'
        await pilot.press('end')
        await pilot.pause()
        assert 'could not be linked' in app.content_text()
        await pilot.press('minus', 'down')
        await pilot.pause()
        assert app.tree_labels()[-1].startswith('  └─> Reviewer One')
        assert 'Lost reply.' in app.content_text()


async def test_series_without_mail_source_reports_it():
    snapshot = synthetic_snapshot(3)
    app = MaintainerApp(lambda: snapshot)
    async with app.run_test(size=(180, 40)) as pilot:
        await loaded(pilot)
        await open_reader(pilot)
        assert 'no mail source' in app.content_text()


async def test_diff_lines_scroll_sideways_while_prose_wraps():
    class Host(App):
        def compose(self):
            yield TextPane(id='pane')

    app = Host()
    async with app.run_test(size=(60, 20)) as pilot:
        pane = app.query_one(TextPane)
        pane.set_blocks([Block('+' + 'x' * 300, wrap=False), Block('word ' * 60)])
        await pilot.pause()
        width = pane.scrollable_content_region.width
        assert pane.virtual_size.width >= 301 > width
        assert pane.virtual_size.height > 3


async def test_titles_and_labels_render_brackets_literally(tmp_path):
    from textual.content import Content
    from textual.widgets import OptionList

    from b4_maintainer.fixtures import build_demo_workspace

    profiles = [
        {'id': 'mm', 'label': '[mm] core', 'project': 'linux', 'lists': ['linux-mm.kvack.org']}
    ]
    root = build_demo_workspace(tmp_path / 'demo', profiles=profiles)
    app = MaintainerApp(FixtureSource(root).load)
    async with app.run_test(size=(180, 40)) as pilot:
        await loaded(pilot)
        views = app.query_one('#views', OptionList)
        prompts = [
            Content.from_markup(str(views.get_option_at_index(i).prompt)).plain
            for i in range(views.option_count)
        ]
        assert any(p.startswith('[mm] core') for p in prompts)


async def test_patch_label_and_content_include_carried_tags(demo_root):
    app = MaintainerApp(FixtureSource(demo_root).load)
    async with app.run_test(size=(180, 40)) as pilot:
        await loaded(pilot)
        await open_reader(pilot)
        assert app.tree_labels()[2] == '  2/5 ·R·'
        await pilot.press('down', 'down')
        await pilot.pause()
        assert (
            'Trailers: A:0 R:1 T:0 (carried A:0 R:1 T:0 · replies A:0 R:0 T:0)'
            in app.content_text()
        )


async def test_tree_labels_and_titles_render_brackets_literally():
    from textual.content import Content

    odd = Message('odd@example.org', 'Rev [x] One', 'rev@example.org', None, 'Re: x', 'Hi.')
    _, app = single_item_app(lambda item, s: stub_thread_with_replies(item, (Reply(odd, 0),)))
    async with app.run_test(size=(180, 40)) as pilot:
        await loaded(pilot)
        await open_reader(pilot)
        await pilot.press('minus', 'down')
        await pilot.pause()
        assert '[x]' in app.tree_labels()[1]
        title = Content.from_markup(str(app.query_one('#content').border_title)).plain
        assert 'Rev [x] One' in title


def stub_thread_with_replies(item, replies):
    msg = Message(
        'p1@example.org', 'Contributor One', 'contributor1@example.org', None, 'subject', 'Body.'
    )
    post = Post(1, msg, 'Body.', 'diff --git a/x b/x', replies)
    return SeriesThread(item.identity, item.series.revision, None, (post,))


def test_tree_art_from_depths():
    from b4_maintainer.reader import tree_art

    assert tree_art([0, 1, 0, 1, 2]) == ['├─>', '│ └─>', '└─>', '  └─>', '    └─>']
    assert tree_art([0, 0, 0]) == ['├─>', '├─>', '└─>']
    assert tree_art([]) == []


def test_collapse_quotes_keeps_short_quotes_and_tail_of_long_ones():
    from b4_maintainer.reader import collapse_quotes

    def text(blocks):
        return '\n'.join(b.text for b in blocks)

    long_quote = '\n'.join(f'> line {n}' for n in range(1, 11)) + '\n\nReply.'
    collapsed = text(collapse_quotes(long_quote, expand=False))
    assert '> [8 quoted lines]' in collapsed
    assert '> line 1\n' not in collapsed + '\n'
    assert '> line 9\n> line 10' in collapsed and 'Reply.' in collapsed
    assert text(collapse_quotes(long_quote, expand=True)) == long_quote

    short = '> a\n> b\n> c\nReply.'
    assert text(collapse_quotes(short, expand=False)) == short


def test_mail_signature_separator_is_not_a_removed_line():
    from rich.style import Style

    from b4_maintainer.reader import diff_style

    assert diff_style('-- ') == Style()
    assert diff_style('-removed') != Style()


def _reply(msgid, author):
    return Message(msgid, author, f'{msgid}', None, 'Re: subject', 'Comment.')


def test_reply_labels_mark_unread_replies():
    from b4_maintainer.reader import reply_labels

    replies = (Reply(_reply('r1@example.org', 'One'), 0), Reply(_reply('r2@example.org', 'Two'), 0))
    labels = reply_labels(replies, unread={'r2@example.org'})
    assert '●' not in labels[0] and '●' in labels[1]
    assert reply_labels(replies) == [label.replace('● ', '') for label in labels]


class SeenActions:
    def __init__(self, item):
        self.item = item
        self.marked = []
        self.loads = 0

    def load(self):
        import dataclasses

        self.loads += 1
        unread = frozenset({'r1@example.org'}) - set(self.marked)
        item = dataclasses.replace(self.item, unread_msgids=unread)
        return Snapshot(Workspace.from_config(workspace_config()), (item,))

    def refresh(self, progress):
        return ''

    def track(self, item):
        return ''

    def toggle_hidden(self, item):
        return ''

    def mark_seen(self, msgids):
        self.marked.extend(msgids)

    def mark_read(self, item):
        self.marked.extend(item.unread_msgids)
        return 'Marked'


async def test_showing_an_unread_reply_marks_it_seen():
    item = make_item(subject='mm: answered')
    actions = SeenActions(item)
    replies = (Reply(_reply('r1@example.org', 'One'), 0), Reply(_reply('r2@example.org', 'Two'), 0))
    app = MaintainerApp(
        actions.load,
        actions=actions,
        thread_loader=lambda s: stub_thread_with_replies(item, replies),
    )
    async with app.run_test(size=(180, 40)) as pilot:
        await loaded(pilot)
        await open_reader(pilot)
        # The patch with an unread reply starts expanded.
        labels = app.tree_labels()
        assert labels[0].startswith('▾ 1/1')
        assert '●' in labels[1] and '●' not in labels[2]
        await select_row(pilot, 1)
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert actions.marked == ['r1@example.org']
        assert '●' not in app.tree_labels()[1]
        await pilot.press('escape')
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.reading is None
        assert actions.loads == 2
        assert app.index.count('responses') == 0


async def test_shift_r_in_reader_clears_unread_marks():
    item = make_item(subject='mm: answered')
    actions = SeenActions(item)
    replies = (Reply(_reply('r1@example.org', 'One'), 0), Reply(_reply('r2@example.org', 'Two'), 0))
    app = MaintainerApp(
        actions.load,
        actions=actions,
        thread_loader=lambda s: stub_thread_with_replies(item, replies),
    )
    async with app.run_test(size=(180, 40)) as pilot:
        await loaded(pilot)
        await open_reader(pilot)
        await pilot.press('minus')  # Collapse: the patch row carries the unread mark.
        await pilot.pause()
        assert app.tree_labels()[0].endswith(' ●')
        await pilot.press('R')
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert actions.marked == ['r1@example.org']
        assert not any('●' in label for label in app.tree_labels())
        assert app.reading is not None


def _tree_thread(item):
    replies = (
        Reply(_reply('r1@example.org', 'One'), 0),
        Reply(_reply('r2@example.org', 'Two'), 1),
    )
    msg = Message('p@example.org', 'C', 'c@example.org', None, 'subject', 'Body.')
    quiet = Message('q@example.org', 'C', 'c@example.org', None, 'subject', 'Body.')
    posts = (Post(1, msg, 'Body.', 'diff', replies), Post(2, quiet, 'Body.', 'diff', ()))
    return SeriesThread(item.identity, 1, None, posts)


def test_tree_rows_nest_replies_under_expanded_posts():
    from b4_maintainer.reader import tree_rows

    item = make_item(subject='mm: tree')
    thread = _tree_thread(item)
    collapsed = tree_rows(thread, item, expanded=set())
    assert [(r.key, r.node) for r in collapsed] == [('1', 0), ('2', 0)]
    assert collapsed[0].label.startswith('▸ 1/1') and collapsed[1].label.startswith('  2/1')
    rows = tree_rows(thread, item, expanded={'1'})
    assert [(r.key, r.node) for r in rows] == [('1', 0), ('1', 1), ('1', 2), ('2', 0)]
    assert rows[0].label.startswith('▾ 1/1')
    assert rows[1].label.startswith('  └─> One') and rows[2].label.startswith('    └─> Two')


def test_collapsed_posts_flag_unread_and_start_expanded():
    from b4_maintainer.reader import initially_expanded, tree_rows

    item = make_item(subject='mm: tree')
    thread = _tree_thread(item)
    assert initially_expanded(thread, {'r2@example.org'}) == {'1'}
    assert initially_expanded(thread, set()) == set()
    rows = tree_rows(thread, item, expanded=set(), unread={'r2@example.org'})
    assert rows[0].label.endswith(' ●')
    assert not tree_rows(thread, item, expanded=set())[0].label.endswith(' ●')


def _branchy(item):
    """1 - 1a - 1aa, 1ab; 1b - 1ba, 1bb."""
    replies = tuple(
        Reply(_reply(f'{name}@example.org', name), depth)
        for name, depth in (('1a', 0), ('1aa', 1), ('1ab', 1), ('1b', 0), ('1ba', 1), ('1bb', 1))
    )
    return stub_thread_with_replies(item, replies)


def names(labels):
    return [
        label.split('> ')[1].removeprefix('● ').split()[0] if '> ' in label else label.split()[1]
        for label in labels
    ]


def test_tree_rows_hide_collapsed_branches():
    from b4_maintainer.reader import tree_rows

    item = make_item(subject='mm: branches')
    thread = _branchy(item)
    rows = tree_rows(thread, item, {'1'}, collapsed={'1a@example.org'})
    assert [(r.key, r.node) for r in rows] == [('1', 0), ('1', 1), ('1', 4), ('1', 5), ('1', 6)]
    assert rows[1].label.endswith(' (+2)')
    unread = tree_rows(thread, item, {'1'}, {'1ab@example.org'}, collapsed={'1a@example.org'})
    assert unread[1].label.endswith(' (+2) ●')


async def test_minus_on_a_reply_collapses_only_its_branch():
    item = make_item(subject='mm: branches')
    snapshot = Snapshot(Workspace.from_config(workspace_config()), (item,))
    app = MaintainerApp(lambda: snapshot, thread_loader=lambda s: _branchy(item))
    async with app.run_test(size=(180, 40)) as pilot:
        await loaded(pilot)
        await open_reader(pilot)
        await pilot.press('minus', 'down')  # Expand the patch; onto 1a.
        await pilot.pause()
        assert app.tree_position() == ('1', 1)
        # Nothing is unread, so both branches start folded.
        assert names(app.tree_labels()) == ['1/1', '1a', '1b']

        await pilot.press('minus')  # Unfold 1a only.
        await pilot.pause()
        assert names(app.tree_labels()) == ['1/1', '1a', '1aa', '1ab', '1b']
        assert app.tree_position() == ('1', 1)
        await pilot.press('minus')  # Fold it again.
        await pilot.pause()
        assert names(app.tree_labels()) == ['1/1', '1a', '1b']

        await select_row(pilot, 2)  # 1b
        await pilot.press('minus')
        await pilot.pause()
        assert names(app.tree_labels()) == ['1/1', '1a', '1b', '1ba', '1bb']

        # On a leaf, - folds its parent's branch and moves there.
        await select_row(pilot, 3)
        assert app.tree_position() == ('1', 5)
        await pilot.press('minus')
        await pilot.pause()
        assert names(app.tree_labels()) == ['1/1', '1a', '1b']
        assert app.tree_position() == ('1', 4)

        # n unfolds a folded branch it lands in.
        await pilot.press('n')
        await pilot.pause()
        assert app.tree_position() == ('1', 5)
        assert names(app.tree_labels()) == ['1/1', '1a', '1b', '1ba', '1bb']

        # On the patch row, - still collapses everything.
        await select_row(pilot, 0)
        await pilot.press('minus')
        await pilot.pause()
        assert names(app.tree_labels()) == ['1/1']


def test_only_branches_with_unread_replies_start_open():
    from b4_maintainer.reader import initially_collapsed

    item = make_item(subject='mm: branches')
    thread = _branchy(item)
    assert initially_collapsed(thread, {'1ba@example.org'}) == {'1a@example.org'}
    assert initially_collapsed(thread, set()) == {'1a@example.org', '1b@example.org'}


async def test_opening_unfolds_only_unread_branches():
    import dataclasses

    item = dataclasses.replace(
        make_item(subject='mm: branches'), unread_msgids=frozenset({'1ba@example.org'})
    )
    snapshot = Snapshot(Workspace.from_config(workspace_config()), (item,))
    app = MaintainerApp(lambda: snapshot, thread_loader=lambda s: _branchy(item))
    async with app.run_test(size=(180, 40)) as pilot:
        await loaded(pilot)
        await open_reader(pilot)
        assert names(app.tree_labels()) == ['1/1', '1a', '1b', '1ba', '1bb']
        assert app.tree_labels()[1].endswith(' (+2)')


async def test_equals_expands_everything_then_collapses_everything():
    item = make_item(subject='mm: branches')
    snapshot = Snapshot(Workspace.from_config(workspace_config()), (item,))
    app = MaintainerApp(lambda: snapshot, thread_loader=lambda s: _branchy(item))
    everything = ['1/1', '1a', '1aa', '1ab', '1b', '1ba', '1bb']
    async with app.run_test(size=(180, 40)) as pilot:
        await loaded(pilot)
        await open_reader(pilot)
        assert names(app.tree_labels()) == ['1/1']
        assert 'equals_sign' in footer_keys(app)
        await pilot.press('equals_sign')
        await pilot.pause()
        assert names(app.tree_labels()) == everything

        await select_row(pilot, 4)  # 1b
        await pilot.press('equals_sign')  # All open: collapse all.
        await pilot.pause()
        assert names(app.tree_labels()) == ['1/1']
        assert app.tree_position() == ('1', 0)

        # Anything folded: = opens everything, keeping the cursor's reply.
        await pilot.press('equals_sign', 'down', 'minus')
        await pilot.pause()
        assert names(app.tree_labels()) == ['1/1', '1a', '1b', '1ba', '1bb']
        await pilot.press('equals_sign')
        await pilot.pause()
        assert names(app.tree_labels()) == everything
        assert app.tree_position() == ('1', 1)


async def test_arrow_keys_expand_and_collapse_in_the_tree():
    item = make_item(subject='mm: branches')
    snapshot = Snapshot(Workspace.from_config(workspace_config()), (item,))
    app = MaintainerApp(lambda: snapshot, thread_loader=lambda s: _branchy(item))
    async with app.run_test(size=(180, 40)) as pilot:
        await loaded(pilot)
        await open_reader(pilot)
        await pilot.press('right')
        await pilot.pause()
        assert names(app.tree_labels()) == ['1/1', '1a', '1b']
        await pilot.press('right')  # Already open: nothing more.
        await pilot.press('down', 'right')
        await pilot.pause()
        assert names(app.tree_labels()) == ['1/1', '1a', '1aa', '1ab', '1b']
        await pilot.press('left')  # Fold 1a again.
        await pilot.pause()
        assert names(app.tree_labels()) == ['1/1', '1a', '1b']
        assert app.tree_position() == ('1', 1)
        await pilot.press('left')  # 1a has nothing open: up to its posting, collapsed.
        await pilot.pause()
        assert names(app.tree_labels()) == ['1/1']
        assert app.tree_position() == ('1', 0)

        # In the content pane the arrows still scroll sideways.
        await pilot.press('tab')
        assert app.focused is app.query_one('#content')
        await pilot.press('right')
        await pilot.pause()
        assert names(app.tree_labels()) == ['1/1']

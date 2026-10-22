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
        assert app.reader_panes() == ('posts', 'thread', 'content')
        labels = app.post_labels()
        assert labels[0].startswith('0/5 cover')
        assert [label.split()[0] for label in labels[1:]] == ['1/5', '2/5', '3/5', '4/5', '5/5']
        thread = app.thread_labels()
        assert 'mm: fix foo leak' in thread[0]
        assert thread[1].startswith('└─> Reviewer One') and thread[1].endswith(' A')
        text = app.content_text()
        assert 'Synthetic cover letter.' in text
        assert 'Acked-by: Reviewer One' not in text

        await pilot.press('escape')
        await pilot.pause()
        assert app.reading is None
        assert app.visible_panes() == ('views', 'series', 'summary')
        assert series_list.highlighted_item is item


async def select_post(pilot, index):
    app = pilot.app
    app.query_one('#posts').highlighted = index
    await pilot.pause()


async def test_thread_tree_shows_patch_or_one_comment(demo_root):
    app = MaintainerApp(FixtureSource(demo_root).load)
    async with app.run_test(size=(180, 40)) as pilot:
        await loaded(pilot)
        await open_reader(pilot)
        await select_post(pilot, 3)
        thread = app.thread_labels()
        assert 'mm: fix foo leak (part 3)' in thread[0]
        assert [label.split(' 10-')[0] for label in thread[1:]] == [
            '├─> Reviewer Two',
            '└─> Reviewer One',
            '  └─> Contributor One',
        ]
        assert thread[1].endswith(' R')
        text = app.content_text()
        assert 'Synthetic change touching mm/foo3.c' in text
        assert 'diff --git a/mm/foo3.c b/mm/foo3.c' in text
        assert 'Reviewed-by: Reviewer Two' not in text

        await pilot.press('tab')
        assert app.focused is app.query_one('#thread')
        await pilot.press('down')
        await pilot.pause()
        text = app.content_text()
        assert 'Reviewed-by: Reviewer Two <reviewer2@example.org>' in text
        assert 'diff --git' not in text


async def test_long_quotes_collapse_and_expand(demo_root):
    app = MaintainerApp(FixtureSource(demo_root).load)
    async with app.run_test(size=(180, 40)) as pilot:
        await loaded(pilot)
        await open_reader(pilot)
        await select_post(pilot, 3)
        app.query_one('#thread').highlighted = 2
        await pilot.pause()
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
        posts, thread = app.query_one('#posts'), app.query_one('#thread')
        await pilot.press('right_square_bracket')
        await pilot.pause()
        assert posts.highlighted == 1
        await pilot.press('left_square_bracket')
        await pilot.pause()
        assert posts.highlighted == 0

        await pilot.press('n')
        await pilot.pause()
        assert (posts.highlighted, thread.highlighted) == (0, 1)
        await pilot.press('n')
        await pilot.pause()
        assert (posts.highlighted, thread.highlighted) == (3, 1)
        await pilot.press('n')
        await pilot.pause()
        assert (posts.highlighted, thread.highlighted) == (3, 2)
        assert 'Comment after a long quote.' in app.content_text()


async def test_reader_layout_follows_width(demo_root):
    app = MaintainerApp(FixtureSource(demo_root).load)
    async with app.run_test(size=(130, 40)) as pilot:
        await loaded(pilot)
        await open_reader(pilot)
        assert app.reader_panes() == ('thread', 'content')
        await pilot.press('right_square_bracket')
        await pilot.pause()
        assert app.query_one('#posts').highlighted == 1

        await pilot.resize_terminal(90, 40)
        await pilot.pause()
        assert app.reader_panes() == ('posts',)
        await pilot.press('enter')
        await pilot.pause()
        assert app.reader_panes() == ('thread',)
        await pilot.press('enter')
        await pilot.pause()
        assert app.reader_panes() == ('content',)
        await pilot.press('escape')
        await pilot.pause()
        assert app.reader_panes() == ('thread',)
        await pilot.press('escape', 'escape')
        await pilot.pause()
        assert app.reading is None


async def test_reader_footer_shows_only_applicable_keys(demo_root):
    app = MaintainerApp(FixtureSource(demo_root).load)
    async with app.run_test(size=(180, 40)) as pilot:
        await loaded(pilot)
        await open_reader(pilot)
        keys = footer_keys(app)
        assert {'escape', 'tab', 'right_square_bracket', 'n', 'grave_accent', 'Q'} <= keys
        assert not {'d', 'c', 'v', 'p', 'enter', 'z'} & keys


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
        assert app.post_labels()[-1] == 'Unattached (1)'
        await pilot.press('end')
        await pilot.pause()
        assert 'could not be linked' in app.content_text()
        assert app.thread_labels()[1].startswith('└─> Reviewer One')
        await pilot.press('tab', 'down')
        await pilot.pause()
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
        assert app.post_labels()[2] == '2/5 ·R·'
        await pilot.press('down', 'down')
        await pilot.pause()
        assert (
            'Trailers: A:0 R:1 T:0 (carried A:0 R:1 T:0 · replies A:0 R:0 T:0)'
            in app.content_text()
        )


async def test_thread_labels_and_titles_render_brackets_literally():
    from textual.content import Content

    odd = Message('odd@example.org', 'Rev [x] One', 'rev@example.org', None, 'Re: x', 'Hi.')
    _, app = single_item_app(lambda item, s: stub_thread_with_replies(item, (Reply(odd, 0),)))
    async with app.run_test(size=(180, 40)) as pilot:
        await loaded(pilot)
        await open_reader(pilot)
        assert '[x]' in app.thread_labels()[1]
        await pilot.press('tab', 'down')
        await pilot.pause()
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
        app._posts.highlighted = 0
        await pilot.pause()
        assert '●' in app.thread_labels()[1] and '●' not in app.thread_labels()[2]
        app._thread_list.highlighted = 1
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert actions.marked == ['r1@example.org']
        assert '●' not in app.thread_labels()[1]
        await pilot.press('escape', 'escape', 'escape')
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
        app._posts.highlighted = 0
        await pilot.pause()
        assert '●' in app.thread_labels()[1]
        await pilot.press('R')
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert actions.marked == ['r1@example.org']
        assert not any('●' in label for label in app.thread_labels())
        assert app.reading is not None

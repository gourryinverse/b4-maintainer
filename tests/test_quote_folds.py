"""Long quotes in replies fold to their last lines; a click or z unfolds them."""

from textual.app import App
from textual.geometry import Offset
from textual.selection import Selection

from b4_maintainer.reader import collapse_quotes
from b4_maintainer.widgets import TextPane, visible_blocks
from b4_maintainer.workspace import Workspace

LONG = '\n'.join(f'> line {n}' for n in range(1, 21)) + '\n\nReply.'


def shown(blocks, opened=()):
    return '\n'.join(b.text for b in visible_blocks(blocks, set(opened)))


def test_a_long_quote_folds_to_its_last_lines():
    blocks = collapse_quotes(LONG, quote_lines=5)
    text = shown(blocks)
    assert text.startswith('> [15 more quoted lines]\n> line 16\n')
    assert '> line 15\n' not in text and text.endswith('> line 20\n\nReply.')
    fold = next(b.fold for b in blocks if b.fold)
    opened = shown(blocks, {fold})
    assert '> line 1\n' in opened and '> line 20' in opened
    assert opened.startswith('> [hide 15 quoted lines]\n> line 1\n')


def test_a_quote_barely_longer_than_the_setting_shows_whole():
    """Folding one line would cost the marker's line: it shows whole."""
    six = '\n'.join(f'> line {n}' for n in range(1, 7)) + '\nReply.'
    assert shown(collapse_quotes(six, quote_lines=5)) == six
    assert '[2 more quoted lines]' in shown(collapse_quotes(six, quote_lines=4))


def test_each_long_quote_folds_on_its_own():
    text = LONG + '\n' + LONG
    blocks = collapse_quotes(text, quote_lines=5)
    folds = list(dict.fromkeys(b.fold for b in blocks if b.fold))
    assert len(folds) == 2
    one = shown(blocks, {folds[0]})
    assert one.count('[15 more quoted lines]') == 1 and one.count('[hide 15') == 1


def test_the_setting_is_read_from_layout():
    assert Workspace.from_config({}).quote_lines == 8
    assert Workspace.from_config({'layout': {'quote_lines': 3}}).quote_lines == 3
    assert not Workspace.from_config({'layout': {'quote_lines': 3}}).warnings


class Host(App):
    def compose(self):
        yield TextPane(id='pane')


async def test_clicking_the_marker_unfolds_and_folds_again():
    app = Host()
    async with app.run_test(size=(80, 40)) as pilot:
        pane = app.query_one(TextPane)
        pane.set_blocks(collapse_quotes(LONG, quote_lines=5))
        await pilot.pause()
        assert pane.lines[0] == '> [15 more quoted lines]'
        await pilot.click(pane, offset=(3, 0))
        await pilot.pause()
        assert pane.lines[0] == '> [hide 15 quoted lines]'
        assert '> line 1' in pane.text
        await pilot.click(pane, offset=(3, 0))
        await pilot.pause()
        assert pane.lines[0] == '> [15 more quoted lines]'
        # A click on quoted text (not the marker) leaves the fold alone.
        await pilot.click(pane, offset=(3, 2))
        await pilot.pause()
        assert pane.lines[0] == '> [15 more quoted lines]'


async def test_unfolding_keeps_the_place_and_a_selection_is_no_click():
    app = Host()
    prose = '\n'.join(f'prose {n}' for n in range(30))
    text = f'{prose}\n{LONG}\n{prose}'
    async with app.run_test(size=(80, 10)) as pilot:
        pane = app.query_one(TextPane)
        pane.set_blocks(collapse_quotes(text, quote_lines=5))
        await pilot.pause()
        pane.scroll_to(0, 30, animate=False, immediate=True)
        await pilot.pause()
        assert pane.lines[30] == '> [15 more quoted lines]'
        app.screen.selections = {pane: Selection(Offset(0, 30), Offset(4, 30))}
        await pilot.pause()
        pane.click_line(30)  # A click with text selected: the selection wins.
        assert pane.lines[30] == '> [15 more quoted lines]'
        app.screen.selections = {}
        await pilot.click(pane, offset=(2, 0))
        await pilot.pause()
        assert pane.lines[30] == '> [hide 15 quoted lines]'
        assert pane.scroll_offset.y == 30


async def test_folds_open_together():
    app = Host()
    async with app.run_test(size=(80, 40)) as pilot:
        pane = app.query_one(TextPane)
        pane.set_blocks(collapse_quotes(LONG + '\n' + LONG, quote_lines=5))
        await pilot.pause()
        assert pane.has_folds
        pane.set_folds(True)
        await pilot.pause()
        assert 'more quoted lines' not in pane.text
        pane.set_folds(False)
        assert pane.text.count('[15 more quoted lines]') == 2
        pane.set_blocks(collapse_quotes(LONG, quote_lines=5), open_folds=True)
        assert '[hide 15 quoted lines]' in pane.text

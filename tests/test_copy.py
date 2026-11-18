"""Copying text out of one pane: mouse selection, ctrl+c, y, and the clipboard tools."""

import pathlib

from textual.app import App
from textual.geometry import Offset
from textual.selection import Selection

from b4_maintainer import clipboard
from b4_maintainer.widgets import Block, TextPane

TEXT = '\n'.join(f'line {n} of the message' for n in range(40))


class Host(App):
    def compose(self):
        yield TextPane(id='left')
        yield TextPane(id='right')

    CSS = 'Screen { layout: horizontal; } TextPane { width: 1fr; }'


async def test_a_drag_selects_within_one_pane_and_ctrl_c_copies_it():
    app = Host()
    async with app.run_test(size=(100, 20)) as pilot:
        left, right = app.query_one('#left', TextPane), app.query_one('#right', TextPane)
        left.set_text('left side\n' * 5)
        right.set_blocks([Block(TEXT)])
        await pilot.pause()
        x = right.region.x
        await pilot.mouse_down(right, offset=(0, 2))
        await pilot.hover(right, offset=(5, 4))  # The cell under the pointer is in.
        await pilot.mouse_up(right, offset=(5, 4))
        await pilot.pause()
        await pilot.press('ctrl+c')
        assert app.clipboard == 'line 2 of the message\nline 3 of the message\nline 4'
        assert 'left side' not in app.clipboard
        assert x > 0


async def test_a_scrolled_pane_selects_the_lines_shown():
    app = Host()
    async with app.run_test(size=(100, 10)) as pilot:
        right = app.query_one('#right', TextPane)
        right.set_blocks([Block(TEXT)])
        await pilot.pause()
        right.scroll_to(0, 20, animate=False, immediate=True)
        await pilot.pause()
        widget, offset = app.screen.get_widget_and_offset_at(right.region.x + 5, right.region.y)
        assert widget is right
        assert offset == Offset(5, 20)


async def test_selected_text_is_highlighted():
    app = Host()
    async with app.run_test(size=(100, 20)) as pilot:
        right = app.query_one('#right', TextPane)
        right.set_blocks([Block(TEXT)])
        await pilot.pause()
        plain = right.render_line(1)
        app.screen.selections = {right: Selection(Offset(0, 1), Offset(4, 1))}
        await pilot.pause()
        lit = right.render_line(1)
        assert lit.text == plain.text
        assert [s.style for s in lit if s.text.startswith('line')] != [
            s.style for s in plain if s.text.startswith('line')
        ]
        assert right.get_selection(Selection(Offset(0, 1), Offset(4, 1))) == ('line', '\n')


async def test_y_copies_the_selection_or_else_the_whole_pane():
    app = Host()
    async with app.run_test(size=(100, 20)) as pilot:
        right = app.query_one('#right', TextPane)
        right.set_blocks([Block('first\nsecond'), Block('+diff line', wrap=False)])
        right.focus()
        await pilot.pause()
        await pilot.press('y')
        assert app.clipboard == 'first\nsecond\n+diff line'
        app.screen.selections = {right: Selection(Offset(0, 1), Offset(3, 1))}
        await pilot.pause()
        await pilot.press('y')
        assert app.clipboard == 'sec'


def test_the_clipboard_tools_follow_the_session():
    def which(name):
        return f'/usr/bin/{name}' if name in {'wl-copy', 'xclip', 'tmux'} else None

    wayland = {'WAYLAND_DISPLAY': 'wayland-0', 'DISPLAY': ':0'}
    assert clipboard.commands(wayland, which) == [['wl-copy']]
    assert clipboard.commands({'DISPLAY': ':0'}, which) == [['xclip', '-selection', 'clipboard']]
    # tmux keeps a copy in its paste buffer too (prefix + ] pastes it).
    assert clipboard.commands({'TMUX': '/tmp/t,1,0'}, which) == [['tmux', 'load-buffer', '-']]
    assert clipboard.commands({}, which) == []
    assert clipboard.commands(wayland, lambda name: None) == []
    # Configured: that command only.
    custom = {'B4M_CLIPBOARD': 'my-copy --primary', **wayland}
    assert clipboard.commands(custom, which) == [['my-copy', '--primary']]


async def test_the_app_copies_through_the_clipboard_tools(tmp_path: pathlib.Path):
    from b4_maintainer.app import MaintainerApp
    from b4_maintainer.fixtures import synthetic_snapshot

    out = tmp_path / 'copied'
    snapshot = synthetic_snapshot(2)
    app = MaintainerApp(lambda: snapshot, clipboard=[['sh', '-c', f'cat > {out}']])
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.copy_to_clipboard('copied text')
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.clipboard == 'copied text'
        assert out.read_text() == 'copied text'


async def test_a_right_click_copies_the_selection():
    app = Host()
    async with app.run_test(size=(100, 20)) as pilot:
        right = app.query_one('#right', TextPane)
        right.set_blocks([Block(TEXT)])
        await pilot.pause()
        app.screen.selections = {right: Selection(Offset(0, 1), Offset(6, 1))}
        await pilot.pause()
        await pilot.click(right, offset=(3, 1), button=3)
        assert app.clipboard == 'line 1'


async def test_a_right_click_without_a_selection_copies_nothing():
    app = Host()
    async with app.run_test(size=(100, 20)) as pilot:
        right = app.query_one('#right', TextPane)
        right.set_blocks([Block(TEXT)])
        await pilot.pause()
        await pilot.click(right, offset=(3, 1), button=3)
        assert app.clipboard == ''


async def test_the_highlight_keeps_the_text_readable():
    """The selection shades the background; the text keeps its own colour."""
    from rich.style import Style

    app = Host()
    async with app.run_test(size=(100, 20)) as pilot:
        right = app.query_one('#right', TextPane)
        right.set_blocks([Block(TEXT, style=Style(color='green'))])
        await pilot.pause()
        app.screen.selections = {right: Selection(Offset(0, 1), Offset(4, 1))}
        await pilot.pause()
        lit = [s for s in right.render_line(1) if s.text == 'line'][0]
        assert lit.style.color == Style(color='green').color
        assert lit.style.bgcolor is not None

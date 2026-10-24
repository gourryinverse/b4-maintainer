"""Contract: the tmux editor opens, watches, focuses, and closes a pane beside the app."""

import shutil
import subprocess
import time
import uuid

import pytest

from b4_maintainer.adapters.tmux import TmuxEditor

pytestmark = pytest.mark.skipif(shutil.which('tmux') is None, reason='tmux not installed')


@pytest.fixture
def server(tmp_path):
    """A private tmux server with one detached 200-column window; the app's pane."""
    socket = f'b4m-test-{uuid.uuid4().hex[:8]}'
    base = ['tmux', '-L', socket, '-f', '/dev/null']
    pane = subprocess.run(
        [*base, 'new-session', '-d', '-x', '200', '-y', '50', '-P', '-F', '#{pane_id}'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    yield socket, pane, base
    subprocess.run([*base, 'kill-server'], capture_output=True)


def editor_script(tmp_path, body):
    script = tmp_path / 'editor.sh'
    script.write_text(f'#!/bin/sh\n{body}\n')
    script.chmod(0o755)
    return [str(script)]


def wait_for(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_editor_pane_edits_the_file_and_exits(server, tmp_path):
    socket, app_pane, base = server
    draft = tmp_path / 'draft.eml'
    draft.write_text('quote\n')
    command = editor_script(tmp_path, 'sleep 0.3; printf "my reply\\n" >> "$1"')
    editor = TmuxEditor(socket=socket, target=app_pane, command=lambda _: command)
    pane = editor.open(draft, tmp_path)
    assert pane.startswith('%') and pane != app_pane
    assert editor.alive(pane) and editor.runs(pane, draft)
    width = subprocess.run(
        [*base, 'display-message', '-p', '-t', pane, '#{pane_width}'],
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert width == '100'  # The default split.
    assert wait_for(lambda: not editor.alive(pane))
    assert draft.read_text() == 'quote\nmy reply\n'


def test_the_editor_can_stack_under_a_session_pane(server, tmp_path):
    socket, app_pane, base = server
    session = subprocess.run(
        [*base, 'split-window', '-h', '-l', '90', '-t', app_pane, '-P', '-F', '#{pane_id}']
        + ['sleep 30'],
        capture_output=True,
        text=True,
    ).stdout.strip()
    draft = tmp_path / 'draft.eml'
    draft.write_text('quote\n')
    editor = TmuxEditor(
        socket=socket, target=app_pane, command=lambda _: editor_script(tmp_path, 'sleep 30')
    )
    pane = editor.open(draft, tmp_path, below=session)

    def geometry(target):
        return subprocess.run(
            [
                *base,
                'display-message',
                '-p',
                '-t',
                target,
                '#{pane_left} #{pane_top} #{pane_width}',
            ],
            capture_output=True,
            text=True,
        ).stdout.split()

    s_left, s_top, s_width = geometry(session)
    e_left, e_top, e_width = geometry(pane)
    assert (e_left, e_width) == (s_left, s_width) and int(e_top) > int(s_top)  # Underneath.
    editor.close(pane)


def test_close_kills_the_pane_and_focus_selects_it(server, tmp_path):
    socket, app_pane, base = server
    draft = tmp_path / 'it has spaces.eml'
    draft.write_text('quote\n')
    editor = TmuxEditor(
        socket=socket, target=app_pane, command=lambda _: editor_script(tmp_path, 'sleep 30')
    )
    pane = editor.open(draft, tmp_path)
    assert editor.runs(pane, draft)
    editor.focus(pane)
    active = subprocess.run(
        [*base, 'display-message', '-p', '#{pane_id}'], capture_output=True, text=True
    ).stdout.strip()
    assert active == pane
    editor.close(pane)
    assert not editor.alive(pane)
    assert not editor.runs(pane, draft)
    editor.close(pane)  # Closing a gone pane is harmless.


def test_failures_raise_editor_errors(tmp_path):
    from b4_maintainer.adapters.tmux import EditorError

    editor = TmuxEditor(socket=f'b4m-none-{uuid.uuid4().hex[:8]}', target='%0')
    with pytest.raises(EditorError):
        editor.open(tmp_path / 'x.eml', tmp_path)
    assert not editor.alive('%0')


async def test_app_drafts_in_a_real_tmux_pane_through_to_b4s_preview(server, tmp_path, demo_root):
    from test_draft import DraftActions, reading, settle

    from b4_maintainer.adapters.drafts import DraftStore
    from b4_maintainer.app import MaintainerApp

    socket, app_pane, _ = server
    command = editor_script(tmp_path, 'sleep 0.5; printf "\\nReviewed.\\n" >> "$1"')
    editor = TmuxEditor(socket=socket, target=app_pane, command=lambda _: command)
    actions = DraftActions(demo_root)
    store = DraftStore(tmp_path / 'drafts')
    app = MaintainerApp(actions.load, actions=actions, drafts=store, editor=editor)
    async with app.run_test(size=(180, 40)) as pilot:
        await reading(pilot)
        await pilot.press('r')
        await settle(pilot)
        msgid = app.draft_msgid()
        assert msgid and editor.alive(store.get(msgid).pane)
        assert wait_for(lambda: not editor.alive(store.get(msgid).pane))
        await pilot.pause(1.5)
        await settle(pilot)
        assert type(app.screen).__name__ == 'FollowupReplyPreviewScreen'
        assert store.get(msgid).text.endswith('Reviewed.\n')


@pytest.mark.skipif(shutil.which('vim') is None, reason='vim not installed')
def test_save_and_close_makes_vim_write_and_exit(server, tmp_path):
    socket, app_pane, base = server
    draft = tmp_path / 'draft.eml'
    draft.write_text('quote\n')
    vim = ['vim', '-u', 'NONE', '-N', '-n']
    editor = TmuxEditor(socket=socket, target=app_pane, command=lambda _: vim)
    pane = editor.open(draft, tmp_path)
    assert wait_for(
        lambda: (
            'vim'
            in subprocess.run(
                [*base, 'display-message', '-p', '-t', pane, '#{pane_current_command}'],
                capture_output=True,
                text=True,
            ).stdout
        )
    )
    subprocess.run(
        [*base, 'send-keys', '-t', pane, 'G', 'o', 'unsaved words', 'Escape', 'i'], check=True
    )
    assert editor.save_and_close(pane)  # Even from insert mode.
    assert wait_for(lambda: not editor.alive(pane))
    assert draft.read_text() == 'quote\nunsaved words\n'


def test_save_and_close_leaves_other_editors_alone(server, tmp_path):
    socket, app_pane, base = server
    draft = tmp_path / 'draft.eml'
    draft.write_text('quote\n')
    editor = TmuxEditor(
        socket=socket, target=app_pane, command=lambda _: editor_script(tmp_path, 'sleep 30')
    )
    pane = editor.open(draft, tmp_path)
    assert not editor.save_and_close(pane)
    assert editor.alive(pane)

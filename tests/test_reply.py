"""Contract: replying runs b4's own reply flow (editor, preview, send) from the reader."""

import contextlib
import datetime
from unittest import mock

import b4
from gitutil import git

from b4_maintainer.adapters.b4state import b4_repo, load_lore_message, mark_answered
from b4_maintainer.adapters.live import LiveSource
from b4_maintainer.app import MaintainerApp, SeriesList
from b4_maintainer.cli import project_main
from b4_maintainer.fixtures import PatchSpec, write_series_mbox

NOW = datetime.datetime.now(datetime.timezone.utc)
MM = 'linux-mm.kvack.org'
PROFILE = (
    '\n[[profiles]]\nid = "mm"\nlabel = "MM"\nproject = "demo"\nlists = ["linux-mm@kvack.org"]\n'
)


def workspace(ws, tmp_path):
    """An enrolled live workspace with one cached series; no network."""
    assert project_main(['add', '-C', str(ws), 'demo', '--list', 'linux-mm@kvack.org']) == 0
    with open(ws / 'workspace.toml', 'a') as fh:
        fh.write(PROFILE)
    checkout = ws / 'projects' / 'demo'
    git('config', 'user.name', 'Example Maintainer', cwd=checkout)
    git('config', 'user.email', 'maintainer@example.org', cwd=checkout)
    path = tmp_path / 'src' / 'series.mbx'
    path.parent.mkdir()
    sent = write_series_mbox(
        path, 'mm: answer me', patches=[PatchSpec('mm/a.c'), PatchSpec('mm/b.c')], date=NOW
    )
    msgs = b4.get_msgs_from_mailbox_or_maildir(str(path))
    source = LiveSource(ws, search_for=lambda checkout: lambda query: msgs if 'l:' in query else [])
    source.refresh()
    return source, sent, checkout


def test_load_lore_message_and_answered_flag(ws, tmp_path):
    from b4.review import messages

    source, sent, checkout = workspace(ws, tmp_path)
    [item] = source.load().items
    lmsg = load_lore_message(item.sources, sent.msgids[1])
    assert lmsg.msgid == sent.msgids[1]
    mark_answered(checkout, sent.msgids[1], None)
    with b4_repo(checkout):
        conn = messages.get_db()
        assert 'Answered' in messages.get_flags(conn, sent.msgids[1]).split()
        conn.close()


async def test_draft_replies_through_b4_preview_and_send(ws, tmp_path):
    source, sent, checkout = workspace(ws, tmp_path)
    source.email_dryrun = True
    app = MaintainerApp(source.load, actions=source)
    outgoing = []

    def fake_send(smtp, msgs, **kwargs):
        outgoing.extend(msgs)
        assert kwargs['dryrun'] is True
        return len(msgs)

    async with app.run_test(size=(180, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.query_one(SeriesList).highlighted_item.series.subject == 'mm: answer me'
        await pilot.press('enter')
        await app.workers.wait_for_complete()
        await pilot.pause()
        app.query_one('#tree').highlighted = 1  # patch 1/2
        await pilot.pause()
        bindings = {k: b.binding.description for k, b in app.screen.active_bindings.items()}
        assert bindings['r'] == 'reply'
        await pilot.press('r')
        await app.workers.wait_for_complete()
        await pilot.pause()
        draft = app.query_one('#draft')
        assert app.focused is draft
        draft.insert('Looks good.\n')
        await pilot.press('ctrl+s')
        await pilot.pause()
        assert type(app.screen).__name__ == 'FollowupReplyPreviewScreen'
        with (
            # Headless test apps cannot drop to the terminal; b4's own tests stub this too.
            mock.patch.object(app, 'suspend', contextlib.nullcontext),
            mock.patch('b4.get_smtp', return_value=(None, 'maintainer@example.org')),
            mock.patch('b4.send_mail', side_effect=fake_send),
        ):
            await pilot.press('ctrl+y')
            await app.workers.wait_for_complete()
            await pilot.pause()

    [msg] = outgoing
    assert msg['In-Reply-To'] == f'<{sent.msgids[1]}>'
    assert msg['Subject'].startswith('Re: ')
    assert 'maintainer@example.org' in str(msg['From'])
    assert 'Looks good.' in msg.get_content()
    from b4.review import messages

    with b4_repo(checkout):
        conn = messages.get_db()
        assert 'Answered' in messages.get_flags(conn, sent.msgids[1]).split()
        conn.close()

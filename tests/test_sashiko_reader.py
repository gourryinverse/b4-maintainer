"""Contract: the reader shows Sashiko reviews under their patches and marks findings in diffs."""

import dataclasses

from test_draft import DraftActions, settle
from test_reader import loaded, open_reader

from b4_maintainer.app import MaintainerApp
from b4_maintainer.fixtures import make_item, workspace_config
from b4_maintainer.model import (
    Finding,
    Message,
    Post,
    Reply,
    SashikoPatch,
    SashikoReview,
    SeriesThread,
)
from b4_maintainer.reader import (
    finding_anchors,
    root_blocks,
    tree_rows,
    with_sashiko,
)
from b4_maintainer.workspace import Snapshot, Workspace

BOTS = ('sashiko-bot@kernel.org',)
DIFF = """\
diff --git a/mm/a.c b/mm/a.c
--- a/mm/a.c
+++ b/mm/a.c
@@ -8,4 +8,5 @@ void foo(void)
 \tint a;
 \tint b;
+\tleak();
 \treturn;
 }"""
LEAK = Finding('high', 'mm: leaks foo', 'Memory is lost.', locations=(('mm/a.c', 10),))
ELSEWHERE = Finding('low', 'mm: old bug', preexisting=True, locations=(('mm/b.c', 99),))
REVIEW = SashikoReview(
    'Reviewed',
    high=1,
    url='https://sashiko.example.org/#/patchset/7',
    patches=(
        SashikoPatch('p1@example.org', 1, 'Reviewed', (LEAK,), '> +\tleak();\n\nThis leaks.\n'),
        SashikoPatch('p2@example.org', 2, 'Reviewed', (ELSEWHERE,), 'Review of part 2.\n'),
    ),
)


def message(msgid, author='Contributor One', email='contributor1@example.org', body='Body.'):
    return Message(msgid, author, email, None, f'subject {msgid}', body)


def thread(item, bot_reply_on_patch=2):
    bot = Reply(message('bot@example.org', '', BOTS[0], 'Sashiko found 1 issue.'), 0)
    human = Reply(message('r@example.org', 'Reviewer One', 'reviewer1@example.org'), 0)
    posts = (
        Post(1, message('p1@example.org'), 'First.', DIFF, (human,)),
        Post(2, message('p2@example.org'), 'Second.', DIFF, (bot,) if bot_reply_on_patch else ()),
        Post(3, message('p3@example.org'), 'Third.', DIFF, ()),
    )
    return SeriesThread(item.identity, 1, None, posts)


def reviewed_item(**kwargs):
    return dataclasses.replace(make_item(subject='mm: reviewed'), sashiko=REVIEW, **kwargs)


def test_api_reviews_join_patches_without_sashiko_mail():
    item = reviewed_item()
    merged = with_sashiko(thread(item), item.sashiko, BOTS)
    first, second, third = merged.patches
    assert [r.message.msgid for r in first.replies] == ['r@example.org', 'sashiko:p1@example.org']
    assert first.replies[1].message.body == '> +\tleak();\n\nThis leaks.\n'
    # Sashiko's own mail is already there: never both.
    assert [r.message.msgid for r in second.replies] == ['bot@example.org']
    assert third.replies == ()
    assert with_sashiko(thread(item), None, BOTS) == thread(item)


def test_tree_marks_findings_and_sashiko_replies():
    item = reviewed_item()
    merged = with_sashiko(thread(item), item.sashiko, BOTS)
    labels = [r.label for r in tree_rows(merged, item, {'1', '2'}, bots=BOTS)]
    assert labels[0].startswith('▾ 1/1') and labels[0].endswith(' 1!')
    assert labels[2].strip().startswith('└─> ◆ Sashiko (not on list)')
    assert '1!' not in labels[3]  # Pre-existing findings are not counted.
    assert labels[4].strip().startswith('└─> ◆ Sashiko')
    assert 'not on list' not in labels[4]


def text_of(blocks):
    return '\n'.join(b.text for b in blocks)


def test_patch_content_lists_findings_and_marks_located_diff_lines():
    item = reviewed_item()
    blocks = root_blocks(thread(item), '1', item)
    text = text_of(blocks)
    assert 'Sashiko: 1 finding · https://sashiko.example.org/#/patchset/7' in text
    assert 'high  mm/a.c:10  mm: leaks foo' in text
    lines = text.split('\n')
    marked = lines.index('+\tleak();')
    assert lines[marked + 1].startswith('▲ high: mm: leaks foo')
    # One anchor per finding: its diff mark when located in the diff.
    [anchored] = [b for b in blocks if b.anchor]
    assert anchored.anchor == 'finding-1-1' and anchored.text.startswith('▲')

    # A finding located outside the diff anchors at its list entry.
    other = root_blocks(thread(item), '2', item)
    assert 'low (pre-existing)  mm/b.c:99  mm: old bug' in text_of(other)
    assert '▲' not in text_of(other)
    [anchored] = [b for b in other if b.anchor]
    assert 'mm/b.c:99' in anchored.text


def test_finding_anchors_run_in_series_order():
    item = reviewed_item()
    assert finding_anchors(thread(item), item) == [('1', 'finding-1-1'), ('2', 'finding-2-1')]


def reader_app(actions=None):
    item = reviewed_item()
    snapshot = Snapshot(Workspace.from_config(workspace_config()), (item,))
    load = (lambda: snapshot) if actions is None else actions.load
    app = MaintainerApp(load, actions=actions, thread_loader=lambda s: thread(item))
    return item, app


async def test_bang_jumps_to_each_finding_in_turn():
    _, app = reader_app()
    async with app.run_test(size=(180, 16)) as pilot:
        await loaded(pilot)
        await open_reader(pilot)
        await pilot.press('exclamation_mark')
        await pilot.pause()
        assert app.tree_position() == ('1', 0)
        content = app.query_one('#content')
        top, height = content.scroll_offset.y, content.scrollable_content_region.height
        mark = next(n for n, line in enumerate(app.content_lines()) if line.startswith('▲ high'))
        assert top > 0 and top <= mark < top + height
        assert app.content_lines()[mark - 1] == '+       leak();'  # The marked code line.
        await pilot.press('exclamation_mark')
        await pilot.pause()
        assert app.tree_position() == ('2', 0)
        await pilot.press('exclamation_mark')
        await pilot.pause()
        assert app.tree_position() == ('2', 0)
        assert 'No more findings' in app.status_text()


class ReplyActions(DraftActions):
    def __init__(self, item):
        self.item = item
        self.answered_calls = []
        self.prepared = []

    def load(self):
        return Snapshot(Workspace.from_config(workspace_config()), (self.item,))

    def prepare_reply(self, item, msgid):
        self.prepared.append(msgid)
        return super().prepare_reply(item, msgid)


async def test_replying_to_an_api_review_answers_the_patch_quoting_sashiko():
    item = reviewed_item()
    actions = ReplyActions(item)
    app = MaintainerApp(actions.load, actions=actions, thread_loader=lambda s: thread(item))
    async with app.run_test(size=(180, 30)) as pilot:
        await loaded(pilot)
        await open_reader(pilot)
        await pilot.press('minus')  # Expand patch 1: human reply, then Sashiko's.
        await pilot.press('down', 'down')
        await pilot.pause()
        assert app.tree_position() == ('1', 2)
        await pilot.press('r')
        await settle(pilot)
        assert actions.prepared == ['p1@example.org']
        text = app.query_one('#draft').text
        assert text.startswith('Sashiko (https://sashiko.example.org/#/patchset/7) reported:\n')
        assert '> This leaks.' in text

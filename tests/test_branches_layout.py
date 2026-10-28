"""Contract: Branches lists each project in headed sections (Bases, Rebase and apply
sessions, Prep, Session work, Review, Other, Checkouts) that fold: left folds (from an
item, its section), right unfolds, Enter toggles, and folds survive a reload. The panes
are titled; the footer shows the keys for the selected kind. b makes a plain branch at a
chosen start to pluck onto."""

from test_branch_screen import FakeBranches, make_app, settle

from b4_maintainer.branches import BranchItem, CutResult, DetailRow
from b4_maintainer.branchscreen import Ask, BranchesScreen, Pick


class SectionBranches(FakeBranches):
    def __init__(self):
        super().__init__()
        self.made = []

    def snapshot(self, workspace, trees=True):
        data = super().snapshot(workspace, trees)
        work = BranchItem('work:s2:b4m/fixup/lock', 'work', 'linux', 's2 fixup/lock · 1 commit')
        other = BranchItem('branch:linux:try', 'branch', 'linux', 'try · 0 commits not upstream')
        items = (*data.items, work, other)
        details = {
            **data.details,
            work.key: (DetailRow('fixup b4m/fixup/lock'), DetailRow('  f1  mm: lock', 'f1', '/t')),
        }
        return type(data)(
            items,
            details,
            data.statuses,
            rebases=data.rebases,
            targets={**data.targets, work.key: ('base:linux:me/base', 'branch:linux:try')},
            tips=data.tips,
            identities=data.identities,
            preps=data.preps,
        )

    def branch_starts(self, workspace, project):
        return ['me/base', 'up/stable']

    def new_branch(self, workspace, project, name, start):
        self.made.append((project, name, start))
        return CutResult(True, name)


async def open_branches(pilot):
    await settle(pilot)
    await pilot.press('f4')
    await settle(pilot)
    screen = pilot.app.screen
    assert isinstance(screen, BranchesScreen)
    return screen


def footer_keys(screen):
    return {k for k, a in screen.active_bindings.items() if a.binding.show and a.enabled}


async def test_projects_are_listed_in_titled_sections(demo_root):
    app = make_app(demo_root, SectionBranches())
    async with app.run_test(size=(180, 40)) as pilot:
        screen = await open_branches(pilot)
        assert [label.strip() for label in screen.item_labels()] == ['▸ linux']  # Folded.
        await pilot.press('right')  # The project: its sections, still folded.
        await settle(pilot)
        labels = [label.strip() for label in screen.item_labels()]
        assert labels[0] == '▾ linux'
        for heading in (
            '▸ Bases (1)',
            '▸ Rebase and apply sessions (1)',
            '▸ Prep (1)',
            '▸ Session work (1)',
            '▸ Other (1)',
        ):
            assert heading in labels, heading
        assert labels.index('▸ Prep (1)') < labels.index('▸ Session work (1)')
        screen.select_item('base:linux:me/base')  # Selecting unfolds its section.
        await settle(pilot)
        assert '▾ Bases (1)' in [label.strip() for label in screen.item_labels()]
        titles = [str(screen.query_one(f'#{p}').border_title) for p in ('branch-items',)]
        assert titles == ['Branches']
        assert 'me/base' in str(screen.query_one('#branch-rows').border_title)


async def test_sections_fold_and_stay_folded_across_a_reload(demo_root):
    service = SectionBranches()
    app = make_app(demo_root, service)
    async with app.run_test(size=(180, 40)) as pilot:
        screen = await open_branches(pilot)
        screen.select_item('prep:linux:b4/pa')
        await settle(pilot)
        await pilot.press('left')  # From a branch: its section folds; the cursor on it.
        await settle(pilot)
        assert '▸ Prep (1)' in [label.strip() for label in screen.item_labels()]
        assert 'prep:linux:b4/pa' not in screen.item_keys()
        assert screen.selected_item() is None and screen.selected_heading() == 'linux:prep'
        await pilot.press('f5')
        await settle(pilot)
        assert 'prep:linux:b4/pa' not in screen.item_keys()  # Still folded.
        await pilot.press('right')
        await settle(pilot)
        assert 'prep:linux:b4/pa' in screen.item_keys()
        await pilot.press('enter')  # Enter toggles.
        await settle(pilot)
        assert 'prep:linux:b4/pa' not in screen.item_keys()
        screen.select_heading('linux')
        await pilot.press('left')  # A project folds whole.
        await settle(pilot)
        assert screen.item_keys() == []


async def test_the_footer_shows_the_selected_kinds_keys(demo_root):
    app = make_app(demo_root, SectionBranches())
    async with app.run_test(size=(180, 40)) as pilot:
        screen = await open_branches(pilot)
        screen.select_item('base:linux:me/base')
        await settle(pilot)
        keys = footer_keys(screen)
        assert 'r' in keys and 'S' not in keys  # A base: rebase, not send.
        screen.select_item('prep:linux:b4/pa')
        await settle(pilot)
        keys = footer_keys(screen)
        assert {'S', 'e', 's'} <= keys and 'r' not in keys
        screen.select_heading('linux:prep')
        await settle(pilot)
        assert not {'r', 'S', 'p'} & footer_keys(screen)


async def test_b_makes_a_branch_to_pluck_onto(demo_root):
    service = SectionBranches()
    app = make_app(demo_root, service)
    async with app.run_test(size=(180, 40)) as pilot:
        screen = await open_branches(pilot)
        screen.select_item('work:s2:b4m/fixup/lock')
        await settle(pilot)
        assert 'b' in footer_keys(screen)
        await pilot.press('b')
        await settle(pilot)
        assert isinstance(app.screen, Ask)
        app.screen.query_one('Input').value = 'try-lock'
        await pilot.press('enter')
        await settle(pilot)
        pick = app.screen
        assert isinstance(pick, Pick) and [k for k, _ in pick.choices][:2] == [
            'me/base',
            'up/stable',
        ]
        await pilot.press('down', 'enter')
        await settle(pilot)
        assert service.made == [('linux', 'try-lock', 'up/stable')]
        assert 'Made try-lock' in screen.status_text()


class FlaggedBranches(SectionBranches):
    def snapshot(self, workspace, trees=True):
        import dataclasses

        data = super().snapshot(workspace, trees)
        items = tuple(
            dataclasses.replace(
                i,
                label='me/base · 2 commits',
                info=('On up/stable', '2 behind', 'Checked out in ~/linux'),
                flagged=True,
            )
            if i.kind == 'base'
            else i
            for i in data.items
        )
        return dataclasses.replace(data, items=items)


async def test_details_sit_under_the_list_and_a_star_marks_special_conditions(demo_root):
    app = make_app(demo_root, FlaggedBranches())
    async with app.run_test(size=(180, 40)) as pilot:
        screen = await open_branches(pilot)
        screen.select_item('base:linux:me/base')
        await settle(pilot)
        labels = [label.strip() for label in screen.item_labels()]
        assert 'me/base · 2 commits *' in labels  # Short, marked; no wrapping details.
        assert str(screen.query_one('#branch-info').border_title) == 'Details'
        details = screen.details_text()
        assert 'Checked out in ~/linux' in details and '2 behind' in details
        screen.select_item('prep:linux:b4/pa')
        await settle(pilot)
        assert 'Checked out' not in screen.details_text()
        assert 'b4/pa' in screen.details_text()


class MailBranches(SectionBranches):
    """A fixup and a pull-ahead in session s2's tree; messages built from scripted text."""

    def __init__(self):
        super().__init__()
        self.built = []

    def snapshot(self, workspace, trees=True):
        import dataclasses

        data = super().snapshot(workspace, trees)
        pull = BranchItem('work:s2:b4m/pull-ahead/old', 'work', 'linux', 's2 pull-ahead/old · 1')
        details = {
            **data.details,
            'work:s2:b4m/fixup/lock': (
                DetailRow('fixup b4m/fixup/lock'),
                DetailRow('  f1  mm: lock', 'f1', '/t', branch='b4m/fixup/lock'),
            ),
            pull.key: (
                DetailRow('pull-ahead b4m/pull-ahead/old'),
                DetailRow('  o1  mm: old bug', 'o1', '/t', branch='b4m/pull-ahead/old'),
            ),
        }
        return dataclasses.replace(data, items=(*data.items, pull), details=details)

    def fixup_target(self, workspace, item, branch):
        return 'change-id:foo', 3

    def patch_message(self, workspace, item, row, reply):
        self.built.append((row.key, reply))
        where = f'In-Reply-To: {reply.msgid}\n' if reply else ''
        to = 'Contributor <c@example.org>' if reply else 'Maintainer <sub@example.org>'
        return f'From: Me <me@example.org>\nTo: {to}\nSubject: [PATCH] mm: x\n{where}\nbody\n'


def mail_app(demo_root, service, edited=None):
    from b4_maintainer.patchmail import ReplyTo

    app = make_app(demo_root, service)
    sent = []
    app.branch_mail_reply = lambda identity, counter: ReplyTo(
        '<p3@example.org>', '<p3@example.org>', (('C', 'c@example.org'),), ()
    )
    app.edit_message = lambda text: edited if edited is not None else text
    app.send_message = lambda project, text: sent.append((project, text)) or 'Sent'
    return app, sent


async def test_m_sends_a_fixup_as_a_reply_after_editing_and_a_confirmation(demo_root):
    from b4_maintainer.app import Confirm

    service = MailBranches()
    app, sent = mail_app(demo_root, service, edited=None)
    async with app.run_test(size=(180, 40)) as pilot:
        screen = await open_branches(pilot)
        screen.select_item('work:s2:b4m/fixup/lock')
        await settle(pilot)
        assert 'm' in footer_keys(screen)
        await pilot.press('m')
        await settle(pilot)
        confirm = app.screen
        assert isinstance(confirm, Confirm)
        assert 'In reply to <p3@example.org>' in confirm.prompt
        assert 'To: Contributor <c@example.org>' in confirm.prompt
        assert 'Signed-off-by' in confirm.prompt  # The DCO: said before it goes.
        await pilot.press('y')
        await settle(pilot)
        assert [(p, 'In-Reply-To: <p3@example.org>' in t) for p, t in sent] == [('linux', True)]
        assert service.built[0][0] == 'f1' and service.built[0][1].msgid == '<p3@example.org>'
        assert 'Sent' in screen.status_text()


async def test_m_sends_a_pull_ahead_as_its_own_thread(demo_root):
    service = MailBranches()
    edited = 'From: Me <me@example.org>\nTo: Maintainer <sub@example.org>\nSubject: [PATCH] mm: y\n\nedited\n'
    app, sent = mail_app(demo_root, service, edited=edited)
    async with app.run_test(size=(180, 40)) as pilot:
        screen = await open_branches(pilot)
        screen.select_item('work:s2:b4m/pull-ahead/old')
        await settle(pilot)
        await pilot.press('m')
        await settle(pilot)
        assert 'new thread' in app.screen.prompt and '[PATCH] mm: y' in app.screen.prompt
        await pilot.press('y')
        await settle(pilot)
        assert sent == [('linux', edited)]  # What was saved in the editor goes.
        assert service.built == [('o1', None)]


async def test_a_fixup_replies_to_its_patch_with_its_recipients(demo_root):
    import email.message
    from types import SimpleNamespace

    from b4_maintainer.adapters.fixture import FixtureSource
    from b4_maintainer.app import MaintainerApp

    class Actions:
        def __init__(self):
            self.asked = []

        def prepare_reply(self, item, msgid):
            self.asked.append(msgid)
            msg = email.message.EmailMessage()
            msg['From'] = 'Contributor One <c1@example.org>'
            msg['To'] = 'Linux MM <linux-mm@kvack.org>'
            msg['Cc'] = 'Reviewer <r@example.org>, Contributor One <c1@example.org>'
            msg['References'] = '<cover@example.org>'
            return SimpleNamespace(lmsg=SimpleNamespace(msg=msg))

    source = FixtureSource(demo_root)
    actions = Actions()
    app = MaintainerApp(source.load, actions=actions)
    async with app.run_test(size=(180, 40)) as pilot:
        await settle(pilot)
        item = next(i for i in app.snapshot.items if len(i.series.patches) >= 2)
        third = item.series.patches[1]
        reply = app.branch_mail_reply(item.identity, third.counter)
        assert actions.asked == [third.msgid]
        assert reply.msgid == f'<{third.msgid}>'
        assert reply.references.split()[-1] == f'<{third.msgid}>'
        assert reply.to == (('Contributor One', 'c1@example.org'),)
        assert ('Reviewer', 'r@example.org') in reply.cc
        assert all(a[1] != 'c1@example.org' for a in reply.cc)
        cover = app.branch_mail_reply(item.identity, None)  # No patch named: the cover.
        assert cover.msgid == f'<{item.series.msgids[0]}>'


async def test_n_offers_its_fork_points_and_any_ref_in_a_readable_picker(demo_root):
    service = SectionBranches()
    service.fork_points = lambda workspace, item: []  # None known: a ref is typed.
    app = make_app(demo_root, service)
    async with app.run_test(size=(180, 40)) as pilot:
        screen = await open_branches(pilot)
        screen.select_item('work:s2:b4m/fixup/lock')
        await settle(pilot)
        await pilot.press('tab', 'n')  # The commits pane: the selected commit.
        await settle(pilot)
        app.screen.query_one('Input').value = 'lock-fix'
        await pilot.press('enter')
        await settle(pilot)
        assert isinstance(app.screen, Ask) and 'lock-fix' in app.screen.title_text  # Typed.

    service = SectionBranches()
    app = make_app(demo_root, service)
    async with app.run_test(size=(180, 40)) as pilot:
        screen = await open_branches(pilot)
        screen.select_item('work:s2:b4m/fixup/lock')
        await settle(pilot)
        await pilot.press('tab', 'n')
        await settle(pilot)
        app.screen.query_one('Input').value = 'lock-fix'
        await pilot.press('enter')
        await settle(pilot)
        pick = app.screen
        assert isinstance(pick, Pick)
        keys = [k for k, _label in pick.choices]
        assert keys[:3] == ['up/stable', 'me/base', 'mm/mm-new'] and keys[-1] == '\0typed'
        assert pick.query_one('Vertical').size.width >= 40  # Not squeezed.


async def test_a_message_without_recipients_is_not_sent(demo_root):
    service = MailBranches()
    edited = 'From: Me <me@example.org>\nSubject: [PATCH] mm: y\n\nedited\n'
    app, sent = mail_app(demo_root, service, edited=edited)
    async with app.run_test(size=(180, 40)) as pilot:
        screen = await open_branches(pilot)
        screen.select_item('work:s2:b4m/pull-ahead/old')
        await settle(pilot)
        await pilot.press('m')
        await settle(pilot)
        assert isinstance(app.screen, BranchesScreen) and sent == []
        assert 'no recipients' in screen.status_text()

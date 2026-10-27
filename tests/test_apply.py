"""Contract: a posted series (or some of its patches) goes onto a maintainer base or a prep
branch through b4: authors kept, trailers from the list and a Link added, the branch moved
in one guarded update; the project's HEAD, index, FETCH_HEAD, and worktrees untouched."""

import email.utils
import re
import subprocess

import pytest
from gitutil import git

from b4_maintainer.adapters.apply import apply_series, series_patches

CHANGE_ID = '20261027-mm-foo-0123456789ab'


def write(repo, name, text):
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    git('add', name, cwd=repo)


def commit(repo, message, author='Example Maintainer <maintainer@example.org>'):
    git('commit', '-q', f'--author={author}', '-m', message, cwd=repo)
    return git('rev-parse', 'HEAD', cwd=repo)


def msgid(patch: str) -> str:
    return re.search(r'^Message-ID: <([^>]+)>', patch, re.M | re.I).group(1)


@pytest.fixture
def posted(tmp_path, home, monkeypatch):
    """A project with a maintainer base, and a v2 series against it as one mbox with a
    reviewer's reply to its first patch."""
    (home / '.gitconfig').write_text(
        '[user]\n\tname = Example Maintainer\n\temail = maintainer@example.org\n'
    )
    project = tmp_path / 'projects' / 'demo'
    project.mkdir(parents=True)
    git('init', '-q', '-b', 'master', cwd=project)
    git('config', 'b4.attestation-policy', 'off', cwd=project)  # Offline.
    monkeypatch.setattr('b4.can_network', False)  # No lore search for more trailers.
    write(project, 'mm/foo.c', ''.join(f'line {n}\n' for n in range(1, 21)))
    base = commit(project, 'mm: start')
    git('branch', 'me/base', cwd=project)
    git('checkout', '-q', '-b', 'series', cwd=project)
    contributor = 'Con Tributor <con@example.org>'
    write(project, 'mm/foo.c', (project / 'mm/foo.c').read_text().replace('line 3\n', 'three\n'))
    commit(project, 'mm/foo: name line three\n\nWords for line three.', contributor)
    write(project, 'mm/bar.c', 'bar\n')
    commit(project, 'mm/bar: add bar\n\nA new file.', contributor)
    out = tmp_path / 'patches'
    options = ['-q', '-v2', '--cover-letter', '--thread', f'--base={base}', '-o', str(out)]
    git('format-patch', *options, f'{base}..series', cwd=project)
    patches = [p.read_text() for p in sorted(out.glob('*.patch'))]
    patches[0] = (
        patches[0]
        .replace('*** SUBJECT HERE ***', 'mm: foo and bar')
        .replace('*** BLURB HERE ***', f'Two changes.\n\n---\nchange-id: {CHANGE_ID}')
    )
    cover, first = msgid(patches[0]), msgid(patches[1])
    reply = (
        'From reviewer@example.org Mon Sep 17 00:00:00 2001\n'
        'From: Rev Iewer <reviewer@example.org>\n'
        'To: con@example.org\n'
        'Subject: Re: [PATCH v2 1/2] mm/foo: name line three\n'
        f'Date: {email.utils.formatdate()}\n'
        'Message-ID: <reply1@example.org>\n'
        f'In-Reply-To: <{first}>\n'
        f'References: <{cover}> <{first}>\n'
        '\n'
        '> Words for line three.\n'
        '\n'
        'Reviewed-by: Rev Iewer <reviewer@example.org>\n'
    )
    mbox = tmp_path / 'series.mbx'
    mbox.write_text(''.join(patches) + reply)
    git('checkout', '-q', 'master', cwd=project)
    git('branch', '-q', '-D', 'series', cwd=project)
    return project, base, mbox, first


def untouched(project):
    """What applying must never change in the project, apart from the target branch."""
    gitdir = project / '.git'
    fetch_head = gitdir / 'FETCH_HEAD'
    return {
        'head': git('rev-parse', 'HEAD', cwd=project),
        'symref': git('symbolic-ref', '-q', 'HEAD', cwd=project),
        'index': (gitdir / 'index').read_bytes(),
        'worktrees': git('worktree', 'list', '--porcelain', cwd=project),
        'fetch_head': fetch_head.read_text() if fetch_head.exists() else None,
        'status': git('status', '--porcelain', cwd=project),
        'others': git('for-each-ref', '--exclude=refs/heads/me/base', cwd=project),
    }


def test_the_series_patches_are_listed_by_number(posted):
    project, _base, mbox, _first = posted
    patches = series_patches(project, str(mbox), 2)
    assert [(p.number, p.subject) for p in patches] == [
        (1, 'mm/foo: name line three'),
        (2, 'mm/bar: add bar'),
    ]
    assert patches[0].author == 'Con Tributor <con@example.org>'


def test_a_series_lands_on_the_base_with_list_trailers_and_a_link(posted):
    project, base, mbox, first = posted
    (project / '.git' / 'FETCH_HEAD').write_text('mine\n')
    before = untouched(project)
    result = apply_series(project, 'me/base', str(mbox), 2, [1, 2], base)
    assert result.ok, result.error
    assert untouched(project) == before
    tip = git('rev-parse', 'me/base', cwd=project)
    assert result.tip == tip and len(result.applied) == 2
    log = git('log', '--reverse', '--format=%s|%an <%ae>|%cn', f'{base}..me/base', cwd=project)
    assert log.splitlines() == [
        'mm/foo: name line three|Con Tributor <con@example.org>|Example Maintainer',
        'mm/bar: add bar|Con Tributor <con@example.org>|Example Maintainer',
    ]
    message = git('log', '-1', '--format=%B', 'me/base~1', cwd=project)
    assert 'Reviewed-by: Rev Iewer <reviewer@example.org>' in message
    assert f'Link: https://patch.msgid.link/{first}' in message
    assert 'Signed-off-by: Example Maintainer' not in message  # Not asked for.


def test_some_patches_alone_can_be_picked(posted):
    project, base, mbox, _first = posted
    result = apply_series(project, 'me/base', str(mbox), 2, [2], base)
    assert result.ok, result.error
    log = git('log', '--format=%s', f'{base}..me/base', cwd=project)
    assert log.splitlines() == ['mm/bar: add bar']


def test_a_conflict_changes_nothing_and_says_why(posted):
    project, _base, mbox, _first = posted
    git('checkout', '-q', 'me/base', cwd=project)
    write(project, 'mm/foo.c', (project / 'mm/foo.c').read_text().replace('line 3\n', 'tres\n'))
    tip = commit(project, 'mm/foo: conflicting change')
    git('checkout', '-q', 'master', cwd=project)
    before = untouched(project)
    result = apply_series(project, 'me/base', str(mbox), 2, [1, 2], tip)
    assert not result.ok and 'does not apply' in result.error
    assert 'mm/foo' in result.output
    assert git('rev-parse', 'me/base', cwd=project) == tip
    assert untouched(project) == before  # No scratch worktree left behind.


def test_a_branch_checked_out_or_moved_is_refused(posted):
    project, base, mbox, _first = posted
    moved = apply_series(project, 'me/base', str(mbox), 2, [1], 'f' * 40)
    assert not moved.ok and 'moved' in moved.error
    git('checkout', '-q', 'me/base', cwd=project)
    out = apply_series(project, 'me/base', str(mbox), 2, [1], base)
    assert not out.ok and 'checked out' in out.error
    missing = apply_series(project, 'me/nope', str(mbox), 2, [1], base)
    assert not missing.ok and 'does not exist' in missing.error


def test_the_service_offers_the_projects_bases_and_prep_branches(posted, tmp_path):
    from b4_maintainer.branches import BranchService
    from b4_maintainer.workspace import Workspace

    project, base, mbox, _first = posted
    git('branch', 'b4/pa', base, cwd=project)  # A prep branch.
    git('branch', 'b4/review/x', base, cwd=project)  # b4's review branches are not targets.
    workspace = Workspace.from_config(
        {
            'projects': [
                {
                    'id': 'demo',
                    'path': 'projects/demo',
                    'bases': [{'branch': 'me/base', 'upstream': 'master'}],
                }
            ]
        }
    )
    service = BranchService(tmp_path, lambda: [], lambda sid: tmp_path / sid)
    choices = service.apply_choices(workspace, 'demo', str(mbox), 2)
    assert [p.subject for p in choices.patches] == ['mm/foo: name line three', 'mm/bar: add bar']
    assert choices.targets == (('base:demo:me/base', 'me/base'), ('prep:demo:b4/pa', 'b4/pa'))
    assert choices.tips == {'base:demo:me/base': base, 'prep:demo:b4/pa': base}
    result = service.apply(workspace, 'prep:demo:b4/pa', str(mbox), 2, [1], base)
    assert result.ok, result.error
    assert git('log', '-1', '--format=%s', 'b4/pa', cwd=project) == 'mm/foo: name line three'
    assert git('rev-parse', 'me/base', cwd=project) == base
    refused = service.apply(workspace, 'branch:demo:master', str(mbox), 2, [1], base)
    assert not refused.ok
    none = service.apply_choices(workspace, 'nope', str(mbox), 2)
    assert none.problem and not none.targets


def patch_ids(mbox):
    """The posted patches' (message-id, subject): what a Series knows of them."""
    found = []
    for part in mbox.read_text().split('\nFrom ')[1:]:
        subject = re.search(r'^Subject: \[PATCH v2 [1-9]/2\] (.+)$', part, re.M)
        if subject:
            found.append((msgid(part), subject.group(1)))
    return found


def test_a_series_is_found_on_a_base_and_dropped_with_later_work_kept(posted):
    from b4_maintainer.adapters.bases import drop_commits, series_commits
    from b4_maintainer.workspace import MaintainerBase

    project, base, mbox, _first = posted
    applied = apply_series(project, 'me/base', str(mbox), 2, [1, 2], base)
    assert applied.ok, applied.error
    git('worktree', 'add', '-q', str(project.parent / 'wt'), 'me/base', cwd=project)
    write(project.parent / 'wt', 'later.c', 'later\n')
    later = commit(project.parent / 'wt', 'mm: later work')
    git('worktree', 'remove', str(project.parent / 'wt'), cwd=project)
    ids = patch_ids(mbox)
    found = series_commits(project, MaintainerBase('me/base', 'master'), ids)
    assert [c.subject for c in found] == ['mm/foo: name line three', 'mm/bar: add bar']
    before = untouched(project)
    result = drop_commits(project, 'me/base', [c.sha for c in found], later)
    assert result.ok, result.error
    assert untouched(project) == before
    log = git('log', '--format=%s|%an', f'{base}..me/base', cwd=project)
    assert log.splitlines() == ['mm: later work|Example Maintainer']
    assert 'three' not in git('show', 'me/base:mm/foo.c', cwd=project)
    assert series_commits(project, MaintainerBase('me/base', 'master'), ids) == []


def test_a_drop_that_later_work_depends_on_changes_nothing(posted):
    from b4_maintainer.adapters.bases import drop_commits, series_commits
    from b4_maintainer.workspace import MaintainerBase

    project, base, mbox, _first = posted
    assert apply_series(project, 'me/base', str(mbox), 2, [2], base).ok
    git('worktree', 'add', '-q', str(project.parent / 'wt'), 'me/base', cwd=project)
    write(project.parent / 'wt', 'mm/bar.c', 'bar\nmore bar\n')
    tip = commit(project.parent / 'wt', 'mm/bar: extend bar')
    git('worktree', 'remove', str(project.parent / 'wt'), cwd=project)
    found = series_commits(project, MaintainerBase('me/base', 'master'), patch_ids(mbox))
    result = drop_commits(project, 'me/base', [c.sha for c in found], tip)
    assert not result.ok and result.failed == 'mm/bar: extend bar'
    assert git('rev-parse', 'me/base', cwd=project) == tip
    moved = drop_commits(project, 'me/base', [c.sha for c in found], base)
    assert not moved.ok and 'moved' in moved.error


def test_the_service_finds_the_series_on_bases_and_drops_it(posted, tmp_path):
    from b4_maintainer.branches import BranchService
    from b4_maintainer.workspace import Workspace

    project, base, mbox, _first = posted
    workspace = Workspace.from_config(
        {
            'projects': [
                {
                    'id': 'demo',
                    'path': 'projects/demo',
                    'bases': [{'branch': 'me/base', 'upstream': 'master'}],
                }
            ]
        }
    )
    service = BranchService(tmp_path, lambda: [], lambda sid: tmp_path / sid)
    ids = patch_ids(mbox)
    assert service.drop_choices(workspace, 'demo', ids).problem  # Not applied yet.
    applied = service.apply(workspace, 'base:demo:me/base', str(mbox), 2, [1, 2], base)
    choices = service.drop_choices(workspace, 'demo', ids)
    assert choices.targets == (('base:demo:me/base', 'me/base'),)
    assert choices.tips == {'base:demo:me/base': applied.tip}
    shas = [c.sha for c in choices.commits['base:demo:me/base']]
    assert shas == list(applied.applied)
    assert not service.drop(workspace, 'prep:demo:b4/x', shas, applied.tip).ok
    result = service.drop(workspace, 'base:demo:me/base', shas, applied.tip)
    assert result.ok and git('rev-parse', 'me/base^{tree}', cwd=project) == git(
        'rev-parse', f'{base}^{{tree}}', cwd=project
    )


def test_a_conflict_is_marked_and_its_mbox_can_go_to_an_agent(posted, tmp_path):
    from b4_maintainer.adapters.apply import apply_mbox
    from b4_maintainer.adapters.bases import accept_onto
    from b4_maintainer.adapters.sessiontree import prepare_unapplied_tree

    project, _base, mbox, first = posted
    git('checkout', '-q', 'me/base', cwd=project)
    write(project, 'mm/foo.c', (project / 'mm/foo.c').read_text().replace('line 3\n', 'tres\n'))
    tip = commit(project, 'mm/foo: conflicting change')
    git('checkout', '-q', 'master', cwd=project)
    failed = apply_series(project, 'me/base', str(mbox), 2, [1, 2], tip)
    assert not failed.ok and failed.conflict
    # The agent gets the same patches apply would have taken: picked, trailers, a Link.
    ambytes = apply_mbox(project, str(mbox), 2, [1])
    text = ambytes.decode()
    assert 'mm/foo: name line three' in text and 'mm/bar: add bar' not in text
    assert 'Reviewed-by: Rev Iewer' in text and f'Link: https://patch.msgid.link/{first}' in text
    agent = ('b4-maintainer agent (dry-run)', 'agent@b4-maintainer.invalid')
    dest = tmp_path / 'var' / 'worktrees' / 'a1'
    tree = prepare_unapplied_tree(project, dest, 'b4m/session/a1', ambytes, tip, agent)
    am = ['git', '-c', 'user.name=Agent', '-c', 'user.email=agent@b4-maintainer.invalid', 'am']
    mbx = str(dest / '.git' / 'b4m' / 'series.mbx')
    assert subprocess.run([*am, '-q', '-3', mbx], cwd=dest, capture_output=True).returncode
    # The agent's resolution: the patch's intent (line three named) on this base.
    base_text = git('show', f'{tip}:mm/foo.c', cwd=project)
    (dest / 'mm/foo.c').write_text(base_text.replace('tres', 'three') + '\n')
    git('add', 'mm/foo.c', cwd=dest)
    subprocess.run([*am, '--continue'], cwd=dest, check=True, capture_output=True)
    result_tip = git('rev-parse', tree.branch, cwd=dest)
    # Accepted onto the base only while it is still where the session started.
    git('branch', '-f', 'me/other', tip, cwd=project)
    assert not accept_onto(project, 'me/base', dest, tree.branch, 'f' * 40).ok
    accepted = accept_onto(project, 'me/base', dest, tree.branch, tip)
    assert accepted.ok, accepted.error
    assert git('rev-parse', 'me/base', cwd=project) == result_tip
    message = git('log', '-1', '--format=%B', 'me/base', cwd=project)
    assert 'Reviewed-by: Rev Iewer' in message  # git am keeps the trailers.
    assert git('log', '-1', '--format=%an', 'me/base', cwd=project) == 'Con Tributor'
    assert not git('for-each-ref', 'refs/b4m/', cwd=project)  # No scratch refs left.


class FakeHub:
    def __init__(self):
        self.launched = []

    def overview(self):
        return None

    def situation(self, workspace, overview, skills, project, bases=True):
        return 'situation'

    def launch_apply(self, target, branch, expected, ambytes, numbers, detail, situation):
        from sessiondata import make_record

        self.launched.append((target, branch, expected, ambytes, tuple(numbers), detail))
        return make_record('a1', scope='apply')


def test_the_service_hands_a_conflict_to_an_apply_session_and_accepts_it(posted, tmp_path):
    from b4_maintainer.adapters.apply import ApplyResult
    from b4_maintainer.adapters.sessiontree import prepare_unapplied_tree
    from b4_maintainer.branches import BranchService
    from b4_maintainer.sessions.launch import Target
    from b4_maintainer.sessions.records import SessionRecord, Worktree
    from b4_maintainer.workspace import Workspace

    project, base, mbox, _first = posted
    workspace = Workspace.from_config(
        {
            'projects': [
                {
                    'id': 'demo',
                    'path': 'projects/demo',
                    'bases': [{'branch': 'me/base', 'upstream': 'master'}],
                }
            ]
        }
    )
    records: list[SessionRecord] = []
    hub = FakeHub()
    service = BranchService(tmp_path, lambda: records, lambda sid: tmp_path / sid, hub)
    target = Target('series', 'demo', 'id', 2, 'mm: foo and bar', str(mbox))
    failed = ApplyResult(
        False, error='does not apply', output='CONFLICT in mm/foo.c', conflict=True
    )
    record = service.apply_session(workspace, 'base:demo:me/base', target, [1, 2], failed)
    assert record.id == 'a1'
    [(got, branch, expected, ambytes, numbers, detail)] = hub.launched
    assert (got, branch, expected, numbers, detail) == (
        target,
        'me/base',
        base,
        (1, 2),
        'CONFLICT in mm/foo.c',
    )
    assert b'Link: https://patch.msgid.link/' in ambytes
    # The agent applied it in its tree; A accepts the result onto the base.
    agent = ('Agent', 'agent@b4-maintainer.invalid')
    dest = tmp_path / 'var' / 'worktrees' / 'a1'
    tree = prepare_unapplied_tree(project, dest, 'b4m/session/a1', ambytes, base, agent)
    git(
        '-c',
        'user.name=Agent',
        '-c',
        'user.email=a@x',
        'am',
        '-q',
        '-3',
        str(dest / '.git' / 'b4m' / 'series.mbx'),
        cwd=dest,
    )
    records.append(
        record.replace(
            project='demo',
            choices={'base': 'me/base', 'expected': base, 'patches': '1, 2'},
            worktree=tree,
        )
    )
    assert service.snapshot(workspace).rebases.get('tree:a1') == 'a1'  # A applies to it.
    accepted = service.accept(workspace, 'a1')
    assert accepted.ok, accepted.error
    assert git('rev-parse', 'me/base', cwd=project) == git('rev-parse', tree.branch, cwd=dest)
    assert isinstance(tree, Worktree)

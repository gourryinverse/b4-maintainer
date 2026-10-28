"""Contract: one commit as a patch email, as git format-patch makes it. An agent's commit
becomes the maintainer's (Assisted-by, then their Signed-off-by); another author's keeps
a From: line first. A fixup replies to the patch it fixes (In-Reply-To, References; To
its author, Cc the rest, not the maintainer); a pull-ahead is its own thread, addressed
as b4 addresses a new patch."""

import pytest
from gitutil import git

from b4_maintainer.patchmail import ReplyTo, auto_recipients, patch_mail

ME = ('Example Maintainer', 'maintainer@example.org')
AGENT = ('b4-maintainer agent', 'agent@b4-maintainer.invalid')
GET_MAINTAINER = '#!/bin/sh\ncat >/dev/null\necho "Subsystem Maintainer <sub@example.org>"\n'


@pytest.fixture
def repo(tmp_path, home):
    (home / '.gitconfig').write_text(f'[user]\n\tname = {ME[0]}\n\temail = {ME[1]}\n')
    path = tmp_path / 'linux'
    path.mkdir()
    git('init', '-q', '-b', 'main', cwd=path)
    (path / 'scripts').mkdir()
    (path / 'scripts' / 'get_maintainer.pl').write_text(GET_MAINTAINER)
    (path / 'scripts' / 'get_maintainer.pl').chmod(0o755)
    (path / 'mm.c').write_text('old\n')
    git('add', '-A', cwd=path)
    git('commit', '-qm', 'start', cwd=path)
    return path


def commit(repo, author, subject, body='Because.'):
    (repo / 'mm.c').write_text(subject + '\n')
    git('add', '-A', cwd=repo)
    git(
        '-c',
        f'user.name={author[0]}',
        '-c',
        f'user.email={author[1]}',
        'commit',
        '-qm',
        f'{subject}\n\n{body}',
        cwd=repo,
    )
    return git('rev-parse', 'HEAD', cwd=repo)


def test_an_agents_commit_becomes_the_maintainers_patch(repo):
    sha = commit(repo, AGENT, 'mm: fix the leak')
    msg = patch_mail(repo, sha, ME, AGENT[1], 'Claude:opus')
    assert msg['From'] == 'Example Maintainer <maintainer@example.org>'
    assert msg['Subject'] == '[PATCH] mm: fix the leak'
    assert msg['Message-ID'] and not msg['In-Reply-To']
    body = msg.get_content()
    head, _, diff = body.partition('\n---\n')
    assert head.rstrip().endswith(
        'Assisted-by: Claude:opus\nSigned-off-by: Example Maintainer <maintainer@example.org>'
    )
    assert 'agent@b4-maintainer.invalid' not in body
    assert 'diff --git a/mm.c b/mm.c' in diff


def test_a_fixup_replies_to_the_patch_it_fixes(repo):
    sha = commit(repo, AGENT, 'mm/foo: keep the lock')
    reply = ReplyTo(
        msgid='<p3@example.org>',
        references='<cover@example.org> <p3@example.org>',
        to=(('Contributor One', 'contributor1@example.org'),),
        cc=(('Linux MM', 'linux-mm@kvack.org'), ME),
    )
    msg = patch_mail(repo, sha, ME, AGENT[1], 'Claude:opus', reply=reply)
    assert msg['In-Reply-To'] == '<p3@example.org>'
    assert msg['References'] == '<cover@example.org> <p3@example.org>'
    assert msg['To'] == 'Contributor One <contributor1@example.org>'
    assert msg['Cc'] == 'Linux MM <linux-mm@kvack.org>'  # Not the maintainer.


def test_another_authors_commit_keeps_its_author(repo):
    other = ('Other Author', 'other@example.org')
    sha = commit(repo, other, 'mm: their fix')
    msg = patch_mail(repo, sha, ME, AGENT[1], 'Claude:opus')
    body = msg.get_content()
    assert body.startswith('From: Other Author <other@example.org>\n\n')
    assert 'Assisted-by' not in body
    assert 'Signed-off-by: Example Maintainer <maintainer@example.org>' in body


def test_a_pull_ahead_is_addressed_as_b4_addresses_a_new_patch(repo):
    sha = commit(repo, AGENT, 'mm: fix an old bug')
    msg = patch_mail(repo, sha, ME, AGENT[1], 'Claude:opus')
    to, cc = auto_recipients(repo, msg)
    assert to == [('Subsystem Maintainer', 'sub@example.org')]
    msg = patch_mail(repo, sha, ME, AGENT[1], 'Claude:opus', recipients=(to, cc))
    assert msg['To'] == 'Subsystem Maintainer <sub@example.org>' and not msg['In-Reply-To']


def test_b4s_series_recipients_are_used_too(repo):
    git('config', 'b4.send-series-to', 'Some List <list@example.org>', cwd=repo)
    sha = commit(repo, AGENT, 'mm: fix more')
    to, _cc = auto_recipients(repo, patch_mail(repo, sha, ME, AGENT[1], 'x'))
    assert ('Some List', 'list@example.org') in to


def test_a_projects_prefix_tags_the_subject(repo):
    sha = commit(repo, AGENT, 'daxctl: fix it')
    msg = patch_mail(repo, sha, ME, AGENT[1], 'x', prefix='ndctl PATCH')
    assert msg['Subject'] == '[ndctl PATCH] daxctl: fix it'

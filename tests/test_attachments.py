"""Contract: what a series' sessions produced can go into a reply: their commits as inline
patches (git am --scissors), their files as text, at the end of the draft."""

import pytest
from gitutil import git
from sessiondata import make_record

from b4_maintainer.attachments import gather, render
from b4_maintainer.sessions.records import Worktree
from b4_maintainer.sessions.store import SessionStore

ME = ['-c', 'user.name=Example Maintainer', '-c', 'user.email=maintainer@example.org']
AGENT = 'agent@b4-maintainer.invalid'
SERIES = 'change-id:20261020-mm-foo'


def commit(repo, path, text, message, author=None):
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text(text)
    git('add', path, cwd=repo)
    git(*ME, 'commit', '-q', *(['--author', author] if author else []), '-m', message, cwd=repo)
    return git('rev-parse', 'HEAD', cwd=repo)


@pytest.fixture
def produced(tmp_path, home):
    (home / '.gitconfig').write_text(
        '[user]\n\tname = Example Maintainer\n\temail = maintainer@example.org\n'
    )
    tree = tmp_path / 'tree'
    tree.mkdir()
    git('init', '-q', '-b', 'b4m/session/s1', cwd=tree)
    base = commit(tree, 'mm/foo.c', 'one\ntwo\n', 'mm: start')
    applied = commit(tree, 'mm/foo.c', 'one\n2\n', 'mm/foo: the series patch')
    git('switch', '-q', '-c', 'b4m/fixup/two', cwd=tree)
    fixup = commit(
        tree,
        'mm/foo.c',
        'one\n2\nthree\n',
        'fixup! mm/foo: the series patch\n\nAlso handle three.',
        f'b4-maintainer agent (anthropic/a-model-name-long-enough-to-fold) <{AGENT}>',
    )
    git('switch', '-q', 'b4m/session/s1', cwd=tree)
    store = SessionStore(tmp_path / 'var' / 'sessions')
    record = make_record(
        's1',
        series=SERIES,
        model='anthropic/model-x',
        mode='worktree',
        worktree=Worktree(str(tree), 'b4m/session/s1', base, applied),
    )
    store.create(record)
    other = make_record('s2', series='change-id:other')
    store.create(other)
    out = store.directory('s1') / 'work' / 'out'
    (out / 'tests').mkdir(parents=True)
    (out / 'tests' / 'run.log').write_text(''.join(f'line {n}\n' for n in range(1, 501)))
    (out / 'reproducers').mkdir()
    (out / 'reproducers' / 'race.c').write_text('int main(void) { return 0; }\n')
    (out / 'reproducers' / 'race').write_bytes(b'\x7fELF\x00\x00binary')
    (out / 'artifacts.toml').write_text(
        '[[artifact]]\nkind = "results"\ntitle = "selftests on v3"\nsummary = "All pass."\n'
        'path = "out/tests/run.log"\n\n'
        '[[artifact]]\nkind = "finding"\ntitle = "two is wrong"\nsummary = "Line two."\n'
    )
    return store, [record, other], fixup


def test_a_series_sessions_commits_and_files_are_offered(produced):
    store, records, fixup = produced
    found = gather(records, SERIES, store.directory)
    labels = [a.label for a in found]
    assert any('fixup! mm/foo: the series patch' in label for label in labels)
    assert any('results: selftests on v3' in label for label in labels)
    assert any('finding: two is wrong' in label for label in labels)
    assert any('reproducers/race.c' in label for label in labels)
    assert not any(label.endswith('reproducers/race') for label in labels)  # Binary.
    assert not any('run.log' in label and 'results' not in label for label in labels)
    patch = next(a for a in found if a.kind == 'patch')
    assert patch.sha == fixup and patch.session == 's1' and patch.path is None
    assert patch.title == 'fixup! mm/foo: the series patch'


def test_patches_render_for_git_am_and_files_as_text(produced):
    store, records, _fixup = produced
    found = gather(records, SERIES, store.directory)
    patch = next(a for a in found if a.kind == 'patch')
    results = next(a for a in found if 'selftests' in a.label)
    finding = next(a for a in found if 'two is wrong' in a.label)
    me = 'Example Maintainer <maintainer@example.org>'
    text = render([results, finding, patch], AGENT, 'anthropic/model-x', me, max_lines=40)
    assert text.index('selftests on v3') < text.index('-- >8 --')  # Text first, then patches.
    block = text[text.index('-- >8 --') :]
    assert 'From: Example Maintainer <maintainer@example.org>\n' in block  # The agent's, yours.
    assert AGENT not in block  # No folded remainder of the agent's From: line.
    assert 'Subject: [PATCH] fixup! mm/foo: the series patch' in block
    assert 'Assisted-by: anthropic/model-x' in block
    assert '+three' in block and not block.rstrip().endswith('2.')  # No format-patch signature.
    assert 'Mon Sep 17 00:00:00 2001' not in block and 'Date:' not in block
    assert 'line 1\n' in text and 'line 500\n' in text and '… 460 lines cut …' in text
    assert 'Line two.' in text


def test_the_rendered_patch_applies_with_git_am_scissors(produced, tmp_path):
    store, records, fixup = produced
    patch = next(a for a in gather(records, SERIES, store.directory) if a.kind == 'patch')
    me = 'Example Maintainer <maintainer@example.org>'
    reply = (
        'From: Example Maintainer <maintainer@example.org>\n'
        'Subject: Re: [PATCH v3 1/1] mm/foo: the series patch\n'
        'Date: Mon, 26 Oct 2026 12:00:00 +0000\n\n'
        'This needs one more line; the fixup below adds it.\n\n'
        + render([patch], AGENT, 'anthropic/model-x', me)
    )
    mail = tmp_path / 'reply.eml'
    mail.write_text(reply)
    tree = records[0].worktree.path
    git('switch', '-q', '--detach', 'b4m/session/s1', cwd=tree)
    git(*ME, 'am', '-q', '--scissors', str(mail), cwd=tree)
    assert git('log', '-1', '--format=%s|%an', cwd=tree) == (
        'fixup! mm/foo: the series patch|Example Maintainer'
    )
    assert git('show', 'HEAD:mm/foo.c', cwd=tree) == git('show', f'{fixup}:mm/foo.c', cwd=tree)

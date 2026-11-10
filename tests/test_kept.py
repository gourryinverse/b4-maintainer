"""Contract: a kept artifact is a copy under var/kept/ that outlives its session and
workbench. A file in a kind folder's subfolder (out/reproducers/<name>/README.md) keeps
its whole folder. It goes with a commit, found again after a rebase by its patch-id or
subject, or sits on a named shelf; the same source kept again is refreshed, not doubled."""

import datetime

import pytest
from gitutil import git, make_checkout

from b4_maintainer.kept import KeptError, KeptStore, copy_source, describe, patch_id

WHEN = datetime.datetime(2026, 11, 10, 12, 0, tzinfo=datetime.timezone.utc)
ID = ['-c', 'user.name=Example Maintainer', '-c', 'user.email=maintainer@example.org']


@pytest.fixture
def out(tmp_path):
    out = tmp_path / 'session' / 'work' / 'out'
    race = out / 'reproducers' / 'race'
    race.mkdir(parents=True)
    (race / 'README.md').write_text('# race\nrun ./run.sh\n')
    (race / 'race.c').write_text('int main(void) { return 0; }\n')
    (race / 'run.sh').write_text('#!/bin/sh\n')
    (race / 'run.sh').chmod(0o755)
    (out / 'reports').mkdir()
    (out / 'reports' / 'review.md').write_text('review\n')
    return out


def test_a_file_in_a_kind_folders_subfolder_keeps_the_folder(out):
    assert (
        copy_source(out / 'reproducers' / 'race' / 'README.md', out) == out / 'reproducers' / 'race'
    )
    assert copy_source(out / 'reports' / 'review.md', out) == out / 'reports' / 'review.md'
    assert copy_source(out / 'reports' / 'review.md') == out / 'reports' / 'review.md'


def test_kept_on_a_shelf_it_is_copied_and_outlives_its_source(out, tmp_path):
    store = KeptStore(tmp_path / 'var' / 'kept')
    kept = store.keep(
        'reproducer',
        'race',
        'two threads',
        'base: fails',
        out / 'reproducers' / 'race' / 'README.md',
        out,
        shelf='memfd',
        session='s1',
        session_name='review v2',
        workbench='MM fixes',
        now=WHEN,
    )
    assert kept.path is not None and kept.path.is_dir()
    assert (kept.path / 'race.c').read_text().startswith('int main')
    assert (kept.path / 'run.sh').stat().st_mode & 0o111  # Scripts stay runnable.
    (out / 'reproducers' / 'race' / 'race.c').unlink()  # The session goes.
    [listed] = store.items()
    assert listed == kept and (listed.path / 'race.c').is_file()
    assert store.shelves() == ['memfd']
    text = describe(listed)
    assert 'on the shelf memfd' in text and 'race.c' in text and 'run ./run.sh' in text
    assert 'review v2' in text and 'Tested: base: fails' in text


def test_kept_again_it_is_refreshed_and_moves_not_doubled(out, tmp_path):
    store = KeptStore(tmp_path / 'kept')
    path = out / 'reports' / 'review.md'
    store.keep('analysis', 'review', path=path, out=out, shelf='one')
    path.write_text('review, revised\n')
    again = store.keep('analysis', 'review', path=path, out=out, shelf='two')
    [only] = store.items()
    assert only.id == again.id and only.shelf == 'two'
    assert only.path is not None and only.path.read_text() == 'review, revised\n'


def test_it_needs_a_commit_or_a_shelf_and_a_bounded_size(out, tmp_path, monkeypatch):
    store = KeptStore(tmp_path / 'kept')
    with pytest.raises(KeptError):
        store.keep('analysis', 'review', path=out / 'reports' / 'review.md', shelf=' ')
    monkeypatch.setattr('b4_maintainer.kept.MAX_BYTES', 4)
    with pytest.raises(KeptError, match='over'):
        store.keep('analysis', 'review', path=out / 'reports' / 'review.md', shelf='s')
    assert store.items() == []


def test_a_finding_without_a_file_keeps_its_text(tmp_path):
    store = KeptStore(tmp_path / 'kept')
    kept = store.keep('finding', 'double charge', 'the loser charges twice', shelf='s')
    assert kept.path is None and 'the loser charges twice' in describe(store.items()[0])


def test_removed_it_is_gone(out, tmp_path):
    store = KeptStore(tmp_path / 'kept')
    kept = store.keep('analysis', 'review', path=out / 'reports' / 'review.md', shelf='s')
    assert not store.remove('../elsewhere') and store.remove(kept.id)
    assert store.items() == [] and not (tmp_path / 'kept' / kept.id).exists()


def commit(repo, name, text, subject):
    (repo / name).write_text(text)
    git('add', name, cwd=repo)
    git(*ID, 'commit', '-q', '-m', subject, cwd=repo)
    return git('rev-parse', 'HEAD', cwd=repo)


def test_a_commits_kept_artifacts_are_found_after_a_rebase(out, tmp_path):
    repo = make_checkout(tmp_path / 'linux')
    fix = commit(repo, 'memfd.c', 'recheck\n', 'mm/memfd: recheck the page cache')
    store = KeptStore(tmp_path / 'kept')
    store.keep(
        'reproducer',
        'race',
        path=out / 'reproducers' / 'race' / 'README.md',
        out=out,
        project='linux',
        commit=fix,
        subject='mm/memfd: recheck the page cache',
        patch_id=patch_id(repo, fix),
    )
    store.keep('analysis', 'review', path=out / 'reports' / 'review.md', shelf='elsewhere')
    assert [k.title for k in store.with_commit(repo, fix)] == ['race']
    # Rebased onto new upstream work: a new sha, the same change.
    git('checkout', '-q', '-b', 'upstream', 'HEAD~1', cwd=repo)
    commit(repo, 'other.c', 'other\n', 'mm: unrelated upstream change')
    git(*ID, 'cherry-pick', fix, cwd=repo)
    moved = git('rev-parse', 'HEAD', cwd=repo)
    assert moved != fix and [k.title for k in store.with_commit(repo, moved)] == ['race']
    # Reworded but the same patch: still found; another change is not.
    git(*ID, 'commit', '-q', '--amend', '-m', 'mm/memfd: recheck under the mutex', cwd=repo)
    assert [k.title for k in store.with_commit(repo, git('rev-parse', 'HEAD', cwd=repo))] == [
        'race'
    ]
    other = commit(repo, 'third.c', 'x\n', 'mm: something else')
    assert store.with_commit(repo, other) == []
    assert store.keep('analysis', 'one', shelf='x').where() == 'on the shelf x'

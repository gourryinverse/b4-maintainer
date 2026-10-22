"""Contract: tree evidence from commit trailers and subjects, as b4 ty matches them."""

import datetime

from gitutil import git

from b4_maintainer.adapters.treematch import RefCache, message_ids
from b4_maintainer.fixtures import make_series
from b4_maintainer.model import Authority, Coverage, Patch
from b4_maintainer.workspace import Ref

NOW = datetime.datetime.now(datetime.timezone.utc)
IDENTITY = ['-c', 'user.name=Example Maintainer', '-c', 'user.email=maintainer@example.org']
MAIN = Ref('m', 'master', Authority.MAINLINE)
UNSTABLE = Ref('u', 'unstable', Authority.PRELIMINARY)
NEW = Ref('a', 'new', Authority.PRELIMINARY)


def commit(repo, subject, *trailers):
    message = subject + '\n\nBody.\n' + (('\n' + '\n'.join(trailers) + '\n') if trailers else '')
    git(*IDENTITY, 'commit', '-q', '--allow-empty', '-m', message, cwd=repo)


def repo(tmp_path):
    path = tmp_path / 'linux'
    path.mkdir()
    git('init', '-q', '-b', 'master', cwd=path)
    commit(path, 'base')
    commit(path, 'mm: already upstream', 'Link: https://lore.kernel.org/r/upstream-1@example.org')
    git('checkout', '-q', '-b', 'unstable', cwd=path)
    commit(path, 'mm: first patch', 'Link: https://lore.kernel.org/p1@example.org')
    commit(path, 'mm: second patch', 'Link: https://lore.kernel.org/all/p2%40example.org/')
    git('checkout', '-q', '-b', 'new', cwd=path)
    commit(path, 'mm: picked by subject only')
    commit(path, 'mm: newest', 'Message-ID: <p9@example.org>')
    git('checkout', '-q', 'master', cwd=path)
    return path


def series(*patches):
    base = make_series('mm: series', msgid='cover@example.org')
    return base.__class__(
        **{
            **base.__dict__,
            'patches': tuple(
                Patch(n, msgid, subject) for n, (msgid, subject) in enumerate(patches, 1)
            ),
        }
    )


def test_message_ids_from_link_and_message_id_trailers():
    body = (
        'Subject\n\nLink: https://lore.kernel.org/r/a@example.org\n'
        'Link: https://lore.kernel.org/all/b%40example.org/\n'
        'Link: https://patch.msgid.link/c@example.org\n'
        'Message-ID: <d@example.org>\n'
        'Link: https://sashiko.dev/#/patchset/123 [1]\n'
    )
    assert message_ids(body) == {'a@example.org', 'b@example.org', 'c@example.org', 'd@example.org'}


def test_coverage_per_ref(tmp_path):
    path = repo(tmp_path)
    cache = RefCache(tmp_path / 'refs.json', path)
    assert cache.update([MAIN, UNSTABLE, NEW], since=NOW - datetime.timedelta(days=30)) == 3
    both = series(('p1@example.org', 'mm: first patch'), ('p2@example.org', 'mm: second patch'))
    assert cache.coverage(both, UNSTABLE) == Coverage.FULL
    assert cache.coverage(both, NEW) == Coverage.FULL  # new contains unstable
    assert cache.coverage(both, MAIN) == Coverage.NONE
    partial = series(('p1@example.org', 'mm: first patch'), ('px@example.org', 'mm: not merged'))
    assert cache.coverage(partial, UNSTABLE) == Coverage.PARTIAL
    by_subject = series(('other@example.org', 'mm: picked by subject only'))
    assert cache.coverage(by_subject, NEW) == Coverage.FULL
    assert cache.coverage(by_subject, UNSTABLE) == Coverage.NONE
    upstream = series(('upstream-1@example.org', 'mm: already upstream'))
    assert cache.coverage(upstream, MAIN) == Coverage.FULL
    # Beyond-mainline ranges exclude what mainline already has.
    assert cache.coverage(upstream, UNSTABLE) == Coverage.NONE


def test_unchanged_tips_are_not_rescanned_and_moved_tips_are_stale(tmp_path):
    path = repo(tmp_path)
    since = NOW - datetime.timedelta(days=30)
    cache = RefCache(tmp_path / 'refs.json', path)
    cache.update([MAIN, UNSTABLE], since=since)
    again = RefCache(tmp_path / 'refs.json', path)
    assert again.update([MAIN, UNSTABLE], since=since) == 0
    git('checkout', '-q', 'unstable', cwd=path)
    commit(path, 'mm: late addition', 'Link: https://lore.kernel.org/late@example.org')
    late = series(('late@example.org', 'mm: late addition'))
    fresh = RefCache(tmp_path / 'refs.json', path)
    assert fresh.coverage(late, UNSTABLE) == Coverage.STALE
    assert fresh.update([MAIN, UNSTABLE], since=since) == 1
    assert fresh.coverage(late, UNSTABLE) == Coverage.FULL


def test_missing_ref_and_metadata_only_series_are_unchecked(tmp_path):
    path = repo(tmp_path)
    cache = RefCache(tmp_path / 'refs.json', path)
    gone = Ref('x', 'no/such-ref', Authority.PRELIMINARY)
    cache.update([MAIN, gone], since=NOW - datetime.timedelta(days=30))
    s = series(('p1@example.org', 'mm: first patch'))
    assert cache.coverage(s, gone) == Coverage.UNCHECKED
    assert cache.coverage(series(), MAIN) == Coverage.UNCHECKED
